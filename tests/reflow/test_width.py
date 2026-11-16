"""Synthetic integration cases with fixed, independently specified widths."""
import html
import itertools
import unittest

import pysubs2

from mediawrench.reflow import Config, InputError, normalize, reflow
from mediawrench.reflow.io import srt_text
from mediawrench.reflow.layout import layout
from mediawrench.reflow.optimize import _candidate


def fallback(text):
    return {"segments": [{"text": text, "start": 0, "end": 3}]}


class WidthTests(unittest.TestCase):
    def assert_preserved(self, report, text):
        self.assertEqual("".join(c["original_text"] for c in report["cues"]), text)
        self.assertTrue(report["metrics"]["tokens_conserved"])
        self.assertTrue(report["metrics"]["original_text_conserved"])
        decoded = pysubs2.SSAFile.from_string(srt_text(report), format_="srt", keep_html_tags=True)
        self.assertEqual([html.unescape(s.text.replace(r"\N", "\n")) for s in decoded],
                         [c["text"] for c in report["cues"]])

    def test_cjk_modes_have_explicit_widths(self):
        expected = {"codepoint": (["你好世界"], [4]), "display": (["你好", "世界"], [4, 4])}
        for mode, (lines, widths) in expected.items():
            report = reflow(fallback("你好世界"), Config(max_chars=4, width_mode=mode))
            self.assertEqual(report["cues"][0]["lines"], lines)
            self.assertEqual(report["cues"][0]["line_widths"], widths)
            self.assertEqual(report["config"]["width_mode"], mode)
            self.assertTrue(report["feasible"])
            self.assert_preserved(report, "你好世界")

    def test_combining_emoji_flag_and_keycap_widths(self):
        for text, codepoints, cells in (("e\u0301", 2, 1), ("👩🏽‍💻", 4, 2),
                                        ("🇨🇳", 2, 2), ("1️⃣", 3, 2), ("Ａ", 1, 2)):
            for mode, width in (("codepoint", codepoints), ("display", cells)):
                with self.subTest(text=text, mode=mode):
                    report = reflow(fallback(text), Config(max_chars=10, width_mode=mode))
                    self.assertEqual(report["cues"][0]["line_widths"], [width])
                    self.assert_preserved(report, text)
        report = reflow(fallback("e\u0301 👩🏽‍💻"), Config(max_chars=2, width_mode="display"))
        self.assertEqual(report["cues"][0]["lines"], ["e\u0301", "👩🏽‍💻"])
        self.assertEqual(report["cues"][0]["line_widths"], [1, 2])
        self.assertTrue(report["feasible"])
        self.assert_preserved(report, "e\u0301 👩🏽‍💻")

    def test_impossible_cluster_width_never_splits_or_truncates(self):
        for mode in ("codepoint", "display"):
            for text, lines, widths in (("か\u3099か", ["か\u3099", "か"], [2, 1] if mode == "codepoint" else [2, 2]),
                                       ("👩🏽‍💻", ["👩🏽‍💻"], [4] if mode == "codepoint" else [2])):
                report = reflow(fallback(text), Config(max_chars=1, width_mode=mode))
                self.assertEqual(report["cues"][0]["lines"], lines)
                self.assertEqual(report["cues"][0]["line_widths"], widths)
                self.assertFalse(report["feasible"])
                self.assertEqual(report["constraints"][0]["code"], "line_width")
                self.assertEqual(report["constraints"][0]["width_mode"], mode)
                self.assert_preserved(report, text)

    def test_dp_pruning_uses_display_width_not_codepoint_count(self):
        # 12 code points excluding the separator, but only six cells in total.
        text = "e\u0301e\u0301e\u0301 e\u0301e\u0301e\u0301"
        fixture = {"segments": [{"text": text, "words": [
            {"word": "e\u0301e\u0301e\u0301", "start": 0, "end": 1.5},
            {"word": "e\u0301e\u0301e\u0301", "start": 1.5, "end": 3}]}]}
        report = reflow(fixture, Config(max_chars=3, width_mode="display"))
        self.assertEqual(len(report["cues"]), 1)
        self.assertEqual(report["cues"][0]["line_widths"], [3, 3])
        self.assertTrue(report["feasible"])
        self.assert_preserved(report, text)

    def test_word_boundaries_inside_graphemes_are_indivisible(self):
        for parts in (("か", "\u3099", "か"), ("👩🏽‍", "💻"), ("e", "\u0301")):
            text = "".join(parts)
            fixture = {"segments": [{"text": text, "words": [
                {"word": part, "start": i, "end": i + 1} for i, part in enumerate(parts)]}]}
            for mode in ("codepoint", "display"):
                for algorithm in ("dp", "fixed"):
                    with self.subTest(parts=parts, mode=mode, algorithm=algorithm):
                        report = reflow(fixture, Config(max_chars=1, width_mode=mode),
                                        algorithm=algorithm, group_words=1)
                        self.assertEqual(report["cues"][0]["word_ids"][:2],
                                         ["/segments/0/words/0", "/segments/0/words/1"])
                        self.assert_preserved(report, text)

    def test_synthesized_text_does_not_insert_spaces_inside_clusters(self):
        for parts in (("e", "\u0301"), ("👩🏽‍", "💻"), ("🇨", "🇳")):
            records = [{"word": part, "start": i, "end": i + 1} for i, part in enumerate(parts)]
            for fixture in ({"words": records}, {"segments": [{"words": records}]}):
                for mode in ("codepoint", "display"):
                    with self.subTest(parts=parts, fixture=fixture, mode=mode):
                        report = reflow(fixture, Config(max_chars=1, width_mode=mode))
                        self.assertEqual([c["original_text"] for c in report["cues"]], ["".join(parts)])
                        self.assertFalse(any(d["code"] == "inferred_separator" for d in report["diagnostics"]))
                        self.assert_preserved(report, "".join(parts))

    def test_default_mode_and_speaking_rate_remain_codepoints(self):
        default = reflow(fallback("e\u0301 👩🏽‍💻"), Config(max_chars=2))
        explicit = reflow(fallback("e\u0301 👩🏽‍💻"), Config(max_chars=2, width_mode="codepoint"))
        self.assertEqual(default, explicit)
        for mode in ("codepoint", "display"):
            report = reflow(fallback("e\u0301"), Config(max_cps=0.5, width_mode=mode))
            self.assertEqual(report["constraints"][0]["code"], "speaking_rate")
            self.assertEqual(report["constraints"][0]["cps"], 0.666667)

    def test_atomic_group_duration_can_be_infeasible_without_splitting(self):
        fixture = {"segments": [{"text": "e\u0301", "words": [
            {"word": "e", "start": 0, "end": 1}, {"word": "\u0301", "start": 1, "end": 2}]}]}
        report = reflow(fixture, Config(target_duration=1, max_duration=1, width_mode="display"))
        self.assertFalse(report["feasible"])
        self.assertEqual([c["text"] for c in report["cues"]], ["e\u0301"])
        self.assertEqual([v["code"] for v in report["constraints"]], ["duration"])
        self.assertTrue(any(d["code"] == "grapheme_boundary_join" for d in report["diagnostics"]))
        self.assert_preserved(report, "e\u0301")

    def test_hangul_jamo_clusters_break_only_between_syllables(self):
        text = "\u1100\u1161\u11a8" * 2
        for mode, width in (("codepoint", 3), ("display", 2)):
            self.assertEqual(layout(text, width, mode), ([text[:3], text[3:]], 0))

    def test_display_dp_matches_all_short_partitions(self):
        # Enumerate all partitions without the DP pruning rule under test.
        for words in (("e\u0301" * 3, " e\u0301" * 3, " 你好", " 👩🏽‍💻"),
                      ("你好", "世界", "再见", "朋友")):
            fixture = {"segments": [{"text": "".join(words), "words": [
                {"word": word, "start": i * 0.6, "end": (i + 1) * 0.6}
                for i, word in enumerate(words)]}]}
            config = Config(max_chars=4, target_duration=1.5, max_duration=3, width_mode="display")
            units = normalize(fixture).units
            scores = []
            for mask in itertools.product((False, True), repeat=len(words) - 1):
                points = [0] + [i + 1 for i, cut in enumerate(mask) if cut] + [len(words)]
                cues = [_candidate(units, a, b, config) for a, b in zip(points, points[1:])]
                if any(b - a > 1 and any(v["code"] in ("line_width", "duration") for v in c["violations"])
                       for (a, b), c in zip(zip(points, points[1:]), cues)):
                    continue
                scores.append([sum(c["score"][i] for c in cues) for i in range(3)])
            self.assertEqual(reflow(fixture, config)["objective"], min(scores))

    def test_invalid_mode_and_unmeasurable_display_text_fail(self):
        with self.assertRaisesRegex(InputError, "width_mode"):
            Config(width_mode="pixels")
        with self.assertRaisesRegex(InputError, "display width"):
            reflow(fallback("a\u009bb"), Config(width_mode="display"))

    def test_cluster_crossing_segment_boundary_is_rejected(self):
        fixture = {"segments": [{"text": "か", "start": 0, "end": 1},
                                 {"text": "\u3099", "start": 1, "end": 2}]}
        with self.assertRaisesRegex(InputError, "grapheme.*segment"):
            reflow(fixture)


if __name__ == "__main__":
    unittest.main()
