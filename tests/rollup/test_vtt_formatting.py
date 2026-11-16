"""Synthetic contracts for balanced b/i/u on retained WebVTT spans."""
import hashlib
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert, parse_vtt
from mediawrench.rollup import core


def document(*payloads, settings=""):
    return "WEBVTT Formatting\nLanguage: en\n\n" + "\n\n".join(
        f"source-{i}\n00:0{i - 1}.000 --> 00:0{i + 1}.000{settings}\n{payload}"
        for i, payload in enumerate(payloads, 1)) + "\n"


class VttFormattingTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        original, _ = parse_vtt(source, preserve_metadata=True)
        loaded, warnings = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertFalse(warnings)
        data = result.provenance
        self.assertEqual(data["vtt_sha256"], hashlib.sha256(result.vtt.encode()).hexdigest())
        records = {r["id"]: r for r in data["tokens"]}
        for record in records.values():
            with self.subTest(token=record["id"]):
                cue = original[record["source_cue"] - 1]
                token = next(t for t in cue.tokens if t.id == record["id"])
                self.assertEqual((record["source_char_start"], record["source_char_end"], record["source_line"]),
                                 (token.char_start, token.char_end, token.line))
                # Source ranges remain in the original markup, not rendered text.
                self.assertTrue(cue.raw[token.char_start:token.char_end])
                root = records[record["representative"]]
                self.assertEqual(root["action"], "retained")
                self.assertEqual(root["output"], record["output"])
                location = record["output"]
                actual = loaded[location["cue"] - 1].tokens[location["token"] - 1]
                self.assertEqual(actual.text, token.text)
                self.assertEqual(actual.contexts, token.contexts)
                self.assertEqual(actual.glyph_contexts, token.glyph_contexts)
        for emitted, record in zip(loaded, data["output_cues"]):
            span = record["source_span"]
            source_cue = original[span["cue"] - 1]
            self.assertEqual([t.id for t in source_cue.tokens[span["token_start"] - 1:span["token_end"] - 1]],
                             [r["id"] for r in records.values() if r["action"] == "retained" and r["output"]["cue"] == record["index"]])
            self.assertEqual((emitted.identifier, emitted.settings), (source_cue.identifier, source_cue.settings))
        return loaded

    def test_whole_line_formatting_contract(self):
        source = document("<b>Hello there</b>", "<b>Hello there</b>\nNew line", settings=" align:start")
        result = convert(source, output_format="vtt")
        loaded = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in loaded],
                         [(0, 3000, "<b>Hello there</b>"), (1000, 3000, "New line")])
        self.assertEqual(result.report["summary"]["removed_tokens"], 2)
        self.assertEqual(result.provenance["tokens"][0]["source_char_start"], 3)
        self.assertEqual(result.provenance["tokens"][1]["source_char_end"], 14)
        self.assertEqual(result.provenance["tokens"][-1]["source_char_start"], 23)
        self.assertFalse(result.report["warnings"])

    def test_rewrapped_nested_tags_reopen_on_new_span(self):
        source = document("<b><i>We\nare ready</i></b>", "<b><i>We are\nready <u>now</u></i></b>")
        result = convert(source, output_format="vtt", word_overlap=True)
        loaded = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in loaded],
                         [(0, 3000, "<b><i>We\nare ready</i></b>"),
                          (1000, 3000, "<b><i><u>now</u></i></b>")])
        self.assertEqual(result.provenance["output_cues"][1]["source_span"],
                         {"cue": 2, "token_start": 4, "token_end": 5})

    def test_sliding_word_splits_close_and_reopen_tags(self):
        source = document("<b><i>We\nare ready</i></b>", "<b><i>are\nready now</i></b>",
                          "<b><i>ready now today</i></b>")
        result = convert(source, output_format="vtt", word_overlap=True)
        loaded = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in loaded],
                         [(0, 2000, "<b><i>We</i></b>"), (0, 3000, "<b><i>are</i></b>"),
                          (0, 4000, "<b><i>ready</i></b>"), (1000, 4000, "<b><i>now</i></b>"),
                          (2000, 4000, "<b><i>today</i></b>")])

    def test_within_word_changes_entities_and_whitespace(self):
        source = document("<b>H</b>ello\t<i>th<u>er</u>e</i> &amp; &lt;b&gt;",
                          "<b>H</b>ello <i>th<u>er</u>e</i> &amp; &lt;b&gt; <u>now</u>")
        result = convert(source, output_format="vtt", word_overlap=True)
        loaded = self.assert_mappings(source, result)
        self.assertEqual([c.raw for c in loaded],
                         ["<b>H</b>ello <i>th<u>er</u>e</i> &amp; &lt;b&gt;", "<u>now</u>"])
        self.assertEqual(loaded[0].tokens[0].glyph_contexts, (("b",), (), (), (), ()))
        raw = result.provenance["source_cues"][0]["raw_text"]
        for token, expected in zip(result.provenance["tokens"][:3],
                                   ["H</b>ello", "th<u>er</u>e", "&amp;"]):
            self.assertEqual(raw[token["source_char_start"]:token["source_char_end"]], expected)
        token = result.provenance["tokens"][2]
        self.assertEqual(raw[token["source_char_start"]:token["source_char_end"]], "&amp;")

    def test_different_formatting_retains_both_displays(self):
        pairs = [("<b>Hello there</b>", "<i>Hello there</i>"),
                 ("<b>H</b>ello there", "<b>He</b>llo there"),
                 ("<b><i>Hello there</i></b>", "<i><b>Hello there</b></i>")]
        for old, new in pairs:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    source = document(old, new + "\nNew line")
                    result = convert(source, output_format="vtt", word_overlap=word)
                    loaded = self.assert_mappings(source, result)
                    self.assertEqual(result.report["summary"]["removed_tokens"], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in loaded], [(0, 2000), (1000, 3000)])
                    if word:
                        self.assertIn("contexts", result.report["ambiguities"][0]["reason"])

    def test_formatting_does_not_bypass_existing_overlap_guards(self):
        source = document("<b>We\nare ready</b>", "<b>We are\nready now</b>")
        result = convert(source, output_format="vtt")
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        for changed, reason in [(source.replace("00:01.000 -->", "00:02.000 -->"), "strict temporal"),
                                (source.replace("00:03.000\n", "00:03.000 align:end\n"), "settings")]:
            with self.subTest(reason=reason):
                result = convert(changed, output_format="vtt", word_overlap=True, max_gap_ms=10000)
                self.assertEqual(result.report["summary"]["removed_tokens"], 0)
                self.assertIn(reason, result.report["ambiguities"][0]["reason"])

    def test_unsupported_and_malformed_markup_still_rejected(self):
        payloads = ["<b>Hello <00:00.500>there</b>", "<c.>Hello there</c>",
                    "<b.red>Hello there</b.red>", "<i.>Hello there</i>", "<u.red >Hello there</u>",
                    "<v Ada>Hello there", "<lang en>Hello there", "<ruby>Hello there</ruby>",
                    "<b><i>Hello there</b></i>", "<b>Hello there", "Hello</b> there", "<b>Hello &bad; there</b>"]
        for payload in payloads:
            for flatten in (False, True):
                with self.subTest(payload=payload, flatten=flatten), self.assertRaises(CaptionError):
                    convert("WEBVTT\n\n00:00.000 --> 00:01.000\n" + payload,
                            output_format="vtt", flatten=flatten)

    def test_srt_still_requires_explicit_flattening(self):
        source = "WEBVTT\n\n00:00.000 --> 00:02.000\n<b>Hello there</b>\n"
        with self.assertRaisesRegex(CaptionError, "flatten"):
            convert(source)
        result = convert(source, flatten=True)
        self.assertIn("Hello there", result.srt)
        self.assertEqual(result.report["warnings"][0]["kind"], "lossy_flattening")

    def test_readback_detects_formatting_loss(self):
        source = document("<b>Hello there</b>")
        real_parser = core._parse_vtt_document
        calls = 0

        def corrupt_readback(text, **options):
            nonlocal calls
            calls += 1
            if calls == 2:
                text = text.replace("<b>", "<i>").replace("</b>", "</i>")
            return real_parser(text, **options)

        with patch.object(core, "_parse_vtt_document", side_effect=corrupt_readback):
            with self.assertRaisesRegex(CaptionError, "roundtrip"):
                convert(source, output_format="vtt")
        self.assertEqual(calls, 2)
