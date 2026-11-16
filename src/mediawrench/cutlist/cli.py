"""CLI: candidate discovery never changes an explicit selection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .companions import verify_companions
from .core import CutlistError, build_timeline, file_sha256, load_plan, read_srt
from .export import output_paths, render_outputs, validate_paths, write_outputs
from .probe import build_probed_timeline, verify_probe_evidence


USAGE = """%(prog)s SRT --query TEXT
       %(prog)s SRT --plan PLAN --media-file NAME --output-prefix PREFIX [--dry-run] [--ffprobe PATH ...]
       %(prog)s verify-companions PROVENANCE --llc FILE --csv FILE
       %(prog)s verify-probe-evidence PROVENANCE [options]"""

DESCRIPTION = """\
Convert explicit SRT cue-ID keep/drop selections to a LosslessCut v2 project (.llc),
a headerless seconds CSV and a provenance JSON. Never cuts media; ffprobe is optional,
read-only and only used when passed explicitly with --ffprobe.

modes:
  SRT --query TEXT        print every cue containing TEXT as JSON; selects nothing
  SRT --plan PLAN ...     export (or --dry-run) the cuts selected by a JSON plan
  verify-companions       compare saved .llc/.csv files with their provenance JSON;
                          writes and executes nothing (see: %(prog)s verify-companions --help)
  verify-probe-evidence   check saved ffprobe duration/rate/origin evidence and replay
                          its recorded probe (see: %(prog)s verify-probe-evidence --help)"""

EPILOG = """\
exit codes:
  export/query           0 success (including no matches); 2 invalid input or options (stderr)
  verify-companions      0 matched; 1 mismatch (JSON); 2 malformed or unreadable input
  verify-probe-evidence  0 verified (or preflight-only success); 1 stale (JSON); 2 malformed evidence

FFmpeg is needed only for --ffprobe and verify-probe-evidence replay; install it separately."""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mediawrench cutlist", usage=USAGE, description=DESCRIPTION,
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("srt", type=Path, metavar="SRT", help="UTF-8 SRT with unique positive numeric cue IDs")
    p.add_argument("--query", metavar="TEXT", help="case-sensitive literal text; print ALL candidates as JSON and exit (never select)")
    p.add_argument("--plan", type=Path, help="JSON keep/drop ranges, padding, optional frameRate and duration (required unless --ffprobe supplies it)")
    p.add_argument("--media-file", metavar="NAME", help="media filename reference only, e.g. interview.wav; project goes beside media")
    p.add_argument("--output-prefix", type=Path, metavar="PREFIX", help="existing directory plus basename; appends .llc, .csv, .provenance.json")
    p.add_argument("--dry-run", action="store_true", help="validate and print timeline/output paths without writing")
    p.add_argument("--ffprobe", type=Path, metavar="PATH", help="explicit local executable path; probe --media-file beside output prefix, even during dry-run")
    p.add_argument("--verify-frame-rate-stream", type=int, metavar="INDEX",
                   help="opt in: require plan frameRate to exactly match avg_frame_rate and r_frame_rate of this absolute ffprobe video stream index; requires --ffprobe")
    p.add_argument("--verify-zero-origin", action="store_true",
                   help="opt in: require selected video stream start_time to be known and exactly zero; requires --ffprobe and --verify-frame-rate-stream")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['verify-companions']:
        p = argparse.ArgumentParser(prog='mediawrench cutlist verify-companions',
                                    description='Compare explicit LLC/CSV to saved authoritative cuts. No writes or execution.',
                                    epilog='Exit codes: 0 matched; 1 mismatch (JSON); 2 malformed, unsupported or unreadable input (stderr). No authentication.')
        p.add_argument('provenance', type=Path, help='saved .provenance.json written by an export')
        p.add_argument('--llc', type=Path, required=True, help='explicit existing LosslessCut v2 file')
        p.add_argument('--csv', type=Path, required=True, help='explicit existing headerless seconds CSV')
        args = p.parse_args(argv[1:])
        try:
            report = verify_companions(args.provenance, llc_path=args.llc, csv_path=args.csv)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['matches'] else 1
        except (CutlistError, OSError, UnicodeError) as exc:
            print(f'mediawrench cutlist: {exc}', file=sys.stderr)
            return 2
    if argv[:1] == ['verify-probe-evidence']:
        p = argparse.ArgumentParser(prog='mediawrench cutlist verify-probe-evidence',
                                    description='Check recorded media/executable hashes and replay the saved file-only probe argv. No writes.',
                                    epilog='Exit codes: 0 verified or preflight-only success; 1 stale (JSON report); 2 malformed, unsupported or unreadable evidence (stderr).')
        p.add_argument('provenance', type=Path, help='saved .provenance.json with durationProbe evidence')
        p.add_argument('--preflight-only', action='store_true',
                       help='opt in: check all saved evidence and current files without executing ffprobe; success is preflight-only, not replay verified')
        p.add_argument('--srt', type=Path, help='opt in: hash this current SRT against source.srtSha256; requires --plan')
        p.add_argument('--plan', type=Path, help='opt in: hash this current plan against source.planSha256; requires --srt; never reapplied')
        p.add_argument('--require-zero-origin', action='store_true',
                       help='opt in: require saved schema-3 zero-origin evidence before replay; duration-only/rate-only evidence is rejected')
        p.add_argument('--expect-video-stream', type=int, metavar='INDEX',
                       help='opt in: require saved selected absolute video index (0..2147483647); schema 1 is rejected, a different saved index is stale without replay')
        p.add_argument('--expect-frame-rate', metavar='RATE',
                       help='opt in: require saved selected declared rational frame rate; schema 1 is rejected, a different exact rate is stale without replay')
        p.add_argument('--expect-media-sha256', metavar='DIGEST',
                       help='opt in: require saved media SHA-256 (64 lowercase ASCII hex characters); a different digest is stale before current-input hashing or replay')
        p.add_argument('--expect-ffprobe-sha256', metavar='DIGEST',
                       help='opt in: require saved ffprobe SHA-256 (64 lowercase ASCII hex characters); a different digest is stale before current-input hashing or replay')
        args = p.parse_args(argv[1:])
        if (args.srt is None) != (args.plan is None):
            p.error('--srt and --plan must be supplied together')
        try:
            report = verify_probe_evidence(args.provenance, srt_path=args.srt, plan_path=args.plan,
                                           require_zero_origin=args.require_zero_origin,
                                           expected_video_stream_index=args.expect_video_stream,
                                           expected_frame_rate=args.expect_frame_rate,
                                           expected_media_sha256=args.expect_media_sha256,
                                           expected_ffprobe_sha256=args.expect_ffprobe_sha256,
                                           preflight_only=args.preflight_only)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report['stale'] else 0
        except (CutlistError, OSError, UnicodeError) as exc:
            print(f'mediawrench cutlist: {exc}', file=sys.stderr)
            return 2
    p = parser()
    args = p.parse_args(argv)
    try:
        cues = read_srt(args.srt)
        if args.query is not None:
            if (not args.query or any([args.plan, args.media_file, args.output_prefix, args.dry_run, args.ffprobe])
                    or args.verify_frame_rate_stream is not None or args.verify_zero_origin):
                raise CutlistError("Nonempty --query is discovery only; do not combine with export options")
            matches = [{"id": cue.id, "start": cue.start_ms / 1000, "end": cue.end_ms / 1000,
                        "text": cue.text} for cue in cues if args.query in cue.text]
            print(json.dumps({"query": args.query, "candidates": matches, "selected": False}, ensure_ascii=False, indent=2))
            return 0
        if not all([args.plan, args.media_file, args.output_prefix]):
            raise CutlistError("Export requires --plan, --media-file and --output-prefix")
        if args.verify_zero_origin and (args.ffprobe is None or args.verify_frame_rate_stream is None):
            raise CutlistError('--verify-zero-origin requires --ffprobe and --verify-frame-rate-stream and plan frameRate')
        if args.verify_frame_rate_stream is not None and args.ffprobe is None:
            raise CutlistError('--verify-frame-rate-stream requires --ffprobe and plan frameRate')
        paths = output_paths(args.output_prefix)
        media_path = args.output_prefix.parent / args.media_file
        protected = (args.srt, args.plan, media_path)
        if args.ffprobe is not None:
            protected += (args.ffprobe,)
        validate_paths(paths, protected)
        plan = load_plan(args.plan)
        if args.ffprobe is not None:
            timeline = build_probed_timeline(cues, plan, media_path=media_path, ffprobe_path=args.ffprobe,
                                             video_stream_index=args.verify_frame_rate_stream,
                                             verify_zero_origin=args.verify_zero_origin)
        else:
            timeline = build_timeline(cues, plan)
        contents = render_outputs(timeline, args.media_file, source={
            "srtFileName": args.srt.name, "srtSha256": file_sha256(args.srt),
            "planFileName": args.plan.name, "planSha256": file_sha256(args.plan)})
        if not args.dry_run:
            write_outputs(paths, contents, protected=protected)
        report = {"dryRun": args.dry_run,
                  "cuts": json.loads(contents["llc"])["cutSegments"],
                  "outputPaths": {key: str(path.resolve()) for key, path in paths.items()}}
        if timeline.quantization_json is not None:
            report['quantization'] = json.loads(timeline.quantization_json)
        if timeline.duration_probe_json is not None:
            report['durationProbe'] = json.loads(timeline.duration_probe_json)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (CutlistError, OSError, UnicodeError) as exc:
        print(f"mediawrench cutlist: {exc}", file=sys.stderr)
        return 2
