"""Explicit saved-evidence/current-file preflight never claims or attempts replay."""

import copy
import hashlib
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, build_probed_timeline, read_srt, render_outputs, verify_probe_evidence
from .test_origin_probe import OriginFixture
from .test_probe import FFPROBE, ROOT, ProbeFixture


class PreflightTests(OriginFixture, unittest.TestCase):
    def save_schema(self, schema):
        timeline = self.build(video_stream_index=None if schema == 1 else 2,
                              verify_zero_origin=schema == 3)
        document = json.loads(render_outputs(timeline, self.media.name, source={
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
        self.saved.write_text(json.dumps(document))
        self.response_file.write_bytes(self.raw)
        return document

    def snapshot(self):
        return {p.name: (p.read_bytes() if p.is_file() else None, p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.directory.iterdir()}

    def test_cli_preflight_success_is_explicit_and_does_not_execute(self):
        self.save_schema(3)
        before = self.snapshot()
        result = self.run_verify_cli('--preflight-only', '--require-zero-origin',
                                     '--srt', str(self.srt), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preflight(json.loads(result.stdout))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.calls.exists())

    def run_verify_cli(self, *extra):
        import sys
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-probe-evidence',
                               str(self.saved), *extra], cwd=ROOT, capture_output=True, text=True, timeout=120)

    def assert_preflight(self, report):
        self.assertEqual((report['status'], report['scope'], report['stale'], report['replayed'], report['issues']),
                         ('preflight-only', 'saved-evidence-and-current-files', False, False, []))
        self.assertNotIn('replayDurationMs', report)

    def test_all_schemas_all_expectations_no_subprocess_and_defaults_replay_exact_argv(self):
        for schema in [1, 2, 3]:
            document = self.save_schema(schema)
            evidence = document['durationProbe']
            kwargs = dict(expected_ffprobe_sha256=evidence['ffprobeSha256'],
                          expected_media_sha256=evidence['mediaSha256'],
                          expected_video_stream_index=None if schema == 1 else 2,
                          expected_frame_rate=None if schema == 1 else '24',
                          require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
            before = self.snapshot()
            with self.subTest(schema=schema), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                self.assert_preflight(verify_probe_evidence(self.saved, preflight_only=True, **kwargs))
                self.assert_preflight(verify_probe_evidence(self.saved, preflight_only=True))
                replay.assert_not_called()
            self.assertEqual(before, self.snapshot())
            for options in [{}, {'preflight_only': False}]:
                with patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                    report = verify_probe_evidence(self.saved, **options, **kwargs)
                    self.assertEqual((report['status'], report['stale'], report['replayed'], report['issues']),
                                     ('verified', False, True, []))
                    self.assertNotIn('scope', report)
                    replay.assert_called_once_with(evidence['argv'], stdin=subprocess.DEVNULL,
                                                  capture_output=True, timeout=120)
                if not options:
                    default_report = report
                else:
                    self.assertEqual(report, default_report)
                self.calls.unlink()

    def test_strict_bool_rejected_before_file_access(self):
        for value in [None, 0, 1, 0.0, 1.0, '', 'false', 'true', [], {}, b'1']:
            with self.subTest(value=value), patch('mediawrench.cutlist.probe.Path') as path, \
                    patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaisesRegex(CutlistError, 'preflight_only must be a boolean'):
                    verify_probe_evidence('/absent', preflight_only=value)
                path.assert_not_called()
                hashed.assert_not_called()
                replay.assert_not_called()

    def assert_same_stale(self, kwargs, code):
        before = self.snapshot()
        with patch('mediawrench.cutlist.probe.subprocess.run') as replay:
            baseline = verify_probe_evidence(self.saved, **kwargs)
            preflight = verify_probe_evidence(self.saved, preflight_only=True, **kwargs)
            self.assertEqual(preflight, baseline)
            self.assertEqual((preflight['status'], preflight['stale'], preflight['replayed']), ('stale', True, False))
            self.assertIn(code, [i['code'] for i in preflight['issues']])
            replay.assert_not_called()
        self.assertEqual(before, self.snapshot())
        return preflight

    def test_every_recorded_coherence_boundary_remains_stale(self):
        for schema in [1, 2, 3]:
            original = self.save_schema(schema)
            mutations = [('raw', 'recorded-stdout-sha256'), ('duration', 'recorded-duration'),
                         ('timeline', 'timeline-duration'), ('version', 'recorded-version'),
                         ('response', 'recorded-response-invalid')]
            if schema >= 2:
                mutations += [('rate', 'recorded-frame-rate'), ('plan', 'recorded-frame-rate'),
                              ('grid', 'recorded-frame-rate')]
            if schema == 3:
                mutations += [('origin', 'recorded-zero-origin'), ('origin-summary', 'recorded-zero-origin')]
            for mutation, code in mutations:
                document = copy.deepcopy(original)
                e = document['durationProbe']
                if mutation == 'raw': e['stdout'] += ' '
                elif mutation == 'duration': e['durationSeconds'] = '2.501000'
                elif mutation == 'timeline': document['normalizedSelection']['durationMs'] += 1
                elif mutation == 'version': e['version'] = 'different'
                elif mutation == 'response': e['stdout'] = '{}'
                elif mutation == 'rate': e['frameRateValidation']['avgFrameRate'] = '25/1'
                elif mutation == 'plan': document['selectionPlan']['frameRate'] = '25'
                elif mutation == 'grid': document['quantization']['frameRate']['numerator'] = 25
                elif mutation == 'origin': document['quantization']['originSeconds'] = 1
                else: e['zeroOriginValidation']['startTimeSeconds'] = '1.000000'
                self.saved.write_text(json.dumps(document))
                with self.subTest(schema=schema, mutation=mutation):
                    self.assert_same_stale({}, code)

    def test_current_files_hash_size_missing_permission_and_read_failures_keep_exact_details(self):
        for schema in [1, 2, 3]:
            original = self.save_schema(schema)
            kwargs = dict(srt_path=self.srt, plan_path=self.plan)
            for kind, current in [('srt', self.srt), ('plan', self.plan), ('media', self.media), ('ffprobe', self.probe)]:
                raw = current.read_bytes()
                for state in ['hash', 'missing', 'directory', 'unreadable']:
                    with self.subTest(schema=schema, kind=kind, state=state):
                        if state == 'hash':
                            current.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                            report = self.assert_same_stale(kwargs, kind + '-sha256')
                            field = original['source' if kind in ('srt', 'plan') else 'durationProbe']
                            expected = field[kind + 'Sha256']
                            self.assertIn({'code': kind + '-sha256', 'message': 'Recorded and current evidence differ',
                                           'expected': expected, 'actual': hashlib.sha256(current.read_bytes()).hexdigest()}, report['issues'])
                        elif state in ('missing', 'directory'):
                            current.unlink()
                            if state == 'directory': current.mkdir()
                            self.assert_same_stale(kwargs, kind + '-unavailable')
                            if state == 'directory': current.rmdir()
                        else:
                            from mediawrench.cutlist.probe import _sha256
                            def read_hash(path):
                                if path == current: raise PermissionError('controlled unreadable input')
                                return _sha256(path)
                            with patch('mediawrench.cutlist.probe._sha256', side_effect=read_hash):
                                report = self.assert_same_stale(kwargs, kind + '-unavailable')
                                self.assertIn('controlled unreadable input', report['issues'][0]['message'])
                        current.write_bytes(raw)
                        if kind == 'ffprobe': current.chmod(0o755)
            self.media.write_bytes(self.media.read_bytes() + b' ')
            self.assert_same_stale(kwargs, 'media-size')
            self.media.write_bytes(self.media.read_bytes()[:-1])
            self.probe.chmod(0o644)
            self.assert_same_stale(kwargs, 'ffprobe-unavailable')
            self.probe.chmod(0o755)

    def test_expectation_precedence_and_combined_details_unchanged(self):
        self.save_schema(3)
        for current in [self.srt, self.plan, self.media, self.probe]: current.unlink()
        kwargs = dict(srt_path=self.srt, plan_path=self.plan, require_zero_origin=True,
                      expected_video_stream_index=1, expected_frame_rate='25',
                      expected_media_sha256='0' * 64, expected_ffprobe_sha256='0' * 64)
        with patch('mediawrench.cutlist.probe._sha256') as hashed:
            report = self.assert_same_stale(kwargs, 'expected-video-stream')
            self.assertEqual([i['code'] for i in report['issues']], ['expected-video-stream',
                'expected-frame-rate', 'expected-media-sha256', 'expected-ffprobe-sha256'])
            hashed.assert_not_called()

    def test_malformed_evidence_sources_and_requirements_remain_invalid(self):
        original = self.save_schema(3)
        for mutation in ['argv', 'schema', 'source', 'grid', 'origin']:
            document = copy.deepcopy(original)
            if mutation == 'argv': document['durationProbe']['argv'] += ['-show_frames']
            elif mutation == 'schema': document['durationProbe']['schemaVersion'] = 4
            elif mutation == 'source': document['source']['planSha256'] = False
            elif mutation == 'grid': document['quantization']['frameRate']['denominator'] = 0
            else: document['durationProbe']['zeroOriginValidation']['streamIndex'] = False
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaises(CutlistError):
                    verify_probe_evidence(self.saved, preflight_only=True, expected_ffprobe_sha256='0' * 64,
                                          srt_path=self.srt, plan_path=self.plan)
                hashed.assert_not_called()
                replay.assert_not_called()
            result = self.run_verify_cli('--preflight-only', '--srt', str(self.srt), '--plan', str(self.plan))
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(result.stdout)
        self.save_schema(1)
        for kwargs in [dict(srt_path=self.srt), dict(require_zero_origin=True),
                       dict(expected_video_stream_index=2), dict(expected_frame_rate='24'),
                       dict(expected_media_sha256='A' * 64), dict(expected_ffprobe_sha256=False)]:
            with self.subTest(kwargs=kwargs), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaises(CutlistError):
                    verify_probe_evidence(self.saved, preflight_only=True, **kwargs)
                replay.assert_not_called()

    def test_preflight_does_not_read_future_replay_response(self):
        self.save_schema(3)
        self.response_file.write_text('invalid replay response')
        with patch('mediawrench.cutlist.probe.subprocess.run') as replay:
            self.assert_preflight(verify_probe_evidence(self.saved, preflight_only=True))
            replay.assert_not_called()
        report = verify_probe_evidence(self.saved)
        self.assertEqual((report['status'], report['replayed']), ('stale', True))
        self.assertIn('replay-invalid', [i['code'] for i in report['issues']])


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real preflight integration')
class RealPreflightTests(ProbeFixture, unittest.TestCase):
    def test_real_schemas_competing_selected_video_no_subprocess_no_writes(self):
        self.media = self.directory / 'rates-multistream.mp4'
        shutil.copyfile(ROOT / 'tests/cutlist/fixtures/rates-multistream.mp4', self.media)
        saved = self.directory / 'saved.provenance.json'
        for schema, selected in [(1, None), (2, 1), (2, 2), (3, 1), (3, 2)]:
            rate = '24' if selected != 2 else '30000/1001'
            self.set_plan({'keep': [1], 'frameRate': rate})
            timeline = build_probed_timeline(read_srt(self.srt), json.loads(self.plan.read_text()),
                media_path=self.media, ffprobe_path=FFPROBE, video_stream_index=selected, verify_zero_origin=schema == 3)
            document = json.loads(render_outputs(timeline, self.media.name, source={
                'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
                'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
            saved.write_text(json.dumps(document))
            evidence = document['durationProbe']
            kwargs = dict(expected_media_sha256=evidence['mediaSha256'],
                          expected_ffprobe_sha256=evidence['ffprobeSha256'], expected_video_stream_index=selected,
                          expected_frame_rate=None if selected is None else rate,
                          require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
            with self.subTest(schema=schema, selected=selected), \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(saved, preflight_only=True, **kwargs)
                self.assertEqual((report['status'], report['scope'], report['replayed'], report['issues']),
                                 ('preflight-only', 'saved-evidence-and-current-files', False, []))
                replay.assert_not_called()
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()})
