"""Opt-in selected-stream origin: real fixtures and controlled negative cases."""

import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, build_probed_timeline, read_srt, render_outputs, verify_probe_evidence
from .test_probe import FFPROBE, ROOT, ProbeFixture


class OriginFixture(ProbeFixture):
    def setUp(self):
        super().setUp()
        self.probe = self.directory / 'probe-double'
        self.response_file = self.directory / 'response.json'
        self.calls = self.directory / 'calls'
        self.probe.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                              'import sys, json\n'
                              f'Path({str(self.calls)!r}).write_text(json.dumps(sys.argv))\n'
                              f'sys.stdout.buffer.write(Path({str(self.response_file)!r}).read_bytes())\n')
        self.probe.chmod(0o755)
        self.response = {'program_version': {'version': 'test-double'},
                         'format': {'duration': '2.500000'},
                         'streams': [{'index': 2, 'codec_type': 'video',
                                      'avg_frame_rate': '24/1', 'r_frame_rate': '24/1',
                                      'start_time': '0.000000'}]}
        self.selection = {'keep': [1], 'frameRate': '48/2'}
        self.set_plan(self.selection)
        self.saved = self.directory / 'saved.provenance.json'

    def build(self, **kwargs):
        self.raw = json.dumps(self.response).encode()
        with patch('mediawrench.cutlist.probe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, self.raw, b'')) as run:
            timeline = build_probed_timeline(read_srt(self.srt), self.selection,
                                             media_path=self.media, ffprobe_path=self.probe,
                                             **{'video_stream_index': 2, 'verify_zero_origin': True, **kwargs})
        self.argv = run.call_args.args[0]
        return timeline

    def record(self):
        timeline = self.build()
        self.document = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        self.saved.write_text(json.dumps(self.document))
        return copy.deepcopy(self.document)

    def cli_verify(self):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-probe-evidence', str(self.saved)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)


class OriginProbeTests(OriginFixture, unittest.TestCase):
    def test_exact_zero_and_schema3_query_summary(self):
        for value in ['0', '0.000000', '-0.000', '+0.0', '00.000000000000000000000']:
            with self.subTest(value=value):
                self.response['streams'][0]['start_time'] = value
                evidence = json.loads(self.build().duration_probe_json)
                self.assertEqual(evidence['schemaVersion'], 3)
                self.assertEqual(evidence['zeroOriginValidation'], {
                    'streamIndex': 2, 'startTimeSeconds': value, 'policy': 'exact-zero-stream-start-time'})
                self.assertEqual(evidence['frameRateValidation']['streamIndex'], 2)
                self.assertIn('r_frame_rate,start_time', self.argv[self.argv.index('-show_entries') + 1])
                self.assertEqual(self.argv[self.argv.index('-select_streams') + 1], '2')
                self.assertNotIn('-show_frames', self.argv)

    def test_unknown_malformed_and_nonzero_origins_fail_cli_without_outputs_including_dry_run(self):
        self.set_plan({**self.selection, 'duration': 2.5})
        for value in [None, '', 'N/A', 'NaN', 'Infinity', '0/1', '0e0', ' 0', '0.', '.0', 0, False, {},
                      '0' * 65, '1.000000', '-1.0', '0.000000000000000000000000000001']:
            self.response['streams'][0]['start_time'] = value
            self.response_file.write_text(json.dumps(self.response))
            for extra in [[], ['--dry-run']]:
                with self.subTest(value=value, extra=extra):
                    result = self.run_cli(self.probe, '--verify-frame-rate-stream', '2', '--verify-zero-origin', *extra)
                    self.assert_failure(result, 'start_time')
                    self.assertEqual(result.stdout, '')
        del self.response['streams'][0]['start_time']
        self.response_file.write_text(json.dumps(self.response))
        self.assert_failure(self.run_cli(self.probe, '--verify-frame-rate-stream', '2', '--verify-zero-origin'), 'start_time')

    def test_dependencies_and_boolean_library_flag_fail_before_probe(self):
        for kwargs in [{'video_stream_index': None}, {'verify_zero_origin': 1}, {'verify_zero_origin': 'yes'}]:
            with self.subTest(kwargs=kwargs), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                with self.assertRaisesRegex(CutlistError, 'verify_zero_origin'):
                    self.build(**kwargs)
                run.assert_not_called()
        for probe, extra, diagnostic in [
            (None, [], 'requires --ffprobe and --verify-frame-rate-stream'),
            (self.probe, [], 'requires --ffprobe and --verify-frame-rate-stream'),
            (None, ['--verify-frame-rate-stream', '2'], 'requires --ffprobe and --verify-frame-rate-stream')]:
            with self.subTest(extra=extra, probe=probe):
                self.assert_failure(self.run_cli(probe, '--verify-zero-origin', *extra), diagnostic)
                self.assertFalse(self.calls.exists())
        self.set_plan({'keep': [1]})
        self.assert_failure(self.run_cli(self.probe, '--verify-frame-rate-stream', '2', '--verify-zero-origin'), 'frameRate')
        self.assertFalse(self.calls.exists())
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', str(self.srt), '--query', 'keep', '--verify-zero-origin'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('discovery only', result.stderr)

    def test_zero_origin_does_not_bypass_video_selection_or_either_rate(self):
        for field, value in [('index', 0), ('index', True), ('codec_type', 'audio'),
                             ('avg_frame_rate', '25/1'), ('r_frame_rate', '25/1')]:
            original = copy.deepcopy(self.response)
            self.response['streams'][0][field] = value
            with self.subTest(field=field), self.assertRaises(CutlistError):
                self.build()
            self.response = original
        self.response['streams'] *= 2
        with self.assertRaises(CutlistError):
            self.build()

    def test_opt_out_keeps_schema2_query_and_ignores_origin(self):
        self.response['streams'][0]['start_time'] = '1.000000'
        evidence = json.loads(self.build(verify_zero_origin=False).duration_probe_json)
        self.assertEqual(evidence['schemaVersion'], 2)
        self.assertNotIn('zeroOriginValidation', evidence)
        self.assertNotIn('start_time', evidence['argv'][evidence['argv'].index('-show_entries') + 1])

    def test_schema3_replays_exact_query_and_source_pair_still_checked(self):
        self.record()
        self.response_file.write_bytes(self.raw)
        result = self.cli_verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'verified')
        self.assertEqual(json.loads(self.calls.read_text()), self.argv)
        self.calls.unlink()
        # Source preflight remains ahead of any schema3 replay.
        self.document['source'] = {'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
                                   'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        self.saved.write_text(json.dumps(self.document))
        self.srt.write_text(self.srt.read_text().replace('keep', 'edit'))
        report = verify_probe_evidence(self.saved, srt_path=self.srt, plan_path=self.plan)
        self.assertFalse(report['replayed'])
        self.assertIn('srt-sha256', [x['code'] for x in report['issues']])
        self.assertFalse(self.calls.exists())

    def test_saved_origin_shape_schema_policy_index_and_query_invalid_before_replay(self):
        original = self.record()
        for mutation in ['missing', 'policy', 'index', 'bool-index', 'type', 'malformed', 'schema2', 'schema1', 'argv', 'missing-grid-origin', 'bool-grid-origin']:
            self.document = copy.deepcopy(original)
            evidence = self.document['durationProbe']
            origin = evidence['zeroOriginValidation']
            if mutation == 'missing':
                del evidence['zeroOriginValidation']
            elif mutation == 'policy':
                origin['policy'] = 'near-zero'
            elif mutation in ['index', 'bool-index']:
                origin['streamIndex'] = 0 if mutation == 'index' else True
            elif mutation in ['type', 'malformed']:
                origin['startTimeSeconds'] = 0 if mutation == 'type' else 'N/A'
            elif mutation == 'missing-grid-origin':
                del self.document['quantization']['originSeconds']
            elif mutation == 'bool-grid-origin':
                self.document['quantization']['originSeconds'] = False
            elif mutation.startswith('schema'):
                evidence['schemaVersion'] = int(mutation[-1])
            else:
                evidence['argv'] += ['-show_frames']
            self.saved.write_text(json.dumps(self.document))
            with self.subTest(mutation=mutation):
                result = self.cli_verify()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertFalse(result.stdout)
                self.assertFalse(self.calls.exists())

    def test_saved_origin_summary_or_raw_inconsistency_is_stale_without_replay(self):
        original = self.record()
        for mutation in ['summary-nonzero', 'summary-zero-spelling', 'raw-nonzero', 'raw-missing', 'both-nonzero', 'grid-origin']:
            self.document = copy.deepcopy(original)
            evidence = self.document['durationProbe']
            if mutation in ['summary-nonzero', 'both-nonzero']:
                evidence['zeroOriginValidation']['startTimeSeconds'] = '1.000000'
            elif mutation == 'grid-origin':
                self.document['quantization']['originSeconds'] = 1
            elif mutation == 'summary-zero-spelling':
                evidence['zeroOriginValidation']['startTimeSeconds'] = '0'
            if mutation.startswith('raw') or mutation == 'both-nonzero':
                response = json.loads(evidence['stdout'])
                if mutation == 'raw-missing':
                    del response['streams'][0]['start_time']
                else:
                    response['streams'][0]['start_time'] = '1.000000'
                evidence['stdout'] = json.dumps(response)
                evidence['stdoutSha256'] = hashlib.sha256(evidence['stdout'].encode()).hexdigest()
            self.saved.write_text(json.dumps(self.document))
            with self.subTest(mutation=mutation):
                result = self.cli_verify()
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['replayed'])
                self.assertIn('recorded-zero-origin', [x['code'] for x in report['issues']])
                self.assertFalse(self.calls.exists())

    def test_replay_changed_nonzero_unknown_or_equivalent_origin_is_stale(self):
        self.record()
        for value in ['1.000000', '-0.000001', None, 'N/A', '0']:
            self.response['streams'][0]['start_time'] = value
            self.response_file.write_text(json.dumps(self.response))
            with self.subTest(value=value):
                result = self.cli_verify()
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(report['replayed'])
                self.assertIn('replay-zero-origin', [x['code'] for x in report['issues']])

    def test_schema3_invalid_raw_recorded_and_replayed_is_stale(self):
        original = self.record()
        for raw in [b'{', b'{}', b'[]', b'\xff', b'\xed\xa0\x80']:
            with self.subTest(raw=raw):
                document = copy.deepcopy(original)
                document['durationProbe']['stdout'] = raw.decode('utf-8', errors='surrogateescape')
                self.saved.write_text(json.dumps(document))
                with patch('mediawrench.cutlist.probe.subprocess.run') as run:
                    report = verify_probe_evidence(self.saved)
                    self.assertFalse(report['replayed'])
                    self.assertTrue(report['stale'])
                    run.assert_not_called()
                self.saved.write_text(json.dumps(original))
                with patch('mediawrench.cutlist.probe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, raw, b'')):
                    report = verify_probe_evidence(self.saved)
                self.assertTrue(report['replayed'])
                self.assertTrue(report['stale'])
                self.assertIn('replay-zero-origin', [x['code'] for x in report['issues']])

    def test_schema3_retains_saved_rate_plan_and_grid_coherence(self):
        original = self.record()
        for field in ['summary', 'plan', 'grid']:
            document = copy.deepcopy(original)
            if field == 'summary':
                document['durationProbe']['frameRateValidation']['avgFrameRate'] = '25/1'
            elif field == 'plan':
                document['selectionPlan']['frameRate'] = '25'
            else:
                document['quantization']['frameRate']['numerator'] = 25
            self.saved.write_text(json.dumps(document))
            with self.subTest(field=field), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                report = verify_probe_evidence(self.saved)
                self.assertFalse(report['replayed'])
                self.assertIn('recorded-frame-rate', [x['code'] for x in report['issues']])
                run.assert_not_called()


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real zero/nonzero origin integration')
class RealOriginProbeTests(ProbeFixture, unittest.TestCase):
    def fixture(self, label):
        self.media = self.directory / 'clip.mp4'
        shutil.copyfile(ROOT / 'tests' / 'cutlist' / 'fixtures' / f'origin-{label}.mp4', self.media)
        self.media_before = self.media.read_bytes()
        self.set_plan({'keep': [1], 'frameRate': '24'})

    def test_real_zero_origin_exports_and_replays_with_exact_frames(self):
        self.fixture('zero')
        result = self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--verify-zero-origin')
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)['durationProbe']
        self.assertEqual(evidence['schemaVersion'], 3)
        self.assertEqual(Decimal(evidence['zeroOriginValidation']['startTimeSeconds']), 0)
        document = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        self.assertEqual([document['cuts'][0][k] for k in ['startFrame', 'endFrame']], [2, 58])
        self.assertEqual(verify_probe_evidence(Path(str(self.prefix) + '.provenance.json'))['status'], 'verified')
        self.assertEqual(self.media.read_bytes(), self.media_before)

    def test_real_nonzero_rejected_even_dry_run_but_opt_out_unchanged(self):
        self.fixture('one')
        for extra in [[], ['--dry-run']]:
            with self.subTest(extra=extra):
                self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--verify-zero-origin', *extra), 'start_time')
        result = self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['durationProbe']['schemaVersion'], 2)

    def test_real_dry_run_still_checks_rate_and_selected_stream(self):
        self.fixture('zero')
        result = self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--verify-zero-origin', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        self.set_plan({'keep': [1], 'frameRate': '25'})
        self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--verify-zero-origin'), 'frame rate mismatch')
        self.set_plan({'keep': [1], 'frameRate': '24'})
        self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '1', '--verify-zero-origin'), 'video stream')
