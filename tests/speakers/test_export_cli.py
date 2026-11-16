import html
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pysubs2

from mediawrench.speakers import (
    BridgeError, merge, parse_rttm, render_json, render_markdown, render_srt, write_atomic,
)
from mediawrench.speakers.core import load_json

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "speakers"


def example():
    return merge(load_json((EXAMPLES / "whisper.json").read_text()),
                 parse_rttm((EXAMPLES / "interview.rttm").read_text()),
                 aliases={"Alice": "Interviewer", "Bob": "Participant"})


class ExportTests(unittest.TestCase):
    def test_json_roundtrip_and_evidence(self):
        result = example()
        self.assertEqual(load_json(render_json(result)), result)
        item = result["items"][1]
        self.assertEqual(item["speaker"], "AMBIGUOUS")
        self.assertEqual([e["coverage"] for e in item["evidence"]], [1, 1])

    def test_markdown_labels_and_precision(self):
        output = render_markdown(example())
        for text in ("**Interviewer**", "**Participant**", "**AMBIGUOUS**", "**UNKNOWN**", "(segment)", "2014"):
            self.assertIn(text, output)

    def test_srt_actual_roundtrip_timing_and_text(self):
        result = example()
        output = render_srt(result)
        subs = pysubs2.SSAFile.from_string(output, "srt", keep_html_tags=True)
        self.assertEqual(len(subs), len(result["turns"]))
        for cue, turn in zip(subs, result["turns"]):
            self.assertLessEqual(cue.start, turn["start"] * 1000)
            self.assertGreaterEqual(cue.end, turn["end"] * 1000)
            precision = " [segment]" if turn["precision"] == "segment" else ""
            self.assertEqual(html.unescape(cue.text), f"{turn['speaker_label']}{precision}: {' '.join(turn['text'].split())}")

    def test_srt_special_characters_literal_and_markdown_escape(self):
        text = "中文 <i>not styling</i> {\\b1} \\N &\n00:00:01,000 --> 00:00:02,000\n\n42"
        diar = parse_rttm("SPEAKER rec 1 0 2 <NA> <NA> A <NA> <NA>")
        result = merge({"segments": [{"start": 0, "end": 2, "text": text}]}, diar, aliases={"A": "<Guest>*"})
        srt = render_srt(result)
        subs = pysubs2.SSAFile.from_string(srt, "srt", keep_html_tags=True)
        self.assertEqual(len(subs), 1)
        self.assertEqual(html.unescape(subs[0].text), "<Guest>* [segment]: " + " ".join(text.split()))
        self.assertNotIn("<Guest>", render_markdown(result))
        self.assertIn(r"\*", render_markdown(result))
        self.assertEqual(load_json(render_json(result))["items"][0]["text"], text)

    def test_srt_short_and_zero_duration(self):
        diar = parse_rttm("SPEAKER rec 1 0 2 <NA> <NA> A <NA> <NA>")
        result = merge({"words": [{"start": .0001, "end": .0002, "word": "a"},
                                   {"start": 1, "end": 1, "word": "b"}]}, diar)
        subs = pysubs2.SSAFile.from_string(render_srt(result), "srt")
        self.assertEqual([(s.start, s.end) for s in subs], [(0, 1), (1000, 1001)])

    def test_srt_overflow_is_error_instead_of_clamping(self):
        diar = parse_rttm("SPEAKER rec 1 0 400000 <NA> <NA> A <NA> <NA>")
        result = merge({"words": [{"start": 360000, "end": 360001, "word": "a"}]}, diar)
        with self.assertRaisesRegex(BridgeError, "SRT timestamp"):
            render_srt(result)

    def test_atomic_replace_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.json"
            path.write_text("old")
            write_atomic(path, "new\n")
            self.assertEqual(path.read_text(), "new\n")
            # Only this failure path is injected. Actual CLI/format E2E uses real IO.
            with patch("mediawrench.speakers.export.os.replace", side_effect=OSError("simulated IO failure")):
                with self.assertRaises(OSError):
                    write_atomic(path, "bad")
            self.assertEqual(path.read_text(), "new\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_atomic_write_honors_umask_and_keeps_existing_mode(self):
        if os.name != "posix":
            self.skipTest("POSIX file modes")
        with tempfile.TemporaryDirectory() as directory:
            created = Path(directory) / "new.json"
            previous = os.umask(0o022)
            try:
                write_atomic(created, "new\n")
            finally:
                os.umask(previous)
            # Same default as a plain open(): umask applies, not the 0600 of mkstemp.
            self.assertEqual(created.stat().st_mode & 0o777, 0o644)
            existing = Path(directory) / "existing.json"
            existing.write_text("old")
            os.chmod(existing, 0o640)
            write_atomic(existing, "new\n")
            self.assertEqual(existing.read_text(), "new\n")
            self.assertEqual(existing.stat().st_mode & 0o777, 0o640)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                             ["existing.json", "new.json"])


class CLITests(unittest.TestCase):
    def run_cli(self, *args, cwd=ROOT):
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        return subprocess.run([sys.executable, "-m", "mediawrench.speakers", *map(str, args)],
                              cwd=cwd, env=env, capture_output=True, text=True, timeout=120)

    def standard(self, name="whisper.json"):
        return ["--asr", EXAMPLES / name, "--rttm", EXAMPLES / "interview.rttm"]

    def test_help(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--tie-epsilon", proc.stdout)

    def test_three_schema_e2e_all_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("whisper.json", "faster-whisper.json", "whisperx.json"):
                for format_ in ("json", "markdown", "srt"):
                    with self.subTest(schema=name, format=format_):
                        path = Path(directory) / f"{name}.{format_}"
                        proc = self.run_cli(*self.standard(name), "--format", format_, "--output", path,
                                            "--aliases", EXAMPLES / "aliases.json")
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(proc.stdout, "")
                        output = path.read_text()
                        self.assertTrue(output)
                        if format_ == "json":
                            result = load_json(output)
                            self.assertIn("source_rttm", result)
                            self.assertIn("source_asr", result)
                        elif format_ == "srt":
                            self.assertGreater(len(pysubs2.SSAFile.from_string(output, "srt")), 0)

    def test_stdout_structured_json(self):
        proc = self.run_cli(*self.standard())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)["items"]), 9)
        self.assertIn("segment timing", proc.stderr)

    def test_dry_run_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            proc = self.run_cli(*self.standard(), "--dry-run", "--output", path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("no output written", proc.stdout)
            self.assertFalse(path.exists())
            path.write_text("existing")
            proc = self.run_cli(*self.standard(), "--dry-run", "--output", path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(path.read_text(), "existing")

    def test_failures_nonzero_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            path.write_text("sentinel")
            for args in (["--min-coverage", "1.1"], ["--tie-epsilon", "nan"], ["--max-gap", "-1"],
                         ["--recording", "missing"], ["--channel", "2"], ["--format", "vtt"],
                         ["--aliases", Path(directory) / "missing.json"]):
                with self.subTest(args=args):
                    proc = self.run_cli(*self.standard(), *args, "--output", path)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(path.read_text(), "sentinel")

    def test_malformed_input_files(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            for data in (b"\xff", b'{"words":[]}', b'{"segments": [}', b'{"words":[{"word":"a","start":NaN,"end":1}]}'):
                with self.subTest(data=data):
                    bad.write_bytes(data)
                    proc = self.run_cli("--asr", bad, "--rttm", EXAMPLES / "interview.rttm")
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertNotIn("Traceback", proc.stderr)
            bad.write_text("bad RTTM")
            proc = self.run_cli("--asr", EXAMPLES / "whisper.json", "--rttm", bad)
            self.assertEqual(proc.returncode, 2)

    def test_duplicate_alias_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "aliases.json"
            alias.write_text('{"Alice":"one","Alice":"two"}')
            proc = self.run_cli(*self.standard(), "--aliases", alias)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("duplicate JSON key", proc.stderr)

    def test_no_overwriting_inputs_or_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            asr = Path(directory) / "in.json"
            data = (EXAMPLES / "whisper.json").read_bytes()
            asr.write_bytes(data)
            hardlink = Path(directory) / "hardlink.json"
            hardlink.hardlink_to(asr)
            for output in (asr, hardlink):
                with self.subTest(output=output):
                    proc = self.run_cli("--asr", asr, "--rttm", EXAMPLES / "interview.rttm", "--output", output)
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(asr.read_bytes(), data)

    def test_recording_selection_e2e(self):
        with tempfile.TemporaryDirectory() as directory:
            rttm = Path(directory) / "two.rttm"
            rttm.write_text((EXAMPLES / "interview.rttm").read_text() +
                            "\nSPEAKER other 1 0 10 <NA> <NA> Other <NA> <NA>\n")
            args = ["--asr", EXAMPLES / "whisper.json", "--rttm", rttm]
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("multiple recordings", proc.stderr)
            proc = self.run_cli(*args, "--recording", "interview")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["recording"], "interview")

    def test_explicit_clock_correction_cli_all_formats(self):
        args = ["--asr", EXAMPLES / "clock-asr.json", "--rttm", EXAMPLES / "clock-shifted.rttm",
                "--rttm-offset-seconds", "-2", "--rttm-drift-ppm", "250000", "--recording-duration", "6"]
        for format_ in ("json", "markdown", "srt"):
            with self.subTest(format=format_):
                actual = self.run_cli(*args, "--format", format_)
                expected = self.run_cli("--asr", EXAMPLES / "clock-asr.json", "--rttm",
                                        EXAMPLES / "clock-baseline.rttm", "--format", format_)
                self.assertEqual(actual.returncode, 0, actual.stderr)
                self.assertEqual(expected.returncode, 0, expected.stderr)
                if format_ == "json":
                    result = json.loads(actual.stdout)
                    self.assertEqual(result["items"], json.loads(expected.stdout)["items"])
                    self.assertEqual(result["corrected_rttm"][0]["original_start"], 2)
                    self.assertEqual(result["corrected_rttm"][0]["start"], .5)
                else:
                    self.assertEqual(actual.stdout, expected.stdout)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dry.json"
            proc = self.run_cli(*args, "--dry-run", "--output", output)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("4 items", proc.stdout)
            self.assertFalse(output.exists())

    def test_clock_failures_preserve_output_and_dry_run_validates(self):
        args = ["--asr", EXAMPLES / "clock-asr.json", "--rttm", EXAMPLES / "clock-shifted.rttm"]
        failures = [(["--rttm-offset-seconds", "-3"], "negative corrected time"),
                    (["--rttm-offset-seconds", "nan"], "rttm_offset_seconds"),
                    (["--rttm-drift-ppm", "inf"], "rttm_drift_ppm"),
                    (["--rttm-drift-ppm", "-1000000"], "reversed or collapsed"),
                    (["--rttm-offset-seconds", "1e20"], "reversed or collapsed"),
                    (["--recording-duration", "0"], "recording_duration"),
                    (["--rttm-offset-seconds", "-2", "--rttm-drift-ppm", "250000",
                      "--recording-duration", "5"], "RTTM line 3"),
                    (["--rttm-offset-seconds", "-2", "--rttm-drift-ppm", "250000",
                      "--recording-duration", "5.5"], "ASR item 3")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "keep.json"
            output.write_text("sentinel")
            for options, diagnostic in failures:
                for dry_run in ([], ["--dry-run"]):
                    with self.subTest(options=options, dry_run=dry_run):
                        proc = self.run_cli(*args, *options, *dry_run, "--output", output)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertIn(diagnostic, proc.stderr)
                        self.assertNotIn("Traceback", proc.stderr)
                        self.assertEqual(proc.stdout, "")
                        self.assertEqual(output.read_text(), "sentinel")
            self.assertEqual(list(Path(directory).iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
