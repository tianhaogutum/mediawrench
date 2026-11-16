"""Synthetic SRT + real CLI/format integrations for exact constant-rate grids."""

import csv
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from mediawrench.cutlist import Cue, CutlistError, build_timeline, render_outputs
from .test_probe import FFPROBE

ROOT = Path(__file__).resolve().parents[2]


def rational(value):
    return Fraction(value['numerator'], value['denominator'])


def frames(timeline):
    return [(cut.start_frame, cut.end_frame) for cut in timeline.cuts]


class FrameTests(unittest.TestCase):
    def build(self, cues=None, rate='24', **plan):
        cues = cues or (Cue(1, 1, 100, 2400, '原文'),)
        return build_timeline(cues, {'keep': [1], 'duration': 2.5, 'frameRate': rate, **plan})

    def test_exact_24_and_ntsc_examples_and_audit(self):
        for rate, expected in [('24', (2, 58)), ('30000/1001', (3, 72))]:
            with self.subTest(rate=rate):
                timeline = self.build(rate=rate)
                self.assertEqual(frames(timeline), [expected])
                fps = Fraction(rate)
                cut = timeline.cuts[0]
                self.assertEqual(cut.start_ms, expected[0] * 1000 / fps)
                mapping = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])
                self.assertEqual(mapping['schemaVersion'], 2)
                self.assertEqual(mapping['cues'][0]['startMs'], 100)
                self.assertEqual(mapping['cues'][0]['endMs'], 2400)
                self.assertEqual(mapping['selectionPlan']['frameRate'], rate)
                quant = mapping['quantization']
                self.assertEqual(rational(quant['timeBase']), 1 / fps)
                self.assertEqual(rational(quant['maxEndpointErrorSecondsExclusive']), 1 / fps)
                for entry in quant['boundaries']:
                    delta = Fraction(entry['frame'], 1) / fps - Fraction(entry['originalMs'], 1000)
                    self.assertEqual(rational(entry['errorSeconds']), delta)
                    self.assertEqual(entry['direction'], 'later' if delta > 0 else 'earlier' if delta < 0 else 'unchanged')
                    self.assertLess(abs(delta), 1 / fps)
                exported = mapping['cuts'][0]
                self.assertNotIn('startMs', exported)
                self.assertEqual(rational(exported['startExactSeconds']), expected[0] / fps)
                self.assertEqual(rational(exported['endExactSeconds']), expected[1] / fps)
                self.assertTrue(exported['startBoundaryIds'])
                self.assertTrue(exported['endBoundaryIds'])
                for edge in ['start', 'end']:
                    for boundary_id in exported[edge + 'BoundaryIds']:
                        self.assertEqual(quant['boundaries'][boundary_id - 1]['frame'], exported[edge + 'Frame'])
                self.assertEqual(quant['cutBoundaries'][0]['startFrame'], expected[0])

    def test_merge_after_rounding_and_keep_drop_seed_precedence(self):
        cues = (Cue(1, 1, 0, 100, 'A'), Cue(2, 2, 102, 210, 'B'), Cue(3, 3, 800, 900, 'drop'))
        timeline = self.build(cues, keep=[1, 2, 3], drop=[3])
        self.assertEqual(frames(timeline), [(0, 5)])
        self.assertEqual(timeline.cuts[0].cue_ids, (1, 2))

    def test_exclusion_expands_and_duration_clamps_on_grid(self):
        cues = (Cue(1, 1, 0, 2510, 'keep'), Cue(2, 2, 1001, 1101, 'drop'))
        timeline = self.build(cues, duration=2.51, drop=[2], padding=0.1)
        self.assertEqual(frames(timeline), [(0, 24), (27, 60)])
        for cut in timeline.cuts:
            self.assertLessEqual(cut.end_ms, 2510)
            self.assertFalse(cut.start_ms < 1101 and cut.end_ms > 1001)
        mapping = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])
        self.assertEqual(mapping['quantization']['durationFrameFloor'], 60)
        self.assertEqual(mapping['cues'][1]['startMs'], 1001)

    def test_nearest_ties_go_later_and_zero_length_ranges_disappear(self):
        cues = (Cue(1, 1, 1, 2, 'tiny'), Cue(2, 2, 125, 375, 'ties'))
        timeline = self.build(cues, rate='4', keep=[1, 2], duration=1)
        self.assertEqual(frames(timeline), [(1, 2)])
        self.assertEqual(timeline.cuts[0].cue_ids, (2,))
        mapping = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])
        self.assertEqual(mapping['quantization']['collapsedKeepCueIds'], [1])

    def test_duration_clamp_reports_error_and_original_range(self):
        timeline = self.build((Cue(1, 1, 1, 2530, 'keep'),), duration=2.53)
        self.assertEqual(frames(timeline), [(0, 60)])
        mapping = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])
        end = mapping['quantization']['boundaries'][1]
        self.assertTrue(end['clamped'])
        self.assertEqual(end['direction'], 'earlier')
        self.assertEqual(rational(end['errorSeconds']), Fraction(-3, 100))
        self.assertEqual(mapping['cues'][0]['endMs'], 2530)

    def test_adjacent_quantized_drops_merge_and_keep_labels_survive(self):
        cues = (Cue(1, 1, 0, 1000, 'keep'), Cue(2, 2, 101, 120, 'drop'),
                Cue(3, 3, 126, 130, 'drop'))
        timeline = self.build(cues, duration=1, drop=[2, 3], labels={'1': '你好, "A"'})
        self.assertEqual(frames(timeline), [(0, 2), (4, 24)])
        docs = render_outputs(timeline, 'clip.mov')
        self.assertTrue(all(row[2] == '你好, "A"' for row in csv.reader(io.StringIO(docs['csv']))))
        mapping = json.loads(docs['provenance'])
        self.assertEqual([c['id'] for c in mapping['cues']], [1, 2, 3])
        self.assertEqual(mapping['cuts'][1]['contributions'][0]['retainedFrameInterval'], [4, 24])

    def test_24_exports_are_consistent_with_exact_grid_within_one_nanosecond(self):
        docs = render_outputs(self.build(), 'clip.mov')
        llc = json.loads(docs['llc'])
        rows = list(csv.reader(io.StringIO(docs['csv'])))
        mapping = json.loads(docs['provenance'])
        cut = mapping['cuts'][0]
        for edge in ['start', 'end']:
            actual = llc['cutSegments'][0][edge]
            self.assertEqual(actual, cut[edge])
            self.assertLessEqual(abs(Fraction(str(actual)) - rational(cut[edge + 'ExactSeconds'])), Fraction(1, 10**9))
        self.assertEqual([float(value) for value in rows[0][:2]], [cut['start'], cut['end']])
        self.assertEqual(cut['contributions'][0]['paddedIntervalMs'], [100, 2400])
        self.assertEqual(cut['contributions'][0]['quantizedPaddedFrameInterval'], [2, 58])
        self.assertEqual([rational(x) for x in cut['contributions'][0]['sourceOverlapExactSeconds']],
                         [Fraction(1, 10), Fraction(12, 5)])

    def test_long_supported_srt_times_export_within_declared_precision(self):
        timeline = self.build((Cue(1, 1, 123456789, 123456999, 'long'),), duration=123457)
        cut = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])['cuts'][0]
        for edge in ['start', 'end']:
            self.assertLessEqual(abs(Fraction(str(cut[edge])) - rational(cut[edge + 'ExactSeconds'])),
                                 Fraction(1, 10**9))

    def test_seconds_precision_failure_is_explicit(self):
        from dataclasses import replace
        from mediawrench.cutlist import FrameCut
        timeline = self.build()
        # Public library construction can exceed SRT's 99-hour limit. Such
        # numeric exports must fail rather than claiming the 1 ns bound.
        cut = FrameCut(24000000001, 24000000002, Fraction(24), (1,), 'large')
        with self.assertRaisesRegex(CutlistError, 'precision'):
            render_outputs(replace(timeline, cuts=(cut,)), 'clip.mov')

    def test_no_full_frame_empty_after_drop_and_invalid_rates_fail(self):
        for cues, plan in [((Cue(1, 1, 1, 2, 'tiny'),), {}),
                           ((Cue(1, 1, 0, 10, 'short'),), {'duration': 0.01}),
                           ((Cue(1, 1, 0, 80, 'keep'), Cue(2, 2, 1, 79, 'drop')), {'drop': [2]})]:
            with self.subTest(plan=plan), self.assertRaisesRegex(CutlistError, 'frame'):
                self.build(cues, **plan)
        for rate in [None, 24, 23.976, True, '', '0', '-24', '24.0', '30/0', '1/2/3', 'NaN', ' 24', '1' * 100]:
            with self.subTest(rate=rate), self.assertRaisesRegex(CutlistError, 'frameRate'):
                self.build(rate=rate)

    def test_quantization_idempotent_and_different_rate_requires_original(self):
        from mediawrench.cutlist import quantize_timeline
        original = build_timeline((Cue(1, 1, 100, 2400, '原文'),), {'keep': [1], 'duration': 2.5})
        quantized = quantize_timeline(original, '24')
        self.assertIs(quantize_timeline(quantized, '48/2'), quantized)
        self.assertEqual(render_outputs(quantized, 'clip.mov'),
                         render_outputs(quantize_timeline(quantized, '24'), 'clip.mov'))
        self.assertEqual(json.loads(quantized.plan_json), json.loads(original.plan_json))
        with self.assertRaisesRegex(CutlistError, 'original'):
            quantize_timeline(quantized, '30000/1001')

    def test_legacy_millisecond_documents_unchanged(self):
        timeline = build_timeline((Cue(1, 1, 100, 2400, '原文'),), {'keep': [1], 'duration': 2.5})
        docs = render_outputs(timeline, 'clip.mov')
        self.assertEqual(docs['csv'], '0.100,2.400,cues 1\n')
        mapping = json.loads(docs['provenance'])
        self.assertEqual(mapping['schemaVersion'], 1)
        self.assertNotIn('quantization', mapping)
        self.assertEqual(mapping['cuts'][0]['startMs'], 100)

    def test_seeded_frame_cell_oracle_bounds_errors_and_contributions(self):
        rng = random.Random(8703)
        for rate in ['24', '30000/1001']:
            fps = Fraction(rate)
            limit = int(Fraction(2500, 1000) * fps)
            # Independent enumeration of frame cells, not the interval sweep.
            for case in range(80):
                cues = tuple(Cue(i, i, (a := rng.randrange(2499)), rng.randrange(a + 1, 2501), str(i))
                             for i in range(1, 7))
                keep, drop = [1, 2, 3, 4], [3, 5]
                padding = rng.randrange(80)
                contributors = {}
                for cue in cues:
                    if cue.id in set(keep) - set(drop):
                        a = min(limit, int(Fraction(max(0, cue.start_ms - padding), 1000) * fps + Fraction(1, 2)))
                        b = min(limit, int(Fraction(min(2500, cue.end_ms + padding), 1000) * fps + Fraction(1, 2)))
                        for cell in range(a, b):
                            contributors.setdefault(cell, set()).add(cue.id)
                expected = set(contributors)
                for cue in cues:
                    if cue.id in drop:
                        expected = {cell for cell in expected
                                    if not (Fraction(cell, 1) / fps < Fraction(cue.end_ms, 1000)
                                            and Fraction(cell + 1, 1) / fps > Fraction(cue.start_ms, 1000))}
                with self.subTest(rate=rate, case=case):
                    if not expected:
                        with self.assertRaises(CutlistError):
                            self.build(cues, rate, keep=keep, drop=drop, padding=padding / 1000)
                        continue
                    timeline = self.build(cues, rate, keep=keep, drop=drop, padding=padding / 1000)
                    actual = {f for cut in timeline.cuts for f in range(cut.start_frame, cut.end_frame)}
                    self.assertEqual(actual, expected)
                    previous = -1
                    for cut in timeline.cuts:
                        self.assertLess(previous, cut.start_frame)
                        self.assertLess(cut.start_frame, cut.end_frame)
                        self.assertLessEqual(cut.end_frame, limit)
                        self.assertEqual(set(cut.cue_ids), set().union(*(contributors[f] for f in range(cut.start_frame, cut.end_frame))))
                        previous = cut.end_frame
                    mapping = json.loads(render_outputs(timeline, 'clip.mov')['provenance'])
                    for entry in mapping['quantization']['boundaries']:
                        self.assertLess(abs(rational(entry['errorSeconds'])), 1 / fps)


class FrameCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.srt = self.directory / 'cues.srt'
        self.srt.write_text('1\n00:00:00,100 --> 00:00:02,400\n原文\n', encoding='utf-8')
        self.plan = self.directory / 'plan.json'
        self.prefix = self.directory / 'cuts'

    def run_cli(self, rate, *extra, duration=2.5):
        self.plan.write_text(json.dumps({'keep': [1], 'duration': duration, 'frameRate': rate}))
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', str(self.srt),
                               '--plan', str(self.plan), '--media-file', 'clip.mov',
                               '--output-prefix', str(self.prefix), *extra], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)

    def test_real_cli_rational_seconds_not_millisecond_rounding(self):
        result = self.run_cli('30000/1001')
        self.assertEqual(result.returncode, 0, result.stderr)
        llc = json.loads(self.prefix.with_suffix('.llc').read_text())
        rows = list(csv.reader(io.StringIO(self.prefix.with_suffix('.csv').read_text())))
        mapping = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        report = json.loads(result.stdout)
        self.assertEqual(report['quantization'], mapping['quantization'])
        self.assertEqual(report['cuts'], llc['cutSegments'])
        self.assertEqual([(float(a), float(b)) for a, b, _ in rows], [(0.1001, 2.4024)])
        self.assertEqual([(cut['start'], cut['end']) for cut in llc['cutSegments']], [(0.1001, 2.4024)])
        self.assertEqual(mapping['cues'][0]['text'], '原文')

    def test_invalid_cli_no_outputs_and_dry_run(self):
        result = self.run_cli('23.976')
        self.assertEqual(result.returncode, 2)
        self.assertIn('frameRate', result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        result = self.run_cli('24', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        self.assertEqual(json.loads(result.stdout)['quantization']['durationFrameFloor'], 60)

    def test_collapsed_selection_cli_fails_without_outputs(self):
        self.srt.write_text('1\n00:00:00,001 --> 00:00:00,002\ntiny\n')
        result = self.run_cli('24')
        self.assertEqual(result.returncode, 2)
        self.assertIn('frame quantization', result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))

    @unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE')
    def test_real_probe_composes_with_frame_mode(self):
        import wave
        media = self.directory / 'clip.mov'  # demuxer detects WAV bytes, not suffix
        with wave.open(str(media), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\0\0' * 20000)
        result = self.run_cli('30000/1001', '--ffprobe', str(Path(FFPROBE).resolve()))
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        self.assertEqual(mapping['durationProbe']['durationMs'], 2500)
        self.assertEqual(mapping['cuts'][0]['endFrame'], 72)
        self.assertEqual(mapping['selectionPlan']['frameRate'], '30000/1001')


if __name__ == '__main__':
    unittest.main()
