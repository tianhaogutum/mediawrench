"""Offline multi-recording integration: real files, isolated identity and failure."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, merge, parse_rttm

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "examples" / "speakers" / "batch"


class ManifestTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        for path in FIXTURES.iterdir():
            (root / path.name).write_bytes(path.read_bytes())
        return root / "manifest.json", json.loads((root / "manifest.json").read_text())

    def save(self, path, manifest):
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def run_manifest(self, *args, **kwargs):
        from mediawrench.speakers import run_manifest
        return run_manifest(*args, **kwargs)

    def cli(self, *args):
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        return subprocess.run([sys.executable, "-m", "mediawrench.speakers", *map(str, args)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_same_times_are_isolated_by_both_recording_and_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = self.fixture(directory)
            manifest["entries"].append(dict(recording="room-a", channel=2,
                                            asr="room-a.json", rttm="rooms.rttm"))
            self.save(path, manifest)
            output = Path(directory) / "out"
            summary = self.run_manifest(path, output)
            self.assertEqual(summary["status"], "complete")
            self.assertFalse(summary["partial_results"])
            self.assertEqual(summary["counts"], dict(total=3, succeeded=3, failed=0, items=3, turns=3))
            self.assertEqual(summary, json.loads((output / "summary.json").read_text()))
            for entry, speaker in zip(summary["entries"], ("Alice", "Bob", "Carol")):
                result = json.loads((output / entry["result"]).read_text())
                self.assertEqual(result["items"][0]["speaker"], speaker)
                self.assertEqual(set(result["unioned_intervals"]), {speaker})
                self.assertEqual({r["recording"] for r in result["source_rttm"]}, {entry["recording"]})
                self.assertEqual({r["channel"] for r in result["source_rttm"]}, {entry["channel"]})
                self.assertEqual([e["speaker"] for e in result["items"][0]["evidence"]], [speaker])
                # Batch uses the existing merge unchanged, with local IDs starting at zero.
                expected = merge(json.loads((path.parent / entry["asr"]).read_text()),
                                 parse_rttm((path.parent / entry["rttm"]).read_text(),
                                            entry["recording"], entry["channel"]))
                self.assertEqual(result, expected)
            self.assertEqual(len(list(output.iterdir())), 4)

    def test_missing_or_duplicate_identity_rejects_entire_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path, original = self.fixture(directory)
            invalid = []
            for key in ("recording", "channel"):
                missing = copy.deepcopy(original)
                del missing["entries"][1][key]
                invalid.append((missing, f"entries\\[1\\].*{key}"))
            duplicate = copy.deepcopy(original)
            duplicate["entries"][1].update(recording="room-a", channel="01")
            invalid.append((duplicate, "entries\\[1\\].*duplicate.*entries\\[0\\]"))
            for value, diagnostic in invalid:
                with self.subTest(manifest=value):
                    self.save(path, value)
                    output = Path(directory) / "out"
                    with self.assertRaisesRegex(BridgeError, diagnostic):
                        self.run_manifest(path, output)
                    self.assertFalse(output.exists())

    def test_invalid_schema_identity_paths_and_option_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path, original = self.fixture(directory)
            invalid = [[], {}, dict(schema_version=True, entries=original["entries"]),
                       dict(schema_version=2, entries=original["entries"]),
                       dict(schema_version=1, entries=[]),
                       dict(schema_version=1, entries=[None]),
                       dict(original, typo=True)]
            for key, value in (("recording", ""), ("recording", "a b"), ("recording", None),
                               ("channel", True), ("channel", 0), ("channel", "00"),
                               ("channel", "١"), ("channel", 1.0), ("channel", "1 "),
                               ("asr", None), ("rttm", ""), ("aliases", 2),
                               ("options", []), ("options", {"recordng_duration": 2}),
                               ("extra", "typo")):
                bad = copy.deepcopy(original)
                bad["entries"][0][key] = value
                invalid.append(bad)
            for value in invalid:
                with self.subTest(manifest=value):
                    self.save(path, value)
                    output = Path(directory) / "out"
                    with self.assertRaises(BridgeError):
                        self.run_manifest(path, output)
                    self.assertFalse(output.exists())
            path.write_text('{"schema_version":1,"entries":[],"entries":[]}')
            with self.assertRaisesRegex(BridgeError, "duplicate JSON key"):
                self.run_manifest(path, Path(directory) / "out")
            original["entries"][0]["options"] = {"max_gap": 1}
            self.save(path, original)
            path.write_text(path.read_text().replace('"max_gap": 1', '"max_gap": 1e309'))
            with self.assertRaisesRegex(BridgeError, "entries\\[0\\].options.*nonfinite"):
                self.run_manifest(path, Path(directory) / "out")
            self.assertFalse((Path(directory) / "out").exists())

    def test_entry_failures_continue_with_context_and_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = self.fixture(directory)
            # First failure must not stop later valid entries.
            manifest["entries"][0]["asr"] = "missing.json"
            manifest["entries"].append(dict(recording="room-a", channel=2,
                                            asr="room-a.json", rttm="rooms.rttm"))
            self.save(path, manifest)
            output = Path(directory) / "out"
            proc = self.cli("--manifest", path, "--output-dir", output)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("entries[0]", proc.stderr)
            self.assertIn("room-a", proc.stderr)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "partial")
            self.assertTrue(summary["partial_results"])
            self.assertEqual(summary["counts"], dict(total=3, succeeded=2, failed=1, items=2, turns=2))
            failed = summary["entries"][0]
            self.assertEqual(failed["error"]["stage"], "asr")
            self.assertIn("missing.json", failed["error"]["message"])
            self.assertIsNone(failed["result"])
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ["entry-0001.json", "entry-0002.json", "summary.json"])

    def test_each_error_stage_is_local_and_no_identity_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path, original = self.fixture(directory)
            for stage, changes in (("asr", {"asr": "bad.json"}),
                                   ("rttm", {"recording": "absent"}),
                                   ("rttm", {"channel": "3"}),
                                   ("aliases", {"aliases": "bad.json"}),
                                   ("merge", {"options": {"min_coverage": 2}}),
                                   ("merge", {"options": {"rttm_offset_seconds": -1}})):
                with self.subTest(stage=stage, changes=changes):
                    manifest = copy.deepcopy(original)
                    manifest["entries"][0].update(changes)
                    self.save(path, manifest)
                    (path.parent / "bad.json").write_text("{bad")
                    summary = self.run_manifest(path, path.parent / f"out-{len(list(path.parent.iterdir()))}")
                    self.assertEqual(summary["entries"][0]["error"]["stage"], stage)
                    self.assertEqual(summary["entries"][1]["status"], "ok")
                    self.assertEqual(summary["status"], "partial")

    def test_per_entry_aliases_and_clock_options_never_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = self.fixture(directory)
            (path.parent / "labels.json").write_text('{"Alice":"Guest"}')
            manifest["entries"][0].update(aliases="labels.json",
                                           options=dict(rttm_offset_seconds=.5, recording_duration=2))
            self.save(path, manifest)
            output = path.parent / "out"
            summary = self.run_manifest(path, output)
            first, second = [json.loads((output / e["result"]).read_text()) for e in summary["entries"]]
            self.assertEqual(first["items"][0]["speaker_label"], "Guest")
            self.assertEqual(first["items"][0]["coverage"], .5)
            self.assertEqual(first["corrected_rttm"][0]["start"], .5)
            self.assertEqual(second["items"][0]["speaker_label"], "Bob")
            self.assertEqual(second["items"][0]["coverage"], 1)
            self.assertEqual(second["corrected_rttm"][0]["start"], 0)

    def test_nonfinite_asr_metadata_render_failure_is_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.fixture(directory)
            (path.parent / "room-a.json").write_text(
                '{"words":[{"word":"Hello","start":0,"end":1}],"metadata":1e309}')
            output = path.parent / "out"
            summary = self.run_manifest(path, output)
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["entries"][0]["error"]["stage"], "render")
            self.assertFalse((output / "entry-0000.json").exists())
            self.assertEqual(summary["entries"][1]["status"], "ok")

    def test_all_failed_summary_and_dry_run_do_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = self.fixture(directory)
            for entry in manifest["entries"]:
                entry["asr"] = "missing.json"
            self.save(path, manifest)
            output = path.parent / "out"
            dry = self.cli("--manifest", path, "--output-dir", output, "--dry-run")
            self.assertEqual(dry.returncode, 1, dry.stderr)
            self.assertIn("failed", dry.stdout)
            self.assertFalse(output.exists())
            summary = self.run_manifest(path, output)
            self.assertEqual(summary["status"], "failed")
            self.assertFalse(summary["partial_results"])
            self.assertEqual(summary["counts"]["succeeded"], 0)
            self.assertEqual(list(output.iterdir()), [output / "summary.json"])

    def test_cli_success_dry_run_existing_directory_and_mode_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.fixture(directory)
            output = path.parent / "out"
            args = ["--manifest", path, "--output-dir", output]
            dry = self.cli(*args, "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("no output written", dry.stdout)
            self.assertFalse(output.exists())
            for extra in (["--asr", path], ["--rttm", path], ["--recording", "room-a"],
                          ["--channel", "1"], ["--min-coverage", "0.5"],
                          ["--rttm-offset-seconds", "0"], ["--format", "srt"],
                          ["--output", path]):
                with self.subTest(extra=extra):
                    proc = self.cli(*args, *extra)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertFalse(output.exists())
            proc = self.cli(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            for extra in ([], ["--dry-run"]):
                proc = self.cli(*args, *extra)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("exist", proc.stderr)
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)
            proc = self.cli("--output-dir", path.parent / "other")
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertFalse((path.parent / "other").exists())

    def test_structural_cli_errors_and_output_symlinks_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = self.fixture(directory)
            original_asr = (path.parent / "room-a.json").read_bytes()
            del manifest["entries"][1]["channel"]
            self.save(path, manifest)
            output = path.parent / "out"
            for extra in ([], ["--dry-run"]):
                proc = self.cli("--manifest", path, "--output-dir", output, *extra)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("entries[1].channel", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(output.exists())
            # An existing directory or dangling symlink is never replaced.
            path, _ = self.fixture(directory)
            for target in (path.parent, path.parent / "missing-directory"):
                output.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(BridgeError, "already exists"):
                    self.run_manifest(path, output)
                self.assertTrue(output.is_symlink())
                output.unlink()
            self.assertEqual((path.parent / "room-a.json").read_bytes(), original_asr)

    def test_output_io_failure_removes_incomplete_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.fixture(directory)
            output = path.parent / "out"
            from mediawrench.speakers.export import write_atomic

            def fail_summary(destination, text):
                if destination.name == "summary.json":
                    raise OSError("simulated summary write failure")
                write_atomic(destination, text)

            with patch("mediawrench.speakers.manifest.write_atomic", side_effect=fail_summary):
                with self.assertRaisesRegex(OSError, "summary write failure"):
                    self.run_manifest(path, output)
            self.assertFalse(output.exists())
            self.assertEqual(self.run_manifest(path, output)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
