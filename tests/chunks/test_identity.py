"""Strict identity binds transcripts to exact recording/manifest/chunk bytes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.chunks import ManifestError, split, stitch
from mediawrench.chunks.common import json_bytes, read_json, sha256
from .test_tool import write_wav


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source.wav"
        write_wav(self.source, frames=7000, values=lambda f, c: f % 997)
        self.manifest = split(self.source, self.root / "chunks", max_seconds="4", overlap_seconds="1")
        self.manifest_path = self.root / "chunks/manifest.json"
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        for chunk in self.manifest["chunks"]:
            self.path(chunk).write_bytes(json_bytes({
                "recording_sha256": self.manifest["source"]["sha256"],
                "manifest_sha256": sha256(self.manifest_path),
                "chunk_id": chunk["id"], "audio_sha256": chunk["sha256"],
                "segments": [{"start": "1.0001", "end": "1.5", "text": chunk["id"]}]}))
        self.output = self.root / "merged.json"

    def path(self, chunk=None):
        chunk = chunk or self.manifest["chunks"][0]
        return self.transcripts / (chunk["id"] + ".json")

    def join(self, **options):
        return stitch(self.manifest_path, self.transcripts, self.output, **options)

    def assert_no_outputs(self):
        for suffix in (".json", ".srt", ".report.json"):
            self.assertFalse(self.output.with_suffix(suffix).exists())
        self.assertEqual(list(self.root.glob(".acm-*")), [])

    def test_strict_success_without_optional_sample_rate(self):
        result = self.join()
        identity = result["report"]["identity"]
        self.assertEqual(identity["mode"], "strict")
        self.assertEqual(identity["warnings"], [])
        self.assertEqual(result["manifest_sha256"], sha256(self.manifest_path))
        self.assertEqual(result["segments"][0]["start_sample"], 1000)
        self.assertEqual([t["binding"] for t in result["transcripts"]],
                         ["recording_manifest_chunk_sha256"] * 2)
        self.assertEqual(read_json(self.output.with_suffix(".report.json")), result["report"])

    def test_each_required_field_missing_rejected_even_on_last_chunk(self):
        path = self.path(self.manifest["chunks"][-1])
        original = read_json(path)
        for key in ("recording_sha256", "manifest_sha256", "chunk_id", "audio_sha256"):
            with self.subTest(key=key):
                data = original.copy()
                del data[key]
                path.write_bytes(json_bytes(data))
                with self.assertRaisesRegex(ManifestError, "Strict transcript identity.*" + key):
                    self.join()
                self.assert_no_outputs()

    def test_swapped_transcript_files_rejected(self):
        first, second = [self.path(c) for c in self.manifest["chunks"]]
        a, b = first.read_bytes(), second.read_bytes()
        first.write_bytes(b)
        second.write_bytes(a)
        with self.assertRaisesRegex(ManifestError, "chunk_id mismatch"):
            self.join()
        self.assert_no_outputs()

    def test_recording_identity_rejects_other_recording_with_same_chunk(self):
        # A source can change outside chunk 1 while that chunk's WAV stays identical.
        path = self.path()
        data = read_json(path)
        other = self.root / "other.wav"
        raw = self.source.read_bytes()
        other.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        other_manifest = split(other, self.root / "other-chunks", max_seconds="4", overlap_seconds="1")
        self.assertEqual(other_manifest["chunks"][0]["sha256"], data["audio_sha256"])
        data["recording_sha256"] = other_manifest["source"]["sha256"]
        path.write_bytes(json_bytes(data))
        with self.assertRaisesRegex(ManifestError, "recording_sha256 mismatch"):
            self.join()
        self.assert_no_outputs()

    def test_exact_manifest_bytes_reject_whitespace_edit(self):
        # Geometry, PCM and all JSON values are identical; the file is different.
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b" \n")
        with self.assertRaisesRegex(ManifestError, "manifest_sha256 mismatch"):
            self.join()
        self.assert_no_outputs()

    def test_changed_source_wav_rejected_before_publication(self):
        raw = self.source.read_bytes()
        self.source.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaisesRegex(ManifestError, "Source SHA256 mismatch"):
            self.join()
        self.assert_no_outputs()

    def test_native_shapes_require_explicit_compatibility_and_report_warning(self):
        self.path().write_text('{"language":"en","segments":[]}')
        self.path(self.manifest["chunks"][-1]).write_text('[]')
        with self.assertRaisesRegex(ManifestError, "Strict transcript identity"):
            self.join()
        self.assert_no_outputs()
        result = self.join(native_json=True)
        self.assertEqual(result["report"]["identity"]["mode"], "native_json")
        self.assertTrue(result["report"]["identity"]["warnings"])
        self.assertEqual([t["binding"] for t in result["transcripts"]], ["filename_only"] * 2)
        self.assertEqual(result["transcripts"][0]["metadata"]["language"], "en")

    def test_present_identity_is_never_ignored_in_either_mode(self):
        path = self.path()
        original = read_json(path)
        for native_json in (False, True):
            for key in ("recording_sha256", "manifest_sha256", "audio_sha256", "chunk_id", "sample_rate"):
                for value in (None, True, "wrong", "0" * 64):
                    with self.subTest(mode=native_json, key=key, value=value):
                        path.write_bytes(json_bytes(original | {key: value}))
                        with self.assertRaises(ManifestError):
                            self.join(native_json=native_json)
                        self.assert_no_outputs()

    def test_compatibility_preserves_legacy_partial_binding_label(self):
        for chunk in self.manifest["chunks"]:
            path = self.path(chunk)
            data = read_json(path)
            del data["recording_sha256"], data["manifest_sha256"]
            data["sample_rate"] = 1000
            path.write_bytes(json_bytes(data))
        result = self.join(native_json=True)
        self.assertEqual([t["binding"] for t in result["transcripts"]], ["id_rate_sha256"] * 2)
        self.assertTrue(result["report"]["identity"]["warnings"])

    def test_cli_default_rejects_native_and_opt_in_warns(self):
        for chunk in self.manifest["chunks"]:
            self.path(chunk).write_text('[]')
        args = [sys.executable, "-m", "mediawrench.chunks", "stitch",
                str(self.manifest_path), str(self.transcripts), str(self.output)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--native-json", result.stderr)
        self.assert_no_outputs()
        result = subprocess.run(args + ["--native-json"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("weaker", result.stderr)
        self.assertEqual(json.loads(result.stdout)["identity"]["mode"], "native_json")


if __name__ == "__main__":
    unittest.main()
