from copy import deepcopy
import json
import math
import unittest

from mediawrench.asraudit import AuditError, Thresholds, audit, parse_json
from mediawrench.asraudit.core import normalize_intervals


def segment(start=0, end=1, text="Hello", **scores):
    return {"start": start, "end": end, "text": text, **scores}


class EvidenceTests(unittest.TestCase):
    def test_suspicious_and_intentional_repetition_preserved(self):
        document = {"segments": [segment(0, 1), segment(1, 2),
                                 segment(4, 5, "Yes"), segment(5, 6, "Yes")]}
        original = deepcopy(document)
        result = audit(document, [[4, 6]])
        self.assertEqual(result["speech_corroborated_repeats"], [0, 1])
        self.assertEqual(result["flags"][2]["reasons"], ["adjacent_repeat"])
        self.assertEqual(document, original)
        self.assertEqual(result["source_document"], original)
        result["source_document"]["segments"][0]["text"] = "changed copy"
        self.assertEqual(document, original)

    def test_missing_vad_and_scores_unknown_but_empty_vad_is_evidence(self):
        document = {"segments": [segment(no_speech_prob=None)]}
        result = audit(document)
        self.assertEqual(result["flags"], [])
        item = result["segments"][0]
        for key in ("no_speech_prob", "avg_logprob", "outside_speech_fraction", "speech_corroborated_repeat"):
            self.assertIsNone(item[key])
        self.assertIsNone(result["speech_intervals"])
        supplied_empty = audit(document, [])
        self.assertEqual(supplied_empty["speech_intervals"], [])
        self.assertEqual(supplied_empty["flags"][0]["outside_speech_fraction"], 1)

    def test_overlap_union_unsorted_touching_and_boundary(self):
        intervals = [[3, 4], [1, 2], [0, 1.5], [1.25, 3]]
        self.assertEqual(normalize_intervals(intervals), [[0, 4]])
        result = audit({"segments": [segment(0, 4), segment(4, 5), segment(3, 5)]}, intervals)
        self.assertEqual([s["outside_speech_fraction"] for s in result["segments"]], [0, 1, .5])
        self.assertNotIn("outside_speech", result["segments"][2]["reasons"])
        self.assertEqual(intervals[0], [3, 4])

    def test_fraction_exact_threshold_and_zero_threshold(self):
        document = {"segments": [segment(0, 10)]}
        result = audit(document, [[0, 2]])
        self.assertIn("outside_speech", result["flags"][0]["reasons"])
        self.assertEqual(result["flags"][0]["outside_speech_fraction"], .8)
        inside = audit(document, [[0, 10]], Thresholds(outside_speech_fraction=0))
        self.assertEqual(inside["flags"], [])

    def test_outside_fraction_inclusive_decimal_boundaries_without_tolerance(self):
        # Exact, below and above boundaries, including a one-ULP difference.
        for threshold in (.2, .3):
            speech_end = 10 * (1 - threshold)
            for end, flagged in ((speech_end, True),
                                 (math.nextafter(speech_end, math.inf), False),
                                 (math.nextafter(speech_end, -math.inf), True)):
                with self.subTest(threshold=threshold, speech_end=end):
                    report = audit({"segments": [segment(0, 10)]}, [[0, end]],
                                   Thresholds(outside_speech_fraction=threshold))
                    self.assertEqual("outside_speech" in report["segments"][0]["reasons"], flagged)
                    if end == speech_end:
                        self.assertEqual(report["segments"][0]["outside_speech_fraction"], threshold)
        report = audit({"segments": [segment(.1, 1.1)]}, [[.1, .9]],
                       Thresholds(outside_speech_fraction=.2))
        self.assertEqual(report["segments"][0]["outside_speech_fraction"], .2)
        self.assertEqual(report["segments"][0]["reasons"], ["outside_speech"])
        # Multiple disjoint intervals with decimal endpoints must sum exactly.
        report = audit({"segments": [segment(0, 1)]}, [[.1, .4], [.5, 1]],
                       Thresholds(outside_speech_fraction=.2))
        self.assertEqual(report["segments"][0]["reasons"], ["outside_speech"])
        inside = audit({"segments": [segment(.1, .3, "one")]}, [[.1, .2], [.2, .3]],
                       Thresholds(outside_speech_fraction=0))
        self.assertEqual(inside["flags"], [])

    def test_score_and_rate_threshold_boundaries(self):
        config = Thresholds(max_chars_per_second=5)
        result = audit({"segments": [segment(text="a b c d e", no_speech_prob=.8, avg_logprob=-1),
                                     segment(2, 3, "abcdef", no_speech_prob=.799, avg_logprob=-.99)]}, thresholds=config)
        self.assertEqual(result["segments"][0]["reasons"], ["high_no_speech", "low_avg_logprob"])
        self.assertEqual(result["segments"][1]["reasons"], ["excessive_character_rate"])

    def test_unicode_normalization_and_original_strings(self):
        document = {"segments": [segment(0, 1, "  ＹＥＳ\t "), segment(1, 2, "yes"),
                                 segment(2, 3, "你好 世界")]}
        result = audit(document)
        self.assertEqual(result["repeat_runs"], [{"segments": [0, 1], "normalized_phrase": "yes"}])
        self.assertEqual(result["segments"][0]["text"], "  ＹＥＳ\t ")
        self.assertEqual(result["segments"][2]["character_count"], 4)

    def test_repeat_gap_minimum_empty_invalid_and_input_order(self):
        cases = [segment(0, 1), segment(1, 2), segment(5, 6),
                 segment(6, 7, ""), segment(8, 8), segment(9, 10)]
        self.assertEqual(audit({"segments": cases})["repeat_runs"][0]["segments"], [0, 1])
        self.assertEqual(audit({"segments": cases}, thresholds=Thresholds(min_repeat_segments=3))["repeat_runs"], [])
        out_of_order = {"segments": [segment(5, 6), segment(1, 2)]}
        self.assertEqual(audit(out_of_order)["repeat_runs"], [])

    def test_invalid_numeric_timestamps_retained_and_unplaced(self):
        document = {"segments": [segment(-1, 1), segment(2, 2), segment(4, 3)]}
        result = audit(document, [])
        self.assertEqual(result["unplaced_segments"], [0, 1, 2])
        self.assertEqual(result["review_ranges"], [])
        for flag in result["flags"]:
            self.assertEqual(flag["reasons"], ["invalid_timestamp"])
            self.assertIsNone(flag["chars_per_second"])
            self.assertIsNone(flag["outside_speech_fraction"])

    def test_coalescing_sorted_with_reason_union_and_gap(self):
        document = {"segments": [segment(4, 5, no_speech_prob=.9),
                                 segment(1, 2, avg_logprob=-2),
                                 segment(2.25, 3, text="different", no_speech_prob=.95)]}
        ranges = audit(document)["review_ranges"]
        self.assertEqual(ranges[0], {"start": 1, "end": 3, "segments": [1, 2],
                                     "reasons": ["high_no_speech", "low_avg_logprob"]})
        self.assertEqual(ranges[1]["segments"], [0])

    def test_deterministic_config_and_preserved_extra_metadata(self):
        document = {"text": "raw", "language": "en", "segments": [segment(id=99, words=[{"word": "Hello", "probability": .9}])]}
        config = Thresholds(max_chars_per_second=7, coalesce_gap_seconds=.5)
        first = audit(document, [[0, 1]], config)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(audit(document, [[0, 1]], config), sort_keys=True))
        self.assertEqual(first["thresholds"]["max_chars_per_second"], 7)
        self.assertEqual(first["source_document"], document)
        self.assertEqual(audit({"segments": []})["flags"], [])


class ValidationTests(unittest.TestCase):
    def test_bad_transcript_schemas(self):
        cases = [[], {}, {"segments": {}}, {"segments": [None]},
                 {"segments": [{"start": 0, "end": 1}]},
                 {"segments": [segment(start=True)]},
                 {"segments": [segment(start="0")]},
                 {"segments": [segment(text=42)]},
                 {"segments": [], "time_unit": "milliseconds"},
                 {"transcription": []}, {"segments": [], "transcription": []},
                 {"segments": [segment(no_speech_prob=1.1)]},
                 {"segments": [segment(no_speech_prob=-.1)]},
                 {"segments": [segment(avg_logprob="low")]}]
        for document in cases:
            with self.subTest(document=document), self.assertRaises(AuditError):
                audit(document)

    def test_nonfinite_anywhere_and_duplicate_keys(self):
        for raw in [b'{"segments":[],"x":NaN}', b'{"segments":[],"x":Infinity}',
                    b'{"segments":[],"x":1e999}', b'{"segments":[],"segments":[]}',
                    b'\xff', b'{']:
            with self.subTest(raw=raw), self.assertRaises(AuditError):
                parse_json(raw)
        for key in ("start", "end", "no_speech_prob", "avg_logprob"):
            for bad in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(key=key, bad=bad), self.assertRaises(AuditError):
                    audit({"segments": [segment(**{key: bad})]})
        with self.assertRaises(AuditError):
            audit({"segments": [segment()], "extra": float("nan")})

    def test_invalid_vad(self):
        for intervals in ({"intervals": []}, [None], [[0]], [[0, 1, 2]], [[-1, 2]],
                          [[1, 1]], [[2, 1]], [[False, 1]], [[0, "1"]], [[0, float("nan")]]):
            with self.subTest(intervals=intervals), self.assertRaises(AuditError):
                audit({"segments": []}, intervals)

    def test_bad_thresholds_and_non_json_library_inputs(self):
        for config in ({"min_repeat_segments": 1}, {"min_repeat_segments": 2.5},
                       {"max_chars_per_second": 0}, {"repeat_max_gap_seconds": -1},
                       {"high_no_speech_prob": 1.1}, {"outside_speech_fraction": -.1},
                       {"coalesce_gap_seconds": float("inf")}, {"low_avg_logprob": True}):
            with self.subTest(config=config), self.assertRaises(AuditError):
                Thresholds(**config)
        with self.assertRaises(AuditError):
            audit({"segments": [], "extra": (1, 2)})
        with self.assertRaises(AuditError):
            audit({"segments": []}, thresholds={})

    def test_extreme_numbers_fail_instead_of_emitting_nonfinite_report(self):
        with self.assertRaises(AuditError):
            audit({"segments": [segment(end=5e-324)]})
        with self.assertRaises(AuditError):
            audit({"segments": [segment(end=10 ** 400)]})


if __name__ == "__main__":
    unittest.main()
