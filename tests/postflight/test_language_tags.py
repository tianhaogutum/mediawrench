"""Explicit identities gate exact language-tag preservation on real local media."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples.postflight.synthetic import FFMPEG_AVAILABLE, FFMPEG_MISSING, ffmpeg, generate_mapping
from mediawrench.postflight import Options, compare, exit_code, write_report

ROOT = Path(__file__).resolve().parents[2]
MAP = ((0, 1), (1, 0), (2, 2))


def language_checks(report):
    return [c for c in report["checks"] if c["name"] == "language_tag"]


@unittest.skipUnless(FFMPEG_AVAILABLE, FFMPEG_MISSING)
class LanguageTagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="postflight-language-")
        cls.root = Path(cls.temp.name)
        cls.files = generate_mapping(cls.root)
        for name, stream, value in (("retagged", 1, "deu"), ("removed", 1, ""),
                                    ("added", 2, "eng"), ("case_changed", 1, "ENG"),
                                    ("und", 2, "und")):
            cls.files[name] = cls.root / f"{name}.mkv"
            ffmpeg(["-i", cls.files["reordered"], "-map", "0", "-c", "copy",
                    f"-metadata:s:{stream}", f"language={value}", cls.files[name]])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compare(self, case="reordered", **kwargs):
        return compare(self.files["source"], self.files[case],
                       Options(**dict(language_tags="preserve", stream_map=MAP, **kwargs)))

    def test_cli_mapped_english_retagged_german_fails(self):
        # Existing media checks alone accept this exact retagged candidate.
        baseline = compare(self.files["source"], self.files["retagged"], Options(stream_map=MAP))
        self.assertEqual(baseline["verdict"], "PASS", baseline)
        destination = self.root / "cli-language.json"
        args = [sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                str(self.files["retagged"]), "--language-tags", "preserve", "--report", str(destination)]
        args += [arg for a, b in MAP for arg in ("--stream-map", f"{a}:{b}")]
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads(destination.read_text()))
        self.assertEqual(report["verdict"], "FAIL")
        failed = [c for c in language_checks(report) if c["status"] == "FAIL"]
        self.assertEqual(len(failed), 1)
        self.assertEqual({key: failed[0][key] for key in
                          ("actual", "expected", "source_stream", "output_stream", "codec_type")},
                         dict(actual="deu", expected="eng", source_stream=0, output_stream=1, codec_type="audio"))
        self.assertEqual(report["mapping"]["status"], "PASS")
        self.assertTrue(all(c["status"] == "PASS" for c in report["checks"] if "duration" in c["name"]))
        self.assertEqual(report["stream_validation"]["source"][0]["language_tag_status"], "FAIL")
        self.assertEqual(report["stream_validation"]["output"][1]["language_tag_status"], "FAIL")

    def test_reorder_with_tags_and_both_absent_passes(self):
        report = self.compare()
        self.assertEqual(exit_code(report), 0, report)
        self.assertEqual(report["policy"]["language_tags"], "preserve")
        self.assertEqual([(c["source_stream"], c["output_stream"], c["expected"], c["actual"], c["status"])
                          for c in language_checks(report)],
                         [(0, 1, "eng", "eng", "PASS"), (1, 0, "deu", "deu", "PASS"),
                          (2, 2, None, None, "PASS")])
        self.assertTrue(all(s["language_tag_status"] == "PASS"
                            for side in report["stream_validation"].values() for s in side))
        # FFprobe 7 omits Matroska's default und language. Compare exported
        # tags only; we cannot distinguish an omitted tag from suppressed und.
        report = self.compare("und")
        self.assertEqual(exit_code(report), 0, report)
        self.assertIsNone(report["stream_validation"]["output"][2]["language"])

    def test_missing_added_and_case_changed_tags_fail(self):
        for case, source_index, output_index, before, after in (
                ("removed", 0, 1, "eng", None), ("added", 2, 2, None, "eng"),
                ("case_changed", 0, 1, "eng", "ENG")):
            with self.subTest(case=case):
                report = self.compare(case)
                self.assertEqual(exit_code(report), 1, report)
                failed = [c for c in language_checks(report) if c["status"] == "FAIL"]
                self.assertEqual([(c["source_stream"], c["output_stream"], c["expected"], c["actual"])
                                  for c in failed], [(source_index, output_index, before, after)])

    def test_ignore_default_does_not_compare_tags_or_infer_identity(self):
        report = compare(self.files["source"], self.files["retagged"], Options(stream_map=MAP))
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["policy"]["language_tags"], "ignore")
        self.assertEqual(language_checks(report), [])
        self.assertTrue(all(s["language_tag_status"] == "not_required"
                            for side in report["stream_validation"].values() for s in side))
        # Matching tags on an unchanged order still cannot establish identity.
        output = self.root / "unchanged.mkv"
        output.write_bytes(self.files["source"].read_bytes())
        report = compare(self.files["source"], output)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(report["mapping"]["status"], "UNKNOWN")

    def test_invalid_policy_or_missing_map_starts_no_tools(self):
        for options in (Options(language_tags="guess", stream_map=MAP), Options(language_tags="preserve")):
            with self.subTest(options=options):
                report = compare(self.files["source"], self.files["reordered"], options)
                self.assertEqual(exit_code(report), 2)
                self.assertEqual(report["commands"], [])
                self.assertEqual(language_checks(report), [])
                self.assertIn("language_tags", report["checks"][0]["actual"])
        result = subprocess.run([sys.executable, "-m", "mediawrench.postflight", str(self.files["source"]),
                                 str(self.files["reordered"]), "--language-tags", "preserve"],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["commands"], [])
        # The explicit-map contract also applies to a sole AV pair.
        audio = self.root / "single.flac"
        ffmpeg(["-i", self.files["source"], "-map", "0:0", audio])
        other = self.root / "single-copy.flac"
        other.write_bytes(audio.read_bytes())
        report = compare(audio, other, Options(language_tags="preserve"))
        self.assertEqual(exit_code(report), 2)
        self.assertEqual(report["commands"], [])

    def test_invalid_mapping_authorizes_no_language_checks(self):
        report = compare(self.files["source"], self.files["retagged"],
                         Options(language_tags="preserve", stream_map=((0, 1), (0, 0), (2, 2))))
        self.assertEqual(exit_code(report), 1)
        self.assertEqual(report["mapping"]["status"], "FAIL")
        self.assertEqual(language_checks(report), [])
        self.assertTrue(all(s["language_tag_status"] == "unmapped"
                            for side in report["stream_validation"].values() for s in side))

    def test_drops_and_subtitles_are_outside_tag_policy(self):
        report = compare(self.files["source"], self.files["kept_long"],
                         Options(language_tags="preserve", stream_map=((0, 0), (2, 1)), allow_drop=("audio",)))
        self.assertEqual(exit_code(report), 0, report)
        self.assertEqual(report["stream_validation"]["source"][1]["language_tag_status"], "not_required")
        self.assertEqual(len(language_checks(report)), 2)
        subtitle = self.root / "test.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n")
        source, output = self.root / "subs-source.mkv", self.root / "subs-output.mkv"
        ffmpeg(["-i", self.files["source"], "-i", subtitle, "-map", "0", "-map", "1",
                "-c", "copy", "-metadata:s:s:0", "language=eng", source])
        ffmpeg(["-i", source, "-map", "0", "-c", "copy", "-metadata:s:s:0", "language=deu", output])
        report = compare(source, output, Options(language_tags="preserve", subtitles="preserve",
                                                stream_map=((0, 0), (1, 1), (2, 2))))
        self.assertEqual(exit_code(report), 0, report)
        self.assertEqual(len(language_checks(report)), 3)
        for role in ("source", "output"):
            self.assertEqual(report["stream_validation"][role][3]["language_tag_status"], "not_required")

    def test_quick_and_checkpoint_keep_tag_evidence_separate_from_decode(self):
        snapshots = []
        def save(report, *args):
            snapshots.append(copy.deepcopy(report))
            write_report(report, *args)
        destination = self.root / "checkpoint.json"
        with patch("mediawrench.postflight.core.write_report", side_effect=save):
            report = compare(self.files["source"], self.files["retagged"],
                             Options(language_tags="preserve", stream_map=MAP, quick_seconds=0.25),
                             checkpoint_report=destination)
        self.assertEqual(exit_code(report), 1)
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(json.loads(json.dumps(report)), json.loads(destination.read_text()))
        self.assertEqual(snapshots[0]["verdict"], "UNKNOWN")
        # Probe-only snapshots precede identity resolution and tag comparison.
        for snapshot in snapshots[1:3]:
            self.assertEqual(snapshot["verdict"], "UNKNOWN")
            self.assertEqual(snapshot["mapping"]["status"], "not_run")
            self.assertEqual(snapshot["stream_validation"]["source"][0]["language_tag_status"], "not_run")
        for snapshot in snapshots[3:]:
            self.assertEqual(snapshot["verdict"], "FAIL")
            self.assertEqual(snapshot["stream_validation"]["source"][0]["language_tag_status"], "FAIL")
        self.assertEqual(snapshots[3]["mapping"]["status"], "PASS")
        self.assertEqual(snapshots[3]["decoded"], {"source": {}, "output": {}})
        self.assertTrue(all(p["completed_streams"] == p["attempted_streams"] == []
                            for p in snapshots[3]["progress"].values()))
        self.assertFalse(any("duration" in c["name"] or "decode" in c["name"] for c in snapshots[3]["checks"]))
        self.assertEqual(snapshots[3]["stream_validation"]["output"][1]["decode_coverage"], "none")
        self.assertEqual(self.compare(quick_seconds=0.25)["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
