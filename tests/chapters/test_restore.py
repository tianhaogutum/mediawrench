"""Saved conversion source recovery; offline CLI and publication contracts."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.chapters import dumps, json_text, loads

ROOT = Path(__file__).resolve().parents[2]


class RestoreSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audit = self.root / "audit.json"
        self.output = self.root / "recovered.json"
        self.doc = loads('{"duration":3.000007,"archive":{"score":0.12345678901234567890123456789},'
                         '"chapters":[{"start_time":0.000001,"end_time":1.000002,"title":"开场",'
                         '"extra":[true,null,{"a/b~c":"保存"}]},'
                         '{"start_time":2.000003,"end_time":2.999999,"title":"End"}]}', "json")
        self.save()

    def save(self, target="text"):
        self.export = dumps(self.doc, target, allow_lossy=True)
        self.audit.write_text(json_text(self.export.report), encoding="utf-8")

    def test_cli_saved_lossy_text_recovers_source_without_export_or_probe(self):
        # Set MEDIAWRENCH_CLI to an installed console script to exercise it instead.
        command = ([os.environ["MEDIAWRENCH_CLI"], "chapters"] if "MEDIAWRENCH_CLI" in os.environ
                   else [sys.executable, "-m", "mediawrench.chapters"])
        environment = {**os.environ, "PATH": ""}
        environment.pop("PYTHONPATH", None)
        if "MEDIAWRENCH_CLI" not in os.environ:
            environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([*command, "restore-source", str(self.audit), str(self.output)],
                                cwd=self.root, env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"chapters":2,"duration_us":3_000_007})
        self.assertEqual(loads(self.output.read_text(encoding="utf-8"), "json"), self.doc)
        self.assertEqual(self.output.read_text(encoding="utf-8"), dumps(self.doc, "json").text)
        self.assertFalse(list(self.root.glob(".chapter-roundtrip-*")))

    def cli(self, expected=0, *, force=False):
        from mediawrench.chapters.cli import main
        from unittest.mock import patch
        stdout, stderr = StringIO(), StringIO()
        before = (self.audit.read_bytes(), self.audit.stat().st_mtime_ns) if self.audit.is_file() and os.access(self.audit, os.R_OK) else None
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             patch("subprocess.run") as run:
            code = main(["restore-source", str(self.audit), str(self.output), *(["--force"] if force else [])])
        run.assert_not_called()
        self.assertEqual(code, expected, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(list(self.root.rglob(".chapter-roundtrip-*")))
        if before is not None:
            self.assertEqual((self.audit.read_bytes(), self.audit.stat().st_mtime_ns), before)
        if expected == 2:
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            return stderr.getvalue()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), {"chapters":len(self.doc.chapters),
                                                        "duration_us":self.doc.duration_us})
        self.assertEqual(loads(self.output.read_text(encoding="utf-8"), "json"), self.doc)
        self.assertEqual(self.output.read_text(encoding="utf-8"), dumps(self.doc, "json").text)

    def test_lossless_and_lossy_audits_ignore_edited_or_missing_export(self):
        from mediawrench.chapters import read_json
        for target, source in (("json", self.doc), ("text", self.doc), ("ffmetadata", self.doc),
                               ("ffmetadata", loads('{"duration":1,"chapters":[{"start_time":0,"title":"A"}]}', "json"))):
            self.doc = source
            self.save(target)
            saved = read_json(self.audit.read_text())
            self.assertEqual(saved["lossless"], target == "json" or not source.metadata)
            self.assertEqual(saved["source"], source.normalized())
            exported = self.root / "exported"
            exported.write_text(self.export.text, encoding="utf-8")
            for edited in (True, False):
                with self.subTest(target=target, lossless=saved["lossless"], edited=edited):
                    if edited:
                        exported.write_text("00:00:00\tLATER USER EDIT\n", encoding="utf-8")
                    else:
                        exported.unlink()
                    self.cli()
                    self.output.unlink()
            if target == "text":
                self.assertIn("/duration_us", [loss["path"] for loss in saved["losses"]])
                self.assertNotIn("3.000007", self.export.text)

    def test_matroska_flags_displays_and_arbitrary_metadata_survive_both_lossy_formats(self):
        from decimal import Decimal
        self.doc = loads((ROOT / "examples/chapters/multilingual.matroska.xml").read_text(), "matroska")
        self.doc.metadata["matroska"].update(edition_hidden=1, edition_default=1)
        self.doc.chapters[0].metadata["matroska"]["hidden"] = 1
        self.doc.metadata["extra"] = {"score":Decimal("0.98765432109876543210987654321")}
        self.doc.chapters[1].metadata["extra"] = [None, False, {"unicode":"保存"}]
        self.doc.duration_us += 7
        self.assertEqual(self.doc.metadata["matroska"],
                         {"edition_uid":42,"edition_hidden":1,"edition_default":1})
        self.assertGreater(len(self.doc.chapters[0].metadata["matroska"]["displays"]), 1)
        for target in ("text", "ffmetadata"):
            with self.subTest(target=target):
                self.save(target)
                self.assertFalse(self.export.report["lossless"])
                self.cli()
                # Recovery as JSON retains XML-reserved values without reinterpretation.
                actual = loads(self.output.read_text(), "json")
                self.assertEqual(dumps(actual, "matroska", allow_lossy=True).text,
                                 dumps(self.doc, "matroska", allow_lossy=True).text)
                self.output.unlink()

    def test_only_source_is_recovered_not_comparison_or_loss_diagnostics(self):
        # This command validates source recovery, never authenticates the audit.
        self.audit.write_text(json_text({"schema_version":1,"source":self.doc.normalized(),
                                         "comparison":"untrusted", "lossless":False}), encoding="utf-8")
        self.cli()

    def test_existing_destination_requires_force_and_force_replaces_inode(self):
        old = self.root / "old-inode"
        self.output.write_bytes(b"keep")
        old.hardlink_to(self.output)
        self.assertIn("--force", self.cli(2))
        self.assertEqual(self.output.read_bytes(), b"keep")
        self.cli(force=True)
        self.assertEqual(old.read_bytes(), b"keep")
        self.assertFalse(old.samefile(self.output))

    def test_all_audit_aliases_refused_even_with_force(self):
        from unittest.mock import patch
        parent = self.root / "parent-link"
        parent.symlink_to(self.root, target_is_directory=True)
        directory = self.root / "existing"
        directory.mkdir()
        for kind in ("same", "normalized", "parent-symlink", "hardlink", "symlink"):
            target = self.root / "alias.json"
            if kind == "same": target = self.audit
            elif kind == "normalized": target = directory / ".." / self.audit.name
            elif kind == "parent-symlink": target = parent / self.audit.name
            elif kind == "hardlink": target.hardlink_to(self.audit)
            else: target.symlink_to(self.audit)
            self.output = target
            for force in (False, True):
                with self.subTest(kind=kind, force=force), patch("mediawrench.chapters.cli.restore") as restore:
                    self.assertIn("different files", self.cli(2, force=force))
                    restore.assert_not_called()
            if kind in ("hardlink", "symlink"): target.unlink()

    def test_unrelated_and_dangling_destination_symlinks_refused(self):
        other = self.root / "other.json"
        for exists in (False, True):
            if exists: other.write_bytes(b"keep")
            self.output.symlink_to(other)
            for force in (False, True):
                with self.subTest(exists=exists, force=force):
                    self.assertIn("symlink", self.cli(2, force=force))
                    self.assertTrue(self.output.is_symlink())
                    self.assertEqual(other.exists(), exists)
                    if exists: self.assertEqual(other.read_bytes(), b"keep")
            self.output.unlink()

    def assert_invalid_preserves_output(self, raw):
        self.audit.write_bytes(raw)
        for exists in (False, True):
            with self.subTest(raw=raw[:200], exists=exists):
                if exists: self.output.write_bytes(b"keep")
                self.cli(2, force=True)
                if exists:
                    self.assertEqual(self.output.read_bytes(), b"keep")
                    self.output.unlink()
                else:
                    self.assertFalse(self.output.exists())

    def test_strict_json_and_schema_rejections_never_publish(self):
        valid = self.audit.read_bytes()
        cases = [b"{", b"\xff", b"[]", b"null", b"true", b"{}",
                 valid.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
                 valid.replace(b'"schema_version": 1', b'"schema_version": 1e0'),
                 valid.replace(b'"title": "End"', b'"title": "\\ud800"')]
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            cases.append(valid.replace(b'"schema_version": 1', b'"extra": '+token+b', "schema_version": 1'))
        for value in (None, True, False, 0, 2, 1.0, "1", [], {}):
            # json.dumps deliberately produces 1.0 for the decimal schema case.
            cases.append(json.dumps({"schema_version":value,"source":self.doc.normalized()},
                                    default=str).encode())
        for raw in cases:
            self.assert_invalid_preserves_output(raw)

    def test_complete_normalized_source_and_model_validation(self):
        import copy
        from decimal import Decimal
        source = self.doc.normalized()
        sources = [None, [], "bad", {}, {**source,"chapters":[]},
                   {**source,"chapters":{}}, {**source,"chapters":None},
                   {**source,"chapters":[None]}, {**source,"metadata":[]},
                   {**source,"duration_us":True}, {**source,"duration_us":Decimal("3000007.0")},
                   {**source,"duration_us":2**63}, {**source,"duration_us":1}]
        sources += [{k:v for k,v in source.items() if k != missing}
                    for missing in ("duration_us","metadata","chapters")]
        for missing in ("start_us","end_us","title","metadata"):
            modified = copy.deepcopy(source)
            del modified["chapters"][0][missing]
            sources.append(modified)
        for field, values in (("start_us", [True, -1, "1", Decimal("1.0"), 2**63]),
                              ("end_us", [False, 0, None, "1000002", Decimal("1000002.0"), 2**63]),
                              ("title", [None, [], 1, "bad\x00title", "\ud800"]),
                              ("metadata", [None, [], 1])):
            for value in values:
                modified = copy.deepcopy(source)
                modified["chapters"][0][field] = value
                sources.append(modified)
        for start in (0, 1, 1_000_001):  # nonincreasing order or overlapping interval
            modified = copy.deepcopy(source)
            modified["chapters"][1]["start_us"] = start
            sources.append(modified)
        for value in sources:
            saved = {"schema_version":1,"source":value}
            # json_text refuses surrogates; use json.dumps only for that raw fixture.
            raw = (json.dumps(saved, default=str).encode() if "\\ud800" in repr(value)
                   else json_text(saved).encode())
            self.assert_invalid_preserves_output(raw)

    def test_json_structural_metadata_collisions_fail_without_losing_source(self):
        from mediawrench.chapters import Chapter, Document
        for global_metadata, chapter_metadata in (({"duration":"saved"}, {}),
                ({"chapters":["saved"]}, {}), ({}, {"start_time":"saved"}),
                ({}, {"end_time":"saved"}), ({}, {"title":"saved"})):
            doc = Document([Chapter(0,1,"A",chapter_metadata)],1,global_metadata)
            self.assert_invalid_preserves_output(json_text(
                {"schema_version":1,"source":doc.normalized()}).encode())

    def test_missing_directory_and_real_unreadable_audit_never_publish(self):
        self.assertNotEqual(os.geteuid(), 0, "permission contracts require unprivileged user")
        raw = self.audit.read_bytes()
        for failure in ("missing", "directory", "unreadable"):
            if failure == "unreadable": self.audit.chmod(0)
            else:
                self.audit.unlink()
                if failure == "directory": self.audit.mkdir()
            try:
                for exists in (False, True):
                    with self.subTest(failure=failure, exists=exists):
                        if exists: self.output.write_bytes(b"keep")
                        error = self.cli(2, force=True)
                        if failure == "unreadable": self.assertIn("Permission denied", error)
                        if exists:
                            self.assertEqual(self.output.read_bytes(), b"keep")
                            self.output.unlink()
                        else: self.assertFalse(self.output.exists())
            finally:
                if failure == "unreadable": self.audit.chmod(0o600)
                else:
                    if failure == "directory": self.audit.rmdir()
                    self.audit.write_bytes(raw)

    def test_audit_read_error_after_open_never_publishes(self):
        from unittest.mock import patch
        original_open = Path.open
        class BrokenReader(StringIO):
            def read(self, *args): raise OSError("synthetic read failure")
        def broken_open(path, *args, **kwargs):
            return BrokenReader("{") if path == self.audit else original_open(path,*args,**kwargs)
        # Avoid fixture helper's audit snapshot read inside the patched open.
        for exists in (False, True):
            if exists: self.output.write_bytes(b"keep")
            with patch("mediawrench.chapters.cli._read", side_effect=lambda path: broken_open(path).read()):
                self.assertIn("synthetic read failure", self.cli(2, force=True))
            if exists:
                self.assertEqual(self.output.read_bytes(), b"keep")
                self.output.unlink()
            else: self.assertFalse(self.output.exists())

    def test_stage_and_publication_failures_preserve_destination_and_clean_temps(self):
        from unittest.mock import Mock, patch
        create = tempfile.NamedTemporaryFile
        for operation in ("create", "write", "flush", "fsync", "link", "replace"):
            for exists in (False, True):
                if operation == "link" and exists: continue
                with self.subTest(operation=operation, exists=exists):
                    if exists: self.output.write_bytes(b"keep")
                    def fail_stage(*args, **kwargs):
                        if operation == "create": raise OSError("synthetic staging failure")
                        stream = create(*args, **kwargs)
                        setattr(stream, operation, Mock(side_effect=OSError("synthetic staged I/O failure")))
                        return stream
                    target = ("mediawrench.chapters.cli.tempfile.NamedTemporaryFile" if operation in
                              ("create", "write", "flush") else "mediawrench.chapters.cli.os."+operation)
                    kwargs = ({"new":fail_stage} if operation in ("create","write","flush") else
                              {"side_effect":OSError("synthetic publication failure")})
                    with patch(target, **kwargs):
                        self.cli(2, force=operation != "link")
                    if exists:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else: self.assertFalse(self.output.exists())

    def test_missing_unwritable_and_directory_destination_never_publish(self):
        self.assertNotEqual(os.geteuid(), 0, "permission contracts require unprivileged user")
        self.output = self.root / "missing" / "out.json"
        self.cli(2, force=True)
        self.assertFalse(self.output.parent.exists())
        self.output.parent.mkdir()
        self.output.mkdir()
        self.cli(2, force=True)
        self.assertTrue(self.output.is_dir())
        self.output.rmdir()
        for exists in (False, True):
            if exists: self.output.write_bytes(b"keep")
            self.output.parent.chmod(0o500)
            try:
                self.assertIn("Permission denied", self.cli(2, force=True))
                if exists: self.assertEqual(self.output.read_bytes(), b"keep")
                else: self.assertFalse(self.output.exists())
            finally: self.output.parent.chmod(0o700)

    def test_no_clobber_race_preserves_concurrent_output(self):
        from unittest.mock import patch
        link = os.link
        def race(temporary, target):
            self.output.write_bytes(b"concurrent output")
            return link(temporary,target)
        with patch("mediawrench.chapters.cli.os.link", side_effect=race): self.cli(2)
        self.assertEqual(self.output.read_bytes(), b"concurrent output")

    def test_success_summary_only_after_real_publication(self):
        from mediawrench.chapters.cli import main
        class ObservingStdout(StringIO):
            def write(stream, text):
                self.assertEqual(loads(self.output.read_text(), "json"), self.doc)
                return super().write(text)
        with redirect_stdout(ObservingStdout()) as stdout:
            self.assertEqual(main(["restore-source",str(self.audit),str(self.output)]),0)
        self.assertEqual(json.loads(stdout.getvalue()), {"chapters":2,"duration_us":3_000_007})


class RestoreLibraryTests(unittest.TestCase):
    def test_schema_must_be_exact_supported_integer(self):
        from decimal import Decimal
        from mediawrench.chapters import ChapterError, restore
        source = loads('{"duration":1,"chapters":[{"start_time":0}]}', "json")
        for schema in (True,False,Decimal("1.0"),Decimal("1e0"),1.0,"1",None,0,2):
            with self.subTest(schema=schema), self.assertRaisesRegex(ChapterError,"schema"):
                restore({"schema_version":schema,"source":source.normalized()})
        self.assertEqual(restore({"schema_version":1,"source":source.normalized()}),source)

    def test_source_requires_object_and_chapter_array_of_objects(self):
        from mediawrench.chapters import ChapterError, restore
        for source in (None,[],"source",{}, {"chapters":{}}, {"chapters":()},
                       {"chapters":[None]}, {"chapters":[[]]}, {"chapters":["bad"]}):
            with self.subTest(source=source), self.assertRaises(ChapterError):
                restore({"schema_version":1,"source":source})
