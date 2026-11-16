import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.asraudit import audit
from mediawrench.asraudit.output import atomic_outputs, review_srt

ROOT = Path(__file__).resolve().parents[2]


class CLITests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".tools"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.json"
        self.source.write_bytes(b'{ "segments": [{"start":1,"end":2,"text":"Thank you"},\n'
                                b'{"start":2,"end":3,"text":"Thank you"}] }\n')
        self.vad = self.directory / "vad.json"
        self.vad.write_text("[[0, 0.5]]", encoding="utf-8")

    def run_cli(self, *args, source=None):
        return subprocess.run([sys.executable, "-m", "mediawrench.asraudit", str(source or self.source), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_real_cli_json_csv_srt_and_original_bytes_hash(self):
        source_bytes = self.source.read_bytes()
        vad_bytes = self.vad.read_bytes()
        report_path, csv_path, srt_path = [self.directory / name for name in ("report.json", "flags.csv", "review.srt")]
        completed = self.run_cli("--vad", self.vad, "--json", report_path, "--csv", csv_path, "--review-srt", srt_path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["source"]["sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(report["vad_source"]["sha256"], hashlib.sha256(vad_bytes).hexdigest())
        self.assertTrue(report["unchanged_source"])
        self.assertEqual(report["speech_corroborated_repeats"], [0, 1])
        self.assertEqual(report["source_document"], json.loads(source_bytes))
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "Thank you")
        self.assertEqual(rows[0]["no_speech_prob"], "")
        self.assertIn("outside_speech", rows[0]["reasons"])
        self.assertIn("00:00:01,000 --> 00:00:03,000", srt_path.read_text())
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(self.vad.read_bytes(), vad_bytes)
        self.assertEqual(list(self.directory.glob(".asr-audit-*")), [])

    def test_twenty_percent_boundary_reaches_csv_srt_and_repeat_subset(self):
        self.source.write_text(json.dumps({"segments": [
            {"start": 0, "end": 10, "text": "one"},
            {"start": 10, "end": 20, "text": "one"}]}))
        self.vad.write_text("[[5,8], [0,6], [10,18]]")
        csv_path, srt_path = self.directory / "flags.csv", self.directory / "review.srt"
        completed = self.run_cli("--vad", self.vad, "--outside-speech-fraction", ".2",
                                 "--csv", csv_path, "--review-srt", srt_path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["speech_corroborated_repeats"], [0, 1])
        self.assertEqual([f["outside_speech_fraction"] for f in report["flags"]], [.2, .2])
        self.assertEqual(report["review_ranges"][0]["reasons"], ["adjacent_repeat", "outside_speech"])
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
        self.assertTrue(all("outside_speech" in row["reasons"] for row in rows))
        self.assertTrue(all(row["outside_speech_fraction"] == "0.2" for row in rows))
        self.assertIn("00:00:00,000 --> 00:00:20,000", srt_path.read_text())
        self.assertIn("outside_speech", srt_path.read_text())

    def test_native_adapter_cli_matches_normalized_and_preserves_source_bytes(self):
        fixtures = ROOT / "tests" / "asraudit" / "fixtures"
        native = fixtures / "whisper_cpp_v1_7_6.json"
        original = native.read_bytes()
        args = ("--vad", fixtures / "whisper_cpp_speech.json", "--outside-speech-fraction", ".2")
        csv_path, srt_path = self.directory / "native.csv", self.directory / "native.srt"
        completed = self.run_cli(*args, "--input-format", "whisper.cpp-json-v1.7.6",
                                 "--csv", csv_path, "--review-srt", srt_path, source=native)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        normalized = self.run_cli(*args, source=fixtures / "whisper_cpp_normalized.json")
        self.assertEqual(normalized.returncode, 0, normalized.stderr)
        self.assertEqual(report["segments"], json.loads(normalized.stdout)["segments"])
        self.assertEqual(report["source"]["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(report["source_document"], json.loads(original))
        self.assertEqual(native.read_bytes(), original)
        rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
        self.assertEqual([row["text"] for row in rows], [" Thank you, 世界"] * 2)
        self.assertIn("00:00:00,000 --> 00:00:20,000", srt_path.read_text())
        self.assertEqual(self.run_cli(source=native).returncode, 2)

    def test_native_adapter_cli_errors_include_paths_and_write_nothing(self):
        native = json.loads((ROOT / "tests/asraudit/fixtures/whisper_cpp_v1_7_6.json").read_text())
        for value in (10, "10000", float("nan"), float("inf")):
            native["transcription"][0]["offsets"]["to"] = value
            self.source.write_text(json.dumps(native))
            destinations = [self.directory / n for n in ("out.json", "out.csv", "out.srt")]
            completed = self.run_cli("--input-format", "whisper.cpp-json-v1.7.6",
                                     "--json", destinations[0], "--csv", destinations[1],
                                     "--review-srt", destinations[2])
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("transcription[0].offsets.to", completed.stderr)
            self.assertFalse(any(p.exists() for p in destinations))

    def test_stdout_deterministic_unknowns_and_threshold_record(self):
        args = ("--max-chars-per-second", "12", "--min-repeat-segments", "3")
        first, second = self.run_cli(*args), self.run_cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertIsNone(report["speech_intervals"])
        self.assertIsNone(report["vad_source"])
        self.assertIsNone(report["segments"][0]["outside_speech_fraction"])
        self.assertEqual(report["thresholds"]["min_repeat_segments"], 3)
        self.assertEqual(report["thresholds"]["max_chars_per_second"], 12)

    def test_dry_run_writes_nothing(self):
        outputs = [self.directory / name for name in ("report.json", "flags.csv", "review.srt")]
        completed = self.run_cli("--dry-run", "--json", outputs[0], "--csv", outputs[1], "--review-srt", outputs[2])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["unchanged_source"])
        self.assertFalse(any(path.exists() for path in outputs))

    def test_source_output_and_vad_aliases_refused(self):
        hard = self.directory / "hard.json"
        hard.hardlink_to(self.source)
        symbolic = self.directory / "symbolic.json"
        symbolic.symlink_to(self.source)
        before = self.source.read_bytes()
        for path in (self.source, hard, symbolic, self.vad):
            completed = self.run_cli("--vad", self.vad, "--json", path)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("aliases an input", completed.stderr)
        self.assertEqual(self.source.read_bytes(), before)

    def test_existing_or_duplicate_outputs_refused_before_any_write(self):
        existing = self.directory / "old.csv"
        existing.write_text("keep")
        fresh = self.directory / "new.json"
        completed = self.run_cli("--json", fresh, "--csv", existing)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(fresh.exists())
        self.assertEqual(existing.read_text(), "keep")
        completed = self.run_cli("--json", fresh, "--csv", fresh)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("duplicate output", completed.stderr)
        self.assertFalse(fresh.exists())

    def test_missing_directory_or_file_and_bad_thresholds_nonzero(self):
        for args in (("--json", self.directory / "missing" / "out.json"),
                     ("--vad", self.directory / "missing.json"),
                     ("--max-chars-per-second", "nan"),
                     ("--min-repeat-segments", "1"),
                     ("--unrecognized",)):
            with self.subTest(args=args):
                completed = self.run_cli(*args)
                self.assertEqual(completed.returncode, 2)
                self.assertTrue(completed.stderr)
        self.assertEqual(self.run_cli(source=self.directory / "missing.json").returncode, 2)

    def test_unsupported_json_vad_and_nonfinite_fail_without_sidecars(self):
        destination = self.directory / "report.json"
        for content in ("null", "{}", "[[0,1]]", '{"transcription":[]}',
                        '{"segments":[{"start":0,"end":1,"text":"ok","avg_logprob":NaN}]}'):
            self.source.write_text(content)
            completed = self.run_cli("--json", destination)
            self.assertEqual(completed.returncode, 2, content)
            self.assertFalse(destination.exists())
        self.source.write_text('{"segments":[]}')
        for content in ("null", "{}", "[[2,1]]", "[[0,Infinity]]"):
            self.vad.write_text(content)
            completed = self.run_cli("--vad", self.vad, "--json", destination)
            self.assertEqual(completed.returncode, 2, content)
            self.assertFalse(destination.exists())

    def test_invalid_timing_is_not_silently_discarded_from_report(self):
        self.source.write_text('{"segments":[{"start":-1,"end":2,"text":"retained"}]}')
        srt = self.directory / "review.srt"
        completed = self.run_cli("--review-srt", srt)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["unplaced_segments"], [0])
        self.assertIn("invalid timestamps", completed.stderr)
        self.assertEqual(srt.read_text(), "")

    def test_srt_rounds_outward_without_rewriting_input_text(self):
        document = {"segments": [{"start":.0001, "end":.0002, "text":"<b>原文</b>\n\n-->"}]}
        report = audit(document)
        srt = review_srt(report)
        self.assertIn("00:00:00,000 --> 00:00:00,001", srt)
        self.assertIn("excessive_character_rate", srt)
        self.assertEqual(report["source_document"], document)

    def test_staging_failure_leaves_no_partial_outputs_or_temp_files(self):
        first, second = self.directory / "first.json", self.directory / "second.csv"
        with self.assertRaises(UnicodeEncodeError):
            atomic_outputs({first: "valid", second: "unencodable \ud800"}, protected=[self.source])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(list(self.directory.glob(".asr-audit-*")), [])

    def test_csv_roundtrip_preserves_multiline_unicode_quotes(self):
        text = '你好, "interview"\nsecond line\n\nend'
        self.source.write_text(json.dumps({"segments": [{"start": 0, "end": 4, "text": text, "no_speech_prob": .9}]}))
        destination = self.directory / "flags.csv"
        completed = self.run_cli("--csv", destination)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        with destination.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["text"], text)
        self.assertEqual(json.loads(completed.stdout)["source_document"]["segments"][0]["text"], text)


if __name__ == "__main__":
    unittest.main()
