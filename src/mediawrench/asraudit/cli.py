"""Offline command line interface; no subprocesses or network access."""

import argparse
from dataclasses import fields
import hashlib
from pathlib import Path
import sys

from . import __version__
from .adapters import WHISPER_CPP_FORMAT
from .core import AuditError, Thresholds, audit, normalize_intervals, parse_json
from .output import atomic_outputs, csv_flags, csv_tokens, json_report, review_srt, validate_destinations
from .output import validate_token_csv_segments


def parser():
    result = argparse.ArgumentParser(
        prog="mediawrench asr-audit",
        description="Read-only ASR evidence audit. Normalized times and VAD use seconds; native conversion is opt-in. Flags are review prompts, not truth labels.")
    result.add_argument("transcript", type=Path, help="UTF-8 JSON object with segments [{start,end,text,...}]")
    result.add_argument("--input-format", choices=("segments", WHISPER_CPP_FORMAT), default="segments",
                        help="explicit transcript schema; never auto-detects units (default: segments)")
    result.add_argument("--vad", type=Path, help="explicit speech intervals [[start,end],...] in seconds; [] means no supplied speech")
    result.add_argument("--loop-config", type=Path, help="JSON default/languages rules for suspected within-segment text loops")
    result.add_argument("--json", metavar="PATH", default="-", help="JSON report path (default: stdout, '-'); never overwrites existing files")
    result.add_argument("--csv", type=Path, help="flag CSV path; unknown evidence is an empty cell")
    result.add_argument("--token-csv", type=Path,
                        help="flat original tokens from flagged native segments; requires explicit whisper.cpp-json-v1.7.6")
    result.add_argument("--token-csv-timing", choices=("all", "timed", "untimed"),
                        help="filter token CSV by supplied raw timing availability (default: all); requires --token-csv")
    result.add_argument("--token-csv-segment", type=int, action="append", metavar="N",
                        help="select an existing zero-based source segment for token CSV; repeatable, source order retained; requires --token-csv")
    result.add_argument("--review-srt", type=Path, help="coalesced review navigation cues; does not replace transcript")
    result.add_argument("--dry-run", action="store_true", help="validate and print JSON to stdout; write no sidecars")
    result.add_argument("--version", action="version", version=f"mediawrench asr-audit {__version__}")
    defaults = Thresholds()
    descriptions = {
        "min_repeat_segments": "minimum adjacent identical normalized segments (>= 2)",
        "repeat_max_gap_seconds": "maximum temporal gap between adjacent repeated segments",
        "max_chars_per_second": "flag strictly greater non-whitespace Unicode code points/second",
        "high_no_speech_prob": "flag score >= threshold",
        "low_avg_logprob": "flag score <= threshold",
        "outside_speech_fraction": "flag positive outside fraction >= threshold",
        "coalesce_gap_seconds": "maximum gap when merging flagged review intervals",
    }
    for field in fields(Thresholds):
        result.add_argument("--" + field.name.replace("_", "-"),
                            type=int if field.name == "min_repeat_segments" else float,
                            default=getattr(defaults, field.name),
                            help=f"{descriptions[field.name]} (default: {getattr(defaults, field.name)})")
    return result


def _read(path):
    raw = path.read_bytes()
    return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.token_csv_timing is not None and args.token_csv is None:
            raise AuditError("--token-csv-timing requires --token-csv")
        if args.token_csv_segment is not None and args.token_csv is None:
            raise AuditError("--token-csv-segment requires --token-csv")
        if args.token_csv is not None and args.input_format != WHISPER_CPP_FORMAT:
            raise AuditError(f"--token-csv requires --input-format {WHISPER_CPP_FORMAT}")
        thresholds = Thresholds(**{field.name: getattr(args, field.name) for field in fields(Thresholds)})
        raw, source = _read(args.transcript)
        document = parse_json(raw)
        intervals = None
        vad_source = None
        if args.vad is not None:
            vad_raw, vad_source = _read(args.vad)
            # Explicit JSON null is not the absence of a VAD file.
            intervals = normalize_intervals(parse_json(vad_raw))
        loop_config = None
        loop_config_source = None
        if args.loop_config is not None:
            loop_raw, loop_config_source = _read(args.loop_config)
            loop_config = parse_json(loop_raw)
            if loop_config is None:
                raise AuditError("loop_config: expected object, not null")
        report = audit(document, intervals, thresholds, input_format=args.input_format, loop_config=loop_config)
        report["loop_config_source"] = loop_config_source
        report["source"] = source
        report["vad_source"] = vad_source
        # Validate against all source segments, including during dry run. Audit
        # has already validated every native row, regardless of CSV selection.
        selected = validate_token_csv_segments(report, args.token_csv_segment)
        outputs = {}
        destinations = [args.json] if args.json != "-" else []
        destinations.extend(p for p in (args.csv, args.token_csv, args.review_srt) if p is not None)
        protected = [args.transcript] + ([args.vad] if args.vad is not None else [])
        if args.loop_config is not None:
            protected.append(args.loop_config)
        # Check before building a dict so duplicate destinations cannot collapse.
        validate_destinations(destinations, protected)
        if not args.dry_run:
            if args.json != "-":
                outputs[args.json] = json_report(report)
            if args.csv is not None:
                outputs[args.csv] = csv_flags(report)
            if args.token_csv is not None:
                outputs[args.token_csv] = csv_tokens(report, timing=args.token_csv_timing or "all",
                                                    segment_indices=selected)
            if args.review_srt is not None:
                outputs[args.review_srt] = review_srt(report)
            atomic_outputs(outputs, protected)
        if args.json == "-" or args.dry_run:
            sys.stdout.write(json_report(report))
        if report["unplaced_segments"]:
            print("mediawrench asr-audit: invalid timestamps remain in JSON/CSV unplaced_segments; omitted from timed SRT ranges", file=sys.stderr)
        return 0
    except (AuditError, OSError, ValueError, RecursionError, OverflowError) as exc:
        print(f"mediawrench asr-audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
