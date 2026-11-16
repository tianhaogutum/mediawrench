"""Caller media digests bind saved proof before current-file access or replay."""

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


def mismatch(expected, actual):
    return {'code': 'expected-media-sha256',
            'message': 'Saved media SHA-256 differs from caller expectation',
            'expected': expected, 'actual': actual}


class ExpectedMediaTests(OriginFixture, unittest.TestCase):
    def save_schema(self, schema):
        timeline = self.build(video_stream_index=None if schema == 1 else 2,
                              verify_zero_origin=schema == 3)
        document = json.loads(render_outputs(timeline, self.media.name, source={
            'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
            'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()})['provenance'])
        self.saved.write_text(json.dumps(document))
        self.response_file.write_bytes(self.raw)
        return document

    def cli(self, *extra, saved=None):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-probe-evidence',
                               str(self.saved if saved is None else saved), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_cli_mismatch_is_explicit_stale_and_read_only(self):
        document = self.save_schema(3)
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        result = self.cli('--expect-media-sha256', '0' * 64, '--expect-video-stream', '2',
                          '--expect-frame-rate', '48/2', '--require-zero-origin',
                          '--srt', str(self.srt), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['status'], report['stale'], report['replayed']), ('stale', True, False))
        self.assertEqual(report['issues'], [mismatch('0' * 64, document['durationProbe']['mediaSha256'])])
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_all_schemas_match_replay_exact_argv_and_preserve_defaults(self):
        for schema in [1, 2, 3]:
            document = self.save_schema(schema)
            with self.subTest(schema=schema), \
                    patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                report = verify_probe_evidence(self.saved,
                    expected_media_sha256=document['durationProbe']['mediaSha256'],
                    expected_video_stream_index=None if schema == 1 else 2,
                    expected_frame_rate=None if schema == 1 else '48/2', require_zero_origin=schema == 3,
                    srt_path=self.srt, plan_path=self.plan)
                self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                replay.assert_called_once()
                self.assertEqual(replay.call_args.args[0], self.argv)
                self.assertEqual(json.loads(self.calls.read_text()), self.argv)
            self.assertEqual(verify_probe_evidence(self.saved), report)
            self.assertEqual(verify_probe_evidence(self.saved, expected_media_sha256=None), report)
            self.calls.unlink()

    def test_invalid_type_syntax_length_rejected_before_file_access(self):
        for value in [True, False, 0, 1.0, [], {}, b'a' * 64, '', 'a' * 63, 'a' * 65,
                      'A' * 64, 'g' * 64, '0x' + 'a' * 62, ' ' + 'a' * 63, 'a' * 63 + '\n',
                      'a' * 64 + '\n', '\uff10' * 64, '\u0660' * 64, 'a' * 10000]:
            with self.subTest(value=str(value)[:70]), patch('mediawrench.cutlist.probe.Path') as path, \
                    patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaisesRegex(CutlistError, 'expected_media_sha256.*lowercase SHA-256'):
                    verify_probe_evidence('/does/not/exist', expected_media_sha256=value)
                path.assert_not_called()
                hashed.assert_not_called()
                replay.assert_not_called()
        self.save_schema(1)
        for digest in ['0' * 64, 'f' * 64, '0123456789abcdef' * 4]:
            report = verify_probe_evidence(self.saved, expected_media_sha256=digest)
            self.assertEqual(report['issues'][0]['expected'], digest)

    def test_mismatch_precedes_changed_or_missing_current_files_all_schemas(self):
        for schema in [1, 2, 3]:
            document = self.save_schema(schema)
            originals = {p: p.read_bytes() for p in [self.srt, self.plan, self.media, self.probe]}
            for state in ['changed', 'missing']:
                for p, raw in originals.items():
                    if state == 'changed':
                        p.write_bytes(raw + b' ')
                    else:
                        p.unlink()
                before = {p: p.read_bytes() for p in self.directory.iterdir()}
                with self.subTest(schema=schema, state=state), \
                        patch('mediawrench.cutlist.probe._sha256') as hashed, \
                        patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                    report = verify_probe_evidence(self.saved, expected_media_sha256='0' * 64,
                                                   srt_path=self.srt, plan_path=self.plan)
                    self.assertEqual(report['issues'], [mismatch('0' * 64, document['durationProbe']['mediaSha256'])])
                    self.assertFalse(report['replayed'])
                    hashed.assert_not_called()
                    replay.assert_not_called()
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
            for p, raw in originals.items():
                p.write_bytes(raw)
            self.probe.chmod(0o755)

    def test_mismatch_does_not_mask_invalid_evidence_or_requested_source_digests(self):
        original = self.save_schema(3)
        for mutation in ['argv', 'saved-digest', 'source']:
            document = copy.deepcopy(original)
            if mutation == 'argv':
                document['durationProbe']['argv'] += ['-show_frames']
            elif mutation == 'saved-digest':
                document['durationProbe']['mediaSha256'] = 'G' * 64
            else:
                document['source']['planSha256'] = False
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                with self.assertRaises(CutlistError):
                    verify_probe_evidence(self.saved, expected_media_sha256='0' * 64,
                                          srt_path=self.srt, plan_path=self.plan)
                hashed.assert_not_called()
                replay.assert_not_called()
            invalid = self.cli('--expect-media-sha256', '0' * 64, '--srt', str(self.srt), '--plan', str(self.plan))
            self.assertEqual(invalid.returncode, 2, invalid.stderr)
            self.assertFalse(invalid.stdout)
            self.assertFalse(self.calls.exists())

    def test_matching_digest_retains_coherence_current_hash_and_replay_checks(self):
        original = self.save_schema(3)
        digest = original['durationProbe']['mediaSha256']
        kwargs = dict(expected_media_sha256=digest, expected_video_stream_index=2,
                      expected_frame_rate='48/2', require_zero_origin=True, srt_path=self.srt, plan_path=self.plan)
        for mutation, code in [('raw', 'recorded-stdout-sha256'), ('rate', 'recorded-frame-rate'),
                               ('grid', 'recorded-frame-rate'), ('origin', 'recorded-zero-origin'),
                               ('source', 'srt-sha256')]:
            document = copy.deepcopy(original)
            if mutation == 'raw':
                document['durationProbe']['stdout'] += ' '
            elif mutation == 'rate':
                document['durationProbe']['frameRateValidation']['avgFrameRate'] = '25/1'
            elif mutation == 'grid':
                document['quantization']['frameRate']['numerator'] = 25
            elif mutation == 'origin':
                document['quantization']['originSeconds'] = 1
            else:
                document['source']['srtSha256'] = '0' * 64
            self.saved.write_text(json.dumps(document))
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(self.saved, **kwargs)
                self.assertIn(code, [i['code'] for i in report['issues']])
                self.assertFalse(report['replayed'])
                replay.assert_not_called()
        self.saved.write_text(json.dumps(original))
        for current, code in [(self.srt, 'srt-sha256'), (self.plan, 'plan-sha256'),
                              (self.media, 'media-sha256'), (self.probe, 'ffprobe-sha256')]:
            raw = current.read_bytes()
            current.write_bytes(raw + b' ')
            with self.subTest(current=current), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(self.saved, **kwargs)
                self.assertIn(code, [i['code'] for i in report['issues']])
                replay.assert_not_called()
            current.write_bytes(raw)
        self.response['streams'][0]['start_time'] = '1.000000'
        self.response_file.write_text(json.dumps(self.response))
        report = verify_probe_evidence(self.saved, **kwargs)
        self.assertTrue(report['replayed'])
        self.assertIn('replay-zero-origin', [i['code'] for i in report['issues']])

    def test_expectation_mismatches_compose_and_recorded_incoherence_remains(self):
        document = self.save_schema(3)
        document['quantization']['originSeconds'] = 1
        self.saved.write_text(json.dumps(document))
        with patch('mediawrench.cutlist.probe._sha256') as hashed, \
                patch('mediawrench.cutlist.probe.subprocess.run') as replay:
            report = verify_probe_evidence(self.saved, expected_media_sha256='0' * 64,
                expected_video_stream_index=1, expected_frame_rate='25', require_zero_origin=True)
            self.assertEqual([i['code'] for i in report['issues']],
                ['recorded-zero-origin', 'expected-video-stream', 'expected-frame-rate', 'expected-media-sha256'])
            hashed.assert_not_called()
            replay.assert_not_called()

    def test_cli_invalid_digest_precedes_missing_file_and_requirements_stay_invalid(self):
        self.save_schema(2)
        for digest in ['', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, 'a' * 64 + '\n', '\uff10' * 64, 'a' * 10000]:
            with self.subTest(digest=digest[:70]):
                result = self.cli('--expect-media-sha256', digest, saved='/does/not/exist')
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertIn('expected_media_sha256', result.stderr)
                self.assertFalse(self.calls.exists())
        for extra in [[], ['--srt', str(self.srt)], ['--require-zero-origin']]:
            result = self.cli('--expect-media-sha256', *([] if not extra else ['0' * 64, *extra]))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertFalse(self.calls.exists())
        self.save_schema(1)
        for extra in [['--expect-video-stream', '2'], ['--expect-frame-rate', '24'], ['--require-zero-origin']]:
            result = self.cli('--expect-media-sha256', '0' * 64, *extra)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real expected-media integration')
class RealExpectedMediaTests(ProbeFixture, unittest.TestCase):
    def test_real_all_schemas_selected_video_digest_exact_replay_and_mismatch(self):
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
            kwargs = dict(expected_video_stream_index=selected, expected_frame_rate=None if selected is None else rate,
                          require_zero_origin=schema == 3, srt_path=self.srt, plan_path=self.plan)
            before = {p: p.read_bytes() for p in self.directory.iterdir()}
            with self.subTest(schema=schema, selected=selected), \
                    patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                report = verify_probe_evidence(saved, expected_media_sha256=evidence['mediaSha256'], **kwargs)
                self.assertEqual((report['status'], report['replayed'], report['issues']), ('verified', True, []))
                replay.assert_called_once()
                self.assertEqual(replay.call_args.args[0], evidence['argv'])
            with patch('mediawrench.cutlist.probe._sha256') as hashed, \
                    patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(saved, expected_media_sha256='0' * 64, **kwargs)
                self.assertEqual(report['issues'], [mismatch('0' * 64, evidence['mediaSha256'])])
                self.assertFalse(report['replayed'])
                hashed.assert_not_called()
                replay.assert_not_called()
            self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
