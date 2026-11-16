from __future__ import annotations

import copy
from decimal import Decimal
from fractions import Fraction
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

from mediawrench.chunks import ManifestError, split, stitch
from mediawrench.chunks.common import (ceil_samples, floor_samples, json_bytes,
                                         read_json, sha256, wav_info)
from mediawrench.chunks.split import detect_silences
from mediawrench.chunks.stitch import verify_manifest


ROOT = Path(__file__).resolve().parents[2]


def write_wav(path, rate=1000, frames=10000, channels=1, width=2, values=None):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        raw = bytearray()
        for frame in range(frames):
            for channel in range(channels):
                value = values(frame, channel) if values else 0
                if width == 1:
                    raw.append(value + 128)
                else:
                    raw.extend(value.to_bytes(width, "little", signed=True))
        audio.writeframes(raw)


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.wav"
        write_wav(self.source)

    def split(self, **kwargs):
        return split(self.source, self.root / "chunks", max_seconds="4", overlap_seconds="1", **kwargs)

    def transcript_files(self, manifest, segments=None, binding=True):
        directory = self.root / "transcripts"
        directory.mkdir()
        segments = segments or {}
        for chunk in manifest["chunks"]:
            data = {"segments": segments.get(chunk["id"], [])}
            if binding:
                data.update(chunk_id=chunk["id"], sample_rate=manifest["source"]["sample_rate"],
                            audio_sha256=chunk["sha256"], recording_sha256=manifest["source"]["sha256"],
                            manifest_sha256=sha256(self.root / "chunks/manifest.json"))
            (directory / (chunk["id"] + ".json")).write_bytes(json_bytes(data))
        return directory

    def save_manifest(self, manifest):
        (self.root / "chunks/manifest.json").write_bytes(json_bytes(manifest))

    def join(self, directory, **options):
        return stitch(self.root / "chunks/manifest.json", directory, self.root / "merged.json", **options)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "mediawrench.chunks", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def assert_no_outputs(self):
        self.assertFalse((self.root / "merged.json").exists())
        self.assertFalse((self.root / "merged.srt").exists())
        self.assertFalse((self.root / "merged.report.json").exists())

    def test_exact_coverage_and_audio_bytes(self):
        write_wav(self.source, channels=2, values=lambda f, c: (f % 100) * (1 if c else -1))
        manifest = self.split()
        chunks = manifest["chunks"]
        self.assertEqual([(c["start_sample"], c["end_sample"]) for c in chunks], [(0, 4000), (3000, 7000), (6000, 10000)])
        self.assertEqual([c["overlap_previous_samples"] for c in chunks], [0, 1000, 1000])
        with wave.open(str(self.source), "rb") as audio:
            raw = audio.readframes(audio.getnframes())
        for chunk in chunks:
            self.assertLessEqual(chunk["end_sample"] - chunk["start_sample"], 4000)
            path = self.root / "chunks" / chunk["file"]
            self.assertEqual(sha256(path), chunk["sha256"])
            with wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.readframes(audio.getnframes()), raw[chunk["start_sample"] * 4:chunk["end_sample"] * 4])
        self.assertEqual(verify_manifest(self.root / "chunks/manifest.json"), manifest)

    def test_partial_final_chunk_and_zero_overlap(self):
        write_wav(self.source, frames=10001)
        manifest = split(self.source, self.root / "chunks", max_seconds="4", overlap_seconds="0")
        self.assertEqual([(c["start_sample"], c["end_sample"]) for c in manifest["chunks"]], [(0, 4000), (4000, 8000), (8000, 10001)])
        verify_manifest(self.root / "chunks/manifest.json")

    def test_fractional_sample_duration_rules(self):
        self.assertEqual(floor_samples("0.00015", 10000), 1)
        self.assertEqual(ceil_samples("0.00015", 10000), 2)
        write_wav(self.source, rate=10, frames=17)
        m = split(self.source, self.root / "chunks", max_seconds="0.45", overlap_seconds="0.11")
        self.assertEqual(m["max_samples"], 4)
        self.assertEqual(m["overlap_samples"], 2)
        self.assertEqual(m["chunks"][-1]["end_sample"], 17)
        self.assertTrue(all(c["end_sample"] - c["start_sample"] <= 4 for c in m["chunks"]))

    def test_supplied_silence_and_plan_only(self):
        intervals = self.root / "silence.json"
        intervals.write_bytes(json_bytes({"intervals": [{"start": 2.8, "end": 3.2}]}))
        m = self.split(plan_only=True, silence_json=intervals, search_seconds="2")
        self.assertEqual(m["chunks"][0]["end_sample"], 3000)
        self.assertEqual(m["chunks"][0]["boundary_reason"], "silence")
        self.assertEqual([p.name for p in (self.root / "chunks").iterdir()], ["manifest.json"])
        self.assertTrue(all(c["sha256"] is None for c in m["chunks"]))
        with self.assertRaisesRegex(ManifestError, "plan-only"):
            verify_manifest(self.root / "chunks/manifest.json")

    def test_detect_silence_all_pcm_widths_and_channels(self):
        for width in (1, 2, 3, 4):
            with self.subTest(width=width):
                path = self.root / f"{width}.wav"
                amplitude = (1 << (width * 8 - 1)) // 4
                write_wav(path, frames=4000, channels=2, width=width,
                          values=lambda f, c: 0 if 2000 <= f < 3000 else amplitude * (1 if c else -1))
                intervals = detect_silences(path, wav_info(path), window_seconds="0.1", min_seconds="0.5")
                self.assertEqual(intervals, [(2000, 3000)])
        # One active channel must not be classed as silence.
        write_wav(self.source, frames=1000, channels=2, values=lambda f, c: 10000 if c else 0)
        self.assertEqual(detect_silences(self.source, wav_info(self.source)), [])

    def test_detected_plan_chooses_silence_with_hard_bound(self):
        write_wav(self.source, values=lambda f, c: 0 if 2800 <= f < 3200 else 10000)
        m = self.split(detect_silence=True, search_seconds="2")
        self.assertEqual(m["chunks"][0]["end_sample"], 3000)
        self.assertEqual(m["silence"]["method"], "energy")
        self.assertEqual(m["chunks"][-1]["end_sample"], 10000)

    def test_no_progress_boundary_is_not_selected(self):
        path = self.root / "silence.json"
        path.write_bytes(json_bytes({"intervals": [{"start": 0, "end": 10}]}))
        m = split(self.source, self.root / "chunks", max_seconds="1", overlap_seconds="0.999", silence_json=path, search_seconds="99")
        self.assertEqual(m["chunks"][-1]["end_sample"], 10000)
        self.assertTrue(all(c["end_sample"] > c["start_sample"] for c in m["chunks"]))

    def _silence_repro(self, overlap, **kwargs):
        # 12 s, 8 kHz mono, silences at 3-4 s and 7-8 s.
        rate = 8000
        write_wav(self.source, rate=rate, frames=12 * rate,
                  values=lambda f, c: 0 if 3 * rate <= f < 4 * rate or 7 * rate <= f < 8 * rate else 10000)
        return split(self.source, self.root / f"chunks-{overlap}-{len(kwargs)}", max_seconds="5",
                     overlap_seconds=overlap, **kwargs)

    def assert_silence_plan_progresses(self, m, rate=8000, total=12 * 8000):
        chunks = m["chunks"]
        self.assertLessEqual(len(chunks), 6)
        self.assertEqual(chunks[0]["start_sample"], 0)
        self.assertEqual(chunks[-1]["end_sample"], total)
        for previous, current in zip(chunks, chunks[1:]):
            self.assertGreater(current["start_sample"], previous["start_sample"] + rate // 2)
            self.assertLessEqual(current["start_sample"], previous["end_sample"])
            self.assertEqual(current["overlap_previous_samples"], previous["end_sample"] - current["start_sample"])
            self.assertEqual(current["overlap_previous_samples"], m["overlap_samples"])

    def test_silence_midpoint_near_overlap_does_not_stall_detected(self):
        self.assert_silence_plan_progresses(self._silence_repro("1", detect_silence=True))

    def test_silence_midpoint_near_overlap_does_not_stall_supplied(self):
        path = self.root / "s.json"
        path.write_bytes(json_bytes({"intervals": [{"start": 3, "end": 4}, {"start": 7, "end": 8}]}))
        self.assert_silence_plan_progresses(self._silence_repro("1", silence_json=path))

    def test_silence_midpoint_near_overlap_does_not_stall_plan_only_overlap_2(self):
        m = self._silence_repro("2", detect_silence=True, plan_only=True)
        self.assert_silence_plan_progresses(m)
        self.assertEqual(self._silence_repro("0.5", detect_silence=True, plan_only=True)["chunks"][-1]["end_sample"], 96000)

    def test_invalid_duration_and_detector_options(self):
        options = [{"max_seconds": "0"}, {"max_seconds": "1", "overlap_seconds": "1"},
                   {"overlap_seconds": "-1"}, {"max_seconds": "nan"}, {"max_seconds": "Infinity"},
                   {"max_seconds": True}, {"detect_silence": True, "window_seconds": "0"},
                   {"detect_silence": True, "threshold_db": "nan"},
                   {"detect_silence": True, "threshold_db": "1"},
                   {"detect_silence": True, "min_silence_seconds": "0"}]
        for index, kwargs in enumerate(options):
            with self.subTest(kwargs=kwargs), self.assertRaises(ManifestError):
                split(self.source, self.root / str(index), **kwargs)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source.wav"])

    def test_invalid_silence_intervals(self):
        path = self.root / "silence.json"
        for data in ([{"start": 2, "end": 1}], [{"start": 0, "end": 11}],
                     [{"start": 1, "end": 3}, {"start": 2, "end": 4}],
                     [{"start": 0.0001, "end": 0.0002}], [{"start": 1, "end": 2, "oops": 1}]):
            with self.subTest(data=data):
                path.write_bytes(json_bytes({"intervals": data}))
                with self.assertRaises(ManifestError):
                    self.split(silence_json=path)
        path.write_bytes(json_bytes({"intervals": []}))
        with self.assertRaises(ManifestError):
            self.split(silence_json=path, detect_silence=True)

    def test_empty_compressed_float_and_truncated_wav_rejected(self):
        write_wav(self.source, frames=0)
        with self.assertRaises(ManifestError):
            self.split()
        write_wav(self.source)
        raw = bytearray(self.source.read_bytes())
        for format_tag in (3, 6, 7):
            with self.subTest(format_tag=format_tag):
                changed = raw.copy()
                struct.pack_into("<H", changed, 20, format_tag)
                self.source.write_bytes(changed)
                with self.assertRaises(ManifestError):
                    self.split()
        self.source.write_bytes(raw[:-1])
        with self.assertRaisesRegex(ManifestError, "Truncated"):
            self.split()
        self.source.write_bytes(b"not a WAV")
        self.assertNotEqual(self.cli("split", self.source, self.root / "chunks").returncode, 0)

    def test_roundtrip_json_srt_dedup_and_deliberate_repeat(self):
        m = self.split()
        transcripts = self.transcript_files(m, {
            "chunk-000001": [{"start": 1, "end": 2, "text": "Hello & <world>"},
                             {"start": 3.1, "end": 3.5, "text": "Echo", "custom": {"preserved": True}}],
            "chunk-000002": [{"start": 0.1, "end": 0.5, "text": " Echo "},
                             {"start": 2, "end": 2.5, "text": "Echo"}],
            "chunk-000003": [{"start": 2.0001, "end": 2.5001, "text": "世界", "words": [
                {"start": 2.0001, "end": 2.5001, "word": "世界", "probability": 0.9}]}]})
        out = self.join(transcripts)
        self.assertEqual(out["report"]["duplicate_count"], 1)
        self.assertEqual(out["report"]["conflict_count"], 0)
        self.assertEqual([s["text"] for s in out["segments"]], ["Hello & <world>", "Echo", "Echo", "世界"])
        self.assertEqual(len(out["segments"][1]["origins"]), 2)
        self.assertTrue(out["segments"][1]["origins"][0]["original"]["custom"]["preserved"])
        last = out["segments"][-1]
        self.assertEqual((last["start_sample"], last["end_sample"]), (8000, 8501))
        self.assertEqual(last["words"][0]["start_sample"], 8000)
        self.assertEqual(last["words"][0]["original"]["probability"], Decimal("0.9"))
        self.assertEqual(read_json(self.root / "merged.report.json"), out["report"])
        srt = (self.root / "merged.srt").read_text()
        blocks = srt.strip().split("\n\n")
        self.assertEqual(len(blocks), 4)
        self.assertIn("00:00:08,000 --> 00:00:08,501", blocks[-1])
        self.assertIn("Hello &amp; &lt;world&gt;", blocks[0])
        self.assertEqual(out["transcripts"][0]["binding"], "recording_manifest_chunk_sha256")

    def test_raw_whisper_and_faster_whisper_list(self):
        m = self.split()
        transcripts = self.transcript_files(m, binding=False)
        (transcripts / "chunk-000001.json").write_bytes(json_bytes({"language": "en", "text": "Test", "segments": [
            {"id": 0, "start": 1, "end": 2, "text": "Test", "tokens": [1, 2], "avg_logprob": -0.1}]}))
        (transcripts / "chunk-000002.json").write_bytes(json_bytes([
            {"id": 42, "start": 2, "end": 2.5, "text": "List", "words": [
                {"start": 2, "end": 2.5, "word": "List", "probability": 0.99}]}]))
        out = self.join(transcripts, native_json=True)
        self.assertEqual(out["transcripts"][0]["metadata"]["language"], "en")
        self.assertEqual(out["transcripts"][0]["binding"], "filename_only")
        self.assertEqual(out["segments"][0]["origins"][0]["original"]["tokens"], [1, 2])
        self.assertEqual(out["segments"][1]["start"], 5)

    def test_conflicts_preserved_and_cli_exit_3(self):
        m = self.split()
        transcripts = self.transcript_files(m, {
            "chunk-000001": [{"start": 3.1, "end": 3.5, "text": "one"}],
            "chunk-000002": [{"start": 0.1, "end": 0.5, "text": "two"}]})
        result = self.cli("stitch", self.root / "chunks/manifest.json", transcripts, self.root / "merged.json")
        self.assertEqual(result.returncode, 3, result.stderr)
        out = read_json(self.root / "merged.json")
        self.assertEqual([s["text"] for s in out["segments"]], ["one", "two"])
        self.assertEqual(out["report"]["conflict_count"], 1)
        self.assertIn("preserved", result.stderr)

    def test_partial_suffix_matching_is_not_automatic(self):
        m = self.split()
        transcripts = self.transcript_files(m, {
            "chunk-000001": [{"start": 2, "end": 3.5, "text": "before and overlap"}],
            "chunk-000002": [{"start": 0, "end": 1.5, "text": "overlap and after"}]})
        out = self.join(transcripts)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 1)

    def test_distinct_subsample_times_never_dedup(self):
        m = self.split()
        transcripts = self.transcript_files(m, {
            "chunk-000001": [{"start": "3.1001", "end": "3.5001", "text": "again"}],
            "chunk-000002": [{"start": "0.1002", "end": "0.5002", "text": "again"}]})
        out = self.join(transcripts)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 1)

    def test_json_numeric_precision_preserves_conflicting_segments(self):
        m = self.split()
        directory = self.transcript_files(m)
        (directory / "chunk-000001.json").write_text(
            '{"segments":[{"start":3.10000000000000001,"end":3.5,"text":"same"}]}')
        (directory / "chunk-000002.json").write_text(
            '{"segments":[{"start":0.1,"end":0.5,"text":"same"}]}')
        result = self.cli("stitch", self.root / "chunks/manifest.json", directory, self.root / "merged.json", "--native-json")
        self.assertEqual(result.returncode, 3, result.stderr)
        out = json.loads((self.root / "merged.json").read_text(), parse_float=Decimal)
        self.assertEqual(out["report"]["output_segments"], 2)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 1)
        self.assertEqual(out["segments"][0]["origins"][0]["original"]["start"],
                         Decimal("3.10000000000000001"))

    def test_json_numeric_precision_maps_segment_and_word_sample_edges(self):
        m = self.split()
        directory = self.transcript_files(m)
        (directory / "chunk-000001.json").write_text(
            '{"segments":[{"start":3.09999999999999999,"end":3.50000000000000001,'
            '"text":"edge","words":[{"start":3.09999999999999999,'
            '"end":3.50000000000000001,"word":"edge"}]}]}')
        out = self.join(directory, native_json=True)
        segment = out["segments"][0]
        self.assertEqual((segment["start_sample"], segment["end_sample"]), (3099, 3501))
        word = segment["words"][0]
        self.assertEqual((word["start_sample"], word["end_sample"]), (3099, 3501))
        self.assertIn("00:00:03,099 --> 00:00:03,501", (self.root / "merged.srt").read_text())

    def test_json_numeric_precision_preserves_word_alignment_conflict(self):
        m = self.split()
        directory = self.transcript_files(m)
        (directory / "chunk-000001.json").write_text(
            '{"segments":[{"start":3,"end":4,"text":"same","words":['
            '{"start":3.10000000000000001,"end":3.5,"word":"same"}]}]}')
        (directory / "chunk-000002.json").write_text(
            '{"segments":[{"start":0,"end":1,"text":"same","words":['
            '{"start":0.1,"end":0.5,"word":"same"}]}]}')
        out = self.join(directory, native_json=True)
        self.assertEqual(out["report"]["output_segments"], 2)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 1)

    def test_exact_json_numbers_and_decimal_strings_still_dedup(self):
        m = self.split()
        directory = self.transcript_files(m)
        (directory / "chunk-000001.json").write_text(
            '{"segments":[{"start":3.10000000000000001,"end":3.5,"text":"same","words":['
            '{"start":3.10000000000000001,"end":3.5,"word":"same"}]}]}')
        (directory / "chunk-000002.json").write_text(
            '{"segments":[{"start":"0.10000000000000001","end":"0.5","text":"same","words":['
            '{"start":"0.10000000000000001","end":"0.5","word":"same"}]}]}')
        out = self.join(directory, native_json=True)
        self.assertEqual(out["report"]["output_segments"], 1)
        self.assertEqual(out["report"]["duplicate_count"], 1)
        self.assertEqual(out["report"]["conflict_count"], 0)

    def test_json_roundtrip_keeps_exact_numbers_and_metadata_types(self):
        path = self.root / "numbers.json"
        path.write_text('{"nested":[3.10000000000000001,1e-999,-0.0,1.2300e+20,123,'
                        'true,null,"3.10000000000000001"],"世界\\\"\\n":"<>&"}')
        expected = json.loads(path.read_text(), parse_float=Decimal)
        value = read_json(path)
        path.write_bytes(json_bytes(value))
        actual = json.loads(path.read_text(), parse_float=Decimal)
        self.assertEqual(actual, expected)
        self.assertIsInstance(actual["nested"][0], Decimal)  # stays a JSON number
        self.assertEqual(Fraction(str(actual["nested"][0])), Fraction("3.10000000000000001"))
        self.assertEqual(actual["nested"][2].as_tuple(), expected["nested"][2].as_tuple())

    def test_supplied_silence_json_numeric_precision_rounds_inward(self):
        path = self.root / "silence.json"
        path.write_text('{"intervals":[{"start":3.00000000000000001,"end":3.99999999999999999}]}')
        m = self.split(silence_json=path)
        self.assertEqual(m["silence"]["intervals_samples"], [[3001, 3999]])

    def test_srt_timestamp_like_text_rejected_before_any_publication(self):
        m = self.split()
        directory = self.transcript_files(m)
        path = directory / "chunk-000001.json"
        identity = read_json(path)
        for text in ("Meet at 00:00:08,000 then 00:00:09,000",
                     "00:00:08,000 --> 00:00:09,000",
                     "At 0:0:8.0\nthen 0:0:9.0",  # unsafe after whitespace flattening
                     "00:00:08,000 and 00:00:09,000 and 00:00:10,000"):
            with self.subTest(text=text):
                path.write_bytes(json_bytes({**identity, "segments": [{"start": 1, "end": 2, "text": text}]}))
                with self.assertRaisesRegex(ManifestError, "unsupported SRT text"):
                    self.join(directory)
                result = self.cli("stitch", self.root / "chunks/manifest.json", directory, self.root / "merged.json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsupported SRT text", result.stderr)
                self.assert_no_outputs()
                self.assertEqual(list(self.root.glob(".acm-*")), [])

    def test_single_timestamp_text_keeps_one_readable_srt_cue(self):
        m = self.split()
        text = "Meet at 00:00:08,000\nthen go."
        directory = self.transcript_files(m, {"chunk-000001": [{"start": 1, "end": 2, "text": text}]})
        out = self.join(directory)
        self.assertEqual(out["segments"][0]["text"], text)
        self.assertEqual((self.root / "merged.srt").read_text(),
                         "1\n00:00:01,000 --> 00:00:02,000\nMeet at 00:00:08,000 then go.\n\n")

    def test_srt_interoperability_pysubs2(self):
        # pysubs2 is an independent SRT parser (a core mediawrench dependency), used here
        # only to check that merged.srt parses as expected.
        import pysubs2
        m = self.split()
        directory = self.transcript_files(m, {"chunk-000001": [
            {"start": 1, "end": 2, "text": "Meet at 00:00:08,000\nthen go."},
            {"start": 2.1, "end": 2.5, "text": "世界"}]})
        self.join(directory)
        cues = pysubs2.load(str(self.root / "merged.srt"), format_="srt")
        self.assertEqual([(cue.start, cue.end, cue.text) for cue in cues],
                         [(1000, 2000, "Meet at 00:00:08,000 then go."), (2100, 2500, "世界")])

    def test_word_alignment_conflict_is_not_dropped(self):
        m = self.split()
        transcripts = self.transcript_files(m, {
            "chunk-000001": [{"start": 3, "end": 4, "text": "again", "words": [
                {"start": 3, "end": 3.5, "word": "again"}]}],
            "chunk-000002": [{"start": 0, "end": 1, "text": "again", "words": [
                {"start": 0.5, "end": 1, "word": "again"}]}]})
        out = self.join(transcripts)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 1)

    def test_missing_word_evidence_never_discards_conflicting_alignment(self):
        write_wav(self.source, frames=5000)
        m = split(self.source, self.root / "chunks", max_seconds="4", overlap_seconds="3.5")
        data = {}
        for index, chunk in enumerate(m["chunks"]):
            offset = chunk["start_sample"] / 1000
            segment = {"start": 2 - offset, "end": 2.5 - offset, "text": "shared"}
            if index:
                segment["words"] = [{"start": 2 - offset + index / 10,
                                     "end": 2.5 - offset, "word": "shared"}]
            data[chunk["id"]] = [segment]
        out = self.join(self.transcript_files(m, data))
        self.assertEqual(len(out["segments"]), 3)
        self.assertEqual(out["report"]["duplicate_count"], 0)
        self.assertEqual(out["report"]["conflict_count"], 3)

    def test_48khz_ten_second_offset(self):
        write_wav(self.source, rate=48000, frames=22 * 48000)
        m = split(self.source, self.root / "chunks", max_seconds="11", overlap_seconds="1")
        self.assertEqual(m["chunks"][1]["start_sample"], 480000)
        directory = self.transcript_files(m, {"chunk-000002": [{"start": 0, "end": 1, "text": "offset"}]})
        segment = self.join(directory)["segments"][0]
        self.assertEqual((segment["start_sample"], segment["end_sample"]), (480000, 528000))
        self.assertEqual((segment["start"], segment["end"]), (10, 11))
        self.assertIn("00:00:10,000 --> 00:00:11,000", (self.root / "merged.srt").read_text())

    def test_duplicate_inside_one_chunk_and_after_gap_remains(self):
        m = self.split()
        transcripts = self.transcript_files(m, {"chunk-000001": [
            {"start": 1, "end": 2, "text": "go go"}, {"start": 1, "end": 2, "text": "go go"},
            {"start": 2.5, "end": 3, "text": "go go"}]})
        out = self.join(transcripts)
        self.assertEqual(len(out["segments"]), 3)
        self.assertEqual(out["report"]["duplicate_count"], 0)

    def test_more_than_two_overlapping_chunks(self):
        write_wav(self.source, frames=5000)
        m = split(self.source, self.root / "chunks", max_seconds="4", overlap_seconds="3.5")
        data = {}
        for chunk in m["chunks"]:
            offset = chunk["start_sample"] / 1000
            data[chunk["id"]] = [{"start": 2 - offset, "end": 2.5 - offset, "text": "shared"}]
        out = self.join(self.transcript_files(m, data))
        self.assertEqual(len(out["segments"]), 1)
        self.assertEqual(out["report"]["duplicate_count"], 2)
        self.assertEqual(out["report"]["conflict_count"], 0)

    def test_hash_mismatch_and_missing_chunk(self):
        m = self.split()
        path = self.root / "chunks" / m["chunks"][0]["file"]
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ManifestError, "Chunk SHA256 mismatch"):
            verify_manifest(self.root / "chunks/manifest.json")
        path.unlink()
        with self.assertRaisesRegex(ManifestError, "Chunk missing or changed"):
            verify_manifest(self.root / "chunks/manifest.json")

    def test_source_changed_and_wrong_rate(self):
        m = self.split()
        original = self.source.read_bytes()
        self.source.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ManifestError, "Source SHA256 mismatch"):
            verify_manifest(self.root / "chunks/manifest.json")
        self.source.write_bytes(original)
        m["source"]["sample_rate"] = 8000
        self.save_manifest(m)
        with self.assertRaisesRegex(ManifestError, "metadata mismatch"):
            verify_manifest(self.root / "chunks/manifest.json")

    def test_chunk_rate_checked_even_with_new_checksum(self):
        m = self.split()
        path = self.root / "chunks" / m["chunks"][0]["file"]
        write_wav(path, rate=8000, frames=4000)
        m["chunks"][0]["sha256"] = sha256(path)
        self.save_manifest(m)
        with self.assertRaisesRegex(ManifestError, "metadata mismatch"):
            verify_manifest(self.root / "chunks/manifest.json")

    def test_manifest_geometry_ids_and_schema_validation(self):
        m = self.split()
        changes = [lambda x: x.update(schema_version=True), lambda x: x.update(overlap_samples=4000),
                   lambda x: x["chunks"][0].update(start_sample=1),
                   lambda x: x["chunks"][1].update(start_sample=3001),
                   lambda x: x["chunks"][1].update(id="chunk-000001"),
                   lambda x: x["chunks"][1].update(file="../source.wav"),
                   lambda x: x["chunks"][1].update(overlap_previous_samples=0),
                   lambda x: x["chunks"][0].update(sha256="bad"),
                   lambda x: x["chunks"].pop()]
        for change in changes:
            with self.subTest(change=change):
                bad = copy.deepcopy(m)
                change(bad)
                self.save_manifest(bad)
                with self.assertRaises(ManifestError):
                    verify_manifest(self.root / "chunks/manifest.json")
        self.save_manifest(m)

    def test_missing_unexpected_transcript_and_binding(self):
        m = self.split()
        directory = self.transcript_files(m)
        path = directory / "chunk-000001.json"
        original = read_json(path)
        for key, value in (("chunk_id", "wrong"), ("sample_rate", 999), ("audio_sha256", "wrong"), ("sample_rate", True)):
            with self.subTest(key=key, value=value):
                path.write_bytes(json_bytes({**original, key: value}))
                with self.assertRaises(ManifestError):
                    self.join(directory)
                self.assert_no_outputs()
        path.write_bytes(json_bytes(original))
        extra = directory / "unknown.json"
        extra.write_text("[]")
        with self.assertRaisesRegex(ManifestError, "unexpected"):
            self.join(directory)
        extra.unlink()
        path.unlink()
        result = self.cli("stitch", self.root / "chunks/manifest.json", directory, self.root / "merged.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing", result.stderr)
        self.assert_no_outputs()

    def test_bad_timestamps_words_and_text_leave_no_outputs(self):
        m = self.split()
        directory = self.transcript_files(m)
        path = directory / "chunk-000001.json"
        invalid = [{"start": 0, "end": 5, "text": "outside"}, {"start": -1, "end": 1, "text": "negative"},
                   {"start": 1, "end": 1, "text": "zero"}, {"start": 2, "end": 1, "text": "reversed"},
                   {"start": True, "end": 1, "text": "bool"}, {"start": "NaN", "end": 1, "text": "nan"},
                   {"start": 0, "end": 1, "text": " "}, {"start": 0, "end": 1, "text": "x\x00y"},
                   {"start": 0, "end": 1, "text": "bad words", "words": [
                       {"start": 0, "end": 2, "word": "outside"}]},
                   {"start": 0, "end": 1, "text": "bad words", "words": None}]
        identity = read_json(path)
        for segment in invalid:
            with self.subTest(segment=segment):
                path.write_bytes(json_bytes({**identity, "segments": [segment]}))
                with self.assertRaises(ManifestError):
                    self.join(directory)
                self.assert_no_outputs()
        for text in ('{"segments":[{"start":NaN,"end":1,"text":"bad"}]}',
                     '{"segments":[],"unused":1e999}', '{"segments":[],"segments":[]}',
                     '{"segments":[],"unused":1e999999999999999999999999999999999}',
                     '{"segments":[],"invalid_unicode":"\\ud800"}'):
            path.write_text(text)
            with self.assertRaises(ManifestError):
                self.join(directory)
        self.assert_no_outputs()

    def test_no_overwriting_and_atomic_validation(self):
        m = self.split()
        with self.assertRaises(ManifestError):
            self.split()
        directory = self.transcript_files(m)
        (self.root / "merged.srt").write_text("KEEP")
        with self.assertRaisesRegex(ManifestError, "overwrite"):
            self.join(directory)
        self.assertFalse((self.root / "merged.json").exists())
        self.assertEqual((self.root / "merged.srt").read_text(), "KEEP")
        self.assertEqual(list(self.root.glob(".acm-*")), [])

    def test_cli_help_and_plan_only(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("stitch", result.stdout)
        result = self.cli("split", self.source, self.root / "plan", "--plan-only", "--max-seconds", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "plan").glob("*.wav")), [])

    def test_synthetic_demo_end_to_end(self):
        result = subprocess.run([sys.executable, "examples/chunks/demo.py", "--output", str(self.root / "demo")],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        metrics = json.loads(result.stdout)
        self.assertEqual(metrics["max_source_mapping_error_samples"], 0)
        self.assertEqual(metrics["conflicts"], 0)
        self.assertGreater(metrics["removed_duplicates"], 0)


if __name__ == "__main__":
    unittest.main()
