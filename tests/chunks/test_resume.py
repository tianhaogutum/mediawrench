"""Recovery checkpoints must preserve verified PCM and exact coverage."""
import copy
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from mediawrench.chunks import ManifestError, split
from mediawrench.chunks.common import json_bytes, read_json, sha256
from mediawrench.chunks.stitch import verify_manifest

implementation = importlib.import_module("mediawrench.chunks.split")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source.wav"
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((2, 3, 1000, 0, "NONE", "not compressed"))
            audio.writeframes(b"".join((i - 1250).to_bytes(3, "little", signed=True) * 2
                                       for i in range(2501)))
        self.output = self.root / "recovered"
        self.options = dict(max_seconds="0.9009", overlap_seconds="0.1001")

    def interrupt(self):
        original = implementation._write_chunk
        calls = []

        def fail_second(source, path, info, start, end):
            calls.append(start)
            if len(calls) == 2:
                Path(path).write_bytes(b"partial WAV")
                raise OSError("synthetic disk failure")
            return original(source, path, info, start, end)

        with patch.object(implementation, "_write_chunk", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                split(self.source, self.output, resume=True, **self.options)
        return read_json(self.output / "manifest.json")

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.iterdir()}

    def test_recovery_preserves_verified_chunk_and_matches_fresh_pcm(self):
        checkpoint = self.interrupt()
        self.assertIs(checkpoint["complete"], False)
        self.assertTrue(all(c["sha256"] is None for c in checkpoint["chunks"]))
        self.assertEqual(len((self.output / "checkpoint.jsonl").read_bytes().splitlines()), 2)
        self.assertTrue(all(c["sha256"] is None for c in checkpoint["chunks"][1:]))
        first = self.output / "chunk-000001.wav"
        before = (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_ino)
        with self.assertRaisesRegex(ManifestError, "incomplete"):
            verify_manifest(self.output / "manifest.json")
        original = implementation._write_chunk
        with patch.object(implementation, "_write_chunk", wraps=original) as writer:
            recovered = split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(writer.call_count, 3)
        self.assertEqual(before, (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_ino))
        fresh = split(self.source, self.root / "fresh", **self.options)
        expected = [(0, 900), (799, 1699), (1598, 2498), (2397, 2501)]
        self.assertEqual([(c["start_sample"], c["end_sample"]) for c in recovered["chunks"]], expected)
        self.assertEqual(recovered["chunks"], fresh["chunks"])
        with wave.open(str(self.source), "rb") as audio:
            pcm = audio.readframes(2501)
        for c in recovered["chunks"]:
            actual = self.output / c["file"]
            self.assertEqual(actual.read_bytes(), (self.root / "fresh" / c["file"]).read_bytes())
            with wave.open(str(actual), "rb") as audio:
                self.assertEqual(audio.readframes(2501), pcm[c["start_sample"] * 6:c["end_sample"] * 6])
        self.assertIs(recovered["complete"], True)
        self.assertEqual(verify_manifest(self.output / "manifest.json"), recovered)
        before = self.snapshot()
        with patch.object(implementation, "_write_chunk", side_effect=AssertionError("must reuse")):
            self.assertEqual(split(self.source, self.output, resume=True, **self.options), recovered)
        self.assertEqual(self.snapshot(), before)

    def test_changed_source_or_parameters_rejected_before_mutation(self):
        self.interrupt()
        before = self.snapshot()
        # Even a change that rounds to the same maximum frame count is refused.
        for changes in ({"max_seconds": "0.9008"}, {"overlap_seconds": "0.101"},
                        {"search_seconds": "4"}, {"detect_silence": True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ManifestError, "source or parameters"):
                split(self.source, self.output, resume=True, **(self.options | changes))
            self.assertEqual(self.snapshot(), before)
        raw = self.source.read_bytes()
        self.source.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaisesRegex(ManifestError, "source or parameters"):
            split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_or_missing_completed_chunk_rejected_without_writes(self):
        self.interrupt()
        first = self.output / "chunk-000001.wav"
        original = first.read_bytes()
        for mode in ("corrupt", "missing", "symlink"):
            with self.subTest(mode=mode):
                first.unlink(missing_ok=True)
                if mode == "corrupt":
                    first.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                elif mode == "symlink":
                    first.symlink_to(self.source)
                before = (self.output / "manifest.json").read_bytes()
                with self.assertRaisesRegex(ManifestError, "Completed chunk"):
                    split(self.source, self.output, resume=True, **self.options)
                self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_orphan_uncheckpointed_chunk_is_rewritten(self):
        self.interrupt()
        orphan = self.output / "chunk-000002.wav"
        orphan.write_bytes(b"uncommitted data")
        recovered = split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(sha256(orphan), recovered["chunks"][1]["sha256"])
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["checkpoint.jsonl", "chunk-000001.wav", "chunk-000002.wav", "chunk-000003.wav", "chunk-000004.wav", "manifest.json"])

    def test_resume_refuses_noncheckpoint_directories_and_plan_only(self):
        for kind in ("empty", "ordinary", "symlink"):
            destination = self.root / kind
            if kind == "ordinary":
                split(self.source, destination, **self.options)
            elif kind == "symlink":
                destination.symlink_to(self.root, target_is_directory=True)
            else:
                destination.mkdir()
            with self.subTest(kind=kind), self.assertRaises(ManifestError):
                split(self.source, destination, resume=True, **self.options)
        with self.assertRaisesRegex(ManifestError, "plan-only"):
            split(self.source, self.output, resume=True, plan_only=True, **self.options)
        self.assertFalse(self.output.exists())

    def test_completion_marker_is_enforced_but_legacy_manifests_still_work(self):
        manifest = split(self.source, self.output, **self.options)
        self.assertIs(manifest["complete"], True)
        for marker in (False, None, 1, "true"):
            altered = copy.deepcopy(manifest)
            altered["complete"] = marker
            (self.output / "manifest.json").write_bytes(json_bytes(altered))
            with self.subTest(marker=marker), self.assertRaisesRegex(ManifestError, "incomplete"):
                verify_manifest(self.output / "manifest.json")
        del manifest["complete"]
        (self.output / "manifest.json").write_bytes(json_bytes(manifest))
        self.assertEqual(verify_manifest(self.output / "manifest.json"), manifest)

    def test_resume_lock_refuses_second_writer(self):
        import fcntl
        self.interrupt()
        fd = os.open(self.output, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = self.snapshot()
        with self.assertRaisesRegex(ManifestError, "in use"):
            split(self.source, self.output, resume=True, **self.options)
        self.assertEqual(self.snapshot(), before)

    def test_recovery_after_final_completion_marker_write_fails(self):
        original = implementation._checkpoint

        def fail_completion(directory, manifest, **kwargs):
            if manifest["complete"]:
                raise OSError("completion write failed")
            return original(directory, manifest, **kwargs)

        with patch.object(implementation, "_checkpoint", side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, "completion write failed"):
                split(self.source, self.output, resume=True, **self.options)
        manifest = read_json(self.output / "manifest.json")
        self.assertIs(manifest["complete"], False)
        self.assertTrue(all(c["sha256"] is None for c in manifest["chunks"]))
        self.assertEqual(len((self.output / "checkpoint.jsonl").read_bytes().splitlines()), 5)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.wav")}
        with patch.object(implementation, "_write_chunk", side_effect=AssertionError("must reuse all")):
            recovered = split(self.source, self.output, resume=True, **self.options)
        self.assertIs(recovered["complete"], True)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.wav")})

    def test_supplied_content_and_energy_policy_changes_refuse_reuse(self):
        intervals = self.root / "silence.json"
        intervals.write_text('{"intervals":[]}')
        for mode in ("supplied", "energy"):
            with self.subTest(mode=mode):
                destination = self.root / mode
                options = self.options | ({"silence_json": intervals} if mode == "supplied" else
                                          {"detect_silence": True, "search_seconds": "0.1"})
                split(self.source, destination, resume=True, **options)
                before = (destination / "manifest.json").read_bytes()
                if mode == "supplied":
                    # Even content changes with identical intervals are refused.
                    intervals.write_text('{ "intervals": [] }')
                    changed = options
                else:
                    changed = options | {"threshold_db": "-39"}
                with self.assertRaisesRegex(ManifestError, "source or parameters"):
                    split(self.source, destination, resume=True, **changed)
                self.assertEqual((destination / "manifest.json").read_bytes(), before)

    def test_nonintegral_detector_threshold_survives_checkpoint_roundtrip(self):
        options = self.options | {"detect_silence": True, "threshold_db": "-39.1", "search_seconds": "0.1"}
        first = split(self.source, self.output, resume=True, **options)
        before = self.snapshot()
        self.assertEqual(split(self.source, self.output, resume=True, **options), first)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
