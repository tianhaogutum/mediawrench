"""Replacement after a read must not separate JSON values from their digest."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import split, stitch
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json, read_json_snapshot
from .test_tool import write_wav


class ReplacingReader:
    """Replace a pathname after the first read, retaining the open old inode."""
    def __init__(self, stream, replace):
        self.stream, self.replace = stream, replace

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, *args):
        data = self.stream.read(*args)
        self.replace()
        return data


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = self.root / "source.wav"
        write_wav(source, frames=4000)
        (self.root / "alternate.wav").write_bytes(source.read_bytes())
        self.manifest = split(source, self.root / "chunks", max_seconds="4", overlap_seconds="0")
        self.manifest_path = self.root / "chunks/manifest.json"
        self.manifest_raw = self.manifest_path.read_bytes()
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.transcript_path = self.transcripts / "chunk-000001.json"
        self.data = {
            "recording_sha256": self.manifest["source"]["sha256"],
            "manifest_sha256": self.digest(self.manifest_raw),
            "chunk_id": "chunk-000001", "audio_sha256": self.manifest["chunks"][0]["sha256"],
            "confidence": Decimal("0.123456789012345678901"),
            "segments": [{"start": Decimal("3.09999999999999999"),
                          "end": Decimal("3.50000000000000001"), "text": "original"}]}

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    def replace_after_read(self, target, replacement):
        original_open = Path.open
        fired = []

        def replace():
            if not fired:
                fired.append(True)
                temporary = target.with_suffix(".replacement")
                temporary.write_bytes(replacement)
                temporary.replace(target)

        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == target and not fired:
                return ReplacingReader(stream, replace)
            return stream

        return patch.object(Path, "open", opened), fired

    def run_replacement(self, which, native_json):
        self.manifest_path.write_bytes(self.manifest_raw)
        data = {"segments": self.data["segments"], "confidence": self.data["confidence"]} if native_json else self.data
        # Whitespace/CRLF must contribute to hashes; never hash reserialized JSON.
        raw = b" \r\n" + json_bytes(data).replace(b"\n", b"\r\n") + b"  "
        self.transcript_path.write_bytes(raw)
        if which == "manifest":
            target, old = self.manifest_path, self.manifest_raw
            replacement = json_bytes(self.manifest | {
                "source": self.manifest["source"] | {"path": "../alternate.wav"}})
        else:
            target, old = self.transcript_path, raw
            replacement = raw.replace(b'"original"', b'"replacement"')
        output = self.root / f"{which}-{native_json}.json"
        replacement_hook, fired = self.replace_after_read(target, replacement)
        with replacement_hook:
            result = stitch(self.manifest_path, self.transcripts, output, native_json=native_json)
        self.assertEqual(fired, [True])
        self.assertEqual(target.read_bytes(), replacement)
        self.assertNotEqual(self.digest(old), self.digest(replacement))
        self.assertEqual(result["source"], self.manifest["source"])
        self.assertEqual(result["manifest_sha256"], self.digest(self.manifest_raw))
        self.assertEqual(result["transcripts"][0]["sha256"], self.digest(raw))
        segment = result["segments"][0]
        self.assertEqual((segment["start_sample"], segment["end_sample"], segment["text"]),
                         (3099, 3501, "original"))
        self.assertEqual(segment["origins"][0]["original"], self.data["segments"][0])
        self.assertEqual(result["transcripts"][0]["metadata"]["confidence"], self.data["confidence"])
        self.assertEqual(json_bytes(read_json(output)), json_bytes(result))
        self.assertEqual(read_json(output.with_suffix(".report.json")), result["report"])
        self.assertIn("00:00:03,099 --> 00:00:03,501\noriginal", output.with_suffix(".srt").read_text())

    def test_manifest_replacement(self):
        for native in (False, True):
            with self.subTest(native_json=native):
                self.run_replacement("manifest", native)

    def test_transcript_replacement(self):
        for native in (False, True):
            with self.subTest(native_json=native):
                self.run_replacement("transcript", native)

    def test_snapshot_digest_retains_utf8_whitespace_and_exact_numbers(self):
        raw = ' \r\n{"text":"世界", "x":1.00000000000000001, "tiny":1e-999}\r\n'.encode()
        self.transcript_path.write_bytes(raw)
        value, digest = read_json_snapshot(self.transcript_path)
        self.assertEqual(value, {"text": "世界", "x": Decimal("1.00000000000000001"),
                                 "tiny": Decimal("1e-999")})
        self.assertEqual(digest, self.digest(raw))
        self.assertNotEqual(digest, self.digest(json_bytes(value)))

    def test_invalid_captured_json_cannot_be_rescued_by_valid_replacement(self):
        for native in (False, True):
            for which in ("manifest", "transcript"):
                for bad in (b'{"unused":{"x":1,"x":2}}', b'{"unused":NaN}',
                            b'{"unused":1e999}', b'{"unused":"\xff"}',
                            b'{"unused":"\\ud800"}', b'{'):
                    with self.subTest(native_json=native, which=which, bad=bad):
                        self.manifest_path.write_bytes(self.manifest_raw)
                        raw = json_bytes(self.data)
                        self.transcript_path.write_bytes(raw)
                        target = self.manifest_path if which == "manifest" else self.transcript_path
                        replacement = self.manifest_raw if which == "manifest" else raw
                        target.write_bytes(bad)
                        self.assert_rejected_replacement(target, replacement, native)

    def test_captured_identity_mismatch_cannot_be_rescued(self):
        for native in (False, True):
            with self.subTest(native_json=native):
                self.transcript_path.write_bytes(json_bytes(self.data | {"manifest_sha256": "0" * 64}))
                self.assert_rejected_replacement(self.transcript_path, json_bytes(self.data), native,
                                                 error="manifest_sha256 mismatch")

    def assert_rejected_replacement(self, target, replacement, native, error="Cannot read JSON"):
        output = self.root / "rejected.json"
        replacement_hook, fired = self.replace_after_read(target, replacement)
        stdout, stderr = io.StringIO(), io.StringIO()
        with replacement_hook, redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["stitch", str(self.manifest_path), str(self.transcripts), str(output)]
                        + (["--native-json"] if native else []))
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(fired, [True])
        self.assertEqual(target.read_bytes(), replacement)
        self.assertIn(error, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        for suffix in (".json", ".srt", ".report.json"):
            self.assertFalse(output.with_suffix(suffix).exists())
        self.assertFalse(list(self.root.glob(".acm-*")))


if __name__ == "__main__":
    unittest.main()
