"""Speaking-rate contracts use fixed counts, independent of cell widths."""
import copy
import html
import unittest

import pysubs2

from mediawrench.reflow import Config, InputError, reflow
from mediawrench.reflow.io import srt_text


def fallback(text, end=1):
    return {"segments": [{"text": text, "start": 0, "end": end}]}


class RateTests(unittest.TestCase):
    def assert_rate(self, cue, mode, count, cps):
        unit = "codepoints" if mode == "codepoint" else "graphemes"
        self.assertEqual(cue["speaking_rate"], {
            "cps_mode": mode, "count": count, "count_unit": unit,
            "cps": cps, "rate_unit": unit + "/second"})

    def test_codepoint_default_is_compatible_and_has_explicit_units(self):
        fixture = fallback("e\u0301 👩🏽‍💻")
        default = reflow(fixture, Config(max_cps=4))
        explicit = reflow(fixture, Config(max_cps=4, cps_mode="codepoint"))
        self.assertEqual(default, explicit)
        self.assertEqual(default["objective"][:2], [1, 3000])
        self.assert_rate(default["cues"][0], "codepoint", 7, 7)
        violation = default["constraints"][0]
        self.assertEqual(violation["cps"], 7)
        self.assertEqual(violation["max_cps"], 4)
        self.assertEqual(violation["rate_unit"], "codepoints/second")
        self.assertEqual(violation["cps_mode"], "codepoint")

    def test_grapheme_rate_is_independent_of_width_and_line_wrapping(self):
        for width_mode in ("codepoint", "display"):
            for max_chars in (4, 42):
                with self.subTest(width_mode=width_mode, max_chars=max_chars):
                    report = reflow(fallback("e\u0301 👩🏽‍💻"), Config(
                        max_chars=max_chars, width_mode=width_mode, cps_mode="grapheme", max_cps=3))
                    self.assertTrue(report["feasible"])
                    self.assertEqual(report["config"]["cps_mode"], "grapheme")
                    self.assert_rate(report["cues"][0], "grapheme", 3, 3)
                    self.assertEqual(report["cues"][0]["line_widths"],
                                     ([2, 4] if max_chars == 4 else [7]) if width_mode == "codepoint" else [4])
        report = reflow(fallback("你好世界"), Config(max_chars=2, cps_mode="grapheme"))
        self.assertEqual(report["cues"][0]["lines"], ["你好", "世界"])
        self.assert_rate(report["cues"][0], "grapheme", 4, 4)

    def test_known_clusters_and_spaces_have_fixed_counts(self):
        for text, codepoints, graphemes in (("e\u0301", 2, 1), ("👩🏽‍💻", 4, 1),
                ("🇨🇳", 2, 1), ("1️⃣", 3, 1), ("각", 3, 1), ("Ａ", 1, 1),
                ("👩🏽‍💻👩🏽‍💻", 8, 2), ("你好", 2, 2), ("  e\u0301\t\n 👩🏽‍💻  ", 7, 3)):
            for mode, count in (("codepoint", codepoints), ("grapheme", graphemes)):
                with self.subTest(text=text, mode=mode):
                    report = reflow(fallback(text, end=2), Config(cps_mode=mode))
                    self.assert_rate(report["cues"][0], mode, count, count / 2)
                    self.assertEqual(report["cues"][0]["original_text"], text)

    def test_mode_changes_dp_partition_and_fixed_reports_same_rate_policy(self):
        fixture = {"words": [{"word": "e\u0301.", "start": 0, "end": .4},
                             {"word": " e\u0301.", "start": 1.2, "end": 1.6}]}
        for mode, texts, counts, rates in (("codepoint", ["e\u0301. e\u0301."], [7], [4.375]),
                                           ("grapheme", ["e\u0301.", "e\u0301."], [2, 2], [5, 5])):
            config = Config(cps_mode=mode, max_cps=5, target_duration=.4, pause=2)
            report = reflow(fixture, config)
            self.assertEqual(report, reflow(fixture, config))
            self.assertEqual([c["text"] for c in report["cues"]], texts)
            self.assertTrue(report["feasible"])
            for cue, count, rate in zip(report["cues"], counts, rates):
                self.assert_rate(cue, mode, count, rate)
            fixed = reflow(fixture, config, algorithm="fixed", group_words=1)
            self.assertEqual(fixed["feasible"], mode == "grapheme")
            self.assertEqual([v["cps"] for v in fixed["constraints"]], [7.5, 7.5] if mode == "codepoint" else [])

    def test_infeasible_grapheme_rate_keeps_text_times_and_source_links(self):
        fixture = {"segments": [{"text": "e\u0301 👩🏽‍💻", "words": [
            {"word": "e\u0301", "start": 0, "end": .4},
            {"word": " 👩🏽‍💻", "start": .4, "end": 1}]}]}
        original = copy.deepcopy(fixture)
        report = reflow(fixture, Config(cps_mode="grapheme", max_cps=1))
        self.assertFalse(report["feasible"])
        self.assertEqual(fixture, original)
        cue = report["cues"][0]
        self.assertEqual((cue["start_ms"], cue["end_ms"], cue["original_text"]), (0, 1000, "e\u0301 👩🏽‍💻"))
        self.assert_rate(cue, "grapheme", 3, 3)
        violation, = report["constraints"]
        self.assertEqual(violation["code"], "speaking_rate")
        self.assertEqual((violation["cps"], violation["max_cps"]), (3, 1))
        self.assertEqual(report["objective"][:2], [1, 2000])
        self.assertEqual((violation["cps_mode"], violation["rate_unit"]), ("grapheme", "graphemes/second"))
        self.assertEqual(violation["unit_ids"], ["/segments/0/words/0", "/segments/0/words/1"])
        self.assertEqual(violation["cue_id"], cue["id"])
        self.assertEqual([u["source"]["text_span"] for u in report["source_units"]], [[0, 2], [2, 7]])
        self.assertTrue(report["metrics"]["tokens_conserved"])
        decoded = pysubs2.SSAFile.from_string(srt_text(report), format_="srt", keep_html_tags=True)
        self.assertEqual([html.unescape(s.text) for s in decoded], ["e\u0301 👩🏽‍💻"])

    def test_split_asr_cluster_is_counted_after_joining(self):
        fixture = {"words": [{"word": "👩🏽‍", "start": 0, "end": .5},
                             {"word": "💻", "start": .5, "end": 1}]}
        for algorithm in ("dp", "fixed"):
            report = reflow(fixture, Config(cps_mode="grapheme", max_cps=1), algorithm=algorithm, group_words=1)
            self.assertEqual(len(report["cues"]), 1)
            self.assertTrue(report["feasible"])
            self.assert_rate(report["cues"][0], "grapheme", 1, 1)
            self.assertEqual(report["metrics"]["mapped_words"], 2)
            self.assertTrue(any(d["code"] == "grapheme_boundary_join" for d in report["diagnostics"]))

    def test_profile_keeps_numeric_bounds_and_accepts_independent_mode(self):
        profile = dict(name="custom", max_cps=2, min_duration=0, min_gap=0)
        for mode, cps, unit in (("codepoint", 7, "codepoints/second"), ("grapheme", 3, "graphemes/second")):
            report = reflow(fallback("e\u0301 👩🏽‍💻"), Config.from_profile(profile, cps_mode=mode))
            self.assertEqual(report["config"]["profile_name"], "custom")
            self.assertEqual(report["config"]["max_cps"], 2)
            violation, = report["constraints"]
            self.assertEqual((violation["cps"], violation["rate_unit"]), (cps, unit))
            self.assertIn("graphemes" if mode == "grapheme" else "code points", violation["reason"])
        self.assertEqual(Config.from_profile(dict(profile, cps_mode="grapheme")),
                         Config.from_profile(profile, cps_mode="grapheme"))

    def test_rate_uses_unrounded_comparison_at_threshold(self):
        report = reflow(fallback("e\u0301", end=3), Config(cps_mode="grapheme", max_cps=.333333))
        self.assertFalse(report["feasible"])
        self.assertEqual(report["constraints"][0]["cps"], .333333)
        self.assertEqual(report["constraints"][0]["cps_mode"], "grapheme")
        self.assertTrue(reflow(fallback("e\u0301", end=3), Config(cps_mode="grapheme", max_cps=1 / 3))["feasible"])

    def test_invalid_modes_fail_explicitly(self):
        for mode in ("", "display", "GRAPHEME", None, True, 1, [], {}):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(InputError, "cps_mode"):
                    Config(cps_mode=mode)


if __name__ == "__main__":
    unittest.main()
