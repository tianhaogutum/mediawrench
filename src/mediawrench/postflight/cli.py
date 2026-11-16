"""Command-line interface; JSON goes to stdout, optional atomic report file."""
import argparse
from contextlib import contextmanager
import json
import re
import sys
import signal
import threading

from .core import Options, compare, exit_code, write_report


def _stream_pair(value):
    if not re.fullmatch(r"[0-9]+:[0-9]+", value):
        raise argparse.ArgumentTypeError("expected nonnegative absolute SOURCE:OUTPUT stream indices")
    return tuple(map(int, value.split(":")))


@contextmanager
def _cancellation_signals(event):
    """CLI signals request orderly cleanup; library callers own their handlers."""
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda signum, frame: event.set())
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mediawrench postflight",
        description="Read-only local transcode postflight. PASS=0, FAIL=1, UNKNOWN=2. Never replaces media.",
        epilog="Full decode is the default. --quick can PASS only the sampled prefix; see coverage in JSON.")
    parser.add_argument("source", help="Original local media file")
    parser.add_argument("output", help="Transcoded local media file (must be a different file)")
    parser.add_argument("--report", help="Atomic JSON report path; must not alias either media file")
    parser.add_argument("--checkpoint-report", metavar="PATH",
                        help="Opt-in atomic initial, probe outcome, mapping/tag, AV-decode outcome and final JSON reports (may use the --report path)")
    parser.add_argument("--ffmpeg", help="Explicit FFmpeg executable (default: ffmpeg on PATH)")
    parser.add_argument("--ffprobe", help="Explicit ffprobe executable (default: ffprobe on PATH)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds per subprocess, including each stream (default: 30)")
    parser.add_argument("--deadline", type=float, metavar="SECONDS", help="Whole-job time budget including versions, probes and all stream decodes (default: unlimited)")
    parser.add_argument("--absolute-tolerance", type=float, default=0.25, metavar="SECONDS", help="Duration absolute tolerance (default: 0.25)")
    parser.add_argument("--relative-tolerance", type=float, default=0.02, metavar="FRACTION", help="Duration relative tolerance (default: 0.02)")
    parser.add_argument("--quick", nargs="?", const=5.0, type=float, metavar="SECONDS", help="Decode only a prefix (default: 5 seconds); labels partial coverage")
    parser.add_argument("--allow-drop", choices=("audio", "video"), action="append", default=[], help="Explicitly permit fewer streams of this type; repeatable")
    parser.add_argument("--subtitles", choices=("ignore", "preserve"), default="ignore", help="Opt-in subtitle count preservation; does not decode subtitle content")
    parser.add_argument("--language-tags", choices=("ignore", "preserve"), default="ignore",
                        help="Opt-in exact language-tag preservation for explicit AV --stream-map pairs (default: ignore)")
    parser.add_argument("--stream-map", action="append", type=_stream_pair, metavar="SOURCE:OUTPUT",
                        help="Absolute ffprobe AV stream indices; repeat to cover every retained AV stream. Required for ambiguous tracks.")
    args = parser.parse_args(argv)
    options = Options(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, timeout=args.timeout, deadline=args.deadline,
                      absolute_tolerance=args.absolute_tolerance, relative_tolerance=args.relative_tolerance,
                      quick_seconds=args.quick, allow_drop=tuple(args.allow_drop), subtitles=args.subtitles,
                      language_tags=args.language_tags,
                      stream_map=None if args.stream_map is None else tuple(args.stream_map))
    cancelled = threading.Event()
    with _cancellation_signals(cancelled):
        report = compare(args.source, args.output, options, cancel_event=cancelled,
                         checkpoint_report=args.checkpoint_report)
        if args.report:
            try:
                write_report(report, args.report, args.source, args.output)
            except (OSError, ValueError) as exc:
                report["checks"].append({"name": "report_write", "status": "UNKNOWN", "actual": str(exc),
                                         "expected": "safe writable report path"})
                report["verdict"] = "UNKNOWN"
        print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
        return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
