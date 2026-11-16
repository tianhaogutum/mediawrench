import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from examples.postflight.synthetic import FFMPEG_AVAILABLE, FFMPEG_MISSING, ffmpeg, generate
from mediawrench.postflight import Options, compare, exit_code, write_report
from mediawrench.postflight.core import CheckError, _frame_facts

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checks(report, name):
    return [c for c in report["checks"] if c["name"] == name]


@unittest.skipUnless(FFMPEG_AVAILABLE, FFMPEG_MISSING)
class RealMediaTests(unittest.TestCase):
    """Requires real FFmpeg/ffprobe on PATH; skipped (never mocked) without them."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="postflight-test-")
        cls.root = Path(cls.temp.name)
        cls.files = generate(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compare(self, case="healthy", options=None):
        return compare(self.files["source"], self.files[case], options)

    def test_healthy_full_decode_positive_each_stream(self):
        report = self.compare()
        self.assertEqual(report["verdict"], "PASS", report)
        self.assertEqual(report["coverage"], "full")
        for role in ("source", "output"):
            self.assertEqual(len(report["decoded"][role]), 2)
            for facts in report["decoded"][role].values():
                self.assertGreater(facts["frames"], 0)
                self.assertGreater(facts["decoded_bytes"], 0)
                self.assertAlmostEqual(facts["duration_seconds"], 2, delta=0.01)

    def test_real_media_passes_with_whole_job_deadline(self):
        report = self.compare(options=Options(deadline=10))
        self.assertEqual(report["verdict"], "PASS", report)
        self.assertEqual(report["coverage"], "full")
        self.assertIsNone(report["termination"])
        for role in ("source", "output"):
            self.assertEqual(report["progress"][role]["completed_streams"], [0, 1])
            self.assertEqual(report["progress"][role]["pending_streams"], [])

    def test_audio_only_rejected_by_default(self):
        report = self.compare("audio_only")
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(checks(report, "video_stream_count")[0]["actual"], 0)

    def test_explicit_video_drop_passes(self):
        self.assertEqual(self.compare("audio_only", Options(allow_drop=("video",)))["verdict"], "PASS")

    def test_audio_drop_requires_explicit_policy(self):
        out = self.root / "video-only.mkv"
        ffmpeg(["-i", self.files["healthy"], "-map", "0:v", "-c", "copy", out])
        self.assertEqual(compare(self.files["source"], out)["verdict"], "FAIL")
        self.assertEqual(compare(self.files["source"], out, Options(allow_drop=("audio",)))["verdict"], "PASS")

    def test_truncated_rejected(self):
        report = self.compare("truncated")
        self.assertEqual(report["verdict"], "FAIL", report)

    def test_shortened_duration_rejected(self):
        report = self.compare("shortened")
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(checks(report, "container_duration")[0]["status"], "FAIL")

    def test_per_stream_duration_catches_short_audio_under_full_video(self):
        report = self.compare("short_audio")
        self.assertEqual(checks(report, "container_duration")[0]["status"], "PASS")
        self.assertEqual(checks(report, "audio_decoded_duration")[0]["status"], "FAIL")
        self.assertEqual(report["verdict"], "FAIL")

    def test_successful_encoder_and_video_header_do_not_pass_empty_video(self):
        report = self.compare("zero_video")
        self.assertEqual(checks(report, "video_stream_count")[0]["status"], "PASS")
        self.assertEqual(checks(report, "container_duration")[0]["status"], "PASS")
        self.assertEqual(report["verdict"], "FAIL")
        self.assertTrue(any(c["status"] == "FAIL" and c["name"].startswith("video_decode") for c in report["checks"]))

    def test_missing_header_duration_is_unknown_even_with_full_decoded_data(self):
        out = self.root / "live-no-duration.mkv"
        ffmpeg(["-i", self.files["healthy"], "-map", "0", "-c", "copy", "-map_metadata", "-1", "-live", "1", out])
        report = compare(self.files["source"], out)
        self.assertNotIn("duration", report["media"]["output"]["format"])
        self.assertEqual(report["verdict"], "UNKNOWN", report)
        self.assertEqual(checks(report, "container_duration")[0]["status"], "UNKNOWN")
        self.assertTrue(all(c["status"] == "PASS" for c in checks(report, "video_decoded_frames")))

    def test_subtitles_only_checked_when_explicit(self):
        subtitle = self.root / "test.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic test\n", encoding="utf-8")
        src = self.root / "with-subs.mkv"
        ffmpeg(["-i", self.files["source"], "-i", subtitle, "-map", "0", "-map", "1", "-c", "copy", src])
        self.assertEqual(compare(src, self.files["healthy"])["verdict"], "PASS")
        report = compare(src, self.files["healthy"], Options(subtitles="preserve"))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(checks(report, "subtitle_stream_count")[0]["status"], "FAIL")
        dst = self.root / "with-subs-copy.mkv"
        shutil.copyfile(src, dst)
        self.assertEqual(compare(src, dst, Options(subtitles="preserve"))["verdict"], "PASS")

    def test_multiple_audio_streams_each_decode_and_check_duration(self):
        src = self.root / "two-audio.mkv"
        out = self.root / "two-audio-short.mkv"
        ffmpeg(["-i", self.files["source"], "-map", "0:v", "-map", "0:a", "-map", "0:a", "-c", "copy", src])
        ffmpeg(["-i", src, "-map", "0", "-filter:a:1", "atrim=duration=0.5", "-c:v", "copy", "-c:a", "flac", "-threads", "1", out])
        report = compare(src, out, Options(stream_map=((1, 1), (2, 2), (0, 0))))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(len(checks(report, "audio_decoded_frames")), 4)
        durations = checks(report, "audio_decoded_duration")
        self.assertEqual([c["status"] for c in durations], ["PASS", "FAIL"])

    def test_allow_drop_never_allows_extra_streams(self):
        out = self.root / "extra-audio.mkv"
        ffmpeg(["-i", self.files["healthy"], "-map", "0", "-map", "0:a", "-c", "copy", out])
        report = compare(self.files["source"], out, Options(allow_drop=("audio",)))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertEqual(checks(report, "audio_stream_count")[0]["status"], "FAIL")

    def test_quick_labels_partial_and_limits_decoded_frames(self):
        full, quick = self.compare(), self.compare(options=Options(quick_seconds=0.25))
        self.assertEqual(quick["verdict"], "PASS", quick)
        self.assertEqual(quick["coverage"], "partial")
        self.assertLess(quick["decoded"]["output"][0]["frames"], full["decoded"]["output"][0]["frames"])

    def test_audio_only_source_is_supported(self):
        src, dst = self.root / "tone.wav", self.root / "tone.flac"
        ffmpeg(["-i", self.files["source"], "-map", "0:a", "-c:a", "pcm_s16le", "-threads", "1", src])
        ffmpeg(["-i", src, "-c:a", "flac", "-threads", "1", dst])
        self.assertEqual(compare(src, dst)["verdict"], "PASS")

    def test_mp4_transcode_is_supported(self):
        out = self.root / "healthy.mp4"
        ffmpeg(["-i", self.files["source"], "-c:v", "mpeg4", "-c:a", "aac", "-threads", "1", out])
        self.assertEqual(compare(self.files["source"], out)["verdict"], "PASS")

    def test_unreadable_or_missing_path(self):
        self.assertEqual(compare(self.root / "missing", self.files["healthy"])["verdict"], "UNKNOWN")
        self.assertEqual(compare(self.root, self.files["healthy"])["verdict"], "UNKNOWN")

    def test_same_file_and_aliases_rejected(self):
        src = self.files["source"]
        for name, link in (("source-hard", os.link), ("source-sym", os.symlink)):
            alias = self.root / name
            link(src, alias)
            self.assertEqual(compare(src, alias)["verdict"], "UNKNOWN")
        self.assertEqual(compare(src, src)["verdict"], "UNKNOWN")

    def test_corrupt_and_unsupported_input_nonzero(self):
        invalid = self.root / "invalid.bin"
        invalid.write_bytes(b"not media\x00" * 50)
        self.assertEqual(compare(self.files["source"], invalid)["verdict"], "FAIL")
        subtitle = self.root / "standalone.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nUnsupported container\n")
        self.assertEqual(compare(self.files["source"], subtitle)["verdict"], "FAIL")
        self.assertEqual(compare("https://example.invalid/media.mp4", invalid)["verdict"], "UNKNOWN")

    def test_shell_characters_in_path_are_literal(self):
        out = self.root / "literal ;$(touch SHOULD_NOT_EXIST).mkv"
        shutil.copyfile(self.files["healthy"], out)
        self.assertEqual(compare(self.files["source"], out)["verdict"], "PASS")
        self.assertFalse((ROOT / "SHOULD_NOT_EXIST").exists())

    def test_media_never_modified_and_report_atomic(self):
        before = {name: digest(p) for name, p in self.files.items()}
        report = self.compare()
        dest = self.root / "report.json"
        dest.write_text("old report")
        write_report(report, dest, self.files["source"], self.files["healthy"])
        self.assertEqual(json.loads(dest.read_text()), json.loads(json.dumps(report)))
        self.assertEqual(before, {name: digest(p) for name, p in self.files.items()})
        self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_report_refuses_media_alias_and_symlink(self):
        src, out = self.files["source"], self.files["healthy"]
        report = self.compare()
        hard, symmedia = self.root / "report-source-hard", self.root / "report-source-sym"
        os.link(src, hard)
        os.symlink(src, symmedia)
        for target in (src, out, hard, symmedia):
            with self.subTest(target=target), self.assertRaises(ValueError):
                write_report(report, target, src, out)
        sym = self.root / "report-link.json"
        sym.symlink_to(self.root / "elsewhere.json")
        with self.assertRaises(ValueError):
            write_report(report, sym, src, out)

    def test_cli_reports_and_exit_codes(self):
        for case, code in (("healthy", 0), ("audio_only", 1)):
            dest = self.root / f"{case}-report.json"
            proc = subprocess.run([sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                                   str(self.files[case]), "--report", str(dest)], capture_output=True, text=True,
                                  timeout=120, cwd=ROOT)
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), json.loads(dest.read_text()))
        proc = subprocess.run([sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                               str(self.files["healthy"]), "--report", str(self.files["source"])],
                              capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["checks"][-1]["name"], "report_write")

    def test_missing_executable_is_unknown(self):
        report = self.compare(options=Options(ffmpeg=str(self.root / "no-ffmpeg")))
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(exit_code(report), 2)

    def test_hung_executable_is_bounded_unknown(self):
        stub = self.root / "hung-probe"
        stub.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        stub.chmod(0o755)
        start = time.monotonic()
        report = self.compare(options=Options(ffprobe=str(stub), timeout=0.15))
        # Generous bound: slow CI runners (notably macOS) take longer to start and reap.
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertTrue(report["commands"][0]["timed_out"])

    def test_timeout_during_actual_decode_is_unknown(self):
        stub = self.root / "hung-ffmpeg"
        stub.write_text(f"#!{sys.executable}\nimport sys, time\nif '-version' in sys.argv: print('fake timeout fixture')\nelse: time.sleep(60)\n")
        stub.chmod(0o755)
        # The stub's -version reply must beat the per-process timeout even on slow runners.
        report = self.compare(options=Options(ffmpeg=str(stub), timeout=3))
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(report["coverage"], "partial")
        self.assertTrue(any(cmd.get("timed_out") and "-map" in cmd["argv"] for cmd in report["commands"]))

    def test_unreadable_file_is_unknown(self):
        blocked = self.root / "unreadable.mkv"
        shutil.copyfile(self.files["healthy"], blocked)
        blocked.chmod(0)
        try:
            self.assertEqual(compare(self.files["source"], blocked)["verdict"], "UNKNOWN")
        finally:
            blocked.chmod(0o600)

    def test_cli_invalid_numeric_options_are_json_unknown(self):
        proc = subprocess.run([sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                               str(self.files["healthy"]), "--timeout", "nan"],
                              capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["verdict"], "UNKNOWN")

    def test_quick_handles_nonzero_start_timestamp(self):
        src, dst = self.root / "offset-src.mkv", self.root / "offset-dst.mkv"
        ffmpeg(["-i", self.files["healthy"], "-map", "0", "-c", "copy", "-output_ts_offset", "10", src])
        shutil.copyfile(src, dst)
        report = compare(src, dst, Options(quick_seconds=0.25))
        self.assertEqual(report["verdict"], "PASS", report)

    def test_invalid_options_return_unknown(self):
        for opt in (Options(timeout=0), Options(timeout=float("inf")), Options(quick_seconds=-1),
                    Options(absolute_tolerance=-1), Options(relative_tolerance=float("nan")),
                    Options(allow_drop=("subtitle",)), Options(subtitles="unknown")):
            with self.subTest(opt=opt):
                self.assertEqual(self.compare(options=opt)["verdict"], "UNKNOWN")


class FrameFactsTests(unittest.TestCase):
    def test_empty_frames_are_zero(self):
        facts = _frame_facts(io.BytesIO(b"#format: frame checksums\n#tb 0: 1/12\n"))
        self.assertEqual(facts["frames"], 0)
        self.assertIsNone(facts["duration_seconds"])

    def test_invalid_facts_never_invent_duration(self):
        for data in (b"0,0,0,1,2,abc\n", b"#tb 0: 1/0\n", b"#tb 0: 1/12\n0,0,0,0,2,abc\n"):
            with self.subTest(data=data), self.assertRaises(CheckError):
                _frame_facts(io.BytesIO(data))

    def test_unexpected_record_stream_identifiers_reject_all_facts(self):
        for identifier in ("1", "-1", "not-a-stream", "", "0.0", "0_0", "٠", "+0"):
            for prefix in ("", "0,0,0,1,4,00000000\n"):
                data = f"#tb 0: 1/1\n{prefix}{identifier},0,0,1,4,00000000\n".encode()
                with self.subTest(identifier=identifier, prefix=prefix):
                    with self.assertRaises(CheckError) as caught:
                        _frame_facts(io.BytesIO(data))
                    self.assertEqual(caught.exception.status, "UNKNOWN")
                    diagnostic = "unexpected framehash stream identifier" if identifier.isascii() else "ascii"
                    self.assertIn(diagnostic, str(caught.exception))
        for identifier in ("0", "00", " 0 "):
            facts = _frame_facts(io.BytesIO(f"#tb 0: 1/1\n{identifier},0,0,1,4,00000000\n".encode()))
            self.assertEqual(facts, dict(frames=1, decoded_bytes=4, start_seconds=0.0,
                                         end_seconds=1.0, duration_seconds=1.0))

    def test_dts_requires_signed_ascii_integer_without_ordering_constraint(self):
        for dts in ("", " ", "not-a-timestamp", "1.0", "1_0", "1e2", "+", "-", "--1", "0x1"):
            for prefix in ("", "0,0,0,1,4,00000000\n"):
                with self.subTest(dts=dts, prefix=prefix):
                    data = f"#tb 0: 1/1\n{prefix}0,{dts},0,1,4,00000000\n".encode()
                    with self.assertRaises(CheckError) as caught:
                        _frame_facts(io.BytesIO(data))
                    self.assertEqual(caught.exception.status, "UNKNOWN")
                    self.assertIn("malformed framehash DTS", str(caught.exception))
        for dts in ("0", "+0", "-0", "001", " +42 ", "-42", str(10**400), str(-10**400)):
            with self.subTest(dts=dts):
                # DTS may precede/follow PTS and decrease across records.
                data = f"#tb 0: 1/1\n0,{dts},0,1,4,00000000\n0,-1,1,1,4,00000000\n".encode()
                self.assertEqual(_frame_facts(io.BytesIO(data)), dict(frames=2, decoded_bytes=8,
                    start_seconds=0.0, end_seconds=2.0, duration_seconds=2.0))

    def test_timebase_declarations_must_be_consistent_and_positive(self):
        record = "0,0,0,1,4,00000000\n"
        for between, suffix in (("", record), (record, record), (record, ""), ("", "")):
            data = f"#tb 0: 1/1\n{between}#tb 0: 1/2\n{suffix}".encode()
            with self.subTest(between=between, suffix=suffix):
                with self.assertRaises(CheckError) as caught:
                    _frame_facts(io.BytesIO(data))
                self.assertEqual(caught.exception.status, "UNKNOWN")
                self.assertIn("conflicting framehash timebase", str(caught.exception))
        for invalid in ("0/1", "-1/2"):
            for suffix in ("", "#tb 0: 1/1\n" + record):
                with self.subTest(invalid=invalid, suffix=suffix):
                    with self.assertRaises(CheckError) as caught:
                        _frame_facts(io.BytesIO(f"#tb 0: {invalid}\n{suffix}".encode()))
                    self.assertEqual(caught.exception.status, "UNKNOWN")
                    self.assertIn("nonpositive framehash timebase", str(caught.exception))

    def test_equivalent_timebase_declarations_keep_exact_facts(self):
        record = "0,0,0,1,4,00000000\n"
        for second in ("1/1", "2/2", f"{10**400}/{10**400}"):
            for between, suffix in (("", record), (record, "")):
                with self.subTest(second=second, between=between):
                    facts = _frame_facts(io.BytesIO(f"#tb 0: 1/1\n{between}#tb 0: {second}\n{suffix}".encode()))
                    self.assertEqual(facts, dict(frames=1, decoded_bytes=4, start_seconds=0.0,
                                                 end_seconds=1.0, duration_seconds=1.0))
        facts = _frame_facts(io.BytesIO(b"#tb 0: 1/2\n#tb 0: 2/4\n"))
        self.assertEqual(facts, dict(frames=0, decoded_bytes=0, start_seconds=None,
                                     end_seconds=None, duration_seconds=None))

    def test_positive_span_float_rounding_boundary(self):
        # Exact subtraction must precede float conversion, even at huge offsets.
        for pts, denominator in ((0, 10**400), (0, 2**1075), (10**400, 10**400)):
            data = f"#tb 0: 1/{denominator}\n0,0,{pts},1,4,00000000\n".encode()
            with self.subTest(pts=pts, denominator=denominator):
                with self.assertRaises(CheckError) as caught:
                    _frame_facts(io.BytesIO(data))
                self.assertEqual(caught.exception.status, "UNKNOWN")
                self.assertIn("underflows to zero", str(caught.exception))
        # Just above half the smallest subnormal rounds to a positive float.
        facts = _frame_facts(io.BytesIO(f"#tb 0: 1/{2**1075 - 1}\n0,0,0,1,4,00000000\n".encode()))
        self.assertEqual(facts["duration_seconds"], 2**-1074)
        # Endpoints can round equal while the independently converted span survives.
        facts = _frame_facts(io.BytesIO(f"#tb 0: 1/{10**20}\n0,0,{10**20},1,4,00000000\n".encode()))
        self.assertEqual((facts["start_seconds"], facts["end_seconds"]), (1.0, 1.0))
        self.assertEqual(facts["duration_seconds"], 1e-20)

    def test_timestamp_span_and_bytes_measured(self):
        facts = _frame_facts(io.BytesIO(b"#tb 0: 1/12\n0,0,120,1,100,abcd\n0,1,121,1,100,abcd\n"))
        self.assertEqual(facts["frames"], 2)
        self.assertEqual(facts["decoded_bytes"], 200)
        self.assertAlmostEqual(facts["duration_seconds"], 2/12)
        self.assertEqual(facts["start_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
