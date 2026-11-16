"""Local synthetic profile integrations, with a separately calculated path oracle."""
import copy
import itertools
from pathlib import Path
import unittest

from mediawrench.reflow import Config, InputError, reflow
from mediawrench.reflow.io import load_json, srt_text

ROOT = Path(__file__).resolve().parents[2]


def profile(name="test", max_cps=20, min_duration=0, min_gap=0):
    return dict(name=name, max_cps=max_cps, min_duration=min_duration, min_gap=min_gap)


class ProfileTests(unittest.TestCase):
    def test_two_named_profiles_have_exact_different_deterministic_partitions(self):
        fixture = load_json(ROOT / "examples/reflow/reading-profile.json")
        original = copy.deepcopy(fixture)
        for name, texts, times in (
                ("brief", ["One. Two.", "Three."], [(0, 800), (1000, 1400)]),
                ("patient", ["One. Two. Three."], [(0, 1400)])):
            config = Config.from_profile(load_json(ROOT / f"examples/reflow/profiles/{name}.json"),
                                         target_duration=0.5, max_duration=3)
            report = reflow(fixture, config)
            self.assertEqual(report, reflow(fixture, config))
            self.assertEqual(report["config"]["profile_name"], name)
            self.assertEqual([c["text"] for c in report["cues"]], texts)
            self.assertEqual([(c["start_ms"], c["end_ms"]) for c in report["cues"]], times)
            self.assertTrue(report["feasible"])
            self.assertTrue(report["metrics"]["tokens_conserved"])
            self.assertEqual("".join(c["original_text"] for c in report["cues"]),
                             "".join(w["word"] for w in fixture["words"]))
            self.assertEqual([(w["start"], w["end"]) for w in report["normalized_words"]],
                             [(w["start"], w["end"]) for w in fixture["words"]])
            srt_text(report)
        self.assertEqual(fixture, original)

    def test_dense_paragraphs_explain_each_conflict_without_retiming(self):
        fixture = load_json(ROOT / "examples/reflow/profile-conflict.json")
        config = Config.from_profile(profile("dense", max_cps=5, min_duration=1, min_gap=.12))
        report = reflow(fixture, config)
        self.assertFalse(report["feasible"])
        self.assertEqual([c["text"] for c in report["cues"]], ["One.", "Two."])
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in report["cues"]], [(0, 400), (400, 800)])
        self.assertEqual([v["code"] for v in report["cues"][0]["violations"]],
                         ["speaking_rate", "minimum_duration", "minimum_gap"])
        self.assertEqual([v["code"] for v in report["cues"][1]["violations"]],
                         ["speaking_rate", "minimum_duration"])
        gap = next(v for v in report["constraints"] if v["code"] == "minimum_gap")
        self.assertEqual((gap["gap_ms"], gap["min_ms"], gap["shortfall_ms"]), (0, 120, 120))
        self.assertEqual(gap["next_unit_id"], "/segments/1/words/0")
        self.assertEqual(gap["unit_ids"], ["/segments/0/words/0"])
        for v in report["constraints"]:
            self.assertTrue(v["reason"])
        self.assertEqual([c["timing"]["duration_delta_ms"] for c in report["cues"]], [0, 0])
        srt_text(report)

    def test_gap_alone_changes_partition_and_fixed_baseline_reports_it(self):
        fixture = {"words": [{"word": "A.", "start": 0, "end": .4},
                             {"word": " B.", "start": .4, "end": .8}]}
        config = Config.from_profile(profile(min_gap=.1), target_duration=.4)
        dp = reflow(fixture, config)
        self.assertEqual([c["text"] for c in dp["cues"]], ["A. B."])
        fixed = reflow(fixture, config, algorithm="fixed", group_words=1)
        self.assertEqual([v["code"] for v in fixed["constraints"]], ["minimum_gap"])
        self.assertEqual(fixed["constraints"][0]["shortfall_ms"], 100)

    def test_minimum_duration_alone_changes_partition(self):
        fixture = {"words": [{"word": "A.", "start": 0, "end": .4},
                             {"word": " B.", "start": .8, "end": 1.2}]}
        layout = dict(target_duration=.4, pause=2)
        loose = reflow(fixture, Config.from_profile(profile(), **layout))
        bound = reflow(fixture, Config.from_profile(profile(min_duration=1), **layout))
        self.assertEqual([c["text"] for c in loose["cues"]], ["A.", "B."])
        self.assertEqual([c["text"] for c in bound["cues"]], ["A. B."])
        self.assertTrue(bound["feasible"])

    def test_fallback_and_contradictory_duration_bounds_remain_explicit(self):
        config = Config.from_profile(profile(min_duration=2), target_duration=.5, max_duration=1)
        for fixture in ({"segments": [{"text": "Keep all.", "start": 0, "end": 1.5}]},
                        {"words": [{"word": "Keep all.", "start": 0, "end": 1.5}]}):
            report = reflow(fixture, config)
            self.assertEqual({v["code"] for v in report["constraints"]}, {"duration", "minimum_duration"})
            self.assertEqual(report["cues"][0]["original_text"], "Keep all.")
            self.assertEqual(report["cues"][0]["end_ms"], 1500)

    def test_repairs_have_originals_and_explicit_deviations(self):
        fixture = {"words": [{"word": "A", "start": 0, "end": 1},
                             {"word": " B", "start": .5, "end": .9}]}
        report = reflow(fixture, Config.from_profile(profile(min_duration=1)))
        self.assertEqual(report["normalized_words"][1]["start"], .5)
        repair = next(d for d in report["diagnostics"] if d["code"] == "timing_repair")
        self.assertEqual(repair["source_id"], "/words/1")
        self.assertEqual(repair["from_ms"], [500, 900])
        self.assertEqual(repair["to_ms"], [1000, 1001])
        self.assertEqual(repair["delta_ms"], [500, 101])
        self.assertEqual(repair["duration_delta_ms"], -399)
        self.assertTrue(all(c["timing"]["duration_delta_ms"] == 0 for c in report["cues"]))

    def test_profile_validation_and_no_silent_overrides(self):
        invalid = [None, [], {}, dict(profile(), extra=2), dict(profile(), name=" "),
                   dict(profile(), name=None), dict(profile(), name=12), dict(profile(), name="bad\nname")]
        for key in ("max_cps", "min_duration", "min_gap"):
            for value in (True, -1, "1", float("nan"), float("inf"), 10**400):
                invalid.append(dict(profile(), **{key: value}))
        invalid += [profile(max_cps=0), profile(min_duration=.0001), profile(min_gap=.0001),
                    profile(min_duration=3601), profile(min_gap=3601)]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(InputError):
                    Config.from_profile(item)
        for key, value in (("max_cps", 30), ("min_duration", .5), ("min_gap", .5), ("profile_name", "other")):
            with self.assertRaisesRegex(InputError, "override"):
                Config.from_profile(profile(), **{key: value})

    def test_exact_thresholds_final_cue_and_grapheme_group(self):
        config = Config.from_profile(profile(max_cps=2, min_duration=1, min_gap=.12))
        fixture = {"segments": [{"text": "A.", "start": 0, "end": 1},
                                 {"text": "B.", "start": 1.12, "end": 2.12}]}
        report = reflow(fixture, config)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["constraints"], [])
        # The 1ms unit times still form one indivisible grapheme, so the whole
        # group carries a duration deficit instead of cutting off the accent.
        fixture = {"words": [{"word": "e", "start": 0, "end": .001},
                             {"word": "\u0301", "start": .001, "end": .002}]}
        report = reflow(fixture, config)
        self.assertEqual([c["text"] for c in report["cues"]], ["e\u0301"])
        self.assertEqual({v["code"] for v in report["constraints"]}, {"speaking_rate", "minimum_duration"})
        self.assertEqual(report["metrics"]["mapped_words"], 2)
        srt_text(report)

    def test_profile_dp_matches_independent_exhaustive_oracle(self):
        # All 8 partitions; independently score short ASCII candidates. No package
        # candidate/scoring/layout helpers are used. Width/duration never prune here.
        words = ["A.", " B.", " C.", " D."]
        starts, ends = [0, 400, 1100, 1700], [400, 900, 1500, 2100]
        fixture = {"words": [dict(word=w, start=s / 1000, end=e / 1000)
                             for w, s, e in zip(words, starts, ends)]}
        for cps_limit, min_ms, gap_ms in itertools.product((3, 20), (0, 800, 1400), (0, 250)):
            scores = []
            for mask in itertools.product((False, True), repeat=3):
                cuts = [0] + [i + 1 for i, cut in enumerate(mask) if cut] + [4]
                score = [0, 0, 0]
                for a, b in zip(cuts, cuts[1:]):
                    duration = ends[b - 1] - starts[a]
                    cps = len("".join(words[a:b]).strip()) * 1000 / duration
                    short = max(0, min_ms - duration)
                    gap_short = max(0, gap_ms - (starts[b] - ends[b - 1])) if b < 4 else 0
                    score[0] += (cps > cps_limit) + (short > 0) + (gap_short > 0)
                    score[1] += max(0, round((cps - cps_limit) * 1000)) + short + gap_short
                    internal = sum(3 * (starts[k] - ends[k - 1]) for k in range(a + 1, b)
                                   if starts[k] - ends[k - 1] >= 500)
                    boundary = max(0, 400 - min(starts[b] - ends[b - 1], 1000)) if b < 4 else 0
                    score[2] += 400 + (duration - 500) ** 2 * 1000 // 500 ** 2 + internal + boundary
                scores.append(score)
            config = Config.from_profile(profile(max_cps=cps_limit, min_duration=min_ms / 1000,
                                                  min_gap=gap_ms / 1000), target_duration=.5)
            self.assertEqual(reflow(fixture, config)["objective"], min(scores))


if __name__ == "__main__":
    unittest.main()
