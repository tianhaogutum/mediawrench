"""Read-only Matroska inventory contract, using real local media unless noted.

INPUT --list-streams needs no export/profile/report. JSON OK/exit 0 lists every
absolute index, type, codec, channel count, reported sample rate/raw depth/layout, language/title and
default/forced flags. Unknown metadata stays null; text is preserved exactly.
No files are written and FFmpeg is never invoked. Inventory is not a decode or
normalization eligibility check. Mixed modes are usage errors (2); valid WAV/
FLAC is UNSUPPORTED (3); invalid paths/probes/budgets are ERROR (4).
"""
import json
import os
from pathlib import Path
from unittest import mock
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from mediawrench.loudness import Toolchain, list_streams
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


def native_rows(ffprobe, source):
    """ffprobe's own layout/raw-depth fields, normalized like list_streams.

    Whether an unnamed mono layout or a raw sample depth is reported depends on the
    FFmpeg build (distribution packages differ), so inventories are compared with the
    native probe instead of one build's values.
    """
    run = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                          "stream=index,channel_layout,bits_per_raw_sample", "-of", "json", str(source)],
                         capture_output=True, text=True, check=True, timeout=120)

    def depth(value):
        return int(value) if value not in (None, "", "N/A", "unknown") and int(value) else None

    return {row["index"]: {"channel_layout": None if row.get("channel_layout") in (None, "", "unknown")
                           else row["channel_layout"],
                           "bits_per_raw_sample": depth(row.get("bits_per_raw_sample"))}
            for row in json.loads(run.stdout)["streams"]}


def with_native(rows, native):
    return [{**row, **native[row["stream_index"]]} for row in rows]


@requires_ffmpeg
class ListStreamsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG
        cls.ffprobe = FFPROBE
        scratch = ROOT / ".tools/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix="list-streams-", dir=scratch)
        cls.source = Path(cls.fixtures.name) / "two tracks.mkv"
        subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
                        "-f", "lavfi", "-i", "color=size=16x16:rate=1:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
                        "-f", "lavfi", "-i", "sine=frequency=880:duration=1:sample_rate=48000",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1",
                        "-c:a", "pcm_s16le", "-c:a:1", "flac", "-ac:a:1", "2",
                        "-threads", "1", "-n", str(cls.source)],
                       capture_output=True, check=True, timeout=120)
        cls.source_bytes = cls.source.read_bytes()
        cls.tagged = Path(cls.fixtures.name) / "tagged tracks.mkv"
        subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-i", str(cls.source),
                        "-map", "0", "-map", "0:1", "-c", "copy",
                        "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=English — café 🎧",
                        "-metadata:s:a:1", "language=jpn", "-metadata:s:a:1", "title=日本語・解説",
                        "-metadata:s:a:2", "language=", "-metadata:s:a:2", "title=",
                        "-disposition:v:0", "0", "-disposition:a:0", "default",
                        "-disposition:a:1", "forced", "-disposition:a:2", "0", "-n", str(cls.tagged)],
                       capture_output=True, check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.fixtures.name)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def cli(self, source=None, *args):
        return subprocess.run([sys.executable, "-m", "mediawrench.loudness", str(source or self.source),
                               "--list-streams", "--ffprobe", self.ffprobe, *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def result(self, run, code=0, status="OK"):
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], status)
        self.assertNotIn("errno", result)
        self.assertNotIn("report_errno", result)
        self.assertFalse(result["published"])
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        return result

    def fake_probe(self, body):
        """Local executable injection; not real-media evidence."""
        wrapper = self.root / "ffprobe-fake"
        wrapper.write_text(f"#!{sys.executable}\n{body}\n")
        wrapper.chmod(0o755)
        return str(wrapper)

    def test_reported_sample_rates_match_native_probe(self):
        native = subprocess.run([self.ffprobe, "-v", "error", "-show_entries",
                                 "stream=index,sample_rate", "-of", "json", str(self.source)],
                                capture_output=True, text=True, check=True, timeout=120)
        reported = json.loads(native.stdout)["streams"]
        self.assertEqual([row.get("sample_rate") for row in reported], [None, "44100", "48000"])
        with mock.patch("mediawrench.loudness.core.subprocess.Popen", wraps=subprocess.Popen) as spawn, \
             mock.patch("mediawrench.loudness.core.tempfile.TemporaryDirectory", side_effect=AssertionError("staging")), \
             mock.patch("mediawrench.loudness.core.tempfile.mkstemp", side_effect=AssertionError("report")):
            api = list_streams(self.source, Toolchain(ffmpeg="/must-not-run", ffprobe=self.ffprobe))
        self.assertEqual(api["status"], "OK", api)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][0], self.ffprobe)
        self.assertEqual([row["sample_rate"] for row in api["streams"]], [None, 44100, 48000])
        self.assertEqual(self.result(self.cli()), api)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reported_raw_sample_depths_match_native_probe(self):
        source = self.root / "reported depths.mkv"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-i", str(self.source),
                        "-map", "0:0", "-map", "0:1", "-map", "0:2", "-map", "0:1",
                        "-map", "0:2", "-map", "0:1", "-c:v", "copy",
                        "-c:a:0", "flac", "-sample_fmt:a:0", "s16",
                        "-c:a:1", "flac", "-sample_fmt:a:1", "s32",
                        "-c:a:2", "pcm_s16le", "-c:a:3", "pcm_s24le", "-c:a:4", "aac",
                        "-threads", "1", "-n", str(source)],
                       capture_output=True, check=True, timeout=120)
        before = source.read_bytes()
        native = subprocess.run([self.ffprobe, "-v", "error", "-show_entries",
                                 "stream=index,codec_name,bits_per_raw_sample", "-of", "json", str(source)],
                                capture_output=True, text=True, check=True, timeout=120)
        reported = json.loads(native.stdout)["streams"]
        # FLAC always records its raw depth; PCM/AAC raw-depth reporting varies by FFmpeg build.
        self.assertEqual([row.get("bits_per_raw_sample") for row in reported][1:3], ["16", "24"])
        expected_depths = [v["bits_per_raw_sample"] for _, v in sorted(native_rows(self.ffprobe, source).items())]
        with mock.patch("mediawrench.loudness.core.subprocess.Popen", wraps=subprocess.Popen) as spawn, \
             mock.patch("mediawrench.loudness.core.tempfile.TemporaryDirectory", side_effect=AssertionError("staging")), \
             mock.patch("mediawrench.loudness.core.tempfile.mkstemp", side_effect=AssertionError("report")):
            api = list_streams(source, Toolchain(ffmpeg="/must-not-run", ffprobe=self.ffprobe))
        self.assertEqual(api["status"], "OK", api)
        self.assertEqual([row["bits_per_raw_sample"] for row in api["streams"]], expected_depths)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0], api["commands"][0])
        self.assertEqual(spawn.call_args.args[0][0], self.ffprobe)
        self.assertEqual(self.result(self.cli(source)), api)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_raw_sample_depth_strings_unknowns_and_non_audio_rows(self):
        # Fault injection: report this field only, never codec/sample_fmt/bits_per_sample.
        values = [None, "", "N/A", "unknown", "0", "000", "16", "024", "1", "64"]
        rows = [{"index": i + 2, "codec_type": "audio", "bits_per_raw_sample": value}
                for i, value in enumerate(values)]
        rows += [{"index": 1, "codec_type": "audio", "codec_name": "pcm_s24le",
                  "sample_fmt": "s32", "bits_per_sample": 24},
                 {"index": 0, "codec_type": "video"},
                 {"index": 99, "codec_type": "attachment"},
                 {"index": 98, "codec_type": "data", "bits_per_raw_sample": "32"}]
        info = {"streams": rows, "format": {"format_name": "matroska,webm"}}
        probe = self.fake_probe(f"print({json.dumps(info)!r})")
        api = list_streams(self.source, Toolchain(ffmpeg="/must-not-run", ffprobe=probe))
        self.assertEqual(api["status"], "OK", api)
        expected = [None, None, None, None, None, None, None, None, 16, 24, 1, 64, 32, None]
        self.assertEqual([row["bits_per_raw_sample"] for row in api["streams"]], expected)
        self.assertEqual(self.result(self.cli(None, "--ffprobe", probe)), api)

    def test_malformed_raw_sample_depths_reject_entire_inventory(self):
        values = [True, False, 0, 16, 24, -1, 24.0, 0.5, [], {}, "-1", "+24", "24.0",
                  "2.4e1", " 24", "24 ", "24\n", "２４", "٢٤", "NaN", "Infinity",
                  "unknown ", "n/a", "oops", "24bit"]
        for codec_type in ("audio", "video"):
            for value in values:
                with self.subTest(codec_type=codec_type, value=value):
                    info = {"streams": [{"index": 0, "bits_per_raw_sample": "16"},
                                        {"index": 1, "codec_type": codec_type, "bits_per_raw_sample": value}],
                            "format": {"format_name": "matroska,webm"}}
                    probe = self.fake_probe(f"print({json.dumps(info)!r})")
                    api = list_streams(self.source, Toolchain(ffmpeg="/must-not-run", ffprobe=probe))
                    self.assertEqual(api["status"], "ERROR", api)
                    self.assertIn("ffprobe", api["error"])
                    self.assertIn("raw sample bit depth", api["error"])
                    self.assertNotIn("streams", api)
                    self.assertEqual(self.result(self.cli(None, "--ffprobe", probe), 4, "ERROR"), api)

    def test_sample_rate_strings_unknowns_and_non_audio_rows(self):
        # Injected ffprobe encodings: only ASCII decimal strings, never inferred.
        values = [None, "", "N/A", "unknown", "0", "000", "44100", "048000", "1"]
        rows = [{"index": i + 2, "codec_type": "audio", "sample_rate": value}
                for i, value in enumerate(values)]
        rows += [{"index": 1, "codec_type": "audio", "codec_name": "pcm_s16le"},
                 {"index": 0, "codec_type": "video"},
                 {"index": 99, "codec_type": "attachment"},
                 {"index": 98, "codec_type": "data", "sample_rate": "96000"}]
        info = {"streams": rows, "format": {"format_name": "matroska,webm"}}
        probe = self.fake_probe(f"print({json.dumps(info)!r})")
        api = list_streams(self.source, Toolchain(ffmpeg="/must-not-run", ffprobe=probe))
        self.assertEqual(api["status"], "OK", api)
        expected = [None, None, None, None, None, None, None, None, 44100, 48000, 1, 96000, None]
        self.assertEqual([row["sample_rate"] for row in api["streams"]], expected)
        self.assertEqual(self.result(self.cli(None, "--ffprobe", probe)), api)

    def test_malformed_sample_rates_reject_entire_inventory(self):
        values = [True, False, 0, 44100, -1, 44100.0, 0.5, [], {}, "-1", "+44100",
                  "44100.0", "4.41e4", " 44100", "44100 ", "44100\n", "４４１００",
                  "٤٤١٠٠", "NaN", "Infinity", "unknown ", "n/a", "oops", "48000Hz"]
        for codec_type in ("audio", "video"):
            for value in values:
                with self.subTest(codec_type=codec_type, value=value):
                    info = {"streams": [{"index": 0, "sample_rate": "48000"},
                                        {"index": 1, "codec_type": codec_type, "sample_rate": value}],
                            "format": {"format_name": "matroska,webm"}}
                    probe = self.fake_probe(f"print({json.dumps(info)!r})")
                    api = list_streams(self.source, Toolchain(ffmpeg="/must-not-run", ffprobe=probe))
                    self.assertEqual(api["status"], "ERROR", api)
                    self.assertIn("ffprobe", api["error"])
                    self.assertIn("sample rate", api["error"])
                    self.assertNotIn("streams", api)
                    self.assertEqual(self.result(self.cli(None, "--ffprobe", probe), 4, "ERROR"), api)

    def test_tagged_inventory_preserves_unicode_and_missing_text_read_only(self):
        source = self.root / self.tagged.name
        before = self.tagged.read_bytes()
        source.write_bytes(before)
        native = native_rows(self.ffprobe, source)
        source.chmod(0o444)
        self.root.chmod(0o555)
        def no_write_open(path, mode="r", *args, **kwargs):
            self.assertFalse(any(flag in mode for flag in "wax+"), mode)
            return original_open(path, mode, *args, **kwargs)
        original_open = Path.open
        try:
            # Spy on actual process creation, and fail on any Python staging/report write.
            with mock.patch("mediawrench.loudness.core.subprocess.Popen", wraps=subprocess.Popen) as spawn, \
                 mock.patch("mediawrench.loudness.core.tempfile.TemporaryDirectory", side_effect=AssertionError("staging")), \
                 mock.patch("mediawrench.loudness.core.tempfile.mkstemp", side_effect=AssertionError("report temp")), \
                 mock.patch.object(Path, "open", no_write_open):
                result = list_streams(source, Toolchain(ffmpeg="/must-not-run", ffprobe=self.ffprobe))
            self.assertEqual(result["status"], "OK", result)
            self.assertFalse(result["published"])
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[0], result["commands"][0])
            self.assertEqual(result["commands"][0][0], self.ffprobe)
            self.assertEqual(len(result["commands"]), 1)
            expected = [
                {"stream_index": 0, "codec_type": "video", "codec": "ffv1", "channels": None,
                 "sample_rate": None, "bits_per_raw_sample": None, "channel_layout": None, "language": None, "title": None, "default": False, "forced": False},
                {"stream_index": 1, "codec_type": "audio", "codec": "pcm_s16le", "channels": 1,
                 "sample_rate": 44100, "bits_per_raw_sample": None, "channel_layout": None, "language": "eng", "title": "English — café 🎧",
                 "default": True, "forced": False},
                {"stream_index": 2, "codec_type": "audio", "codec": "flac", "channels": 2,
                 "sample_rate": 48000, "bits_per_raw_sample": 16, "channel_layout": "stereo", "language": "jpn", "title": "日本語・解説",
                 "default": False, "forced": True},
                {"stream_index": 3, "codec_type": "audio", "codec": "pcm_s16le", "channels": 1,
                 "sample_rate": 44100, "bits_per_raw_sample": None, "channel_layout": None, "language": None, "title": None, "default": False, "forced": False}]
            expected = with_native(expected, native)
            self.assertEqual(result["streams"], expected)
            run = self.cli(source)
            self.assertEqual(self.result(run)["streams"], expected)
            self.assertIn("English — café 🎧", run.stdout)
            self.assertIn("日本語・解説", run.stdout)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(list(self.root.iterdir()), [source])
        finally:
            self.root.chmod(0o755)

    def test_inventory_absolute_indices_codecs_layouts_without_publication(self):
        # A read-only directory and source need no writable export/report destination.
        source = self.root / self.source.name
        source.write_bytes(self.source_bytes)
        native = native_rows(self.ffprobe, source)
        source.chmod(0o444)
        self.root.chmod(0o555)
        try:
            result = self.result(self.cli(source))
            self.assertEqual(result["format"], "matroska,webm")
            self.assertEqual(result["streams"], with_native([
                {"stream_index": 0, "codec_type": "video", "codec": "ffv1",
                 "channels": None, "sample_rate": None, "bits_per_raw_sample": None, "channel_layout": None,
                 "language": None, "title": None, "default": False, "forced": False},
                {"stream_index": 1, "codec_type": "audio", "codec": "pcm_s16le",
                 "channels": 1, "sample_rate": 44100, "bits_per_raw_sample": None, "channel_layout": None,
                 "language": None, "title": None, "default": True, "forced": False},
                {"stream_index": 2, "codec_type": "audio", "codec": "flac",
                 "channels": 2, "sample_rate": 48000, "bits_per_raw_sample": 16, "channel_layout": "stereo",
                 "language": None, "title": None, "default": False, "forced": False}], native))
            self.assertEqual(source.read_bytes(), self.source_bytes)
            self.assertEqual(list(self.root.iterdir()), [source])
        finally:
            self.root.chmod(0o755)
        self.assertEqual(len(result["commands"]), 1)
        argv = result["commands"][0]
        self.assertEqual(argv[0], self.ffprobe)
        self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
        self.assertEqual(argv[argv.index("-format_whitelist") + 1], "wav,flac,matroska")

    def test_inventory_includes_unsupported_codec_and_multichannel_short_silence(self):
        source = self.root / "six-channel.mka"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "anullsrc=channel_layout=5.1:sample_rate=48000", "-t", "0.1", "-c:a", "aac",
                        "-threads", "1", "-n", str(source)], capture_output=True, check=True, timeout=120)
        before = source.read_bytes()
        native = native_rows(self.ffprobe, source)
        result = self.result(self.cli(source))
        # The AAC encoder's reported layout name (5.1 vs 5.1(side)) depends on the FFmpeg build.
        self.assertEqual(result["streams"], with_native([{"stream_index": 0, "codec_type": "audio", "codec": "aac",
                                             "channels": 6, "sample_rate": 48000,
                                             "language": None, "title": None, "default": False, "forced": False}], native))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_video_only_inventory_is_valid(self):
        source = self.root / "video.mkv"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-i", str(self.source),
                        "-map", "0:0", "-c", "copy", "-n", str(source)],
                       capture_output=True, check=True, timeout=120)
        result = self.result(self.cli(source))
        self.assertEqual(result["streams"], [{"stream_index": 0, "codec_type": "video", "codec": "ffv1",
                                             "channels": None, "sample_rate": None, "bits_per_raw_sample": None, "channel_layout": None,
                                             "language": None, "title": None, "default": False, "forced": False}])

    def test_normalization_arguments_cannot_be_mixed_with_listing(self):
        for args in ((str(self.root / "out.wav"),), ("--report", str(self.root / "report.json")),
                     ("--target-i", "-16"), ("--target-tp", "-1.5"), ("--tolerance", "0.5"),
                     ("--target-lra", "11"), ("--audio-stream", "1"), ("--ffmpeg", "missing")):
            with self.subTest(args=args):
                run = self.cli(None, *args)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertEqual(run.stdout, "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_directory_corrupt_and_network_referencing_input_fail_read_only(self):
        corrupt = self.root / "corrupt.mkv"
        corrupt.write_bytes(b"not matroska")
        playlist = self.root / "playlist.mkv"
        playlist.write_text("ffconcat version 1.0\nfile 'https://example.invalid/audio.wav'\n")
        for source in (self.root / "absent.mkv", self.root, corrupt, playlist,
                       "https://example.invalid/video.mkv"):
            with self.subTest(source=source):
                result = self.result(self.cli(source), 4, "ERROR")
                self.assertNotIn("streams", result)
                if source == playlist:
                    self.assertIn("not on whitelist", result["error"])
        self.assertEqual(corrupt.read_bytes(), b"not matroska")
        self.assertEqual(set(self.root.iterdir()), {corrupt, playlist})

    def test_non_matroska_wav_is_unsupported(self):
        source = self.root / "one.wav"
        subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-i", str(self.source),
                        "-map", "0:1", "-c:a", "pcm_s16le", "-n", str(source)],
                       capture_output=True, check=True, timeout=120)
        result = self.result(self.cli(source), 3, "UNSUPPORTED")
        self.assertIn("Matroska", result["error"])
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_missing_probe_and_invalid_budgets_are_structured_errors(self):
        for args in (("--ffprobe", str(self.root / "missing")), ("--timeout", "0"),
                     ("--timeout", "nan"), ("--total-timeout", "inf"), ("--total-timeout", "0")):
            with self.subTest(args=args):
                self.result(self.cli(None, *args), 4, "ERROR")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_probe_json_is_an_error_not_a_partial_inventory(self):
        base = {"streams": [{"index": 7, "codec_type": "audio"}],
                "format": {"format_name": "matroska,webm"}}
        for value in ("bad-json", None, [], {}, {**base, "streams": {}},
                      {**base, "streams": [None]}, {**base, "streams": [{"index": -1}]},
                      {**base, "streams": [{"index": True}]},
                      {**base, "streams": base["streams"] * 2},
                      {**base, "streams": [{"index": 7, "channels": "oops"}]}):
            with self.subTest(value=value):
                probe = self.fake_probe(f"print({json.dumps(value)!r})")
                result = self.result(self.cli(None, "--ffprobe", probe), 4, "ERROR")
                self.assertIn("ffprobe", result["error"])
                self.assertNotIn("streams", result)

    def test_unknown_metadata_stays_null_and_indices_stay_absolute(self):
        info = {"streams": [{"index": 9, "codec_type": "attachment"},
                            {"index": 3, "codec_type": "audio", "codec_name": "pcm_s16le",
                             "channels": 1, "channel_layout": "unknown"}],
                "format": {"format_name": "matroska,webm"}}
        probe = self.fake_probe(f"print({json.dumps(info)!r})")
        result = self.result(self.cli(None, "--ffprobe", probe))
        self.assertEqual(result["streams"], [
            {"stream_index": 3, "codec_type": "audio", "codec": "pcm_s16le",
             "channels": 1, "sample_rate": None, "bits_per_raw_sample": None, "channel_layout": None,
             "language": None, "title": None, "default": None, "forced": None},
            {"stream_index": 9, "codec_type": "attachment", "codec": None,
             "channels": None, "sample_rate": None, "bits_per_raw_sample": None, "channel_layout": None,
             "language": None, "title": None, "default": None, "forced": None}])

    def test_optional_metadata_preserves_text_and_independent_flags(self):
        # Injected ffprobe schema boundary, not evidence of real Matroska encoding.
        info = {"streams": [
            {"index": 9, "tags": {"language": "", "title": "  café\n日本語 🎧  ", "other": [1]},
             "disposition": {"default": 1, "forced": 1, "other": "ignored"}},
            {"index": 3, "tags": {"language": None, "title": None}, "disposition": {"default": 0}},
            {"index": 7, "tags": {}, "disposition": {}}],
            "format": {"format_name": "matroska,webm"}}
        probe = self.fake_probe(f"print({json.dumps(info)!r})")
        rows = self.result(self.cli(None, "--ffprobe", probe))["streams"]
        self.assertEqual([row["stream_index"] for row in rows], [3, 7, 9])
        self.assertIsNone(rows[0]["language"])
        self.assertIsNone(rows[0]["title"])
        self.assertIs(rows[0]["default"], False)
        self.assertIsNone(rows[0]["forced"])
        self.assertIsNone(rows[1]["default"])
        self.assertIsNone(rows[1]["forced"])
        self.assertEqual(rows[2]["language"], "")
        self.assertEqual(rows[2]["title"], "  café\n日本語 🎧  ")
        self.assertIs(rows[2]["default"], True)
        self.assertIs(rows[2]["forced"], True)

    def test_malformed_optional_metadata_is_error_without_partial_inventory(self):
        bad_streams = [{"tags": value} for value in (None, [], "eng", 7)]
        bad_streams += [{"disposition": value} for value in (None, [], "default", 1)]
        bad_streams += [{"tags": {name: value}} for name in ("language", "title")
                        for value in (7, True, [], {})]
        bad_streams += [{"disposition": {name: value}} for name in ("default", "forced")
                        for value in (None, True, False, "1", 1.0, -1, 2, [], {})]
        for bad in bad_streams:
            with self.subTest(bad=bad):
                info = {"streams": [{"index": 0}, {"index": 1, **bad}],
                        "format": {"format_name": "matroska,webm"}}
                probe = self.fake_probe(f"print({json.dumps(info)!r})")
                result = self.result(self.cli(None, "--ffprobe", probe), 4, "ERROR")
                self.assertIn("ffprobe", result["error"])
                self.assertNotIn("streams", result)

    def test_probe_timeouts_and_cancellation_reap_process(self):
        probe = self.fake_probe(f"import os, time\nfrom pathlib import Path\n"
                                f"Path({str(self.root / 'ready')!r}).write_text(str(os.getpid()))\n"
                                "time.sleep(20)")
        # Generous bounds: the stub's Python cold start must fit inside them on slow runners.
        for args, kind in ((("--total-timeout", "2"), "deadline_exceeded"),
                           (("--timeout", "2"), None), ((), "cancelled")):
            with self.subTest(args=args, kind=kind):
                ready = self.root / "ready"
                ready.unlink(missing_ok=True)
                proc = subprocess.Popen([sys.executable, "-m", "mediawrench.loudness", str(self.source),
                                         "--list-streams", "--ffprobe", probe, *args], cwd=ROOT,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    until = time.monotonic() + 30
                    while not ready.exists() and proc.poll() is None and time.monotonic() < until:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "must reach the injected probe")
                    if kind == "cancelled":
                        proc.send_signal(signal.SIGTERM)
                    stdout, stderr = proc.communicate(timeout=60)
                    run = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
                    result = self.result(run, 4, "ERROR")
                    self.assertEqual(result.get("error_kind"), kind)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(ready.read_text()), 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()


if __name__ == "__main__":
    unittest.main()
