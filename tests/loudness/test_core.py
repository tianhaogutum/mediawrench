"""Unit checks for defensive parsing and deterministic failure/race injection.

Mocked failures here are supplementary; real FFmpeg E2E lives in test_e2e.py.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.loudness.core import (AudioError, Profile, Runner, Toolchain,
                                 finite_measurement, gate, normalize, parse_loudnorm)


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parents[2] / ".tools/test-tmp"
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def test_profile_invalid_and_nonfinite(self):
        for args in [(-4, -1.5, 0.5), (-16, -10, 0.5), (-16, -1.5, 0),
                     (-16, -1.5, -0.1), (-16, -1.5, 2.1),
                     (float("nan"), -1.5, 0.5), (-16, float("inf"), 0.5),
                     (-16, -1.5, float("inf")), (-16, -1.5, 0.5, 0)]:
            with self.subTest(args=args), self.assertRaises(AudioError):
                Profile(*args).validate()

    def test_timeout_validation(self):
        for value in (0, -1, float("nan"), float("inf"), 3601):
            with self.subTest(value=value), self.assertRaises(AudioError):
                Runner(Toolchain(timeout=value))
        for value in (0, -1, float("nan"), float("inf"), 86401):
            with self.subTest(total_timeout=value), self.assertRaises(AudioError):
                Runner(Toolchain(total_timeout=value))

    def test_parser_handles_log_prefix_and_rejects_missing_or_duplicate(self):
        raw = {"input_i": "-16.00", "input_tp": "-1.5", "input_lra": "2.0",
               "input_thresh": "-26.00", "target_offset": "0.00"}
        line = "[Parsed_loudnorm @ abc]\n" + json.dumps(raw)
        self.assertEqual(parse_loudnorm("log {unrelated}\n" + line), raw)
        for text in ("", "{invalid}", line + line):
            with self.subTest(text=text), self.assertRaises(AudioError):
                parse_loudnorm(text)

    def test_nonfinite_and_missing_measurements_never_invent_lufs(self):
        base = {"input_i": "-16", "input_tp": "-2", "input_lra": "1",
                "input_thresh": "-26", "target_offset": "0"}
        for key in base:
            for value in ("-inf", "inf", "nan", None, "nonsense"):
                with self.subTest(key=key, value=value), self.assertRaises(AudioError):
                    finite_measurement({**base, key: value})
        with self.assertRaises(AudioError):
            finite_measurement({})

    def test_peak_is_hard_ceiling_and_lufs_is_two_sided(self):
        base = {"input_i": "-16", "input_tp": "-1.49", "input_lra": "1",
                "input_thresh": "-26", "target_offset": "0"}
        profile = Profile(-16, -1.5, 0.5)
        self.assertFalse(gate(base, profile)["peak_pass"])
        self.assertTrue(gate({**base, "input_tp": "-1.50"}, profile)["peak_pass"])
        for value in ("-16.51", "-15.49"):
            self.assertFalse(gate({**base, "input_i": value}, profile)["lufs_pass"])

    def test_invalid_profile_writes_strict_json_without_running_tools(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as work:
            root = Path(work)
            source = root / "input.wav"
            source.write_bytes(b"untouched")
            with patch.object(Runner, "run", side_effect=AssertionError("must not run")):
                result = normalize(source, root / "out.wav", Profile(float("nan"), -1.5, 0.5),
                                   root / "report.json")
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(json.loads((root / "report.json").read_text())["profile"]["target_i"], "nan")
            self.assertFalse((root / "out.wav").exists())
            self.assertEqual(source.read_bytes(), b"untouched")

    def test_invalid_stream_index_fails_before_running_tools(self):
        for index in (-1, True, "1", 1.0, float("nan"), [1, 2]):
            with self.subTest(index=index), tempfile.TemporaryDirectory(dir=self.temp_root) as work:
                root = Path(work)
                source = root / "input.mkv"
                source.write_bytes(b"untouched")
                with patch.object(Runner, "run", side_effect=AssertionError("must not run")):
                    result = normalize(source, root / "out.wav", Profile(-16, -1.5, 0.5),
                                       root / "report.json", audio_stream=index)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn("nonnegative integer", result["error"])
                self.assertEqual(json.loads((root / "report.json").read_text()), result)
                self.assertFalse((root / "out.wav").exists())


if __name__ == "__main__":
    unittest.main()
