import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".tools" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="chapter-roundtrip-test-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.json"
        self.source.write_text((ROOT / "examples/chapters/chapters.info.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.output = self.root / "out.ffmetadata"
        self.report = self.root / "audit.json"

    def run_cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def convert(self, *extra, expected=0, target="ffmetadata"):
        return self.run_cli("convert", self.source, self.output, "--from", "json", "--to", target,
                            *extra, expected=expected)

    def test_cli_file_format_end_to_end(self):
        result = self.convert("--report", self.report)
        self.assertTrue(json.loads(result.stdout)["roundtrip_equal"])
        self.assertTrue(json.loads(self.report.read_text())["lossless"])
        restored = self.root / "restored.json"
        self.run_cli("convert", self.output, restored, "--from", "ffmetadata", "--to", "json")
        equal = self.run_cli("compare", self.source, restored, "--left-format", "json", "--right-format", "json")
        self.assertTrue(json.loads(equal.stdout)["equal"])
        text = self.root / "chapters.txt"
        self.run_cli("convert", restored, text, "--from", "json", "--to", "text",
                     "--allow-lossy", "--report", self.root / "text-audit.json")
        self.run_cli("compare", self.source, text, "--left-format", "json", "--right-format", "text",
                     "--right-duration", "60")

    def test_failed_loss_does_not_create_outputs(self):
        self.convert("--report", self.report, target="text", expected=2)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_allow_lossy_requires_sidecar(self):
        self.convert("--allow-lossy", target="text", expected=2)
        self.assertFalse(self.output.exists())

    def test_allow_lossy_writes_source_report(self):
        self.convert("--allow-lossy", "--report", self.report, target="text")
        report = json.loads(self.report.read_text())
        self.assertFalse(report["lossless"])
        self.assertEqual(report["source"]["duration_us"], 60_000_000)
        self.assertEqual(report["losses"][0]["path"], "/duration_us")

    def test_dry_run_has_no_writes_even_in_nonexistent_directory(self):
        self.output = self.root / "missing" / "out.txt"
        self.report = self.root / "missing" / "audit.json"
        result = self.convert("--allow-lossy", "--report", self.report, "--dry-run", target="text")
        self.assertIn("source", json.loads(result.stdout))
        self.assertFalse(self.output.parent.exists())

    def test_dry_run_can_emit_complete_loss_audit_without_sidecar(self):
        result = self.convert("--allow-lossy", "--dry-run", target="text")
        self.assertFalse(json.loads(result.stdout)["lossless"])
        self.assertFalse(self.output.exists())

    def test_existing_output_rejected_then_force_replaces_atomically(self):
        self.output.write_text("keep me")
        self.convert(expected=2)
        self.assertEqual(self.output.read_text(), "keep me")
        self.convert("--force")
        self.assertTrue(self.output.read_text().startswith(";FFMETADATA1\n"))
        self.assertEqual(list(self.root.glob(".chapter-roundtrip-*")), [])

    def test_existing_sidecar_prevents_output(self):
        self.report.write_text("keep me")
        self.convert("--report", self.report, expected=2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.report.read_text(), "keep me")

    def test_cannot_overwrite_input_even_with_force(self):
        original = self.source.read_text()
        self.output = self.source
        self.convert("--force", expected=2)
        self.assertEqual(self.source.read_text(), original)

    def test_paths_cannot_alias_report(self):
        self.convert("--report", self.source, "--force", expected=2)
        self.convert("--report", self.output, expected=2)
        self.assertFalse(self.output.exists())

    def test_symlink_and_hardlink_input_alias_rejected(self):
        self.output.symlink_to(self.source)
        self.convert("--force", expected=2)
        self.output.unlink()
        self.output.hardlink_to(self.source)
        self.convert("--force", expected=2)

    def test_symlink_destination_rejected(self):
        target = self.root / "other.txt"
        target.write_text("keep")
        self.output.symlink_to(target)
        self.convert("--force", expected=2)
        self.assertEqual(target.read_text(), "keep")

    def test_staging_failure_keeps_destination_and_cleans_temps(self):
        self.output = self.root / "missing" / "output"
        self.convert("--report", self.report, expected=2)
        self.assertFalse(self.report.exists())
        self.assertEqual(list(self.root.glob(".chapter-roundtrip-*")), [])

    def test_invalid_input_nonzero_no_outputs(self):
        for raw in (b"not json", b"\xff\xfe", b'{"chapters":[{"start_time":0}]}',
                    b'{"duration":4,"chapters":[{"start_time":2},{"start_time":1}]}'):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                self.convert(expected=2)
                self.assertFalse(self.output.exists())

    def test_unsupported_format_and_section_nonzero(self):
        self.run_cli("convert", self.source, self.output, "--from", "auto", "--to", "json", expected=2)
        self.source.write_text(";FFMETADATA1\n[STREAM]\ntitle=x\n")
        self.run_cli("inspect", self.source, "--from", "ffmetadata", expected=2)

    def test_compare_differences_exit_one(self):
        other = self.root / "different.json"
        other.write_text(self.source.read_text().replace("Talk", "Changed"))
        result = self.run_cli("compare", self.source, other, "--left-format", "json", "--right-format", "json", expected=1)
        self.assertEqual(json.loads(result.stdout)["differences"][0]["path"], "/chapters/1/title")

    def test_inspect_integer_microseconds(self):
        result = self.run_cli("inspect", self.source, "--from", "json")
        self.assertEqual(json.loads(result.stdout)["chapters"][0]["end_us"], 30_000_000)

    def test_unknown_metadata_nonzero_until_explicit_loss(self):
        data = json.loads(self.source.read_text())
        data["chapters"][0]["unknown"] = {"x": 1}
        self.source.write_text(json.dumps(data))
        error = self.convert(expected=2)
        self.assertIn("unknown", error.stderr)
        self.convert("--allow-lossy", "--report", self.report)
        self.assertEqual(json.loads(self.report.read_text())["losses"][0]["value"], {"x": 1})

    def test_reader_does_not_translate_bare_carriage_return(self):
        self.source.write_bytes(b"00:00:00\tx\r")
        self.run_cli("inspect", self.source, "--from", "text", "--duration", "1", expected=2)

    def test_help(self):
        self.assertIn("convert", self.run_cli("--help").stdout)


    def test_matroska_xml_json_xml_cli_roundtrip(self):
        fixture = ROOT / "examples/chapters/multilingual.matroska.xml"
        self.run_cli("convert", fixture, self.source, "--from", "matroska", "--to", "json", "--force")
        restored = self.root / "restored.xml"
        result = self.run_cli("convert", self.source, restored, "--from", "json", "--to", "matroska")
        self.assertTrue(json.loads(result.stdout)["lossless"])
        self.run_cli("compare", fixture, restored, "--left-format", "matroska", "--right-format", "matroska")

    def test_default_edition_xml_json_xml_cli_roundtrip(self):
        fixture = ROOT / "examples/chapters/default-edition.matroska.xml"
        expected = ROOT / "examples/chapters/default-edition.chapters.json"
        self.run_cli("convert", fixture, self.source, "--from", "matroska", "--to", "json", "--force")
        self.assertEqual(json.loads(self.source.read_text())["matroska"], {"edition_default": 1})
        self.run_cli("compare", expected, self.source, "--left-format", "json", "--right-format", "json")
        restored = self.root / "restored.xml"
        self.run_cli("convert", self.source, restored, "--from", "json", "--to", "matroska")
        self.assertIn("<EditionFlagDefault>1</EditionFlagDefault>", restored.read_text())
        self.run_cli("compare", fixture, restored, "--left-format", "matroska", "--right-format", "matroska")

    def test_default_edition_loss_refusal_and_saved_audit(self):
        fixture = ROOT / "examples/chapters/default-edition.matroska.xml"
        for target, path in (("ffmetadata", "/metadata/matroska"), ("text", "/metadata")):
            with self.subTest(target=target):
                args = ("convert", fixture, self.output, "--from", "matroska", "--to", target)
                error = self.run_cli(*args, "--report", self.report, expected=2)
                self.assertIn(path, error.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
                self.run_cli(*args, "--allow-lossy", expected=2)
                self.assertFalse(self.output.exists())
                self.run_cli(*args, "--allow-lossy", "--report", self.report)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["source"]["metadata"], {"matroska": {"edition_default": 1}})
                loss = next(item for item in report["losses"] if item["path"] == path)
                self.assertEqual(loss["value"], {"edition_default": 1} if target == "ffmetadata"
                                 else {"matroska": {"edition_default": 1}})
                self.assertFalse(report["lossless"])
                self.assertFalse(report["comparison"]["equal"])
                self.output.unlink()
                self.report.unlink()

    def test_default_edition_invalid_input_never_publishes(self):
        raw = (ROOT / "examples/chapters/default-edition.matroska.xml").read_text()
        flag = "<EditionFlagDefault>1</EditionFlagDefault>"
        for source_format, target, invalid in (
                ("matroska", "json", raw.replace(flag, flag * 2)),
                ("matroska", "json", raw.replace(flag, "<EditionFlagDefault>2</EditionFlagDefault>")),
                ("matroska", "json", raw.replace(flag, flag + "<EditionFlagOrdered>1</EditionFlagOrdered>")),
                ("json", "matroska", (ROOT / "examples/chapters/default-edition.chapters.json").read_text().replace(
                    '"edition_default": 1', '"edition_default": true'))):
            with self.subTest(invalid=invalid):
                self.source.write_text(invalid)
                self.run_cli("convert", self.source, self.output, "--from", source_format, "--to", target,
                             "--allow-lossy", "--report", self.report, expected=2)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())

    def test_hidden_edition_xml_json_xml_cli_roundtrip(self):
        fixture = ROOT / "examples/chapters/hidden-edition.matroska.xml"
        expected = ROOT / "examples/chapters/hidden-edition.chapters.json"
        result = self.run_cli("convert", fixture, self.source, "--from", "matroska", "--to", "json", "--force")
        self.assertTrue(json.loads(result.stdout)["lossless"])
        self.assertEqual(json.loads(self.source.read_text())["matroska"],
                         {"edition_uid": 42, "edition_hidden": 1, "edition_default": 1})
        self.run_cli("compare", expected, self.source, "--left-format", "json", "--right-format", "json")
        restored = self.root / "restored.xml"
        self.run_cli("convert", self.source, restored, "--from", "json", "--to", "matroska")
        for flag in ("Hidden", "Default"):
            self.assertIn(f"<EditionFlag{flag}>1</EditionFlag{flag}>", restored.read_text())
        self.run_cli("compare", fixture, restored, "--left-format", "matroska", "--right-format", "matroska")

    def test_hidden_chapters_xml_json_xml_cli_roundtrip(self):
        fixture = ROOT / "examples/chapters/hidden-chapters.matroska.xml"
        expected = ROOT / "examples/chapters/hidden-chapters.chapters.json"
        result = self.run_cli("convert", fixture, self.source, "--from", "matroska", "--to", "json", "--force")
        self.assertTrue(json.loads(result.stdout)["lossless"])
        data = json.loads(self.source.read_text())
        self.assertEqual(data["chapters"][0]["matroska"]["hidden"], 1)
        self.assertNotIn("matroska", data["chapters"][1])
        self.run_cli("compare", expected, self.source, "--left-format", "json", "--right-format", "json")
        restored = self.root / "restored.xml"
        self.run_cli("convert", self.source, restored, "--from", "json", "--to", "matroska")
        self.assertEqual(restored.read_text().count("<ChapterFlagHidden>1</ChapterFlagHidden>"), 1)
        self.assertNotIn("<ChapterFlagHidden>0", restored.read_text())
        self.run_cli("compare", fixture, restored, "--left-format", "matroska", "--right-format", "matroska")

    def test_hidden_chapters_loss_refusal_and_saved_audit(self):
        from mediawrench.chapters import dumps, loads, read_json, restore
        fixture = ROOT / "examples/chapters/hidden-chapters.matroska.xml"
        source = loads(fixture.read_text(), "matroska")
        for target, path in (("ffmetadata", "/chapters/0/metadata/matroska"),
                             ("text", "/chapters/0/metadata")):
            with self.subTest(target=target):
                args = ("convert", fixture, self.output, "--from", "matroska", "--to", target)
                error = self.run_cli(*args, "--report", self.report, expected=2)
                self.assertIn(path, error.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
                self.run_cli(*args, "--allow-lossy", expected=2)
                self.assertFalse(self.output.exists())
                self.run_cli(*args, "--allow-lossy", "--report", self.report)
                report = read_json(self.report.read_text())
                self.assertEqual(report["source"], source.normalized())
                loss = next(item for item in report["losses"] if item["path"] == path)
                self.assertEqual(loss["value"], source.chapters[0].metadata["matroska"]
                                 if target == "ffmetadata" else source.chapters[0].metadata)
                recovered = restore(report)
                self.assertEqual(loads(dumps(recovered, "matroska").text, "matroska"), source)
                self.output.unlink()
                self.report.unlink()

    def test_hidden_chapters_invalid_input_never_publishes_even_with_force(self):
        raw = (ROOT / "examples/chapters/hidden-chapters.matroska.xml").read_text()
        flag = "<ChapterFlagHidden>1</ChapterFlagHidden>"
        enabled = "<ChapterFlagEnabled>1</ChapterFlagEnabled>"
        cases = []
        for tag in ("ChapterFlagHidden", "ChapterFlagEnabled"):
            original = f"<{tag}>1</{tag}>"
            for value in ("", "2", "-1", "true", "false", "01", "+1", "1.0"):
                cases.append(("matroska", "json", raw.replace(original, f"<{tag}>{value}</{tag}>")))
            for extra in (original, f"<{tag}>0</{tag}>"):
                cases.append(("matroska", "json", raw.replace(original, original + extra)))
        for invalid in (raw.replace(flag, '<ChapterFlagHidden x="1">1</ChapterFlagHidden>'),
                        raw.replace(flag, "<ChapterFlagHidden><Value>1</Value></ChapterFlagHidden>"),
                        raw.replace(enabled, "<ChapterFlagEnabled>0</ChapterFlagEnabled>"),
                        raw.replace(flag, flag + "<ChapterSegmentUID>abc</ChapterSegmentUID>"),
                        raw.replace(flag, flag + "<ChapterSegmentEditionUID>42</ChapterSegmentEditionUID>"),
                        raw.replace(flag, flag + "<ChapterAtom/>"),
                        raw.replace("</EditionEntry>", "<EditionEntry/></EditionEntry>"),
                        raw.replace("</Chapters>", "<EditionEntry/></Chapters>"),
                        raw.replace("<EditionUID>", "<EditionFlagOrdered>1</EditionFlagOrdered><EditionUID>")):
            cases.append(("matroska", "json", invalid))
        data = json.loads((ROOT / "examples/chapters/hidden-chapters.chapters.json").read_text())
        for value in (0, -1, 2, True, False, "1", 1.0, None, [], {}):
            data["chapters"][0]["matroska"]["hidden"] = value
            cases.append(("json", "matroska", json.dumps(data)))
        for source_format, target, invalid in cases:
            with self.subTest(source_format=source_format, invalid=invalid):
                self.source.write_text(invalid)
                args = ("convert", self.source, self.output, "--from", source_format, "--to", target,
                        "--allow-lossy", "--report", self.report)
                self.run_cli(*args, expected=2)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
                self.output.write_bytes(b"output sentinel")
                self.report.write_bytes(b"report sentinel")
                self.run_cli(*args, "--force", expected=2)
                self.assertEqual(self.output.read_bytes(), b"output sentinel")
                self.assertEqual(self.report.read_bytes(), b"report sentinel")
                self.assertEqual(self.source.read_text(), invalid)
                self.assertEqual(list(self.root.glob(".chapter-roundtrip-*")), [])
                self.output.unlink()
                self.report.unlink()

    def test_hidden_edition_loss_refusal_and_saved_audit(self):
        from mediawrench.chapters import dumps, loads, read_json, restore
        fixture = ROOT / "examples/chapters/hidden-edition.matroska.xml"
        source = loads(fixture.read_text(), "matroska")
        for target, path in (("ffmetadata", "/metadata/matroska"), ("text", "/metadata")):
            with self.subTest(target=target):
                args = ("convert", fixture, self.output, "--from", "matroska", "--to", target)
                error = self.run_cli(*args, "--report", self.report, expected=2)
                self.assertIn(path, error.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
                self.run_cli(*args, "--allow-lossy", expected=2)
                self.assertFalse(self.output.exists())
                self.run_cli(*args, "--allow-lossy", "--report", self.report)
                report = read_json(self.report.read_text())
                self.assertFalse(report["lossless"])
                self.assertEqual(report["source"]["metadata"], source.metadata)
                loss = next(item for item in report["losses"] if item["path"] == path)
                self.assertEqual(loss["value"], source.metadata["matroska"] if target == "ffmetadata"
                                 else source.metadata)
                recovered = restore(report)
                self.assertEqual(loads(dumps(recovered, "matroska").text, "matroska"), source)
                self.output.unlink()
                self.report.unlink()

    def test_hidden_edition_invalid_input_never_publishes(self):
        raw = (ROOT / "examples/chapters/hidden-edition.matroska.xml").read_text()
        flag = "<EditionFlagHidden>1</EditionFlagHidden>"
        cases = []
        for name in ("Hidden", "Default"):
            original = f"<EditionFlag{name}>1</EditionFlag{name}>"
            for value in ("", "2", "-1", "true", "false", "01", "+1", "1.0"):
                cases.append(("matroska", "json", raw.replace(
                    original, f"<EditionFlag{name}>{value}</EditionFlag{name}>")))
            for extra in (original, f"<EditionFlag{name}>0</EditionFlag{name}>"):
                cases.append(("matroska", "json", raw.replace(original, original + extra)))
        for invalid in (raw.replace(flag, '<EditionFlagHidden x="1">1</EditionFlagHidden>'),
                        raw.replace(flag, "<EditionFlagHidden><Value>1</Value></EditionFlagHidden>"),
                        raw.replace(flag, flag + "<EditionFlagOrdered>1</EditionFlagOrdered>"),
                        raw.replace(flag, flag + "<EditionEntry/>"),
                        raw.replace("</Chapters>", "<EditionEntry/></Chapters>"),
                        raw.replace("</ChapterAtom>", "<ChapterAtom/></ChapterAtom>"),
                        raw.replace("<ChapterAtom>", "<ChapterAtom><ChapterFlagHidden>2</ChapterFlagHidden>"),
                        raw.replace("<ChapterAtom>", "<ChapterAtom><ChapterFlagEnabled>0</ChapterFlagEnabled>"),
                        raw.replace("<ChapterAtom>", "<ChapterAtom><ChapterSegmentUID>abc</ChapterSegmentUID>")):
            cases.append(("matroska", "json", invalid))
        data = json.loads((ROOT / "examples/chapters/hidden-edition.chapters.json").read_text())
        for value in (0, True, "1", 1.0, None):
            data["matroska"]["edition_hidden"] = value
            cases.append(("json", "matroska", json.dumps(data)))
        for source_format, target, invalid in cases:
            with self.subTest(source_format=source_format, invalid=invalid):
                self.source.write_text(invalid)
                args = ("convert", self.source, self.output, "--from", source_format, "--to", target,
                        "--allow-lossy", "--report", self.report)
                self.run_cli(*args, expected=2)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
                self.output.write_bytes(b"output sentinel")
                self.report.write_bytes(b"report sentinel")
                self.run_cli(*args, "--force", expected=2)
                self.assertEqual(self.output.read_bytes(), b"output sentinel")
                self.assertEqual(self.report.read_bytes(), b"report sentinel")
                self.output.unlink()
                self.report.unlink()

    def test_matroska_loss_requires_report_and_retains_displays(self):
        fixture = ROOT / "examples/chapters/multilingual.matroska.xml"
        for target in ("ffmetadata", "text"):
            with self.subTest(target=target):
                args = ("convert", fixture, self.output, "--from", "matroska", "--to", target)
                self.run_cli(*args, expected=2)
                self.assertFalse(self.output.exists())
                self.run_cli(*args, "--allow-lossy", expected=2)
                self.assertFalse(self.output.exists())
                self.run_cli(*args, "--allow-lossy", "--report", self.report)
                report = json.loads(self.report.read_text())
                details = report["source"]["chapters"][0]["metadata"]["matroska"]
                self.assertEqual(details["uid"], 101)
                self.assertEqual(details["displays"][1]["language_ietf"], "zh-Hans")
                self.assertEqual(details["displays"][1]["string"], "开场 & <档案> '甲'")
                self.output.unlink()
                self.report.unlink()

    def test_matroska_invalid_layouts_never_publish(self):
        raw = (ROOT / "examples/chapters/multilingual.matroska.xml").read_text(encoding="utf-8")
        for invalid in (raw.replace("<EditionFlagOrdered>0", "<EditionFlagOrdered>1"),
                        raw.replace("<ChapterUID>102", "<ChapterUID>101"),
                        raw.replace("00:00:00.000001000", "00:00:00.000000001"),
                        raw.replace("</ChapterAtom>", "<ChapterAtom/></ChapterAtom>", 1)):
            with self.subTest(invalid=invalid):
                self.source.write_text(invalid, encoding="utf-8")
                result = self.run_cli("convert", self.source, self.output, "--from", "matroska", "--to", "json",
                                     "--allow-lossy", "--report", self.report, expected=2)
                self.assertNotIn("unsupported format", result.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
