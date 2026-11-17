from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from importlib.metadata import version
from itertools import chain
from pathlib import Path

import srt

from . import __version__
from .core import CutSyncError, make_timeline, remap, round_ms
from .cuts import infer_format, parse_fps, parse_time_exact
from .storage import validate_destinations, write_outputs
from .subtitles import parse_for_cli, parse_keep_for_cli

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_CUES = 100_000


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mediawrench cutsync",
        description="Apply explicit KEEP intervals to sidecar SRT; write SRT and per-cue JSON.",
        epilog="Cut lists describe kept ranges, in source order. No video is read or edited.",
    )
    result.add_argument("input", type=Path, help="original SRT file")
    result.add_argument("--keep", required=True, type=Path, help="KEEP ranges: .csv, .json or .llc")
    result.add_argument("-o", "--output", required=True, type=Path, help="new SRT file")
    result.add_argument("--report", type=Path, help="audit JSON (default: OUTPUT.changes.json)")
    result.add_argument(
        "--keep-format",
        choices=("csv", "csv-frames", "json", "llc"),
        help="override the format inferred from the --keep extension",
    )
    result.add_argument("--fps", help="frame CSV only: source FPS, e.g. 25 or 30000/1001")
    result.add_argument(
        "--parse-timeout",
        type=float,
        default=10,
        help="each parser's time budget, greater than 0 and at most 60 seconds (default: 10)",
    )
    result.add_argument(
        "--boundary",
        choices=("split", "drop", "longest"),
        default="split",
        help="crossing cue: clip each piece, drop cue, or keep longest piece (default: split)",
    )
    result.add_argument(
        "--duration",
        help="source media duration for open keep ends; seconds or HH:MM:SS.mmm",
    )
    result.add_argument(
        "--selected-only",
        action="store_true",
        help=".llc only: omit segments explicitly marked selected:false",
    )
    result.add_argument(
        "--min-duration-ms",
        type=int,
        default=1,
        help="discard fragments shorter than this many milliseconds (default: 1)",
    )
    result.add_argument(
        "--input-encoding",
        default="utf-8-sig",
        help="original SRT encoding (default: utf-8-sig)",
    )
    result.add_argument(
        "--short-fractions",
        choices=("reject", "decimal", "milliseconds"),
        default="reject",
        help="interpret noncanonical ,95 as 950 ms or 95 ms; default rejects ambiguous times",
    )
    result.add_argument("--force", action="store_true", help="replace existing output/report files")
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="print complete audit JSON; write no files",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return result


def _read(path: Path, encoding: str) -> tuple[str, str]:
    # A plain open() on a named pipe can block before any byte/time limit.
    # Open nonblocking and verify the actual descriptor, avoiding a stat/open race.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CutSyncError(f"input is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as fp:
            raw = fp.read(MAX_INPUT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise CutSyncError(f"input exceeds 8 MiB limit: {path}")
    try:
        text = raw.decode(encoding)
    except (UnicodeError, LookupError) as exc:
        raise CutSyncError(f"cannot decode {path} as {encoding}: {exc}") from exc
    return text, hashlib.sha256(raw).hexdigest()


def run(args: argparse.Namespace) -> dict:
    report = args.report or args.output.with_suffix(".changes.json")
    protected = [args.input, args.keep]
    # A dry run may target existing files, but never aliases the inputs or itself.
    validate_destinations([args.output, report], protected, overwrite=args.force or args.dry_run)
    source_text, source_hash = _read(args.input, args.input_encoding)
    keep_text, keep_hash = _read(args.keep, "utf-8-sig")
    duration = (
        parse_time_exact(args.duration, context="duration") if args.duration is not None else None
    )
    keep_format = args.keep_format or infer_format(args.keep)
    intervals = parse_keep_for_cli(
        keep_text,
        timeout=args.parse_timeout,
        format=keep_format,
        duration_ms=duration,
        selected_only=args.selected_only,
        fps=args.fps,
    )
    timeline = make_timeline(intervals)
    subtitles, timestamp_changes = parse_for_cli(
        source_text,
        short_fractions=args.short_fractions,
        timeout=args.parse_timeout,
    )
    if len(subtitles) > MAX_CUES:
        raise CutSyncError("input exceeds 100,000-cue limit")
    output, audit = remap(
        subtitles, timeline, boundary=args.boundary, min_duration_ms=args.min_duration_ms
    )
    output_text = srt.compose(output, reindex=False)
    for change in timestamp_changes:
        entry = audit["cues"][change["source_position"] - 1]
        entry.setdefault("timestamp_normalizations", []).append(change)
        entry["needs_review"] = True
    audit["summary"]["review_cues"] = sum(entry["needs_review"] for entry in audit["cues"])
    audit["summary"]["normalized_timestamp_fields"] = len(timestamp_changes)
    # The report keeps the original tool name ("srt-cut-sync") so saved audits stay comparable.
    audit["tool"] = {"name": "srt-cut-sync", "version": __version__, "srt_version": version("srt")}
    audit["input"] = {
        "file": args.input.name,
        "sha256": source_hash,
        "encoding": args.input_encoding,
        "short_fractions": args.short_fractions,
        "parser_timeout_seconds": args.parse_timeout,
        "text_preprocessing": "optional leading BOM removed; CRLF/CR newlines normalized to LF",
    }
    audit["keep_input"] = {
        "file": args.keep.name,
        "sha256": keep_hash,
        "format": keep_format,
        "selected_only": args.selected_only,
        "fps": str(parse_fps(args.fps)) if args.fps is not None else None,
        "source_duration_ms": round_ms(duration) if duration is not None else None,
        "source_duration_exact_ms": str(duration) if duration is not None else None,
        "rounding": "exact rational mapping; final timestamps nearest millisecond, half up",
    }
    audit["output"] = {
        "file": args.output.name,
        "sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "encoding": "utf-8",
        "newline": "LF",
    }
    if args.dry_run:
        # ASCII JSON is valid UTF-8 and also survives redirected ASCII consoles.
        json.dump(audit, sys.stdout, ensure_ascii=True, indent=2, allow_nan=False)
        sys.stdout.write("\n")
    else:
        write_outputs(
            [
                (args.output, output_text),
                (
                    report,
                    chain(
                        json.JSONEncoder(
                            ensure_ascii=False,
                            indent=2,
                            allow_nan=False,
                        ).iterencode(audit),
                        ["\n"],
                    ),
                ),
            ],
            protected,
            overwrite=args.force,
        )
    summary = audit["summary"]
    print(
        f"{summary['input_cues']} input cues → {summary['output_cues']} output cues; "
        f"{summary['dropped']} dropped, {summary['trimmed']} trimmed, {summary['split']} split; "
        f"{summary['review_cues']} need review; timeline {timeline.duration_ms} ms",
        file=sys.stderr,
    )
    if summary["review_cues"]:
        print(
            "Review boundary cues: whole text is retained; words are never split or inferred.",
            file=sys.stderr,
        )
    return audit


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        run(args)
    except (CutSyncError, OSError, UnicodeError) as exc:
        print(f"mediawrench cutsync: error: {str(exc)[:2000]}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mediawrench cutsync: interrupted", file=sys.stderr)
        return 130
    return 0
