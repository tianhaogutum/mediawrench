import json
from pathlib import Path
import runpy
import unittest

from mediawrench.reflow import reflow

ROOT = Path(__file__).resolve().parents[2]


class ExperimentTests(unittest.TestCase):
    def test_recorded_measurements_are_reproducible(self):
        benchmark = runpy.run_path(str(ROOT / "benchmarks/reflow/benchmark.py"))["benchmark"]
        expected = json.loads((ROOT / "benchmarks/reflow/benchmark-results.json").read_text())
        self.assertEqual(benchmark(), expected)

    def test_huge_unaligned_paragraph_retained_with_bounded_lines(self):
        text = "unaligned " * 10000
        report = reflow({"segments": [{"start": 0, "end": 100, "text": text}]})
        self.assertFalse(report["feasible"])
        self.assertEqual(len(report["cues"][0]["lines"]), 2)
        self.assertEqual(report["cues"][0]["original_text"], text)
        self.assertEqual(report["source_segments"][0]["text"], text)
