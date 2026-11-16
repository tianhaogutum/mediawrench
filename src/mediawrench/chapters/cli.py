"""Offline CLI. File publication is atomic per file, with no implicit overwrite."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

from .core import ChapterError, FORMATS, check_export, compare, dumps, json_text, loads, read_json, restore


def _read(path):
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _hash_input(path, *, capture=None):
    """Fingerprint binary bytes in bounded chunks, optionally retaining audit bytes.

    The audit must still fit in memory for JSON parsing. Capturing it in this
    pass ensures its digest covers exactly the bytes parsed, not a second read.
    This does not lock inputs or provide a snapshot shared with the probe.
    """
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if capture is not None:
                capture.extend(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def _check_inputs(saved, media, report):
    """Check only the saved binding envelope and explicitly supplied file bytes."""
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
            or saved["schema_version"] != 1):
        raise ChapterError("expected a version 1 verification result")
    status, equal = saved.get("status"), saved.get("equal")
    if ("equal" not in saved or not any(status == state and equal is value
            for state, value in (("passed", True), ("different", False), ("skipped", None)))):
        raise ChapterError("verification requires passed/true, different/false or skipped/null status/equal")
    fingerprints = saved.get("input_fingerprints")
    expected = {}
    for name in ("media", "report"):
        value = fingerprints.get(name) if isinstance(fingerprints, dict) else None
        if (not isinstance(value, dict) or not isinstance(value.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                or type(value.get("size_bytes")) is not int or value["size_bytes"] < 0):
            raise ChapterError(f"input_fingerprints.{name} requires lowercase 64-hex sha256 "
                               "and nonnegative integer size_bytes")
        expected[name] = {"sha256": value["sha256"], "size_bytes": value["size_bytes"]}
    checks = {}
    for name, path in (("media", media), ("report", report)):
        actual = _hash_input(path)
        checks[name] = {"expected": expected[name], "actual": actual,
                        "equal": expected[name] == actual}
    matches = all(check["equal"] for check in checks.values())
    return {"schema_version": 1, "kind": "input-byte-binding",
            "scope": "SHA-256 and byte length equality only; no chapter verification performed",
            "status": "passed" if matches else "different", "equal": matches,
            "saved_verification": {"status": status, "equal": equal}, "checks": checks}


def _publish(outputs, *, force=False):
    """Stage and fsync all files before publishing the report, then the output.

    A pair of files is not a filesystem transaction. Publishing the report first
    leaves the audit available if output publication subsequently fails. This
    does not promise transactional or power-loss durability across two files.
    """
    staged = []
    try:
        for path, content in outputs:
            target = Path(path)
            if target.is_symlink():
                raise ChapterError(f"refusing to replace symlink: {target}")
            if target.exists() and not force:
                raise ChapterError(f"output exists: {target}; use --force to replace")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=".chapter-roundtrip-", dir=target.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                staged.append((temporary, target))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, target in staged:
            if force:
                os.replace(temporary, target)
            else:
                # link provides atomic no-clobber semantics on a local filesystem.
                os.link(temporary, target)
                temporary.unlink()
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _check_paths(input_path, output_path, report_path):
    paths = [Path(input_path), Path(output_path)]
    if report_path is not None:
        paths.append(Path(report_path))
    for i, first in enumerate(paths):
        for second in paths[i + 1:]:
            if first.resolve() == second.resolve() or (
                    first.exists() and second.exists() and first.samefile(second)):
                raise ChapterError("input, output, and report must be different files")


def parser():
    root = argparse.ArgumentParser(
        prog="mediawrench chapters",
        description="Exact, loss-audited conversion of local chapter sidecars (offline).")
    commands = root.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="convert a sidecar; reject loss by default", description="convert a sidecar; reject loss by default")
    convert.add_argument("input", type=Path, help="source chapter sidecar")
    convert.add_argument("output", type=Path, help="destination sidecar (refused if it exists unless --force)")
    convert.add_argument("--from", dest="input_format", choices=FORMATS, required=True, help="input sidecar format")
    convert.add_argument("--to", dest="output_format", choices=FORMATS, required=True, help="output sidecar format")
    convert.add_argument("--duration", help="exact duration in decimal seconds")
    convert.add_argument("--report", type=Path, help="atomic JSON audit sidecar with full source")
    convert.add_argument("--allow-lossy", action="store_true", help="approve reported loss; requires --report")
    convert.add_argument("--force", action="store_true", help="replace existing outputs atomically")
    convert.add_argument("--dry-run", action="store_true", help="print audit JSON only; write no files")
    inspect = commands.add_parser("inspect", help="validate and print normalized integer microseconds", description="validate and print normalized integer microseconds")
    inspect.add_argument("input", type=Path, help="chapter sidecar to validate")
    inspect.add_argument("--from", dest="input_format", choices=FORMATS, required=True, help="input sidecar format")
    inspect.add_argument("--duration", help="exact duration in decimal seconds")
    diff = commands.add_parser("compare", help="compare normalized content; exit 1 on differences", description="compare normalized content; exit 1 on differences")
    diff.add_argument("left", type=Path, help="first chapter sidecar")
    diff.add_argument("right", type=Path, help="second chapter sidecar")
    diff.add_argument("--left-format", choices=FORMATS, required=True, help="format of LEFT")
    diff.add_argument("--right-format", choices=FORMATS, required=True, help="format of RIGHT")
    diff.add_argument("--left-duration", help="exact duration of LEFT in decimal seconds")
    diff.add_argument("--right-duration", help="exact duration of RIGHT in decimal seconds")
    media = commands.add_parser("verify-media", help="explicitly probe a local file against exported chapters", description="explicitly probe a local file against exported chapters")
    media.add_argument("media", type=Path, help="existing local media file (read only)")
    media.add_argument("--report", type=Path, required=True, help="version 1 conversion audit")
    media.add_argument("--ffprobe", required=True, help="ffprobe executable path or PATH name; explicitly opts in")
    media.add_argument("--output", type=Path, help="also save the exact stdout JSON result atomically")
    media.add_argument("--force", action="store_true", help="replace an existing result file; requires --output")
    media.add_argument("--hash-inputs", action="store_true",
                       help="include SHA-256 and byte lengths of media and exact audit bytes; reads full media")
    media.add_argument("--verify-duration", action="store_true", help="also compare ffprobe's media duration estimate with the exported boundary")
    media.add_argument("--expected-duration", action="append", metavar="SECONDS",
                       help="override expected media length with exact decimal seconds; requires --verify-duration; specify once")
    media.add_argument("--tolerance-us", type=int, default=1000, help="inclusive endpoint/duration tolerance in microseconds (default 1000)")
    media.add_argument("--timeout", type=int, default=15, help="probe timeout in seconds, 1..300 (default 15)")
    inputs = commands.add_parser("check-inputs", help="check saved SHA-256/length bindings only; no probe", description="check saved SHA-256/length bindings only; no probe")
    inputs.add_argument("media", type=Path, help="local media bytes to hash (read only)")
    inputs.add_argument("--report", type=Path, required=True, help="local audit bytes to hash (read only)")
    inputs.add_argument("--verification", type=Path, required=True,
                        help="version 1 saved verify-media result with input_fingerprints")
    inputs.add_argument("--output", type=Path, help="also save the exact stdout JSON result atomically")
    inputs.add_argument("--force", action="store_true", help="replace an existing result file; requires --output")
    recovery = commands.add_parser("restore-source", help="recover saved audit source as canonical JSON; no probe", description="recover saved audit source as canonical JSON; no probe")
    recovery.add_argument("audit", type=Path, help="version 1 conversion audit with complete normalized source")
    recovery.add_argument("output", type=Path, help="canonical supported JSON destination")
    recovery.add_argument("--force", action="store_true", help="replace an existing destination atomically")
    exported = commands.add_parser("check-export", help="compare a sidecar with saved audit exported expectations; no probe", description="compare a sidecar with saved audit exported expectations; no probe")
    exported.add_argument("audit", type=Path, help="version 1 audit with comparison.right exported expectations")
    exported.add_argument("exported", type=Path, help="current exported sidecar (read only; format from audit)")
    return root


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command == "check-export":
            result = check_export(read_json(_read(args.audit)), _read(args.exported))
            sys.stdout.write(json_text(result))
            return 0 if result["equal"] else 1
        if args.command == "restore-source":
            _check_paths(args.audit, args.output, None)
            doc = restore(read_json(_read(args.audit)))
            recovered = dumps(doc, "json")
            _publish([(args.output, recovered.text)], force=args.force)
            sys.stdout.write(json_text({"chapters": len(doc.chapters), "duration_us": doc.duration_us}))
            return 0
        if args.command == "check-inputs":
            if args.force and args.output is None:
                raise ChapterError("--force requires --output")
            if args.output is not None:
                for source in (args.media, args.report, args.verification):
                    _check_paths(source, args.output, None)
            result = _check_inputs(read_json(_read(args.verification)), args.media, args.report)
            result_json = json_text(result)
            if args.output is not None:
                _publish([(args.output, result_json)], force=args.force)
            sys.stdout.write(result_json)
            return 0 if result["equal"] else 1
        if args.command == "verify-media":
            from .media import verify_media
            if args.force and args.output is None:
                raise ChapterError("--force requires --output")
            if args.output is not None:
                _check_paths(args.media, args.output, args.report)
            if args.expected_duration is not None and len(args.expected_duration) != 1:
                raise ChapterError("--expected-duration must be specified once")
            if args.hash_inputs:
                audit_bytes = bytearray()
                audit_fingerprint = _hash_input(args.report, capture=audit_bytes)
                audit = read_json(audit_bytes.decode("utf-8"))
            else:
                audit = read_json(_read(args.report))
            report = verify_media(args.media, audit, ffprobe=args.ffprobe,
                                  tolerance_us=args.tolerance_us, timeout=args.timeout,
                                  verify_duration=args.verify_duration,
                                  expected_duration=args.expected_duration[0] if args.expected_duration else None)
            if args.hash_inputs:
                report["input_fingerprints"] = {"media": _hash_input(report["media"]),
                                                "report": audit_fingerprint}
            result_json = json_text(report)
            if args.output is not None:
                _publish([(args.output, result_json)], force=args.force)
            sys.stdout.write(result_json)
            if report["status"] == "skipped":
                return 3
            return 0 if report["equal"] else 1
        if args.command == "inspect":
            doc = loads(_read(args.input), args.input_format, duration=args.duration)
            sys.stdout.write(json_text(doc.normalized()))
            return 0
        if args.command == "compare":
            left = loads(_read(args.left), args.left_format, duration=args.left_duration)
            right = loads(_read(args.right), args.right_format, duration=args.right_duration)
            report = compare(left, right)
            sys.stdout.write(json_text(report))
            return 0 if report["equal"] else 1
        if args.allow_lossy and args.report is None and not args.dry_run:
            raise ChapterError("--allow-lossy requires --report (or --dry-run with audit on stdout)")
        _check_paths(args.input, args.output, args.report)
        doc = loads(_read(args.input), args.input_format, duration=args.duration)
        result = dumps(doc, args.output_format, allow_lossy=args.allow_lossy)
        audit = json_text(result.report)
        if args.dry_run:
            sys.stdout.write(audit)
            return 0
        outputs = []
        if args.report is not None:
            outputs.append((args.report, audit))
        outputs.append((args.output, result.text))
        _publish(outputs, force=args.force)
        sys.stdout.write(json_text({"chapters": len(doc.chapters),
                                    "lossless": result.report["lossless"],
                                    "loss_count": len(result.report["losses"]),
                                    "roundtrip_equal": result.report["comparison"]["equal"]}))
        return 0
    except (ChapterError, OSError, UnicodeError, RecursionError) as exc:
        print(f"mediawrench chapters: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
