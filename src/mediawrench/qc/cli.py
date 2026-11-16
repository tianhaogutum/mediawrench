"""``mediawrench qc`` command line. Exit 0 pass, 1 a check at or above --fail-on, 2 usage/input error."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .core import FAIL_ON, Options, QCInputError, run

EPILOG = """\
checks (each reuses another mediawrench command's library):
  streams, decode, duration   postflight: ffprobe + full FFmpeg decode, master vs deliverable
  loudness                    loudness: integrated LUFS and true peak (--target-i/--target-tp/--tolerance)
  chapters                    chapters verify-media: titles, starts and ends (--chapters FILE)
  subtitles                   strict SRT/WebVTT parse, cue timing vs the deliverable, reading speed

statuses: PASS, WARN (style limits), FAIL, UNKNOWN (could not decide), SKIPPED (not
requested, or FFmpeg missing). exit status: 0 nothing at or above --fail-on, 1 a check at
or above it, 2 usage or input error. Read-only: nothing is written.

examples:
  mediawrench qc master.mov episode.mp4
  mediawrench qc master.mov episode.mp4 --target-i -16 --target-tp -1.5 --tolerance 1 \\
      --chapters chapters.json --subtitles episode.srt
  mediawrench qc master.mov episode.mp4 --format json --fail-on warn > qc.json
"""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def parser() -> argparse.ArgumentParser:
    cli = Parser(prog="mediawrench qc", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
                 description="Pre-delivery QC gate: check a finished file against its master before you ship it.")
    cli.add_argument("--version", action="version", version=f"mediawrench qc {__version__}")
    cli.add_argument("master", type=Path, help="the source/master file the deliverable was made from")
    cli.add_argument("deliverable", type=Path, help="the file you are about to ship (read only)")
    cli.add_argument("--format", choices=("text", "json"), default="text")
    cli.add_argument("--fail-on", choices=tuple(FAIL_ON), default="unknown",
                     help="lowest status that makes the exit status 1: fail, unknown (default), warn, skipped")
    cli.add_argument("--no-color", action="store_true", help="disable color (also: NO_COLOR, non-TTY stdout)")
    media = cli.add_argument_group("decode (postflight)")
    media.add_argument("--quick", nargs="?", const=5.0, type=float, metavar="SECONDS",
                       help="decode only a prefix (default 5 s) instead of the whole file")
    media.add_argument("--allow-drop", choices=("audio", "video"), action="append", default=[],
                       help="the deliverable may have fewer streams of this type (e.g. an audio-only podcast)")
    media.add_argument("--ffmpeg", help="FFmpeg executable (default: ffmpeg on PATH)")
    media.add_argument("--ffprobe", help="ffprobe executable (default: ffprobe on PATH)")
    media.add_argument("--timeout", type=float, default=60.0, help="seconds per FFmpeg process (default 60)")
    loud = cli.add_argument_group("loudness")
    loud.add_argument("--target-i", type=float, metavar="LUFS", help="integrated loudness target, e.g. -16")
    loud.add_argument("--target-tp", type=float, metavar="DBTP", help="true-peak ceiling, e.g. -1.5")
    loud.add_argument("--tolerance", type=float, metavar="LU", help="allowed LUFS deviation, e.g. 1")
    chap = cli.add_argument_group("chapters")
    chap.add_argument("--chapters", type=Path, metavar="FILE",
                      help="expected chapters: JSON, FFmetadata, Matroska XML or text (HH:MM:SS title)")
    chap.add_argument("--chapters-format", choices=("json", "ffmetadata", "text", "matroska"),
                      help="override the format guessed from the file name")
    chap.add_argument("--chapter-tolerance-ms", type=float, default=1.0, metavar="MS",
                      help="allowed start/end difference (default 1 ms)")
    subs = cli.add_argument_group("subtitles")
    subs.add_argument("--subtitles", type=Path, action="append", default=[], metavar="FILE",
                      help="SRT or WebVTT file shipped with the deliverable; repeatable")
    subs.add_argument("--max-cps", type=float, metavar="N", help="reading-speed limit, characters/second (default 20)")
    subs.add_argument("--max-chars", type=int, metavar="N", help="characters per line (default 42)")
    subs.add_argument("--max-lines", type=int, default=2, metavar="N", help="lines per cue (default 2)")
    return cli


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled and text else text


MARKS = {"PASS": ("✓", "32"), "WARN": ("!", "33"), "FAIL": ("✗", "31"), "UNKNOWN": ("?", "33"), "SKIPPED": ("-", "2")}


def render_text(doc: dict, s: Style, master: str, deliverable: str) -> str:
    out = [s("1", "mediawrench qc") + f"  {master} → {deliverable}"]
    facts = []
    if doc["tools"]["ffmpeg_version"]:
        facts.append(f"FFmpeg {doc['tools']['ffmpeg_version']}")
    if doc["decode"]:
        facts.append(f"{doc['decode']} decode")
    if doc["deliverable_duration_seconds"] is not None:
        facts.append(f"deliverable {doc['deliverable_duration_seconds']:.3f} s")
    if not doc["tools"]["ffmpeg"] or not doc["tools"]["ffprobe"]:
        facts.append("FFmpeg not found: media checks skipped")
    out += ["  " + s("2", " · ".join(facts)), ""]
    for check in doc["checks"]:
        mark, color = MARKS[check["status"]]
        label = s(color, f"{mark} {check['status']:<8}")
        summary = s("2", check["summary"]) if check["status"] == "SKIPPED" else check["summary"]
        out.append(f"  {label} {check['name']:<10} {summary}")
        out += [" " * 24 + s("2", line) for line in check["details"]]
    counts, summary = doc["summary"]["counts"], doc["summary"]
    parts = [f"{counts['PASS']} passed"]
    for status, word in (("WARN", "warning"), ("FAIL", "failed"), ("UNKNOWN", "unknown"), ("SKIPPED", "skipped")):
        if counts[status]:
            text = f"{counts[status]} {word}" + ("s" if word == "warning" and counts[status] > 1 else "")
            parts.append(s(MARKS[status][1], text))
    verdict = s("1;31", "FAIL (exit 1)") if summary["failed"] else s("1;32", "ship it (exit 0)")
    out += ["", " · ".join(parts), f"--fail-on {summary['fail_on']}: {verdict}"]
    return "\n".join(out) + "\n"


def _use_color(args) -> bool:
    if args.no_color or args.format != "text" or os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    options = Options(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, timeout=args.timeout, quick=args.quick,
                      allow_drop=tuple(args.allow_drop), target_i=args.target_i, target_tp=args.target_tp,
                      tolerance=args.tolerance, chapters=args.chapters, chapters_format=args.chapters_format,
                      chapter_tolerance_ms=args.chapter_tolerance_ms, subtitles=args.subtitles,
                      max_cps=args.max_cps, max_chars=args.max_chars, max_lines=args.max_lines)
    try:
        doc = run(args.master, args.deliverable, options, args.fail_on)
    except QCInputError as exc:
        print(f"mediawrench qc: {exc}", file=sys.stderr)
        return 2
    try:
        if args.format == "json":
            sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n")
        else:
            sys.stdout.write(render_text(doc, Style(_use_color(args)), str(args.master), str(args.deliverable)))
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
    return doc["summary"]["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
