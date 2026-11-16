"""Local synthetic WebVTT preservation fixtures; no network or external player."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pysubs2

from mediawrench.rollup import CaptionError, convert, parse_vtt

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "examples/rollup/preserve-layout.vtt"


class VttOutputTests(unittest.TestCase):
    def test_metadata_settings_text_timing_and_provenance_roundtrip(self):
        result = convert(FIXTURE.read_text(encoding="utf-8"), output_format="vtt")
        self.assertEqual(result.output, result.vtt)
        self.assertEqual(result.output_format, "vtt")
        with self.assertRaises(CaptionError):
            _ = result.srt
        expected = [(0, 3000, "Hello there"), (1000, 3000, "New line")]
        loaded, warnings = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertFalse(warnings)
        self.assertEqual([(c.start_ms, c.end_ms, c.plain) for c in loaded], expected)
        self.assertEqual([c.identifier for c in loaded], ["seed", "growth"])
        self.assertEqual([c.settings for c in loaded], ["align:start position:20%"] * 2)
        data = result.provenance
        self.assertEqual(data["vtt_sha256"], hashlib.sha256(result.vtt.encode()).hexdigest())
        self.assertNotIn("srt_sha256", data)
        self.assertEqual(data["webvtt"]["header"],
                         "WEBVTT Local captions\nKind: captions\nLanguage: en")
        self.assertTrue(result.vtt.startswith(data["webvtt"]["header"] + "\n\n"))
        self.assertEqual([n["before_source_cue"] for n in data["webvtt"]["notes"]], [1, 2, 3])
        for note in data["webvtt"]["notes"]:
            self.assertEqual(result.vtt.count(note["text"]), 1)
        self.assertEqual(data["loss_policy"]["metadata"], "preserve")
        self.assertIn("loss_policy", result.report)
        self.assertEqual(data["summary"]["removed_tokens"], 2)
        records = {t["id"]: t for t in data["tokens"]}
        for token in records.values():
            root = records[token["representative"]]
            self.assertEqual(root["action"], "retained")
            self.assertEqual(root["output"], token["output"])
            cue = loaded[token["output"]["cue"] - 1]
            self.assertEqual(cue.tokens[token["output"]["token"] - 1].text, token["text"])
        for output in data["output_cues"]:
            self.assertIn("source_span", output)
            self.assertEqual(output["settings"], "align:start position:20%")

    def test_pinned_pysubs2_reads_cue_only_output_with_settings(self):
        # pysubs2 1.8.0 does not parse interleaved NOTE/IDs correctly. Exercise
        # its real VTT parser on the cue-only subset that it does understand.
        text = ("WEBVTT\n\n00:00.000 --> 00:02.000 align:start position:20%\nHello there\n\n"
                "00:01.000 --> 00:03.000 align:start position:20%\nHello there\nNew line\n")
        result = convert(text, output_format="vtt")
        loaded = pysubs2.SSAFile.from_string(result.vtt, format_="vtt")
        self.assertEqual([(c.start, c.end, c.plaintext) for c in loaded],
                         [(0, 3000, "Hello there"), (1000, 3000, "New line")])

    def test_changed_settings_prevent_aliasing_and_interval_extension(self):
        source = FIXTURE.read_text().replace("00:03.000 align:start", "00:03.000 align:end")
        result = convert(source, output_format="vtt", word_overlap=True)
        self.assertEqual(result.report["summary"]["removed_tokens"], 0)
        self.assertIn("settings", result.report["ambiguities"][0]["reason"])
        cues, _ = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertEqual(cues[0].end_ms, 2000)
        self.assertEqual(cues[1].settings, "align:end position:20%")

    def test_notes_survive_removed_cues_and_split_fragments(self):
        text = ("WEBVTT\n\n00:00.000 --> 00:02.000 align:start\nAlpha one\nBeta two\n\n"
                "NOTE removed display\n\n00:01.000 --> 00:03.000 align:start\nAlpha one\nBeta two\n\n"
                "NOTE sliding display\n\n00:02.000 --> 00:04.000 align:start\nBeta two\nGamma three\n")
        result = convert(text, output_format="vtt")
        cues, _ = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertEqual([(c.plain, c.end_ms, c.settings) for c in cues],
                         [("Alpha one", 3000, "align:start"), ("Beta two", 4000, "align:start"),
                          ("Gamma three", 4000, "align:start")])
        self.assertLess(result.vtt.index("NOTE removed"), result.vtt.index("NOTE sliding"))
        self.assertEqual(result.vtt.count("NOTE removed display"), 1)

    def test_supported_settings_and_invalid_settings(self):
        valid = ["align:start position:20%,line-left", "align:center position:50%,center size:80%",
                 "align:end position:100%,line-right", "align:left line:-1,end vertical:rl",
                 "align:right line:20%,center vertical:lr"]
        invalid = ["align:middle", "position:101%", "position:-1%", "position:20%,bad",
                   "align:start align:end", "region:top", "unknown:value", "size:NaN%",
                   "line:20.5", "vertical:up", "align:start\x00", "position:20%align:end", "align:start\v", "align:start\u00a0size:50%"]
        for settings in valid:
            with self.subTest(settings=settings):
                text = f"WEBVTT\n\n00:00.000 --> 00:01.000 {settings}\nHello there\n"
                result = convert(text, output_format="vtt")
                self.assertIn(settings, result.vtt)
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(CaptionError):
                convert(f"WEBVTT\n\n00:00.000 --> 00:01.000 {settings}\nHello there\n",
                        output_format="vtt")

    def test_unsupported_structures_fail_even_with_flatten(self):
        cases = ["STYLE\n::cue {color:red}\n\n", "REGION\nid:top\n\n"]
        for block in cases:
            with self.subTest(block=block), self.assertRaisesRegex(CaptionError, "refusing"):
                convert("WEBVTT\n\n" + block + "00:00.000 --> 00:01.000\nHello there\n",
                        output_format="vtt", flatten=True)
        for payload in ["<v. Ada>Hello</v> there", "Hello <00:00.500>there"]:
            with self.subTest(payload=payload), self.assertRaisesRegex(CaptionError, "VTT.*preserv"):
                convert("WEBVTT\n\n00:00.000 --> 00:01.000\n" + payload,
                        output_format="vtt", flatten=True)
        with self.assertRaises(CaptionError):
            convert("WEBVTT\nBad --> header\n\n00:00.000 --> 00:01.000\nHello there",
                    output_format="vtt")

    def test_word_fragments_with_bom_crlf_and_layout(self):
        text = (ROOT / "examples/rollup/word-growing.vtt").read_text().replace(
            "WEBVTT", "WEBVTT Unicode fixture\nLanguage: en").replace(
            "00:02.000\n", "00:02.000 align:start position:20%\n").replace(
            "00:03.000\n", "00:03.000 align:start position:20%\n").replace(
            "00:04.000\n", "00:04.000 align:start position:20%\n")
        result = convert("\ufeff" + text.replace("\n", "\r\n"), output_format="vtt", word_overlap=True)
        cues, _ = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertEqual([(c.start_ms, c.end_ms, c.plain) for c in cues],
                         [(0, 4000, "We are"), (1000, 4000, "ready"), (2000, 4000, "now")])
        self.assertTrue(all(c.settings == "align:start position:20%" for c in cues))
        self.assertEqual(result.provenance["summary"]["removed_tokens"], 5)
        self.assertEqual(result.provenance["output_cues"][1]["source_span"],
                         {"cue": 2, "token_start": 3, "token_end": 4})

    def test_invalid_note_and_unsupported_blocks_publish_no_files(self):
        for block in ["NOTE invalid --> marker", "STYLE\n::cue {color:red}", "REGION\nid:top"]:
            with self.subTest(block=block), tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "in.vtt", Path(directory) / "out.vtt"
                source.write_text("WEBVTT\n\n" + block + "\n\n00:00.000 --> 00:01.000\nHello there")
                done = subprocess.run([sys.executable, "-m", "mediawrench.rollup", str(source),
                                       "--output-format", "vtt", "--flatten", "-o", str(output)],
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 2)
                self.assertFalse(output.exists())
                self.assertFalse(Path(str(output) + ".json").exists())

    def test_srt_policy_still_rejects_layout_and_reports_note_loss(self):
        with self.assertRaises(CaptionError):
            convert(FIXTURE.read_text(), flatten=True)
        result = convert("WEBVTT\n\nNOTE a comment\n\n00:00.000 --> 00:01.000\nHello there")
        self.assertEqual(result.report["warnings"][0]["kind"], "comment_omitted")
        self.assertNotIn("loss_policy", result.provenance)  # default API remains compatible

    def test_escaped_literals_safe_for_vtt_and_invalid_format(self):
        text = "WEBVTT\n\n00:00.000 --> 00:01.000\na &lt;b&gt; &amp; {literal} \\N\n"
        result = convert(text, output_format="vtt")
        cues, _ = parse_vtt(result.vtt, preserve_metadata=True)
        self.assertEqual(cues[0].plain, "a <b> & {literal} \\N")
        self.assertIn("&lt;b&gt; &amp;", result.vtt)
        with self.assertRaisesRegex(CaptionError, "output_format"):
            convert(text, output_format="ass")
        with self.assertRaisesRegex(CaptionError, "VTT input"):
            convert("1\n00:00:00,000 --> 00:00:01,000\nHello there", input_format="srt", output_format="vtt")

    def test_cli_writes_vtt_and_dry_run_and_failure_publish_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.vtt"
            command = [sys.executable, "-m", "mediawrench.rollup", str(FIXTURE),
                       "--output-format", "vtt", "-o", str(output)]
            dry = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, timeout=120)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(json.loads(dry.stdout)["loss_policy"]["metadata"], "preserve")
            self.assertEqual(list(Path(directory).iterdir()), [])
            done = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(output.read_text().startswith("WEBVTT Local captions"))
            self.assertIn("vtt_sha256", json.loads(Path(str(output) + ".json").read_text()))
            source = Path(directory) / "unsupported.vtt"
            source.write_text("WEBVTT\n\nSTYLE\n::cue {color:red}\n\n00:00.000 --> 00:01.000\nHello there")
            failed = subprocess.run([sys.executable, "-m", "mediawrench.rollup", str(source),
                                     "--output-format", "vtt", "--flatten", "-o", str(output), "--force"],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("refusing", failed.stderr)
            self.assertTrue(output.read_text().startswith("WEBVTT Local captions"))
            source.write_text(FIXTURE.read_text())
            # Same-format default path must be safe and must not overwrite input.
            default = subprocess.run([sys.executable, "-m", "mediawrench.rollup", str(source),
                                      "--output-format", "vtt"], capture_output=True, text=True, timeout=120)
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertTrue(source.with_name("unsupported.repaired.vtt").exists())
