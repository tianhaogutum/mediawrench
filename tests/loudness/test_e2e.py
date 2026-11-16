"""Serial integration tests using real external FFmpeg and synthetic local audio.

Missing FFmpeg skips the suite; it is never mocked into a passing result.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples.loudness.synthetic import write_tone
from mediawrench.loudness import Profile, Toolchain, normalize, verify_candidate
from mediawrench.loudness.core import AudioError, Runner, sha256
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class RealAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = Toolchain(FFMPEG, FFPROBE)
        cls.temp_root = ROOT / ".tools/test-tmp"
        cls.temp_root.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix="loudnorm-fixtures-", dir=cls.temp_root)
        cls.fixture_root = Path(cls.fixtures.name)
        cls.tone = cls.fixture_root / "modulated.wav"
        cls.silence = cls.fixture_root / "silence.wav"
        cls.short = cls.fixture_root / "short.wav"
        cls.clipped = cls.fixture_root / "clipped.wav"
        cls.dynamic = cls.fixture_root / "dynamic.wav"
        write_tone(cls.tone)
        write_tone(cls.silence, 4, kind="silence")
        write_tone(cls.short, 0.1)
        write_tone(cls.clipped, kind="clipped")
        write_tone(cls.dynamic, 24, kind="dynamic")
        cls.original_hash = sha256(cls.tone)
        cls.profile = Profile(-16, -1.5, 0.5)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="loudnorm-test-", dir=self.temp_root)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.output = self.root / "output.wav"
        self.report = self.root / "report.json"

    def process(self, source=None, profile=None, tools=None):
        result = normalize(source or self.tone, self.output, profile or self.profile,
                           self.report, tools or self.tools)
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertFalse(list(self.root.glob(".loudnorm-*")), "staging must be cleaned")
        self.assertEqual(sha256(self.tone), self.original_hash, "source was modified")
        return result

    def encode(self, source, output, extra):
        return subprocess.run([self.tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
                               "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
                               "-i", str(source), *extra, "-threads", "1", "-n", str(output)],
                              capture_output=True, text=True, check=True, timeout=120)

    def test_modulated_tone_final_measurement_matches_separate_ffmpeg(self):
        result = self.process()
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["published"])
        self.assertEqual(result["normalization_type"], "linear")
        self.assertEqual(result["candidate_sha256"], sha256(self.output))
        self.assertEqual(result["output_media"]["codec"], "pcm_s24le")
        self.assertEqual(result["output_media"]["sample_rate"], 48000)
        separate = subprocess.run([self.tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats",
                                   "-threads", "1", "-filter_threads", "1", "-i", str(self.output),
                                   "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                                   "-threads", "1", "-f", "null", "-"],
                                  capture_output=True, text=True, check=True, timeout=120)
        # Independent parsing and separate command; compare input statistics, never output_i.
        measured = json.loads("{" + separate.stderr.split("{", 1)[1].split("}", 1)[0] + "}")
        for key in ("input_i", "input_tp", "input_lra"):
            self.assertEqual(measured[key], result["output_measurement"][key])
        self.assertLessEqual(abs(float(measured["input_i"]) + 16), 0.5)
        self.assertLessEqual(float(measured["input_tp"]), -1.5)

    def test_silence_nonfinite_report_and_no_output(self):
        result = self.process(self.silence)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertEqual(result["input_measurement"]["input_i"], "-inf")
        self.assertFalse(self.output.exists())

    def test_short_clip_explicitly_unsupported(self):
        result = self.process(self.short)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertIn("shorter than 3s", result["error"])
        self.assertFalse(self.output.exists())

    def test_clipping_input_processed_without_claiming_restoration(self):
        result = self.process(self.clipped)
        self.assertEqual(result["status"], "PASS", result)
        self.assertGreaterEqual(float(result["input_measurement"]["input_tp"]), 0)
        self.assertTrue(result["checks"]["peak_pass"])

    def test_already_at_target(self):
        first = self.process()
        result = normalize(self.output, self.root / "second.wav", self.profile,
                           self.root / "second.json", self.tools)
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(result["status"], "PASS", result)
        self.assertAlmostEqual(float(result["input_measurement"]["input_i"]), -16, delta=0.5)

    def test_dynamic_fallback_is_actual_reported_mode(self):
        result = self.process(self.dynamic, Profile(-16, -1.5, 0.5, 1))
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["normalization_type"], "dynamic")
        self.assertTrue(result["dynamic_fallback"])
        self.assertEqual(result["processing_measurement"]["normalization_type"], "dynamic")

    def test_dynamic_fallback_rejected_at_stricter_tolerance(self):
        # How far dynamic mode lands from the target depends on the FFmpeg build (-15.64
        # LUFS with FFmpeg 7.0.2), so derive the stricter tolerance from this build's own
        # final-file measurement: the gate must reject anything tighter than that miss.
        loose = self.process(self.dynamic, Profile(-16, -1.5, 0.5, 1))
        self.assertEqual(loose["status"], "PASS", loose)
        self.assertEqual(loose["normalization_type"], "dynamic")
        miss = loose["checks"]["lufs_error"]
        if miss < 0.02:
            self.skipTest(f"dynamic mode landed within {miss} LU of the target with this FFmpeg build")
        self.output.unlink()
        self.report.unlink()
        result = self.process(self.dynamic, Profile(-16, -1.5, round(miss / 2, 2), 1))
        self.assertEqual(result["status"], "FAIL", result)
        self.assertFalse(result["checks"]["lufs_pass"])
        self.assertFalse(result["published"])
        self.assertFalse(self.output.exists())

    def test_strict_rounding_tolerance_rejected_without_output(self):
        result = self.process(profile=Profile(-16.123, -1.5, 0.001))
        self.assertEqual(result["status"], "FAIL", result)
        self.assertFalse(self.output.exists())
        self.assertIn("output_measurement", result)

    def test_deliberately_nonconforming_real_encode_rejected(self):
        self.assertEqual(self.process()["status"], "PASS")
        bad = self.root / "plus12.wav"
        self.encode(self.output, bad, ["-af", "volume=12dB", "-c:a", "pcm_s24le"])
        before = sha256(bad)
        verified = verify_candidate(bad, self.profile, self.tools)
        self.assertEqual(verified["status"], "FAIL")
        self.assertFalse(verified["checks"]["lufs_pass"])
        self.assertFalse(verified["checks"]["peak_pass"])
        self.assertEqual(sha256(bad), before)

    def test_flac_input(self):
        flac = self.root / "input.flac"
        self.encode(self.tone, flac, ["-c:a", "flac"])
        result = self.process(flac)
        self.assertEqual(result["status"], "PASS", result)

    def test_stereo_input_retains_channels_and_resamples(self):
        stereo = self.root / "stereo.wav"
        write_tone(stereo, channels=2, rate=44100)
        result = self.process(stereo)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["output_media"]["channels"], 2)
        self.assertEqual(result["output_media"]["sample_rate"], 48000)

    def test_output_collision_preserves_existing_bytes(self):
        self.output.write_bytes(b"existing output")
        result = self.process()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(self.output.read_bytes(), b"existing output")
        self.assertEqual(result["commands"], [])

    def test_report_collision_preserves_existing_bytes_and_no_output(self):
        self.report.write_bytes(b"existing report")
        result = normalize(self.tone, self.output, self.profile, self.report, self.tools)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("report_error", result)
        self.assertEqual(self.report.read_bytes(), b"existing report")
        self.assertFalse(self.output.exists())

    def test_aliases_never_overwrite_source(self):
        alias = self.root / "alias.wav"
        alias.symlink_to(self.tone)
        result = normalize(self.tone, alias, self.profile, self.report, self.tools)
        self.assertEqual(result["status"], "ERROR")
        result = normalize(self.tone, self.output, self.profile, alias, self.tools)
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(self.output.exists())
        self.assertEqual(sha256(self.tone), self.original_hash)

    def test_missing_ffmpeg_and_ffprobe_explicit_failures(self):
        for missing in ("ffmpeg", "ffprobe"):
            with self.subTest(missing=missing):
                tools = Toolchain("/no-such/ffmpeg" if missing == "ffmpeg" else self.tools.ffmpeg,
                                  "/no-such/ffprobe" if missing == "ffprobe" else self.tools.ffprobe)
                result = self.process(tools=tools)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn("Executable not found", result["error"])
                self.assertFalse(self.output.exists())
                self.report.unlink()

    def test_subprocess_timeout_real_tool(self):
        result = self.process(tools=Toolchain(self.tools.ffmpeg, self.tools.ffprobe, 0.000001))
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("timed out", result["error"])
        self.assertFalse(self.output.exists())

    def test_invalid_media_and_truncated_audio_fail(self):
        bad = self.root / "bad.wav"
        bad.write_bytes(b"not media")
        result = self.process(bad)
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(self.output.exists())
        self.report.unlink()
        # Odd byte count: probe accepts WAV, strict decoding must fail on partial PCM packet.
        bad.write_bytes(self.tone.read_bytes()[:-1])
        result = self.process(bad)
        self.assertEqual(result["status"], "ERROR", result)
        self.assertFalse(self.output.exists())

    def test_multiple_streams_and_video_are_rejected(self):
        multiple = self.root / "multiple.mkv"
        self.encode(self.tone, multiple, ["-map", "0:a", "-map", "0:a", "-c:a", "pcm_s16le"])
        result = self.process(multiple)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertFalse(self.output.exists())
        self.report.unlink()
        video = self.root / "video.mkv"
        subprocess.run([self.tools.ffmpeg, "-v", "error", "-nostdin", "-threads", "1",
                        "-filter_threads", "1", "-f", "lavfi", "-i", "color=size=16x16:rate=1",
                        "-t", "4", "-c:v", "ffv1", "-threads", "1", "-n", str(video)],
                       capture_output=True, check=True, timeout=120)
        result = self.process(video)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertFalse(self.output.exists())

    def test_multichannel_and_compressed_wav_are_rejected(self):
        surround = self.root / "surround.wav"
        write_tone(surround, 4, channels=6)
        result = self.process(surround)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.report.unlink()
        compressed = self.root / "alaw.wav"
        self.encode(self.tone, compressed, ["-c:a", "pcm_alaw"])
        result = self.process(compressed)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertFalse(self.output.exists())

    def test_cli_pass_and_unsupported_exit_codes(self):
        argv = [sys.executable, "-m", "mediawrench.loudness", str(self.tone), str(self.output),
                "--target-i", "-16", "--target-tp", "-1.5", "--tolerance", "0.5",
                "--report", str(self.report), "--ffmpeg", self.tools.ffmpeg, "--ffprobe", self.tools.ffprobe]
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")
        self.output.unlink()
        self.report.unlink()
        argv[3] = str(self.silence)
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "UNSUPPORTED")
        self.assertFalse(self.output.exists())

    def test_cli_verification_failure_and_error_exit_codes(self):
        argv = [sys.executable, "-m", "mediawrench.loudness", str(self.tone), str(self.output),
                "--target-i", "-16.123", "--target-tp", "-1.5", "--tolerance", "0.001",
                "--report", str(self.report), "--ffmpeg", self.tools.ffmpeg, "--ffprobe", self.tools.ffprobe]
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "FAIL")
        self.assertTrue(self.report.exists())
        self.assertFalse(self.output.exists())
        self.report.unlink()
        argv[argv.index("--target-i") + 1] = "-4"
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ERROR")
        self.assertFalse(self.output.exists())

    def test_unicode_space_and_shell_metacharacter_paths(self):
        source = self.root / "音频 ; $(touch sentinel).wav"
        shutil.copyfile(self.tone, source)
        self.output = self.root / "输出 final.wav"
        self.report = self.root / "报告 final.json"
        result = self.process(source)
        self.assertEqual(result["status"], "PASS", result)
        self.assertFalse((self.root / "sentinel").exists())

    def test_missing_input_wrong_extension_and_parent_fail_without_execution(self):
        cases = [(self.root / "missing.wav", self.output),
                 (self.tone, self.root / "wrong.mp3"),
                 (self.tone, self.root / "missing" / "output.wav")]
        for source, output in cases:
            with self.subTest(source=source, output=output):
                result = normalize(source, output, self.profile, self.report, self.tools)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["commands"], [])
                self.assertFalse(output.exists())
                self.assertTrue(self.report.exists())
                self.report.unlink()

    # The following tests inject failures/races after real measurement/encoding. They verify
    # transaction behavior; they are not counted as wholly real-media E2E cases.
    def test_injected_second_pass_failure_cleans_stage_and_retains_report(self):
        original = Runner.run
        def fail_encode(runner, argv):
            if "pcm_s24le" in argv:
                raise AudioError("Injected encoding failure")
            return original(runner, argv)
        with patch.object(Runner, "run", fail_encode):
            result = self.process()
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("Injected encoding failure", result["error"])
        self.assertFalse(self.output.exists())

    def test_publication_race_never_clobbers_competing_output(self):
        real_link = os.link
        def race_link(source, destination, *args, **kwargs):
            if Path(destination) == self.output:
                self.output.write_bytes(b"concurrent writer")
            return real_link(source, destination, *args, **kwargs)
        with patch("mediawrench.loudness.core.os.link", race_link):
            result = self.process()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["errno"], errno.EEXIST)
        self.assertEqual(self.output.read_bytes(), b"concurrent writer")

    def test_native_audio_publication_permission_error_retains_errno(self):
        real_link = os.link
        failure = PermissionError(errno.EACCES, "Injected audio publication denial", str(self.output))
        def fail_audio(source, destination, *args, **kwargs):
            if Path(destination) == self.output:
                raise failure
            return real_link(source, destination, *args, **kwargs)
        with patch("mediawrench.loudness.core.os.link", fail_audio):
            result = self.process()
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["published"])
        self.assertEqual(result["failed_stage"], "publication")
        self.assertEqual(result["errno"], errno.EACCES)
        self.assertEqual(result["error"], str(failure))
        self.assertNotIn("error_kind", result)
        self.assertFalse(self.output.exists())

    def test_report_publication_failure_rolls_back_verified_output(self):
        real_link = os.link
        primary = OSError(errno.ENOSPC, "Injected initial report failure", str(self.report))
        secondary = PermissionError(errno.EACCES, "Injected diagnostic failure", str(self.report))
        attempts = []
        def fail_report(source, destination, *args, **kwargs):
            if Path(destination) == self.report:
                attempts.append(Path(source))
                raise primary if len(attempts) == 1 else secondary
            return real_link(source, destination, *args, **kwargs)
        with patch("mediawrench.loudness.core.os.link", fail_report):
            result = normalize(self.tone, self.output, self.profile, self.report, self.tools)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["errno"], errno.ENOSPC)
        self.assertEqual(result["error"], str(primary))
        self.assertEqual(result["report_errno"], errno.EACCES)
        self.assertEqual(result["report_error"], str(secondary))
        self.assertEqual(result["failed_stage"], "publication")
        self.assertEqual(len(attempts), 2)
        self.assertFalse(any(path.exists() for path in attempts))
        self.assertFalse(result["published"])
        self.assertNotIn("error_kind", result)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        self.assertFalse(list(self.root.glob(".loudnorm-*")))
        self.assertEqual(sha256(self.tone), self.original_hash)


if __name__ == "__main__":
    unittest.main()
