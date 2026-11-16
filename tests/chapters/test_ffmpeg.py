"""Optional REAL mux/probe integration; never downloads or fakes a binary."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

from mediawrench.chapters import dumps, json_text, loads
from mediawrench.chapters.media import verify_media

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "skipped: real ffmpeg and ffprobe are not both on PATH")
class RealFFmpegTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".tools" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="chapter-roundtrip-ffmpeg-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / "silence.wav"
        with wave.open(str(self.audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * 16_000)
        # Deliberate non-millisecond boundaries check that microseconds survive.
        self.doc = loads((ROOT / "examples/chapters/media.chapters.json").read_text(encoding="utf-8"), "json")
        self.report = dumps(self.doc, "ffmetadata").report
        self.audit = self.root / "audit.json"
        self.audit.write_text(json_text(self.report), encoding="utf-8")

    def mux(self, doc=None, *, chapter_text=None):
        media = self.root / "archive.mkv"
        command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
                   "-i", str(self.audio)]
        if doc is None and chapter_text is None:
            command += ["-map", "0:a:0", "-map_chapters", "-1"]
        else:
            sidecar = self.root / "chapters.ffmetadata"
            sidecar.write_text(chapter_text if chapter_text is not None else
                               dumps(doc, "ffmetadata", allow_lossy=True).text, encoding="utf-8")
            command += ["-f", "ffmetadata", "-i", str(sidecar), "-map", "0:a:0",
                        "-map_metadata", "1", "-map_chapters", "1"]
        command += ["-c:a", "copy", "-threads", "1", str(media)]
        subprocess.run(command, check=True, capture_output=True, timeout=120,
                       stdin=subprocess.DEVNULL,
                       env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
        return media

    def cli(self, media, *, expected=0, tolerance=1000, verify_duration=False, expected_duration=None,
            output=None, hash_inputs=False):
        result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", "verify-media", str(media),
                                 "--report", str(self.audit), "--ffprobe", FFPROBE,
                                 "--tolerance-us", str(tolerance)] +
                                (["--verify-duration"] if verify_duration else []) +
                                (["--expected-duration", expected_duration] if expected_duration is not None else []) +
                                (["--output", str(output)] if output is not None else []) +
                                (["--hash-inputs"] if hash_inputs else []), cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if output is not None:
            self.assertEqual(output.read_bytes(), result.stdout.encode("utf-8"))
        return json.loads(result.stdout)

    def test_explicit_duration_covers_audio_beyond_exported_chapters(self):
        # Exported chapters stop at 2s, independently expected audio ends at 3s.
        with wave.open(str(self.audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 24_000)
        full = self.mux(self.doc)
        result = self.cli(full, tolerance=0, verify_duration=True, expected_duration="3")
        self.assertEqual(result["duration"]["expected_us"], 3_000_000)
        self.assertEqual(result["duration"]["actual_us"], 3_000_000)
        self.assertEqual(result["duration"]["expected_source"], "explicit expected_duration")
        default = self.cli(full, expected=1, tolerance=0, verify_duration=True)
        self.assertEqual(default["duration"]["expected_us"], 2_000_000)
        with wave.open(str(self.audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 16_000)
        shortened = self.mux(self.doc)
        self.assertTrue(self.cli(shortened, tolerance=0, verify_duration=True)["equal"])
        result = self.cli(shortened, expected=1, tolerance=0, verify_duration=True, expected_duration="3")
        self.assertEqual(result["duration"]["delta_us"], -1_000_000)
        self.assertEqual([d["path"] for d in result["differences"]], ["/duration_us"])
        self.assertTrue(all(c["end_us"]["within_tolerance"] for c in result["checks"]))

    def test_duration_detects_shortened_audio_with_unchanged_chapters(self):
        full = self.mux(self.doc)
        result = self.cli(full, tolerance=0, verify_duration=True)
        self.assertEqual(result["duration"]["actual_us"], 2_000_000)
        self.assertTrue(result["duration"]["estimated"])
        # Synthetic truncated-media fixture: retain only half the PCM frames,
        # then mux the same 2s chapters. This is a shorter valid container, not
        # evidence that header probing detects every physically damaged file.
        with wave.open(str(self.audio), "rb") as stream:
            params, frames = stream.getparams(), stream.readframes(8000)
        with wave.open(str(self.audio), "wb") as stream:
            stream.setparams(params)
            stream.writeframes(frames)
        shortened = self.mux(self.doc)
        self.assertTrue(self.cli(shortened, tolerance=0)["equal"])
        result = self.cli(shortened, expected=1, tolerance=0, verify_duration=True)
        self.assertEqual(result["duration"]["actual_us"], 1_000_000)
        self.assertEqual(result["duration"]["delta_us"], -1_000_000)
        self.assertEqual([d["path"] for d in result["differences"]], ["/duration_us"])
        self.assertTrue(all(c["end_us"]["within_tolerance"] for c in result["checks"]))

    def test_synthetic_wave_mux_ffprobe_chapters(self):
        media = self.mux(self.doc)
        result = verify_media(media, self.report, ffprobe=FFPROBE, tolerance_us=1000)
        self.assertTrue(result["equal"], result)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["checks"]), 2)
        for check in result["checks"]:
            self.assertTrue(check["title"]["equal"])
            self.assertTrue(check["start_us"]["within_tolerance"])
            self.assertTrue(check["end_us"]["within_tolerance"])
        self.assertTrue(self.cli(media)["equal"])
        self.assertTrue(self.cli(media, tolerance=0)["equal"])
        hashed = self.cli(media, tolerance=0, hash_inputs=True,
                          output=self.root / "fingerprinted.json")
        self.assertTrue(hashed["equal"])
        for key, path in (("media", media), ("report", self.audit)):
            raw = path.read_bytes()
            self.assertEqual(hashed["input_fingerprints"][key], {
                "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
        shifted = copy.deepcopy(self.doc)
        shifted.chapters[0].end_us += 250
        shifted.chapters[1].start_us += 250
        media = self.mux(shifted)
        self.assertTrue(self.cli(media, tolerance=250)["equal"])
        exact = self.cli(media, tolerance=249, expected=1)
        self.assertTrue(all(d["delta_us"] == 250 for d in exact["differences"]))
        self.assertEqual({d["path"] for d in exact["differences"]},
                         {"/chapters/0/end_us", "/chapters/1/start_us"})

    def test_real_media_overlap_fails_within_tolerance_but_touching_and_gap_pass(self):
        exported = dumps(self.doc, "ffmetadata").text
        boundary = self.doc.chapters[0].end_us
        for delta in (1, 0, -1):
            # Raw sidecar bypasses canonical validation to mux actual overlap.
            text = exported.replace(f"END={boundary}\n", f"END={boundary + delta}\n", 1)
            with self.subTest(delta=delta):
                media = self.mux(chapter_text=text)
                result = self.cli(media, expected=1 if delta > 0 else 0,
                                  tolerance=1, verify_duration=True,
                                  output=self.root / f"verification-{delta}.json")
                self.assertEqual(result["actual"][0]["end_us"], boundary + delta)
                self.assertEqual(result["actual"][1]["start_us"], boundary)
                self.assertEqual(result["differences"], [{
                    "path": "/chapters/1/start_us", "kind": "overlap",
                    "previous_path": "/chapters/0/end_us", "previous_end_us": boundary + 1,
                    "actual_us": boundary, "overlap_us": 1}] if delta > 0 else [])
                self.assertEqual(result["duration"]["status"], "passed")

    def test_real_media_missing_chapter_and_no_chapters_exit_one(self):
        doc = copy.deepcopy(self.doc)
        doc.chapters.pop()
        result = self.cli(self.mux(doc), expected=1)
        self.assertIn({"path": "/chapters/1", "kind": "missing",
                       "expected": result["expected"][1], "actual": None}, result["differences"])
        result = self.cli(self.mux(), expected=1)
        self.assertEqual(sum(d["kind"] == "missing" for d in result["differences"]), 2)

    def test_real_media_changed_title_exit_one(self):
        doc = copy.deepcopy(self.doc)
        doc.chapters[1].title = "Changed"
        result = self.cli(self.mux(doc), expected=1)
        self.assertEqual(result["differences"], [{"path": "/chapters/1/title", "kind": "changed",
                                                 "expected": "章节", "actual": "Changed"}])

    def test_real_media_wrong_boundaries_exit_one(self):
        doc = copy.deepcopy(self.doc)
        doc.chapters[0].end_us = doc.chapters[1].start_us = 1_020_000
        result = self.cli(self.mux(doc), expected=1)
        self.assertEqual({d["path"] for d in result["differences"]},
                         {"/chapters/0/end_us", "/chapters/1/start_us"})
        self.assertTrue(all(d["kind"] == "outside_tolerance" for d in result["differences"]))

    def test_real_media_reordered_titles_exit_one(self):
        doc = copy.deepcopy(self.doc)
        doc.chapters[0].title, doc.chapters[1].title = doc.chapters[1].title, doc.chapters[0].title
        result = self.cli(self.mux(doc), expected=1)
        self.assertEqual({d["path"] for d in result["differences"]},
                         {"/chapters/0/title", "/chapters/1/title"})

    def test_invalid_local_media_is_error_not_skipped(self):
        media = self.root / "invalid.mkv"
        media.write_bytes(b"not a media file")
        output = self.root / "verification.json"
        output.write_bytes(b"keep previous result")
        result = subprocess.run([sys.executable, "-m", "mediawrench.chapters", "verify-media", str(media),
                                 "--report", str(self.audit), "--ffprobe", FFPROBE,
                                 "--output", str(output), "--force"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(output.read_bytes(), b"keep previous result")
        self.assertIn("ffprobe exited", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
