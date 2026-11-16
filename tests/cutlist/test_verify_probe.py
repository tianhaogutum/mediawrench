"""Saved evidence contracts: controlled probe doubles plus real ffprobe replay."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.cutlist import build_probed_timeline, read_srt, render_outputs
from .test_probe import FFPROBE, ROOT, write_wav


class VerifyFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.media = self.directory / '-短 clip.wav'
        write_wav(self.media)
        self.srt = self.directory / 'cues.srt'
        self.srt.write_text('1\n00:00:00,100 --> 00:00:02,400\nkeep\n', encoding='utf-8')
        self.saved = self.directory / 'cuts.provenance.json'

    def record(self, probe, frame_rate=None):
        plan = {'keep': [1], 'padding': 0.2}
        if frame_rate:
            plan['frameRate'] = frame_rate
        timeline = build_probed_timeline(read_srt(self.srt), plan,
                                         media_path=self.media, ffprobe_path=probe)
        contents = render_outputs(timeline, self.media.name)
        for key, suffix in [('llc', '.llc'), ('csv', '.csv'), ('provenance', '.provenance.json')]:
            (self.directory / ('cuts' + suffix)).write_text(contents[key], encoding='utf-8')
        self.document = json.loads(contents['provenance'])

    def save(self):
        self.saved.write_text(json.dumps(self.document, ensure_ascii=False), encoding='utf-8')

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist',
                               'verify-probe-evidence', str(self.saved), *extra],
                              cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=120)

    def verify(self):
        from mediawrench.cutlist import verify_probe_evidence
        return verify_probe_evidence(self.saved)

    def assert_stale(self, report, code, replayed=False):
        self.assertEqual(report['status'], 'stale')
        self.assertIs(report['stale'], True)
        self.assertEqual(report['replayed'], replayed)
        self.assertIn(code, [issue['code'] for issue in report['issues']])


class VerifyProbeTests(VerifyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Explicit test double: mutable response/control files allow replay
        # failures while executable and media bytes remain unchanged.
        self.response = self.directory / 'response.json'
        self.response.write_text(json.dumps({'program_version': {'version': 'test-double'},
                                            'format': {'duration': '2.500000'}}))
        self.calls = self.directory / 'calls.jsonl'
        self.probe = self.directory / 'probe-double'
        self.probe.write_text(
            f'#!{sys.executable}\n'
            'import pathlib, sys\n'
            f'root = pathlib.Path({str(self.directory)!r})\n'
            'with (root / "calls.jsonl").open("a") as log:\n'
            '    import json; log.write(json.dumps(sys.argv) + "\\n")\n'
            'sys.stdout.buffer.write((root / "response.json").read_bytes())\n'
            'sys.stderr.write((root / "stderr").read_text() if (root / "stderr").exists() else "")\n'
            'sys.exit(int((root / "exit").read_text()) if (root / "exit").exists() else 0)\n')
        self.probe.chmod(0o755)
        self.record(self.probe)
        self.calls.unlink()

    def test_cli_success_exact_argv_read_only_and_no_path_search(self):
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        with patch.dict(os.environ, {'PATH': ''}):
            result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'verified')
        self.assertIs(report['stale'], False)
        self.assertIs(report['replayed'], True)
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['durationMs'], 2500)
        self.assertEqual(json.loads(self.calls.read_text()), self.document['durationProbe']['argv'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(set(self.directory.iterdir()), set(before) | {self.calls})

    def test_media_change_same_size_and_duration_stops_before_replay(self):
        data = bytearray(self.media.read_bytes())
        data[-1] = 1
        self.media.write_bytes(data)
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assert_stale(json.loads(result.stdout), 'media-sha256')
        self.assertFalse(self.calls.exists())

    def test_executable_change_stops_before_replay(self):
        with self.probe.open('a') as probe:
            probe.write('# changed\n')
        self.assert_stale(self.verify(), 'ffprobe-sha256')
        self.assertFalse(self.calls.exists())

    def test_missing_nonregular_or_nonexecutable_inputs_are_stale(self):
        for kind in ['missing-media', 'directory-media', 'missing-probe', 'nonexecutable-probe']:
            with self.subTest(kind=kind):
                if 'media' in kind:
                    self.media.unlink()
                    if kind == 'directory-media':
                        self.media.mkdir()
                elif kind == 'missing-probe':
                    self.probe.rename(self.directory / 'backup')
                else:
                    self.probe.chmod(0o644)
                self.assert_stale(self.verify(), 'media-unavailable' if 'media' in kind else 'ffprobe-unavailable')
                self.assertFalse(self.calls.exists())
                if 'media' in kind:
                    if self.media.is_dir():
                        self.media.rmdir()
                    write_wav(self.media)
                elif kind == 'missing-probe':
                    (self.directory / 'backup').rename(self.probe)
                else:
                    self.probe.chmod(0o755)

    def test_recorded_stdout_and_duration_must_be_internally_consistent(self):
        original = copy.deepcopy(self.document)
        for field, value, code in [
            ('stdoutSha256', '0' * 64, 'recorded-stdout-sha256'),
            ('durationMs', 2499, 'recorded-duration'),
            ('durationSeconds', '2.500001', 'recorded-duration'),
            ('version', 'changed', 'recorded-version'),
        ]:
            with self.subTest(field=field):
                self.document = copy.deepcopy(original)
                self.document['durationProbe'][field] = value
                self.save()
                self.assert_stale(self.verify(), code)
                self.assertFalse(self.calls.exists())
        self.document = copy.deepcopy(original)
        self.document['normalizedSelection']['durationMs'] = 2501
        self.save()
        self.assert_stale(self.verify(), 'timeline-duration')
        self.assertFalse(self.calls.exists())

    def test_saved_argv_is_validated_before_any_execution(self):
        from mediawrench.cutlist import CutlistError
        original = copy.deepcopy(self.document['durationProbe']['argv'])
        variants = [original + ['-show_streams'], ['/bin/sh', '-c', 'anything'],
                    [*original[:-1], 'https://example.invalid/media'],
                    [x if x != 'file' else 'file,http' for x in original]]
        for argv in variants:
            with self.subTest(argv=argv):
                self.document['durationProbe']['argv'] = argv
                self.save()
                with self.assertRaisesRegex(CutlistError, 'argv'):
                    self.verify()
                self.assertFalse(self.calls.exists())

    def test_replay_changed_duration_and_even_same_floor_are_stale(self):
        for duration in ['2.600000', '2.500500']:
            with self.subTest(duration=duration):
                response = {'program_version': {'version': 'test-double'}, 'format': {'duration': duration}}
                self.response.write_text(json.dumps(response))
                report = self.verify()
                self.assert_stale(report, 'replay-duration', replayed=True)
                self.assertIn('replay-stdout-sha256', [x['code'] for x in report['issues']])

    def test_identical_duration_with_changed_raw_output_is_stale(self):
        self.response.write_text(self.response.read_text() + '\n')
        self.assert_stale(self.verify(), 'replay-stdout-sha256', replayed=True)

    def test_failed_invalid_and_stderr_changed_replays_are_stale(self):
        (self.directory / 'exit').write_text('9')
        self.assert_stale(self.verify(), 'replay-failed', replayed=True)
        (self.directory / 'exit').unlink()
        for raw in [b'\xff', b'{}', b'{"format":{"duration":"N/A"}}',
                    b'{"format":{"duration":"2.500000"},"program_version":{}}',
                    b'{"format":{},"format":{}}', b'{"format":{"duration":NaN}}']:
            with self.subTest(raw=raw):
                self.response.write_bytes(raw)
                self.assert_stale(self.verify(), 'replay-invalid', replayed=True)
        self.response.write_bytes(self.document['durationProbe']['stdout'].encode())
        (self.directory / 'stderr').write_text('new warning')
        self.assert_stale(self.verify(), 'replay-stderr', replayed=True)

    def test_invalid_recorded_response_is_stale_without_replay(self):
        self.document['durationProbe']['stdout'] = '{}'
        self.save()
        self.assert_stale(self.verify(), 'recorded-response-invalid')
        self.assertFalse(self.calls.exists())

    def test_replay_version_change_is_reported(self):
        response = json.loads(self.response.read_text())
        response['program_version']['version'] = 'different'
        self.response.write_text(json.dumps(response))
        self.assert_stale(self.verify(), 'replay-version', replayed=True)

    def test_timeout_and_execution_failure_never_verify(self):
        for error, code in [(subprocess.TimeoutExpired(['probe'], 15), 'replay-timeout'),
                            (OSError('cannot execute'), 'replay-failed')]:
            with self.subTest(error=error), patch('mediawrench.cutlist.probe.subprocess.run', side_effect=error):
                self.assert_stale(self.verify(), code, replayed=True)

    def test_invalid_documents_exit_two_without_replay(self):
        original = copy.deepcopy(self.document)
        invalid = [[], {}, {**original, 'schemaVersion': 3},
                   {**original, 'durationProbe': None},
                   {**original, 'durationProbe': {**original['durationProbe'], 'schemaVersion': True}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'durationMs': True}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'stdout': None}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'durationSeconds': 'N/A'}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'mediaPath': 'relative.wav'}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'ffprobeSha256': 'bad'}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'rounding': 'nearest'}},
                   {**original, 'durationProbe': {**original['durationProbe'], 'returnCode': 1}}]
        for document in invalid:
            with self.subTest(document=document):
                self.saved.write_text(json.dumps(document))
                result = self.cli()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, '')
                self.assertIn('mediawrench cutlist:', result.stderr)
                self.assertFalse(self.calls.exists())
        for raw in ['{', '{"schemaVersion":1,"schemaVersion":1}', '{"a":NaN}', '\ud800']:
            self.saved.write_bytes(raw.encode('utf-8', errors='surrogatepass'))
            self.assertEqual(self.cli().returncode, 2)
        self.saved.unlink()
        self.assertEqual(self.cli().returncode, 2)

    def source_pair(self):
        self.plan = self.directory / 'plan.json'
        self.plan.write_text('{"keep": [1], "padding": 0.2}', encoding='utf-8')
        self.document['source'] = {
            'srtFileName': self.srt.name, 'planFileName': self.plan.name,
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        self.save()
        return ['--srt', str(self.srt), '--plan', str(self.plan)]

    def test_source_pair_cli_unchanged_is_read_only_and_uses_explicit_paths(self):
        self.source_pair()
        # Names are descriptive, not inferred paths or constraints.
        moved = self.directory / 'renamed source.srt'
        self.srt.rename(moved)
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        result = self.cli('--srt', str(moved), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'verified')
        self.assertIs(report['replayed'], True)
        self.assertEqual(report['issues'], [])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(json.loads(self.calls.read_text()), self.document['durationProbe']['argv'])

    def test_source_pair_library_and_relative_cli_paths(self):
        self.source_pair()
        from mediawrench.cutlist import verify_probe_evidence
        report = verify_probe_evidence(self.saved, srt_path=self.srt, plan_path=str(self.plan))
        self.assertEqual(report['status'], 'verified')
        self.calls.unlink()
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist',
                                 'verify-probe-evidence', self.saved.name,
                                 '--srt', self.srt.name, '--plan', self.plan.name],
                                cwd=self.directory, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)['replayed'], True)

    def test_source_same_size_srt_and_plan_edits_are_stale_before_replay(self):
        options = self.source_pair()
        for kind, path, old, new in [('srt', self.srt, b'keep', b'edit'),
                                      ('plan', self.plan, b'0.2', b'0.1')]:
            with self.subTest(kind=kind):
                original = path.read_bytes()
                path.write_bytes(original.replace(old, new))
                self.assertEqual(path.stat().st_size, len(original))
                result = self.cli(*options)
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assert_stale(report, kind + '-sha256')
                issue = next(x for x in report['issues'] if x['code'] == kind + '-sha256')
                self.assertEqual(issue['expected'], self.document['source'][kind + 'Sha256'])
                self.assertEqual(issue['actual'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertFalse(self.calls.exists())
                path.write_bytes(original)

    def test_source_missing_and_nonregular_are_stale_before_replay(self):
        options = self.source_pair()
        for kind, path in [('srt', self.srt), ('plan', self.plan)]:
            for state in ['missing', 'directory']:
                with self.subTest(kind=kind, state=state):
                    original = path.read_bytes()
                    path.unlink()
                    if state == 'directory':
                        path.mkdir()
                    result = self.cli(*options)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assert_stale(json.loads(result.stdout), kind + '-unavailable')
                    self.assertFalse(self.calls.exists())
                    if state == 'directory':
                        path.rmdir()
                    path.write_bytes(original)

    def test_source_unreadable_is_stale_and_both_changes_are_reported(self):
        self.source_pair()
        from mediawrench.cutlist import verify_probe_evidence
        # Deterministic permission failure even on hosts running tests as root.
        from mediawrench.cutlist.probe import _sha256
        def unreadable(path):
            if path == self.srt:
                raise PermissionError('source read denied')
            return _sha256(path)
        self.plan.write_text('changed')
        with patch('mediawrench.cutlist.probe._sha256', side_effect=unreadable):
            report = verify_probe_evidence(self.saved, srt_path=self.srt, plan_path=self.plan)
        self.assert_stale(report, 'srt-unavailable')
        self.assert_stale(report, 'plan-sha256')
        self.assertFalse(self.calls.exists())

    def test_source_invalid_saved_digests_are_input_errors_before_file_checks(self):
        from mediawrench.cutlist import CutlistError, verify_probe_evidence
        options = self.source_pair()
        original = copy.deepcopy(self.document)
        self.srt.unlink()
        for key in ['srtSha256', 'planSha256']:
            for value in [None, 1, True, '', 'bad', 'A' * 64, 'g' * 64, '0' * 63, '0' * 65, '0' * 64 + '\n']:
                with self.subTest(key=key, value=value):
                    self.document = copy.deepcopy(original)
                    if value is None:
                        del self.document['source'][key]
                    else:
                        self.document['source'][key] = value
                    self.save()
                    with self.assertRaisesRegex(CutlistError, 'source.' + key):
                        verify_probe_evidence(self.saved, srt_path=self.srt, plan_path=self.plan)
                    result = self.cli(*options)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('source.' + key, result.stderr)
                    self.assertFalse(self.calls.exists())

    def test_source_missing_or_invalid_object_is_input_error_only_when_opted_in(self):
        options = self.source_pair()
        for value in [None, [], 'invalid', {}]:
            with self.subTest(source=value):
                self.document['source'] = value
                self.save()
                result = self.cli(*options)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('source', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse(self.calls.exists())
        del self.document['source']
        self.save()
        self.assertEqual(self.cli(*options).returncode, 2)
        self.assertFalse(self.calls.exists())
        self.srt.unlink()
        self.plan.unlink()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)['replayed'], True)

    def test_source_incomplete_option_pairs_are_explicit_input_errors(self):
        from mediawrench.cutlist import CutlistError, verify_probe_evidence
        self.source_pair()
        for option, path in [('--srt', self.srt), ('--plan', self.plan)]:
            with self.subTest(option=option):
                result = self.cli(option, str(path))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, '')
                self.assertIn('--srt and --plan must be supplied together', result.stderr)
                kwargs = {'srt_path' if option == '--srt' else 'plan_path': path}
                with self.assertRaisesRegex(CutlistError, 'srt_path and plan_path must be supplied together'):
                    verify_probe_evidence(self.saved, **kwargs)
                self.assertFalse(self.calls.exists())

    def test_source_checks_hash_bytes_without_parsing_or_reapplying_plan(self):
        options = self.source_pair()
        self.plan.write_bytes(b'not a JSON plan')
        self.srt.write_bytes(b'not an SRT')
        for kind, path in [('srt', self.srt), ('plan', self.plan)]:
            self.document['source'][kind + 'Sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.save()
        result = self.cli(*options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'verified')

    def test_source_opt_out_ignores_invalid_digests_and_changed_sources(self):
        self.source_pair()
        self.document['source'] = {'srtSha256': 'bad', 'planSha256': None}
        self.save()
        self.srt.unlink()
        self.plan.write_text('changed')
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)['replayed'], True)

    def test_help_documents_exit_codes_without_replay(self):
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist',
                                 'verify-probe-evidence', '--help'], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0)
        self.assertIn('1 stale', result.stdout)
        self.assertFalse(self.calls.exists())

    def test_verify_rejects_export_options(self):
        result = self.cli('--ffprobe', str(self.probe))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls.exists())


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real ffprobe integration')
class RealVerifyTests(VerifyFixture, unittest.TestCase):
    def test_saved_millisecond_and_frame_provenance_verify_then_media_goes_stale(self):
        for rate in [None, '30000/1001']:
            with self.subTest(rate=rate):
                write_wav(self.media)
                self.record(FFPROBE, rate)
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                result = self.cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['durationMs'], 2500)
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
                write_wav(self.media, frames=24000)
                result = self.cli()
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assert_stale(report, 'media-sha256')
                self.assertIn('media-size', [x['code'] for x in report['issues']])


if __name__ == '__main__':
    unittest.main()
