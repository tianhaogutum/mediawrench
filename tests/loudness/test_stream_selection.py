"""Real pinned-FFmpeg Matroska selection, signal identity and publication gates."""
from array import array
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.loudness import Profile, Toolchain, normalize, verify_candidate
from mediawrench.loudness.core import sha256
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class StreamSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = Toolchain(FFMPEG, FFPROBE)
        cls.profile = Profile(-16, -1.5, 0.5)
        scratch = ROOT / ".tools/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix="streams-fixture-", dir=scratch)
        cls.source = Path(cls.fixtures.name) / "two-tracks.mkv"
        # Absolute indices: 0=video (10s), 1=440Hz mono (6s), 2=880Hz stereo (8s).
        # The second audio track is default; neither default nor container duration is an oracle.
        subprocess.run([cls.tools.ffmpeg, "-v", "error", "-nostdin", "-threads", "1",
                        "-filter_threads", "1", "-filter_complex_threads", "1",
                        "-f", "lavfi", "-i", "color=size=16x16:rate=1:duration=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=48000",
                        "-f", "lavfi", "-i", "sine=frequency=880:duration=8:sample_rate=48000",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1",
                        "-c:a", "pcm_s16le", "-c:a:1", "flac", "-ac:a:1", "2", "-disposition:a:0", "0",
                        "-disposition:a:1", "default", "-threads", "1", "-n", str(cls.source)],
                       capture_output=True, check=True, timeout=120)
        cls.source_hash = sha256(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=Path(self.fixtures.name))
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def cli(self, name, *args):
        return subprocess.run([sys.executable, "-m", "mediawrench.loudness", str(self.source),
                               str(self.root / f"{name}.wav"), "--report", str(self.root / f"{name}.json"),
                               "--target-i", "-16", "--target-tp", "-1.5", "--tolerance", "0.5",
                               "--ffmpeg", self.tools.ffmpeg, "--ffprobe", self.tools.ffprobe, *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_selected_tracks_have_correct_signal_layout_duration_and_final_gate(self):
        for index, frequency, channels, seconds in ((1, 440, 1, 6), (2, 880, 2, 8)):
            with self.subTest(index=index):
                run = self.cli(str(index), "--audio-stream", str(index))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                report = json.loads(run.stdout)
                output = self.root / f"{index}.wav"
                self.assertEqual(json.loads((self.root / f"{index}.json").read_text()), report)
                self.assertEqual(report["input_media"]["stream_index"], index)
                self.assertEqual(report["input_media"]["codec"], "pcm_s16le" if index == 1 else "flac")
                self.assertEqual(report["input_media"]["channel_layout"], "mono" if channels == 1 else "stereo")
                self.assertEqual(report["input_media"]["duration_seconds"], seconds)
                self.assertTrue(all(report["checks"][key] for key in
                                    ("lufs_pass", "peak_pass", "duration_pass", "channels_pass")))
                self.assertEqual(verify_candidate(output, self.profile, self.tools)["status"], "PASS")
                media = json.loads(subprocess.run([self.tools.ffprobe, "-v", "error", "-show_streams",
                                                   "-of", "json", str(output)], capture_output=True,
                                                  text=True, check=True, timeout=120).stdout)["streams"]
                self.assertEqual(len(media), 1, "video must not be copied")
                self.assertEqual((media[0]["codec_name"], media[0]["channels"], media[0]["sample_rate"]),
                                 ("pcm_s24le", channels, "48000"))
                # Independent signal oracle, computed from actual decoded output samples.
                pcm = subprocess.run([self.tools.ffmpeg, "-v", "error", "-i", str(output),
                                      "-t", "1", "-ac", "1", "-f", "f64le", "-"],
                                     capture_output=True, check=True, timeout=120).stdout
                samples = array("d")
                samples.frombytes(pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                def amplitude(hz):
                    real = sum(x * math.cos(2 * math.pi * hz * n / 48000) for n, x in enumerate(samples))
                    imag = sum(x * math.sin(2 * math.pi * hz * n / 48000) for n, x in enumerate(samples))
                    return math.hypot(real, imag)
                self.assertGreater(amplitude(frequency), 100 * amplitude(1320 - frequency))
                for argv in report["commands"]:
                    if "-i" in argv or "-show_streams" in argv:
                        self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
                        self.assertEqual(argv[argv.index("-format_whitelist") + 1], "wav,flac,matroska")
        self.assertEqual(sha256(self.source), self.source_hash)
        self.assertFalse(list(self.root.glob(".loudnorm-*")))

    def test_missing_video_and_nonexistent_selection_publish_no_audio(self):
        for name, args, error in (("missing", (), "Multiple audio streams"),
                                  ("video", ("--audio-stream", "0"), "not an audio stream"),
                                  ("absent", ("--audio-stream", "99"), "does not exist")):
            with self.subTest(name=name):
                run = self.cli(name, *args)
                self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
                report = json.loads(run.stdout)
                self.assertIn(error, report["error"])
                self.assertFalse(report["published"])
                self.assertFalse((self.root / f"{name}.wav").exists())
                self.assertEqual(json.loads((self.root / f"{name}.json").read_text()), report)
                self.assertFalse(any("-af" in argv for argv in report["commands"]))

    def test_repeated_or_nonnumeric_cli_selection_is_usage_error(self):
        for args in (("--audio-stream", "1", "--audio-stream", "2"), ("--audio-stream", "a:0")):
            with self.subTest(args=args):
                run = self.cli("invalid", *args)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertFalse((self.root / "invalid.wav").exists())

    def test_selected_track_still_fails_strict_final_loudness_gate(self):
        report = normalize(self.source, self.root / "strict.wav", Profile(-16.123, -1.5, 0.001),
                           self.root / "strict.json", self.tools, audio_stream=2)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertFalse(report["checks"]["lufs_pass"])
        self.assertIn("output_measurement", report)
        self.assertFalse((self.root / "strict.wav").exists())

    def test_single_audio_track_with_video_needs_no_selection(self):
        single = self.root / "single.mkv"
        subprocess.run([self.tools.ffmpeg, "-v", "error", "-i", str(self.source), "-map", "0:0",
                        "-map", "0:1", "-c", "copy", "-n", str(single)],
                       capture_output=True, check=True, timeout=120)
        report = normalize(single, self.root / "single.wav", self.profile,
                           self.root / "single.json", self.tools)
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["input_media"]["stream_index"], 1)
        self.assertEqual(report["input_media"]["duration_seconds"], 6)

    def test_short_selected_audio_is_not_hidden_by_longer_video(self):
        short = self.root / "short.mkv"
        subprocess.run([self.tools.ffmpeg, "-v", "error", "-i", str(self.source), "-map", "0:0",
                        "-map", "0:1", "-c:v", "copy", "-af", "atrim=duration=2",
                        "-c:a", "pcm_s16le", "-threads", "1", "-n", str(short)],
                       capture_output=True, check=True, timeout=120)
        report = normalize(short, self.root / "short.wav", self.profile,
                           self.root / "short.json", self.tools, audio_stream=1)
        self.assertEqual(report["status"], "UNSUPPORTED", report)
        self.assertIn("shorter than 3s", report["error"])
        self.assertFalse((self.root / "short.wav").exists())
        self.assertFalse(list(self.root.glob(".loudnorm-*")))

    def test_unsupported_selected_codec_publishes_nothing(self):
        compressed = self.root / "compressed.mka"
        subprocess.run([self.tools.ffmpeg, "-v", "error", "-i", str(self.source), "-map", "0:1",
                        "-c:a", "aac", "-threads", "1", "-n", str(compressed)],
                       capture_output=True, check=True, timeout=120)
        report = normalize(compressed, self.root / "compressed.wav", self.profile,
                           self.root / "compressed.json", self.tools, audio_stream=0)
        self.assertEqual(report["status"], "UNSUPPORTED", report)
        self.assertIn("must be PCM or FLAC", report["error"])
        self.assertFalse((self.root / "compressed.wav").exists())

    def test_local_network_playlist_is_rejected_by_demuxer_allowlist(self):
        playlist = self.root / "disguised.mkv"
        playlist.write_text("ffconcat version 1.0\nfile 'https://example.invalid/segment.wav'\n")
        report = normalize(playlist, self.root / "playlist.wav", self.profile,
                           self.root / "playlist.json", self.tools)
        self.assertEqual(report["status"], "ERROR", report)
        self.assertIn("not on whitelist", report["error"])
        self.assertFalse((self.root / "playlist.wav").exists())


if __name__ == "__main__":
    unittest.main()
