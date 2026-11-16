import copy
import math
from pathlib import Path
import random
import unittest

from mediawrench.speakers import BridgeError, merge, parse_rttm
from mediawrench.speakers.core import load_json, normalize, OverlapIndex, union

ROOT = Path(__file__).resolve().parents[2]


def rttm(*rows):
    return parse_rttm("\n".join(f"SPEAKER rec 1 {a} {duration} <NA> <NA> {s} <NA> <NA>" for a, duration, s in rows))


def words(*entries):
    return {"words": [dict(start=a, end=b, word=text) for a, b, text in entries]}


class RTTMTests(unittest.TestCase):
    def test_standard_and_comments(self):
        result = parse_rttm("# header\n\nSPEAKER interview 1 0.000 2.000 <NA> <NA> Alice <NA> <NA>\n")
        self.assertEqual((result.recording, result.channel), ("interview", "1"))
        self.assertEqual(result.intervals, {"Alice": ((0.0, 2.0),)})
        self.assertEqual(result.source_records[0]["line"], 3)

    def test_union_duplicates_nested_adjacent(self):
        result = rttm((1, 1, "A"), (0, 2, "A"), (0, 2, "A"), (2, 1, "A"), (4, 1, "A"))
        self.assertEqual(result.intervals["A"], ((0, 3), (4, 5)))
        self.assertEqual(len(result.source_records), 5)

    def test_recording_selection(self):
        text = "SPEAKER one 1 0 1 <NA> <NA> A <NA> <NA>\nSPEAKER two 1 0 1 <NA> <NA> B <NA> <NA>"
        with self.assertRaisesRegex(BridgeError, "multiple recordings"):
            parse_rttm(text)
        self.assertEqual(set(parse_rttm(text, "two").intervals), {"B"})
        with self.assertRaisesRegex(BridgeError, "not found"):
            parse_rttm(text, "three")

    def test_channel_selection(self):
        text = "SPEAKER one 1 0 1 <NA> <NA> A <NA> <NA>\nSPEAKER one 2 0 1 <NA> <NA> B <NA> <NA>"
        with self.assertRaisesRegex(BridgeError, "multiple channels"):
            parse_rttm(text)
        self.assertEqual(set(parse_rttm(text, channel="2").intervals), {"B"})
        with self.assertRaisesRegex(BridgeError, "not found"):
            parse_rttm(text, channel="9")

    def test_invalid_rttm(self):
        for text in ("", "# only a comment", "SPEAKER rec 1 0 1 A", "LEXEME rec 1 0 1 <NA> <NA> A <NA> <NA>"):
            with self.subTest(text=text), self.assertRaises(BridgeError):
                parse_rttm(text)
        for start, duration, speaker, channel in (("nan", "1", "A", "1"), ("0", "inf", "A", "1"),
                ("-1", "1", "A", "1"), ("0", "-1", "A", "1"), ("0", "0", "A", "1"),
                ("bad", "1", "A", "1"), ("1e308", "1e308", "A", "1"),
                ("1e308", "1", "A", "1"), ("0", "1", "UNKNOWN", "1"),
                ("0", "1", "A", "0"), ("0", "1", "A", "x")):
            with self.subTest(start=start, duration=duration, speaker=speaker, channel=channel), self.assertRaises(BridgeError):
                parse_rttm(f"SPEAKER rec {channel} {start} {duration} <NA> <NA> {speaker} <NA> <NA>")


class AssignmentTests(unittest.TestCase):
    def test_largest_union_not_sum_of_duplicates(self):
        result = merge(words((0, 1, "Hello")), rttm((0, .6, "A"), (0, .6, "A"), (0, 1, "B")))
        item = result["items"][0]
        self.assertEqual(item["speaker"], "B")
        self.assertAlmostEqual(item["evidence"][0]["coverage"], .6)
        self.assertEqual(item["evidence"][0]["intervals"], [[0, .6]])
        self.assertTrue(item["simultaneous_speech"])

    def test_exact_tie_and_near_tie(self):
        for epsilon, expected in ((0, "B"), (.001, "AMBIGUOUS")):
            result = merge(words((0, 1, "word")), rttm((0, .75, "A"), (0, .7501, "B")), tie_epsilon=epsilon)
            self.assertEqual(result["items"][0]["speaker"], expected)
        item = merge(words((.5, 1, "Hello")), rttm((0, 2, "A"), (0, 2, "B")))["items"][0]
        self.assertEqual((item["speaker"], item["reason"], item["candidates"]),
                         ("AMBIGUOUS", "coverage_tie", ["A", "B"]))

    def test_no_intersection_and_touching(self):
        result = merge(words((1, 2, "a"), (3, 4, "b")), rttm((0, 1, "A"), (2, 1, "B")))
        self.assertTrue(all(i["speaker"] == "UNKNOWN" and not i["evidence"] for i in result["items"]))

    def test_sequential_speakers_are_not_simultaneous(self):
        item = merge(words((0, 2, "a")), rttm((0, 1, "A"), (1, 1, "B")))["items"][0]
        self.assertEqual(item["speaker"], "AMBIGUOUS")
        self.assertFalse(item["simultaneous_speech"])

    def test_minimum_coverage(self):
        data, diar = words((0, 1, "a")), rttm((0, .4, "A"))
        self.assertEqual(merge(data, diar)["items"][0]["reason"], "insufficient_coverage")
        self.assertEqual(merge(data, diar, min_coverage=.4)["items"][0]["speaker"], "A")
        self.assertEqual(merge(words((2, 3, "a")), diar, min_coverage=0)["items"][0]["speaker"], "UNKNOWN")

    def test_zero_duration(self):
        item = merge(words((0, 0, "a")), rttm((0, 1, "A")))["items"][0]
        self.assertEqual((item["speaker"], item["reason"]), ("UNKNOWN", "zero_duration"))

    def test_aliases_are_display_only(self):
        result = merge(words((0, 1, "Hi")), rttm((0, 1, "A")), aliases={"A": "Interviewer"})
        item = result["items"][0]
        self.assertEqual((item["speaker"], item["speaker_label"]), ("A", "Interviewer"))
        self.assertEqual(item["evidence"][0]["speaker"], "A")

    def test_bad_aliases(self):
        diar = rttm((0, 1, "A"), (1, 1, "B"))
        for aliases in ({"A": "same", "B": "same"}, {"A": "B"}, {"C": "Guest"},
                        {"A": "UNKNOWN"}, {"A": "AMBIGUOUS"}, {"A": " "}, {"A": "bad\nlabel"}, []):
            with self.subTest(aliases=aliases), self.assertRaises(BridgeError):
                merge(words((0, 1, "a")), diar, aliases=aliases)

    def test_bad_config(self):
        for key, value in (("min_coverage", 1.1), ("tie_epsilon", 1.1), ("max_gap", -1),
                           ("min_coverage", math.nan), ("tie_epsilon", math.inf), ("max_gap", True)):
            with self.subTest(key=key, value=value), self.assertRaises(BridgeError):
                merge(words((0, 1, "a")), rttm((0, 1, "A")), **{key: value})

    def test_index_matches_independent_point_sweep(self):
        rng = random.Random(86)
        for _ in range(60):
            rows = [(rng.randrange(20) / 2, rng.randrange(1, 10) / 2, s)
                    for s in ("A", "B") for _ in range(10)]
            diar = rttm(*rows)
            start, end = sorted(rng.sample(range(31), 2))
            start, end = start / 2, end / 2
            evidence, _ = OverlapIndex(diar).evidence(start, end)
            actual = {e["speaker"]: e["overlap"] for e in evidence}
            for speaker in ("A", "B"):
                points = sorted({start, end} | {max(start, min(end, p)) for a, d, s in rows if s == speaker for p in (a, a + d)})
                expected = sum(b - a for a, b in zip(points, points[1:])
                               if any(s == speaker and x <= (a + b) / 2 < x + d for x, d, s in rows))
                self.assertAlmostEqual(actual.get(speaker, 0), expected)


class NormalizationTests(unittest.TestCase):
    def test_demo_conservation_and_precision(self):
        data = load_json((ROOT / "examples/speakers/whisper.json").read_text())
        original = copy.deepcopy(data)
        result = merge(data, parse_rttm((ROOT / "examples/speakers/interview.rttm").read_text()))
        self.assertEqual(data, original)
        self.assertEqual(result["source_asr"], original)
        self.assertEqual([i["source_word"] for i in result["items"] if i["source_word"] is not None], list(range(7)))
        for si, segment in enumerate(data["segments"]):
            self.assertEqual("".join(i["text"] for i in result["items"] if i["source_segment"] == si), segment["text"])
        self.assertEqual(sum(i["precision"] == "segment" for i in result["items"]), 3)
        self.assertEqual([i for t in result["turns"] for i in t["item_ids"]], list(range(9)))

    def test_whisperx_missing_word_timing(self):
        data = load_json((ROOT / "examples/speakers/whisperx.json").read_text())
        items, warnings = normalize(data)
        self.assertEqual(len(items), 3)  # flattened word_segments must not double words
        self.assertEqual((items[1]["start"], items[1]["end"], items[1]["precision"]), (3, 5, "segment"))
        self.assertEqual("".join(i["text"] for i in items), "Lost 2014. words")
        self.assertEqual(len(warnings), 2)

    def test_partial_timing_uses_segment(self):
        items, _ = normalize({"segments": [{"start": 0, "end": 1, "text": "a b", "words": [
            {"word": "a", "start": 0}, {"word": "b", "end": None}]}]})
        self.assertEqual([i["precision"] for i in items], ["segment", "segment"])

    def test_segment_only_and_missing_text(self):
        items, _ = normalize({"segments": [{"start": 0, "end": 1, "text": "No words."}]})
        self.assertEqual((items[0]["text"], items[0]["source_word"]), ("No words.", None))
        items, _ = normalize(words((0, .5, "hello"), (.5, 1, "world")))
        self.assertEqual("".join(i["text"] for i in items), "hello world")
        self.assertEqual([i["word"] for i in items], ["hello", "world"])

    def test_residual_prefix_middle_suffix(self):
        text = "prefix a middle b suffix"
        data = {"segments": [{"start": 0, "end": 2, "text": text, "words": [
            {"word": "a", "start": 0, "end": 1}, {"word": "b", "start": 1, "end": 2}]}]}
        items, _ = normalize(data)
        self.assertEqual("".join(i["text"] for i in items), text)
        self.assertEqual([i["precision"] for i in items], ["segment", "word", "segment", "word", "segment"])

    def test_unicode_and_whitespace(self):
        text = "  你好\n world!  "
        items, _ = normalize({"segments": [{"start": 0, "end": 1, "text": text, "words": [
            {"word": "你好", "start": 0, "end": .5}, {"word": " world!", "start": .5, "end": 1}]}]})
        self.assertEqual("".join(i["text"] for i in items), text)

    def test_turn_grouping_gap_order_and_precision(self):
        data = words((0, .5, "a"), (.6, 1, "b"), (3, 4, "c"))
        diar = rttm((0, 5, "A"))
        one = merge(data, diar, max_gap=.2)
        self.assertEqual([t["item_ids"] for t in one["turns"]], [[0, 1], [2]])
        self.assertEqual(one, merge(data, diar, max_gap=.2))
        self.assertEqual(len(merge(data, diar, max_gap=0)["turns"]), 3)

    def test_invalid_schema(self):
        invalid = [[], {}, {"segments": []}, {"segments": "text"}, {"segments": [None]},
                   {"words": []}, {"words": [{"text": "a", "start": 0, "end": 1}]},
                   {"words": [{"word": "a"}]}, {"words": [{"word": " "}]},
                   {"segments": [{"text": "a"}]}, {"segments": [{"start": 0, "end": 1}]},
                   {"segments": [{"start": 0, "end": 1, "text": "a", "words": {}}]},
                   {"segments": [], "words": []}, {"words": [], "word_segments": []}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(BridgeError):
                normalize(data)

    def test_nonfinite_reversed_and_mixed_units(self):
        for value in (math.nan, math.inf, -1, "0", True, 10**400):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                normalize(words((value, 1, "a")))
        for data in (words((2, 1, "a")), words((2, 3, "a"), (0, 1, "b")),
                     {"time_unit": "ms", "words": [{"word": "a", "start": 0, "end": 1}]},
                     {"segments": [{"start": 0, "end": 1, "words": [{"word": "a", "start": 0, "end": 1000}]}]},
                     {"segments": [{"start": 0, "end": 1, "words": [{"word": "a", "start": 1000}]}]},
                     {"segments": [{"start": 0, "end": 1, "text": "a", "time_units": "ms"}]},
                     {"words": [{"word": "a", "start": 0, "end": 1, "time_unit": "milliseconds"}]},
                     {"segments": [{"start": 2, "end": 3, "text": "a"}, {"start": 0, "end": 1, "text": "b"}]}):
            with self.subTest(data=data), self.assertRaises(BridgeError):
                normalize(data)

    def test_inconsistent_word_text_is_error(self):
        with self.assertRaisesRegex(BridgeError, "does not match"):
            normalize({"segments": [{"start": 0, "end": 1, "text": "one", "words": [
                {"word": "two", "start": 0, "end": 1}]}]})

    def test_strict_json(self):
        for text in ('{"A":"one","A":"two"}', '{"start":NaN}', '{"end":Infinity}', '{bad'):
            with self.subTest(text=text), self.assertRaises(BridgeError):
                load_json(text)

    def test_root_transcript_cannot_silently_drop_text(self):
        data = words((0, 1, "hello"))
        data["text"] = "hello missing words"
        with self.assertRaisesRegex(BridgeError, "unlocated transcript text"):
            normalize(data)


if __name__ == "__main__":
    unittest.main()
