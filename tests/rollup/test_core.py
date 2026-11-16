from pathlib import Path
import json
import tempfile
import unittest

import pysubs2

from mediawrench.rollup import CaptionError, convert, parse_vtt

ROOT = Path(__file__).resolve().parents[2]


def vtt(*cues):
    return "WEBVTT\n\n" + "\n\n".join(f"{start} --> {end}\n{text}" for start, end, text in cues) + "\n"


def words(result):
    return " ".join(e.plaintext for e in pysubs2.SSAFile.from_string(result.srt, format_="srt")).split()


class RepairTests(unittest.TestCase):
    def test_rollup_and_later_repeat(self):
        result = convert((ROOT / "examples/rollup/rolling.vtt").read_text(encoding="utf-8"))
        self.assertEqual(words(result), "Hello there New line Hello there".split())
        self.assertEqual(result.report["summary"]["removed_tokens"], 2)

    def test_identical_overlapping_cues_merge_interval(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Hello there"),
                             ("00:01.000", "00:03.000", "Hello there")))
        self.assertEqual(words(result), ["Hello", "there"])
        self.assertEqual([(e.start, e.end) for e in pysubs2.SSAFile.from_string(result.srt, format_="srt")],
                         [(0, 3000)])

    def test_three_line_rollover_aliases_resolve_to_retained_tokens(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Alpha one\nBeta two\nGamma three"),
                             ("00:01.000", "00:03.000", "Beta two\nGamma three\nDelta four"),
                             ("00:02.000", "00:04.000", "Gamma three\nDelta four\nEpsilon five")))
        self.assertEqual(words(result), "Alpha one Beta two Gamma three Delta four Epsilon five".split())
        self.assertEqual(result.report["summary"]["removed_tokens"], 8)
        records = {t["id"]: t for t in result.provenance["tokens"]}
        self.assertEqual(len(records), 18)
        for token in records.values():
            root = records[token["representative"]]
            self.assertEqual(root["action"], "retained")
            self.assertEqual(root["text"], token["text"])
            self.assertEqual(root["output"], token["output"])

    def test_repeat_after_gap_even_with_large_threshold(self):
        data = vtt(("00:00.000", "00:02.000", "Hello there"), ("00:05.000", "00:07.000", "Hello there"))
        self.assertEqual(words(convert(data, max_gap_ms=10000)), "Hello there Hello there".split())

    def test_rollover_does_not_prolong_unmatched_earlier_lines(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Alpha one\nBeta two\nGamma three"),
                             ("00:01.000", "00:03.000", "Beta two\nGamma three\nDelta four"),
                             ("00:02.000", "00:04.000", "Gamma three\nDelta four\nEpsilon five")))
        by_text = {c["text"]: c for c in result.provenance["output_cues"]}
        self.assertEqual(by_text["Alpha one"]["end_ms"], 2000)
        self.assertEqual(by_text["Beta two"]["end_ms"], 3000)
        self.assertEqual(by_text["Gamma three"]["end_ms"], 4000)

    def test_adjacent_touching_identical_are_retained(self):
        data = vtt(("00:00.000", "00:01.000", "Thank you"), ("00:01.000", "00:02.000", "Thank you"))
        result = convert(data)
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        self.assertTrue(result.report["ambiguities"])

    def test_configurable_partial_rollup_gap(self):
        data = vtt(("00:00.000", "00:01.000", "Hello there"),
                   ("00:01.100", "00:03.000", "Hello there\nNew line"))
        self.assertEqual(len(words(convert(data))), 6)
        self.assertEqual(words(convert(data, max_gap_ms=100)), "Hello there New line".split())

    def test_ambiguous_repeated_phrase_retained(self):
        data = vtt(("00:00.000", "00:02.000", "go now go now"),
                   ("00:01.000", "00:03.000", "go now go now\nPlease stop"))
        result = convert(data)
        self.assertEqual(len(words(result)), 10)
        self.assertEqual(result.report["summary"]["ambiguous_pairs"], 1)

    def test_partial_line_match_retained(self):
        data = vtt(("00:00.000", "00:02.000", "I said very good"),
                   ("00:01.000", "00:03.000", "very good indeed"))
        result = convert(data)
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        self.assertIn("cuts through", result.report["ambiguities"][0]["reason"])

    def test_single_word_retained_and_flagged(self):
        data = vtt(("00:00.000", "00:02.000", "yes"), ("00:01.000", "00:03.000", "yes\nplease"))
        result = convert(data)
        self.assertEqual(words(result), ["yes", "yes", "please"])
        self.assertEqual(result.report["summary"]["ambiguous_pairs"], 1)

    def test_nonadjacent_repetition_preserved(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Hello there"),
                             ("00:00.500", "00:02.500", "Other speech"),
                             ("00:01.000", "00:03.000", "Hello there")))
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)

    def test_nested_cue_retained(self):
        result = convert(vtt(("00:00.000", "00:05.000", "Hello there"),
                             ("00:01.000", "00:03.000", "Hello there\nNew line")))
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        self.assertTrue(result.report["ambiguities"])

    def test_case_and_punctuation_sensitive(self):
        for second in ["hello there", "Hello there!"]:
            with self.subTest(second=second):
                result = convert(vtt(("00:00.000", "00:02.000", "Hello there"),
                                     ("00:01.000", "00:03.000", second + "\nNext words")))
                self.assertEqual(result.report["summary"]["removed_tokens"], 0)

    def test_unicode_bom_crlf_and_ids(self):
        data = "\ufeffWEBVTT\r\n\r\n讲者一\r\n00:00.000 --> 00:02.000\r\n你好 世界 🌍\r\n"
        result = convert(data)
        self.assertEqual(words(result), ["你好", "世界", "🌍"])
        self.assertTrue(all(t["source_cue_id"] == "讲者一" for t in result.provenance["tokens"]))

    def test_tags_require_explicit_flatten_and_report(self):
        data = vtt(("00:00.000", "00:02.000", "<v Alice><b>Hello</b> <i>there</i></v>"))
        with self.assertRaisesRegex(CaptionError, "--flatten"):
            convert(data)
        result = convert(data, flatten=True)
        self.assertEqual(words(result), ["Hello", "there"])
        self.assertEqual(result.report["warnings"][0]["kind"], "lossy_flattening")

    def test_common_tags_nested_and_implicit_voice_end(self):
        result = convert(vtt(("00:00.000", "00:02.000",
                              "<v Alice><c.green><u>Bon</u></c> <lang fr>jour</lang>")), flatten=True)
        self.assertEqual(words(result), ["Bon", "jour"])

    def test_changed_speaker_not_deduplicated(self):
        data = vtt(("00:00.000", "00:02.000", "<v Alice>Hello there</v>"),
                   ("00:01.000", "00:03.000", "<v Bob>Hello there</v>\nNew line"))
        self.assertEqual(convert(data, flatten=True).report["summary"]["removed_tokens"], 0)

    def test_inline_timestamps_preserved_in_provenance(self):
        data = vtt(("00:00.000", "00:04.000", "Hi <00:01.000>new <00:02.500>world"))
        result = convert(data, flatten=True)
        tokens = result.provenance["tokens"]
        self.assertEqual([(t["source_start_ms"], t["source_end_ms"]) for t in tokens],
                         [(0, 1000), (1000, 2500), (2500, 4000)])
        self.assertEqual(words(result), ["Hi", "new", "world"])
        self.assertEqual(data.split("\n\n")[1].split("\n", 1)[1].rstrip()[tokens[1]["source_char_start"]:
                         tokens[1]["source_char_end"]], "new")

    def test_disjoint_inline_speech_is_retained(self):
        data = vtt(("00:00.000", "00:03.000", "Hello there <00:01.000>"),
                   ("00:02.000", "00:04.000", "Hello there"))
        result = convert(data, flatten=True)
        self.assertEqual(words(result), "Hello there Hello there".split())
        self.assertIn("separate speech", result.report["ambiguities"][0]["reason"])

    def test_different_internal_line_boundaries_are_retained(self):
        data = vtt(("00:00.000", "00:02.000", "Hello\nthere friend"),
                   ("00:01.000", "00:03.000", "Hello there\nfriend\nNew line"))
        result = convert(data)
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        self.assertIn("internal line", result.report["ambiguities"][0]["reason"])

    def test_identical_nested_cues_merge(self):
        result = convert(vtt(("00:00.000", "00:05.000", "Hello there"),
                             ("00:01.000", "00:03.000", "Hello there")))
        self.assertEqual(words(result), ["Hello", "there"])
        self.assertEqual(result.provenance["output_cues"][0]["end_ms"], 5000)

    def test_simultaneous_partial_rollup_keeps_valid_intervals(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Hello there"),
                             ("00:00.000", "00:03.000", "Hello there\nNew line")))
        self.assertEqual(words(result), "Hello there New line".split())
        self.assertTrue(all(c["end_ms"] > c["start_ms"] for c in result.provenance["output_cues"]))

    def test_entity_decoding_without_hidden_loss(self):
        result = convert(vtt(("00:00.000", "00:02.000", "Café &amp; tea&nbsp;please")))
        self.assertEqual(words(result), ["Café", "&", "tea", "please"])
        self.assertEqual(result.provenance["source_cues"][0]["raw_text"], "Café &amp; tea&nbsp;please")

    def test_note_is_reported_with_original_text(self):
        data = "WEBVTT\n\nNOTE hello\nretained in map\n\n00:00.000 --> 00:01.000\nHello there\n"
        result = convert(data)
        self.assertIn("retained in map", result.report["warnings"][0]["text"])

    def test_real_file_roundtrip_and_idempotent_srt_rerun(self):
        first = convert((ROOT / "examples/rollup/rolling.vtt").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "actual.srt"
            target.write_text(first.srt, encoding="utf-8")
            loaded = pysubs2.load(str(target), encoding="utf-8", format_="srt")
            self.assertEqual(" ".join(e.plaintext for e in loaded).split(),
                             "Hello there New line Hello there".split())
            rerun = convert(target.read_text(encoding="utf-8"), input_format="srt")
        self.assertEqual(first.srt, rerun.srt)
        self.assertEqual(rerun.report["mode"], "srt_passthrough")
        self.assertEqual(rerun.report["summary"]["removed_tokens"], 0)

    def test_output_time_bounds(self):
        result = convert(vtt(("99:59:58.000", "99:59:59.999", "Last two words")))
        self.assertIn("99:59:59,999", result.srt)

    def test_input_errors(self):
        invalid = ["", "WEBVTT", "WEBVTT\nX-TIMESTAMP-MAP=abc\n\n",
                   "WEBVTT\n\nSTYLE\n::cue {color:red}\n",
                   "WEBVTT\n\nREGION\nid:top\n",
                   vtt(("00:00.000", "00:01.000 align:start", "Hello there")),
                   vtt(("00:00.000", "00:01.000", "<ruby>日<rt>にち</rt></ruby>")),
                   vtt(("00:00.000", "00:01.000", "<script>Hello</script>")),
                   vtt(("00:00.000", "00:01.000", "<b>Hello</i>")),
                   vtt(("00:00.000", "00:01.000", "<b>Hello")),
                   vtt(("00:00.000", "00:01.000", "Hello &bogus;")),
                   vtt(("00:00.000", "00:01.000", "Hello \x00 there")),
                   vtt(("00:00.000", "00:01.000", " "))]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(CaptionError):
                convert(data, flatten=True)

    def test_invalid_times(self):
        pairs = [("-00:01.000", "00:02.000"), ("00:00.000", "00:00.000"),
                 ("00:03.000", "00:02.000"), ("00:60.000", "00:62.000"),
                 ("60:00.000", "61:00.000"), ("0:01.000", "00:02.000"),
                 ("00:00,000", "00:02.000"), ("00:00.0", "00:02.000"),
                 ("100:00:00.000", "100:00:02.000")]
        for start, end in pairs:
            with self.subTest(start=start, end=end), self.assertRaises(CaptionError):
                convert(vtt((start, end, "Hello there")))

    def test_invalid_inline_timestamps(self):
        for text in ["<00:00.000>Hello there", "<00:02.000>Hello there",
                     "<00:01.000>Hello <00:00.500>there", "<00:01.00>Hello"]:
            with self.subTest(text=text), self.assertRaises(CaptionError):
                convert(vtt(("00:00.000", "00:02.000", text)), flatten=True)

    def test_unsorted_cues_rejected(self):
        with self.assertRaisesRegex(CaptionError, "ordered"):
            convert(vtt(("00:02.000", "00:04.000", "Hello there"),
                        ("00:00.000", "00:03.000", "New line")))

    def test_unsafe_srt_literal_is_rejected_not_stripped(self):
        for text in ["a {word}", r"literal \N", "a &lt;b&gt; there"]:
            with self.subTest(text=text), self.assertRaises(CaptionError):
                convert(vtt(("00:00.000", "00:02.000", text)))

    def test_invalid_library_options(self):
        for gap in [-1, 0.5, True, "2"]:
            with self.subTest(gap=gap), self.assertRaises(CaptionError):
                convert("WEBVTT\n\n", max_gap_ms=gap)
        with self.assertRaises(CaptionError):
            convert("whatever", input_format="ass")

    def test_strict_srt_rejects_silent_parser_loss(self):
        for text in ["junk", "1\n00:00:00,000 --> 00:00:01,000\n<b>Hi</b>\n",
                     "1\n00:00:00,000 --> 00:00:00,000\nHi\n",
                     "1\n00:00:00,000 --> 00:00:01,000\nHi\n\njunk"]:
            with self.subTest(text=text), self.assertRaises(CaptionError):
                convert(text, input_format="srt")

    def test_all_labeled_cases(self):
        from examples.rollup.experiment import stamp
        cases = json.loads((ROOT / "examples/rollup/cases.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["name"]):
                result = convert(vtt(*[(stamp(a), stamp(b), t) for a, b, t in case["cues"]]))
                self.assertEqual(words(result), case["expected"].split())
                self.assertEqual(result.report["summary"]["removed_tokens"], case["duplicates"])
