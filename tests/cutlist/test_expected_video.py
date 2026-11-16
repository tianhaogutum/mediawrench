"""Caller-selected absolute indices constrain saved proof without retargeting it."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, build_probed_timeline, read_srt, render_outputs, verify_probe_evidence
from .test_origin_probe import OriginFixture
from .test_probe import FFPROBE, ROOT, ProbeFixture


class ExpectedVideoTests(OriginFixture, unittest.TestCase):
    def save_schema(self, schema, index=2):
        self.response['streams'][0]['index'] = index
        timeline = self.build(video_stream_index=None if schema == 1 else index,
                              verify_zero_origin=schema == 3)
        document = json.loads(render_outputs(timeline, self.media.name, source={
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
        self.saved.write_text(json.dumps(document))
        self.response_file.write_bytes(self.raw)
        return document

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-probe-evidence',
                               str(self.saved), *extra], cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_cli_mismatch_is_explicit_stale_and_read_only(self):
        self.save_schema(3)
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        result = self.cli('--expect-video-stream', '1', '--require-zero-origin',
                          '--srt', str(self.srt), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['status'], report['stale'], report['replayed']), ('stale', True, False))
        self.assertEqual(report['issues'], [{
            'code': 'expected-video-stream',
            'message': 'Saved selected video stream differs from caller expectation',
            'expected': 1, 'actual': 2}])
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_index_type_and_bounds_rejected_before_file_access(self):
        for value in [True, False, '2', 2.0, -1, 2**31, [], {}]:
            with self.subTest(value=value), patch('mediawrench.cutlist.probe.Path') as path, \
                    patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaisesRegex(CutlistError, 'Video stream index must be an integer in'):
                    verify_probe_evidence('/does/not/exist', expected_video_stream_index=value)
                path.assert_not_called()
                hashed.assert_not_called()
                replay.assert_not_called()

    def test_matching_schema2_and3_and_extreme_indices_replay_exact_argv(self):
        for schema, index in [(2, 0), (2, 2**31 - 1), (2, 2), (3, 2)]:
            self.save_schema(schema, index)
            with self.subTest(schema=schema, index=index), \
                    patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                report = verify_probe_evidence(self.saved, expected_video_stream_index=index,
                    require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
                self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                replay.assert_called_once()
                self.assertEqual(replay.call_args.args[0], self.argv)
                self.assertEqual(json.loads(self.calls.read_text()), self.argv)
            self.assertEqual(verify_probe_evidence(self.saved), report)
            self.assertEqual(verify_probe_evidence(self.saved, expected_video_stream_index=None), report)
            self.calls.unlink()

    def test_duration_only_cannot_prove_selection_and_default_is_preserved(self):
        self.save_schema(1)
        for extra in [[], ['--require-zero-origin']]:
            result = self.cli('--expect-video-stream', '2', *extra)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn('requires durationProbe schemaVersion', result.stderr)
        with patch('mediawrench.cutlist.probe._sha256') as hashed, \
                patch('mediawrench.cutlist.probe.subprocess.run') as replay:
            with self.assertRaisesRegex(CutlistError, 'expected_video_stream_index requires durationProbe schemaVersion 2 or 3'):
                verify_probe_evidence(self.saved, expected_video_stream_index=2)
            hashed.assert_not_called()
            replay.assert_not_called()
        self.assertFalse(self.calls.exists())
        self.assertEqual(verify_probe_evidence(self.saved)['status'], 'verified')

    def test_mismatch_precedes_all_current_input_hashing_even_if_inputs_changed(self):
        for schema in [2, 3]:
            self.save_schema(schema)
            for current in [self.srt, self.plan, self.media]:
                current.write_bytes(current.read_bytes() + b' ')
            with self.subTest(schema=schema), patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(self.saved, expected_video_stream_index=1,
                    require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
                self.assertEqual(report['issues'][0]['code'], 'expected-video-stream')
                self.assertEqual((report['issues'][0]['expected'], report['issues'][0]['actual']), (1, 2))
                self.assertFalse(report['replayed'])
                hashed.assert_not_called()
                replay.assert_not_called()
            self.assertFalse(self.calls.exists())

    def test_matching_expectation_retains_invalid_stale_source_and_replay_checks(self):
        original = self.save_schema(3)
        for mutation, code in [('argv', None), ('raw-index', 'recorded-frame-rate'),
                               ('grid', 'recorded-zero-origin'), ('source', 'srt-sha256')]:
            document = copy.deepcopy(original)
            if mutation == 'argv':
                document['durationProbe']['argv'] += ['-show_frames']
            elif mutation == 'raw-index':
                raw = json.loads(document['durationProbe']['stdout'])
                raw['streams'][0]['index'] = 1
                document['durationProbe']['stdout'] = json.dumps(raw)
                document['durationProbe']['stdoutSha256'] = hashlib.sha256(json.dumps(raw).encode()).hexdigest()
            elif mutation == 'grid':
                document['quantization']['originSeconds'] = 1
            else:
                document['source']['srtSha256'] = '0' * 64
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                kwargs = dict(expected_video_stream_index=2, require_zero_origin=True,
                              srt_path=self.srt, plan_path=self.plan)
                if code is None:
                    with self.assertRaises(CutlistError):
                        verify_probe_evidence(self.saved, **kwargs)
                else:
                    report = verify_probe_evidence(self.saved, **kwargs)
                    self.assertFalse(report['replayed'])
                    self.assertIn(code, [x['code'] for x in report['issues']])
                replay.assert_not_called()
        self.saved.write_text(json.dumps(original))
        self.response['streams'][0]['start_time'] = '1.000000'
        self.response_file.write_text(json.dumps(self.response))
        report = verify_probe_evidence(self.saved, expected_video_stream_index=2, require_zero_origin=True)
        self.assertTrue(report['replayed'])
        self.assertIn('replay-zero-origin', [x['code'] for x in report['issues']])

    def test_cli_invalid_index_pair_and_origin_requirements_stay_invalid(self):
        self.save_schema(2)
        for extra in [['--expect-video-stream', x] for x in ['-1', str(2**31), 'true', '1.0']] + [
                ['--expect-video-stream'], ['--expect-video-stream', '2', '--srt', str(self.srt)],
                ['--expect-video-stream', '2', '--require-zero-origin']]:
            with self.subTest(extra=extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertTrue(result.stderr)
                self.assertFalse(self.calls.exists())


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real expected-video integration')
class RealExpectedVideoTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.media = self.directory / 'rates-multistream.mp4'
        shutil.copyfile(ROOT / 'tests/cutlist/fixtures/rates-multistream.mp4', self.media)
        self.saved = self.directory / 'saved.provenance.json'

    def record_selected(self, index, schema):
        self.set_plan({'keep': [1], 'frameRate': '24' if index == 1 else '30000/1001'})
        timeline = build_probed_timeline(read_srt(self.srt), json.loads(self.plan.read_text()),
            media_path=self.media, ffprobe_path=FFPROBE, video_stream_index=index, verify_zero_origin=schema == 3)
        document = json.loads(render_outputs(timeline, self.media.name, source={
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
        self.saved.write_text(json.dumps(document))
        return document['durationProbe']

    def test_valid_proof_for_other_real_zero_origin_video_is_stale_before_hash_or_replay(self):
        for schema in [2, 3]:
            for selected, expected in [(1, 2), (2, 1)]:
                evidence = self.record_selected(selected, schema)
                self.assertEqual(evidence['schemaVersion'], schema)
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                with self.subTest(schema=schema, selected=selected), \
                        patch('mediawrench.cutlist.probe._sha256') as hashed, \
                        patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                    report = verify_probe_evidence(self.saved, expected_video_stream_index=expected,
                        require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual((report['status'], report['replayed']), ('stale', False))
                    self.assertEqual([(x['code'], x['expected'], x['actual']) for x in report['issues']],
                                     [('expected-video-stream', expected, selected)])
                    hashed.assert_not_called()
                    replay.assert_not_called()
                extra = ['--require-zero-origin'] if schema == 3 else []
                result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-probe-evidence',
                    str(self.saved), '--expect-video-stream', str(expected), *extra,
                    '--srt', str(self.srt), '--plan', str(self.plan)], cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(json.loads(result.stdout), report)
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_matching_real_index_replays_exact_saved_selection_and_defaults_unchanged(self):
        for schema in [2, 3]:
            for selected in [1, 2]:
                evidence = self.record_selected(selected, schema)
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                with self.subTest(schema=schema, selected=selected), \
                        patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                    report = verify_probe_evidence(self.saved, expected_video_stream_index=selected,
                        require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                    replay.assert_called_once()
                    self.assertEqual(replay.call_args.args[0], evidence['argv'])
                    argv = replay.call_args.args[0]
                    self.assertEqual(argv[argv.index('-select_streams') + 1], str(selected))
                self.assertEqual(verify_probe_evidence(self.saved), report)
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
