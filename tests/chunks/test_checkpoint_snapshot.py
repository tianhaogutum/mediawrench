"""Journal headers must bind the checkpoint bytes that supplied the plan."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import split
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json
from .test_snapshot import ReplacingReader
from .test_tool import write_wav

implementation = importlib.import_module("mediawrench.chunks.split")


class CheckpointSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source, self.output = self.root / "source.wav", self.root / "chunks"
        write_wav(self.source, frames=4000)
        self.options = dict(max_seconds="3", overlap_seconds="0", resume=True)
        original = implementation._write_chunk
        calls = []

        def interrupt(*args):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("seed interruption")
            return original(*args)

        with patch.object(implementation, "_write_chunk", interrupt):
            with self.assertRaisesRegex(OSError, "seed interruption"):
                split(self.source, self.output, **self.options)
        self.target, self.journal = self.output / "manifest.json", self.output / "checkpoint.jsonl"
        self.captured = self.target.read_bytes()
        self.replacement = json_bytes(read_json(self.target) | {"max_samples": 2999})

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for p in self.output.iterdir() if p.is_file()}

    def header(self, raw, tail=b""):
        records = self.journal.read_bytes().splitlines(keepends=True)
        record = json.loads(records[0])
        record["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        self.journal.write_bytes(json.dumps(record).encode() + b"\n" + b"".join(records[1:]) + tail)

    def replacement_hook(self, raw):
        original_open, captured_state = Path.open, []

        def replace():
            temporary = self.target.with_suffix(".replacement")
            temporary.write_bytes(raw)
            temporary.replace(self.target)
            # Baseline after the external mutation: the application may not
            # mutate even the replacement manifest when refusing this capture.
            captured_state.append(None)
            captured_state[0] = self.snapshot()

        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return ReplacingReader(stream, replace) if path == self.target and not captured_state else stream

        return patch.object(Path, "open", opened), captured_state

    def cli(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["split", str(self.source), str(self.output), "--max-seconds", "3",
                         "--overlap-seconds", "0", "--resume"])
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_refused(self, replacement=None, error=None):
        if replacement is None:
            before = self.snapshot()
            code, stdout, stderr = self.cli()
        else:
            hook, states = self.replacement_hook(replacement)
            with hook:
                code, stdout, stderr = self.cli()
            self.assertEqual(len(states), 1)
            before = states[0]
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("mediawrench chunks:"))
        if error:
            self.assertIn(error, stderr)
        self.assertEqual(self.snapshot(), before)

    def test_header_matching_only_replacement_cannot_authorize_captured_plan(self):
        self.header(self.replacement, tail=b'{"chunk_id":')
        self.assert_refused(error="binding mismatch")
        self.assert_refused(self.replacement, error="binding mismatch")

    def test_matching_captured_header_recovers_despite_replacement(self):
        self.header(self.captured, tail=b'{"chunk_id":')
        before = self.snapshot()
        hook, states = self.replacement_hook(self.replacement)
        with hook:
            code, _, stderr = self.cli()
        self.assertEqual(len(states), 1)
        self.assertEqual(code, 0, stderr)
        result = read_json(self.target)
        self.assertTrue(result["complete"])
        self.assertEqual(result["max_samples"], 3000)
        self.assertEqual(result["chunks"][0]["end_sample"], 3000)
        self.assertEqual(self.snapshot()["chunk-000001.wav"], before["chunk-000001.wav"])
        self.assertTrue(self.journal.read_bytes().endswith(b"\n"))
        completed = self.snapshot()
        with patch.object(implementation, "_write_chunk", side_effect=AssertionError("must reuse")):
            self.assertEqual(split(self.source, self.output, **self.options), result)
        self.assertEqual(self.snapshot(), completed)

    def test_captured_whitespace_is_part_of_header_identity(self):
        self.target.write_bytes(self.captured + b" ")
        self.assert_refused(self.captured, error="binding mismatch")
        self.target.write_bytes(self.captured + b" ")
        self.header(self.captured + b" ")
        hook, states = self.replacement_hook(self.captured)
        with hook:
            code, _, stderr = self.cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(states), 1)

    def test_invalid_or_mismatched_capture_cannot_be_rescued(self):
        invalid = (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                   b'{"x":"\xff"}', b'{"x":"\\ud800"}', b'{', b'[]', self.replacement)
        self.header(self.captured, tail=b'{"chunk_id":')
        for raw in invalid:
            with self.subTest(raw=raw):
                self.target.write_bytes(raw)
                self.assert_refused(self.captured)


if __name__ == "__main__":
    unittest.main()
