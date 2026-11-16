"""Verify a manifest and map offline Whisper/faster-whisper JSON to source samples."""
from __future__ import annotations

import html
import math
from pathlib import Path
import re
import stat

from .common import (ManifestError, _check_source, _source_revision, integer,
                     json_bytes, publish_files,
                     read_json_snapshot, seconds, sha256, wav_info)


def _digest(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ManifestError(f"{name} must be a lowercase SHA256 hex digest")
    return value


def _chunk_revision(path):
    """Conservative regular-file revision across separate hash/PCM reads."""
    try:
        current = path.lstat()
    except OSError as exc:
        raise ManifestError(f"Chunk missing or changed since validation: {path.name}") from exc
    if not stat.S_ISREG(current.st_mode):
        raise ManifestError(f"Chunk must remain a regular file: {path.name}")
    # Reader atime is excluded. This is not a captured PCM byte snapshot.
    return (current.st_dev, current.st_ino, current.st_mode, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns)


def _check_chunk(path, revision):
    if _chunk_revision(path) != revision:
        raise ManifestError(f"Chunk identity or metadata changed since validation: {path.name}")


def _check_chunks(chunks):
    for path, revision in chunks:
        _check_chunk(path, revision)


def verify_manifest(path):
    """Validate a realized manifest and its audio, preserving the value-only API."""
    return _verified_manifest_snapshot(path)[0]


def _verified_manifest_snapshot(path):
    path = Path(path).resolve()
    manifest, digest = read_json_snapshot(path)
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ManifestError("Unsupported manifest schema_version (expected 1)")
    if manifest.get("tool") != "audio-chunk-manifest" or manifest.get("plan_only") is not False:
        raise ManifestError("Only realized chunk manifests (from mediawrench chunks split without --plan-only) can be stitched; plan-only has no audio")
    if (("complete" in manifest and manifest["complete"] is not True)
            or (manifest.get("resumable") is True and manifest.get("complete") is not True)):
        raise ManifestError("Cannot stitch an incomplete split; finish it with split --resume")
    source = manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not source["path"]:
        raise ManifestError("Missing source path/metadata")
    for key in ("sample_rate", "channels", "sample_width", "total_samples"):
        integer(source.get(key), f"source.{key}", 1)
    source_path = (path.parent / source["path"]).resolve()
    source_revision = _source_revision(source_path)
    source_digest = sha256(source_path)
    _check_source(source_path, source_revision)
    if source_digest != _digest(source.get("sha256"), "source.sha256"):
        raise ManifestError("Source SHA256 mismatch")
    source_info = wav_info(source_path)
    _check_source(source_path, source_revision)
    if source_info != {key: source[key] for key in ("sample_rate", "channels", "sample_width", "total_samples")}:
        raise ManifestError("Source sample rate/PCM metadata mismatch")
    maximum = integer(manifest.get("max_samples"), "max_samples", 1)
    overlap = integer(manifest.get("overlap_samples"), "overlap_samples")
    if overlap >= maximum:
        raise ManifestError("Manifest overlap must be smaller than maximum")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ManifestError("Manifest needs nonempty chunks")
    previous = None
    verified_chunks = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise ManifestError("Invalid chunk record")
        expected_id = f"chunk-{index + 1:06d}"
        if chunk.get("id") != expected_id or chunk.get("file") != expected_id + ".wav":
            raise ManifestError(f"Invalid/duplicate chunk id or filename; expected {expected_id}")
        start = integer(chunk.get("start_sample"), "chunk.start_sample")
        end = integer(chunk.get("end_sample"), "chunk.end_sample", 1)
        actual_overlap = 0 if previous is None else previous["end_sample"] - start
        declared_overlap = integer(chunk.get("overlap_previous_samples"), "overlap_previous_samples")
        if not (start < end <= source["total_samples"]) or end - start > maximum:
            raise ManifestError("Invalid chunk sample bounds or maximum duration exceeded")
        if previous is None:
            if start != 0 or declared_overlap != 0:
                raise ManifestError("First chunk must begin at source sample 0 without overlap")
        elif (start != previous["end_sample"] - overlap or start <= previous["start_sample"]
              or end <= previous["end_sample"] or declared_overlap != actual_overlap):
            raise ManifestError("Chunk coverage/overlap must advance without gaps or redundant chunks")
        chunk_path = path.parent / chunk["file"]
        revision = _chunk_revision(chunk_path)
        chunk_digest = sha256(chunk_path)
        _check_chunk(chunk_path, revision)
        if chunk_digest != _digest(chunk.get("sha256"), f"{expected_id}.sha256"):
            raise ManifestError(f"Chunk SHA256 mismatch: {expected_id}")
        expected = {key: source[key] for key in ("sample_rate", "channels", "sample_width")}
        expected["total_samples"] = end - start
        chunk_info = wav_info(chunk_path)
        _check_chunk(chunk_path, revision)
        if chunk_info != expected:
            raise ManifestError(f"Chunk sample rate/PCM metadata mismatch: {expected_id}")
        verified_chunks.append((chunk_path, revision))
        previous = chunk
    if previous["end_sample"] != source["total_samples"]:
        raise ManifestError("Chunks do not cover the end of the source")
    _check_source(source_path, source_revision)
    _check_chunks(verified_chunks)
    return manifest, digest, source_path, source_revision, verified_chunks


def _time_range(item, rate, length, label, *, allow_zero=False):
    if not isinstance(item, dict) or "start" not in item or "end" not in item:
        raise ManifestError(f"{label} requires start and end")
    start, end = seconds(item["start"], label + ".start"), seconds(item["end"], label + ".end")
    if start > end or (start == end and not allow_zero) or end * rate > length:
        raise ManifestError(f"{label} timestamp outside chunk, reversed, or zero-length")
    return start, end


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be nonempty text")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise ManifestError(f"{label} contains unsupported control characters")
    return value


IDENTITY_FIELDS = ("recording_sha256", "manifest_sha256", "chunk_id", "audio_sha256")
NATIVE_WARNING = ("Native JSON compatibility mode provides weaker identity guarantees: "
                  "recording, manifest and chunk identity are not required; filenames alone "
                  "cannot prove which audio was transcribed.")


def read_transcript(path, chunk, rate, *, recording_sha256, manifest_sha256, native_json=False):
    data, digest = read_json_snapshot(path)
    metadata, binding = {}, "filename_only"
    missing = [key for key in IDENTITY_FIELDS if not isinstance(data, dict) or key not in data]
    if missing and not native_json:
        raise ManifestError(f"Strict transcript identity requires {', '.join(missing)}: {path}; "
                            "use --native-json only for weaker native-JSON compatibility")
    if isinstance(data, dict):
        metadata = {key: value for key, value in data.items() if key != "segments"}
        if "chunk_id" in data and data["chunk_id"] != chunk["id"]:
            raise ManifestError(f"Transcript chunk_id mismatch: {path}")
        if "sample_rate" in data:
            integer(data["sample_rate"], "transcript.sample_rate", 1)
            if data["sample_rate"] != rate:
                raise ManifestError(f"Transcript sample rate mismatch: {path}")
        for key, expected in (("recording_sha256", recording_sha256),
                              ("manifest_sha256", manifest_sha256), ("audio_sha256", chunk["sha256"])):
            if key in data:
                _digest(data[key], f"transcript.{key}")
                if data[key] != expected:
                    raise ManifestError(f"Transcript {key} mismatch: {path}")
        if all(key in data for key in ("chunk_id", "sample_rate", "audio_sha256")):
            binding = "id_rate_sha256"
        if not missing:
            binding = "recording_manifest_chunk_sha256"
        segments = data.get("segments")
    else:
        segments = data
    if not isinstance(segments, list):
        raise ManifestError(f"Transcript must be a segment list or an object containing segments: {path}")
    result = []
    length, offset = chunk["end_sample"] - chunk["start_sample"], chunk["start_sample"]
    for index, segment in enumerate(segments):
        label = f"{chunk['id']} segment {index}"
        start, end = _time_range(segment, rate, length, label)
        text = _text(segment.get("text"), label + ".text")
        mapped_words = []
        if "words" in segment:
            if not isinstance(segment["words"], list):
                raise ManifestError(f"{label}.words must be a list")
            last_start = start
            for word_index, word in enumerate(segment["words"]):
                ws, we = _time_range(word, rate, length, f"{label} word {word_index}", allow_zero=True)
                if ws < start or we > end or ws < last_start:
                    raise ManifestError(f"{label}: word timestamps must be ordered and inside segment")
                last_start = ws
                _text(word.get("word"), label + ".word")
                mapped_words.append({"word": word["word"],
                                     "start_sample": offset + math.floor(ws * rate),
                                     "end_sample": offset + math.ceil(we * rate), "original": word})
        origin = {"chunk_id": chunk["id"], "segment_index": index, "original": segment,
                  "chunk_start_sample": offset,
                  "local_start_sample": math.floor(start * rate), "local_end_sample": math.ceil(end * rate)}
        record = {"text": text, "start_sample": offset + math.floor(start * rate),
                  "end_sample": offset + math.ceil(end * rate), "origins": [origin]}
        if "words" in segment:
            record["words"] = mapped_words
        result.append(record)
    return result, {"chunk_id": chunk["id"], "file": Path(path).name, "sha256": digest,
                    "binding": binding, "metadata": metadata, "segment_count": len(segments)}


def _normalized(text):
    return " ".join(text.split())


def _ref(record):
    origin = record["origins"][0]
    return {"chunk_id": origin["chunk_id"], "segment_index": origin["segment_index"]}


def _exact_times(record, rate):
    origin = record["origins"][0]
    original = origin["original"]
    return tuple(origin["chunk_start_sample"] + seconds(original[key]) * rate for key in ("start", "end"))


def _same_words(left, right, rate):
    # Text alone cannot justify dropping conflicting word alignments.
    if ("words" in left) != ("words" in right):
        return False
    if "words" in left and "words" in right:
        def signature(record):
            offset = record["origins"][0]["chunk_start_sample"]
            return [(w["word"], offset + seconds(w["original"]["start"]) * rate,
                     offset + seconds(w["original"]["end"]) * rate) for w in record["words"]]
        return signature(left) == signature(right)
    return True


def merge_segments(manifest, transcript_dir, *, manifest_sha256, native_json=False):
    rate = manifest["source"]["sample_rate"]
    kept, active, removed, conflicts, transcripts = [], [], [], [], []
    transcript_dir = Path(transcript_dir)
    expected = {chunk["id"] + ".json" for chunk in manifest["chunks"]}
    if not transcript_dir.is_dir():
        raise ManifestError(f"Transcript directory does not exist: {transcript_dir}")
    actual = {path.name for path in transcript_dir.glob("*.json")}
    if actual != expected:
        raise ManifestError(f"Transcript JSON ids mismatch: missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}")
    for chunk in manifest["chunks"]:
        records, provenance = read_transcript(
            transcript_dir / (chunk["id"] + ".json"), chunk, rate,
            recording_sha256=manifest["source"]["sha256"], manifest_sha256=manifest_sha256,
            native_json=native_json)
        transcripts.append(provenance)
        active = [(old, root, old_chunk) for old, root, old_chunk in active
                  if old["end_sample"] > chunk["start_sample"]]
        pending = []
        for record in records:
            duplicate_root = None
            for old, root, old_chunk in active:
                overlap_start = max(chunk["start_sample"], old_chunk["start_sample"])
                overlap_end = min(chunk["end_sample"], old_chunk["end_sample"])
                intersection_start = max(record["start_sample"], old["start_sample"], overlap_start)
                intersection_end = min(record["end_sample"], old["end_sample"], overlap_end)
                if intersection_start >= intersection_end:
                    continue
                identical = (record["start_sample"] == old["start_sample"] and
                             record["end_sample"] == old["end_sample"] and
                             _normalized(record["text"]) == _normalized(old["text"]) and
                             _exact_times(record, rate) == _exact_times(old, rate) and
                             _same_words(record, old, rate) and
                             overlap_start <= record["start_sample"] < record["end_sample"] <= overlap_end)
                if identical:
                    if duplicate_root is None:
                        duplicate_root = root
                else:
                    conflicts.append({"left": _ref(old), "right": _ref(record),
                                      "intersection_start_sample": intersection_start,
                                      "intersection_end_sample": intersection_end,
                                      "reason": "overlapping transcripts differ in text, timing, words, or cross boundary"})
            if duplicate_root is None:
                kept.append(record)
                root = record
            else:
                removed.append({"kept": _ref(duplicate_root), "removed": _ref(record),
                                "start_sample": record["start_sample"], "end_sample": record["end_sample"],
                                "reason": "exact normalized text and sample timing within shared chunk overlap",
                                "origin": record["origins"][0]})
                duplicate_root["origins"].extend(record["origins"])
                root = duplicate_root
            pending.append((record, root, chunk))
        active.extend(pending)
    kept.sort(key=lambda record: (record["start_sample"], record["end_sample"]))
    for index, record in enumerate(kept):
        record.update(id=index, start=record["start_sample"] / rate, end=record["end_sample"] / rate)
        for word in record.get("words", []):
            word.update(start=word["start_sample"] / rate, end=word["end_sample"] / rate)
    report = {"status": "conflicts" if conflicts else "ok", "removed_duplicates": removed,
              "conflicts": conflicts, "input_segments": sum(t["segment_count"] for t in transcripts),
              "output_segments": len(kept), "duplicate_count": len(removed), "conflict_count": len(conflicts)}
    report["identity"] = {"mode": "native_json" if native_json else "strict",
                          "required_fields": [] if native_json else list(IDENTITY_FIELDS),
                          "warnings": [NATIVE_WARNING] if native_json else []}
    return kept, transcripts, report


def _srt_time(milliseconds):
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds_part, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d},{millis:03d}"


def to_srt(segments, rate):
    blocks = []
    for index, segment in enumerate(segments, 1):
        start = segment["start_sample"] * 1000 // rate
        end = (segment["end_sample"] * 1000 + rate - 1) // rate
        text = html.escape(_normalized(segment["text"]), quote=False)
        # Permissive SRT readers (verified with pysubs2 1.8.0) recognize any
        # payload line containing two timestamps as a timing line, even without
        # an arrow. Refuse ambiguous text rather than publish corrupted cues.
        if len(re.findall(r"\d{1,2}:\d{1,2}:\d{1,2}[.,]\d{1,3}", text)) >= 2:
            raise ManifestError(f"Cue {index}: unsupported SRT text contains multiple timestamp-like sequences")
        blocks.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n")
    return "".join(blocks)


def stitch(manifest_path, transcript_dir, output_json, *, native_json=False):
    """Write JSON, sibling .srt, and .report.json after complete validation.

    Conflicts preserve both segments, are included in JSON/report, and produce
    CLI exit code 3. The library returns the merged object for inspection.
    Strict transcript identity is required unless native_json=True explicitly
    opts into weaker compatibility; present identity fields are always checked.
    """
    if type(native_json) is not bool:
        raise ManifestError("native_json must be a boolean")
    manifest_path = Path(manifest_path).resolve()
    manifest, manifest_digest, source_path, source_revision, verified_chunks = _verified_manifest_snapshot(manifest_path)
    segments, transcripts, report = merge_segments(
        manifest, transcript_dir, manifest_sha256=manifest_digest, native_json=native_json)
    output_json = Path(output_json)
    if output_json.suffix.lower() != ".json":
        raise ManifestError("Merged output must have a .json extension")
    result = {"schema_version": 1, "source": manifest["source"],
              "manifest_sha256": manifest_digest, "sample_rate": manifest["source"]["sample_rate"],
              "segments": segments, "transcripts": transcripts, "report": report}
    outputs = {output_json: json_bytes(result),
               output_json.with_suffix(".srt"): to_srt(segments, result["sample_rate"]).encode("utf-8"),
               output_json.with_suffix(".report.json"): json_bytes(report)}
    # Include changes during chunk/transcript validation and serialization. This
    # metadata check is conservative, not atomic with staging/publication.
    _check_source(source_path, source_revision)
    _check_chunks(verified_chunks)
    publish_files(outputs)
    return result
