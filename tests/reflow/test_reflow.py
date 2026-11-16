import copy
import html
import itertools
from pathlib import Path
import random
import unittest

import pysubs2

from mediawrench.reflow import Config, InputError, normalize, reflow
from mediawrench.reflow.io import load_json, srt_text
from mediawrench.reflow.optimize import _candidate

ROOT = Path(__file__).resolve().parents[2]


def segment(words, text=None, start=0, end=None):
    records = [{"word": w, "start": i, "end": i + 0.8} for i, w in enumerate(words)]
    item = {"start": start, "end": end if end is not None else len(words), "words": records}
    if text is not None:
        item["text"] = text
    return {"segments": [item]}


class ReflowTests(unittest.TestCase):
    def test_pause_beats_fixed_midphrase(self):
        fixture = load_json(ROOT / "examples/reflow/pause.json")
        config = Config(max_chars=16, target_duration=1.5, max_duration=3)
        dp = reflow(fixture, config)
        baseline = reflow(fixture, config, algorithm="fixed", group_words=3)
        self.assertLess(dp["objective"], baseline["objective"])
        self.assertEqual([c["text"] for c in dp["cues"]], ["Hello there.", "Next topic."])
        self.assertEqual(dp["metrics"]["pause_boundary_penalty"], 0)
        self.assertEqual(baseline["metrics"]["pause_boundary_penalty"], 3400)

    def test_dp_matches_exhaustive_partition_oracle(self):
        # Enumerate every partition, not just another implementation of DP.
        rng = random.Random(82)
        for trial in range(20):
            words = ["a" * rng.randrange(1, 7) + ("." if rng.randrange(3) == 0 else "") for _ in range(6)]
            fixture = segment(words, " ".join(words))
            for i, word in enumerate(fixture["segments"][0]["words"]):
                word["start"] = i * 0.6 + (0.6 if i >= 3 else 0)
                word["end"] = word["start"] + 0.4
            config = Config(max_chars=8, target_duration=1.5, max_duration=2.5, max_cps=12)
            normalized = normalize(fixture)
            best = None
            for mask in itertools.product((False, True), repeat=5):
                points = [0] + [i + 1 for i, cut in enumerate(mask) if cut] + [6]
                score = [0, 0, 0]
                for a, b in zip(points, points[1:]):
                    cue = _candidate(normalized.units, a, b, config)
                    if b > a + 1 and any(v["code"] in ("line_width", "duration") for v in cue["violations"]):
                        break
                    score = [a + b for a, b in zip(score, cue["score"])]
                else:
                    if best is None or score < best:
                        best = score
            self.assertEqual(reflow(fixture, config)["objective"], best, trial)

    def test_whisper_preserves_strings_ids_and_spans(self):
        fixture = load_json(ROOT / "examples/reflow/pause.json")
        original = copy.deepcopy(fixture)
        report = reflow(fixture)
        self.assertEqual(fixture, original)
        self.assertEqual([w["word"] for w in report["normalized_words"]], [w["word"] for w in fixture["segments"][0]["words"]])
        self.assertEqual([w["original_id"] for w in report["normalized_words"]], ["hello", "there", "next", "topic"])
        self.assertEqual("".join(c["original_text"] for c in report["cues"]), fixture["segments"][0]["text"])
        for unit in report["source_units"]:
            a, b = unit["source"]["text_span"]
            self.assertEqual(unit["text"], fixture["segments"][0]["text"][a:b])
        self.assertTrue(report["metrics"]["tokens_conserved"])

    def test_whisperx_flat_words_and_word_segments(self):
        for key in ("words", "word_segments"):
            report = reflow({key: [{"word": "Hello", "start": 0, "end": 1}, {"word": "world.", "start": 1, "end": 2}]})
            self.assertEqual(report["cues"][0]["text"], "Hello world.")
            self.assertEqual(report["normalized_words"][1]["word"], "world.")
            self.assertTrue(any(d["code"] == "inferred_separator" for d in report["diagnostics"]))

    def test_whisperx_redundant_mirror_no_duplicate(self):
        fixture = segment(["One", " two"], "One two")
        fixture["word_segments"] = copy.deepcopy(fixture["segments"][0]["words"])
        report = reflow(fixture)
        self.assertEqual(report["metrics"]["source_words"], 2)
        self.assertEqual(len(report["source_units"]), 2)
        fixture["word_segments"][0]["word"] = "Other"
        with self.assertRaisesRegex(InputError, "mirror disagrees"):
            reflow(fixture)

    def test_whisperx_untimed_mirror(self):
        fixture = segment(["One", "two"], "One two")
        del fixture["segments"][0]["words"][1]["end"]
        fixture["word_segments"] = copy.deepcopy(fixture["segments"][0]["words"])
        report = reflow(fixture)
        self.assertTrue(report["cues"][0]["fallback"])
        self.assertEqual(len(report["normalized_words"]), 2)

    def test_unicode_cjk_without_spaces(self):
        report = reflow(segment(["你好", "世界", "再见", "朋友"], "你好世界再见朋友"), Config(max_chars=4))
        self.assertTrue(report["feasible"])
        self.assertEqual("".join(c["original_text"] for c in report["cues"]), "你好世界再见朋友")
        self.assertTrue(all(len(line) <= 4 for c in report["cues"] for line in c["lines"]))
        flat = reflow({"words": [{"word": "你好", "start": 0, "end": 1}, {"word": "世界", "start": 1, "end": 2}]})
        self.assertEqual(flat["cues"][0]["text"], "你好世界")

    def test_combining_and_emoji_original_strings(self):
        text = "Cafe\u0301 👩🏽‍💻 says hi."
        report = reflow(segment(["Cafe\u0301", "👩🏽‍💻", "says", "hi."], text))
        self.assertEqual("".join(c["original_text"] for c in report["cues"]), text)
        self.assertEqual(report["normalized_words"][1]["word"], "👩🏽‍💻")

    def test_no_words_segment_fallback(self):
        fixture = {"segments": [{"start": 0, "end": 4, "text": "Keep every unaligned character."}]}
        report = reflow(fixture)
        self.assertEqual(report["cues"][0]["original_text"], fixture["segments"][0]["text"])
        self.assertEqual(report["diagnostics"][0]["reason"], "no_words")
        self.assertEqual(report["cues"][0]["unit_ids"], ["/segments/0/text"])

    def test_partial_unaligned_segment_text_fallback(self):
        for text in ("Unaligned: Hello world.", "Hello £13.60 world.", "Hello world. Extra text."):
            fixture = segment(["Hello", "world."], text)
            report = reflow(fixture)
            self.assertTrue(report["cues"][0]["fallback"])
            self.assertEqual(report["cues"][0]["original_text"], text)
            self.assertEqual(report["metrics"]["mapped_words"], 2)
            self.assertEqual(report["source_units"][0]["source"]["text_span"], [0, len(text)])

    def test_untimed_word_uses_entire_segment(self):
        fixture = segment(["Pay", "£13.60", "now."], "Pay £13.60 now.")
        fixture["segments"][0]["words"][1].pop("start")
        report = reflow(fixture)
        self.assertEqual(report["diagnostics"][0]["reason"], "untimed_word")
        self.assertEqual(report["cues"][0]["original_text"], "Pay £13.60 now.")
        self.assertIsNone(report["normalized_words"][1]["start_ms"])
        self.assertEqual(report["metrics"]["mapped_words"], 3)

    def test_untimed_no_segment_text_fallback_joins_words(self):
        fixture = segment(["Pay", "now."])
        del fixture["segments"][0]["words"][0]["end"]
        report = reflow(fixture)
        self.assertEqual(report["cues"][0]["original_text"], "Pay now.")
        self.assertTrue(report["cues"][0]["fallback"])

    def test_fallback_missing_segment_bounds_rejected(self):
        for fixture in ({"segments": [{"text": "Untimed."}]},
                        {"words": [{"word": "Untimed."}]}):
            with self.assertRaises(InputError):
                reflow(fixture)

    def test_conflicting_word_text_rejected(self):
        for text in ("", "Hello", "Unrelated transcript"):
            with self.assertRaisesRegex(InputError, "conflicts with segment text"):
                reflow(segment(["Hello", "world"], text))

    def test_long_word_infeasible_never_truncated(self):
        word = "supercalifragilisticexpialidocious"
        report = reflow(segment([word], word), Config(max_chars=5, max_cps=100))
        self.assertFalse(report["feasible"])
        self.assertEqual(report["constraints"][0]["code"], "line_width")
        self.assertEqual(report["cues"][0]["text"], word)

    def test_long_fallback_at_most_two_lines_with_overflow(self):
        text = "one two three four five six seven eight nine ten"
        report = reflow({"segments": [{"start": 0, "end": 10, "text": text}]}, Config(max_chars=5))
        self.assertLessEqual(len(report["cues"][0]["lines"]), 2)
        self.assertEqual(report["cues"][0]["original_text"], text)
        self.assertEqual({v["code"] for v in report["constraints"]}, {"line_width", "duration"})

    def test_high_speaking_rate_reported(self):
        report = reflow({"words": [{"word": "speed", "start": 0, "end": 0.01}]})
        self.assertFalse(report["feasible"])
        self.assertEqual(report["constraints"][0]["code"], "speaking_rate")
        self.assertEqual(report["constraints"][0]["cps"], 500)

    def test_overlaps_and_zero_duration_repaired(self):
        fixture = {"words": [{"word": "A", "start": 1, "end": 2},
                             {"word": "B", "start": 1.5, "end": 1.9},
                             {"word": "C", "start": 0, "end": 0}]}
        report = reflow(fixture)
        self.assertEqual([(u["start_ms"], u["end_ms"]) for u in report["source_units"]], [(1000, 2000), (2000, 2001), (2001, 2002)])
        self.assertEqual(report["normalized_words"][1]["start"], 1.5)
        self.assertEqual(sum(d["code"] == "timing_repair" for d in report["diagnostics"]), 2)
        for previous, current in zip(report["cues"], report["cues"][1:]):
            self.assertLessEqual(previous["end_ms"], current["start_ms"])
        srt_text(report)

    def test_millisecond_half_up(self):
        report = reflow({"words": [{"word": "A", "start": 0.0005, "end": 0.0015}]})
        self.assertEqual(report["cues"][0]["start_ms"], 1)
        self.assertEqual(report["cues"][0]["end_ms"], 2)

    def test_stable_tie_break_earlier_predecessor(self):
        # Two cuts have identical cost: [aa] [aa aa] and [aa aa] [aa].
        fixture = {"words": [{"word": "aa", "start": i, "end": i + 1} for i in range(3)]}
        config = Config(max_chars=2, target_duration=1.5, max_duration=3, max_cps=100)
        outputs = [reflow(fixture, config) for _ in range(10)]
        self.assertTrue(all(r == outputs[0] for r in outputs))
        self.assertEqual(outputs[0]["cues"][0]["word_ids"], ["/words/0"])
        norm = normalize(fixture)
        path1 = [_candidate(norm.units, 0, 1, config), _candidate(norm.units, 1, 3, config)]
        path2 = [_candidate(norm.units, 0, 2, config), _candidate(norm.units, 2, 3, config)]
        self.assertEqual([sum(c["score"][i] for c in path1) for i in range(3)], [sum(c["score"][i] for c in path2) for i in range(3)])

    def test_srt_real_pysubs2_roundtrip(self):
        report = reflow(load_json(ROOT / "examples/reflow/pause.json"))
        encoded = srt_text(report)
        decoded = pysubs2.SSAFile.from_string(encoded, format_="srt")
        self.assertEqual([(c.start, c.end, c.plaintext) for c in decoded], [(0, 1200, "Hello there."), (2000, 3000, "Next topic.")])
        self.assertTrue(report["srt_roundtrip"]["verified"])

    def test_literal_markup_and_backslashes_survive_roundtrip(self):
        text = r"<i>literal</i> {\p1} \N &amp; >"
        report = reflow({"segments": [{"start": 0, "end": 5, "text": text}]})
        encoded = srt_text(report)
        decoded = pysubs2.SSAFile.from_string(encoded, format_="srt", keep_html_tags=True)
        self.assertEqual(html.unescape(decoded[0].text.replace(r"\N", "\n")), report["cues"][0]["text"])
        self.assertIn("&#92;N", encoded)

    def test_srt_timestamp_injection_rejected(self):
        report = reflow({"segments": [{"start": 0, "end": 2, "text": "00:00:00,000 --> 00:00:01,000"}]})
        with self.assertRaisesRegex(InputError, "SRT roundtrip"):
            srt_text(report)

    def test_nan_infinite_negative_reversed_times(self):
        for bad in (float("nan"), float("inf"), -1, True, "0", 10**400):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(InputError):
                    reflow({"words": [{"word": "A", "start": bad, "end": 2}]})
        with self.assertRaisesRegex(InputError, "end precedes start"):
            reflow({"words": [{"word": "A", "start": 2, "end": 1}]})

    def test_mixed_unknown_schemas(self):
        fixtures = [[], {}, {"results": []}, {"segments": {}, "words": []},
            {"segments": [{"start": 0, "end": 1, "words": [{"text": "No", "start": 0, "end": 1}]}]},
            {"words": [{"word": "A", "text": "A", "start": 0, "end": 1}]},
            {"words": [{"word": " ", "start": 0, "end": 1}]}]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                with self.assertRaises(InputError):
                    reflow(fixture)

    def test_empty_segments_reported_but_not_silently_discarded(self):
        fixture = {"segments": [{"text": ""}, {"text": "A", "start": 0, "end": 1}]}
        report = reflow(fixture)
        self.assertEqual(report["diagnostics"][0]["code"], "empty_segment")
        self.assertEqual(len(report["source_segments"]), 2)
        with self.assertRaisesRegex(InputError, "no renderable text"):
            reflow({"segments": []})

    def test_invalid_configuration(self):
        for kwargs in ({"max_chars": 0}, {"max_chars": True}, {"max_cps": float("nan")},
                       {"target_duration": 8}, {"pause": 0}, {"max_duration": 0.0001}):
            with self.assertRaises(InputError):
                Config(**kwargs)

    def test_input_control_characters(self):
        for word in ("a\x00b", "a\ud800b"):
            with self.assertRaises(InputError):
                reflow({"words": [{"word": word, "start": 0, "end": 1}]})

    def test_multiple_segments_keep_order_and_boundaries(self):
        report = reflow({"segments": [{"text": "First.", "start": 1, "end": 3},
                                       {"text": "Second.", "start": 2, "end": 4}]})
        self.assertEqual([c["text"] for c in report["cues"]], ["First.", "Second."])
        self.assertEqual(report["cues"][1]["start_ms"], 3000)
        self.assertEqual(len(report["diagnostics"]), 3)

    def test_dense_large_input_conserves_all_words(self):
        words = [{"word": " a", "start": i / 10, "end": (i + 1) / 10} for i in range(1000)]
        report = reflow({"words": words})
        self.assertEqual(report["metrics"]["mapped_words"], 1000)
        self.assertTrue(report["metrics"]["original_text_conserved"])
        srt_text(report)


if __name__ == "__main__":
    unittest.main()
