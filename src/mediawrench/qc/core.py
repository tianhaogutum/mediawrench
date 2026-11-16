"""Pre-delivery checks for one deliverable against its master. Read-only.

Each check is a thin adapter over an existing mediawrench library function:

- streams, decode, duration: ``postflight.compare`` (ffprobe + full FFmpeg decode of
  every audio/video stream in both files).
- loudness: the ``loudness`` measurement and gate (``Profile.filter``, ``parse_loudnorm``,
  ``gate``) run on the deliverable's first audio stream.
- chapters: ``chapters.loads``/``dumps`` for the expected list, then
  ``chapters.media.verify_media`` (ffprobe) on the deliverable.
- subtitles: the strict SRT reader from ``cutlist`` or the WebVTT reader from ``rollup``,
  with ``reflow``'s default reading limits (42 characters per line, 20 characters/second).

Statuses: PASS, WARN (style limits), FAIL, UNKNOWN (ran but could not decide) and
SKIPPED (not requested, or FFmpeg is not installed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil

from . import SCHEMA, __version__

STATUSES = ("PASS", "WARN", "FAIL", "UNKNOWN", "SKIPPED")
SEVERITY = {"PASS": 0, "SKIPPED": 0, "WARN": 1, "UNKNOWN": 2, "FAIL": 3}
# --fail-on LEVEL: the statuses that make the exit status 1.
FAIL_ON = {"fail": {"FAIL"}, "unknown": {"FAIL", "UNKNOWN"},
           "warn": {"FAIL", "UNKNOWN", "WARN"}, "skipped": {"FAIL", "UNKNOWN", "WARN", "SKIPPED"}}
CHAPTER_SUFFIXES = {".json": "json", ".xml": "matroska", ".txt": "text",
                    ".ffmetadata": "ffmetadata", ".ffmeta": "ffmetadata", ".meta": "ffmetadata"}
ROLE = {"source": "master", "output": "deliverable"}
MAX_DETAILS = 5


class QCInputError(ValueError):
    """Unusable arguments or inputs: exit status 2, no check is reported."""


@dataclass
class Options:
    ffmpeg: str | None = None
    ffprobe: str | None = None
    timeout: float = 60.0
    quick: float | None = None
    allow_drop: tuple[str, ...] = ()
    target_i: float | None = None
    target_tp: float | None = None
    tolerance: float | None = None
    chapters: Path | None = None
    chapters_format: str | None = None
    chapter_tolerance_ms: float = 1.0
    subtitles: list[Path] = field(default_factory=list)
    max_cps: float | None = None
    max_chars: int | None = None
    max_lines: int = 2


def _check(name, status, summary, details=(), source="", **facts):
    return {"name": name, "status": status, "summary": summary,
            "details": list(details)[:MAX_DETAILS], "source": source, "facts": facts}


def _stamp(us) -> str:
    ms = round(float(us) / 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _seconds(value) -> float:
    """postflight and chapters keep exact values; rationals arrive as 'n/d' strings."""
    if isinstance(value, str) and "/" in value:
        top, bottom = value.split("/")
        return int(top) / int(bottom)
    return float(value)


def _tool(name: str, explicit: str | None) -> str | None:
    found = shutil.which(explicit or name)
    if explicit and not found:
        raise QCInputError(f"--{name} {explicit}: not an executable")
    return found


def _chapter_format(path: Path, text: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if text.lstrip("﻿").startswith(";FFMETADATA1"):
        return "ffmetadata"
    if path.suffix.lower() in CHAPTER_SUFFIXES:
        return CHAPTER_SUFFIXES[path.suffix.lower()]
    raise QCInputError(f"{path}: cannot tell the chapter format from the name; pass --chapters-format")


def validate(master: Path, deliverable: Path, options: Options) -> dict:
    """Check arguments and readable inputs before any tool runs; return resolved tools."""
    from ..chapters.core import ChapterError, loads
    from ..loudness.core import AudioError, Profile
    for label, path in (("master", master), ("deliverable", deliverable), *(("subtitles", p) for p in options.subtitles)):
        if not path.is_file():
            raise QCInputError(f"{label} {path}: not a readable file")
    if master.resolve() == deliverable.resolve():
        raise QCInputError("master and deliverable are the same file")
    targets = (options.target_i, options.target_tp, options.tolerance)
    if any(v is not None for v in targets):
        if None in targets:
            raise QCInputError("loudness needs all of --target-i, --target-tp and --tolerance")
        try:
            Profile(*targets).validate()
        except AudioError as exc:
            raise QCInputError(f"loudness target: {exc}") from exc
    if options.chapters is not None:
        try:
            text = options.chapters.read_text(encoding="utf-8")
            options.chapters_format = _chapter_format(options.chapters, text, options.chapters_format)
            # Text lists have no end times; the real check ends the last chapter at the deliverable's end.
            loads(text, options.chapters_format, duration="359999" if options.chapters_format == "text" else None)
        except (OSError, UnicodeError, ChapterError) as exc:
            raise QCInputError(f"chapters {options.chapters}: {exc}") from exc
    if options.chapter_tolerance_ms < 0 or options.timeout <= 0 or options.max_lines < 1:
        raise QCInputError("--chapter-tolerance-ms must be >= 0, --timeout > 0 and --max-lines >= 1")
    return {"ffmpeg": _tool("ffmpeg", options.ffmpeg), "ffprobe": _tool("ffprobe", options.ffprobe)}


# --- streams, decode, duration: mediawrench postflight -----------------------------------

def _bucket(name: str) -> str | None:
    if name in ("probe", "av_stream_presence", "language_tag") or name.endswith("_stream_count"):
        return "streams"
    if name.endswith(("_decoded_frames", "_decode")):
        return "decode"
    if name.endswith("_duration"):
        return "duration"
    return None  # preflight, job_termination: apply to every bucket


def _worst(statuses) -> str:
    statuses = list(statuses)
    return max(statuses, key=SEVERITY.get) if statuses else "UNKNOWN"


def _streams_text(rows) -> tuple[str, list[str]]:
    parts, details = [], []
    order = {"video_stream_count": 0, "audio_stream_count": 1}
    for c in sorted(rows, key=lambda c: order.get(c["name"], 2)):
        if c["name"].endswith("_stream_count"):
            kind = c["name"].removesuffix("_stream_count")
            if c["actual"] == 0 and str(c["expected"]) in ("0", "0..0"):
                continue  # neither file has this kind of stream
            note = "" if c["status"] == "PASS" else f" (expected {c['expected']})"
            parts.append(f"{kind} {c['expected']} → {c['actual']}{note}".replace("0..", "≤"))
        elif c["status"] != "PASS":
            details.append(f"{c['name']} ({ROLE.get(c.get('role'), 'both')}): {c['actual']}")
    return ", ".join(parts) or (details[0] if details else "no stream facts"), details


def _decode_text(rows) -> tuple[str, list[str]]:
    bad = [c for c in rows if c["status"] != "PASS"]
    if bad:
        lines = []
        for c in bad:
            kind = c["name"].split("_")[0]
            what = "0 frames decoded" if c["name"].endswith("_frames") else str(c["actual"]).splitlines()[0][:120]
            lines.append(f"{ROLE[c['role']]} {kind} stream {c['stream_index']}: {what}")
        return lines[0], lines[1:]
    frames = [f"{c['name'].split('_')[0]} {c['frames']:,} frames" for c in rows if c.get("role") == "output"]
    return "deliverable decodes without errors: " + ", ".join(frames), []


def _duration_text(rows) -> tuple[str, list[str]]:
    lines = []
    for c in sorted(rows, key=lambda c: c["status"] == "PASS"):
        label = "container" if c["name"] == "container_duration" else c["name"].split("_")[0]
        if c["actual"] is None or c["expected"] is None or c.get("delta_seconds") is None:
            lines.append(f"{label}: duration unknown")
            continue
        text = f"{label} {_seconds(c['actual']):.3f} s vs master {_seconds(c['expected']):.3f} s"
        if c["status"] == "FAIL":
            text += f" (off by {c['delta_seconds']:.3f} s, allowed {c['tolerance_seconds']:.3f} s)"
        lines.append(text)
    if not lines:
        return "no duration facts", []
    if all(c["status"] == "PASS" for c in rows):
        order = {"container_duration": 0, "video_decoded_duration": 1, "audio_decoded_duration": 2}
        rows = sorted(rows, key=lambda c: order.get(c["name"], 3))
        parts = [f"{'container' if c['name'] == 'container_duration' else c['name'].split('_')[0]} "
                 f"{_seconds(c['actual']):.3f} s" for c in rows]
        return (f"{', '.join(parts)}; master {_seconds(rows[0]['expected']):.3f} s, "
                f"allowed ±{rows[0]['tolerance_seconds']:.3f} s"), []
    return lines[0], lines[1:]


def postflight_checks(master: Path, deliverable: Path, options: Options, tools: dict):
    names = ("streams", "decode", "duration")
    if not (tools["ffmpeg"] and tools["ffprobe"]):
        reason = "ffmpeg/ffprobe not found (install FFmpeg or pass --ffmpeg/--ffprobe)"
        return [_check(n, "SKIPPED", reason, source="postflight") for n in names], None
    from ..postflight.core import Options as PostflightOptions, compare
    report = compare(master, deliverable, PostflightOptions(
        ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"], timeout=options.timeout,
        quick_seconds=options.quick, allow_drop=tuple(options.allow_drop)))
    general = [c for c in report["checks"] if _bucket(c["name"]) is None]
    texts = {"streams": _streams_text, "decode": _decode_text, "duration": _duration_text}
    checks = []
    for name in names:
        rows = [c for c in report["checks"] if _bucket(c["name"]) == name]
        status = _worst(c["status"] for c in rows + general)
        if rows:
            summary, details = texts[name](rows)
        else:
            summary, details = "not checked", []
        details = [f"{c['name']}: {c['actual']}" for c in general if c["status"] != "PASS"] + details
        if not rows and general:
            summary, details = details[0], details[1:]
        checks.append(_check(name, status, summary, details, "postflight",
                             coverage=report["coverage"], checks=rows + general))
    return checks, report


# --- loudness: mediawrench loudness measurement and gate ---------------------------------

def loudness_check(deliverable: Path, options: Options, tools: dict) -> dict:
    if options.target_i is None:
        return _check("loudness", "SKIPPED", "not requested (pass --target-i, --target-tp, --tolerance)",
                      source="loudness")
    if not tools["ffmpeg"]:
        return _check("loudness", "SKIPPED", "ffmpeg not found", source="loudness")
    from ..loudness.core import AudioError, Profile, Runner, Toolchain, gate, parse_loudnorm
    from ..postflight.core import FORMATS
    profile = Profile(options.target_i, options.target_tp, options.tolerance)
    runner = Runner(Toolchain(tools["ffmpeg"], tools["ffprobe"] or "ffprobe",
                              min(options.timeout, 3600), min(options.timeout + 30, 86400)))
    # Same loudnorm measurement as `loudness --verify-only`, but any container that
    # postflight accepts (MP4/MOV/WebM/...), first audio stream.
    prefix = runner.ffmpeg_prefix()
    prefix[prefix.index("-format_whitelist") + 1] = FORMATS
    try:
        result = runner.run(prefix + ["-i", str(deliverable.resolve()), "-map", "0:a:0", "-af",
                                      profile.filter() + ":print_format=json", "-f", "null", "-"])
        facts = gate(parse_loudnorm(result.stderr), profile)
    except AudioError as exc:
        if "matches no streams" in str(exc):
            return _check("loudness", "FAIL", "the deliverable has no audio stream to measure", source="loudness")
        message = str(exc).strip().splitlines()[-1][:160] if str(exc).strip() else type(exc).__name__
        return _check("loudness", "UNKNOWN", f"could not measure: {message}", source="loudness")
    lufs, peak = facts["integrated_lufs"], facts["true_peak_dbtp"]
    summary = (f"{lufs:.1f} LUFS (target {profile.target_i:g} ±{profile.tolerance:g}), "
               f"true peak {peak:.1f} dBTP (ceiling {profile.target_tp:g})")
    details = []
    if not facts["lufs_pass"]:
        side = "too quiet" if lufs < profile.target_i else "too loud"
        details.append(f"integrated loudness {abs(lufs - profile.target_i):.1f} LU {side}")
    if not facts["peak_pass"]:
        details.append(f"true peak {peak - profile.target_tp:.1f} dB over the ceiling")
    return _check("loudness", "FAIL" if details else "PASS", summary, details, "loudness", **facts)


# --- chapters: mediawrench chapters verify-media -----------------------------------------

def _chapter_difference(d: dict) -> str:
    parts = d["path"].split("/")
    number = int(parts[2]) + 1 if len(parts) > 2 and parts[2].isdigit() else None
    kind = d["kind"]
    if kind == "length":
        return f"{d['actual']} chapters in the deliverable, expected {d['expected']}"
    if kind in ("missing", "unexpected"):
        row = d["expected"] if kind == "missing" else d["actual"]
        return f"{kind} chapter {number} \"{row['title']}\" at {_stamp(row['start_us'])}"
    if kind == "changed":
        return f"chapter {number} title \"{d['actual']}\", expected \"{d['expected']}\""
    if kind == "outside_tolerance":
        what = parts[-1].removesuffix("_us")
        label = f"chapter {number} {what}" if number else what
        delta = _seconds(d["delta_us"]) / 1e6
        return f"{label} {_stamp(_seconds(d['actual_us']))}, expected {_stamp(d['expected_us'])} ({delta:+.3f} s)"
    return f"chapter {number}: {kind} ({d.get('reason', '')})"


def chapters_check(deliverable: Path, options: Options, tools: dict, duration: float | None) -> dict:
    if options.chapters is None:
        return _check("chapters", "SKIPPED", "not requested (pass --chapters FILE)", source="chapters")
    if not tools["ffprobe"]:
        return _check("chapters", "SKIPPED", "ffprobe not found", source="chapters")
    from ..chapters.core import ChapterError, dumps, loads
    from ..chapters.media import verify_media
    fmt = options.chapters_format
    if fmt == "text" and duration is None:
        return _check("chapters", "UNKNOWN", "text chapters need the deliverable duration (ffprobe probe failed)",
                      source="chapters")
    try:
        doc = loads(options.chapters.read_text(encoding="utf-8"), fmt,
                    duration=f"{duration:.6f}" if fmt == "text" else None)
    except ChapterError as exc:
        return _check("chapters", "FAIL", f"expected chapters do not fit the deliverable: {exc}", source="chapters")
    try:
        result = verify_media(deliverable, dumps(doc, "json", allow_lossy=True).report, ffprobe=tools["ffprobe"],
                              tolerance_us=round(options.chapter_tolerance_ms * 1000),
                              timeout=max(1, min(300, int(options.timeout))))
    except ChapterError as exc:
        return _check("chapters", "UNKNOWN", f"could not read chapters: {exc}", source="chapters")
    if result["status"] == "skipped":
        return _check("chapters", "SKIPPED", result.get("reason", "skipped"), source="chapters")
    titles = ", ".join(c.title for c in doc.chapters)
    titles = titles if len(titles) <= 60 else titles[:57] + "..."
    if result["status"] == "passed":
        return _check("chapters", "PASS", f"{len(doc.chapters)} chapters match ({titles}), "
                      f"times within {options.chapter_tolerance_ms:g} ms", source="chapters",
                      expected=len(doc.chapters), actual=len(result["actual"]))
    lines = [_chapter_difference(d) for d in result["differences"]]
    if result["differences"][0]["kind"] == "length" and result["actual"] == []:
        lines[0] = f"no chapters in the deliverable, expected {len(doc.chapters)} ({titles})"
        lines = lines[:1]
    return _check("chapters", "FAIL", lines[0], lines[1:], "chapters", expected=len(doc.chapters),
                  actual=len(result["actual"]), differences=result["differences"])


# --- subtitles: cutlist SRT / rollup WebVTT readers, reflow reading limits ----------------

_MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _read_cues(path: Path) -> list[tuple[int, int, str]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".vtt" or text.startswith("WEBVTT"):
        from ..rollup.core import parse_vtt
        return [(c.start_ms, c.end_ms, c.plain) for c in parse_vtt(text, preserve_metadata=True)[0]]
    from ..cutlist.core import read_srt
    return [(c.start_ms, c.end_ms, _MARKUP.sub("", c.text)) for c in read_srt(path)]


def subtitle_check(path: Path, options: Options, duration: float | None) -> dict:
    from ..cutlist.core import CutlistError
    from ..reflow.layout import display_text, measure_width
    from ..reflow.model import Config
    from ..rollup.core import CaptionError
    defaults = Config()
    max_cps = options.max_cps if options.max_cps is not None else defaults.max_cps
    max_chars = options.max_chars if options.max_chars is not None else defaults.max_chars
    name = f"{path.name}:"
    try:
        cues = _read_cues(path)
    except UnicodeError:
        return _check("subtitles", "FAIL", f"{name} not UTF-8", source="subtitles", file=str(path))
    except (CutlistError, CaptionError) as exc:
        # SRT has no optional syntax, so any reader error is a defect. The WebVTT reader
        # covers a documented subset; features outside it are UNKNOWN, not FAIL.
        unsupported = isinstance(exc, CaptionError) and re.search(r"unsupported|preserves only", str(exc), re.I)
        status, verb = ("UNKNOWN", "uses a feature the reader does not support") if unsupported else ("FAIL", "does not parse")
        return _check("subtitles", status, f"{name} {verb}: {exc}", source="subtitles", file=str(path))
    errors, warnings, fastest, widest = [], [], 0.0, 0
    end_ms = None if duration is None else round(duration * 1000)
    for i, (start, end, text) in enumerate(cues):
        n = i + 1
        lines = [display_text(line) for line in text.split("\n") if line.strip()]
        cps = len(display_text(text)) * 1000 / (end - start)
        width = max((measure_width(line) for line in lines), default=0)
        fastest, widest = max(fastest, cps), max(widest, width)
        if i and start < cues[i - 1][0]:
            errors.append(f"cue {n} starts before cue {i} ({_stamp(start * 1000)})")
        elif i and start < cues[i - 1][1]:
            warnings.append(f"cue {n} overlaps cue {i} by {(cues[i - 1][1] - start) / 1000:.3f} s")
        if end_ms is not None and start >= end_ms:
            errors.append(f"cue {n} starts at {_stamp(start * 1000)}, after the deliverable ends ({_stamp(end_ms * 1000)})")
        elif end_ms is not None and end > end_ms:
            warnings.append(f"cue {n} ends at {_stamp(end * 1000)}, after the deliverable ends ({_stamp(end_ms * 1000)})")
        if cps > max_cps:
            warnings.append(f"cue {n} reads at {cps:.1f} chars/s (limit {max_cps:g})")
        if width > max_chars:
            warnings.append(f"cue {n} has a {width}-character line (limit {max_chars})")
        if len(lines) > options.max_lines:
            warnings.append(f"cue {n} has {len(lines)} lines (limit {options.max_lines})")
    facts = dict(file=str(path), cues=len(cues), max_cps=round(fastest, 2), max_line_chars=widest,
                 last_end_ms=cues[-1][1], deliverable_ms=end_ms)
    head = f"{name} {len(cues)} cues"
    if errors or warnings:
        problems = errors + warnings
        return _check("subtitles", "FAIL" if errors else "WARN", f"{head}; {problems[0]}", problems[1:],
                      "subtitles", **facts)
    where = "" if end_ms is None else f", last ends {_stamp(cues[-1][1] * 1000)} of {_stamp(end_ms * 1000)}"
    return _check("subtitles", "PASS", f"{head}{where}; max {fastest:.1f} chars/s, {widest} chars/line",
                  source="subtitles", **facts)


# --- the report -----------------------------------------------------------------------------

def run(master: Path, deliverable: Path, options: Options, fail_on: str = "unknown") -> dict:
    tools = validate(master, deliverable, options)
    checks, postflight = postflight_checks(master, deliverable, options, tools)
    duration = None
    if postflight and postflight["media"].get("output", {}).get("format", {}).get("duration") is not None:
        duration = _seconds(postflight["media"]["output"]["format"]["duration"])
    checks.append(loudness_check(deliverable, options, tools))
    checks.append(chapters_check(deliverable, options, tools, duration))
    if options.subtitles:
        checks += [subtitle_check(path, options, duration) for path in options.subtitles]
    else:
        checks.append(_check("subtitles", "SKIPPED", "not requested (pass --subtitles FILE)", source="subtitles"))
    counts = {status: sum(c["status"] == status for c in checks) for status in STATUSES}
    failed = any(c["status"] in FAIL_ON[fail_on] for c in checks)
    version = None
    if postflight and "ffmpeg" in postflight["tools"]:
        match = re.search(r"[0-9]+(?:\.[0-9]+)+", postflight["tools"]["ffmpeg"]["version"])
        version = match.group() if match else postflight["tools"]["ffmpeg"]["version"]
    return {"schema": SCHEMA, "mediawrench_version": __version__, "master": str(master),
            "deliverable": str(deliverable), "deliverable_duration_seconds": duration,
            "tools": {**tools, "ffmpeg_version": version},
            "decode": None if not postflight else (f"first {options.quick:g} s" if options.quick else "full"),
            "checks": checks,
            "summary": {"counts": counts, "fail_on": fail_on, "failed": failed, "exit_code": int(failed)},
            "postflight": postflight}
