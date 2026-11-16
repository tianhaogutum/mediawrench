"""Caller rate expectations bind exact saved proof without retargeting replay."""

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, build_probed_timeline, read_srt, render_outputs, verify_probe_evidence
from .test_origin_probe import OriginFixture
from .test_probe import FFPROBE, ROOT, ProbeFixture


def ratio(numerator, denominator=1):
    return {'numerator': numerator, 'denominator': denominator}


def mismatch(expected, actual):
    return {'code': 'expected-frame-rate',
            'message': 'Saved selected declared frame rate differs from caller expectation',
            'expected': expected, 'actual': actual}


class ExpectedRateTests(OriginFixture, unittest.TestCase):
    def save_schema(self, schema):
        timeline = self.build(video_stream_index=None if schema == 1 else 2,
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
        result = self.cli('--expect-frame-rate', '30000/1001', '--expect-video-stream', '2',
                          '--require-zero-origin', '--srt', str(self.srt), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['status'], report['stale'], report['replayed']), ('stale', True, False))
        self.assertEqual(report['issues'], [mismatch(ratio(30000, 1001), ratio(24))])
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_matching_equivalent_schema2_and3_replay_exact_argv_and_defaults(self):
        for schema in [2, 3]:
            self.save_schema(schema)
            for expected in ['24', '24/1', '48/2']:
                with self.subTest(schema=schema, expected=expected), \
                        patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                    report = verify_probe_evidence(self.saved, expected_frame_rate=expected,
                        expected_video_stream_index=2, require_zero_origin=schema == 3,
                        srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                    replay.assert_called_once()
                    self.assertEqual(replay.call_args.args[0], self.argv)
                    self.assertEqual(json.loads(self.calls.read_text()), self.argv)
                self.calls.unlink(missing_ok=True)
            self.assertEqual(verify_probe_evidence(self.saved), report)
            self.assertEqual(verify_probe_evidence(self.saved, expected_frame_rate=None), report)
            self.calls.unlink()

    def test_invalid_type_syntax_and_bounds_rejected_before_file_access(self):
        for value in [True, False, 24, 24.0, [], {}, '', '0', '-24', '+24', ' 24', '24 ', '24.0',
                      '2.4e1', '24/0', '0/1', '024', '24/01', '1/2/3', '24\n', '9' * 10000,
                      '1000000001/1000000001', '1/1000000001', '1000001', '1000000000/999']:
            with self.subTest(value=str(value)[:50]), patch('mediawrench.cutlist.probe.Path') as path, \
                    patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaisesRegex(CutlistError, 'frameRate'):
                    verify_probe_evidence('/does/not/exist', expected_frame_rate=value)
                path.assert_not_called()
                hashed.assert_not_called()
                replay.assert_not_called()
        # Inclusive parser bounds are accepted, then the saved selected rate differs.
        self.save_schema(2)
        for expected, exact in [('1000000', ratio(1000000)), ('1/1000000000', ratio(1, 1000000000)),
                                ('1000000000/1000000000', ratio(1))]:
            report = verify_probe_evidence(self.saved, expected_frame_rate=expected)
            self.assertEqual(report['issues'], [mismatch(exact, ratio(24))])
            self.assertFalse(report['replayed'])

    def test_schema1_cannot_prove_rate_even_with_frame_grid_and_default_is_preserved(self):
        document = self.save_schema(1)
        self.assertIn('frameRate', document['quantization'])
        result = self.cli('--expect-frame-rate', '24')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)
        self.assertIn('expected_frame_rate requires durationProbe schemaVersion 2 or 3', result.stderr)
        with patch('mediawrench.cutlist.probe._sha256') as hashed, \
                patch('mediawrench.cutlist.probe.subprocess.run') as replay:
            with self.assertRaisesRegex(CutlistError, 'expected_frame_rate requires durationProbe schemaVersion 2 or 3'):
                verify_probe_evidence(self.saved, expected_frame_rate='24')
            hashed.assert_not_called()
            replay.assert_not_called()
        self.assertFalse(self.calls.exists())
        self.assertEqual(verify_probe_evidence(self.saved)['status'], 'verified')

    def test_near_mismatch_precedes_all_current_hashing_and_combines_index_mismatch(self):
        for schema in [2, 3]:
            self.save_schema(schema)
            for current in [self.srt, self.plan, self.media, self.probe]:
                current.write_bytes(current.read_bytes() + b' ')
            before = {p: p.read_bytes() for p in self.directory.iterdir()}
            with self.subTest(schema=schema), patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(self.saved, expected_frame_rate='24000/1001',
                    expected_video_stream_index=1, require_zero_origin=schema == 3,
                    srt_path=self.srt, plan_path=self.plan)
                self.assertEqual(report['issues'][-1], mismatch(ratio(24000, 1001), ratio(24)))
                self.assertEqual([x['code'] for x in report['issues']], ['expected-video-stream', 'expected-frame-rate'])
                self.assertFalse(report['replayed'])
                hashed.assert_not_called()
                replay.assert_not_called()
            self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_matching_rate_retains_saved_coherence_source_hash_and_replay_checks(self):
        original = self.save_schema(3)
        for mutation, code in [('argv', None), ('rate-type', None), ('source-shape', None),
                               ('raw-rate', 'recorded-frame-rate'), ('summary', 'recorded-frame-rate'),
                               ('plan', 'recorded-frame-rate'), ('grid', 'recorded-frame-rate'),
                               ('origin', 'recorded-zero-origin'), ('source', 'srt-sha256')]:
            document = copy.deepcopy(original)
            if mutation == 'argv':
                document['durationProbe']['argv'] += ['-show_frames']
            elif mutation == 'rate-type':
                document['durationProbe']['frameRateValidation']['declaredFrameRate'] = 24
            elif mutation == 'source-shape':
                document['source']['srtSha256'] = 'bad'
            elif mutation == 'raw-rate':
                raw = json.loads(document['durationProbe']['stdout'])
                raw['streams'][0]['avg_frame_rate'] = '25/1'
                document['durationProbe']['stdout'] = json.dumps(raw)
                document['durationProbe']['stdoutSha256'] = hashlib.sha256(json.dumps(raw).encode()).hexdigest()
            elif mutation == 'summary':
                document['durationProbe']['frameRateValidation']['avgFrameRate'] = '25/1'
            elif mutation == 'plan':
                document['selectionPlan']['frameRate'] = '25'
            elif mutation == 'grid':
                document['quantization']['frameRate'] = ratio(25)
            elif mutation == 'origin':
                document['quantization']['originSeconds'] = 1
            else:
                document['source']['srtSha256'] = '0' * 64
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                kwargs = dict(expected_frame_rate='24', expected_video_stream_index=2, require_zero_origin=True,
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
        self.response['streams'][0]['avg_frame_rate'] = '25/1'
        self.response_file.write_text(json.dumps(self.response))
        report = verify_probe_evidence(self.saved, expected_frame_rate='48/2', require_zero_origin=True)
        self.assertTrue(report['replayed'])
        self.assertIn('replay-frame-rate', [x['code'] for x in report['issues']])

    def test_mismatch_does_not_mask_malformed_saved_shape_or_requested_source_digest(self):
        original = self.save_schema(3)
        for mutation in ['argv', 'source']:
            document = copy.deepcopy(original)
            if mutation == 'argv':
                document['durationProbe']['argv'] += ['-show_frames']
            else:
                document['source']['planSha256'] = False
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaises(CutlistError):
                    verify_probe_evidence(self.saved, expected_frame_rate='25', srt_path=self.srt, plan_path=self.plan)
                hashed.assert_not_called()
                replay.assert_not_called()

    def test_cli_invalid_rate_pair_and_origin_requirements_remain_invalid(self):
        self.save_schema(2)
        for extra in [['--expect-frame-rate', x] for x in ['0', '24.0', 'true', '24/0', '1000001', '9' * 22]] + [
                ['--expect-frame-rate'], ['--expect-frame-rate', '24', '--srt', str(self.srt)],
                ['--expect-frame-rate', '24', '--require-zero-origin']]:
            with self.subTest(extra=extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertTrue(result.stderr)
                self.assertFalse(self.calls.exists())


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real expected-rate integration')
class RealExpectedRateTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.media = self.directory / 'rates-multistream.mp4'
        shutil.copyfile(ROOT / 'tests/cutlist/fixtures/rates-multistream.mp4', self.media)
        self.saved = self.directory / 'saved.provenance.json'

    def record_selected(self, selected, schema):
        self.set_plan({'keep': [1], 'frameRate': '24' if selected == 1 else '30000/1001'})
        timeline = build_probed_timeline(read_srt(self.srt), json.loads(self.plan.read_text()),
            media_path=self.media, ffprobe_path=FFPROBE, video_stream_index=selected, verify_zero_origin=schema == 3)
        document = json.loads(render_outputs(timeline, self.media.name, source={
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
        self.saved.write_text(json.dumps(document))
        return document['durationProbe']

    def test_real_other_and_near_rate_stale_before_hash_or_replay_without_writes(self):
        for schema in [2, 3]:
            for selected, wrong in [(1, '30000/1001'), (2, '24'), (2, '2997/100')]:
                self.record_selected(selected, schema)
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                with self.subTest(schema=schema, selected=selected, wrong=wrong), \
                        patch('mediawrench.cutlist.probe._sha256') as hashed, \
                        patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                    report = verify_probe_evidence(self.saved, expected_frame_rate=wrong,
                        expected_video_stream_index=selected, require_zero_origin=schema == 3,
                        srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual((report['status'], report['replayed']), ('stale', False))
                    expected = {'30000/1001': ratio(30000, 1001), '24': ratio(24), '2997/100': ratio(2997, 100)}[wrong]
                    self.assertEqual(report['issues'], [mismatch(expected, ratio(24) if selected == 1 else ratio(30000, 1001))])
                    hashed.assert_not_called()
                    replay.assert_not_called()
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_real_equivalent_rate_exact_selected_replay_with_all_options(self):
        for schema in [2, 3]:
            for selected, expected in [(1, '48/2'), (2, '60000/2002')]:
                evidence = self.record_selected(selected, schema)
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                with self.subTest(schema=schema, selected=selected), \
                        patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                    report = verify_probe_evidence(self.saved, expected_frame_rate=expected,
                        expected_video_stream_index=selected, require_zero_origin=schema == 3,
                        srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                    replay.assert_called_once()
                    self.assertEqual(replay.call_args.args[0], evidence['argv'])
                self.assertEqual(verify_probe_evidence(self.saved), report)
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
