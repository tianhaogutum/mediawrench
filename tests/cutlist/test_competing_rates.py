"""Real absolute-index selection between exact competing zero-origin rates."""

import csv
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from mediawrench.cutlist import verify_probe_evidence
from .test_probe import FFPROBE, ROOT, ProbeFixture


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real competing-rate integration')
class CompetingRateTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.media = self.directory / 'rates-multistream.mp4'
        shutil.copyfile(ROOT / 'tests/cutlist/fixtures/rates-multistream.mp4', self.media)
        self.media_before = self.media.read_bytes()

    def query(self, index):
        # Independent supported query: absolute indices, never video ordinals.
        return [str(Path(FFPROBE).resolve()), '-v', 'error', '-protocol_whitelist', 'file',
                '-select_streams', str(index), '-show_program_version', '-show_entries',
                'format=duration,format_name,size:program_version=version:'
                'stream=index,codec_type,avg_frame_rate,r_frame_rate,start_time',
                '-of', 'json', '-i', str(self.media.resolve())]

    def selected_cli(self, index, *extra):
        return self.run_cli(FFPROBE, '--verify-frame-rate-stream', str(index),
                            '--verify-zero-origin', *extra)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.directory.iterdir()}

    def test_independent_absolute_queries_establish_both_exact_rates_and_zero_origins(self):
        # Pinned: absolute index, stream type, exact video rates and zero video origins.
        # The container duration (2.5025 s with FFmpeg 7.0) and the ALAC audio stream's
        # rate/start fields are incidental metadata that differ between FFmpeg builds.
        for index, kind, rate in [(0, 'audio', None), (1, 'video', '24/1'),
                                   (2, 'video', '30000/1001')]:
            with self.subTest(index=index):
                result = subprocess.run(self.query(index), capture_output=True, check=True, timeout=120)
                data = json.loads(result.stdout)
                self.assertAlmostEqual(float(data['format']['duration']), 2.5025, delta=0.05)
                self.assertEqual(len(data['streams']), 1)
                stream = data['streams'][0]
                self.assertEqual((stream['index'], stream['codec_type']), (index, kind))
                if kind == 'video':
                    self.assertEqual(Decimal(stream['start_time']), 0)
                    self.assertEqual((stream['avg_frame_rate'], stream['r_frame_rate']), (rate, rate))
        self.assertEqual(self.media.read_bytes(), self.media_before)

    def test_matching_and_equivalent_rational_plans_export_exact_grids_and_replay_selected_index(self):
        # Fixed expected times are independent of the runtime's rate/rounding helpers.
        for index, declared, raw_rate, frames, times in [
            (1, '24', '24/1', (2, 58), (Fraction(1, 12), Fraction(29, 12))),
            (2, '30000/1001', '30000/1001', (3, 72), (Fraction(1001, 10000), Fraction(3003, 1250))),
            (2, '60000/2002', '30000/1001', (3, 72), (Fraction(1001, 10000), Fraction(3003, 1250))),
        ]:
            with self.subTest(index=index, declared=declared):
                self.prefix = self.directory / ('cuts-' + declared.replace('/', '-'))
                self.set_plan({'keep': [1], 'frameRate': declared})
                before = self.snapshot()
                dry = self.selected_cli(index, '--dry-run')
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertEqual(self.snapshot(), before)
                result = self.selected_cli(index)
                self.assertEqual(result.returncode, 0, result.stderr)
                saved = Path(str(self.prefix) + '.provenance.json')
                document = json.loads(saved.read_text())
                evidence = document['durationProbe']
                self.assertEqual(evidence, json.loads(dry.stdout)['durationProbe'])
                self.assertEqual(evidence['schemaVersion'], 3)
                self.assertEqual(evidence['argv'], self.query(index))
                self.assertEqual(evidence['frameRateValidation'], {
                    'streamIndex': index, 'declaredFrameRate': declared,
                    'avgFrameRate': raw_rate, 'rFrameRate': raw_rate,
                    'policy': 'exact-avg-and-r-frame-rate'})
                origin = dict(evidence['zeroOriginValidation'])
                self.assertEqual(Decimal(origin.pop('startTimeSeconds')), 0)
                self.assertEqual(origin, {'streamIndex': index, 'policy': 'exact-zero-stream-start-time'})
                grid = document['quantization']
                self.assertEqual(grid['originSeconds'], 0)
                self.assertEqual(grid['frameRate'], {'numerator': Fraction(declared).numerator,
                                                    'denominator': Fraction(declared).denominator})
                self.assertEqual(document['selectionPlan'], {'keep': [1], 'frameRate': declared})
                self.assertEqual(len(document['cuts']), 1)
                cut = document['cuts'][0]
                self.assertEqual((cut['startFrame'], cut['endFrame']), frames)
                llc = json.loads(self.prefix.with_suffix('.llc').read_text())
                rows = list(csv.reader(self.prefix.with_suffix('.csv').read_text().splitlines()))
                self.assertEqual(llc['cutSegments'], json.loads(result.stdout)['cuts'])
                for edge, frame, exact, cell in zip(('start', 'end'), frames, times, rows[0][:2]):
                    ratio = cut[edge + 'ExactSeconds']
                    self.assertEqual(Fraction(ratio['numerator'], ratio['denominator']), exact)
                    self.assertEqual(float(cell), cut[edge])
                    self.assertEqual(llc['cutSegments'][0][edge], cut[edge])
                    numeric = Fraction(cell)
                    self.assertLessEqual(abs(numeric - exact), Fraction(1, 10**9))
                    self.assertEqual(int(numeric * Fraction(declared) + Fraction(1, 2)), frame)
                before = self.snapshot()
                with patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
                    report = verify_probe_evidence(saved, require_zero_origin=True,
                                                    srt_path=self.srt, plan_path=self.plan)
                self.assertEqual((report['status'], report['replayed'], report['issues']),
                                 ('verified', True, []))
                replay.assert_called_once()
                self.assertEqual(replay.call_args.args[0], self.query(index))
                self.assertEqual(self.snapshot(), before)

    def test_crossed_rates_and_near_ntsc_rate_fail_without_tolerance_or_other_stream_fallback(self):
        for index, declared, actual in [(1, '30000/1001', '24/1'), (2, '24', '30000/1001'),
                                         (2, '2997/100', '30000/1001')]:
            self.set_plan({'keep': [1], 'frameRate': declared})
            for extra in [[], ['--dry-run']]:
                with self.subTest(index=index, declared=declared, extra=extra):
                    before = self.snapshot()
                    result = self.selected_cli(index, *extra)
                    self.assert_failure(result, f'Video frame rate mismatch for stream {index}: '
                                                f'declared {declared}, avg_frame_rate={actual}')
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(self.snapshot(), before)

    def test_opt_out_still_uses_explicit_plan_grid_without_rate_or_origin_inference(self):
        self.set_plan({'keep': [1], 'frameRate': '25'})
        before = self.snapshot()
        result = self.run_cli(FFPROBE, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['durationProbe']['schemaVersion'], 1)
        self.assertNotIn('-select_streams', report['durationProbe']['argv'])
        self.assertNotIn('stream=', report['durationProbe']['argv'][
            report['durationProbe']['argv'].index('-show_entries') + 1])
        self.assertEqual(report['quantization']['frameRate'], {'numerator': 25, 'denominator': 1})
        self.assertEqual(self.snapshot(), before)
