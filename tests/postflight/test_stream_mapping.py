"""Real local FFmpeg fixtures prove caller-selected stream identity and coverage."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from examples.postflight.synthetic import FFMPEG_AVAILABLE, FFMPEG_MISSING, ffmpeg, generate_mapping
from mediawrench.postflight import Options, compare, exit_code

ROOT = Path(__file__).resolve().parents[2]
MAP = ((0, 1), (1, 0), (2, 2))


def checks(report, name):
    return [c for c in report["checks"] if c["name"] == name]


@unittest.skipUnless(FFMPEG_AVAILABLE, FFMPEG_MISSING)
class StreamMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="postflight-mapping-")
        cls.files = generate_mapping(Path(cls.temp.name))
        cls.files["unchanged"] = Path(cls.temp.name) / "unchanged.mkv"
        shutil.copyfile(cls.files["source"], cls.files["unchanged"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compare(self, case="reordered", **options):
        return compare(self.files["source"], self.files[case], Options(**options))

    def test_reordered_unequal_tracks_pass_with_explicit_indices(self):
        report = self.compare(stream_map=MAP)
        self.assertEqual(report["verdict"], "PASS", report)
        self.assertEqual(report["mapping"]["mode"], "explicit")
        self.assertEqual([(p["source_stream"], p["output_stream"]) for p in report["mapping"]["pairs"]], list(MAP))
        audio = checks(report, "audio_decoded_duration")
        self.assertEqual([(c["source_stream"], c["output_stream"], c["status"]) for c in audio],
                         [(0, 1, "PASS"), (1, 0, "PASS")])
        self.assertAlmostEqual(audio[0]["expected"], 2, delta=0.01)
        self.assertAlmostEqual(audio[1]["expected"], 0.5, delta=0.01)
        for role in ("source", "output"):
            self.assertEqual(len(report["stream_validation"][role]), 3)
            self.assertTrue(all(s["decode_coverage"] == "full" and s["duration_status"] == "PASS"
                                and s["decode_status"] == "PASS" for s in report["stream_validation"][role]))
        source, output = report["stream_validation"]["source"], report["stream_validation"]["output"]
        self.assertEqual([s["language"] for s in source[:2]], ["eng", "deu"])
        self.assertEqual([s["language"] for s in output[:2]], ["deu", "eng"])
        self.assertEqual(source[0]["disposition"]["default"], 1)
        self.assertEqual(source[1]["disposition"]["forced"], 1)
        self.assertEqual(output[0]["peer_stream"], 1)

    def test_no_map_never_guesses_multi_track_identity(self):
        # Even an unchanged order with matching metadata must request identity.
        for case in ("reordered", "unchanged"):
            with self.subTest(case=case):
                report = self.compare(case)
                self.assertEqual(report["verdict"], "UNKNOWN", report)
                self.assertEqual(checks(report, "stream_mapping")[0]["status"], "UNKNOWN")
                self.assertEqual(checks(report, "audio_decoded_duration"), [])
                self.assertEqual(report["mapping"]["pairs"],
                                 [{"source_stream": 2, "output_stream": 2, "codec_type": "video"}])
                self.assertEqual(report["stream_validation"]["source"][0]["duration_status"], "unmapped")

    def test_explicit_map_does_not_hide_shortened_retained_track(self):
        report = self.compare("shortened", stream_map=MAP)
        self.assertEqual(report["verdict"], "FAIL", report)
        self.assertEqual(checks(report, "container_duration")[0]["status"], "PASS")
        self.assertEqual([c["status"] for c in checks(report, "audio_decoded_duration")], ["FAIL", "PASS"])
        self.assertEqual(report["stream_validation"]["source"][0]["duration_status"], "FAIL")

    def test_invalid_maps_reject_with_specific_diagnostics(self):
        cases = [
            (((0, 1), (0, 0), (2, 2)), "duplicate source"),
            (((0, 1), (1, 1), (2, 2)), "duplicate output"),
            (((0, 1), (1, 99), (2, 2)), "output stream 99 does not exist"),
            (((99, 1), (1, 0), (2, 2)), "source stream 99 does not exist"),
            (((0, 2), (1, 0), (2, 1)), "type mismatch"),
            (((0, 1), (2, 2)), "uncovered output"),
            ((), "uncovered output"),
        ]
        for mapping, error in cases:
            with self.subTest(mapping=mapping):
                report = self.compare(stream_map=mapping, allow_drop=("audio",))
                self.assertEqual(report["verdict"], "FAIL", report)
                self.assertEqual(exit_code(report), 1)
                self.assertIn(error, " ".join(report["mapping"]["errors"]))
                self.assertEqual(report["mapping"]["pairs"], [])
                self.assertEqual(checks(report, "audio_decoded_duration"), [])

    def test_source_omission_requires_drop_policy_and_keeps_decode_coverage(self):
        mapping = ((0, 0), (2, 1))
        report = self.compare("kept_long", stream_map=mapping)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIn("uncovered source", " ".join(report["mapping"]["errors"]))
        report = self.compare("kept_long", stream_map=mapping, allow_drop=("audio",))
        self.assertEqual(report["verdict"], "PASS", report)
        omitted = report["stream_validation"]["source"][1]
        self.assertEqual(omitted["mapping_status"], "allowed_drop")
        self.assertEqual(omitted["decode_coverage"], "full")
        self.assertEqual(omitted["duration_status"], "not_required")
        self.assertIsNone(omitted["peer_stream"])
        self.assertEqual(self.compare("kept_long", allow_drop=("audio",))["verdict"], "UNKNOWN")

    def test_subtitles_remain_count_only_and_cannot_be_av_mapped(self):
        subtitle = Path(self.temp.name) / "mapping.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n", encoding="utf-8")
        src, out = Path(self.temp.name) / "subs-source.mkv", Path(self.temp.name) / "subs-output.mkv"
        ffmpeg(["-i", self.files["source"], "-i", subtitle, "-map", "0", "-map", "1", "-c", "copy", src])
        shutil.copyfile(src, out)
        mapping = ((0, 0), (1, 1), (2, 2))
        report = compare(src, out, Options(stream_map=mapping, subtitles="preserve"))
        self.assertEqual(report["verdict"], "PASS", report)
        sub = report["stream_validation"]["source"][3]
        self.assertEqual(sub["decode_coverage"], "none")
        self.assertEqual(sub["decode_status"], "not_required")
        self.assertEqual(sub["subtitle_count_status"], "PASS")
        report = compare(src, out, Options(stream_map=mapping + ((3, 3),)))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIn("not audio/video", " ".join(report["mapping"]["errors"]))

    def test_quick_mapping_reports_prefix_coverage(self):
        report = self.compare(stream_map=MAP, quick_seconds=0.25)
        self.assertEqual(report["verdict"], "PASS", report)
        self.assertEqual(report["coverage"], "partial")
        self.assertTrue(all(s["decode_coverage"] == "partial" for side in report["stream_validation"].values() for s in side))

    def test_malformed_library_maps_are_unknown_without_running_tools(self):
        for mapping in (((-1, 0),), ((True, 0),), ((0, "1"),), ((0,),), "0:1"):
            with self.subTest(mapping=mapping):
                report = self.compare(stream_map=mapping)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(report["commands"], [])

    def test_cli_mapping_pass_error_and_persisted_report(self):
        dest = Path(self.temp.name) / "report.json"
        args = [sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                str(self.files["reordered"]), "--report", str(dest)]
        for maps, expected in ((["0:1", "1:0", "2:2"], 0), (["0:1", "0:0", "2:2"], 1), ([], 2)):
            result = subprocess.run(args + [arg for pair in maps for arg in ("--stream-map", pair)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(json.loads(result.stdout), json.loads(dest.read_text()))
        for invalid in ("0", "0:1:2", "-1:0", "a:0"):
            result = subprocess.run(args + [f"--stream-map={invalid}"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SOURCE:OUTPUT", result.stderr)


if __name__ == "__main__":
    unittest.main()
