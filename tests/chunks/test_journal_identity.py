"""Recovery must mutate only the journal file whose commits it replayed."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import split
from mediawrench.chunks.cli import main
from .test_tool import write_wav

implementation = importlib.import_module("mediawrench.chunks.split")


class JournalIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source.wav"
        write_wav(self.source, frames=4000)

    def seed(self, output, all_committed=False):
        original_write, original_checkpoint = implementation._write_chunk, implementation._checkpoint
        calls = 0

        def interrupted(*args):
            nonlocal calls
            calls += 1
            if calls == 2 and not all_committed:
                raise OSError("seed interruption")
            return original_write(*args)

        def checkpoint(directory, manifest, **kwargs):
            if all_committed and manifest["complete"]:
                raise OSError("seed interruption")
            return original_checkpoint(directory, manifest, **kwargs)

        with patch.object(implementation, "_write_chunk", interrupted), \
                patch.object(implementation, "_checkpoint", checkpoint):
            with self.assertRaisesRegex(OSError, "seed interruption"):
                split(self.source, output, max_seconds="3", overlap_seconds="0", resume=True)

    def snapshot(self, output):
        state = {}
        for p in output.iterdir():
            st = p.lstat()
            raw = p.readlink() if p.is_symlink() else p.read_bytes() if p.is_file() else None
            state[p.name] = (raw, st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns)
        return state

    def refuse_replacement(self, mode, tail=b"", all_committed=False, during_replay=False):
        output = self.root / (mode + str(len(list(self.root.iterdir()))))
        self.seed(output, all_committed)
        journal, first = output / "checkpoint.jsonl", output / "chunk-000001.wav"
        with journal.open("ab") as stream:
            stream.write(tail)
        raw = journal.read_bytes()
        original_info, states = implementation.wav_info, []

        def change():
            if mode.startswith("inplace-"):
                before = journal.stat()
                replacement = {
                    "inplace-same-size": b"X" * (len(raw) - 1) + b"\n",
                    "inplace-restored-mtime": b"X" * (len(raw) - 1) + b"\n",
                    "inplace-truncate": b"X",
                    "inplace-append-complete": raw + b"{}\n",
                    "inplace-append-tail": raw + b'{"chunk_id":',
                    "inplace-tail-edit": raw[:-1] + b"Y",
                    "inplace-identical": raw,
                    "inplace-metadata": raw,
                }[mode]
                if mode == "inplace-metadata":
                    os.utime(journal, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
                else:
                    with journal.open("r+b") as stream:
                        stream.write(replacement)
                        stream.truncate()
                    if mode == "inplace-restored-mtime":
                        os.utime(journal, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(journal.stat().st_ino, before.st_ino)
                self.assertEqual(journal.stat().st_dev, before.st_dev)
                if mode in ("inplace-same-size", "inplace-restored-mtime"):
                    self.assertEqual(journal.stat().st_size, before.st_size)
            else:
                temporary = self.root / "replacement"
                if mode == "symlink":
                    temporary.symlink_to(self.source)
                elif mode == "directory":
                    temporary.mkdir()
                elif mode == "fifo":
                    os.mkfifo(temporary)
                elif mode == "missing":
                    pass
                else:
                    replacement = (b"X" * (len(raw) - 1) + b"\n" if mode == "same-size" else
                                   raw if mode == "identical" else b"REPLACED INVALID JOURNAL\n")
                    temporary.write_bytes(replacement)
                # Directory replacement requires removing the old regular file.
                if mode in ("missing", "directory"):
                    journal.unlink()
                if mode != "missing":
                    temporary.replace(journal)
            states.append(self.snapshot(output))

        def inspect(path):
            info = original_info(path)
            if Path(path) == first and not states and not during_replay:
                change()
            return info

        original_replay = implementation._replay_journal

        def replay(*args):
            result = original_replay(*args)
            if during_replay:
                change()
            return result

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(implementation, "wav_info", inspect), \
                patch.object(implementation, "_replay_journal", replay), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["split", str(self.source), str(output), "--max-seconds", "3",
                         "--overlap-seconds", "0", "--resume"])
        self.assertEqual(len(states), 1)
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertIn("journal", stderr.getvalue().lower())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.snapshot(output), states[0])

    def test_regular_replacement_after_replay_refuses_before_mutation(self):
        for mode in ("short", "same-size", "identical"):
            for tail in (b"", b'{"chunk_id":'):
                with self.subTest(mode=mode, tail=tail):
                    self.refuse_replacement(mode, tail)

    def test_nonregular_or_missing_replacement_refuses_without_mutation(self):
        for mode in ("symlink", "directory", "missing", "fifo"):
            with self.subTest(mode=mode):
                self.refuse_replacement(mode, b'{"chunk_id":')

    def test_all_committed_replacement_cannot_publish_final_manifest(self):
        self.refuse_replacement("short", all_committed=True)

    def test_same_inode_edits_after_replay_refuse_before_mutation(self):
        for mode in ("same-size", "truncate", "append-complete", "append-tail",
                     "identical", "restored-mtime", "metadata"):
            for tail in (b"", b'{"chunk_id":', b"x" * 511):
                with self.subTest(mode=mode, tail_length=len(tail)):
                    self.refuse_replacement("inplace-" + mode, tail)
        self.refuse_replacement("inplace-tail-edit", b"x" * 511)

    def test_all_committed_same_inode_edit_cannot_publish_final_manifest(self):
        self.refuse_replacement("inplace-same-size", all_committed=True)

    def test_same_inode_edit_during_replay_refuses(self):
        self.refuse_replacement("inplace-same-size", during_replay=True)


if __name__ == "__main__":
    unittest.main()
