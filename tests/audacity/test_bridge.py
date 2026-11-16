import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pysubs2

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.cli import _atomic_outputs
from mediawrench.audacity.core import validate_sidecar
from mediawrench.audacity.formats import decode_input, parse_srt


ROOT = Path(__file__).resolve().parents[2]
SRT = ("7\n00:00:01,000 --> 00:00:02,500\nHello\nworld\n\n"
       "9\n00:00:03,000 --> 00:00:04,000\nHello\nworld\n\n"
       "12\n00:00:05,000 --> 00:00:06,000\n  café 中文 🎵\t<i>hi</i> {literal} "
       "\\N \\n \\t / end  \n\n")


def resign(sidecar):
    """Test structural checks independently of the corruption detector."""
    data = {k: v for k, v in sidecar.items() if k != "integrity_sha256"}
    sidecar["integrity_sha256"] = hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()
    return sidecar


class RoundtripTests(unittest.TestCase):
    def setUp(self):
        self.labels, self.sidecar = export_subtitles(SRT.encode(), "srt")

    def test_real_srt_moved_timing_exact_payload(self):
        edited = self.labels.replace("1.000\t2.500", "1.250\t2.750")
        output, report = import_labels(edited, self.sidecar, source=SRT.encode())
        parsed = pysubs2.SSAFile.from_string(output, format_="srt", keep_html_tags=True)
        self.assertEqual((parsed[0].start, parsed[0].end), (1250, 2750))
        self.assertEqual([c.text for c in parse_srt(output)[0]],
                         [c["text"] for c in self.sidecar["cues"]])
        self.assertTrue(report["source_verified"])
        self.assertTrue(report["labels_changed_since_export"])
        self.assertEqual([a["kind"] for a in report["actions"]], ["moved"])

    def test_readable_projection_and_escape_safety(self):
        self.assertIn("[cue:7] Hello / world", self.labels)
        self.assertEqual(len(self.labels.splitlines()), 3)
        self.assertTrue(all(len(line.split("\t")) == 3 for line in self.labels.splitlines()))
        self.assertIn("\\t<i>hi</i>", self.labels)
        self.assertIn("\\\\N", self.labels)
        self.assertIn("中文 🎵", self.labels)

    def test_identical_texts_distinguished_after_crossing(self):
        rows = self.labels.splitlines()
        rows[0] = rows[0].replace("1.000\t2.500", "7.000\t8.000")
        edited = "\n".join([rows[1], rows[2], rows[0]]) + "\n"
        _, report = import_labels(edited, self.sidecar)
        self.assertEqual([c["id"] for c in report["output_cues"]], ["9", "12", "7"])
        self.assertEqual(report["output_cues"][-1]["start_ms"], 7000)

    def test_bom_and_crlf(self):
        labels, sidecar = export_subtitles(b"\xef\xbb\xbf" + SRT.replace("\n", "\r\n").encode(), "srt")
        output, _ = import_labels(labels, sidecar)
        self.assertEqual([c.text for c in parse_srt(output)[0]],
                         [c.text for c in parse_srt(SRT)[0]])

    def test_unchanged_hash_is_informational(self):
        _, report = import_labels(self.labels, self.sidecar)
        self.assertFalse(report["labels_changed_since_export"])
        edited = self.labels.replace("1.000", "1.001", 1)
        _, report = import_labels(edited, self.sidecar)
        self.assertTrue(report["labels_changed_since_export"])

    def test_text_conflict_has_original_and_edited(self):
        with self.assertRaises(BridgeError) as caught:
            import_labels(self.labels.replace("[cue:7] Hello / world", "[cue:7] Changed"), self.sidecar)
        conflict = caught.exception.report["conflicts"][0]
        self.assertEqual(conflict["original_text"], "Hello\nworld")
        self.assertEqual(conflict["edited_label"], "Changed")

    def test_explicit_original_text_policy(self):
        output, report = import_labels(self.labels.replace("[cue:7] Hello / world", "[cue:7] Changed"),
                                       self.sidecar, text_policy="original")
        self.assertEqual(parse_srt(output)[0][0].text, "Hello\nworld")
        self.assertEqual(report["actions"][0]["policy"], "original")

    def test_explicit_edited_text_is_literal_single_line(self):
        output, _ = import_labels(self.labels.replace("[cue:7] Hello / world", "[cue:7] new / text \\n"),
                                  self.sidecar, text_policy="edited")
        self.assertEqual(parse_srt(output)[0][0].text, "new / text \\n")

    def test_empty_edited_text_rejected(self):
        with self.assertRaisesRegex(BridgeError, "Empty"):
            import_labels(self.labels.replace("[cue:7] Hello / world", "[cue:7]"),
                          self.sidecar, text_policy="edited")

    def test_unknown_id_always_rejected(self):
        with self.assertRaisesRegex(BridgeError, "unknown cue ID"):
            import_labels(self.labels.replace("[cue:7]", "[cue:other]"), self.sidecar,
                          new_policy="add", missing_policy="drop")

    def test_malformed_reserved_id_rejected(self):
        for prefix in ("[cue:]", "[cue:7", "[cue:7]oops", "[cue:é]"):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(BridgeError, "malformed"):
                import_labels(self.labels.replace("[cue:7]", prefix), self.sidecar, new_policy="add")

    def test_duplicate_id_always_rejected(self):
        with self.assertRaisesRegex(BridgeError, "Duplicate label cue ID"):
            import_labels(self.labels + "7.000\t8.000\t[cue:7] Hello / world\n", self.sidecar)

    def test_missing_cue_default_is_conflict(self):
        with self.assertRaises(BridgeError) as caught:
            import_labels("\n".join(self.labels.splitlines()[1:]), self.sidecar)
        self.assertEqual(caught.exception.report["conflicts"][0]["kind"], "missing_cue")

    def test_missing_cue_keep(self):
        output, report = import_labels("\n".join(self.labels.splitlines()[1:]), self.sidecar,
                                       missing_policy="keep")
        self.assertEqual(len(parse_srt(output)[0]), 3)
        self.assertEqual(report["output_cues"][0]["line"], None)

    def test_missing_cue_drop(self):
        output, report = import_labels("\n".join(self.labels.splitlines()[1:]), self.sidecar,
                                       missing_policy="drop")
        self.assertEqual(len(parse_srt(output)[0]), 2)
        self.assertEqual(report["output_count"], 2)

    def test_explicit_drop_all(self):
        output, report = import_labels("", self.sidecar, missing_policy="drop")
        self.assertEqual(output, "")
        self.assertEqual(report["output_count"], 0)

    def test_new_label_default_conflict(self):
        with self.assertRaises(BridgeError) as caught:
            import_labels(self.labels + "7\t8\tnew\n", self.sidecar)
        self.assertEqual(caught.exception.report["conflicts"][0]["kind"], "new_label")

    def test_new_label_add(self):
        output, report = import_labels(self.labels + "7\t8\tnew\n", self.sidecar, new_policy="add")
        self.assertEqual(parse_srt(output)[0][-1].text, "new")
        self.assertIsNone(report["output_cues"][-1]["id"])

    def test_new_label_ignore(self):
        output, _ = import_labels(self.labels + "7\t8\tnew\n", self.sidecar, new_policy="ignore")
        self.assertEqual(len(parse_srt(output)[0]), 3)

    def test_multiple_conflicts_collected(self):
        edited = self.labels.splitlines()[1].replace("Hello / world", "changed") + "\n7\t8\tnew\n"
        with self.assertRaises(BridgeError) as caught:
            import_labels(edited, self.sidecar)
        self.assertEqual(len(caught.exception.report["conflicts"]), 4)

    def test_tampered_sidecar_explains_hash(self):
        self.sidecar["cues"][0]["text"] = "tampered"
        with self.assertRaisesRegex(BridgeError, "integrity hash mismatch.*edited label hashes"):
            import_labels(self.labels, self.sidecar)

    def test_source_hash_mismatch(self):
        with self.assertRaisesRegex(BridgeError, "Source hash mismatch"):
            import_labels(self.labels, self.sidecar, source=b"different")

    def test_duplicate_sidecar_id_even_with_valid_digest(self):
        self.sidecar["cues"][1]["id"] = "7"
        with self.assertRaisesRegex(BridgeError, "Duplicate sidecar"):
            validate_sidecar(resign(self.sidecar))

    def test_invalid_sidecar_schema_and_types(self):
        for sidecar in ([], {}, {"schema": "unknown"}):
            with self.subTest(sidecar=sidecar), self.assertRaises(BridgeError):
                validate_sidecar(sidecar)
        for field, value in (("start_ms", True), ("end_ms", "2500"), ("text", []),
                             ("label_text", "wrong"), ("metadata", []), ("source_id", 42)):
            bad = copy.deepcopy(self.sidecar)
            bad["cues"][0][field] = value
            with self.subTest(field=field), self.assertRaises(BridgeError):
                validate_sidecar(resign(bad))

    def test_invalid_policies_rejected(self):
        for key in ("text_policy", "missing_policy", "new_policy"):
            with self.subTest(key=key), self.assertRaises(BridgeError):
                import_labels(self.labels, self.sidecar, **{key: "guess"})


class FormatBoundaryTests(unittest.TestCase):
    def test_plain_labels_require_explicit_loss(self):
        with self.assertRaisesRegex(BridgeError, "allow-lossy"):
            import_labels("1\t2\tplain\n")

    def test_plain_labels_loss_warning_and_literal_text(self):
        output, report = import_labels("1\t2\t[cue:7] literal / text \\t\n", allow_lossy=True)
        self.assertIn("LOSSY", report["warnings"][0])
        self.assertEqual(parse_srt(output)[0][0].text, "[cue:7] literal / text \\t")

    def test_point_label_requires_duration(self):
        with self.assertRaisesRegex(BridgeError, "point label requires"):
            import_labels("1\t1\tpoint", allow_lossy=True)

    def test_point_duration_real_srt(self):
        output, report = import_labels("1\t1\tpoint", allow_lossy=True, point_duration="0.5")
        cue = pysubs2.SSAFile.from_string(output, format_="srt")[0]
        self.assertEqual((cue.start, cue.end), (1000, 1500))
        self.assertEqual(report["actions"][0]["kind"], "point_duration")

    def test_point_duration_invalid(self):
        for duration in ("0", "-1", "nan", "inf", "0.0004", "1e2"):
            with self.subTest(duration=duration), self.assertRaises(BridgeError):
                import_labels("1\t1\tpoint", allow_lossy=True, point_duration=duration)

    def test_millisecond_rounding_audited(self):
        output, report = import_labels("1.000499\t2.000500\ttext", allow_lossy=True)
        self.assertIn("00:00:01,000 --> 00:00:02,001", output)
        self.assertEqual(report["actions"][0]["kind"], "millisecond_rounding")

    def test_collapsed_interval_rejected(self):
        with self.assertRaisesRegex(BridgeError, "collapses"):
            import_labels("1.0001\t1.0002\ttext", allow_lossy=True)

    def test_invalid_times(self):
        for start, end in (("nan", "2"), ("1", "inf"), ("-1", "2"), ("2", "1"),
                           ("1e0", "2"), ("0", "360000"), ("0", "1,5"),
                           ("1.0000000000001", "2"), ("1", "NaN")):
            with self.subTest(start=start, end=end), self.assertRaises(BridgeError):
                import_labels(f"{start}\t{end}\ttext", allow_lossy=True)

    def test_label_time_order(self):
        with self.assertRaisesRegex(BridgeError, "out of start-time order"):
            import_labels("2\t3\ta\n1\t2\tb", allow_lossy=True)

    def test_overlaps_and_equal_start_supported(self):
        output, _ = import_labels("1\t3\ta\n1\t2\tb", allow_lossy=True)
        self.assertEqual(len(parse_srt(output)[0]), 2)

    def test_invalid_tsv_and_frequency_continuation(self):
        for labels in ("one field", "1\t2\ta\tb", "1\t2\ttext\n\n", "\n1\t2\ttext"):
            with self.subTest(labels=labels), self.assertRaises(BridgeError):
                import_labels(labels, allow_lossy=True)
        with self.assertRaisesRegex(BridgeError, "frequency continuation"):
            import_labels("1\t2\ttext\n\\\t100\t200", allow_lossy=True)

    def test_empty_label_text_and_empty_input_rejected(self):
        for labels in ("", "1\t2", "1\t2\t", "1\t2\t  "):
            with self.subTest(labels=labels), self.assertRaises(BridgeError):
                import_labels(labels, allow_lossy=True)

    def test_unsupported_input_format(self):
        with self.assertRaisesRegex(BridgeError, "Only SRT"):
            export_subtitles(b"something", "ass")

    def test_invalid_encoding_control_chars(self):
        for raw in (b"\xff", b"a\x00b", b"a\rb"):
            with self.subTest(raw=raw), self.assertRaises(BridgeError):
                decode_input(raw)
        with self.assertRaises(BridgeError):
            import_labels("1\t2\ta\x01b", allow_lossy=True)

    def test_invalid_srt(self):
        bad_inputs = ("", "not subtitles", SRT.replace("2,500", "0,500", 1),
                      SRT.replace("[unused]", "noop") + "bad block",
                      SRT.replace("9\n00:", "7\n00:"),
                      SRT.replace("00:00:03,000", "00:00:00,000"),
                      SRT.replace("00:00:02,500", "00:60:02,500"),
                      SRT.replace(" --> ", " --> X1:2 ", 1))
        for source in bad_inputs:
            with self.subTest(source=source[:60]), self.assertRaises(BridgeError):
                export_subtitles(source.encode(), "srt")

    def test_ambiguous_timing_in_payload_rejected(self):
        with self.assertRaisesRegex(BridgeError, "ambiguous timestamp"):
            export_subtitles(b"1\n00:00:01,000 --> 00:00:02,000\n"
                             b"00:00:03,000 --> 00:00:04,000\n", "srt")

    def test_long_numeric_source_id_gets_short_visible_id(self):
        labels, sidecar = export_subtitles(SRT.replace("7\n", "12345678901234567890\n", 1).encode(), "srt")
        self.assertIn("[cue:c1]", labels)
        self.assertEqual(sidecar["cues"][0]["source_id"], "12345678901234567890")

    def test_vtt_real_format_end_to_end_and_metadata(self):
        source = ("WEBVTT synthetic\n\nNOTE keep this note\nprovenance\n\n"
                  "speaker α\n00:01.000 --> 00:02.500\nHello\nworld\n\n"
                  "00:00:03.000 --> 00:00:04.000\n中文\n\n")
        labels, sidecar = export_subtitles(source.encode(), "vtt")
        output, report = import_labels(labels.replace("1.000", "1.250"), sidecar)
        self.assertEqual(parse_srt(output)[0][0].text, "Hello\nworld")
        self.assertEqual(parse_srt(output)[0][0].start_ms, 1250)
        self.assertEqual(sidecar["cues"][0]["source_id"], "speaker α")
        self.assertEqual(report["source"]["metadata"]["notes"][0]["text"],
                         "NOTE keep this note\nprovenance")
        self.assertIn("WebVTT", report["warnings"][0])

    def test_unsupported_vtt_features_rejected(self):
        payloads = ("STYLE\n::cue {color: red}", "REGION\nid:r",
                    "00:01.000 --> 00:02.000 align:invalid\ntext",
                    "00:01.000 --> 00:02.000\n<v speaker>text",
                    "00:01.000 --> 00:02.000\na &amp; b",
                    "00:01.000 --> 00:02.000\na --> b",
                    "00:01.000 --> 00:02.000", "NOTE --> invalid")
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(BridgeError):
                export_subtitles(("WEBVTT\n\n" + payload).encode(), "vtt")

    def test_invalid_vtt_header_and_timestamps(self):
        for source in ("not WEBVTT", "WEBVTT\nX-TIMESTAMP-MAP=anything\n\n",
                       "WEBVTT\n\n00:60.000 --> 00:61.000\ntext", "WEBVTT --> bad\n\n",
                       "WEBVTT\n\n00:02.000 --> 00:01.000\ntext"):
            with self.subTest(source=source), self.assertRaises(BridgeError):
                export_subtitles(source.encode(), "vtt")


class CLITests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tools").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="asb-test-", dir=ROOT / ".tools")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.srt"
        self.source.write_text(SRT, encoding="utf-8")
        self.labels = self.root / "labels.txt"
        self.sidecar = self.root / "labels.json"
        self.output = self.root / "out.srt"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "mediawrench.audacity", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)

    def do_export(self):
        result = self.run_cli("export", self.source, "--labels", self.labels, "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_help(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("export", result.stdout)

    def test_cli_file_end_to_end(self):
        self.do_export()
        self.labels.write_text(self.labels.read_text().replace("1.000", "1.250"), encoding="utf-8")
        result = self.run_cli("import", self.labels, "--sidecar", self.sidecar,
                              "--source", self.source, "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parse_srt(self.output.read_text())[0][0].start_ms, 1250)
        self.assertEqual(json.loads(Path(str(self.output) + ".report.json").read_text())["output_count"], 3)

    def test_failure_json_and_no_outputs(self):
        self.do_export()
        self.labels.write_text(self.labels.read_text().replace("Hello / world", "changed", 1))
        result = self.run_cli("import", self.labels, "--sidecar", self.sidecar, "--output", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["report"]["status"], "error")
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".report.json").exists())

    def test_export_dry_run_writes_nothing(self):
        result = self.run_cli("export", self.source, "--labels", self.labels,
                              "--sidecar", self.sidecar, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.labels.exists())
        self.assertFalse(self.sidecar.exists())

    def test_import_dry_run_writes_nothing(self):
        self.do_export()
        result = self.run_cli("import", self.labels, "--sidecar", self.sidecar,
                              "--output", self.output, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".report.json").exists())

    def test_no_clobber_and_no_partial_sidecar(self):
        self.labels.write_text("keep")
        result = self.run_cli("export", self.source, "--labels", self.labels, "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.labels.read_text(), "keep")
        self.assertFalse(self.sidecar.exists())

    def test_output_path_collision(self):
        result = self.run_cli("export", self.source, "--labels", self.labels, "--sidecar", self.labels)
        self.assertEqual(result.returncode, 2)
        self.assertIn("distinct", result.stderr)
        self.assertFalse(self.labels.exists())

    def test_import_report_collision(self):
        self.do_export()
        result = self.run_cli("import", self.labels, "--sidecar", self.sidecar,
                              "--output", self.output, "--report", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_missing_output_directory(self):
        result = self.run_cli("export", self.source, "--labels", self.root / "missing" / "labels.txt",
                              "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.sidecar.exists())

    def test_invalid_sidecar_json_and_duplicate_keys(self):
        self.do_export()
        for raw in ('{"bad":', '{"schema": "a", "schema": "b"}', '{"x":NaN}', "[]"):
            self.sidecar.write_text(raw)
            result = self.run_cli("import", self.labels, "--sidecar", self.sidecar,
                                  "--output", self.output)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)["status"], "error")
            self.assertFalse(self.output.exists())

    def test_plain_cli_warns_loss(self):
        self.labels.write_text("1\t2\tplain")
        result = self.run_cli("import", self.labels, "--output", self.output, "--allow-lossy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOSSY", result.stderr)

    def test_missing_file_nonzero(self):
        result = self.run_cli("export", self.root / "missing.srt", "--labels", self.labels,
                              "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.labels.exists())

    def test_symlink_output_rejected(self):
        self.labels.symlink_to(self.root / "not-created")
        result = self.run_cli("export", self.source, "--labels", self.labels, "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.sidecar.exists())

    def test_unsupported_vtt_cli_nonzero(self):
        self.source = self.root / "source.vtt"
        self.source.write_text("WEBVTT\n\nSTYLE\n::cue {color:red}\n\n")
        result = self.run_cli("export", self.source, "--labels", self.labels, "--sidecar", self.sidecar)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported", result.stderr)
        self.assertFalse(self.labels.exists())

    def test_atomic_staging_failure_leaves_no_files(self):
        # This failure-injection test covers I/O cleanup only; format E2E above
        # executes real library parsing, real subprocesses and real files.
        with patch("mediawrench.audacity.cli.os.link", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                _atomic_outputs({self.output: "complete", self.sidecar: "complete"}, False)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.root.glob(".asb-*")), [])

    def test_second_file_failure_rolls_back_first(self):
        real_link = os.link

        def fail_second(source, target):
            if target == self.sidecar:
                raise OSError("second link failed")
            real_link(source, target)

        with patch("mediawrench.audacity.cli.os.link", side_effect=fail_second):
            with self.assertRaises(OSError):
                _atomic_outputs({self.output: "complete", self.sidecar: "complete"}, False)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.root.glob(".asb-*")), [])

    def test_racing_existing_file_is_never_overwritten(self):
        real_link = os.link

        def create_racing_file(source, target):
            if target == self.sidecar:
                self.sidecar.write_text("racing contents")
            real_link(source, target)

        with patch("mediawrench.audacity.cli.os.link", side_effect=create_racing_file):
            with self.assertRaises(FileExistsError):
                _atomic_outputs({self.output: "complete", self.sidecar: "complete"}, False)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.sidecar.read_text(), "racing contents")
        self.assertEqual(list(self.root.glob(".asb-*")), [])


if __name__ == "__main__":
    unittest.main()
