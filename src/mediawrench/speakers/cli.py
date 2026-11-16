"""Command-line interface; input/validation errors always return nonzero."""

import argparse
from pathlib import Path
import sys

from .core import BridgeError, load_json, merge, parse_rttm
from .export import render_json, render_markdown, render_srt, write_atomic
from .manifest import (OPTIONS, _validate_manifest_sha256, diagnose_manifest_input_coverage, diagnose_manifest_options,
                       run_manifest, validate_manifest)


def _manifest_sha256(value: str) -> str:
    try:
        return _validate_manifest_sha256(value)
    except BridgeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mediawrench speakers",
        description="Offline RTTM + Whisper-compatible JSON speaker bridge. All timestamps are seconds.")
    parser.add_argument("--asr", type=Path, help="ASR JSON: segments with words, or root words")
    parser.add_argument("--rttm", type=Path, help="standard ten-field SPEAKER records")
    parser.add_argument("--manifest", type=Path, help="offline manifest of explicit recording/channel file pairs")
    parser.add_argument("--validate-manifest", action="store_true",
                        help="manifest mode: validate structure/expected-hash policy only; read no entry inputs and write no outputs")
    parser.add_argument("--validate-options", action="store_true",
                        help="requires --validate-manifest: also check input-independent merge option values; no entry reads")
    parser.add_argument("--diagnose-options", action="store_true",
                        help="requires --validate-manifest; conflicts with --validate-options: report one option error per invalid entry (exit 1); no entry reads")
    parser.add_argument("--diagnose-input-coverage", action="store_true",
                        help="requires --validate-manifest: report every missing expected input SHA-256 (exit 1); coverage only, no entry reads")
    parser.add_argument("--output-dir", type=Path, help="manifest mode: NEW directory for JSON evidence, requested SRT/Markdown companions and summary")
    parser.add_argument("--summary-format", choices=("text", "json"),
                        help="manifest mode: stdout summary format (default: text); diagnostics stay on stderr")
    parser.add_argument("--observe-input-sha256", action="store_true",
                        help="manifest mode: record SHA-256 and byte size of the parsed manifest and each completed input read (default: omitted)")
    parser.add_argument("--manifest-sha256", type=_manifest_sha256, metavar="DIGEST",
                        help="manifest mode: require exact raw manifest SHA-256 before parsing (64 lowercase ASCII hex characters; default: unchecked)")
    parser.add_argument("--require-input-sha256", action="store_true",
                        help="manifest mode: require expected SHA-256 for every referenced ASR/RTTM/aliases input before processing (default: partial expectations allowed)")
    parser.add_argument("--recording", help="select one RTTM recording (required when multiple)")
    parser.add_argument("--channel", help="select one RTTM channel (required when multiple)")
    parser.add_argument("--rttm-offset-seconds", type=float,
                        help="signed offset added after RTTM drift scaling (default: 0)")
    parser.add_argument("--rttm-drift-ppm", type=float,
                        help="explicit RTTM scale correction: 1 + ppm/1000000 (default: 0; never inferred)")
    parser.add_argument("--recording-duration", type=float,
                        help="positive duration on the ASR clock; bound RTTM/ASR items to [0, duration]")
    parser.add_argument("--rttm-out-of-range-policy", choices=("error", "clip"),
                        help="corrected RTTM boundaries: reject (default: error) or clip; clip requires --recording-duration; ASR stays strict")
    parser.add_argument("--aliases", type=Path, help="JSON object mapping speaker IDs to unique display names")
    parser.add_argument("--min-coverage", type=float, help="minimum winning coverage fraction (default: 0.5)")
    parser.add_argument("--tie-epsilon", type=float, help="absolute coverage fraction tie tolerance (default: 1e-6)")
    parser.add_argument("--max-gap", type=float, help="maximum silence gap within a turn in seconds (default: 1)")
    parser.add_argument("--format", choices=("json", "markdown", "srt"))
    parser.add_argument("--output", type=Path, help="atomic file output; default: stdout")
    parser.add_argument("--dry-run", action="store_true", help="validate, merge and render, report counts; write no output file")
    args = parser.parse_args(argv)
    if args.diagnose_input_coverage:
        if not args.validate_manifest:
            parser.error("--diagnose-input-coverage requires --validate-manifest")
        conflicts = [key for key in ("require_input_sha256", "validate_options", "diagnose_options")
                     if getattr(args, key)]
        if conflicts:
            parser.error("--diagnose-input-coverage cannot be combined with: " +
                         ", ".join("--" + key.replace("_", "-") for key in conflicts))
    if args.diagnose_options and not args.validate_manifest:
        parser.error("--diagnose-options requires --validate-manifest")
    if args.diagnose_options and args.validate_options:
        parser.error("--diagnose-options cannot be combined with --validate-options")
    if args.validate_options and not args.validate_manifest:
        parser.error("--validate-options requires --validate-manifest")
    if args.summary_format is not None and args.manifest is None:
        parser.error("--summary-format requires --manifest")
    if args.observe_input_sha256 and args.manifest is None:
        parser.error("--observe-input-sha256 requires --manifest")
    if args.manifest_sha256 is not None and args.manifest is None:
        parser.error("--manifest-sha256 requires --manifest")
    if args.require_input_sha256 and args.manifest is None:
        parser.error("--require-input-sha256 requires --manifest")
    if args.validate_manifest and args.manifest is None:
        parser.error("--validate-manifest requires --manifest")
    options = {key: getattr(args, key) for key in OPTIONS if getattr(args, key) is not None}
    if args.manifest:
        conflicts = {key for key in ("asr", "rttm", "recording", "channel", "aliases", "format", "output")
                     if getattr(args, key) is not None} | options.keys()
        if conflicts:
            parser.error("--manifest cannot be combined with single-recording options: " +
                         ", ".join("--" + key.replace("_", "-") for key in sorted(conflicts)))
        if args.validate_manifest:
            conflicts = [key for key in ("output_dir", "dry_run", "observe_input_sha256")
                         if getattr(args, key)]
            if conflicts:
                parser.error("--validate-manifest cannot be combined with: " +
                             ", ".join("--" + key.replace("_", "-") for key in conflicts))
        elif args.output_dir is None:
            parser.error("--manifest requires --output-dir")
    elif args.asr is None or args.rttm is None or args.output_dir is not None:
        parser.error("single-recording mode requires --asr and --rttm; --output-dir requires --manifest")
    try:
        if args.validate_manifest:
            if args.diagnose_input_coverage:
                report = diagnose_manifest_input_coverage(args.manifest,
                    expected_manifest_sha256=args.manifest_sha256)
                if args.summary_format == "json":
                    sys.stdout.write(render_json(report))
                else:
                    counts = report["counts"]
                    print(f"Manifest input coverage: {counts['entries']} entries, "
                          f"{counts['expected_inputs']}/{counts['referenced_inputs']} input expectations, "
                          f"{counts['missing_inputs']} missing; "
                          "coverage only; input files not read; no output written")
                    for slot in report["missing_input_sha256"]:
                        print(f"entries[{slot['index']}] ({slot['recording']}/{slot['channel']}) "
                              f"input_sha256.{slot['input']}: missing expected SHA-256 for {slot['path']}")
                return 1 if report["missing_input_sha256"] else 0
            if args.diagnose_options:
                report = diagnose_manifest_options(args.manifest,
                    expected_manifest_sha256=args.manifest_sha256,
                    require_input_sha256=args.require_input_sha256)
                if args.summary_format == "json":
                    sys.stdout.write(render_json(report))
                else:
                    counts = report["counts"]
                    print(f"Manifest option diagnostics: {counts['entries']} entries, "
                          f"{counts['invalid_option_entries']} invalid option entries; "
                          "input files not read; no output written")
                    for error in report["option_errors"]:
                        print(error["message"])
                return 1 if report["option_errors"] else 0
            report = validate_manifest(args.manifest, expected_manifest_sha256=args.manifest_sha256,
                                       require_input_sha256=args.require_input_sha256,
                                       validate_options=args.validate_options)
            if args.summary_format == "json":
                sys.stdout.write(render_json(report))
            else:
                counts = report["counts"]
                print(f"Manifest validated: {counts['entries']} entries, "
                      f"{counts['expected_inputs']}/{counts['referenced_inputs']} input expectations; "
                      + ("merge option values checked; " if args.validate_options else "")
                      + "input files not read; no output written")
            return 0
        if args.manifest:
            summary = run_manifest(args.manifest, args.output_dir, dry_run=args.dry_run,
                                   observe_input_sha256=args.observe_input_sha256,
                                   expected_manifest_sha256=args.manifest_sha256,
                                   require_input_sha256=args.require_input_sha256)
            counts = summary["counts"]
            if args.summary_format == "json":
                sys.stdout.write(render_json(summary))
            else:
                destination = "no output written" if args.dry_run else f"summary: {args.output_dir / 'summary.json'}"
                print(f"{summary['status']}: {counts['succeeded']} succeeded, {counts['failed']} failed; {destination}")
            for entry in summary["entries"]:
                errors = list(entry.get("export_errors", {}).values())
                if not errors and "error" in entry:
                    errors = [entry["error"]]
                for error in errors:
                    print(f"error: {error['message']}", file=sys.stderr)
                if entry["result"] is not None:
                    for warning in entry["warnings"]:
                        print(f"warning: entries[{entry['index']}] "
                              f"({entry['recording']}/{entry['channel']}): {warning}", file=sys.stderr)
            return 1 if counts["failed"] else 0
        inputs = [args.asr, args.rttm] + ([args.aliases] if args.aliases else [])
        if args.output and any(args.output.resolve() == path.resolve() or
                               (args.output.exists() and args.output.samefile(path)) for path in inputs):
            raise BridgeError("output must not overwrite an input file")
        asr = load_json(args.asr.read_text(encoding="utf-8"))
        aliases = load_json(args.aliases.read_text(encoding="utf-8")) if args.aliases else None
        diarization = parse_rttm(args.rttm.read_text(encoding="utf-8"), args.recording, args.channel)
        result = merge(asr, diarization, aliases=aliases, **options)
        output = {"json": render_json, "markdown": render_markdown, "srt": render_srt}[args.format or "json"](result)
        if args.dry_run:
            print(f"Valid: {len(result['items'])} items, {len(result['turns'])} turns; no output written")
        elif args.output:
            write_atomic(args.output, output)
        else:
            sys.stdout.write(output)
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        return 0
    except (BridgeError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
