"""A local CLI; automatic measurements never imply anchor approval."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from soundfile import SoundFileError

from . import __version__
from .inspection import export_clips, inspect_text
from .render import render
from .review import analyze, edit_anchor, fit, fit_csv, load_session
from .storage import ReviewError, check_output, json_text, write_json, write_text


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mediawrench drift", description="Review manual sync anchors and correct continuous linear clock drift using ChronoSync.")
    root.add_argument("--version", action="version", version=f"mediawrench drift {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    measure = commands.add_parser("analyze", help="propose start/middle/end anchors; none are accepted automatically")
    measure.add_argument("reference", type=Path)
    measure.add_argument("target", type=Path)
    measure.add_argument("--out", type=Path, required=True)
    measure.add_argument("--offset-hint", type=float, default=0, help="approximate target time minus reference time, in seconds")
    measure.add_argument("--window", type=float, default=4, help="analysis window seconds, 0.25–30")
    measure.add_argument("--search", type=float, default=1, help="maximum residual lag around the hint, <= 0.45 * window")
    measure.add_argument("--rate", type=int, default=8000, help="bounded analysis rate, 1000–48000 Hz")
    measure.add_argument("--positions", type=float, nargs="+", default=[0.1, 0.5, 0.9], help="2–101 fractional positions within hinted overlap")
    for side in ("reference", "target"):
        measure.add_argument(f"--{side}-stream", type=int, default=0, help="zero-based audio stream ordinal")
        measure.add_argument(f"--{side}-channel", type=int, default=0, help="zero-based channel used for analysis")
    measure.add_argument("--force", action="store_true")

    anchor = commands.add_parser("anchor", help="accept, reject, or replace a time pair in a review JSON")
    anchor.add_argument("review", type=Path)
    anchor.add_argument("--id", required=True)
    anchor.add_argument("--reference-seconds", type=float)
    anchor.add_argument("--target-seconds", type=float)
    choice = anchor.add_mutually_exclusive_group()
    choice.add_argument("--accept", action="store_true")
    choice.add_argument("--reject", action="store_true")
    anchor.add_argument("--note")

    inspect = commands.add_parser("inspect", help="show proposed/accepted pairs, confidence and missing manual times")
    inspect.add_argument("review", type=Path)
    clips = commands.add_parser("clips", help="export local stereo listening pairs: reference left, target right")
    clips.add_argument("review", type=Path)
    clips.add_argument("--out", type=Path, required=True, help="new output directory")
    clips.add_argument("--seconds", type=float, default=2, help="duration per anchor, 0.5–10 seconds")
    clips.add_argument("--rate", type=int, default=16000)

    estimate = commands.add_parser("fit", help="fit only accepted anchors and export residual plot data")
    estimate.add_argument("review", type=Path)
    estimate.add_argument("--out", type=Path)
    estimate.add_argument("--csv", type=Path)
    estimate.add_argument("--force", action="store_true")

    correct = commands.add_parser("render", help="stream corrected WAV to the reference timeline and check actual audio residuals")
    correct.add_argument("review", type=Path)
    correct.add_argument("--out", type=Path, required=True)
    correct.add_argument("--report", type=Path, help="defaults to OUTPUT.report.json")
    correct.add_argument("--subtype", choices=["FLOAT", "PCM_24", "PCM_16"], default="FLOAT")
    correct.add_argument("--timeout", type=float, default=300, help="total FFmpeg stream timeout seconds")
    correct.add_argument("--no-verify", action="store_true", help="skip output-window GCC checks (explicitly recorded)")
    correct.add_argument("--force", action="store_true")
    for sub in (estimate, correct):
        sub.add_argument("--max-residual-ms", type=float, default=10, help="block rendering above this accepted-anchor residual")
        sub.add_argument("--max-drift-ppm", type=float, default=20000, help="maximum permitted absolute relative clock drift")
    return root


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "analyze":
            check_output(args.out, protected=[args.reference, args.target], overwrite=args.force)
            result = analyze(args.reference, args.target, offset_hint=args.offset_hint,
                             window_seconds=args.window, search_seconds=args.search,
                             positions=args.positions, rate=args.rate,
                             reference_stream=args.reference_stream, target_stream=args.target_stream,
                             reference_channel=args.reference_channel, target_channel=args.target_channel)
            write_json(args.out, result, overwrite=args.force)
            measurable = sum(a["target_seconds"] is not None for a in result["anchors"])
            print(f"Wrote {args.out}: {len(result['anchors'])} anchor slots, {measurable} measured candidates, none accepted.", file=sys.stderr)
        else:
            session = load_session(args.review)
            if args.command == "inspect":
                print(inspect_text(session), end="")
            elif args.command == "clips":
                result = export_clips(session, args.out, seconds=args.seconds, rate=args.rate)
                print(json_text(result), end="")
            elif args.command == "anchor":
                if not any((args.accept, args.reject, args.reference_seconds is not None,
                            args.target_seconds is not None, args.note is not None)):
                    raise ReviewError("anchor requires --accept, --reject, a replacement time, or --note")
                accepted = True if args.accept else False if args.reject else None
                result = edit_anchor(session, args.id, reference_seconds=args.reference_seconds,
                                     target_seconds=args.target_seconds, accepted=accepted, note=args.note)
                check_output(args.review, protected=[session['reference']['path'], session['target']['path']], overwrite=True)
                write_json(args.review, result, overwrite=True)
                print(f"Updated {args.id} in {args.review}.", file=sys.stderr)
            elif args.command == "fit":
                protected = [args.review, session["reference"]["path"], session["target"]["path"]]
                if args.out:
                    check_output(args.out, protected=protected, overwrite=args.force)
                if args.csv:
                    check_output(args.csv, protected=[*protected, *([args.out] if args.out else [])], overwrite=args.force)
                result = fit(session, max_residual_ms=args.max_residual_ms, max_drift_ppm=args.max_drift_ppm)
                if args.out:
                    write_json(args.out, result, overwrite=args.force)
                if args.csv:
                    write_text(args.csv, fit_csv(result), overwrite=args.force)
                print(json_text(result), end="")
            elif args.command == "render":
                report = args.report or args.out.with_suffix(".report.json")
                result = render(session, args.out, report=report, overwrite=args.force,
                                subtype=args.subtype, timeout=args.timeout, verify=not args.no_verify,
                                max_residual_ms=args.max_residual_ms, max_drift_ppm=args.max_drift_ppm,
                                protected=[args.review])
                print(json_text(result), end="")
        return 0
    except KeyboardInterrupt:
        print("mediawrench drift: interrupted; unfinished render was discarded", file=sys.stderr)
        return 130
    except (ReviewError, OSError, SoundFileError, json.JSONDecodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(f"mediawrench drift: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
