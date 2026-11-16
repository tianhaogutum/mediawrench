import json
from pathlib import Path
import random
import tempfile
import unittest

import pysubs2

from mediawrench.cutlist import Cue, CutlistError, build_timeline, load_plan, read_srt, render_outputs


ROOT = Path(__file__).resolve().parents[2]


def intervals(timeline):
    return [(cut.start_ms, cut.end_ms) for cut in timeline.cuts]


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "input.srt"

    def read(self, text):
        self.path.write_text(text, encoding="utf-8")
        return read_srt(self.path)

    def test_real_pysubs2_roundtrip(self):
        original = pysubs2.load(str(ROOT / "examples/cutlist/interview.srt"), encoding="utf-8")
        original.save(str(self.path), format_="srt", encoding="utf-8")
        cues = read_srt(self.path)
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cues],
                         [(cue.start, cue.end) for cue in original])
        self.assertIn("你好", cues[0].text)

    def test_original_ids_file_order_bom_crlf_and_verbatim_text(self):
        cues = self.read('\ufeff10\r\n00:00:03,000 --> 00:00:04,000\r\n<i>你好</i>\r\n123\r\n\r\n2\r\n00:00:00,000 --> 00:00:01,000\r\nB\r\n')
        self.assertEqual([cue.id for cue in cues], [10, 2])
        self.assertEqual([cue.ordinal for cue in cues], [1, 2])
        self.assertEqual(cues[0].text, "<i>你好</i>\n123")
        timeline = build_timeline(cues, {"keep": [10, 2], "duration": 4})
        self.assertEqual([cut.cue_ids for cut in timeline.cuts], [(2,), (10,)])

    def test_malformed_srt_is_never_silently_skipped(self):
        valid = '1\n00:00:01,000 --> 00:00:02,000\ntext'
        cases = ['', 'junk\n' + valid, valid + '\n\ntrailing junk',
                 valid + '\n\n' + valid, valid.replace('1\n', 'x\n', 1),
                 valid.replace('1\n', '0\n', 1), valid.replace('00:00:01,000', '00:60:01,000'),
                 valid.replace('00:00:02,000', '00:00:02.000'),
                 valid.replace('00:00:02,000', '00:00:02,000 X1:12'),
                 valid.replace('00:00:01,000', '100:00:01,000'),
                 valid.replace('00:00:01,000', '00:00:02,000'),
                 valid.replace('00:00:01,000', '00:00:03,000'),
                 valid.replace('text', ''), valid.replace('text', 'other --> timing'),
                 valid.replace('text', '00:00:05,000 00:00:06,000')]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(CutlistError):
                self.read(text)

    def test_invalid_utf8_srt(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(CutlistError):
            read_srt(self.path)

    def test_json_rejects_duplicate_keys_nan_and_invalid_json(self):
        for text in ['{"keep":[1],"keep":[2]}', '{"duration":NaN}', '{"duration":Infinity}',
                     '{"duration":-Infinity}', '[]', '{', '{"labels":{"1":"a","1":"b"}}']:
            with self.subTest(text=text), self.assertRaises(CutlistError):
                self.path.write_text(text, encoding="utf-8")
                load_plan(self.path)

    def test_json_invalid_utf8(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(CutlistError):
            load_plan(self.path)

    def test_json_decimal_precision_never_rounded_before_validation(self):
        for value in ['0.10000000000000001', '0.00000000000000000001', '1e999', '1e1000000000']:
            self.path.write_text('{"keep":[1],"duration":' + value + '}')
            with self.subTest(value=value), self.assertRaises(CutlistError):
                load_plan(self.path)
        self.path.write_text('{"keep":[1],"duration":1e1,"padding":1e-3}')
        self.assertEqual(load_plan(self.path), {'keep': [1], 'duration': 10, 'padding': 0.001})


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.cues = (Cue(1, 1, 0, 2000, "你好"), Cue(2, 2, 1800, 4000, "B"),
                     Cue(3, 3, 3500, 4500, "Drop"), Cue(4, 4, 4000, 6000, "C"))

    def build(self, **kwargs):
        return build_timeline(self.cues, {"keep": [1, 2, 4], "duration": 6, **kwargs})

    def test_padding_union_exclusion_and_both_boundaries(self):
        timeline = self.build(drop=[3], padding=0.1)
        self.assertEqual(intervals(timeline), [(0, 3500), (4500, 6000)])
        self.assertEqual([cut.cue_ids for cut in timeline.cuts], [(1, 2), (4,)])

    def test_same_cue_keep_and_drop_has_no_padding_ghost(self):
        timeline = self.build(keep=[1, 4], drop=[4], padding=0.1)
        self.assertEqual(intervals(timeline), [(0, 2100)])
        self.assertEqual(timeline.keep_ids, (1, 4))

    def test_inclusive_ranges_deduplicate_and_sort(self):
        timeline = self.build(keep=[4, {"from": 1, "to": 4}, 1], drop=[3])
        self.assertEqual(timeline.keep_ids, (1, 2, 3, 4))
        self.assertEqual(intervals(timeline), [(0, 3500), (4500, 6000)])

    def test_adjacent_union_at_exact_millisecond(self):
        cues = (Cue(1, 1, 1, 2, "a"), Cue(2, 2, 2, 3, "b"))
        timeline = build_timeline(cues, {"keep": [1, 2], "duration": 0.003})
        self.assertEqual(intervals(timeline), [(1, 3)])

    def test_disjoint_intervals_remain_disjoint(self):
        self.assertEqual(intervals(self.build(keep=[1, 4])), [(0, 2000), (4000, 6000)])

    def test_nested_overlapping_and_adjacent_drops(self):
        cues = (Cue(1, 1, 0, 10000, "keep"), Cue(2, 2, 1000, 3000, "drop"),
                Cue(3, 3, 2000, 2500, "drop"), Cue(4, 4, 3000, 4000, "drop"),
                Cue(5, 5, 6000, 7000, "drop"))
        timeline = build_timeline(cues, {"keep": [1], "drop": [2, 3, 4, 5], "duration": 10})
        self.assertEqual(intervals(timeline), [(0, 1000), (4000, 6000), (7000, 10000)])
        self.assertTrue(all(cut.cue_ids == (1,) for cut in timeline.cuts))

    def test_drop_spanning_multiple_kept_intervals(self):
        cues = (Cue(1, 1, 0, 1000, "a"), Cue(2, 2, 2000, 3000, "b"), Cue(3, 3, 500, 2500, "drop"))
        timeline = build_timeline(cues, {"keep": [1, 2], "drop": [3], "duration": 3})
        self.assertEqual(intervals(timeline), [(0, 500), (2500, 3000)])

    def test_unknown_ids_and_ranges(self):
        for kwargs in [{"keep": [99]}, {"drop": [99]}, {"keep": [{"from": 1, "to": 99}]}]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CutlistError, "[Uu]nknown"):
                self.build(**kwargs)
        with self.assertRaises(CutlistError):
            build_timeline((self.cues[0], self.cues[3]), {"keep": [{"from": 1, "to": 2}], "duration": 6})

    def test_empty_selection_errors(self):
        for kwargs in [{"keep": []}, {"keep": [1], "drop": [1]}, {"keep": [3], "drop": [2, 4]}]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CutlistError, "Empty"):
                self.build(**kwargs)

    def test_duration_and_padding_validation_including_unselected_cues(self):
        for kwargs in [{"duration": 0}, {"duration": -1}, {"duration": 5}, {"duration": True},
                       {"duration": "6"}, {"duration": float("inf")}, {"duration": 6.0001},
                       {"padding": -0.1}, {"padding": 7}, {"padding": 0.0001},
                       {"padding": float("nan")}, {"duration": 1e30}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(CutlistError):
                self.build(**kwargs)
        with self.assertRaises(CutlistError):
            self.build(keep=[1], duration=2)

    def test_required_and_unknown_plan_fields(self):
        for plan in [{"keep": [1]}, {"duration": 6}, {"keep": [1], "duration": 6, "query": "a"}, []]:
            with self.subTest(plan=plan), self.assertRaises(CutlistError):
                build_timeline(self.cues, plan)

    def test_unsupported_selection_syntax(self):
        for value in ['1-4', ["1-4"], [True], [1.0], [[1, 4]], [{"from": 4, "to": 1}],
                      [{"from": 1, "to": 2, "step": 1}], [{"from": True, "to": 2}], [0], [-1]]:
            for field in ['keep', 'drop']:
                with self.subTest(value=value, field=field), self.assertRaises(CutlistError):
                    self.build(**{field: value})

    def test_unicode_labels_and_invalid_labels(self):
        timeline = self.build(labels={"1": "你好, first", "2": '"Second"'})
        self.assertEqual(timeline.cuts[0].name, '你好, first / "Second" / cue 4')
        for labels in [[], {"99": "x"}, {"3": "not kept"}, {"1": ""}, {"1": "a\nb"},
                       {"1": 2}, {"01": "x"}, {1: "x"}]:
            with self.subTest(labels=labels), self.assertRaises(CutlistError):
                self.build(labels=labels)

    def test_deterministic_ordering_and_serialization(self):
        plan = {"keep": [4, 2, 1], "duration": 6, "drop": [3], "padding": 0.1}
        a = build_timeline(self.cues, plan)
        b = build_timeline(tuple(reversed(self.cues)), dict(reversed(list(plan.items()))))
        self.assertEqual(a.cuts, b.cuts)
        self.assertEqual(a.plan_json, b.plan_json)
        self.assertEqual(render_outputs(a, "test.wav"), render_outputs(a, "test.wav"))

    def test_provenance_of_padding_only_fragments(self):
        cues = (Cue(1, 1, 1000, 2000, "keep"), Cue(2, 2, 950, 2100, "drop"))
        timeline = build_timeline(cues, {"keep": [1], "drop": [2], "duration": 3, "padding": 0.2})
        self.assertEqual(intervals(timeline), [(800, 950), (2100, 2200)])
        maps = json.loads(render_outputs(timeline, "a.wav")["provenance"])["cuts"]
        self.assertTrue(all(cut["contributions"][0]["sourceOverlapMs"] is None for cut in maps))
        self.assertTrue(all(cut["overlappingCueIds"] == [] for cut in maps))

    def test_invalid_library_cues(self):
        for cues in [(), (self.cues[0], self.cues[0]), (Cue(1, 1, -1, 100, "x"),),
                     (Cue(1, 1, 0, 0, "x"),), (Cue(True, 1, 0, 1, "x"),)]:
            with self.subTest(cues=cues), self.assertRaises(CutlistError):
                build_timeline(cues, {"keep": [1], "duration": 6})

    def test_seeded_interval_oracle_and_provenance(self):
        rng = random.Random(87)
        for case in range(200):
            cues = tuple(Cue(i, i, (start := rng.randrange(99)), rng.randrange(start + 1, 101), str(i))
                         for i in range(1, 9))
            keep = [cue.id for cue in cues if rng.choice([True, False])]
            drop = [cue.id for cue in cues if rng.randrange(4) == 0]
            padding = rng.randrange(10)
            expected = set()
            for cue in cues:
                if cue.id in keep and cue.id not in drop:
                    expected.update(range(max(0, cue.start_ms - padding), min(100, cue.end_ms + padding)))
            for cue in cues:
                if cue.id in drop:
                    expected.difference_update(range(cue.start_ms, cue.end_ms))
            plan = {"keep": keep, "drop": drop, "padding": padding / 1000, "duration": 0.1}
            with self.subTest(case=case):
                if not expected:
                    with self.assertRaises(CutlistError):
                        build_timeline(cues, plan)
                    continue
                timeline = build_timeline(cues, plan)
                actual = {ms for cut in timeline.cuts for ms in range(cut.start_ms, cut.end_ms)}
                self.assertEqual(actual, expected)
                for cut in timeline.cuts:
                    expected_ids = tuple(sorted(cue.id for cue in cues if cue.id in set(keep) - set(drop)
                                               and cue.start_ms - padding < cut.end_ms
                                               and cut.start_ms < cue.end_ms + padding))
                    self.assertEqual(cut.cue_ids, expected_ids)
                mapping = json.loads(render_outputs(timeline, "test.wav")["provenance"])
                for cut in mapping["cuts"]:
                    self.assertEqual(cut["overlappingCueIds"], sorted(cue.id for cue in cues
                                     if cue.start_ms < cut["endMs"] and cut["startMs"] < cue.end_ms))
