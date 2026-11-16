"""Per-entry SRT companions retain JSON evidence through presentation failures."""

import html
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pysubs2

from mediawrench.speakers import BridgeError, merge, parse_rttm, run_manifest
from mediawrench.speakers.export import write_atomic

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / "examples" / "speakers" / "batch"


class ManifestSrtTests(unittest.TestCase):
    def save(self, root, entries):
        path = root / "manifest.json"
        path.write_text(json.dumps(dict(schema_version=1, entries=entries)), encoding="utf-8")
        return path

    def entry(self, recording="room-a", channel=1, **extra):
        return dict(recording=recording, channel=channel, asr=str(BATCH / "room-a.json"),
                    rttm=str(BATCH / "rooms.rttm"), exports=["srt"], **extra)

    def cli(self, path, output, *extra):
        return subprocess.run([sys.executable, "-m", "mediawrench.speakers", "--manifest",
                               str(path), "--output-dir", str(output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def cues(self, path):
        return [(e.start, e.end, html.unescape(e.text)) for e in
                pysubs2.load(str(path), format_="srt", keep_html_tags=True)]

    def test_srt_failure_retains_json_and_later_clipped_and_ordinary_results(self):
        path = BATCH / "manifest-srt.json"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            summary = run_manifest(path, output)
            self.assertEqual(summary, json.loads((output / "summary.json").read_text()))
            self.assertEqual(summary["status"], "partial")
            self.assertTrue(summary["partial_results"])
            self.assertEqual(summary["counts"], dict(total=3, succeeded=2, failed=1, items=6, turns=6))
            late, clipped, ordinary = summary["entries"]
            self.assertEqual(late["status"], "partial")
            self.assertEqual(late["result"], "entry-0000.json")
            self.assertEqual(late["exports"]["srt"], dict(status="error", file=None))
            self.assertEqual(late["error"]["stage"], "export:srt")
            self.assertIn("entries[0] (after-limit/1) export:srt:", late["error"]["message"])
            self.assertIn("99:59:59.999", late["error"]["message"])
            self.assertEqual(sorted(p.name for p in output.iterdir()), [
                "entry-0000.json", "entry-0001.json", "entry-0001.srt",
                "entry-0002.json", "entry-0002.srt", "summary.json"])
            for entry in summary["entries"]:
                result = json.loads((output / entry["result"]).read_text())
                asr = json.loads(Path(entry["asr"]).read_text())
                expected = merge(asr, parse_rttm(Path(entry["rttm"]).read_text(),
                                                entry["recording"], entry["channel"]),
                                 **entry["options"])
                self.assertEqual(result, expected)
                self.assertEqual(result["source_asr"], asr)
            for entry in (clipped, ordinary):
                self.assertEqual(entry["status"], "ok")
                self.assertEqual(entry["exports"]["srt"], dict(
                    status="ok", file=f"entry-{entry['index']:04d}.srt"))
            self.assertEqual(self.cues(output / "entry-0001.srt"), [
                (0, 1000, "A: First"), (1000, 3000, "AMBIGUOUS: boundary"),
                (3000, 4000, "B: last"), (4000, 4001, "UNKNOWN: point")])
            self.assertEqual(self.cues(output / "entry-0002.srt"), [(0, 1000, "Bob: Goodbye")])
            result = json.loads((output / "entry-0001.json").read_text())
            self.assertEqual([r["clipping_status"] for r in result["corrected_rttm"]],
                             ["dropped", "clipped", "unchanged", "clipped", "dropped"])

    def test_success_local_aliases_channels_and_literal_srt_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aliases = root / "aliases.json"
            aliases.write_text(json.dumps({"Alice": "Guest"}))
            asr = root / "asr.json"
            asr.write_text(json.dumps(dict(words=[dict(word="Hi <b> {x} \\N", start=.125, end=.875)])))
            first = self.entry(aliases=str(aliases))
            first["asr"] = str(asr)
            path = self.save(root, [first, self.entry(channel="02"), self.entry("room-b")])
            output = root / "out"
            summary = run_manifest(path, output)
            self.assertEqual(summary["status"], "complete")
            self.assertFalse(summary["partial_results"])
            self.assertEqual(summary["counts"]["succeeded"], 3)
            self.assertEqual(self.cues(output / "entry-0000.srt"), [(125, 875, "Guest: Hi <b> {x} \\N")])
            self.assertEqual(self.cues(output / "entry-0001.srt"), [(0, 1000, "Carol: Hello")])
            self.assertEqual(self.cues(output / "entry-0002.srt"), [(0, 1000, "Bob: Hello")])
            self.assertEqual(summary["entries"][1]["channel"], "2")

    def test_omitted_and_empty_exports_keep_json_only_summary_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.entry()
            del entry["exports"]
            path = self.save(root, [entry])
            old = run_manifest(path, root / "omitted")
            entry["exports"] = []
            self.save(root, [entry])
            empty = run_manifest(path, root / "empty")
            self.assertEqual(old, empty)
            self.assertNotIn("exports", old["entries"][0])
            self.assertEqual(sorted(p.name for p in (root / "omitted").iterdir()),
                             ["entry-0000.json", "summary.json"])

    def test_invalid_export_requests_fail_preflight_before_any_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for invalid in (None, "srt", True, {}, [None], [True], [1], [[]], [{}],
                            ["SRT"], ["Markdown"], ["json"], ["../escape.srt"], ["srt", "srt"]):
                with self.subTest(exports=invalid):
                    bad = self.entry("room-b")
                    bad["exports"] = invalid
                    path = self.save(root, [self.entry(), bad])
                    with patch("mediawrench.speakers.manifest.merge") as merging:
                        with self.assertRaisesRegex(BridgeError, r"entries\[1\].exports"):
                            run_manifest(path, root / "out")
                        merging.assert_not_called()
                    for extra in ([], ["--dry-run"]):
                        proc = self.cli(path, root / "out", *extra)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertIn("entries[1].exports", proc.stderr)
                        self.assertNotIn("Traceback", proc.stderr)
                    self.assertFalse((root / "out").exists())

    def test_failed_prerequisites_skip_srt_without_losing_other_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.json"
            bad.write_text("{bad")
            metadata = root / "metadata.json"
            metadata.write_text('{"words":[{"word":"Hi","start":0,"end":1}],"metadata":1e309}')
            for stage, changes in (("asr", dict(asr=str(bad))), ("aliases", dict(aliases=str(bad))),
                                   ("rttm", dict(recording="absent")),
                                   ("merge", dict(options=dict(min_coverage=2))),
                                   ("render", dict(asr=str(metadata)))):
                with self.subTest(stage=stage):
                    entry = self.entry()
                    entry.update(changes)
                    path = self.save(root, [entry, self.entry("room-b")])
                    output = root / stage
                    summary = run_manifest(path, output)
                    failed, success = summary["entries"]
                    self.assertEqual(failed["status"], "error")
                    self.assertIsNone(failed["result"])
                    self.assertEqual(failed["error"]["stage"], stage)
                    self.assertEqual(failed["exports"]["srt"], dict(status="skipped", file=None))
                    self.assertFalse((output / "entry-0000.srt").exists())
                    self.assertEqual(success["status"], "ok")
                    self.assertEqual(self.cues(output / "entry-0001.srt"), [(0, 1000, "Bob: Hello")])

    def test_all_srt_failures_are_partial_but_all_prerequisite_failures_are_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            late = json.loads((BATCH / "manifest-srt.json").read_text())["entries"][0]
            for field in ("asr", "rttm"):
                late[field] = str(BATCH / late[field])
            path = self.save(root, [late])
            output = root / "partial"
            summary = run_manifest(path, output)
            self.assertEqual(summary["status"], "partial")
            self.assertTrue(summary["partial_results"])
            self.assertEqual(summary["counts"], dict(total=1, succeeded=0, failed=1, items=1, turns=1))
            self.assertEqual(sorted(p.name for p in output.iterdir()), ["entry-0000.json", "summary.json"])
            late["asr"] = "missing.json"
            self.save(root, [late])
            summary = run_manifest(path, root / "failed")
            self.assertEqual(summary["status"], "failed")
            self.assertFalse(summary["partial_results"])
            self.assertEqual(summary["counts"], dict(total=1, succeeded=0, failed=1, items=0, turns=0))
            self.assertEqual(summary["entries"][0]["exports"]["srt"]["status"], "skipped")

    def test_cli_dry_run_matches_real_outcome_and_preserves_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = BATCH / "manifest-srt.json"
            output = root / "out"
            dry = self.cli(path, output, "--dry-run")
            self.assertEqual(dry.returncode, 1, dry.stderr)
            self.assertIn("2 succeeded, 1 failed", dry.stdout)
            self.assertIn("no output written", dry.stdout)
            self.assertIn("entries[0] (after-limit/1) export:srt:", dry.stderr)
            self.assertEqual(dry.stderr.count("warning: entries[1] (clipping/1)"), 4)
            self.assertFalse(output.exists())
            dry_summary = run_manifest(path, output, dry_run=True)
            real = self.cli(path, output)
            self.assertEqual(real.returncode, 1, real.stderr)
            self.assertEqual(dry.stderr, real.stderr)
            dry_summary["dry_run"] = False
            self.assertEqual(dry_summary, json.loads((output / "summary.json").read_text()))
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            for extra in ([], ["--dry-run"]):
                proc = self.cli(path, output, *extra)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            for target in (output, root / "absent"):
                link = root / "link"
                link.symlink_to(target, target_is_directory=True)
                for extra in ([], ["--dry-run"]):
                    proc = self.cli(path, link, *extra)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertTrue(link.is_symlink())
                link.unlink()

    def test_subtitle_write_failure_is_run_level_and_removes_incomplete_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, [self.entry()])
            output = root / "out"

            def fail_srt(destination, text):
                if destination.suffix == ".srt":
                    self.assertTrue((output / "entry-0000.json").is_file())
                    raise OSError("simulated subtitle disk failure")
                write_atomic(destination, text)

            with patch("mediawrench.speakers.manifest.write_atomic", side_effect=fail_srt):
                with self.assertRaisesRegex(OSError, "subtitle disk failure"):
                    run_manifest(path, output)
            self.assertFalse(output.exists())
            self.assertEqual(run_manifest(path, output)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
