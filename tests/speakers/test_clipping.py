"""Explicit boundary policy: preserve all timing evidence while clipping activity."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.speakers import BridgeError, merge, parse_rttm, run_manifest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "speakers"
OPTIONS = dict(rttm_offset_seconds=-2, rttm_drift_ppm=1000000,
               recording_duration=4, rttm_out_of_range_policy="clip")


class ClippingTests(unittest.TestCase):
    def fixture(self):
        return (json.loads((EXAMPLES / "clipping-asr.json").read_text()),
                parse_rttm((EXAMPLES / "clipping.rttm").read_text()))

    def test_clipped_assignment_matches_manual_baseline_and_audits_every_row(self):
        asr, diar = self.fixture()
        originals = copy.deepcopy((asr, diar))
        result = merge(asr, diar, **OPTIONS)
        baseline = merge(asr, parse_rttm(
            "SPEAKER clipping 1 0 2 <NA> <NA> A <NA> <NA>\n"
            "SPEAKER clipping 1 2 2 <NA> <NA> B <NA> <NA>"))
        self.assertEqual([i["speaker"] for i in result["items"]],
                         ["A", "AMBIGUOUS", "B", "UNKNOWN"])
        self.assertFalse(result["items"][1]["simultaneous_speech"])
        for key in ("items", "turns", "source_asr"):
            self.assertEqual(result[key], baseline[key])
        self.assertEqual(result["unioned_intervals"],
                         {"A": [[0, 2]], "B": [[2, 4]], "Early": [], "Late": []})
        self.assertEqual(result["source_rttm"], list(diar.source_records))
        expected = [(-2, -1, 0, 0, "dropped"), (-1, 2, 0, 2, "clipped"),
                    (0, 1, 0, 1, "unchanged"), (2, 6, 2, 4, "clipped"),
                    (6, 8, 4, 4, "dropped")]
        self.assertEqual(len(result["corrected_rttm"]), 5)
        diagnostics = []
        for raw, row, endpoints in zip(diar.source_records, result["corrected_rttm"], expected):
            self.assertEqual(tuple(row[k] for k in ("unclipped_start", "unclipped_end",
                                                    "start", "end", "clipping_status")), endpoints)
            self.assertEqual(row["line"], raw["line"])
            for key in ("start", "end"):
                self.assertEqual(row[f"original_{key}"], raw[key])
                self.assertEqual(row[f"unclipped_{key}"], raw[key] * 2 - 2)
            self.assertEqual(len(row["diagnostics"]), 0 if endpoints[-1] == "unchanged" else 1)
            for diagnostic in row["diagnostics"]:
                for part in (f"RTTM line {raw['line']}", "original", "corrected", "effective", endpoints[-1]):
                    self.assertIn(part, diagnostic)
            diagnostics.extend(row["diagnostics"])
        self.assertEqual(result["warnings"], diagnostics)
        self.assertEqual(result["config"]["rttm_time_correction"]["out_of_range_policy"], "clip")
        self.assertEqual((asr, diar), originals)
        self.assertEqual(result, merge(asr, diar, **OPTIONS))

    def test_default_rejection_and_explicit_error_are_equivalent(self):
        asr, diar = self.fixture()
        for options in ({}, {"rttm_out_of_range_policy": "error"}):
            with self.subTest(options=options), self.assertRaisesRegex(
                    BridgeError, "RTTM line 2.*negative corrected time"):
                merge(asr, diar, rttm_offset_seconds=-2, **options)
        in_range = parse_rttm("SPEAKER clipping 1 0 4 <NA> <NA> A <NA> <NA>")
        strict = merge(asr, in_range, recording_duration=4)
        self.assertEqual(strict, merge(asr, in_range, recording_duration=4,
                                       rttm_out_of_range_policy="error"))
        clipped = merge(asr, in_range, recording_duration=4, rttm_out_of_range_policy="clip")
        for key in ("items", "turns", "corrected_rttm", "warnings", "unioned_intervals"):
            self.assertEqual(strict[key], clipped[key])
        self.assertEqual(strict["corrected_rttm"][0]["clipping_status"], "unchanged")
        # Upper bound rejection is still the strict default, independently of the lower bound.
        with self.assertRaisesRegex(BridgeError, "RTTM line 1.*outside recording range"):
            merge(asr, in_range, recording_duration=3)

    def test_entirely_outside_and_boundary_touching_rows_keep_audit_and_aliases(self):
        asr, _ = self.fixture()
        # Corrected spans end at zero, start at duration, or are entirely beyond it.
        diar = parse_rttm("\n".join(
            f"SPEAKER clipping 1 {start} {duration} <NA> <NA> {speaker} <NA> <NA>"
            for start, duration, speaker in ((0, 1, "A"), (3, 1, "B"), (4, 1, "C"))))
        result = merge(asr, diar, aliases={"A": "Early", "B": "Late"}, **OPTIONS)
        self.assertEqual(result["unioned_intervals"], {"A": [], "B": [], "C": []})
        self.assertEqual([(r["start"], r["end"]) for r in result["corrected_rttm"]],
                         [(0, 0), (4, 4), (4, 4)])
        self.assertTrue(all(r["clipping_status"] == "dropped" for r in result["corrected_rttm"]))
        self.assertEqual(len(result["items"]), 4)
        self.assertTrue(all(i["speaker"] == "UNKNOWN" and not i["evidence"] for i in result["items"]))
        with self.assertRaisesRegex(BridgeError, "unknown speaker"):
            merge(asr, diar, aliases={"absent": "Guest"}, **OPTIONS)

    def test_both_boundaries_clip_one_spanning_row_before_union(self):
        asr, _ = self.fixture()
        diar = parse_rttm("SPEAKER clipping 1 0 4 <NA> <NA> A <NA> <NA>")
        result = merge(asr, diar, **OPTIONS)
        self.assertEqual(result["unioned_intervals"], {"A": [[0, 4]]})
        self.assertEqual([i["coverage"] for i in result["items"]], [1, 1, 1, 0])
        self.assertEqual(result["corrected_rttm"][0]["clipping_status"], "clipped")

    def test_policy_requires_duration_and_rejects_invalid_values(self):
        asr, diar = self.fixture()
        for value in (None, True, 1, [], {}, "", "CLIP", "ignore"):
            with self.subTest(policy=value), self.assertRaisesRegex(BridgeError, "rttm_out_of_range_policy"):
                merge(asr, diar, **dict(OPTIONS, rttm_out_of_range_policy=value))
        for duration in (None, 0, -1, float("nan"), float("inf"), True, "4"):
            with self.subTest(duration=duration), self.assertRaisesRegex(BridgeError, "recording_duration"):
                merge(asr, diar, **dict(OPTIONS, recording_duration=duration))

    def test_clipping_does_not_repair_invalid_correction_raw_rttm_or_asr(self):
        asr, diar = self.fixture()
        for changes, diagnostic in (({"rttm_drift_ppm": -1000000}, "reversed or collapsed"),
                                    ({"rttm_offset_seconds": 1e20}, "RTTM line 2.*collapsed"),
                                    ({"rttm_offset_seconds": float("inf")}, "rttm_offset_seconds")):
            with self.subTest(changes=changes), self.assertRaisesRegex(BridgeError, diagnostic):
                merge(asr, diar, **dict(OPTIONS, **changes))
        huge = parse_rttm("SPEAKER clipping 1 1e10 1e10 <NA> <NA> A <NA> <NA>")
        with self.assertRaisesRegex(BridgeError, "nonfinite corrected time"):
            merge(asr, huge, **dict(OPTIONS, rttm_drift_ppm=1e308))
        for bad_asr in ({"words": [{"word": "outside", "start": 3, "end": 4.1}]},
                        {"segments": [{"text": "coarse", "start": 0, "end": 5}]},
                        {"words": [{"word": "negative", "start": -1, "end": 1}]}):
            with self.subTest(asr=bad_asr), self.assertRaises(BridgeError):
                merge(bad_asr, diar, **OPTIONS)
        mixed = (EXAMPLES / "clipping.rttm").read_text()
        with self.assertRaisesRegex(BridgeError, "RTTM line 7"):
            merge(asr, parse_rttm(mixed + "SPEAKER other 2 -1 1 <NA> <NA> C <NA> <NA>\n",
                                 "clipping", "1"), **OPTIONS)
        # Correction applies only to selected rows, even if excluded rows would be negative.
        selected = parse_rttm(mixed + "SPEAKER other 2 0 1 <NA> <NA> C <NA> <NA>\n",
                              "clipping", "1")
        self.assertEqual(len(merge(asr, selected, **OPTIONS)["corrected_rttm"]), 5)

    def cli(self, *args):
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        return subprocess.run([sys.executable, "-m", "mediawrench.speakers", *map(str, args)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_cli_clip_and_dry_run_emit_row_diagnostics(self):
        args = ["--asr", EXAMPLES / "clipping-asr.json", "--rttm", EXAMPLES / "clipping.rttm",
                "--rttm-offset-seconds", "-2", "--rttm-drift-ppm", "1000000",
                "--recording-duration", "4", "--rttm-out-of-range-policy", "clip"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "clipped.json"
            proc = self.cli(*args, "--output", output)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            expected = merge(*self.fixture(), **OPTIONS)
            self.assertEqual(json.loads(output.read_text()), expected)
            self.assertEqual(proc.stderr, "".join(f"warning: {w}\n" for w in expected["warnings"]))
            for format_ in ("json", "markdown", "srt"):
                dry = self.cli(*args, "--format", format_, "--dry-run", "--output", output)
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertIn("4 items", dry.stdout)
                self.assertEqual(dry.stderr, proc.stderr)
                self.assertEqual(json.loads(output.read_text()), expected)

    def test_cli_failures_preserve_output_including_dry_run(self):
        args = ["--asr", EXAMPLES / "clipping-asr.json", "--rttm", EXAMPLES / "clipping.rttm"]
        failures = [(["--rttm-offset-seconds", "-2"], "RTTM line 2"),
                    (["--rttm-out-of-range-policy", "clip"], "recording_duration"),
                    (["--rttm-out-of-range-policy", "typo"], "invalid choice"),
                    (["--recording-duration", "3", "--rttm-out-of-range-policy", "clip"], "ASR item 2")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "keep.json"
            output.write_text("sentinel")
            for options, diagnostic in failures:
                for dry in ([], ["--dry-run"]):
                    with self.subTest(options=options, dry=dry):
                        proc = self.cli(*args, *options, *dry, "--output", output)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertIn(diagnostic, proc.stderr)
                        self.assertNotIn("Traceback", proc.stderr)
                        self.assertEqual(proc.stdout, "")
                        self.assertEqual(output.read_text(), "sentinel")

    def test_manifest_clipping_policy_is_local_and_diagnostics_reach_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            entries = [dict(recording="clipping", channel=1, asr=str(EXAMPLES / "clipping-asr.json"),
                            rttm=str(EXAMPLES / "clipping.rttm"), options=OPTIONS),
                       dict(recording="room-b", channel=1, asr=str(EXAMPLES / "batch/room-b.json"),
                            rttm=str(EXAMPLES / "batch/rooms.rttm"))]
            path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
            output = Path(directory) / "out"
            summary = run_manifest(path, output)
            self.assertEqual(summary["status"], "complete")
            first, second = [json.loads((output / e["result"]).read_text()) for e in summary["entries"]]
            self.assertEqual(first, merge(*self.fixture(), **OPTIONS))
            self.assertEqual(summary["entries"][0]["warnings"], first["warnings"])
            self.assertEqual(second["config"]["rttm_time_correction"]["out_of_range_policy"], "error")
            self.assertEqual(second["warnings"], [])
            entries[0]["options"] = dict(OPTIONS, rttm_out_of_range_policy="error")
            path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
            partial = run_manifest(path, Path(directory) / "partial")
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["entries"][0]["error"]["stage"], "merge")
            self.assertIn("RTTM line 2", partial["entries"][0]["error"]["message"])
            # Single-recording policy cannot be mixed into manifest CLI mode.
            proc = self.cli("--manifest", path, "--output-dir", Path(directory) / "conflict",
                            "--rttm-out-of-range-policy", "clip")
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("cannot be combined", proc.stderr)


if __name__ == "__main__":
    unittest.main()
