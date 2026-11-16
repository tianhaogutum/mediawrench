"""Command-line interface. No subprocesses, network access, or model loading."""
import argparse
import sys

from . import __version__
from .compare import compare_profiles
from .io import atomic_write_many, json_text, load_json, srt_text, validate_paths
from .model import Config, InputError
from .optimize import reflow


def _profile_summary(config):
    return dict(name=config.profile_name, max_cps=config.max_cps,
                min_duration=config.min_duration, min_gap=config.min_gap,
                cps_mode=config.cps_mode,
                rate_unit='graphemes/second' if config.cps_mode == 'grapheme' else 'codepoints/second')


def main(argv=None):
    # Keep omission distinct from explicit defaults so inspection rejects every
    # reflow option, including e.g. --max-chars 42 or --algorithm dp.
    parser = argparse.ArgumentParser(prog="mediawrench reflow", description="Reflow existing ASR word timestamps offline into SRT plus provenance JSON.",
                                     argument_default=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"mediawrench reflow {__version__}",
                        help="print the package version and exit; no transcript required")
    parser.add_argument("input", nargs="?", help="UTF-8 Whisper/faster-whisper/WhisperX transcript JSON (required unless --inspect-profile or --inspect-pair)")
    parser.add_argument("--inspect-profile", metavar="JSON", action="append",
                        help="validate a profile without a transcript; print resolved bounds/units as JSON; only --cps-mode may be combined")
    parser.add_argument("--inspect-pair", nargs=2, metavar=("LEFT_JSON", "RIGHT_JSON"), action="append",
                        help="validate two distinct named profiles without a transcript; JSON bounds/units only; optional shared --cps-mode")
    parser.add_argument("-o", "--output", action="append",
                        help="SRT destination; supply at most once (required unless --dry-run; unavailable with --compare-profiles)")
    parser.add_argument("--report", action="append",
                        help="JSON diagnostic/comparison destination; supply at most once (required unless --dry-run)")
    parser.add_argument("--max-chars", type=int, help="width units per line (default: 42; at most two lines)")
    parser.add_argument("--width-mode", choices=("codepoint", "display"),
                        help="line width: Unicode code points (default) or grapheme display cells; independent of --cps-mode")
    parser.add_argument("--target-duration", type=float, metavar="SECONDS")
    parser.add_argument("--max-duration", type=float, metavar="SECONDS")
    parser.add_argument("--max-cps", type=float, help="maximum units/second in --cps-mode (default: 20; cannot override --profile)")
    parser.add_argument("--cps-mode", choices=("codepoint", "grapheme"), action="append",
                        help="speaking-rate units, including spaces: saved profile mode or codepoint by default; explicit mode must agree with saved units")
    parser.add_argument("--profile", metavar="JSON", action="append",
                        help="local named reading profile; supply exactly once: name, max_cps, min_duration, min_gap, optional cps_mode")
    parser.add_argument("--compare-profiles", nargs=2, metavar=("LEFT_JSON", "RIGHT_JSON"), action="append",
                        help="compare two named profiles by source IDs; supply exactly once; JSON only, no --output/--profile/--max-cps")
    parser.add_argument("--pause", type=float, metavar="SECONDS", help="internal pauses at least this long incur a cost")
    parser.add_argument("--algorithm", choices=("dp", "fixed"), help="fixed is the N-word benchmark baseline")
    parser.add_argument("--group-words", type=int, help="N for fixed baseline (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="validate and print full report; write no files")
    parser.add_argument("--strict", action="store_true", help="exit 3 after emitting results if constraints are infeasible")
    args = parser.parse_args(argv)
    # Retain occurrences until each mode's single-selection contract is checked;
    # argparse's normal store action would silently discard earlier choices.
    inspections = vars(args).keys() & {'inspect_profile', 'inspect_pair'}
    if inspections:
        if len(inspections) > 1:
            parser.error("--inspect-pair cannot be combined with --inspect-profile")
        selector = next(iter(inspections))
        flag = '--' + selector.replace('_', '-')
        if len(getattr(args, selector)) != 1:
            parser.error(f"{flag} must be supplied exactly once")
        if len(getattr(args, 'cps_mode', [])) > 1:
            parser.error(f"--cps-mode may be supplied at most once with {flag}")
        incompatible = vars(args).keys() - {selector, 'cps_mode'}
        if incompatible:
            parser.error(f"{flag} cannot be combined with a transcript or reflow/output options: "
                         + ', '.join(sorted(incompatible)))
        try:
            options = {'cps_mode': args.cps_mode[0]} if hasattr(args, 'cps_mode') else {}
            paths = getattr(args, selector)[0]
            if selector == 'inspect_profile':
                payload = dict(mode='profile_inspection',
                               profile=_profile_summary(Config.from_profile(load_json(paths), **options)))
            else:
                configs = []
                for side, path in zip(('left', 'right'), paths):
                    try:
                        configs.append(Config.from_profile(load_json(path), **options))
                    except (InputError, OSError, ValueError, OverflowError, RecursionError) as exc:
                        raise InputError(f'{side} profile: {exc}') from exc
                left, right = configs
                if left.profile_name == right.profile_name:
                    raise InputError('inspection profiles must have distinct names')
                payload = dict(mode='profile_pair_inspection',
                               profiles=dict(left=_profile_summary(left), right=_profile_summary(right)))
            sys.stdout.write(json_text(dict(format_version=1, tool='wordcue-reflow', **payload)))
            return 0
        except (InputError, OSError, ValueError, OverflowError, RecursionError) as exc:
            print(f"mediawrench reflow: error: {exc}", file=sys.stderr)
            return 2
    if hasattr(args, 'compare_profiles'):
        if len(args.compare_profiles) != 1:
            parser.error("--compare-profiles must be supplied exactly once")
        args.compare_profiles = args.compare_profiles[0]
    if hasattr(args, 'profile'):
        if len(args.profile) != 1:
            parser.error("--profile must be supplied exactly once")
        args.profile = args.profile[0]
    if hasattr(args, 'report'):
        if len(args.report) != 1:
            parser.error("--report must be supplied exactly once")
        args.report = args.report[0]
    if hasattr(args, 'output'):
        if len(args.output) != 1:
            parser.error("--output must be supplied exactly once")
        args.output = args.output[0]
    if hasattr(args, 'cps_mode'):
        # Preserve ordinary reflow/comparison's last-occurrence resolution.
        args.cps_mode = args.cps_mode[-1]
    args = argparse.Namespace(**dict(input=None, output=None, report=None,
        max_chars=42, width_mode='codepoint', target_duration=3.0, max_duration=6.0,
        max_cps=None, cps_mode=None, profile=None, compare_profiles=None, pause=0.5,
        algorithm='dp', group_words=3, dry_run=False, strict=False) | vars(args))
    if args.input is None:
        parser.error("input transcript is required unless --inspect-profile or --inspect-pair")
    if args.compare_profiles is not None:
        if args.output is not None or args.profile is not None or args.max_cps is not None:
            parser.error("--compare-profiles cannot be combined with --output, --profile or --max-cps")
        if not args.dry_run and not args.report:
            parser.error("--report is required for --compare-profiles unless --dry-run")
    elif not args.dry_run and (not args.output or not args.report):
        parser.error("--output and --report are required unless --dry-run")
    try:
        layout_options = dict(max_chars=args.max_chars, target_duration=args.target_duration,
                              max_duration=args.max_duration, pause=args.pause, width_mode=args.width_mode)
        if args.cps_mode is not None:
            layout_options["cps_mode"] = args.cps_mode
        if args.compare_profiles is not None:
            validate_paths(args.input, *args.compare_profiles, args.report)
            # Keep shared layout failures outside the side-specific profile guard.
            # Fully validate left before reading right (or the transcript).
            Config(**layout_options)
            profiles = []
            for side, path in zip(('left', 'right'), args.compare_profiles):
                try:
                    profile = load_json(path)
                    Config.from_profile(profile, **layout_options)
                except (InputError, OSError, ValueError, OverflowError, RecursionError) as exc:
                    raise InputError(f'{side} profile: {exc}') from exc
                profiles.append(profile)
            left, right = profiles
            comparison = compare_profiles(load_json(args.input), left, right,
                algorithm=args.algorithm, group_words=args.group_words, **layout_options)
            encoded_json = json_text(comparison)
            if args.dry_run:
                sys.stdout.write(encoded_json)
            else:
                atomic_write_many([(args.report, encoded_json)])
            summaries = comparison['profiles']
            print(f"mediawrench reflow: comparing {summaries['left']['name']!r} -> {summaries['right']['name']!r}; "
                  f"{len(comparison['boundary_differences'])} boundary differences; "
                  f"{len(comparison['constraint_differences'])} constraint differences", file=sys.stderr)
            return 3 if args.strict and any(not p['feasible'] for p in summaries.values()) else 0
        validate_paths(args.input, args.output, args.report, args.profile)
        if args.profile is not None:
            if args.max_cps is not None:
                raise InputError("cannot override --profile with --max-cps; edit or copy the profile instead")
            config = Config.from_profile(load_json(args.profile), **layout_options)
        else:
            config = Config(max_cps=20.0 if args.max_cps is None else args.max_cps, **layout_options)
        data = load_json(args.input)
        report = reflow(data, config, algorithm=args.algorithm, group_words=args.group_words)
        encoded_srt = srt_text(report)
        encoded_json = json_text(report)
        if args.dry_run:
            sys.stdout.write(encoded_json)
        else:
            atomic_write_many([(args.output, encoded_srt), (args.report, encoded_json)])
        m = report["metrics"]
        print(f"mediawrench reflow: {m['cue_count']} cues; {m['mapped_words']}/{m['source_words']} words mapped; "
              f"{m['constraint_violations']} constraint violations; "
              f"{sum(d['code'] == 'segment_fallback' for d in report['diagnostics'])} fallback segments", file=sys.stderr)
        return 3 if args.strict and not report["feasible"] else 0
    except (InputError, OSError, ValueError, OverflowError, RecursionError) as exc:
        print(f"mediawrench reflow: error: {exc}", file=sys.stderr)
        return 2
