"""Command-line boundary and no-clobber atomic output staging."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import create_mapping_template, export_subtitles, import_labels
from .formats import BridgeError, decode_input


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise BridgeError(f"Nonfinite JSON constant: {value}")


def _read_json(path: Path, kind: str) -> dict:
    try:
        value = json.loads(decode_input(path.read_bytes()), object_pairs_hook=_unique_json,
                           parse_constant=_bad_constant)
        if not isinstance(value, dict):
            raise BridgeError(f'{kind} JSON must be an object')
        return value
    except ValueError as exc:
        raise BridgeError(f"Invalid {kind} JSON: {exc}") from exc


def _check_outputs(paths):
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise BridgeError("Output paths must be distinct (including symlink aliases)")
    for path in paths:
        if path.exists() or path.is_symlink():
            raise BridgeError(f"Refusing to overwrite existing output: {path}")
        if not path.parent.is_dir():
            raise BridgeError(f"Output directory does not exist: {path.parent}")


def _atomic_outputs(outputs: dict[Path, str], dry_run: bool):
    """Stage complete files, then atomically create each without overwriting.

    No multi-file filesystem transaction is promised: an abrupt process/OS crash
    between links can leave a subset of complete files. Ordinary errors roll back.
    """
    paths = list(outputs)
    _check_outputs(paths)
    if dry_run:
        return
    staged = []
    linked = []
    try:
        for path, contents in outputs.items():
            fd, name = tempfile.mkstemp(prefix=".asb-", dir=path.parent)
            temp = Path(name)
            staged.append(temp)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        for path, temp in zip(paths, staged):
            os.link(temp, path)  # atomic no-replace; fails on a racing existing file
            linked.append((path, temp))
    except OSError:
        for path, temp in linked:
            if path.exists() and path.samefile(temp):
                path.unlink()
        raise
    finally:
        for temp in staged:
            temp.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="mediawrench audacity",
        description="Audited SRT/WebVTT timing roundtrips through Audacity TSV labels.")
    cli.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = cli.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Subtitle -> readable labels + lossless text sidecar", description="Subtitle -> readable labels + lossless text sidecar")
    export.add_argument("source", type=Path, help="Local SRT or WebVTT subtitle file")
    export.add_argument("--format", choices=("srt", "vtt"), help="Default: source filename suffix")
    export.add_argument("--labels", required=True, type=Path, help="New Audacity label file to import as a label track")
    export.add_argument("--sidecar", required=True, type=Path, help="New JSON sidecar that keeps the original cue payloads")
    export.add_argument("--dry-run", action="store_true", help="Validate and report; write no files")
    template = commands.add_parser("mapping-template", help="Validate caller-declared split/merge operations and write hash-bound mapping JSON",
        description="Validate caller-declared split/merge operations and write hash-bound mapping JSON")
    template.add_argument("labels", type=Path, help="Edited Audacity labels; target lines are 1-based")
    template.add_argument("--sidecar", required=True, type=Path, help="JSON sidecar written by export")
    template.add_argument("--operations", required=True, type=Path,
        help="JSON object with only operations: explicit source IDs, lines, separators and split texts; no inference")
    template.add_argument("--output", required=True, type=Path, help="New mapping JSON file for review")
    template.add_argument("--point-duration", help="Positive duration in seconds, required for point labels")
    template.add_argument("--missing-policy", choices=("error", "keep", "drop"), default="error",
        help="Validate omitted originals (default: error); supply the choice again at import")
    template.add_argument("--new-policy", choices=("error", "add", "ignore"), default="error",
        help="Validate unmapped rows without IDs (default: error); supply the choice again at import")
    template.add_argument("--source", type=Path,
        help="Optionally verify sidecar against original subtitle bytes (SHA-256 only; not authentication)")
    template.add_argument("--dry-run", action="store_true", help="Validate and print mapping JSON; write no files")
    imp = commands.add_parser("import", help="Edited labels + sidecar -> SRT/WebVTT + JSON audit", description="Edited labels + sidecar -> SRT/WebVTT + JSON audit")
    imp.add_argument("labels", type=Path, help="Labels exported from Audacity after editing")
    imp.add_argument("--sidecar", type=Path, help="JSON sidecar written by export (required unless --allow-lossy)")
    imp.add_argument("--mapping", type=Path,
                     help="Explicit JSON split/merge rules bound to sidecar and edited labels")
    imp.add_argument("--source", type=Path, help="Optionally verify sidecar against original subtitle bytes")
    imp.add_argument("--output", required=True, type=Path, help="New SRT/WebVTT output file")
    imp.add_argument("--format", choices=("srt", "vtt"), default="srt",
                     help="Output format (default: srt, independent of filename suffix)")
    imp.add_argument("--report", type=Path, help="Default: OUTPUT.report.json; errors go to stderr as JSON")
    imp.add_argument("--text-policy", choices=("error", "original", "edited"), default="error",
                     help="Label text changed in Audacity: refuse (default), keep the original or take the edit")
    imp.add_argument("--missing-policy", choices=("error", "keep", "drop"), default="error",
                     help="Original cues missing from the labels: refuse (default), keep or drop them")
    imp.add_argument("--new-policy", choices=("error", "add", "ignore"), default="error",
                     help="Labels without a [cue:ID] prefix: refuse (default), add as new cues or ignore")
    imp.add_argument("--settings-loss-policy", choices=("warn", "error"), default="warn",
                     help="Retained WebVTT cue settings lost to SRT: warn (default) or refuse output; "
                          "independent of identifier-loss policy")
    imp.add_argument("--identifier-loss-policy", choices=("warn", "error"), default="warn",
                     help="Original WebVTT identifiers lost to SRT or merges in either format: "
                          "warn (default) or refuse output")
    imp.add_argument("--document-metadata-loss-policy", choices=("warn", "error"), default="warn",
                     help="WebVTT header/NOTE text lost to SRT, even with all cues dropped: "
                          "warn (default) or refuse output; independent of cue loss policies")
    imp.add_argument("--note-relocation-policy", choices=("allow", "error"), default="allow",
                     help="WebVTT non-preamble NOTE blocks moved to the VTT output preamble: "
                          "allow (default) or refuse output; independent of text loss policies")
    imp.add_argument("--point-duration", help="Positive duration in seconds, required for point labels")
    imp.add_argument("--allow-lossy", action="store_true", help="Explicitly allow conversion without sidecar")
    imp.add_argument("--dry-run", action="store_true", help="Validate and report; write no files")
    return cli


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "export":
            # Check a list first: a dict would silently collapse identical paths.
            _check_outputs([args.labels, args.sidecar])
            labels, sidecar = export_subtitles(args.source.read_bytes(),
                                                args.format or args.source.suffix.lower().lstrip("."))
            _atomic_outputs({args.labels: labels, args.sidecar: _json(sidecar)}, args.dry_run)
            print(_json({"status": "ok", "dry_run": args.dry_run,
                         "cue_count": len(sidecar["cues"]), "labels": str(args.labels),
                         "sidecar": str(args.sidecar)}), end="")
        elif args.command == "mapping-template":
            _check_outputs([args.output])
            plan = _read_json(args.operations, "operations")
            if set(plan) != {"operations"}:
                raise BridgeError("Operations JSON must contain exactly the operations field")
            mapping = create_mapping_template(decode_input(args.labels.read_bytes()),
                _read_json(args.sidecar, "sidecar"), plan["operations"],
                point_duration=args.point_duration,
                missing_policy=args.missing_policy, new_policy=args.new_policy,
                source=args.source.read_bytes() if args.source is not None else None)
            contents = _json(mapping)
            _atomic_outputs({args.output: contents}, args.dry_run)
            print(contents, end="")
        else:
            report_path = args.report or Path(str(args.output) + ".report.json")
            _check_outputs([args.output, report_path])
            labels = decode_input(args.labels.read_bytes())
            sidecar = _read_json(args.sidecar, 'sidecar') if args.sidecar is not None else None
            mapping = _read_json(args.mapping, 'mapping') if args.mapping is not None else None
            subtitle, report = import_labels(labels, sidecar, text_policy=args.text_policy,
                                        missing_policy=args.missing_policy, new_policy=args.new_policy,
                                        point_duration=args.point_duration, allow_lossy=args.allow_lossy,
                                        source=args.source.read_bytes() if args.source is not None else None,
                                        output_format=args.format, mapping=mapping,
                                        settings_loss_policy=args.settings_loss_policy,
                                        identifier_loss_policy=args.identifier_loss_policy,
                                        document_metadata_loss_policy=args.document_metadata_loss_policy,
                                        note_relocation_policy=args.note_relocation_policy)
            report["dry_run"] = args.dry_run
            _atomic_outputs({args.output: subtitle, report_path: _json(report)}, args.dry_run)
            for warning in report["warnings"]:
                print("warning: " + warning, file=sys.stderr)
            print(_json(report), end="")
        return 0
    except (BridgeError, OSError, UnicodeError, RecursionError) as exc:
        error = {"status": "error", "error": str(exc)}
        if isinstance(exc, BridgeError) and exc.report is not None:
            error["report"] = exc.report
        print(_json(error), file=sys.stderr, end="")
        return 2
