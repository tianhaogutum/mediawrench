from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.rollup.cli import _atomic_write

ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="caption-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "input.vtt"
        self.source.write_bytes((ROOT / "examples/rollup/rolling.vtt").read_bytes())
        self.output = self.folder / "output.srt"
        self.mapping = Path(str(self.output) + ".json")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "mediawrench.rollup", str(self.source),
                               "-o", str(self.output), *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})

    def test_actual_cli_files_hash_and_provenance(self):
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads(self.mapping.read_text(encoding="utf-8"))
        self.assertEqual(data["srt_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(data["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(len(data["tokens"]), 8)
        self.assertEqual(sum(t["action"] == "retained" for t in data["tokens"]), 6)
        self.assertEqual(json.loads(done.stdout)["summary"]["removed_tokens"], 2)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["input.vtt", "output.srt", "output.srt.json"])

    def test_dry_run_is_read_only_and_reports(self):
        before = sorted(p.name for p in self.folder.iterdir())
        done = self.run_cli("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)["dry_run"])
        self.assertEqual(before, sorted(p.name for p in self.folder.iterdir()))

    def test_existing_output_requires_force(self):
        self.output.write_text("keep me", encoding="utf-8")
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn("--force", done.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(self.mapping.exists())

    def test_word_overlap_requires_explicit_flag_and_writes_source_spans(self):
        self.source.write_bytes((ROOT / "examples/rollup/word-growing.vtt").read_bytes())
        default = self.run_cli("--dry-run")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(json.loads(default.stdout)["summary"]["removed_tokens"], 0)
        done = self.run_cli("--word-overlap")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["summary"]["removed_tokens"], 5)
        data = json.loads(self.mapping.read_text(encoding="utf-8"))
        self.assertTrue(data["options"]["word_overlap"])
        self.assertEqual([c["text"] for c in data["output_cues"]], ["We are", "ready", "now"])
        self.assertEqual(data["output_cues"][1]["source_span"],
                         {"cue": 2, "token_start": 3, "token_end": 4})
        self.assertEqual(data["srt_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_word_overlap_dry_run_reports_without_writes(self):
        self.source.write_bytes((ROOT / "examples/rollup/word-growing.vtt").read_bytes())
        done = self.run_cli("--word-overlap", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["summary"]["removed_tokens"], 5)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.mapping.exists())

    def test_word_overlap_pause_is_successful_conservative_conversion(self):
        self.source.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nWe are\n\n"
                               "00:03.000 --> 00:05.000\nWe are ready\n", encoding="utf-8")
        done = self.run_cli("--word-overlap", "--max-gap-ms", "10000")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["summary"]["removed_tokens"], 0)
        self.assertIn("strict temporal overlap", report["ambiguities"][0]["reason"])
        self.assertTrue(self.output.exists())

    def test_existing_map_does_not_leave_partial_srt(self):
        self.mapping.write_text("keep map", encoding="utf-8")
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.mapping.read_text(encoding="utf-8"), "keep map")

    def test_force_replaces_both_outputs_and_cleans_temps(self):
        self.output.write_text("old srt", encoding="utf-8")
        self.mapping.write_text("old map", encoding="utf-8")
        done = self.run_cli("--force")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Hello there", self.output.read_text(encoding="utf-8"))
        self.assertEqual(len(list(self.folder.iterdir())), 3)

    def test_dry_run_force_never_changes_existing_files(self):
        self.output.write_text("original", encoding="utf-8")
        done = self.run_cli("--dry-run", "--force")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.mapping.exists())

    def test_invalid_input_has_no_outputs(self):
        self.source.write_text("WEBVTT\n\n00:03.000 --> 00:01.000\nHello there\n", encoding="utf-8")
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.mapping.exists())

    def test_unsupported_settings_even_flatten_exit_nonzero(self):
        self.source.write_text("WEBVTT\n\n00:00.000 --> 00:01.000 position:50%\nHello there\n", encoding="utf-8")
        done = self.run_cli("--flatten")
        self.assertEqual(done.returncode, 2)
        self.assertIn("Unsupported cue settings", done.stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_utf8(self):
        self.source.write_bytes(b"WEBVTT\n\n\xff")
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertFalse(self.output.exists())

    def test_missing_input(self):
        self.source.unlink()
        self.assertEqual(self.run_cli().returncode, 2)

    def test_input_cannot_be_overwritten(self):
        original = self.source.read_bytes()
        for args in [("--output", self.source), ("--map", self.source)]:
            with self.subTest(args=args):
                done = self.run_cli(*args, "--force")
                self.assertEqual(done.returncode, 2)
                self.assertEqual(self.source.read_bytes(), original)

    def test_alias_paths_and_symlink_and_hardlink_rejected(self):
        self.assertEqual(self.run_cli("--map", self.output).returncode, 2)
        self.output.symlink_to(self.source)
        self.assertEqual(self.run_cli("--force").returncode, 2)
        self.output.unlink()
        os.link(self.source, self.output)
        self.assertEqual(self.run_cli("--force").returncode, 2)

    def test_missing_parent_is_not_created(self):
        target = self.folder / "missing" / "out.srt"
        self.assertEqual(self.run_cli("--output", target).returncode, 2)
        self.assertFalse(target.parent.exists())

    def test_custom_mapping_path_and_extension_override(self):
        custom = self.folder / "provenance.json"
        self.source.rename(self.folder / "input.bin")
        self.source = self.folder / "input.bin"
        done = self.run_cli("--input-format", "vtt", "--map", custom)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(custom.exists())
        self.assertFalse(self.mapping.exists())

    def test_actual_cli_idempotence(self):
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.source = self.output
        self.output = self.folder / "second.srt"
        done = self.run_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.source.read_bytes(), self.output.read_bytes())
        self.assertEqual(json.loads(done.stdout)["mode"], "srt_passthrough")

    def test_staged_publication_failure_rolls_back_both_files(self):
        # Fault injection checks rollback only; E2E format tests use real I/O.
        second = self.folder / "second.txt"
        self.output.write_bytes(b"original first")
        second.write_bytes(b"original second")
        replace = os.replace
        def fail_second(source, target):
            if Path(target) == second and str(source).endswith(".tmp"):
                raise OSError("injected publication failure")
            return replace(source, target)
        with patch("mediawrench.rollup.cli.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                _atomic_write({self.output: b"new first", second: b"new second"}, force=True)
        self.assertEqual(self.output.read_bytes(), b"original first")
        self.assertEqual(second.read_bytes(), b"original second")
        self.assertEqual(len(list(self.folder.iterdir())), 3)

    def test_exclusive_publication_preserves_concurrent_file(self):
        self.output.write_bytes(b"concurrent")
        with self.assertRaises(FileExistsError):
            _atomic_write({self.output: b"new"}, force=False)
        self.assertEqual(self.output.read_bytes(), b"concurrent")
        self.assertEqual(len(list(self.folder.iterdir())), 2)
