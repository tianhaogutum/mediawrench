import importlib.util
from pathlib import Path
import unittest

from mediawrench.asraudit import audit

# The seeded evaluation script lives outside the package, in benchmarks/asr-audit/run.py.
_RUN = Path(__file__).resolve().parents[2] / "benchmarks" / "asr-audit" / "run.py"
_spec = importlib.util.spec_from_file_location("mediawrench_asr_audit_benchmark", _RUN)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
dataset, evaluate = _module.dataset, _module.evaluate


class ExperimentTests(unittest.TestCase):
    def test_seeded_counts_and_explicit_false_positives(self):
        result = evaluate()
        self.assertEqual(result["segments"], 84)
        self.assertEqual(result["positive_segments"], 32)
        self.assertEqual(result["negative_segments"], 52)
        baseline = result["baseline_repetition_only"]
        joined = result["repeat_and_outside_speech"]
        self.assertEqual([baseline[k] for k in ("true_positives", "false_positives", "false_negatives", "true_negatives")], [32, 28, 0, 24])
        self.assertEqual([joined[k] for k in ("true_positives", "false_positives", "false_negatives", "true_negatives")], [24, 4, 8, 48])
        self.assertEqual(joined["recall"], .75)
        self.assertEqual(joined["precision"], 24 / 28)
        self.assertEqual(result, evaluate())

    def test_missing_vad_has_no_corroboration_or_silence_evidence(self):
        data = dataset()
        report = audit(data["transcript"])
        self.assertEqual(report["speech_corroborated_repeats"], [])
        self.assertTrue(all(flag["outside_speech_fraction"] is None for flag in report["flags"]))
        self.assertTrue(all("outside_speech" not in flag["reasons"] for flag in report["flags"]))

    def test_false_positives_and_false_negatives_match_counterexamples(self):
        result = evaluate()
        labels = {item["segment"]: item for item in dataset()["labels"]}
        for index in result["repeat_and_outside_speech"]["false_positive_segments"]:
            self.assertEqual(labels[index]["category"], "intentional_missing_vad_coverage")
        for index in result["repeat_and_outside_speech"]["false_negative_segments"]:
            self.assertEqual(labels[index]["category"], "suspicious_inside")


if __name__ == "__main__":
    unittest.main()
