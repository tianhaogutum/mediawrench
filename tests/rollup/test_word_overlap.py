"""Synthetic WebVTT integration fixtures for explicit word-overlap repair."""
from pathlib import Path
import unittest

import pysubs2

from mediawrench.rollup import CaptionError, convert, parse_vtt
from .test_core import vtt, words

ROOT = Path(__file__).resolve().parents[2]


class WordOverlapTests(unittest.TestCase):
    def assert_provenance(self, result):
        data = result.provenance
        records = {t["id"]: t for t in data["tokens"]}
        self.assertEqual(len(records), data["summary"]["input_tokens"])
        for token in records.values():
            root = records[token["representative"]]
            self.assertEqual(root["action"], "retained")
            self.assertEqual(root["text"], token["text"])
            self.assertEqual(root["output"], token["output"])
            source = data["source_cues"][token["source_cue"] - 1]
            self.assertEqual(source["raw_text"][token["source_char_start"]:token["source_char_end"]],
                             token["text"])
        for output in data["output_cues"]:
            span = output["source_span"]
            ids = [f'c{span["cue"]}:t{n}' for n in range(span["token_start"], span["token_end"])]
            self.assertEqual([records[key]["text"] for key in ids], output["text"].split())
            self.assertTrue(all(records[key]["action"] == "retained" for key in ids))
            self.assertGreater(output["end_ms"], output["start_ms"])

    def test_growing_line_retains_new_words_and_source_spans(self):
        result = convert((ROOT / "examples/rollup/word-growing.vtt").read_text(encoding="utf-8"),
                         word_overlap=True)
        self.assertEqual(words(result), "We are ready now".split())
        self.assertEqual(result.report["summary"]["removed_tokens"], 5)
        self.assertEqual([(c["start_ms"], c["end_ms"], c["text"], c["source_span"])
                          for c in result.provenance["output_cues"]], [
            (0, 4000, "We are", {"cue": 1, "token_start": 1, "token_end": 3}),
            (1000, 4000, "ready", {"cue": 2, "token_start": 3, "token_end": 4}),
            (2000, 4000, "now", {"cue": 3, "token_start": 4, "token_end": 5})])
        self.assertFalse(result.report["ambiguities"])
        self.assertTrue(all(e["reason"] == "adjacent_word_rollup" for e in result.provenance["edits"]))
        self.assert_provenance(result)

    def test_sliding_word_window_splits_only_extended_tokens(self):
        result = convert(vtt(("00:00.000", "00:02.000", "We are"),
                             ("00:01.000", "00:03.000", "We are ready"),
                             ("00:02.000", "00:04.000", "are ready now")), word_overlap=True)
        self.assertEqual(words(result), "We are ready now".split())
        self.assertEqual([(c["text"], c["start_ms"], c["end_ms"])
                          for c in result.provenance["output_cues"]],
                         [("We", 0, 3000), ("are", 0, 4000),
                          ("ready", 1000, 4000), ("now", 2000, 4000)])
        self.assert_provenance(result)

    def test_rewrapped_growth_preserves_source_lines_and_readback_locations(self):
        text = (ROOT / "examples/rollup/word-rewrapped.vtt").read_text(encoding="utf-8")
        for output_format in ["srt", "vtt"]:
            with self.subTest(output_format=output_format):
                result = convert(text, word_overlap=True, output_format=output_format)
                self.assertEqual([(c["start_ms"], c["end_ms"], c["text"], c["source_span"])
                                  for c in result.provenance["output_cues"]], [
                    (0, 3000, "We\nare ready", {"cue": 1, "token_start": 1, "token_end": 4}),
                    (1000, 3000, "now", {"cue": 2, "token_start": 4, "token_end": 5})])
                self.assertEqual(result.report["summary"]["removed_tokens"], 3)
                self.assertFalse(result.report["ambiguities"])
                self.assert_provenance(result)
                if output_format == "srt":
                    loaded = pysubs2.SSAFile.from_string(result.srt, format_="srt")
                    readback = [(c.start, c.end, c.plaintext) for c in loaded]
                else:
                    loaded, _ = parse_vtt(result.vtt, preserve_metadata=True)
                    readback = [(c.start_ms, c.end_ms, c.plain) for c in loaded]
                    self.assertEqual([c.identifier for c in loaded], ["seed", "growth"])
                self.assertEqual(readback, [(0, 3000, "We\nare ready"), (1000, 3000, "now")])
                for token in result.provenance["tokens"]:
                    location = token["output"]
                    self.assertEqual(readback[location["cue"] - 1][2].split()[location["token"] - 1],
                                     token["text"])
                self.assertEqual([t["source_line"] for t in result.provenance["tokens"]],
                                 [1, 2, 2, 1, 1, 2, 2])

    def test_rewrapped_pair_requires_opt_in(self):
        text = (ROOT / "examples/rollup/word-rewrapped.vtt").read_text(encoding="utf-8")
        default, disabled = convert(text), convert(text, word_overlap=False)
        self.assertEqual(default.provenance, disabled.provenance)
        self.assertEqual(default.srt, disabled.srt)
        self.assertEqual(words(default), "We are ready We are ready now".split())
        self.assertEqual(default.report["summary"]["removed_tokens"], 0)
        self.assertIn("line", default.report["ambiguities"][0]["reason"])
        identical = convert(text.replace("ready now", "ready"))
        self.assertEqual(identical.report["summary"]["removed_tokens"], 0)
        self.assertIn("internal line", identical.report["ambiguities"][0]["reason"])

    def test_rewrapped_sliding_chain_extends_only_matching_words(self):
        result = convert(vtt(("00:00.000", "00:02.000", "We\nare ready"),
                             ("00:01.000", "00:03.000", "are\nready now"),
                             ("00:02.000", "00:04.000", "ready\nnow today")), word_overlap=True)
        self.assertEqual(words(result), "We are ready now today".split())
        self.assertEqual([(c["start_ms"], c["end_ms"], c["text"])
                          for c in result.provenance["output_cues"]],
                         [(0, 2000, "We"), (0, 3000, "are"), (0, 4000, "ready"),
                          (1000, 4000, "now"), (2000, 4000, "today")])
        self.assertEqual(result.report["summary"]["removed_tokens"], 4)
        self.assertFalse(result.report["ambiguities"])
        self.assert_provenance(result)

    def test_rewrapped_identical_display_uses_interval_union(self):
        for end in ["00:01.500", "00:03.000"]:
            with self.subTest(end=end):
                result = convert(vtt(("00:00.000", "00:02.000", "We\nare ready"),
                                     ("00:01.000", end, "We are\nready")), word_overlap=True)
                self.assertEqual(words(result), "We are ready".split())
                self.assertEqual([(c["text"], c["end_ms"]) for c in result.provenance["output_cues"]],
                                 [("We\nare ready", 2000 if end == "00:01.500" else 3000)])
                self.assert_provenance(result)

    def test_rewrapped_candidates_still_require_conservative_evidence(self):
        cases = [
            ("We\nare ready", "We are\nready now", "00:02.000", "00:03.000", "strict temporal"),
            ("We\nare ready", "We are\nready now", "00:02.001", "00:03.000", "strict temporal"),
            ("We\nare ready", "We are\nready now", "00:01.000", "00:01.500", "nested cue"),
            ("We\nare ready", "are\nready", "00:01.000", "00:03.000", "suffix-only"),
            ("We\nare ready", "we are\nready now", "00:01.000", "00:03.000", None),
            ("We\nare ready", "We are\nready! now", "00:01.000", "00:03.000", None),
            ("We\nare ready", "ready\nnow", "00:01.000", "00:03.000", "short/repetitive"),
            ("go\nnow go now", "go now\ngo now today", "00:01.000", "00:03.000", "multiple possible"),
            (".\n!", ". !\nnext", "00:01.000", "00:03.000", "word evidence"),
            ("We\nare <00:00.500>ready", "We are\nready now", "00:01.000", "00:03.000", "separate speech"),
        ]
        for old, new, start, end, reason in cases:
            with self.subTest(old=old, new=new, start=start, end=end):
                result = convert(vtt(("00:00.000", "00:02.000", old), (start, end, new)),
                                 word_overlap=True, flatten=True, max_gap_ms=10000)
                self.assertEqual(result.report["summary"]["removed_tokens"], 0)
                self.assertEqual([c["end_ms"] for c in result.provenance["output_cues"]],
                                 [2000, 1500 if end == "00:01.500" else 3000])
                if reason:
                    self.assertIn(reason, result.report["ambiguities"][0]["reason"])

    def test_rewrapped_contexts_must_agree(self):
        for tag, other in [("v Alice", "v Bob"), ("lang en", "lang fr"), ("b", "i")]:
            with self.subTest(tag=tag):
                old = f'<{tag}>We\nare ready</{tag.split()[0]}>'
                for current_tag, removed in [(tag, 3), (other, 0)]:
                    new = f'<{current_tag}>We are\nready now</{current_tag.split()[0]}>'
                    result = convert(vtt(("00:00.000", "00:02.000", old),
                                         ("00:01.000", "00:03.000", new)),
                                     word_overlap=True, flatten=True)
                    self.assertEqual(result.report["summary"]["removed_tokens"], removed)
                    if not removed:
                        self.assertIn("contexts", result.report["ambiguities"][0]["reason"])

    def test_rewrapped_vtt_settings_must_agree(self):
        text = (ROOT / "examples/rollup/word-rewrapped.vtt").read_text(encoding="utf-8")
        text = text.replace("00:02.000\n", "00:02.000 align:start\n")
        for setting, removed in [("align:start", 3), ("align:end", 0)]:
            with self.subTest(setting=setting):
                result = convert(text.replace("00:03.000\n", f"00:03.000 {setting}\n"),
                                 word_overlap=True, output_format="vtt")
                self.assertEqual(result.report["summary"]["removed_tokens"], removed)
                if not removed:
                    self.assertIn("settings", result.report["ambiguities"][0]["reason"])
                    self.assertEqual(result.provenance["output_cues"][0]["end_ms"], 2000)

    def test_default_and_explicit_false_preserve_existing_behavior(self):
        text = (ROOT / "examples/rollup/word-growing.vtt").read_text(encoding="utf-8")
        default, disabled = convert(text), convert(text, word_overlap=False)
        self.assertEqual(default.srt, disabled.srt)
        self.assertEqual(default.provenance, disabled.provenance)
        self.assertEqual(words(default), "We are We are ready We are ready now".split())
        self.assertEqual(default.report["summary"]["removed_tokens"], 0)
        self.assertEqual(default.provenance["options"], {"flatten": False, "max_gap_ms": 0})

    def test_pauses_and_touching_cues_are_diagnosed_even_with_gap_override(self):
        for start in ["00:02.000", "00:02.001", "00:05.000"]:
            with self.subTest(start=start):
                text = vtt(("00:00.000", "00:02.000", "We are"),
                           (start, "00:07.000", "We are ready"))
                result = convert(text, word_overlap=True, max_gap_ms=10000)
                self.assertEqual(words(result), "We are We are ready".split())
                self.assertIn("strict temporal overlap", result.report["ambiguities"][0]["reason"])

    def test_uncertain_matches_keep_original_words_with_diagnostic(self):
        cases = [
            ("yes", "yes please", "short/repetitive"),
            ("go go", "go go now", "multiple possible alignments"),
            ("We go go", "go go now", "multiple possible alignments"),
            ("你好世界", "你好世界今天", "word boundaries"),
            ("你好世界", "你好世界", "short/repetitive"),
            ("We ar", "We are ready", "word boundaries"),
            (". !", ". ! next", "word evidence"),
            ("We are ready", "are ready", "suffix-only"),
        ]
        for old, new, reason in cases:
            with self.subTest(old=old, new=new):
                result = convert(vtt(("00:00.000", "00:02.000", old),
                                     ("00:01.000", "00:03.000", new)), word_overlap=True)
                self.assertEqual(words(result), (old + " " + new).split())
                self.assertEqual(result.report["summary"]["removed_tokens"], 0)
                self.assertIn(reason, result.report["ambiguities"][0]["reason"])

    def test_context_mismatch_is_diagnosed(self):
        for old, new in [("<v Alice>We are</v>", "<v Bob>We are ready</v>"),
                         ("<lang en>We are</lang>", "<lang fr>We are ready</lang>"),
                         ("<b>We are</b>", "We are ready")]:
            with self.subTest(old=old):
                result = convert(vtt(("00:00.000", "00:02.000", old),
                                     ("00:01.000", "00:03.000", new)),
                                 flatten=True, word_overlap=True)
                self.assertEqual(words(result), "We are We are ready".split())
                self.assertIn("contexts", result.report["ambiguities"][0]["reason"])

    def test_disjoint_inline_speech_and_nested_growth_are_retained(self):
        cases = [
            ("00:03.000", "00:02.000", "00:04.000", "We are <00:01.000>", "separate speech"),
            ("00:05.000", "00:01.000", "00:03.000", "We are", "nested cue"),
        ]
        for end, start2, end2, old, reason in cases:
            with self.subTest(reason=reason):
                result = convert(vtt(("00:00.000", end, old), (start2, end2, "We are ready")),
                                 flatten=True, word_overlap=True)
                self.assertEqual(words(result), "We are We are ready".split())
                self.assertIn(reason, result.report["ambiguities"][0]["reason"])

    def test_supported_inline_word_growth_keeps_segment_timing(self):
        text = vtt(("00:00.000", "00:03.000", "We <00:01.000>are"),
                   ("00:00.500", "00:04.000", "We <00:01.000>are <00:02.000>ready"))
        result = convert(text, flatten=True, word_overlap=True)
        self.assertEqual(words(result), "We are ready".split())
        self.assertEqual([(t["source_start_ms"], t["source_end_ms"])
                          for t in result.provenance["tokens"]],
                         [(0, 1000), (1000, 3000), (500, 1000), (1000, 2000), (2000, 4000)])

    def test_no_fuzzy_or_nonadjacent_repair(self):
        text = vtt(("00:00.000", "00:03.000", "We are"),
                   ("00:00.500", "00:03.500", "Other speech"),
                   ("00:01.000", "00:04.000", "We are ready"))
        self.assertEqual(words(convert(text, word_overlap=True)),
                         "We are Other speech We are ready".split())
        for new in ["we are ready", "We are! ready"]:
            with self.subTest(new=new):
                result = convert(vtt(("00:00.000", "00:02.000", "We are"),
                                     ("00:01.000", "00:03.000", new)), word_overlap=True)
                self.assertEqual(result.report["summary"]["removed_tokens"], 0)

    def test_srt_rerun_with_mode_is_still_passthrough(self):
        first = convert((ROOT / "examples/rollup/word-growing.vtt").read_text(encoding="utf-8"), word_overlap=True)
        second = convert(first.srt, input_format="srt", word_overlap=True)
        self.assertEqual(first.srt, second.srt)
        self.assertEqual(second.report["mode"], "srt_passthrough")
        self.assertEqual(second.report["summary"]["removed_tokens"], 0)

    def test_word_overlap_requires_boolean(self):
        for value in [1, None, "true"]:
            with self.subTest(value=value), self.assertRaisesRegex(CaptionError, "word_overlap"):
                convert("WEBVTT\n\n", word_overlap=value)
