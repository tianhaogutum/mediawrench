"""Synthetic clock fixtures: explicit user parameters, never inferred drift."""

import copy
import math
from pathlib import Path
import unittest

from mediawrench.speakers import BridgeError, merge, parse_rttm
from mediawrench.speakers.core import load_json

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "speakers"


class ClockCorrectionTests(unittest.TestCase):
    def fixture(self):
        asr = load_json((EXAMPLES / "clock-asr.json").read_text())
        shifted = parse_rttm((EXAMPLES / "clock-shifted.rttm").read_text())
        baseline = parse_rttm((EXAMPLES / "clock-baseline.rttm").read_text())
        return asr, shifted, baseline

    def test_known_offset_and_drift_restore_baseline_and_audit(self):
        asr, shifted, baseline = self.fixture()
        original = copy.deepcopy(shifted)
        expected = merge(asr, baseline)
        self.assertEqual([i["speaker"] for i in expected["items"]],
                         ["A", "AMBIGUOUS", "B", "UNKNOWN"])
        self.assertNotEqual(merge(asr, shifted)["items"], expected["items"])
        actual = merge(asr, shifted, rttm_offset_seconds=-2, rttm_drift_ppm=250000,
                       recording_duration=6)
        for key in ("items", "turns", "unioned_intervals", "source_asr"):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(shifted, original)
        self.assertEqual(actual["source_rttm"], list(shifted.source_records))
        correction = actual["config"]["rttm_time_correction"]
        self.assertEqual(correction["scale"], 1.25)
        self.assertEqual(correction["offset_seconds"], -2)
        self.assertEqual(correction["drift_ppm"], 250000)
        self.assertEqual(correction["recording_duration"], 6)
        self.assertIn("original * (1 + drift_ppm / 1000000) + offset_seconds", correction["formula"])
        for raw, corrected in zip(actual["source_rttm"], actual["corrected_rttm"]):
            self.assertEqual(corrected["line"], raw["line"])
            for boundary in ("start", "end"):
                self.assertEqual(corrected[f"original_{boundary}"], raw[boundary])
                self.assertEqual(corrected[boundary], raw[boundary] * correction["scale"] - 2)

    def test_realistic_ppm_both_signs_and_offset_only(self):
        asr, _, baseline = self.fixture()
        expected = merge(asr, baseline)
        for offset, ppm in ((-2, 1000), (0.25, -1000), (-2, 0), (0, 1000)):
            with self.subTest(offset=offset, ppm=ppm):
                scale = 1 + ppm / 1000000
                text = "\n".join(
                    f"SPEAKER clock 1 {(r['start'] - offset) / scale!r} "
                    f"{(r['end'] - r['start']) / scale!r} <NA> <NA> {r['speaker']} <NA> <NA>"
                    for r in baseline.source_records)
                actual = merge(asr, parse_rttm(text), rttm_offset_seconds=offset, rttm_drift_ppm=ppm)
                self.assertEqual([i["speaker"] for i in actual["items"]],
                                 [i["speaker"] for i in expected["items"]])
                for a, b in zip(actual["items"], expected["items"]):
                    self.assertAlmostEqual(a["coverage"], b["coverage"])

    def test_zero_correction_preserves_existing_results(self):
        asr, _, baseline = self.fixture()
        default = merge(asr, baseline)
        explicit = merge(asr, baseline, rttm_offset_seconds=0, rttm_drift_ppm=0)
        self.assertEqual(default, explicit)
        self.assertEqual(default["source_rttm"], list(baseline.source_records))
        for raw, corrected in zip(default["source_rttm"], default["corrected_rttm"]):
            self.assertEqual((raw["start"], raw["end"]), (corrected["start"], corrected["end"]))

    def test_correct_selected_rows_before_union_with_line_provenance(self):
        diar = parse_rttm(
            "# comment\nSPEAKER other 1 0 1 <NA> <NA> C <NA> <NA>\n"
            "SPEAKER clock 1 2 2 <NA> <NA> A <NA> <NA>\n"
            "SPEAKER clock 1 2 1 <NA> <NA> A <NA> <NA>\n"
            "SPEAKER clock 2 0 1 <NA> <NA> D <NA> <NA>\n"
            "SPEAKER clock 1 4 2 <NA> <NA> A <NA> <NA>", "clock", "1")
        asr = {"words": [{"word": "yes", "start": 1, "end": 5}]}
        result = merge(asr, diar, rttm_offset_seconds=-2, rttm_drift_ppm=250000)
        self.assertEqual(result["unioned_intervals"], {"A": [[0.5, 5.5]]})
        self.assertEqual([r["line"] for r in result["corrected_rttm"]], [3, 4, 6])
        self.assertEqual(result["items"][0]["coverage"], 1)
        # Reusing a parsed diarization must not apply correction twice.
        self.assertEqual(result, merge(asr, diar, rttm_offset_seconds=-2, rttm_drift_ppm=250000))

    def test_invalid_config(self):
        asr, diar, _ = self.fixture()
        for key in ("rttm_offset_seconds", "rttm_drift_ppm", "recording_duration"):
            for value in (math.nan, math.inf, -math.inf, True, "1", 10**400):
                with self.subTest(key=key, value=value), self.assertRaises(BridgeError):
                    merge(asr, diar, **{key: value})
        for ppm in (-1000000, -1000001):
            with self.subTest(ppm=ppm), self.assertRaisesRegex(BridgeError, "reversed or collapsed"):
                merge(asr, diar, rttm_drift_ppm=ppm)
        for duration in (0, -1):
            with self.subTest(duration=duration), self.assertRaisesRegex(BridgeError, "recording_duration"):
                merge(asr, diar, recording_duration=duration)

    def test_negative_overflow_and_collapsed_intervals_are_diagnosed(self):
        asr, diar, _ = self.fixture()
        for kwargs, diagnostic in (
            ({"rttm_offset_seconds": -3}, "RTTM line 2.*negative corrected time"),
            ({"rttm_offset_seconds": 1e20}, "RTTM line 2.*reversed or collapsed"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(BridgeError, diagnostic):
                merge(asr, diar, **kwargs)
        huge = parse_rttm("SPEAKER clock 1 1e10 1e10 <NA> <NA> A <NA> <NA>")
        with self.assertRaisesRegex(BridgeError, "RTTM line 1.*nonfinite corrected time"):
            merge(asr, huge, rttm_drift_ppm=1e308)

    def test_recording_range_checks_both_clocks_including_coarse_items(self):
        asr, shifted, _ = self.fixture()
        kwargs = dict(rttm_offset_seconds=-2, rttm_drift_ppm=250000)
        with self.assertRaisesRegex(BridgeError, "RTTM line 3.*outside recording range"):
            merge(asr, shifted, recording_duration=5, **kwargs)
        # RTTM ends at 5.5; last ASR item ends at 6. Never drop that word.
        with self.assertRaisesRegex(BridgeError, "ASR item 3.*outside recording range"):
            merge(asr, shifted, recording_duration=5.5, **kwargs)
        coarse = {"segments": [{"start": 5, "end": 6.1, "text": "coarse"}]}
        with self.assertRaisesRegex(BridgeError, "ASR item 0.*outside recording range"):
            merge(coarse, shifted, recording_duration=6, **kwargs)
        self.assertEqual(len(merge(asr, shifted, recording_duration=6, **kwargs)["items"]), 4)

    def test_exact_recording_boundaries_are_valid(self):
        _, shifted, _ = self.fixture()
        asr = {"words": [{"word": "first", "start": 0, "end": 1},
                         {"word": "last", "start": 4, "end": 5},
                         {"word": "point", "start": 5, "end": 5}]}
        result = merge(asr, shifted, rttm_offset_seconds=-2.5, rttm_drift_ppm=250000,
                       recording_duration=5)
        self.assertEqual(result["unioned_intervals"], {"A": [[0, 2.5]], "B": [[2.5, 5]]})
        self.assertEqual([i["speaker"] for i in result["items"]], ["A", "B", "UNKNOWN"])
        self.assertEqual(result["items"][-1]["reason"], "zero_duration")


if __name__ == "__main__":
    unittest.main()
