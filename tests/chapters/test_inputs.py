"""Offline saved-result byte binding; synthetic bytes are not media evidence."""
import hashlib
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def fingerprint(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


class CheckInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "local.mkv"
        self.audit = self.root / "audit.json"
        self.verification = self.root / "verification.json"
        self.output = self.root / "binding.json"
        self.media.write_bytes(b"abc")
        self.audit.write_bytes(b'{"audit": "synthetic"}\r\n')
        self.saved = {"schema_version": 1, "status": "skipped", "equal": None,
                      "media": "/old/location/local.mkv",
                      "input_fingerprints": {"media": fingerprint(self.media.read_bytes()),
                                             "report": fingerprint(self.audit.read_bytes())}}
        self.save()

    def save(self):
        self.verification.write_text(json.dumps(self.saved), encoding="utf-8")

    def argv(self):
        return ["check-inputs", str(self.media), "--report", str(self.audit),
                "--verification", str(self.verification)]

    def cli(self, expected=0, *, extra=()):
        from mediawrench.chapters.cli import main
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             patch("mediawrench.chapters.media.verify_media") as verify, \
             patch("mediawrench.chapters.media.shutil.which") as which, \
             patch("mediawrench.chapters.media.subprocess.run") as run:
            try:
                code = main([*self.argv(), *map(str, extra)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expected, stdout.getvalue() + stderr.getvalue())
        for mock in (verify, which, run):
            mock.assert_not_called()
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(list(self.root.glob(".chapter-roundtrip-*")))
        self.stdout = stdout.getvalue()
        if expected == 2:
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue())
            return stderr.getvalue()
        self.assertEqual(stderr.getvalue(), "")
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["kind"], "input-byte-binding")
        self.assertEqual(result["status"], "passed" if expected == 0 else "different")
        self.assertIs(result["equal"], expected == 0)
        return result

    def test_output_preserves_all_saved_states_for_equal_and_different_bytes(self):
        inputs = (self.media, self.audit, self.verification)
        for state, equal in (("passed", True), ("different", False), ("skipped", None)):
            self.saved.update(status=state, equal=equal)
            self.save()
            for changed in (None, self.media, self.audit):
                with self.subTest(state=state, changed=changed):
                    originals = {p: p.read_bytes() for p in inputs}
                    if changed:
                        changed.write_bytes(originals[changed] + b"\x00\xff")
                    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inputs}
                    result = self.cli(1 if changed else 0, extra=("--output", self.output))
                    self.assertEqual(result["saved_verification"], {"status": state, "equal": equal})
                    self.assertEqual(self.output.read_bytes(), self.stdout.encode("utf-8"))
                    saved_stdout = self.stdout
                    self.assertEqual(self.cli(1 if changed else 0), result)
                    self.assertEqual(self.stdout, saved_stdout)
                    self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in inputs}, before)
                    self.output.unlink()
                    if changed:
                        changed.write_bytes(originals[changed])

    def test_output_is_published_before_stdout(self):
        from mediawrench.chapters.cli import main
        writes = []

        class ObservedStdout:
            def write(stream, text):
                self.assertEqual(self.output.read_bytes(), text.encode("utf-8"))
                writes.append(text)

        with redirect_stdout(ObservedStdout()):
            code = main([*self.argv(), "--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(len(writes), 1)

    def test_existing_output_requires_force_and_atomic_replacement(self):
        self.output.write_bytes(b"keep")
        old_link = self.root / "old-binding.json"
        old_link.hardlink_to(self.output)
        error = self.cli(2, extra=("--output", self.output))
        self.assertIn("use --force", error)
        self.assertEqual(self.output.read_bytes(), b"keep")
        self.cli(extra=("--output", self.output, "--force"))
        self.assertEqual(self.output.read_bytes(), self.stdout.encode("utf-8"))
        self.assertEqual(old_link.read_bytes(), b"keep")
        self.assertFalse(self.output.samefile(old_link))

    def test_force_requires_output_before_reading_inputs(self):
        with patch("mediawrench.chapters.cli._read") as read, \
             patch("mediawrench.chapters.cli._hash_input") as hash_input:
            self.assertIn("--force requires --output", self.cli(2, extra=("--force",)))
        read.assert_not_called()
        hash_input.assert_not_called()

    def test_output_cannot_alias_any_input_even_with_force(self):
        originals = {p: p.read_bytes() for p in (self.media, self.audit, self.verification)}
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        for source in originals:
            for kind in ("same", "normalized", "parent-symlink", "hardlink", "symlink"):
                target = {"same": source,
                          "normalized": self.root / ".." / self.root.name / source.name,
                          "parent-symlink": parent_alias / source.name}.get(kind, self.output)
                if kind == "hardlink":
                    target.hardlink_to(source)
                elif kind == "symlink":
                    target.symlink_to(source)
                for force in ((), ("--force",)):
                    with self.subTest(source=source, kind=kind, force=force), \
                         patch("mediawrench.chapters.cli._hash_input") as hash_input:
                        self.cli(2, extra=("--output", target, *force))
                        hash_input.assert_not_called()
                        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
                if kind in ("hardlink", "symlink"):
                    target.unlink()

    def test_output_symlinks_to_unrelated_or_missing_files_are_refused(self):
        other = self.root / "other.json"
        for exists in (False, True):
            if exists:
                other.write_bytes(b"keep")
            self.output.symlink_to(other)
            for force in ((), ("--force",)):
                with self.subTest(exists=exists, force=force):
                    self.assertIn("symlink", self.cli(2, extra=("--output", self.output, *force)))
                    self.assertTrue(self.output.is_symlink())
                    self.assertEqual(other.exists(), exists)
                    if exists:
                        self.assertEqual(other.read_bytes(), b"keep")
            self.output.unlink()

    def test_input_aliases_remain_allowed_with_output(self):
        # Only output/input aliases are forbidden: the byte inputs may coincide.
        self.audit = self.media
        self.saved["input_fingerprints"]["report"] = fingerprint(self.media.read_bytes())
        self.save()
        self.cli(extra=("--output", self.output))

    def test_invalid_saved_fields_and_json_never_publish(self):
        original = self.verification.read_bytes()
        invalid = [b"{", b"\xff", b'{"schema_version":1,"schema_version":1}',
                   b'{"unknown":NaN}', b'{"unknown":Infinity}',
                   original.replace(b'"size_bytes": 3', b'"size_bytes": 3e0')]
        for field, value in (("schema_version", True), ("equal", True),
                              ("input_fingerprints", {})):
            invalid.append(json.dumps({**self.saved, field: value}).encode())
        for raw in invalid:
            self.verification.write_bytes(raw)
            for exists in (False, True):
                with self.subTest(raw=raw, exists=exists):
                    if exists:
                        self.output.write_bytes(b"keep")
                    self.cli(2, extra=("--output", self.output, "--force"))
                    if exists:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else:
                        self.assertFalse(self.output.exists())

    def test_missing_directory_and_real_unreadable_inputs_never_publish(self):
        # The normal unprivileged CLI must encounter actual EACCES, not a mock.
        self.assertNotEqual(os.geteuid(), 0, "run permission contracts unprivileged")
        self.media.write_bytes(b"changed")  # Difference cannot hide later read errors.
        for source in (self.media, self.audit, self.verification):
            raw = source.read_bytes()
            for failure in ("missing", "directory", "unreadable"):
                if failure == "unreadable":
                    source.chmod(0)
                else:
                    source.unlink()
                    if failure == "directory":
                        source.mkdir()
                try:
                    for exists in (False, True):
                        with self.subTest(source=source, failure=failure, exists=exists):
                            if exists:
                                self.output.write_bytes(b"keep")
                            error = self.cli(2, extra=("--output", self.output, "--force"))
                            if failure == "unreadable":
                                self.assertIn("Permission denied", error)
                            if exists:
                                self.assertEqual(self.output.read_bytes(), b"keep")
                                self.output.unlink()
                            else:
                                self.assertFalse(self.output.exists())
                finally:
                    if failure == "unreadable":
                        source.chmod(0o600)
                    else:
                        if failure == "directory":
                            source.rmdir()
                        source.write_bytes(raw)

    def test_partial_input_read_errors_never_publish(self):
        original_open = Path.open
        self.media.write_bytes(b"changed")
        for source in (self.media, self.audit, self.verification):
            for exists in (False, True):
                with self.subTest(source=source, exists=exists):
                    if exists:
                        self.output.write_bytes(b"keep")

                    class FailingReader(BytesIO if source != self.verification else StringIO):
                        def read(reader, size=-1):
                            if source == self.verification or reader.tell():
                                raise OSError("synthetic input read failure")
                            return super().read(1)

                    def broken_open(path, *args, **kwargs):
                        if path == source:
                            return FailingReader("{" if source == self.verification else b"a")
                        return original_open(path, *args, **kwargs)

                    with patch.object(Path, "open", broken_open):
                        self.cli(2, extra=("--output", self.output, "--force"))
                    if exists:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else:
                        self.assertFalse(self.output.exists())

    def test_staging_and_publication_failures_preserve_destination_and_clean_temps(self):
        missing = self.root / "missing" / "binding.json"
        self.cli(2, extra=("--output", missing))
        self.assertFalse(missing.parent.exists())
        for function in ("fsync", "link", "replace"):
            for exists in (False, True):
                # link is the no-clobber branch, which refuses existing files early.
                if function == "link" and exists:
                    continue
                with self.subTest(function=function, exists=exists):
                    if exists:
                        self.output.write_bytes(b"keep")
                    force = () if function == "link" else ("--force",)
                    with patch("mediawrench.chapters.cli.os." + function,
                               side_effect=OSError("synthetic publication I/O failure")):
                        self.cli(2, extra=("--output", self.output, *force))
                    if exists:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else:
                        self.assertFalse(self.output.exists())

    def test_staged_write_and_flush_errors_clean_temps(self):
        create = tempfile.NamedTemporaryFile
        for operation in ("write", "flush"):
            for exists in (False, True):
                with self.subTest(operation=operation, exists=exists):
                    if exists:
                        self.output.write_bytes(b"keep")

                    def fail_stage(*args, **kwargs):
                        stream = create(*args, **kwargs)
                        setattr(stream, operation, unittest.mock.Mock(
                            side_effect=OSError("synthetic staged I/O failure")))
                        return stream

                    with patch("mediawrench.chapters.cli.tempfile.NamedTemporaryFile", fail_stage):
                        self.cli(2, extra=("--output", self.output, "--force"))
                    if exists:
                        self.assertEqual(self.output.read_bytes(), b"keep")
                        self.output.unlink()
                    else:
                        self.assertFalse(self.output.exists())

    def test_no_clobber_when_output_appears_during_publication(self):
        link = os.link

        def race(temporary, target):
            self.output.write_bytes(b"concurrent result")
            return link(temporary, target)

        with patch("mediawrench.chapters.cli.os.link", side_effect=race):
            self.cli(2, extra=("--output", self.output))
        self.assertEqual(self.output.read_bytes(), b"concurrent result")

    def test_real_unwritable_output_parent_preserves_existing_result(self):
        self.assertNotEqual(os.geteuid(), 0, "run permission contracts unprivileged")
        destination = self.root / "unwritable"
        destination.mkdir()
        self.output = destination / "binding.json"
        for exists in (False, True):
            if exists:
                self.output.write_bytes(b"keep")
            destination.chmod(0o500)
            try:
                self.assertIn("Permission denied", self.cli(
                    2, extra=("--output", self.output, "--force")))
                self.assertFalse(list(destination.glob(".chapter-roundtrip-*")))
                if exists:
                    self.assertEqual(self.output.read_bytes(), b"keep")
                else:
                    self.assertFalse(self.output.exists())
            finally:
                destination.chmod(0o700)

    def test_cli_unchanged_known_bytes_pass_without_probe(self):
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", *self.argv()],
                                cwd=ROOT, env={**os.environ, "PATH": ""},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {
            "schema_version": 1, "kind": "input-byte-binding",
            "scope": "SHA-256 and byte length equality only; no chapter verification performed",
            "status": "passed", "equal": True,
            "saved_verification": {"status": "skipped", "equal": None},
            "checks": {"media": {"equal": True,
                                 "expected": {"sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                                              "size_bytes": 3},
                                 "actual": self.saved["input_fingerprints"]["media"]},
                       "report": {"equal": True,
                                  "expected": self.saved["input_fingerprints"]["report"],
                                  "actual": self.saved["input_fingerprints"]["report"]}}})
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_cli_output_saves_exact_stdout_bytes_without_probe(self):
        output = self.root / "binding.json"
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", *self.argv(),
                                 "--output", str(output)], cwd=ROOT,
                                env={**os.environ, "PATH": ""},
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(output.read_bytes(), result.stdout)
        self.assertEqual(json.loads(result.stdout)["saved_verification"],
                         {"status": "skipped", "equal": None})
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_same_length_binary_change_and_audit_formatting_change_are_differences(self):
        original = self.audit.read_bytes()
        self.media.write_bytes(b"a\x00\xff")
        result = self.cli(1)
        self.assertEqual(result["checks"]["media"], {
            "expected": self.saved["input_fingerprints"]["media"],
            "actual": fingerprint(b"a\x00\xff"), "equal": False})
        self.assertTrue(result["checks"]["report"]["equal"])
        self.media.write_bytes(b"abc")
        # Same JSON meaning; only the observed audit bytes differ.
        changed = original + b" \n"
        self.audit.write_bytes(changed)
        self.assertEqual(json.loads(original), json.loads(changed))
        result = self.cli(1)
        self.assertTrue(result["checks"]["media"]["equal"])
        self.assertEqual(result["checks"]["report"], {
            "expected": self.saved["input_fingerprints"]["report"],
            "actual": fingerprint(changed), "equal": False})
        self.media.write_bytes(b"longer")
        result = self.cli(1)
        self.assertFalse(any(c["equal"] for c in result["checks"].values()))

    def test_digest_and_length_are_independently_required_for_both_inputs(self):
        original = copy.deepcopy(self.saved)
        for name in ("media", "report"):
            for field, value in (("sha256", "0" * 64), ("size_bytes", 999)):
                with self.subTest(name=name, field=field):
                    self.saved = copy.deepcopy(original)
                    self.saved["input_fingerprints"][name][field] = value
                    self.save()
                    result = self.cli(1)
                    self.assertFalse(result["checks"][name]["equal"])
                    self.assertEqual(result["checks"][name]["expected"],
                                     self.saved["input_fingerprints"][name])

    def test_relocated_identical_inputs_ignore_recorded_paths(self):
        moved = self.root / "moved"
        moved.mkdir()
        for attr in ("media", "audit", "verification"):
            old = getattr(self, attr)
            new = moved / old.name
            old.rename(new)
            setattr(self, attr, new)
            self.assertFalse(old.exists())
        self.cli()

    def test_empty_and_multichunk_binary_inputs_hash_in_bounded_reads(self):
        self.media.write_bytes(b"")
        self.saved["input_fingerprints"]["media"] = {
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "size_bytes": 0}
        self.save()
        self.cli()
        binary = bytes(range(256)) * 9000 + b"\x00\xff\r\n"
        raw_audit = self.audit.read_bytes() + b" " * (1024 * 1024 + 17)
        self.media.write_bytes(binary)
        self.audit.write_bytes(raw_audit)
        self.saved["input_fingerprints"] = {"media": fingerprint(binary), "report": fingerprint(raw_audit)}
        self.save()
        original_open = Path.open
        reads = {self.media: [], self.audit: []}
        opens = {self.media: 0, self.audit: 0}

        class BoundedReader:
            def __init__(reader, stream, path):
                reader.stream, reader.path = stream, path

            def __enter__(reader):
                reader.stream.__enter__()
                return reader

            def __exit__(reader, *args):
                return reader.stream.__exit__(*args)

            def read(reader, size=-1):
                self.assertGreater(size, 0, "unbounded byte-binding input read")
                self.assertLessEqual(size, 1024 * 1024)
                chunk = reader.stream.read(size)
                reads[reader.path].append(len(chunk))
                return chunk

        def guarded_open(path, mode="r", *args, **kwargs):
            self.assertIn(mode, ("r", "rb"), "check-inputs must never write files")
            stream = original_open(path, mode, *args, **kwargs)
            if path in reads:
                self.assertEqual(mode, "rb")
                opens[path] += 1
                return BoundedReader(stream, path)
            return stream

        with patch.object(Path, "open", guarded_open):
            result = self.cli()
        for path, name, raw in ((self.media, "media", binary), (self.audit, "report", raw_audit)):
            self.assertEqual(result["checks"][name]["actual"], fingerprint(raw))
            self.assertEqual(opens[path], 1)
            self.assertGreater(len(reads[path]), 2)
            self.assertEqual(sum(reads[path]), len(raw))

    def test_actual_saved_passed_different_skipped_results_keep_original_state(self):
        from mediawrench.chapters import dumps, loads
        from mediawrench.chapters.cli import main
        audit = dumps(loads('{"duration":1,"chapters":[{"start_time":0,"title":"A"}]}',
                            "json"), "ffmetadata").report
        self.audit.write_text(json.dumps(audit), encoding="utf-8")
        chapters = [{"time_base": "1/1", "start": 0, "end": 1, "tags": {"title": "A"}}]
        for state, equal, expected, executable, data in (
                ("passed", True, 0, "/synthetic/probe", {"chapters": chapters}),
                ("different", False, 1, "/synthetic/probe", {"chapters": []}),
                ("skipped", None, 3, None, {})):
            with self.subTest(state=state):
                self.media.write_bytes(b"abc")
                stdout = StringIO()
                with redirect_stdout(stdout), \
                     patch("mediawrench.chapters.media.shutil.which", return_value=executable), \
                     patch("mediawrench.chapters.media.subprocess.run", return_value=
                           subprocess.CompletedProcess([], 0, json.dumps(data), "")):
                    code = main(["verify-media", str(self.media), "--report", str(self.audit),
                                 "--ffprobe", "synthetic-probe", "--hash-inputs", "--output",
                                 str(self.verification), "--force"])
                self.assertEqual(code, expected)
                saved_bytes = self.verification.read_bytes()
                self.assertEqual(saved_bytes, stdout.getvalue().encode("utf-8"))
                self.assertEqual(json.loads(saved_bytes)["status"], state)
                result = self.cli()
                self.assertEqual(result["saved_verification"], {"status": state, "equal": equal})
                self.assertEqual(self.verification.read_bytes(), saved_bytes)
                self.media.write_bytes(b"xyz")
                result = self.cli(1)
                self.assertEqual(result["saved_verification"], {"status": state, "equal": equal})

    def test_missing_or_invalid_fingerprints_fail_before_hashing(self):
        original = copy.deepcopy(self.saved)
        invalid = [None, [], "digest", True, 12]
        cases = [dict(original, input_fingerprints=value) for value in invalid]
        cases.append({k: v for k, v in original.items() if k != "input_fingerprints"})
        for name in ("media", "report"):
            for value in [*invalid, {}, {"sha256": "0" * 64}, {"size_bytes": 0}]:
                saved = copy.deepcopy(original)
                saved["input_fingerprints"][name] = value
                cases.append(saved)
            saved = copy.deepcopy(original)
            del saved["input_fingerprints"][name]
            cases.append(saved)
            for field, values in (
                    ("sha256", [None, False, 123, [], {}, "", "a" * 63, "a" * 65,
                                "A" * 64, "g" * 64, "a" * 63 + "\n", "é" * 64]),
                    ("size_bytes", [None, False, True, -1, 3.0, 3.5, "3", [], {}])):
                for value in values:
                    saved = copy.deepcopy(original)
                    saved["input_fingerprints"][name][field] = value
                    cases.append(saved)
        for saved in cases:
            with self.subTest(saved=saved):
                self.saved = saved
                self.save()
                with patch("mediawrench.chapters.cli._hash_input") as hash_input:
                    self.assertIn("input_fingerprints", self.cli(2))
                hash_input.assert_not_called()

    def test_saved_schema_and_status_require_supported_canonical_values(self):
        original = copy.deepcopy(self.saved)
        for field, values in (("schema_version", [None, False, True, 0, 2, 1.0, "1", [], {}]),
                              ("status", [None, True, "unknown", [], {}]),
                              ("equal", [False, True, 0, 1, "null", [], {}])):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.saved = copy.deepcopy(original)
                    self.saved[field] = value
                    self.save()
                    self.cli(2)
            self.saved = copy.deepcopy(original)
            del self.saved[field]
            self.save()
            self.cli(2)
        for raw in (b"null", b"[]", b"1", b"true", b"{}"):
            self.verification.write_bytes(raw)
            self.cli(2)

    def test_saved_json_errors_and_noncanonical_exponent_size_fail(self):
        original = self.verification.read_bytes()
        for raw in (b"{", b"\xff", b'{"schema_version":1,"schema_version":1}',
                    b'{"unknown":NaN}', b'{"unknown":Infinity}',
                    original.replace(b'"size_bytes": 3', b'"size_bytes": 3e0')):
            with self.subTest(raw=raw):
                self.verification.write_bytes(raw)
                self.cli(2)

    def test_missing_and_directory_inputs_fail_with_empty_stdout(self):
        for path in (self.media, self.audit, self.verification):
            with self.subTest(path=path):
                raw = path.read_bytes()
                path.unlink()
                self.cli(2)
                self.assertFalse(path.exists())
                path.mkdir()
                self.cli(2)
                path.rmdir()
                path.write_bytes(raw)

    def test_permission_and_partial_read_errors_in_any_input_have_no_stdout(self):
        original_open = Path.open
        # Include a media difference before the audit fails: never emit partial JSON.
        self.media.write_bytes(b"changed")
        for target in (self.media, self.audit, self.verification):
            for partial in (False, True):
                with self.subTest(target=target, partial=partial):
                    class FailingReader(BytesIO if target != self.verification else StringIO):
                        def read(reader, size=-1):
                            if reader.tell():
                                raise OSError("synthetic partial-read failure")
                            if target == self.verification:
                                raise OSError("synthetic saved JSON read failure")
                            return super().read(1)

                    def broken_open(path, *args, **kwargs):
                        if path == target:
                            if not partial:
                                raise PermissionError("synthetic permission failure")
                            return FailingReader("{" if target == self.verification else b"a")
                        return original_open(path, *args, **kwargs)

                    with patch.object(Path, "open", broken_open):
                        self.cli(2)

    def test_required_arguments_and_probe_option_are_rejected(self):
        for argv in (["check-inputs", str(self.media)], self.argv()[:-2],
                     self.argv() + ["--ffprobe", "ffprobe"]):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", *argv],
                                        cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
