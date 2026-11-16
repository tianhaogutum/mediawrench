"""Conservative content checks using external ffprobe and per-stream FFmpeg decode."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from threading import Event
from typing import Any

# Only self-contained local containers; excludes playlists, concat, image sequences,
# devices and script demuxers. These names are FFmpeg demuxer names, not extensions.
FORMATS = "matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,avi,ogg,wav,flac,mp3,aac,mpeg,mpegts,asf,flv"
EXIT_CODES = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}


@dataclass(frozen=True)
class Options:
    ffmpeg: str | None = None
    ffprobe: str | None = None
    timeout: float = 30.0  # Per invocation, including each stream decode.
    absolute_tolerance: float = 0.25
    relative_tolerance: float = 0.02
    quick_seconds: float | None = None
    allow_drop: tuple[str, ...] = ()
    subtitles: str = "ignore"
    deadline: float | None = None  # Whole comparison budget, including tool versions.
    stream_map: tuple[tuple[int, int], ...] | None = None  # Absolute source/output AV indices.
    language_tags: str = "ignore"  # Exact tag preservation for explicitly mapped AV pairs.

    def validate(self) -> None:
        for name in ("timeout", "absolute_tolerance", "relative_tolerance"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name == "timeout" and value == 0):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'timeout' else 'nonnegative'}")
        if self.deadline is not None and (not math.isfinite(self.deadline) or self.deadline <= 0):
            raise ValueError("deadline must be finite and positive")
        if self.quick_seconds is not None and (not math.isfinite(self.quick_seconds) or self.quick_seconds <= 0):
            raise ValueError("quick_seconds must be finite and positive")
        if set(self.allow_drop) - {"audio", "video"}:
            raise ValueError("allow_drop accepts only audio/video")
        if self.subtitles not in ("ignore", "preserve"):
            raise ValueError("subtitles must be ignore or preserve")
        if self.language_tags not in ("ignore", "preserve"):
            raise ValueError("language_tags must be ignore or preserve")
        if self.language_tags == "preserve" and self.stream_map is None:
            raise ValueError("language_tags=preserve requires an explicit stream_map (--stream-map)")
        if self.stream_map is not None:
            if (not isinstance(self.stream_map, (tuple, list)) or
                    any(not isinstance(pair, (tuple, list)) or len(pair) != 2 or
                        any(type(index) is not int or index < 0 for index in pair)
                        for pair in self.stream_map)):
                raise ValueError("stream_map must contain pairs of nonnegative integer SOURCE:OUTPUT indices")


def exit_code(report: dict) -> int:
    return EXIT_CODES[report["verdict"]]


class CheckError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def resolve_tool(name: str, explicit: str | None = None) -> str:
    """Explicit path, then PATH. FFmpeg is never downloaded or bundled."""
    candidate = explicit or shutil.which(name)
    if not candidate:
        raise CheckError("UNKNOWN", f"Missing {name}; install FFmpeg or supply --{name} PATH")
    resolved = shutil.which(str(candidate))
    if not resolved:
        raise CheckError("UNKNOWN", f"Executable unavailable: {candidate}")
    return str(Path(resolved).resolve())


class JobStopped(CheckError):
    def __init__(self, reason: str, message: str):
        super().__init__("UNKNOWN", message)
        self.reason = reason


class _JobControl:
    def __init__(self, started: float, deadline: float | None, cancel_event: Event | None):
        self.expires = None if deadline is None else started + deadline
        self.deadline = deadline
        self.cancel_event = cancel_event

    def checkpoint(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobStopped("cancelled", "Comparison cancelled")
        if self.expires is not None and time.monotonic() >= self.expires:
            raise JobStopped("deadline", f"Whole-job deadline of {self.deadline:g}s exceeded")

    def remaining(self) -> float:
        return math.inf if self.expires is None else max(0, self.expires - time.monotonic())


def _kill_group(proc: subprocess.Popen) -> None:
    # Kill descendants even when the direct tool has already exited. Reap the
    # direct child on every path, including KeyboardInterrupt and parse errors.
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        # macOS reports EPERM instead of ESRCH for a group whose members are all zombies.
        pass
    proc.wait()


def _run(argv: list[str], timeout: float, history: list[dict], *, job: _JobControl,
         decode: bool = False) -> Any:
    """Spool output privately; enforce both invocation and shared job budgets."""
    job.checkpoint()
    record = {"argv": argv, "timeout_seconds": timeout}
    history.append(record)
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    start = time.monotonic()
    record["job_remaining_seconds"] = None if job.expires is None else round(job.remaining(), 6)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = None
        try:
            try:
                job.checkpoint()
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                        env=env, start_new_session=(os.name == "posix"))
                while True:
                    job.checkpoint()
                    remaining = timeout - (time.monotonic() - start)
                    if remaining <= 0:
                        record.update(timed_out=True, stop_reason="subprocess_timeout")
                        raise CheckError("UNKNOWN", f"Subprocess exceeded {timeout:g}s timeout")
                    try:
                        proc.wait(timeout=min(0.1, remaining, job.remaining()))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                job.checkpoint()
            except KeyboardInterrupt as exc:
                record["stop_reason"] = "cancelled"
                raise JobStopped("cancelled", "Comparison interrupted") from exc
            except JobStopped as exc:
                record["stop_reason"] = exc.reason
                if exc.reason == "deadline":
                    record["timed_out"] = True
                raise
            finally:
                if proc is not None:
                    _kill_group(proc)
                    record["returncode"] = proc.returncode
                err.seek(0)
                diagnostic = err.read(16384).decode("utf-8", errors="replace").strip()
                if diagnostic:
                    record["stderr_excerpt"] = diagnostic
            if proc.returncode != 0:
                unsupported = ("decoder" in diagnostic.lower() and
                               any(s in diagnostic.lower() for s in ("not found", "unknown", "unsupported")))
                status = "UNKNOWN" if unsupported or proc.returncode < 0 else "FAIL"
                raise CheckError(status, diagnostic or f"Subprocess exited {proc.returncode}")
            if diagnostic and decode:
                raise CheckError("FAIL", diagnostic)
            job.checkpoint()
            out.seek(0)
            if decode:
                return _frame_facts(out, checkpoint=job.checkpoint)
            data = out.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise CheckError("UNKNOWN", "Probe output exceeded 8 MiB")
            job.checkpoint()
            return data.decode("utf-8", errors="replace")
        except OSError as exc:
            raise CheckError("UNKNOWN", f"Cannot execute tool: {exc}") from exc
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - start, 6)


def _frame_facts(lines, *, checkpoint=None) -> dict:
    timebase = None
    frames = size = 0
    first = last = None
    try:
        for number, raw in enumerate(lines):
            if checkpoint is not None and number % 256 == 0:
                checkpoint()
            line = raw.decode("ascii").strip()
            if line.startswith("#tb 0:"):
                declared = Fraction(line.split(":", 1)[1].strip())
                if declared <= 0:
                    raise ValueError("nonpositive framehash timebase for output stream 0")
                if timebase is not None and declared != timebase:
                    raise ValueError("conflicting framehash timebase declarations for output stream 0")
                timebase = declared
            elif line and not line.startswith("#"):
                parts = [part.strip() for part in line.split(",")]
                if len(parts) != 6 or timebase is None or timebase <= 0:
                    raise ValueError("unexpected framehash record")
                # Each invocation maps one input stream to output stream 0;
                # the framehash identifier is not the absolute input index.
                if not parts[0] or any(digit != "0" for digit in parts[0]):
                    raise ValueError(f"unexpected framehash stream identifier {parts[0][:80]!r}; expected output stream 0")
                # DTS must be an integer token, but only PTS measures the span.
                # Do not constrain DTS magnitude or its ordering relative to PTS.
                dts_digits = parts[1][1:] if parts[1].startswith(("+", "-")) else parts[1]
                if not dts_digits or any(digit not in "0123456789" for digit in dts_digits):
                    raise ValueError(f"malformed framehash DTS {parts[1][:80]!r}; expected signed ASCII integer")
                pts, duration, byte_count = int(parts[2]), int(parts[3]), int(parts[4])
                if duration <= 0 or byte_count <= 0:
                    raise ValueError("nonpositive decoded frame duration/size")
                begin, end = pts * timebase, (pts + duration) * timebase
                first = begin if first is None else min(first, begin)
                last = end if last is None else max(last, end)
                frames += 1
                size += byte_count
        if checkpoint is not None:
            checkpoint()
        # Convert the exact ratios only after parsing. A finite rational can
        # exceed float seconds or a positive span can round to zero; neither
        # permits certifying that stream's frames, bytes or timestamps.
        span = last - first if first is not None else None
        seconds = float(span) if span is not None else None
        if span is not None and span > 0 and seconds == 0:
            raise ValueError("positive decoded timestamp span underflows to zero seconds")
        return {"frames": frames, "decoded_bytes": size,
                "start_seconds": float(first) if first is not None else None,
                "end_seconds": float(last) if last is not None else None,
                "duration_seconds": seconds}
    except (ValueError, UnicodeError, ZeroDivisionError, OverflowError) as exc:
        raise CheckError("UNKNOWN", f"Cannot interpret decoded framehash: {exc}") from exc


def _probe_number(value: str) -> float:
    """Reject overflow and JSON parser constants before publishing any inventory."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite number in probe JSON")
    return number


def _positive(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        # Finite JSON integers may exceed the float range without invalidating
        # the probe inventory; they cannot supply a known positive duration.
        return None


def _media_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a regular local file: {path}")
    with path.open("rb") as media:
        media.read(1)
    return path


def _check(report: dict, name: str, status: str, actual=None, expected=None, **facts) -> None:
    report["checks"].append(dict(name=name, status=status, actual=actual, expected=expected, **facts))


def _duration(report: dict, name: str, source, output, options: Options, **context) -> None:
    source, output = _positive(source), _positive(output)
    if source is None or output is None:
        _check(report, name, "UNKNOWN", output, "positive known duration", source=source, **context)
        return
    delta = abs(source - output)
    tolerance = options.relative_tolerance * source
    if math.isfinite(tolerance):
        tolerance += options.absolute_tolerance
    if not math.isfinite(tolerance):
        _check(report, name, "UNKNOWN", output, source, delta_seconds=delta,
               tolerance_seconds=None,
               diagnostic="Duration tolerance is not representable as finite seconds",
               **context)
        return
    _check(report, name, "PASS" if delta <= tolerance else "FAIL", output, source,
           delta_seconds=delta, tolerance_seconds=tolerance, **context)


def _resolve_mapping(report: dict, options: Options) -> None:
    """Resolve only caller identities or the sole pair of a type; never infer tracks."""
    mapping = report["mapping"]
    sides = {role: {s["index"]: s for s in report["media"][role]["streams"]}
             for role in ("source", "output")}
    pairs, errors = [], []
    if options.stream_map is not None:
        used = {"source": set(), "output": set()}
        for source, output in options.stream_map:
            valid = True
            for role, index in (("source", source), ("output", output)):
                if index in used[role]:
                    errors.append(f"duplicate {role} stream {index}")
                    valid = False
                used[role].add(index)
                if index not in sides[role]:
                    errors.append(f"{role} stream {index} does not exist")
                    valid = False
                elif sides[role][index].get("codec_type") not in ("audio", "video"):
                    errors.append(f"{role} stream {index} is not audio/video; stream maps accept AV only")
                    valid = False
            if valid:
                kind = sides["source"][source].get("codec_type")
                if kind != sides["output"][output].get("codec_type"):
                    errors.append(f"type mismatch for source {source} -> output {output}")
                else:
                    pairs.append(dict(source_stream=source, output_stream=output, codec_type=kind))
        for role in sides:
            for index, stream in sides[role].items():
                kind = stream.get("codec_type")
                if (kind in ("audio", "video") and index not in used[role] and
                        (role == "output" or kind not in options.allow_drop)):
                    errors.append(f"uncovered {role} {kind} stream {index}")
        # An invalid explicit plan never authorizes even a subset of its pairs.
        mapping.update(status="FAIL" if errors else "PASS", pairs=[] if errors else pairs, errors=errors)
    else:
        for kind in ("audio", "video"):
            before, after = ([s for s in sides[role].values() if s.get("codec_type") == kind]
                             for role in ("source", "output"))
            if after and (len(before) > 1 or len(after) > 1):
                errors.append(f"ambiguous {kind} identity; supply --stream-map SOURCE:OUTPUT for every retained AV stream")
            elif len(before) == len(after) == 1:
                pairs.append(dict(source_stream=before[0]["index"], output_stream=after[0]["index"], codec_type=kind))
        mapping.update(status="UNKNOWN" if errors else "PASS", pairs=pairs, errors=errors)
    _check(report, "stream_mapping", mapping["status"], mapping["errors"] or mapping["pairs"],
           "one-to-one, type-matched coverage of retained AV streams")


def _stream_validation(report: dict, options: Options) -> dict:
    """Include uncovered and interrupted streams, not just successful comparisons."""
    result = {}
    for role in ("source", "output"):
        if role not in report["media"]:
            result[role] = None  # Inventory is unknown until a successful probe.
            continue
        rows = []
        for stream in report["media"][role]["streams"]:
            index, kind = stream["index"], stream.get("codec_type")
            av = kind in ("audio", "video")
            pair = next((p for p in report["mapping"]["pairs"] if p[f"{role}_stream"] == index), None)
            dropped = (av and role == "source" and pair is None and kind in options.allow_drop
                       and report["mapping"]["status"] == "PASS")
            duration = next((c["status"] for c in report["checks"]
                             if c["name"] == f"{kind}_decoded_duration" and c.get(f"{role}_stream") == index), None)
            language = next((c["status"] for c in report["checks"]
                             if c["name"] == "language_tag" and c.get(f"{role}_stream") == index), None)
            decode = next((c["status"] for c in report["checks"]
                           if c.get("role") == role and c.get("stream_index") == index and
                           c["name"] in (f"{kind}_decoded_frames", f"{kind}_decode")), None)
            subtitle = kind == "subtitle" and options.subtitles == "preserve"
            count = next((c["status"] for c in report["checks"]
                          if c["name"] == "subtitle_stream_count"), None) if subtitle else None
            rows.append({"stream_index": index, "codec_type": kind,
                         "language": stream.get("tags", {}).get("language"),
                         "disposition": stream.get("disposition", {}),
                         "mapping_status": "mapped" if pair else "allowed_drop" if dropped else "unresolved" if av else "not_required",
                         "peer_stream": pair["output_stream" if role == "source" else "source_stream"] if pair else None,
                         "decode_coverage": report["requested_coverage"] if index in report["decoded"][role] else "none",
                         "decode_status": (decode or "not_run") if av else "not_required",
                         "duration_status": (duration or ("not_run" if pair else "not_required" if dropped else "unmapped")) if av else "not_required",
                         "language_tag_status": (language or ("not_run" if pair or report["mapping"]["status"] == "not_run"
                                                             else "unmapped"))
                         if av and options.language_tags == "preserve" and not dropped else "not_required",
                         "subtitle_count_status": (count or "not_run") if subtitle else "not_required"})
        result[role] = rows
    return result


def _refresh_report(report: dict, options: Options, started: float, *, final: bool) -> None:
    """Derive consistent coverage for both persisted snapshots and final results."""
    report["report_state"] = "complete" if final else "in_progress"
    progress = report["progress"]
    for facts in progress.values():
        if facts["discovered_streams"] is not None:
            facts["pending_streams"] = [index for index in facts["discovered_streams"]
                                        if index not in facts["completed_streams"]]
    report["coverage"] = "partial"
    if (final and report["requested_coverage"] == "full" and report["termination"] is None
            and all(p["probe_completed"] and p["pending_streams"] == [] for p in progress.values())):
        report["coverage"] = "full"
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    report["stream_validation"] = _stream_validation(report, options)
    statuses = {check["status"] for check in report["checks"]}
    report["verdict"] = ("FAIL" if "FAIL" in statuses else "UNKNOWN"
                         if not final or "UNKNOWN" in statuses or not statuses else "PASS")
    if any(c["name"] == "checkpoint_write" for c in report["checks"]):
        report["verdict"] = "UNKNOWN"  # Persistence failure always exits 2.


def compare(source: str | Path, output: str | Path, options: Options | None = None,
            *, cancel_event: Event | None = None, checkpoint_report: str | Path | None = None) -> dict:
    """Inspect both files without modifying either. Returns a JSON-serializable report.

    FAIL dominates UNKNOWN: a proven failed check rejects even if another check
    could not run. PASS requires every performed required check to pass.
    Set cancel_event to stop cooperatively without installing signal handlers;
    completed stream facts survive cancellation or an Options.deadline expiry.
    With checkpoint_report, atomically persist an initial in-progress snapshot,
    each probe/decode outcome, resolved mapping/tag decisions, and the final
    result. A failed
    write stops the job and forces UNKNOWN, overriding any media verdict.
    """
    started = time.monotonic()
    options = options or Options()
    job = _JobControl(started, options.deadline, cancel_event)
    decoded = {"source": {}, "output": {}}
    progress = {role: {"probe_completed": False, "discovered_streams": None,
                       "attempted_streams": [], "completed_streams": [], "pending_streams": None}
                for role in ("source", "output")}
    report = {"schema_version": 1, "verdict": "UNKNOWN",
              "coverage": "partial",
              "requested_coverage": "partial" if options.quick_seconds is not None else "full",
              "source": str(source), "output": str(output), "policy": {key: str(value) if isinstance(value, float) and not math.isfinite(value) else value
                         for key, value in asdict(options).items()},
              "tools": {}, "media": {}, "checks": [], "commands": [],
              "mapping": {"mode": "explicit" if options.stream_map is not None else "unambiguous",
                          "status": "not_run", "pairs": [], "errors": []},
              "decoded": decoded, "progress": progress, "termination": None}
    checkpoint_ready = checkpoint_failed = False

    def persist_checkpoint() -> None:
        nonlocal checkpoint_failed
        try:
            write_report(report, checkpoint_report, source, output)
        except (OSError, ValueError) as exc:
            checkpoint_failed = True
            _check(report, "checkpoint_write", "UNKNOWN", str(exc), "safe writable checkpoint path")
            raise JobStopped("checkpoint_write", f"Cannot write checkpoint: {exc}") from exc

    try:
        options.validate()
        job.checkpoint()
        src, dst = _media_path(source), _media_path(output)
        if os.path.samefile(src, dst):
            raise ValueError("Input and output refer to the same file (including links)")
        report.update(source=str(src), output=str(dst))
        if checkpoint_report is not None:
            _refresh_report(report, options, started, final=False)
            persist_checkpoint()  # Validate destination and clear stale results before tools.
            checkpoint_ready = True
        binaries = {name: resolve_tool(name, getattr(options, name)) for name in ("ffprobe", "ffmpeg")}
        for name, binary in binaries.items():
            try:
                version = _run([binary, "-version"], options.timeout, report["commands"], job=job)
            except JobStopped:
                raise
            except CheckError as exc:
                raise CheckError("UNKNOWN", f"Cannot establish {name} version: {exc}") from exc
            report["tools"][name] = {"path": binary, "version": version.splitlines()[0] if version else "unknown"}
        for role, path in (("source", src), ("output", dst)):
            try:
                probe = _run([binaries["ffprobe"], "-v", "error", "-threads", "1",
                              "-protocol_whitelist", "file,pipe", "-format_whitelist", FORMATS,
                              "-show_entries", "format=format_name,duration:stream=index,codec_type,codec_name,duration:stream_tags=language:stream_disposition",
                              "-of", "json", str(path)], options.timeout, report["commands"], job=job)
                data = json.loads(probe, parse_float=_probe_number, parse_constant=_probe_number)
                job.checkpoint()
                streams = data["streams"]
                if (not isinstance(streams, list) or
                        any(type(s.get("index")) is not int or s["index"] < 0 for s in streams) or
                        len({s["index"] for s in streams}) != len(streams)):
                    raise ValueError("Invalid streams in probe JSON")
                # Validate every stream before publishing any part of the inventory:
                # diagnostic rows render tags even when language policy is ignore.
                for stream in streams:
                    tags = stream.get("tags", {})
                    if (not isinstance(tags, dict) or
                            ("language" in tags and not isinstance(tags["language"], str))):
                        raise ValueError(f"Invalid language tags for stream {stream['index']} in probe JSON")
                if "format" in data and not isinstance(data["format"], dict):
                    raise ValueError("Invalid format object in probe JSON")
                report["media"][role] = data
                progress[role]["probe_completed"] = True
                progress[role]["discovered_streams"] = [s["index"] for s in streams
                                                          if s.get("codec_type") in ("audio", "video")]
                _check(report, "probe", "PASS", len(streams), "valid stream headers", role=role)
            except JobStopped:
                raise
            except CheckError as exc:
                _check(report, "probe", exc.status, str(exc), "readable supported media", role=role)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                _check(report, "probe", "UNKNOWN", str(exc), "valid ffprobe JSON", role=role)
            if checkpoint_ready:
                _refresh_report(report, options, started, final=False)
                persist_checkpoint()  # Keep every finished probe outcome before the next tool.
        if "source" in report["media"] and "output" in report["media"]:
            _resolve_mapping(report, options)
            if options.language_tags == "preserve":
                # The validated explicit plan alone establishes identity. Tags
                # are compared afterward and never choose or repair a pair.
                sides = {role: {s["index"]: s for s in report["media"][role]["streams"]}
                         for role in ("source", "output")}
                for pair in report["mapping"]["pairs"]:
                    before = sides["source"][pair["source_stream"]].get("tags", {}).get("language")
                    after = sides["output"][pair["output_stream"]].get("tags", {}).get("language")
                    _check(report, "language_tag", "PASS" if before == after else "FAIL", after, before, **pair)
            if checkpoint_ready:
                _refresh_report(report, options, started, final=False)
                persist_checkpoint()  # Keep identity/tag evidence before the first decode.
        for role, path in (("source", src), ("output", dst)):
            for stream in report["media"].get(role, {}).get("streams", []):
                kind, index = stream.get("codec_type"), stream["index"]
                if kind not in ("audio", "video"):
                    continue
                argv = [binaries["ffmpeg"], "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                        "-filter_threads", "1", "-filter_complex_threads", "1", "-threads", "1",
                        "-err_detect", "explode", "-protocol_whitelist", "file,pipe",
                        "-format_whitelist", FORMATS, "-copyts", "-start_at_zero", "-i", str(path), "-map", f"0:{index}"]
                if options.quick_seconds is not None:
                    argv += ["-t", str(options.quick_seconds)]
                argv += ["-c:v", "rawvideo", "-c:a", "pcm_s16le", "-threads", "1"]
                if kind == "video":
                    argv += ["-fps_mode", "passthrough"]
                argv += ["-f", "framehash", "-hash", "adler32", "pipe:1"]
                job.checkpoint()
                progress[role]["attempted_streams"].append(index)
                try:
                    facts = _run(argv, options.timeout, report["commands"], job=job, decode=True)
                    decoded[role][index] = facts
                    progress[role]["completed_streams"].append(index)
                    _check(report, f"{kind}_decoded_frames", "PASS" if facts["frames"] > 0 else "FAIL",
                           facts["frames"], ">0", role=role, stream_index=index, **facts)
                    if checkpoint_ready:
                        _refresh_report(report, options, started, final=False)
                        persist_checkpoint()
                except JobStopped:
                    raise
                except CheckError as exc:
                    _check(report, f"{kind}_decode", exc.status, str(exc), "error-free decode", role=role, stream_index=index)
                    if checkpoint_ready:
                        _refresh_report(report, options, started, final=False)
                        persist_checkpoint()  # Retain failure evidence before any next stream starts.
        job.checkpoint()
        if "source" in report["media"] and "output" in report["media"]:
            sides = {role: report["media"][role]["streams"] for role in ("source", "output")}
            for role in sides:
                count = sum(s.get("codec_type") in ("audio", "video") for s in sides[role])
                _check(report, "av_stream_presence", "PASS" if count else "FAIL", count, ">0", role=role)
            kinds = ("audio", "video", "subtitle") if options.subtitles == "preserve" else ("audio", "video")
            for kind in kinds:
                before = [s for s in sides["source"] if s.get("codec_type") == kind]
                after = [s for s in sides["output"] if s.get("codec_type") == kind]
                allow_drop = kind in options.allow_drop
                ok = len(after) <= len(before) if allow_drop else len(after) == len(before)
                _check(report, f"{kind}_stream_count", "PASS" if ok else "FAIL", len(after),
                       f"0..{len(before)}" if allow_drop else len(before))
            for pair in report["mapping"]["pairs"]:
                da = decoded["source"].get(pair["source_stream"], {}).get("duration_seconds")
                db = decoded["output"].get(pair["output_stream"], {}).get("duration_seconds")
                _duration(report, f"{pair['codec_type']}_decoded_duration", da, db, options,
                          source_stream=pair["source_stream"], output_stream=pair["output_stream"])
            _duration(report, "container_duration", report["media"]["source"].get("format", {}).get("duration"),
                      report["media"]["output"].get("format", {}).get("duration"), options)
        job.checkpoint()
    except (JobStopped, KeyboardInterrupt) as exc:
        reason = exc.reason if isinstance(exc, JobStopped) else "cancelled"
        report["termination"] = {"reason": reason, "message": str(exc) or "Comparison interrupted"}
        _check(report, "job_termination", "UNKNOWN", report["termination"]["message"], "completed comparison")
    except (OSError, ValueError, CheckError) as exc:
        _check(report, "preflight", exc.status if isinstance(exc, CheckError) else "UNKNOWN",
               str(exc), "valid local paths, options and executable tools")
    _refresh_report(report, options, started, final=True)
    if checkpoint_ready and not checkpoint_failed:
        try:
            persist_checkpoint()
        except JobStopped as exc:
            report["termination"] = {"reason": exc.reason, "message": str(exc)}
            _refresh_report(report, options, started, final=True)
    return report


def write_report(report: dict, destination: str | Path, source: str | Path, output: str | Path) -> None:
    """Atomically write JSON, refusing to overwrite either media path or alias."""
    dest = Path(destination).expanduser().absolute()
    for media in (Path(source).expanduser(), Path(output).expanduser()):
        if dest.resolve() == media.resolve() or (dest.exists() and media.exists() and os.path.samefile(dest, media)):
            raise ValueError("Report destination aliases a media file; refusing to overwrite")
    if dest.is_symlink():
        raise ValueError("Report destination must not be a symlink")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dest.parent,
                                         prefix=".postflight-", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, dest)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
