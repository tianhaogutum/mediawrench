"""Supplied-silence geometry and resume identity must describe the same bytes."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import split
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import read_json
from mediawrench.chunks.split import supplied_silences
from .test_snapshot import ReplacingReader
from .test_tool import write_wav

implementation = importlib.import_module("mediawrench.chunks.split")
A = b' \r\n{"intervals":[{"start":1,"end":2}]}\r\n '
B = b'{"intervals":[{"start":2,"end":3}]}\n'


class SilenceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source, self.silence = self.root / "source.wav", self.root / "silence.json"
        write_wav(self.source, frames=4000)
        self.silence.write_bytes(A)
        self.options = dict(max_seconds="3", overlap_seconds="0", search_seconds="2",
                            silence_json=self.silence, resume=True)
        self.output = self.root / "chunks"

    def replacement(self, raw):
        original_open, fired = Path.open, []

        def replace():
            if not fired:
                fired.append(True)
                temporary = self.silence.with_suffix(".replacement")
                temporary.write_bytes(raw)
                temporary.replace(self.silence)

        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return ReplacingReader(stream, replace) if path == self.silence and not fired else stream

        return patch.object(Path, "open", opened), fired

    def snapshot(self):
        return {str(p.relative_to(self.output)): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for p in self.output.rglob("*") if p.is_file()} if self.output.exists() else {}

    def prepare(self, state):
        if state == "fresh":
            return
        if state == "complete":
            split(self.source, self.output, **self.options)
            return
        original = implementation._write_chunk
        calls = []

        def fail_second(*args):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("synthetic interruption")
            return original(*args)

        with patch.object(implementation, "_write_chunk", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "synthetic interruption"):
                split(self.source, self.output, **self.options)
        self.assertFalse(read_json(self.output / "manifest.json")["complete"])

    def cli(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["split", str(self.source), str(self.output), "--max-seconds", "3",
                         "--overlap-seconds", "0", "--search-seconds", "2",
                         "--silence-json", str(self.silence), "--resume"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_replacement_binds_parsed_intervals_and_digest_then_reuses(self):
        hook, fired = self.replacement(B)
        with hook:
            manifest = split(self.source, self.output, **self.options)
        self.assertEqual(fired, [True])
        self.assertEqual(self.silence.read_bytes(), B)
        self.assertEqual(manifest["silence"]["intervals_samples"], [[1000, 2000]])
        self.assertEqual(manifest["chunks"][0]["end_sample"], 1500)
        self.assertEqual(manifest["split_parameters"]["silence_json_sha256"], hashlib.sha256(A).hexdigest())
        self.assertEqual(read_json(self.output / "manifest.json"), manifest)
        self.silence.write_bytes(A)
        before = self.snapshot()
        with patch.object(implementation, "_write_chunk", side_effect=AssertionError("must reuse")):
            self.assertEqual(split(self.source, self.output, **self.options), manifest)
        self.assertEqual(self.snapshot(), before)

    def test_matching_replacement_on_incomplete_and_complete_resume(self):
        for state in ("incomplete", "complete"):
            with self.subTest(state=state):
                self.output = self.root / state
                self.silence.write_bytes(A)
                self.prepare(state)
                before = self.snapshot()
                hook, fired = self.replacement(B)
                with hook:
                    code, _, stderr = self.cli()
                self.assertEqual(code, 0, stderr)
                self.assertEqual(fired, [True])
                manifest = read_json(self.output / "manifest.json")
                self.assertTrue(manifest["complete"])
                self.assertEqual(manifest["split_parameters"]["silence_json_sha256"], hashlib.sha256(A).hexdigest())
                after = self.snapshot()
                for name in before:
                    if name.endswith(".wav") or state == "complete":
                        self.assertEqual(after[name], before[name])

    def test_invalid_or_mismatched_capture_never_mutates_outputs(self):
        invalid = (b'{"intervals":[],"intervals":[]}', b'{"intervals":[],"x":NaN}',
                   b'{"intervals":[],"x":1e999}', b'{"intervals":[],"x":"\xff"}',
                   b'{"intervals":[],"x":"\\ud800"}', b'{', b'[]',
                   b'{"intervals":[{"start":2,"end":1}]}')
        for state in ("fresh", "incomplete", "complete"):
            self.output = self.root / state
            self.silence.write_bytes(A)
            self.prepare(state)
            captures = invalid + ((B, A + b" ") if state != "fresh" else ())
            for raw in captures:
                with self.subTest(state=state, raw=raw):
                    self.silence.write_bytes(raw)
                    before = self.snapshot()
                    hook, fired = self.replacement(A)
                    with hook:
                        code, stdout, stderr = self.cli()
                    self.assertEqual(code, 2, stderr)
                    self.assertEqual(stdout, "")
                    self.assertTrue(stderr.startswith("mediawrench chunks:"))
                    self.assertEqual(fired, [True])
                    self.assertEqual(self.silence.read_bytes(), A)
                    self.assertEqual(self.snapshot(), before)
                    if state == "fresh":
                        self.assertFalse(self.output.exists())
                    self.assertFalse(list(self.root.glob(f".{state}-*")))

    def test_value_only_helper_preserves_exact_inward_rounding(self):
        self.silence.write_bytes(b'{"intervals":[{"start":1.00000000000000001,"end":2.00099999999999999}]}')
        self.assertEqual(supplied_silences(self.silence, 1000, 4000), [(1001, 2000)])
        manifest = split(self.source, self.output, **self.options)
        self.assertEqual(manifest["silence"]["intervals_samples"], [[1001, 2000]])
        self.assertEqual(manifest["split_parameters"]["silence_json_sha256"], hashlib.sha256(self.silence.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
