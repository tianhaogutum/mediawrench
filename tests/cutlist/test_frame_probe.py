"""Explicit stream/rate contracts, real synthetic CFR video and saved replay."""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, build_probed_timeline, read_srt, render_outputs, verify_probe_evidence
from .test_probe import FFPROBE, ROOT, ProbeFixture


def write_y4m(path, rate='24:1', frames=60):
    # Raw 16x16 planar 4:2:0 video: every frame has the exact header cadence.
    path.write_bytes(f'YUV4MPEG2 W16 H16 F{rate} Ip A1:1 C420jpeg\n'.encode()
                     + (b'FRAME\n' + bytes([16]) * 256 + bytes([128]) * 128) * frames)


class FrameProbeTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.probe = self.directory / 'probe-double'
        self.probe.write_text('# test double, subprocess.run is injected\n')
        self.probe.chmod(0o755)
        self.response = {'program_version': {'version': 'test-double'},
                         'format': {'duration': '2.500000'},
                         'streams': [{'index': 2, 'codec_type': 'video',
                                      'avg_frame_rate': '24/1', 'r_frame_rate': '24/1'}]}
        self.selection = {'keep': [1], 'frameRate': '48/2'}

    def build(self, index=2):
        self.raw = json.dumps(self.response).encode()
        result = subprocess.CompletedProcess([], 0, self.raw, b'')
        with patch('mediawrench.cutlist.probe.subprocess.run', return_value=result) as run:
            timeline = build_probed_timeline(read_srt(self.srt), self.selection,
                                             media_path=self.media, ffprobe_path=self.probe,
                                             video_stream_index=index)
        self.argv = run.call_args.args[0]
        return timeline

    def save(self):
        self.saved = self.directory / 'saved.provenance.json'
        self.saved.write_text(json.dumps(self.document))

    def test_explicit_absolute_stream_index_exact_rational_match_and_evidence(self):
        timeline = self.build()
        self.assertEqual(self.selection, {'keep': [1], 'frameRate': '48/2'})
        self.document = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        evidence = self.document['durationProbe']
        self.assertEqual(evidence['schemaVersion'], 2)
        self.assertEqual(evidence['argv'], self.argv)
        self.assertEqual(self.argv[self.argv.index('-select_streams') + 1], '2')
        self.assertIn('stream=index,codec_type,avg_frame_rate,r_frame_rate', self.argv[self.argv.index('-show_entries') + 1])
        self.assertEqual(evidence['frameRateValidation'], {
            'streamIndex': 2, 'declaredFrameRate': '48/2', 'avgFrameRate': '24/1',
            'rFrameRate': '24/1', 'policy': 'exact-avg-and-r-frame-rate'})
        self.assertEqual(evidence['stdoutSha256'], hashlib.sha256(self.raw).hexdigest())

    def test_invalid_index_or_missing_frame_rate_fails_before_execution(self):
        def invalid(index):
            build_probed_timeline(read_srt(self.srt), self.selection,
                                  media_path=self.media, ffprobe_path=self.probe,
                                  video_stream_index=index)

        for index in [-1, True, '0', 2**31, 0.5]:
            with self.subTest(index=index), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                with self.assertRaisesRegex(CutlistError, 'stream index'):
                    invalid(index)
                run.assert_not_called()
        for rate in [None, 24, '23.976', '0/1']:
            self.selection['frameRate'] = rate
            with self.subTest(rate=rate), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                with self.assertRaisesRegex(CutlistError, 'frameRate'):
                    invalid(2)
                run.assert_not_called()
        del self.selection['frameRate']
        with patch('mediawrench.cutlist.probe.subprocess.run') as run:
            with self.assertRaisesRegex(CutlistError, 'frameRate'):
                invalid(2)
            run.assert_not_called()

    def test_missing_ambiguous_wrong_index_or_nonvideo_stream_fails(self):
        stream = self.response['streams'][0]
        for streams in [None, {}, [], [stream, stream], [{**stream, 'index': 0}],
                        [{**stream, 'index': True}], [{**stream, 'codec_type': 'audio'}]]:
            self.response['streams'] = streams
            with self.subTest(streams=streams), self.assertRaisesRegex(CutlistError, 'video stream'):
                self.build()

    def test_missing_unknown_malformed_or_mismatched_rates_fail(self):
        original = copy.deepcopy(self.response)
        for key in ['avg_frame_rate', 'r_frame_rate']:
            for rate in [None, '0/0', '0/1', 'N/A', '24.0', 24, '24/0', '-24/1',
                         '25/1', '24000/1001', '23999/1000', '9' * 100]:
                self.response = copy.deepcopy(original)
                self.response['streams'][0][key] = rate
                with self.subTest(key=key, rate=rate), self.assertRaisesRegex(CutlistError, 'frame.rate|frame_rate'):
                    self.build()

    def test_schema2_replay_verifies_and_changed_rate_is_stale(self):
        timeline = self.build()
        self.document = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        self.save()
        with patch('mediawrench.cutlist.probe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, self.raw, b'')) as run:
            report = verify_probe_evidence(self.saved)
        self.assertEqual(report['status'], 'verified')
        self.assertEqual(run.call_args.args[0], self.argv)
        self.response['streams'][0]['avg_frame_rate'] = '25/1'
        changed = json.dumps(self.response).encode()
        with patch('mediawrench.cutlist.probe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, changed, b'')):
            stale = verify_probe_evidence(self.saved)
        self.assertTrue(stale['replayed'])
        self.assertTrue(stale['stale'])
        self.assertIn('replay-frame-rate', [x['code'] for x in stale['issues']])

    def test_saved_rate_summary_plan_and_grid_must_agree_before_replay(self):
        timeline = self.build()
        original = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        for field in ['avgFrameRate', 'rFrameRate', 'declaredFrameRate', 'plan', 'grid']:
            self.document = copy.deepcopy(original)
            if field == 'plan':
                self.document['selectionPlan']['frameRate'] = '25'
            elif field == 'grid':
                self.document['quantization']['frameRate']['numerator'] = 25
            else:
                self.document['durationProbe']['frameRateValidation'][field] = '25/1'
            self.save()
            with self.subTest(field=field), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                report = verify_probe_evidence(self.saved)
                self.assertTrue(report['stale'])
                self.assertFalse(report['replayed'])
                self.assertIn('recorded-frame-rate', [x['code'] for x in report['issues']])
                run.assert_not_called()

    def test_saved_schema_or_stream_argv_changes_are_invalid(self):
        timeline = self.build()
        original = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        for mutation in ['index', 'argv', 'missing', 'policy', 'schema1']:
            self.document = copy.deepcopy(original)
            evidence = self.document['durationProbe']
            if mutation == 'index':
                evidence['frameRateValidation']['streamIndex'] = True
            elif mutation == 'argv':
                evidence['argv'][evidence['argv'].index('-select_streams') + 1] = 'v:0'
            elif mutation == 'missing':
                del evidence['frameRateValidation']
            elif mutation == 'policy':
                evidence['frameRateValidation']['policy'] = 'close-enough'
            else:
                evidence['schemaVersion'] = 1
            self.save()
            with self.subTest(mutation=mutation), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                with self.assertRaises(CutlistError):
                    verify_probe_evidence(self.saved)
                run.assert_not_called()

    def test_invalid_recorded_or_replayed_schema2_json_is_stale(self):
        timeline = self.build()
        original = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        for raw in [b'{', b'{}', b'\xff', b'[]', b'\xed\xa0\x80']:
            with self.subTest(raw=raw):
                self.document = copy.deepcopy(original)
                self.document['durationProbe']['stdout'] = raw.decode('utf-8', errors='surrogateescape')
                self.save()
                with patch('mediawrench.cutlist.probe.subprocess.run') as run:
                    stale = verify_probe_evidence(self.saved)
                    self.assertTrue(stale['stale'])
                    self.assertFalse(stale['replayed'])
                    run.assert_not_called()
                self.document = copy.deepcopy(original)
                self.save()
                with patch('mediawrench.cutlist.probe.subprocess.run', return_value=subprocess.CompletedProcess([], 0, raw, b'')):
                    stale = verify_probe_evidence(self.saved)
                self.assertTrue(stale['stale'])
                self.assertTrue(stale['replayed'])
                self.assertIn('replay-frame-rate', [x['code'] for x in stale['issues']])

    def test_cli_requires_probe_and_declared_rate_and_rejects_query_option(self):
        self.set_plan({'keep': [1], 'duration': 2.5, 'frameRate': '24'})
        self.assert_failure(self.run_cli(None, '--verify-frame-rate-stream', '0'), 'requires --ffprobe')
        self.assert_failure(self.run_cli(self.probe, '--verify-frame-rate-stream', '-1'), 'stream index')
        self.set_plan({'keep': [1]})
        self.assert_failure(self.run_cli(self.probe, '--verify-frame-rate-stream', '0'), 'frameRate')
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', str(self.srt),
                                 '--query', 'keep', '--verify-frame-rate-stream', '0'],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2)
        self.assertIn('discovery only', result.stderr)


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real CFR video integration')
class RealFrameProbeTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.media = self.directory / '-短 clip.y4m'

    def fixture(self, rate='24:1', frames=60, declared='24'):
        write_y4m(self.media, rate, frames)
        self.media_before = self.media.read_bytes()
        self.set_plan({'keep': [1], 'frameRate': declared})

    def test_real_cfr_integer_and_ntsc_exports_and_replay(self):
        for rate, frames, declared, expected in [('24:1', 60, '48/2', [2, 58]),
                                                  ('30000:1001', 75, '30000/1001', [3, 72])]:
            with self.subTest(rate=rate):
                self.fixture(rate, frames, declared)
                self.prefix = self.directory / ('cuts-' + rate.replace(':', '-'))
                result = self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0')
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                saved = Path(str(self.prefix) + '.provenance.json')
                document = json.loads(saved.read_text())
                self.assertEqual(document['durationProbe'], report['durationProbe'])
                cut = document['cuts'][0]
                self.assertEqual([cut['startFrame'], cut['endFrame']], expected)
                llc = json.loads(self.prefix.with_suffix('.llc').read_text())
                rows = list(csv.reader(io.StringIO(self.prefix.with_suffix('.csv').read_text())))
                self.assertEqual(llc['cutSegments'], report['cuts'])
                self.assertEqual([float(x) for x in rows[0][:2]], [cut['start'], cut['end']])
                self.assertEqual(verify_probe_evidence(saved)['status'], 'verified')
                self.assertEqual(self.media.read_bytes(), self.media_before)

    def test_real_mismatched_rate_fails_without_outputs_even_dry_run(self):
        for declared in ['25', '24000/1001']:
            self.fixture(declared=declared)
            for extra in [[], ['--dry-run']]:
                with self.subTest(declared=declared, extra=extra):
                    self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', *extra), 'frame rate mismatch')

    def test_real_nonexistent_stream_fails_and_audio_is_not_video(self):
        self.fixture()
        self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '1'), 'video stream')
        self.media = self.directory / '-短 clip.wav'  # original real WAV from setUp
        self.media_before = self.media.read_bytes()
        self.assert_failure(self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0'), 'video stream')

    def test_opt_out_keeps_declared_grid_and_schema1_duration_query(self):
        self.fixture(declared='25')
        result = self.run_cli(FFPROBE, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)['durationProbe']
        self.assertEqual(evidence['schemaVersion'], 1)
        self.assertNotIn('frameRateValidation', evidence)
        self.assertNotIn('-select_streams', evidence['argv'])

    def test_dry_run_validates_and_writes_nothing(self):
        self.fixture()
        result = self.run_cli(FFPROBE, '--verify-frame-rate-stream', '0', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['durationProbe']['frameRateValidation']['streamIndex'], 0)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        self.assertEqual(self.media.read_bytes(), self.media_before)
