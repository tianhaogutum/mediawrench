"""Streaming PCM inspection, silence windows, and exact chunk coverage."""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
from decimal import Decimal
from itertools import islice
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import wave

from .common import (ManifestError, _check_source, _source_revision, _unique_object,
                     ceil_samples, floor_samples,
                     json_bytes, read_json_snapshot, seconds, sha256, wav_info)


JOURNAL = "checkpoint.jsonl"
JOURNAL_FORMAT = "audio-chunk-manifest-journal-v1"
JOURNAL_LINE_LIMIT = 512


def _journal_line(record):
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii")
    if len(line) > JOURNAL_LINE_LIMIT:
        raise ManifestError("Checkpoint journal record exceeds 512 bytes")
    return line


def _open_journal(destination, stack):
    path = destination / JOURNAL
    # NONBLOCK prevents a raced FIFO from hanging; fstat validates the opened
    # object, and NOFOLLOW rejects symlinks even if the path changed at open.
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ManifestError(f"Checkpoint journal must be a regular file: {exc}") from exc
    stream = stack.enter_context(os.fdopen(descriptor, "r+b"))
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ManifestError("Checkpoint journal must be a regular file")
    return stream


def _journal_revision(stream):
    current = os.fstat(stream.fileno())
    # Ignore atime: replay and external readers may legitimately update it.
    return current.st_size, current.st_mtime_ns, current.st_ctime_ns


def _check_journal(destination, stream, revision):
    """Conservatively refuse path or metadata changes since replay/own write."""
    opened = os.fstat(stream.fileno())
    try:
        current = (destination / JOURNAL).lstat()
    except OSError as exc:
        raise ManifestError("Checkpoint journal pathname changed since replay") from exc
    if (not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
        raise ManifestError("Checkpoint journal pathname changed since replay")
    if _journal_revision(stream) != revision:
        raise ManifestError("Checkpoint journal contents or metadata changed since replay/own write")


def _append_commit(stream, chunk):
    # Use the retained replay descriptor, never reopen a mutable pathname.
    line = _journal_line({"chunk_id": chunk["id"], "sha256": chunk["sha256"]})
    stream.write(line)
    stream.flush()
    os.fsync(stream.fileno())


def _replay_journal(stream, manifest, manifest_digest):
    """Overlay complete records without modifying disk. Return safe EOF offset."""
    if any("sha256" not in c or c["sha256"] is not None for c in manifest["chunks"]):
        raise ManifestError("Incomplete journal checkpoint must have null base digests")
    header = {"format": JOURNAL_FORMAT, "manifest_sha256": manifest_digest}
    offset, index = 0, -1
    while True:
        line = stream.readline(JOURNAL_LINE_LIMIT + 1)
        if len(line) > JOURNAL_LINE_LIMIT or (len(line) == JOURNAL_LINE_LIMIT and not line.endswith(b"\n")):
            raise ManifestError("Checkpoint journal record exceeds 512 bytes")
        if not line.endswith(b"\n"):
            if index < 0:
                raise ManifestError("Missing complete checkpoint journal header")
            # Only the final unterminated tail can be an interrupted append.
            return offset
        try:
            record = json.loads(line.decode("ascii"), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise ManifestError(f"Malformed checkpoint journal record: {exc}") from exc
        if index < 0:
            if record != header:
                raise ManifestError("Checkpoint journal header/version/manifest binding mismatch")
        else:
            if (not isinstance(record, dict) or set(record) != {"chunk_id", "sha256"}
                    or index >= len(manifest["chunks"])
                    or record["chunk_id"] != manifest["chunks"][index]["id"]
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                raise ManifestError("Invalid or out-of-order checkpoint journal commit")
            manifest["chunks"][index]["sha256"] = record["sha256"]
        offset += len(line)
        index += 1


def supplied_silences(path, rate, total):
    """Return inward-rounded intervals, retaining the value-only helper API."""
    return _supplied_silence_snapshot(path, rate, total)[0]


def _supplied_silence_snapshot(path, rate, total):
    """Validate intervals and retain the digest of the exact bytes parsed."""
    data, digest = read_json_snapshot(path)
    if not isinstance(data, dict) or set(data) != {"intervals"} or not isinstance(data["intervals"], list):
        raise ManifestError('Silence JSON must be {"intervals": [{"start": seconds, "end": seconds}]}')
    result = []
    previous = 0
    for item in data["intervals"]:
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            raise ManifestError("Each silence interval requires only start and end")
        start_time, end_time = seconds(item["start"]), seconds(item["end"])
        start, end = math.ceil(start_time * rate), math.floor(end_time * rate)
        if start_time >= end_time or end_time * rate > total or start >= end or start < previous:
            raise ManifestError("Silence intervals must be ordered, disjoint, in audio, and contain a full sample")
        result.append((start, end))
        previous = end
    return result, digest


def detect_silences(path, info, *, threshold_db=-40, window_seconds="0.02", min_seconds="0.1"):
    try:
        threshold_db = float(threshold_db)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ManifestError("Silence threshold must be finite and between -120 and 0 dBFS") from exc
    if not math.isfinite(threshold_db) or not -120 <= threshold_db <= 0:
        raise ManifestError("Silence threshold must be finite and between -120 and 0 dBFS")
    rate, channels, width = info["sample_rate"], info["channels"], info["sample_width"]
    window = floor_samples(window_seconds, rate, "window seconds")
    minimum = ceil_samples(min_seconds, rate, "minimum silence")
    if not 1 <= window <= rate or minimum < 1:
        raise ManifestError("Energy window must be 1 sample to 1 second; minimum silence must be positive")
    full_scale = 1 << (width * 8 - 1)
    threshold_sq = (full_scale * 10 ** (threshold_db / 20)) ** 2
    intervals, run_start = [], None
    with wave.open(str(path), "rb") as audio:
        position = 0
        while position < info["total_samples"]:
            count = min(window, info["total_samples"] - position)
            raw = audio.readframes(count)
            if len(raw) != count * channels * width:
                raise ManifestError("PCM source changed or was truncated during silence detection")
            squares = [0] * channels
            for index in range(count * channels):
                offset = index * width
                sample = (raw[offset] - 128 if width == 1 else
                          int.from_bytes(raw[offset:offset + width], "little", signed=True))
                squares[index % channels] += sample * sample
            # Every channel must be quiet. Opposite-phase channels cannot cancel.
            quiet = max(squares) / count <= threshold_sq
            if quiet and run_start is None:
                run_start = position
            if not quiet and run_start is not None:
                if position - run_start >= minimum:
                    intervals.append((run_start, position))
                run_start = None
            position += count
        if run_start is not None and position - run_start >= minimum:
            intervals.append((run_start, position))
    return intervals


def plan_chunks(total, maximum, overlap, silences, search):
    chunks = []
    start = 0
    silence_index = 0
    # A silence-chosen end must leave the next chunk at least this far past the
    # current start, otherwise a midpoint at/near start + overlap stalls planning.
    minimum_step = max(1, (maximum - overlap) // 2)
    while start < total:
        end = min(start + maximum, total)
        reason = "source_end" if end == total else "max_duration"
        if end < total:
            lower = max(start + overlap + minimum_step, end - search)
            candidates = []
            while silence_index < len(silences) and silences[silence_index][1] < lower:
                silence_index += 1
            for left, right in islice(silences, silence_index, None):
                if left > end:
                    break
                lo, hi = max(left, lower), min(right, end)
                if lo <= hi:
                    candidates.append(min(max((left + right) // 2, lo), hi))
            if candidates:
                end = max(candidates)
                reason = "silence"
        previous_end = chunks[-1]["end_sample"] if chunks else start
        chunks.append({"id": f"chunk-{len(chunks) + 1:06d}", "start_sample": start,
                       "end_sample": end, "overlap_previous_samples": previous_end - start,
                       "boundary_reason": reason})
        if end == total:
            break
        start = end - overlap
    return chunks


def _write_chunk(source, path, info, start, end):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(info["channels"])
        target.setsampwidth(info["sample_width"])
        target.setframerate(info["sample_rate"])
        source.setpos(start)
        remaining = end - start
        while remaining:
            count = min(65536, remaining)
            block = source.readframes(count)
            if len(block) != count * info["channels"] * info["sample_width"]:
                raise ManifestError("PCM source changed or was truncated during splitting")
            target.writeframesraw(block)
            remaining -= count


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _checkpoint(directory, manifest, *, validate=None):
    """Commit after the closed chunk and its directory entry have been synced."""
    temporary = directory / ".manifest.json.partial"
    # A killed writer may leave this uncommitted file. Never follow a symlink.
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("xb") as stream:
            stream.write(json_bytes(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        if validate is not None:
            validate()
        os.replace(temporary, directory / "manifest.json")
        _sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _committed_revision(path):
    """Conservative file revision for the separate streaming hash/PCM reads."""
    try:
        current = path.lstat()
    except OSError as exc:
        raise ManifestError(f"Completed chunk missing or changed: {path.name}") from exc
    if not stat.S_ISREG(current.st_mode):
        raise ManifestError(f"Completed chunk must be a regular file: {path.name}")
    # Do not include atime: our validation and other readers can update it.
    return (current.st_dev, current.st_ino, current.st_mode, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns)


def _check_committed(path, revision):
    if _committed_revision(path) != revision:
        raise ManifestError(f"Completed chunk identity or metadata changed since validation: {path.name}")


def _check_chunks(validated):
    for path, revision in validated:
        _check_committed(path, revision)


def _hash_chunk(path, validated):
    """Retain the regular pathname revision that supplied a fresh digest."""
    revision = _committed_revision(path)
    digest = sha256(path)
    _check_committed(path, revision)
    validated.append((path, revision))
    return digest


def _load_checkpoint(destination, expected, stack):
    previous, manifest_digest = read_json_snapshot(destination / "manifest.json")
    if (not isinstance(previous, dict) or previous.get("resumable") is not True
            or type(previous.get("complete")) is not bool
            or not isinstance(previous.get("chunks"), list)
            or any(not isinstance(c, dict) for c in previous["chunks"])):
        raise ManifestError("Output is not a resumable split checkpoint")
    if "checkpoint_format" in previous and previous["checkpoint_format"] != "jsonl-v1":
        raise ManifestError("Unsupported checkpoint format")
    identity = {**previous, "complete": False,
                "chunks": [{**c, "sha256": None} for c in previous["chunks"]]}
    # Pre-journal checkpoints retain their original format and write strategy.
    identity["checkpoint_format"] = "jsonl-v1"
    if identity != expected:
        raise ManifestError("Cannot resume: source or parameters differ from checkpoint")
    journal, journal_end, revision = None, None, None
    if previous.get("checkpoint_format") == "jsonl-v1" and not previous["complete"]:
        # Bind commits to the exact bytes that supplied the validated plan,
        # even if the manifest pathname was replaced after acquisition.
        journal = _open_journal(destination, stack)
        revision = _journal_revision(journal)
        journal_end = _replay_journal(journal, previous, manifest_digest)
        _check_journal(destination, journal, revision)
    # Validate ALL committed chunks before writing anything, including on no-op
    # resumes of a complete directory. Null digests never authorize reuse.
    reused = []
    for chunk in previous["chunks"]:
        digest = chunk.get("sha256")
        if digest is None:
            if previous["complete"]:
                raise ManifestError("Complete checkpoint has an unfinished chunk")
            continue
        path = destination / chunk["file"]
        chunk_revision = _committed_revision(path)
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or sha256(path) != digest):
            raise ManifestError(f"Completed chunk missing or SHA256 mismatch: {chunk['id']}")
        _check_committed(path, chunk_revision)
        info = {key: expected["source"][key] for key in ("sample_rate", "channels", "sample_width")}
        info["total_samples"] = chunk["end_sample"] - chunk["start_sample"]
        if wav_info(path) != info:
            raise ManifestError(f"Completed chunk PCM metadata mismatch: {chunk['id']}")
        _check_committed(path, chunk_revision)
        reused.append((path, chunk_revision))
    return previous, journal, journal_end, revision, reused


def _resume_split(source, destination, manifest, info, source_revision):
    try:
        import fcntl
    except ImportError as exc:
        raise ManifestError("Resumable splitting requires POSIX directory locks (tested on Linux)") from exc
    if destination.is_symlink():
        raise ManifestError("Resumable output directory must not be a symlink")
    _check_source(source, source_revision)
    if not destination.exists():
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        try:
            _checkpoint(staging, manifest)
            with (staging / JOURNAL).open("xb") as stream:
                stream.write(_journal_line({"format": JOURNAL_FORMAT,
                                           "manifest_sha256": sha256(staging / "manifest.json")}))
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(staging)
            if destination.exists() or destination.is_symlink():
                raise ManifestError(f"Output directory already exists: {destination}")
            staging.rename(destination)
            _sync_directory(destination.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with ExitStack() as stack:
        stack.callback(os.close, descriptor)
        try:
            # Lock the directory inode: no stale lock file after SIGKILL.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManifestError("Resumable output directory is in use by another writer") from exc
        manifest, journal, journal_end, revision, validated = _load_checkpoint(destination, manifest, stack)
        # A later chunk's validation may have raced a change to an earlier one.
        # Recheck the whole reused set before the first recovery mutation.
        _check_chunks(validated)
        _check_source(source, source_revision)
        if journal is not None:
            # Validate identity/revision after ALL committed WAV checks, before
            # tail repair, partial cleanup, chunk writes or manifest publication.
            _check_journal(destination, journal, revision)
            if os.fstat(journal.fileno()).st_size != journal_end:
                journal.truncate(journal_end)
                journal.flush()
                os.fsync(journal.fileno())
                revision = _journal_revision(journal)
            journal.seek(0, os.SEEK_END)
        with wave.open(str(source), "rb") as audio:
            for chunk in manifest["chunks"]:
                if chunk["sha256"] is not None:
                    continue
                if journal is not None:
                    _check_journal(destination, journal, revision)
                path = destination / chunk["file"]
                temporary = destination / ("." + chunk["file"] + ".partial")
                temporary.unlink(missing_ok=True)
                try:
                    _write_chunk(audio, temporary, info, chunk["start_sample"], chunk["end_sample"])
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    if journal is not None:
                        _check_journal(destination, journal, revision)
                    os.replace(temporary, path)
                    _sync_directory(destination)
                    # Rename can change ctime. Capture/hash the final pathname
                    # after our own publication, then keep ALL revision fields.
                    # Failure here leaves an uncommitted orphan for recovery.
                    chunk["sha256"] = _hash_chunk(path, validated)
                    if manifest.get("checkpoint_format") == "jsonl-v1":
                        _check_journal(destination, journal, revision)
                        _append_commit(journal, chunk)
                        revision = _journal_revision(journal)
                    else:
                        _checkpoint(destination, manifest)
                finally:
                    temporary.unlink(missing_ok=True)
        if sha256(source) != manifest["source"]["sha256"]:
            raise ManifestError("Source changed while planning/splitting")
        # Also guard final publication and the completed no-op return. These
        # checks are not atomic with external writes; exclusive ownership is
        # still required. Retain metadata, not an open descriptor per chunk.
        _check_chunks(validated)
        _check_source(source, source_revision)
        if not manifest["complete"]:
            if journal is not None:
                _check_journal(destination, journal, revision)
            manifest["complete"] = True
            def validate_completion():
                # Include serialization and fsync of the pending complete
                # manifest before replacing the incomplete checkpoint.
                _check_chunks(validated)
                _check_source(source, source_revision)
                if journal is not None:
                    _check_journal(destination, journal, revision)
            _checkpoint(destination, manifest, validate=validate_completion)
        return manifest


def split(source, output_dir, *, max_seconds="30", overlap_seconds="1", silence_json=None,
          detect_silence=False, search_seconds="5", threshold_db=-40,
          window_seconds="0.02", min_silence_seconds="0.1", plan_only=False, resume=False):
    """Create chunks and a version-1 manifest; optionally retain resumable checkpoints.

    Durations are decimal seconds; maxima round down and overlaps round up.
    Plan-only manifests intentionally have null chunk hashes and cannot be stitched.
    Resume validates the source, parameters and every committed chunk before reuse.
    """
    source, destination = Path(source).resolve(), Path(output_dir).absolute()
    if resume and plan_only:
        raise ManifestError("Cannot combine --resume and --plan-only")
    if not resume and (destination.exists() or destination.is_symlink()):
        raise ManifestError(f"Output directory already exists: {destination}")
    if not destination.parent.is_dir():
        raise ManifestError(f"Output parent does not exist: {destination.parent}")
    if silence_json and detect_silence:
        raise ManifestError("Choose supplied silence intervals or energy detection, not both")
    source_revision = _source_revision(source)
    source_hash = sha256(source)
    _check_source(source, source_revision)
    info = wav_info(source)
    _check_source(source, source_revision)
    rate, total = info["sample_rate"], info["total_samples"]
    maximum = floor_samples(max_seconds, rate, "maximum seconds")
    overlap = ceil_samples(overlap_seconds, rate, "overlap seconds")
    search = floor_samples(search_seconds, rate, "search seconds")
    if maximum < 1 or overlap >= maximum:
        raise ManifestError("Maximum must span at least one sample and overlap must be smaller than maximum")
    silences = []
    silence_digest = None
    method = "none"
    if silence_json:
        method = "supplied"
        silences, silence_digest = _supplied_silence_snapshot(silence_json, rate, total)
    elif detect_silence:
        method = "energy"
        silences = detect_silences(source, info, threshold_db=threshold_db,
                                  window_seconds=window_seconds, min_seconds=min_silence_seconds)
    chunks = plan_chunks(total, maximum, overlap, silences, search)
    manifest = {"schema_version": 1, "tool": "audio-chunk-manifest", "plan_only": bool(plan_only),
                "complete": False,
                "source": {"path": os.path.relpath(source, destination), "sha256": source_hash, **info},
                "max_samples": maximum, "overlap_samples": overlap,
                "silence": {"method": method, "search_samples": search,
                            "intervals_samples": [list(item) for item in silences]}, "chunks": chunks}
    if detect_silence:
        # Match read_json's exact decimal representation of the JSON token.
        # Comparing Decimal('-39.1') to its binary float would reject our own
        # freshly written checkpoint even on the initial resumable invocation.
        manifest["silence"].update(threshold_db=Decimal(str(float(threshold_db))),
                                   window_samples=floor_samples(window_seconds, rate),
                                   min_silence_samples=ceil_samples(min_silence_seconds, rate))
    for chunk in chunks:
        chunk.update(file=chunk["id"] + ".wav", sha256=None)
    # All modes bind hashing, PCM inspection and silence/geometry planning to
    # one resolved regular-file revision, before creating any staging paths.
    _check_source(source, source_revision)
    if resume:
        manifest["resumable"] = True
        manifest["checkpoint_format"] = "jsonl-v1"
        # Canonical exact times also distinguish requests with identical rounded
        # geometry. Inactive detector options are intentionally irrelevant.
        manifest["split_parameters"] = {
            "max_seconds": str(seconds(max_seconds)), "overlap_seconds": str(seconds(overlap_seconds)),
            "search_seconds": str(seconds(search_seconds)),
            "silence_json_sha256": silence_digest}
        if detect_silence:
            manifest["split_parameters"].update(window_seconds=str(seconds(window_seconds)),
                                                min_silence_seconds=str(seconds(min_silence_seconds)))
        # Energy detection and supplied-silence planning may read other files
        # after source inspection. Refuse a changed source before recovery.
        _check_source(source, source_revision)
        return _resume_split(source, destination, manifest, info, source_revision)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        validated = []
        with wave.open(str(source), "rb") as audio:
            for chunk in chunks:
                if not plan_only:
                    path = staging / chunk["file"]
                    _write_chunk(audio, path, info, chunk["start_sample"], chunk["end_sample"])
                    chunk["sha256"] = _hash_chunk(path, validated)
        if sha256(source) != source_hash:
            raise ManifestError("Source changed while planning/splitting")
        _check_source(source, source_revision)
        _check_chunks(validated)
        manifest["complete"] = not plan_only
        (staging / "manifest.json").write_bytes(json_bytes(manifest))
        # Include copying, the final hash and manifest serialization. A restored
        # digest does not excuse a changed revision. This check and rename are
        # not atomic with external writers; no immutable PCM snapshot is held.
        _check_source(source, source_revision)
        _check_chunks(validated)
        if destination.exists() or destination.is_symlink():
            raise ManifestError(f"Output directory already exists: {destination}")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest
