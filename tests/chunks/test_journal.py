"""Journal commits are bounded, ordered, and recover only a crash tail."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from mediawrench.chunks import ManifestError, split
from mediawrench.chunks.common import json_bytes, read_json, sha256

implementation = importlib.import_module("mediawrench.chunks.split")


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source.wav"
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((1, 2, 1000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(range(128)) * 2)
        self.output = self.root / "chunks"
        self.options = dict(max_seconds="0.032", overlap_seconds="0")

    def interrupt(self, output=None):
        original = implementation._write_chunk
        count = 0

        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("interrupted")
            return original(*args)

        with patch.object(implementation, "_write_chunk", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "interrupted"):
                split(self.source, output or self.output, resume=True, **self.options)
        return read_json((output or self.output) / "manifest.json")

    def snapshot(self, output=None):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                for p in (output or self.output).iterdir()}

    def test_fixed_chunk_count_gradient_writes_plan_only_twice(self):
        rows = []
        for count in (8, 32, 128):
            output = self.root / f"gradient-{count}"
            options = dict(max_seconds=str(0.128 / count), overlap_seconds="0")
            writes = []
            original = implementation._checkpoint

            def measured(directory, manifest, **kwargs):
                writes.append(len(json_bytes(manifest)))
                return original(directory, manifest, **kwargs)

            with patch.object(implementation, "_checkpoint", side_effect=measured):
                manifest = split(self.source, output, resume=True, **options)
            self.assertEqual(len(manifest["chunks"]), count)
            journal = output / "checkpoint.jsonl"
            journal_bytes = journal.stat().st_size if journal.exists() else 0
            rows.append(dict(chunks=count, plan_writes=len(writes), plan_bytes=sum(writes),
                             journal_bytes=journal_bytes, checkpoint_bytes=sum(writes) + journal_bytes))
        print("checkpoint_gradient=" + json.dumps(rows), flush=True)
        self.assertEqual([r["plan_writes"] for r in rows], [2, 2, 2])
        for left, right in zip(rows, rows[1:]):
            self.assertLess(right["checkpoint_bytes"], 5 * left["checkpoint_bytes"])

    def test_base_manifest_stays_immutable_until_completion(self):
        base = self.interrupt()
        self.assertEqual(base["checkpoint_format"], "jsonl-v1")
        self.assertTrue(all(c["sha256"] is None for c in base["chunks"]))
        raw = (self.output / "manifest.json").read_bytes()
        lines = (self.output / "checkpoint.jsonl").read_bytes().splitlines()
        self.assertEqual(json.loads(lines[0]), {"format": "audio-chunk-manifest-journal-v1",
                                              "manifest_sha256": sha256(self.output / "manifest.json")})
        self.assertEqual(json.loads(lines[1]), {"chunk_id": "chunk-000001",
                                              "sha256": sha256(self.output / "chunk-000001.wav")})
        with patch.object(implementation, "_write_chunk", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                split(self.source, self.output, resume=True, **self.options)
        self.assertEqual((self.output / "manifest.json").read_bytes(), raw)

    def test_unterminated_tail_is_discarded_and_orphan_rewritten(self):
        for tail in (b'{"chunk_id":', b'\xff', b'{"chunk_id":"chunk-000002","sha256":"' + b'0' * 64 + b'"}'):
            with self.subTest(tail=tail):
                output = self.root / str(len(list(self.root.iterdir())))
                self.interrupt(output)
                journal = output / "checkpoint.jsonl"
                prefix = journal.read_bytes()
                with journal.open("ab") as stream:
                    stream.write(tail)
                (output / "chunk-000002.wav").write_bytes(b"orphan")
                first = self.snapshot(output)["chunk-000001.wav"]
                original = implementation._write_chunk
                with patch.object(implementation, "_write_chunk", wraps=original) as writer:
                    result = split(self.source, output, resume=True, **self.options)
                self.assertEqual(writer.call_count, 3)
                self.assertEqual(self.snapshot(output)["chunk-000001.wav"], first)
                self.assertTrue(journal.read_bytes().startswith(prefix))
                self.assertEqual(len(journal.read_bytes().splitlines()), 5)
                self.assertEqual(sha256(output / "chunk-000002.wav"), result["chunks"][1]["sha256"])

    def test_invalid_complete_records_refuse_before_mutation(self):
        base = self.interrupt()
        journal = self.output / "checkpoint.jsonl"
        header, first = journal.read_bytes().splitlines(keepends=True)
        first_record = json.loads(first)
        wrong_id = dict(first_record, chunk_id="chunk-000003")
        wrong_hash = dict(first_record, chunk_id="chunk-000002", sha256="X" * 64)
        wrong_binding = dict(json.loads(header), manifest_sha256="0" * 64)
        cases = [header + first + b'{bad}\n', header + first + first,
                 header + first + json.dumps(wrong_id).encode() + b'\n',
                 header + first + json.dumps(wrong_hash).encode() + b'\n',
                 json.dumps(wrong_binding).encode() + b'\n' + first,
                 header + first + b'{"chunk_id":"chunk-000002","chunk_id":"chunk-000002"}\n',
                 header + first + b'x' * 512, b'', header.rstrip(b'\n')]
        for raw in cases:
            with self.subTest(raw=raw):
                journal.write_bytes(raw)
                before = self.snapshot()
                with patch.object(implementation, "_write_chunk", side_effect=AssertionError("no writes")):
                    with self.assertRaises(ManifestError):
                        split(self.source, self.output, resume=True, **self.options)
                self.assertEqual(self.snapshot(), before)
        journal.write_bytes(header + first + b'partial')
        # Tail truncation must wait for WAV verification and parameter checks.
        chunk = self.output / "chunk-000001.wav"
        chunk.write_bytes(b"damaged")
        before = self.snapshot()
        with self.assertRaisesRegex(ManifestError, "Completed chunk"):
            split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(c["sha256"] is None for c in base["chunks"]))

    def test_missing_symlink_or_unsupported_journal_is_refused(self):
        base = self.interrupt()
        journal = self.output / "checkpoint.jsonl"
        journal.unlink()
        for mode in ("missing", "symlink", "directory"):
            with self.subTest(mode=mode):
                if mode == "symlink":
                    journal.symlink_to(self.source)
                elif mode == "directory":
                    journal.mkdir()
                with self.assertRaises(ManifestError):
                    split(self.source, self.output, resume=True, **self.options)
                if mode == "directory":
                    journal.rmdir()
                else:
                    journal.unlink(missing_ok=True)
        base["checkpoint_format"] = "jsonl-v999"
        (self.output / "manifest.json").write_bytes(json_bytes(base))
        with self.assertRaisesRegex(ManifestError, "checkpoint format"):
            split(self.source, self.output, resume=True, **self.options)

    def test_append_failure_reuses_only_newline_terminated_commits(self):
        for complete_line in (False, True):
            with self.subTest(complete_line=complete_line):
                output = self.root / str(complete_line)
                original = implementation._append_commit
                calls = 0

                def fail_second(stream, chunk):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if complete_line:
                            original(stream, chunk)
                        else:
                            stream.write(b'{"chunk_id":')
                            stream.flush()
                        raise OSError("append interrupted")
                    original(stream, chunk)

                with patch.object(implementation, "_append_commit", side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "append interrupted"):
                        split(self.source, output, resume=True, **self.options)
                before = self.snapshot(output)
                with patch.object(implementation, "_write_chunk", wraps=implementation._write_chunk) as writer:
                    split(self.source, output, resume=True, **self.options)
                self.assertEqual(writer.call_count, 2 if complete_line else 3)
                self.assertEqual(self.snapshot(output)["chunk-000001.wav"], before["chunk-000001.wav"])
                if complete_line:
                    self.assertEqual(self.snapshot(output)["chunk-000002.wav"], before["chunk-000002.wav"])

    def test_missing_base_digest_and_extra_commit_refuse_without_mutation(self):
        self.interrupt()
        base = read_json(self.output / "manifest.json")
        del base["chunks"][0]["sha256"]
        (self.output / "manifest.json").write_bytes(json_bytes(base))
        before = self.snapshot()
        with self.assertRaisesRegex(ManifestError, "null base digests"):
            split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(self.snapshot(), before)
        other = self.root / "all-committed"
        original = implementation._checkpoint

        def fail_completion(directory, manifest, **kwargs):
            if manifest["complete"]:
                raise OSError("completion failed")
            return original(directory, manifest, **kwargs)

        with patch.object(implementation, "_checkpoint", side_effect=fail_completion):
            with self.assertRaises(OSError):
                split(self.source, other, resume=True, **self.options)
        with (other / "checkpoint.jsonl").open("ab") as stream:
            stream.write(b'{"chunk_id":"chunk-000005","sha256":"' + b'0' * 64 + b'"}\n')
        before = self.snapshot(other)
        with self.assertRaisesRegex(ManifestError, "out-of-order"):
            split(self.source, other, resume=True, **self.options)
        self.assertEqual(self.snapshot(other), before)

    def test_final_manifest_authoritative_and_legacy_checkpoints_supported(self):
        final = split(self.source, self.output, resume=True, **self.options)
        (self.output / "checkpoint.jsonl").unlink()
        before = self.snapshot()
        self.assertEqual(split(self.source, self.output, resume=True, **self.options), final)
        self.assertEqual(self.snapshot(), before)
        legacy = copy.deepcopy(final)
        del legacy["checkpoint_format"]
        legacy["complete"] = False
        for chunk in legacy["chunks"][1:]:
            chunk["sha256"] = None
        (self.output / "manifest.json").write_bytes(json_bytes(legacy))
        original = implementation._write_chunk
        with patch.object(implementation, "_write_chunk", wraps=original) as writer:
            result = split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(writer.call_count, 3)
        self.assertEqual(result["chunks"], final["chunks"])


if __name__ == "__main__":
    unittest.main()
