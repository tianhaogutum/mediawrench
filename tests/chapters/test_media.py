"""Probe-contract tests use synthetic JSON, not real media evidence."""
import copy
import hashlib
from decimal import Decimal
from fractions import Fraction
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chapters import Chapter, Document, dumps

ROOT = Path(__file__).resolve().parents[2]


def document():
    return Document([Chapter(0, 1_000_000, "Intro"),
                     Chapter(1_000_000, 2_000_000, "章节")], 2_000_000)


def probe_data():
    return {"chapters": [
        {"time_base": "1/1000", "start": 0, "end": 1000, "tags": {"title": "Intro"}},
        {"time_base": "1/1000", "start": 1000, "end": 2000, "tags": {"title": "章节"}}]}


class MediaCLITests(unittest.TestCase):
    def test_missing_explicit_probe_is_skipped_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "local.mkv"
            media.touch()
            report = root / "audit.json"
            report.write_text(json.dumps(dumps(document(), "ffmetadata").report), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", "verify-media",
                                     str(media), "--report", str(report), "--ffprobe",
                                     str(root / "missing-ffprobe")], cwd=ROOT,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 3, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "skipped")
            self.assertIsNone(data["equal"])
            self.assertIn("ffprobe", data["reason"])
            self.assertEqual(data["checks"], [])


class ProbeHarness:
    def setUp(self):
        from mediawrench.chapters.media import verify_media
        self.verify = verify_media
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.media = Path(self.temporary.name) / "-synthetic.mkv"
        self.media.touch()
        self.report = dumps(document(), "ffmetadata").report

    def run_probe(self, data, **kwargs):
        result = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"), \
             patch("mediawrench.chapters.media.subprocess.run", return_value=result) as run:
            report = self.verify(self.media, self.report, ffprobe="ffprobe", **kwargs)
        return report, run


class MediaResultFileTests(ProbeHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.audit = self.media.parent / "audit.json"
        self.audit.write_text(json.dumps(self.report), encoding="utf-8")
        self.output = self.media.parent / "verification.json"

    def cli(self, *options, data=None, expected=0, probe_result=None, probe_error=None,
            executable="/tools/ffprobe"):
        from mediawrench.chapters.cli import main
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             patch("mediawrench.chapters.media.shutil.which", return_value=executable), \
             patch("mediawrench.chapters.media.subprocess.run", return_value=
                   probe_result or subprocess.CompletedProcess(
                       [], 0, json.dumps(probe_data() if data is None else data), ""),
                   side_effect=probe_error) as run:
            try:
                code = main(["verify-media", str(self.media), "--report", str(self.audit),
                             "--ffprobe", "ffprobe", *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expected, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.last_probe = run
        self.assertEqual(list(self.media.parent.glob(".chapter-roundtrip-*")), [])
        if expected == 2:
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_hash_inputs_saves_known_media_digest_and_exact_audit_bytes(self):
        self.media.write_bytes(b"abc")
        # Noncanonical formatting, UTF-8 and CRLF must identify the parsed bytes.
        raw = (" \r\n" + json.dumps(self.report, ensure_ascii=False, indent=3)
               .replace("\n", "\r\n") + "\r\n ").encode("utf-8")
        self.audit.write_bytes(raw)
        default, _ = self.cli()
        stdout, _ = self.cli("--hash-inputs", "--output", self.output)
        result = json.loads(stdout)
        self.assertEqual(result.pop("input_fingerprints"), {
            "media": {"sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                      "size_bytes": 3},
            "report": {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}})
        self.assertEqual(result, json.loads(default))
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
        self.assertEqual(self.media.read_bytes(), b"abc")
        self.assertEqual(self.audit.read_bytes(), raw)

        self.cli("--hash-inputs", "--output", self.output, expected=2)
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
        forced, _ = self.cli("--hash-inputs", "--output", self.output, "--force")
        self.assertEqual(forced, stdout)
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))

    def test_hash_inputs_binary_multichunk_and_audit_are_read_once_in_bounded_chunks(self):
        raw = bytes(range(256)) * 9000 + b"\x00\xff\r\n"
        self.media.write_bytes(raw)
        audit_bytes = self.audit.read_bytes()
        original_open = Path.open
        reads = {self.media: [], self.audit: []}
        opens = {self.media: 0, self.audit: 0}

        class BoundedReader:
            def __init__(reader, stream, path):
                reader.stream, reader.path = stream, path

            def __enter__(reader):
                reader.stream.__enter__()
                return reader

            def __exit__(reader, *args):
                return reader.stream.__exit__(*args)

            def read(reader, size=-1):
                self.assertGreater(size, 0, "unbounded input read")
                self.assertLessEqual(size, 1024 * 1024)
                chunk = reader.stream.read(size)
                reads[reader.path].append(len(chunk))
                return chunk

        def guarded_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if path in reads:
                self.assertEqual(mode, "rb", "fingerprints require exact binary bytes")
                opens[path] += 1
                return BoundedReader(stream, path)
            return stream

        with patch.object(Path, "open", guarded_open):
            stdout, _ = self.cli("--hash-inputs", "--output", self.output)
        fingerprints = json.loads(stdout)["input_fingerprints"]
        for path, key, data in ((self.media, "media", raw), (self.audit, "report", audit_bytes)):
            self.assertEqual(fingerprints[key], {"sha256": hashlib.sha256(data).hexdigest(),
                                                 "size_bytes": len(data)})
            self.assertEqual(opens[path], 1)
            self.assertEqual(sum(reads[path]), len(data))
        self.assertGreater(len(reads[self.media]), 3)
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))

    def test_hash_inputs_empty_media_known_digest_for_different_and_skipped_results(self):
        overlap = probe_data()
        overlap["chapters"][0].update(time_base="1/3000000", end=3_000_001)
        for code, options, kwargs in (
                (1, ("--verify-duration", "--expected-duration", "3"), {"data": overlap}),
                (3, ("--verify-duration",), {}),
                (3, ("--verify-duration",), {"executable": None})):
            with self.subTest(code=code, kwargs=kwargs):
                default, _ = self.cli(*options, expected=code, **kwargs)
                stdout, _ = self.cli("--hash-inputs", "--output", self.output,
                                     *options, expected=code, **kwargs)
                result = json.loads(stdout)
                fingerprints = result.pop("input_fingerprints")
                self.assertEqual(fingerprints["media"], {
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "size_bytes": 0})
                self.assertEqual(fingerprints["report"], {
                    "sha256": hashlib.sha256(self.audit.read_bytes()).hexdigest(),
                    "size_bytes": len(self.audit.read_bytes())})
                self.assertEqual(result, json.loads(default))
                self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
                self.output.unlink()

    def test_default_never_calls_input_hasher_even_with_saved_or_skipped_result(self):
        with patch("mediawrench.chapters.cli._hash_input", side_effect=AssertionError("implicit hash")):
            stdout, _ = self.cli("--output", self.output)
            self.assertNotIn("input_fingerprints", json.loads(stdout))
            self.output.unlink()
            stdout, _ = self.cli("--output", self.output, executable=None, expected=3)
            self.assertNotIn("input_fingerprints", json.loads(stdout))

    def test_hash_open_and_partial_read_failures_never_publish_even_when_skipped(self):
        original_open = Path.open
        for source in (self.media, self.audit):
            for failure in ("open", "read"):
                for executable in ("/tools/ffprobe", None):
                    for existing in (False, True):
                        if existing:
                            self.output.write_bytes(b"keep")
                        reads = []

                        class FailingReader(BytesIO):
                            def read(reader, size=-1):
                                reads.append(size)
                                if len(reads) > 1:
                                    raise OSError("synthetic partial read failure")
                                return b"partial"

                        def failing_open(path, mode="r", *args, **kwargs):
                            if path == source and mode == "rb":
                                if failure == "open":
                                    raise PermissionError("synthetic permission failure")
                                return FailingReader()
                            return original_open(path, mode, *args, **kwargs)

                        with self.subTest(source=source, failure=failure, probe=executable,
                                          existing=existing), patch.object(Path, "open", failing_open):
                            self.cli("--hash-inputs", "--output", self.output, "--force",
                                     executable=executable, expected=2)
                        if existing:
                            self.assertEqual(self.output.read_bytes(), b"keep")
                            self.output.unlink()
                        else:
                            self.assertFalse(self.output.exists())
                        if failure == "read":
                            self.assertEqual(len(reads), 2)

    def test_output_saves_matching_result_identical_to_stdout(self):
        default, _ = self.cli()
        self.assertFalse(self.output.exists())
        stdout, stderr = self.cli("--output", self.output)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, default)
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
        self.assertEqual(json.loads(stdout)["status"], "passed")
        self.assertEqual(self.cli()[0], default)
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))

    def test_saved_different_and_unavailable_results_keep_exact_details(self):
        overlap = probe_data()
        overlap["chapters"][0].update(time_base="1/3000000", end=3_000_001)
        fallback = {**probe_data(), "streams": [
            {"codec_type": "audio", "duration_ts": 6_000_001, "time_base": "1/3000000"},
            {"codec_type": "audio"}]}
        for data, code in ((probe_data(), 3), (overlap, 1), (fallback, 1)):
            with self.subTest(data=data):
                options = ("--verify-duration", "--expected-duration", "3", "--tolerance-us", "1")
                default, _ = self.cli(*options, data=data, expected=code)
                stdout, _ = self.cli(*options, "--output", self.output, data=data, expected=code)
                self.assertEqual(stdout, default)
                self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
                result = json.loads(stdout)
                duration = result["duration"]
                self.assertEqual(duration["expected_source"], "explicit expected_duration")
                self.assertEqual(duration["expected_us"], 3_000_000)
                if data is overlap:
                    self.assertEqual(result["differences"], [{
                        "path": "/chapters/1/start_us", "kind": "overlap",
                        "previous_path": "/chapters/0/end_us", "previous_end_us": "3000001/3",
                        "actual_us": 1_000_000, "overlap_us": "1/3"}])
                    self.assertEqual(result["checks"][0]["end_us"]["delta_us"], "1/3")
                if data is fallback:
                    self.assertEqual(duration["actual_us"], "6000001/3")
                    self.assertEqual(duration["delta_us"], "-2999999/3")
                    self.assertEqual(duration["source"], "/streams/0/duration_ts")
                    self.assertTrue(duration["estimated"])
                    self.assertEqual([d["kind"] for d in duration["diagnostics"]],
                                     ["stream_duration_fallback", "incomplete_stream_durations",
                                      "estimated_duration"])
                else:
                    self.assertEqual(duration["status"], "unavailable")
                    self.assertIsNone(duration["estimated"])
                self.output.unlink()

    def test_missing_probe_also_saves_skipped_result(self):
        stdout, _ = self.cli("--output", self.output, "--verify-duration",
                             executable=None, expected=3)
        self.last_probe.assert_not_called()
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
        result = json.loads(stdout)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["duration"]["expected_source"],
                         "conversion report comparison.right.duration_us")

    def test_existing_output_needs_force_and_replacement_preserves_old_hardlink(self):
        self.output.write_bytes(b"keep")
        old_link = self.media.parent / "old-result.json"
        old_link.hardlink_to(self.output)
        _, stderr = self.cli("--output", self.output, expected=2)
        self.assertIn("use --force", stderr)
        self.assertEqual(self.output.read_bytes(), b"keep")
        stdout, _ = self.cli("--output", self.output, "--force")
        self.assertEqual(self.output.read_bytes(), stdout.encode("utf-8"))
        self.assertEqual(old_link.read_bytes(), b"keep")
        self.assertFalse(old_link.samefile(self.output))

    def test_force_requires_output_before_probing(self):
        _, stderr = self.cli("--force", expected=2)
        self.assertIn("--force requires --output", stderr)
        self.last_probe.assert_not_called()

    def test_output_cannot_alias_either_input_even_with_force(self):
        originals = {p: p.read_bytes() for p in (self.media, self.audit)}
        parent_alias = self.media.parent / "parent-alias"
        parent_alias.symlink_to(self.media.parent, target_is_directory=True)
        for source in originals:
            for kind in ("same", "normalized", "parent-symlink", "hardlink", "symlink"):
                if kind == "same":
                    target = source
                elif kind == "normalized":
                    target = self.media.parent / ".." / self.media.parent.name / source.name
                elif kind == "parent-symlink":
                    target = parent_alias / source.name
                else:
                    target = self.output
                    if kind == "hardlink":
                        target.hardlink_to(source)
                    else:
                        target.symlink_to(source)
                for force in ((), ("--force",), ("--hash-inputs",), ("--hash-inputs", "--force")):
                    with self.subTest(source=source, kind=kind, force=force):
                        self.cli("--output", target, *force, expected=2)
                        self.last_probe.assert_not_called()
                        for path, original in originals.items():
                            self.assertEqual(path.read_bytes(), original)
                if kind in ("hardlink", "symlink"):
                    self.output.unlink()

    def test_output_symlinks_to_unrelated_or_missing_files_are_rejected(self):
        other = self.media.parent / "unrelated.json"
        for exists in (False, True):
            if exists:
                other.write_bytes(b"keep")
            self.output.symlink_to(other)
            for force in ((), ("--force",), ("--hash-inputs",), ("--hash-inputs", "--force")):
                with self.subTest(exists=exists, force=force):
                    self.cli("--output", self.output, *force, expected=2)
                    self.assertTrue(self.output.is_symlink())
                    self.assertEqual(other.exists(), exists)
                    if exists:
                        self.assertEqual(other.read_bytes(), b"keep")
            self.output.unlink()

    def test_invalid_inputs_and_probe_failures_never_publish_even_with_force(self):
        audit_bytes = self.audit.read_bytes()
        cases = [
            {"probe_result": subprocess.CompletedProcess([], 0, "not JSON", "")},
            {"data": {"chapters": [{"time_base": "1/0", "start": 0, "end": 1}]}},
            {"probe_result": subprocess.CompletedProcess([], 9, "", "probe failed")},
            {"probe_error": subprocess.TimeoutExpired("synthetic-probe", 15)},
            {"probe_error": OSError("cannot execute probe")},
            {"data": {**probe_data(), "format": {"duration": "bad"}}},
            {"options": ("--expected-duration", "1")},
            {"options": ("--timeout", "0")},
            {"audit": b"not JSON"}, {"audit": b"{}"}, {"missing_media": True}]
        for case in cases:
            for existing in (False, True):
                with self.subTest(case=case, existing=existing):
                    if existing:
                        self.output.write_bytes(b"keep")
                    self.audit.write_bytes(case.get("audit", audit_bytes))
                    if case.get("missing_media"):
                        self.media.unlink()
                    self.cli("--hash-inputs", "--output", self.output, "--force", "--verify-duration",
                             *case.get("options", ()), expected=2,
                             **{k: v for k, v in case.items() if k in ("data", "probe_result", "probe_error")})
                    if existing:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else:
                        self.assertFalse(self.output.exists())
                    self.media.touch()
        self.audit.write_bytes(audit_bytes)

    def test_staging_and_publication_failures_preserve_destination_and_clean_temps(self):
        self.cli("--hash-inputs", "--output", self.media.parent / "missing" / "result.json", expected=2)
        self.assertFalse((self.media.parent / "missing").exists())
        for function in ("fsync", "replace"):
            self.output.write_bytes(b"keep")
            with self.subTest(function=function), \
                 patch("mediawrench.chapters.cli.os." + function, side_effect=OSError("synthetic I/O failure")):
                self.cli("--hash-inputs", "--output", self.output, "--force", expected=2)
            self.assertEqual(self.output.read_bytes(), b"keep")

    def test_no_clobber_when_output_appears_during_publication(self):
        import os
        link = os.link

        def race(temporary, target):
            self.output.write_bytes(b"concurrent result")
            return link(temporary, target)

        with patch("mediawrench.chapters.cli.os.link", side_effect=race):
            self.cli("--hash-inputs", "--output", self.output, expected=2)
        self.assertEqual(self.output.read_bytes(), b"concurrent result")


class ProbeContractTests(ProbeHarness, unittest.TestCase):
    def test_confirmed_one_microsecond_overlap_fails_within_endpoint_tolerance(self):
        # Synthetic probe output, not real media.
        self.report = dumps(Document([Chapter(0, 1_000_000, "Intro"),
                                      Chapter(1_000_000, 2_000_000, "Next")], 2_000_000),
                            "ffmetadata").report
        data = {"chapters": [
            {"time_base": "1/1000000", "start": 0, "end": 1_000_001,
             "tags": {"title": "Intro"}},
            {"time_base": "1/1000000", "start": 1_000_000, "end": 2_000_000,
             "tags": {"title": "Next"}}]}
        result, _ = self.run_probe(data, tolerance_us=1)
        self.assertEqual(result["status"], "different")
        self.assertFalse(result["equal"])
        self.assertTrue(all(c[n]["within_tolerance"] for c in result["checks"]
                            for n in ("start_us", "end_us")))
        self.assertEqual(result["differences"], [{
            "path": "/chapters/1/start_us", "kind": "overlap",
            "previous_path": "/chapters/0/end_us", "previous_end_us": 1_000_001,
            "actual_us": 1_000_000, "overlap_us": 1}])

    def test_integer_and_fractional_overlaps_ignore_tolerance_and_keep_exact_values(self):
        from mediawrench.chapters.core import MAX_TIME
        for base, end, serialized, width in (("1/1000000", 1_000_001, 1_000_001, 1),
                                             ("1/3000000", 3_000_001, "3000001/3", "1/3")):
            data = probe_data()
            data["chapters"][0].update(time_base=base, end=end, end_time="1.000000")
            original = copy.deepcopy(data)
            for tolerance in (0, 1, MAX_TIME):
                with self.subTest(base=base, tolerance=tolerance):
                    result, _ = self.run_probe(data, tolerance_us=tolerance)
                    self.assertEqual(result["status"], "different")
                    self.assertFalse(result["equal"])
                    overlap = [d for d in result["differences"] if d["kind"] == "overlap"]
                    self.assertEqual(overlap, [{"path": "/chapters/1/start_us", "kind": "overlap",
                        "previous_path": "/chapters/0/end_us", "previous_end_us": serialized,
                        "actual_us": 1_000_000, "overlap_us": width}])
                    self.assertEqual(result["actual"][0]["end_us"], serialized)
                    self.assertEqual(result["checks"][0]["end_us"]["delta_us"], width)
                    self.assertEqual([d["kind"] for d in result["differences"]],
                                     ["outside_tolerance", "overlap"] if tolerance == 0 else ["overlap"])
            self.assertEqual(data, original)

    def test_touching_boundaries_and_integer_fractional_gaps_do_not_overlap(self):
        for first_end, next_start in ((3_000_000, 3_000_000), (2_999_999, 3_000_000),
                                       (3_000_000, 3_000_001), (2_999_997, 3_000_000),
                                       (3_000_001, 3_000_001)):
            data = probe_data()
            data["chapters"][0].update(time_base="1/3000000", end=first_end)
            data["chapters"][1].update(time_base="1/3000000", start=next_start, end=6_000_000)
            with self.subTest(first_end=first_end, next_start=next_start):
                result, _ = self.run_probe(data, tolerance_us=1)
                self.assertEqual(result["status"], "passed")
                self.assertTrue(result["equal"])
                self.assertEqual(result["differences"], [])
        # Reversing disjoint chapters still fails order, without inventing overlap.
        data["chapters"].reverse()
        result, _ = self.run_probe(data, tolerance_us=3_000_000)
        self.assertIn("order", [d["kind"] for d in result["differences"]])
        self.assertNotIn("overlap", [d["kind"] for d in result["differences"]])

    def test_extra_chapters_overlap_and_other_differences_are_preserved(self):
        data = probe_data()
        data["chapters"][1]["tags"]["title"] = "Changed"
        data["chapters"].append({"time_base": "1/3000000", "start": 5_999_999, "end": 9_000_000})
        result, _ = self.run_probe(data, tolerance_us=3_000_000)
        self.assertEqual([(d["path"], d["kind"]) for d in result["differences"]],
                         [("/chapters", "length"), ("/chapters/1/title", "changed"),
                          ("/chapters/2", "unexpected"), ("/chapters/2/start_us", "overlap")])
        self.assertEqual(result["differences"][-1]["actual_us"], "5999999/3")
        self.assertEqual(result["differences"][-1]["overlap_us"], "1/3")
        # Retain order and interval diagnoses alongside an actual overlap.
        data["chapters"][1].update(start=-1)
        result, _ = self.run_probe(data, tolerance_us=3_000_000)
        self.assertTrue({"length", "changed", "unexpected", "order", "invalid_interval", "overlap"}
                        <= {d["kind"] for d in result["differences"]})

    def test_overlap_is_not_hidden_by_optional_or_unavailable_duration(self):
        data = probe_data()
        data["chapters"][0].update(time_base="1/3000000", end=3_000_001)
        for value, duration_status in ((None, "unavailable"), ("2", "passed"), ("3", "different")):
            data["format"] = {"duration": value}
            with self.subTest(duration=value):
                result, _ = self.run_probe(data, tolerance_us=1, verify_duration=True,
                                           expected_duration="2")
                self.assertEqual(result["status"], "different")
                self.assertFalse(result["equal"])
                self.assertEqual(result["duration"]["status"], duration_status)
                self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")
                self.assertIn("overlap", [d["kind"] for d in result["differences"]])
                if value is not None:
                    self.assertTrue(result["duration"]["estimated"])
                    self.assertEqual(result["duration"]["source"], "/format/duration")
                    self.assertIn("estimated_duration", [d["kind"] for d in result["duration"]["diagnostics"]])

    def test_cli_overlap_exits_one_even_when_requested_duration_is_unavailable(self):
        from mediawrench.chapters.cli import main
        audit = self.media.parent / "audit.json"
        audit.write_text(json.dumps(self.report), encoding="utf-8")
        data = probe_data()
        data["chapters"][0].update(time_base="1/1000000", end=1_000_001)
        for options in ([], ["--verify-duration"], ["--verify-duration", "--expected-duration", "3"]):
            stdout, stderr = StringIO(), StringIO()
            with self.subTest(options=options), redirect_stdout(stdout), redirect_stderr(stderr), \
                 patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"), \
                 patch("mediawrench.chapters.media.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, json.dumps(data), "")):
                code = main(["verify-media", str(self.media), "--report", str(audit),
                             "--ffprobe", "ffprobe", "--tolerance-us", "1"] + options)
            self.assertEqual(code, 1)
            self.assertEqual(stderr.getvalue(), "")
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "different")
            self.assertFalse(result["equal"])
            self.assertEqual([d["kind"] for d in result["differences"]], ["overlap"])
            if options:
                self.assertEqual(result["duration"]["status"], "unavailable")

    def test_actual_endpoint_upper_bound_cannot_be_bypassed_by_tolerance(self):
        from mediawrench.chapters import ChapterError
        from mediawrench.chapters.core import MAX_TIME
        self.report = dumps(Document([Chapter(0, 2_000_000, "Intro")], 2_000_000),
                            "ffmetadata").report
        data = {"chapters": [{"time_base": "1/1000000", "start": 0, "end": MAX_TIME + 1,
                              "tags": {"title": "Intro"}}]}
        with self.assertRaisesRegex(ChapterError,
                                    f"ffprobe /chapters/0/end must not exceed {MAX_TIME} microseconds"):
            self.run_probe(data, tolerance_us=MAX_TIME)

    def test_upper_bound_validates_converted_starts_ends_and_unexpected_chapters(self):
        from mediawrench.chapters import ChapterError
        from mediawrench.chapters.core import MAX_TIME
        for index in (0, 2):
            for name in ("start", "end"):
                for base, tick in (("1/1000000", MAX_TIME + 1),
                                   ("1/3000000", 3 * MAX_TIME + 1),
                                   ("1/1", MAX_TIME // 1_000_000 + 1)):
                    data = probe_data()
                    if index == 2:
                        data["chapters"].append({"time_base": "1/1000000", "start": 2_000_000,
                                                 "end": 3_000_000})
                    data["chapters"][index].update(time_base=base, **{name: tick})
                    for tolerance in (0, MAX_TIME):
                        for verify_duration in (False, True):
                            with self.subTest(index=index, name=name, base=base,
                                              tolerance=tolerance, duration=verify_duration), \
                                 self.assertRaisesRegex(ChapterError,
                                     f"ffprobe /chapters/{index}/{name} must not exceed {MAX_TIME} microseconds"):
                                self.run_probe(data, tolerance_us=tolerance,
                                               verify_duration=verify_duration)

    def test_upper_bound_is_inclusive_and_keeps_rational_endpoints_exact(self):
        from mediawrench.chapters.core import MAX_TIME
        self.report = dumps(Document([Chapter(0, 2_000_000, "Intro")], 2_000_000),
                            "ffmetadata").report
        for start, end in ((0, 3 * MAX_TIME), (0, 3 * MAX_TIME - 1),
                           (3 * MAX_TIME - 1, 3 * MAX_TIME)):
            data = {"chapters": [{"time_base": "1/3000000", "start": start, "end": end,
                                  "tags": {"title": "Intro"}}]}
            result, _ = self.run_probe(data, tolerance_us=MAX_TIME)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["equal"])
            exact, _ = self.run_probe(data, tolerance_us=0)
            self.assertEqual(exact["status"], "different")
            for name, tick, expected in (("start_us", start, 0), ("end_us", end, 2_000_000)):
                value, delta = Fraction(tick, 3), Fraction(tick, 3) - expected
                serialized = value.numerator if value.denominator == 1 else str(value)
                self.assertEqual(result["actual"][0][name], serialized)
                self.assertIs(type(result["actual"][0][name]), type(serialized))
                self.assertEqual(exact["checks"][0][name]["delta_us"],
                                 delta.numerator if delta.denominator == 1 else str(delta))
        # MAX_TIME is also legal as a start; an empty interval still differs.
        data["chapters"][0]["start"] = 3 * MAX_TIME
        result, _ = self.run_probe(data, tolerance_us=MAX_TIME)
        self.assertEqual(result["actual"][0]["start_us"], MAX_TIME)
        self.assertEqual([d["kind"] for d in result["differences"]], ["invalid_interval"])

    def test_invalid_intervals_remain_differences_with_large_tolerance(self):
        from mediawrench.chapters.core import MAX_TIME
        for start, end in ((-1, 1), (0, -1), (0, 0), (2, 1)):
            data = probe_data()
            data["chapters"][0].update(time_base="1/1000000", start=start, end=end)
            result, _ = self.run_probe(data, tolerance_us=MAX_TIME)
            self.assertEqual(result["status"], "different")
            self.assertEqual(result["differences"], [{"path": "/chapters/0", "kind": "invalid_interval",
                                                     "reason": "requires 0 <= start < end"}])

    def test_cli_out_of_range_endpoints_exit_two_without_json(self):
        from mediawrench.chapters.cli import main
        from mediawrench.chapters.core import MAX_TIME
        audit = self.media.parent / "audit.json"
        audit.write_text(json.dumps(self.report), encoding="utf-8")
        argv = ["verify-media", str(self.media), "--report", str(audit), "--ffprobe", "ffprobe",
                "--tolerance-us", str(MAX_TIME)]
        for name in ("start", "end"):
            for base, tick in (("1/1000000", MAX_TIME + 1), ("1/3000000", 3 * MAX_TIME + 1)):
                data = probe_data()
                data["chapters"][1].update(time_base=base, **{name: tick})
                for options in ([], ["--verify-duration", "--expected-duration", "3"]):
                    stdout, stderr = StringIO(), StringIO()
                    with self.subTest(name=name, base=base, options=options), \
                         redirect_stdout(stdout), redirect_stderr(stderr), \
                         patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"), \
                         patch("mediawrench.chapters.media.subprocess.run", return_value=
                               subprocess.CompletedProcess([], 0, json.dumps(data), "")):
                        self.assertEqual(main(argv + options), 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(),
                                     f"mediawrench chapters: ffprobe /chapters/1/{name} "
                                     f"must not exceed {MAX_TIME} microseconds\n")

    def test_checks_every_field_and_uses_local_argv_without_shell(self):
        result, run = self.run_probe(probe_data())
        self.assertTrue(result["equal"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["checks"]), 2)
        for check in result["checks"]:
            self.assertTrue(check["title"]["equal"])
            for name in ("start_us", "end_us"):
                self.assertTrue(check[name]["within_tolerance"])
                self.assertEqual(check[name]["delta_us"], 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], str(self.media.resolve()))
        self.assertIn("file", args[0])
        self.assertIn("-show_chapters", args[0])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 15)

    def test_missing_extra_title_and_boundaries_have_precise_paths(self):
        variants = []
        missing = probe_data()
        missing["chapters"].pop()
        variants.append((missing, "/chapters/1", "missing"))
        extra = probe_data()
        extra["chapters"].append({"time_base": "1/1000", "start": 2000, "end": 3000})
        variants.append((extra, "/chapters/2", "unexpected"))
        title = probe_data()
        title["chapters"][1]["tags"]["title"] = "Changed"
        variants.append((title, "/chapters/1/title", "changed"))
        for key in ("start", "end"):
            timing = probe_data()
            timing["chapters"][1][key] += 20
            variants.append((timing, f"/chapters/1/{key}_us", "outside_tolerance"))
        for data, path, kind in variants:
            with self.subTest(path=path):
                result, _ = self.run_probe(data, tolerance_us=1000)
                self.assertFalse(result["equal"])
                self.assertIn((path, kind), [(d["path"], d["kind"]) for d in result["differences"]])

    def test_rational_ticks_do_not_use_rounded_start_time(self):
        data = probe_data()
        data["chapters"][0].update(time_base="1/3000000", start=1, end=3000000,
                                    start_time="0.000000")
        result, _ = self.run_probe(data, tolerance_us=0)
        self.assertEqual(result["checks"][0]["start_us"]["actual_us"], "1/3")
        self.assertEqual(result["differences"][0]["delta_us"], "1/3")
        self.assertFalse(result["equal"])
        self.assertTrue(self.run_probe(data, tolerance_us=1)[0]["equal"])

    def test_tolerance_is_inclusive_and_applies_to_both_bounds(self):
        data = probe_data()
        data["chapters"][0]["start"] = 1
        data["chapters"][1]["end"] = 2001
        self.assertTrue(self.run_probe(data, tolerance_us=1000)[0]["equal"])
        result, _ = self.run_probe(data, tolerance_us=999)
        self.assertEqual({d["path"] for d in result["differences"]},
                         {"/chapters/0/start_us", "/chapters/1/end_us"})

    def test_order_is_never_sorted_even_with_large_tolerance(self):
        data = probe_data()
        data["chapters"].reverse()
        result, _ = self.run_probe(data, tolerance_us=3_000_000)
        self.assertFalse(result["equal"])
        self.assertIn("order", [d["kind"] for d in result["differences"]])
        data["chapters"][1]["start"] = data["chapters"][0]["start"]
        self.assertIn("order", [d["kind"] for d in self.run_probe(data)[0]["differences"]])

    def test_empty_chapters_is_difference_and_no_inference_of_missing_bounds(self):
        result, _ = self.run_probe({"chapters": []})
        self.assertFalse(result["equal"])
        self.assertEqual(sum(d["kind"] == "missing" for d in result["differences"]), 2)
        data = probe_data()
        del data["chapters"][0]["end"]
        from mediawrench.chapters import ChapterError
        with self.assertRaisesRegex(ChapterError, "end"):
            self.run_probe(data)

    def test_exported_projection_not_original_source_is_expected(self):
        source = Document([Chapter(0, 1_000_000, "A\nB")], 1_000_000)
        self.report = dumps(source, "text", allow_lossy=True).report
        data = {"chapters": [{"time_base": "1/1000", "start": 0, "end": 1000,
                               "tags": {"title": "A B"}}]}
        self.assertTrue(self.run_probe(data)[0]["equal"])

    def test_malformed_reports_data_and_options_are_errors(self):
        from mediawrench.chapters import ChapterError
        for data in ({}, [], {"chapters": None}, {"chapters": [None]},
                     {"chapters": [{"time_base": "0/1", "start": 0, "end": 1}]},
                     {"chapters": [{"time_base": "1/0", "start": 0, "end": 1}]},
                     {"chapters": [{"time_base": "1/1000", "start": True, "end": 1}]},
                     {"chapters": [{"time_base": "1/1000", "start": 0, "end": 1,
                                    "tags": {"title": 3}}]}):
            with self.subTest(data=data), self.assertRaises(ChapterError):
                self.run_probe(data)
        original = copy.deepcopy(self.report)
        for report in ({}, {"schema_version": 1}, {**original, "comparison": {}},
                       {**original, "target_format": "unknown"},
                       {**original, "schema_version": True}, {**original, "schema_version": 2}):
            self.report = report
            with self.subTest(report=report), self.assertRaises(ChapterError):
                self.run_probe(probe_data())
        self.report = original
        for options in ({"tolerance_us": -1}, {"tolerance_us": True},
                        {"timeout": 0}, {"timeout": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ChapterError):
                self.run_probe(probe_data(), **options)

    def test_execution_timeout_nonzero_and_invalid_json_are_errors(self):
        from mediawrench.chapters import ChapterError
        with patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"):
            for error in (subprocess.TimeoutExpired("ffprobe", 15), PermissionError("denied")):
                with self.subTest(error=error), \
                     patch("mediawrench.chapters.media.subprocess.run", side_effect=error), \
                     self.assertRaises(ChapterError):
                    self.verify(self.media, self.report, ffprobe="ffprobe")

            for result in (subprocess.CompletedProcess([], 1, "", "invalid data"),
                           subprocess.CompletedProcess([], 0, "not JSON", "")):
                with self.subTest(result=result), \
                     patch("mediawrench.chapters.media.subprocess.run", return_value=result), \
                     self.assertRaises(ChapterError):
                    self.verify(self.media, self.report, ffprobe="ffprobe")


class DurationContractTests(ProbeHarness, unittest.TestCase):
    def test_explicit_duration_overrides_boundary_without_changing_chapters(self):
        original = copy.deepcopy(self.report)
        data = {**probe_data(), "format": {"duration": "3.000000"}}
        result, _ = self.run_probe(data, verify_duration=True, expected_duration="3",
                                   tolerance_us=0)
        self.assertTrue(result["equal"])
        self.assertEqual(result["duration"]["expected_us"], 3_000_000)
        self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")
        self.assertEqual(result["duration"]["source"], "/format/duration")
        self.assertIn("estimated_duration", [d["kind"] for d in result["duration"]["diagnostics"]])
        default, _ = self.run_probe(data, verify_duration=True, tolerance_us=0)
        self.assertFalse(default["equal"])
        self.assertEqual(default["duration"]["expected_us"], 2_000_000)
        self.assertEqual(default["duration"]["expected_source"],
                         "conversion report comparison.right.duration_us")
        self.assertEqual(result["checks"], default["checks"])
        data["format"]["duration"] = "2.000000"
        shortened, _ = self.run_probe(data, verify_duration=True, expected_duration="3",
                                      tolerance_us=0)
        self.assertEqual(shortened["differences"], [{"path": "/duration_us",
                         "kind": "outside_tolerance", "expected_us": 3_000_000,
                         "actual_us": 2_000_000, "delta_us": -1_000_000, "tolerance_us": 0}])
        self.assertTrue(self.run_probe(data, verify_duration=True)[0]["equal"])
        self.assertEqual(self.report, original)

    def test_explicit_duration_exact_range_extent_and_tolerance(self):
        from mediawrench.chapters import ChapterError
        from mediawrench.chapters.core import MAX_TIME
        data = {**probe_data(), "format": {"duration": "3.000001"}}
        for value in ("3.000001", Decimal("3.000001")):
            with self.subTest(value=value):
                result, _ = self.run_probe(data, verify_duration=True, expected_duration=value,
                                           tolerance_us=0)
                self.assertTrue(result["equal"])
                self.assertEqual(result["duration"]["expected_us"], 3_000_001)
        for value in ("3", 3):
            self.assertTrue(self.run_probe(data, verify_duration=True, expected_duration=value,
                                          tolerance_us=1)[0]["equal"])
            self.assertFalse(self.run_probe(data, verify_duration=True, expected_duration=value,
                                           tolerance_us=0)[0]["equal"])
        # Coverage is about chapter extent, not the report's longer boundary.
        doc = document()
        doc.duration_us = 4_000_000
        self.report = dumps(doc, "json").report
        data["format"]["duration"] = "2"
        self.assertTrue(self.run_probe(data, verify_duration=True, expected_duration="2",
                                      tolerance_us=0)[0]["equal"])
        maximum = "9223372036854.775807"
        data["format"]["duration"] = maximum
        result, _ = self.run_probe(data, verify_duration=True, expected_duration=maximum,
                                   tolerance_us=0)
        self.assertTrue(result["equal"])
        self.assertEqual(result["duration"]["expected_us"], MAX_TIME)
        for value in ("0", "1.999999", "-1", "3.0000001", "9223372036854.775808",
                      "nan", "inf", "3e0", "3/1", "", " 3", "+3", "3.", ".3",
                      "1" * 5000, True, 3.0, Fraction(3), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value), \
                 patch("mediawrench.chapters.media.subprocess.run") as run, \
                 self.assertRaises(ChapterError):
                self.verify(self.media, self.report, ffprobe="missing", verify_duration=True,
                            expected_duration=value, tolerance_us=MAX_TIME)
            run.assert_not_called()
        with self.assertRaisesRegex(ChapterError, "requires --verify-duration"):
            self.run_probe(data, expected_duration="3")

    def test_explicit_duration_keeps_fallback_estimates_and_unavailable_provenance(self):
        data = {**probe_data(), "format": {"duration": "N/A"}, "streams": [
            {"codec_type": "audio", "duration_ts": 9, "time_base": "1/3"},
            {"codec_type": "audio"}]}
        result, _ = self.run_probe(data, verify_duration=True, expected_duration="3", tolerance_us=0)
        self.assertTrue(result["equal"])
        self.assertEqual(result["duration"]["source"], "/streams/0/duration_ts")
        self.assertTrue(result["duration"]["estimated"])
        self.assertEqual([d["kind"] for d in result["duration"]["diagnostics"]],
                         ["stream_duration_fallback", "incomplete_stream_durations", "estimated_duration"])
        del data["streams"]
        result, _ = self.run_probe(data, verify_duration=True, expected_duration="3")
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["equal"])
        self.assertEqual(result["duration"]["expected_us"], 3_000_000)
        self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")
        self.assertEqual(result["duration"]["diagnostics"][0]["kind"], "unavailable_duration")
        data["chapters"].pop()
        self.assertEqual(self.run_probe(data, verify_duration=True, expected_duration="3")[0]["status"],
                         "different")
        with patch("mediawrench.chapters.media.shutil.which", return_value=None):
            result = self.verify(self.media, self.report, ffprobe="missing", verify_duration=True,
                                 expected_duration="3")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["duration"]["expected_us"], 3_000_000)
        self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")

    def test_cli_explicit_duration_exit_codes_and_invalid_options(self):
        from mediawrench.chapters.cli import main
        audit = self.media.parent / "audit.json"
        audit.write_text(json.dumps(self.report), encoding="utf-8")
        argv = ["verify-media", str(self.media), "--report", str(audit), "--ffprobe", "ffprobe",
                "--tolerance-us", "0"]
        for value, expected_exit in (("3", 0), ("2", 1), (None, 3), ("bad", 2)):
            data = {**probe_data(), "format": {"duration": value}}
            stdout, stderr = StringIO(), StringIO()
            with self.subTest(value=value), redirect_stdout(stdout), redirect_stderr(stderr), \
                 patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"), \
                 patch("mediawrench.chapters.media.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, json.dumps(data), "")):
                self.assertEqual(main(argv + ["--verify-duration", "--expected-duration", "3"]), expected_exit)
            if expected_exit != 2:
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["duration"]["expected_us"], 3_000_000)
                self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("/format/duration", stderr.getvalue())
        invalid = [["--expected-duration", "3"], ["--verify-duration", "--expected-duration"],
                   ["--verify-duration", "--expected-duration", "3", "--expected-duration", "4"],
                   ["--verify-duration", "--expected-duration", "3", "--expected-duration", "3"],
                   ["--verify-duration", "--duration", "3"]]
        invalid += [["--verify-duration", "--expected-duration=" + value] for value in
                    ("0", "1.999999", "-1", "3.0000001", "9223372036854.775808",
                     "nan", "inf", "3e0", "3/1", "", " 3", "+3", "3.", ".3")]
        for options in invalid:
            stdout, stderr = StringIO(), StringIO()
            with self.subTest(options=options), redirect_stdout(stdout), redirect_stderr(stderr), \
                 patch("mediawrench.chapters.media.subprocess.run") as run:
                try:
                    code = main(argv + options)
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn("Traceback", stderr.getvalue())
            run.assert_not_called()

    def test_cli_duration_exit_codes_and_unavailable_diagnostics(self):
        from mediawrench.chapters.cli import main
        audit = self.media.parent / "audit.json"
        audit.write_text(json.dumps(self.report), encoding="utf-8")
        argv = ["verify-media", str(self.media), "--report", str(audit), "--ffprobe", "ffprobe",
                "--verify-duration", "--tolerance-us", "0"]
        for value, expected_exit in (("2.000000", 0), ("1.000000", 1), (None, 3), ("bad", 2)):
            data = {**probe_data(), "format": {"duration": value}}
            stdout, stderr = StringIO(), StringIO()
            with self.subTest(value=value), redirect_stdout(stdout), redirect_stderr(stderr), \
                 patch("mediawrench.chapters.media.shutil.which", return_value="/tools/ffprobe"), \
                 patch("mediawrench.chapters.media.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, json.dumps(data), "")):
                self.assertEqual(main(argv), expected_exit)
            if expected_exit == 2:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("/format/duration", stderr.getvalue())
            else:
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["duration"]["status"],
                                 {0: "passed", 1: "different", 3: "unavailable"}[expected_exit])
                if expected_exit == 3:
                    self.assertIsNone(result["equal"])
                    self.assertIn("unavailable", result["reason"])

    def test_format_duration_opt_in_exact_comparison_and_diagnostics(self):
        data = {**probe_data(), "format": {"duration": "2.000000"}}
        result, run = self.run_probe(data, verify_duration=True, tolerance_us=0)
        self.assertTrue(result["equal"])
        duration = result["duration"]
        self.assertEqual(duration["status"], "passed")
        self.assertEqual(duration["expected_us"], 2_000_000)
        self.assertEqual(duration["actual_us"], 2_000_000)
        self.assertEqual(duration["source"], "/format/duration")
        self.assertTrue(duration["estimated"])
        self.assertIn("estimated_duration", [d["kind"] for d in duration["diagnostics"]])
        self.assertIn("-show_format", run.call_args.args[0])
        self.assertIn("-show_streams", run.call_args.args[0])
        # Shorter AND longer physical durations must fail, even if chapters match.
        for value, delta in (("1.000000", -1_000_000), ("3.000000", 1_000_000)):
            data["format"]["duration"] = value
            result, _ = self.run_probe(data, verify_duration=True)
            self.assertEqual(result["status"], "different")
            self.assertEqual(result["differences"][0]["path"], "/duration_us")
            self.assertEqual(result["differences"][0]["delta_us"], delta)

    def test_duration_tolerance_rationals_and_exported_boundary(self):
        data = {**probe_data(), "format": {"duration": "2.000001"}}
        self.assertTrue(self.run_probe(data, verify_duration=True, tolerance_us=1)[0]["equal"])
        self.assertFalse(self.run_probe(data, verify_duration=True, tolerance_us=0)[0]["equal"])
        data["format"]["duration"] = "2.0000001"
        result, _ = self.run_probe(data, verify_duration=True, tolerance_us=0)
        self.assertEqual(result["duration"]["delta_us"], "1/10")
        # JSON duration survives export; FFmetadata deliberately loses trailing space.
        doc = document()
        doc.duration_us = 3_000_000
        self.report = dumps(doc, "ffmetadata", allow_lossy=True).report
        data["format"]["duration"] = "2.0"
        self.assertTrue(self.run_probe(data, verify_duration=True)[0]["equal"])
        self.report = dumps(doc, "json").report
        self.assertEqual(self.run_probe(data, verify_duration=True)[0]["duration"]["expected_us"],
                         3_000_000)

    def test_stream_fallback_prefers_ticks_longest_audio_video_not_cover_art(self):
        data = {**probe_data(), "format": {"duration": "N/A"}, "streams": [
            {"codec_type": "audio", "duration_ts": 6, "time_base": "1/3", "duration": "9"},
            {"codec_type": "video", "duration": "1.5"},
            {"codec_type": "video", "duration": "99", "disposition": {"attached_pic": 1}},
            {"codec_type": "subtitle", "duration": "100"},
            {"codec_type": "audio"}]}
        result, _ = self.run_probe(data, verify_duration=True, tolerance_us=0)
        self.assertTrue(result["equal"])
        self.assertEqual(result["duration"]["source"], "/streams/0/duration_ts")
        kinds = [d["kind"] for d in result["duration"]["diagnostics"]]
        self.assertIn("stream_duration_fallback", kinds)
        self.assertIn("incomplete_stream_durations", kinds)
        data["streams"][0].update(duration_ts=1, time_base="1/3000000")
        data["streams"] = data["streams"][:1]
        result, _ = self.run_probe(data, verify_duration=True)
        self.assertEqual(result["duration"]["actual_us"], "1/3")
        # Format is authoritative when available; no mixing of inconsistent fields.
        data["format"]["duration"] = "2"
        self.assertTrue(self.run_probe(data, verify_duration=True)[0]["equal"])

    def test_unavailable_never_passes_or_hides_known_chapter_differences(self):
        for format_data in ({}, {"duration": None}, {"duration": "N/A"}):
            data = {**probe_data(), "format": format_data, "streams": []}
            result, _ = self.run_probe(data, verify_duration=True)
            self.assertEqual(result["status"], "skipped")
            self.assertIsNone(result["equal"])
            self.assertEqual(result["duration"]["status"], "unavailable")
            for key in ("actual_us", "source", "estimated", "delta_us", "within_tolerance"):
                self.assertIsNone(result["duration"][key])
            self.assertIn("unavailable_duration", [d["kind"] for d in result["duration"]["diagnostics"]])
        data["chapters"].pop()
        self.assertEqual(self.run_probe(data, verify_duration=True)[0]["status"], "different")
        with patch("mediawrench.chapters.media.shutil.which", return_value=None):
            result = self.verify(self.media, self.report, ffprobe="missing", verify_duration=True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["duration"]["status"], "unavailable")

    def test_duration_fields_ignored_unless_requested_and_strict_when_requested(self):
        from mediawrench.chapters import ChapterError
        for value in ("nan", "inf", "-1", "1/2", "", True, 2, "1e99", "1" * 5000):
            data = {**probe_data(), "format": {"duration": value}}
            result, run = self.run_probe(data)
            self.assertTrue(result["equal"])
            self.assertNotIn("duration", result)
            self.assertNotIn("-show_format", run.call_args.args[0])
            with self.subTest(value=value), self.assertRaises(ChapterError):
                self.run_probe(data, verify_duration=True)
        for data in ({"format": []}, {"streams": {}}, {"streams": [None]},
                     {"streams": [{"codec_type": "audio", "duration_ts": True, "time_base": "1/2"}]},
                     {"streams": [{"codec_type": "audio", "duration_ts": 2, "time_base": "1/0"}]},
                     {"streams": [{"codec_type": "audio", "duration_ts": 2}]}):
            with self.subTest(data=data), self.assertRaises(ChapterError):
                self.run_probe({**probe_data(), **data}, verify_duration=True)
        with self.assertRaises(ChapterError):
            self.run_probe(probe_data(), verify_duration="yes")
