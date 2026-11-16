"""Command line interface; output staging stays on each target filesystem."""
from __future__ import annotations

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .core import CaptionError, convert


def _same_path(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))


def _validate_paths(source: Path, outputs: list[Path], force: bool) -> None:
    if _same_path(*outputs):
        raise CaptionError("Caption and provenance paths must be different")
    for output in outputs:
        if _same_path(source, output):
            raise CaptionError("Output must not overwrite the input, even with --force")
        if output.is_symlink():
            raise CaptionError(f"Refusing output symlink: {output}")
        if not output.parent.is_dir():
            raise CaptionError(f"Output directory does not exist: {output.parent}")
        if output.exists() and (not force or not output.is_file()):
            raise CaptionError(f"Output exists (use --force for regular files): {output}")


def _atomic_write(outputs: dict[Path, bytes], *, force: bool) -> None:
    staged: dict[Path, str] = {}
    backups: dict[Path, str] = {}
    published: list[Path] = []
    try:
        for target, content in outputs.items():
            with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp",
                                             dir=target.parent, delete=False) as handle:
                staged[target] = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        if force:
            for target in outputs:
                if target.exists():
                    # Same-directory hard links allow restoration without reading files.
                    fd, backup = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".bak", dir=target.parent)
                    os.close(fd)
                    os.unlink(backup)
                    os.link(target, backup, follow_symlinks=False)
                    backups[target] = backup
        for target, temp in staged.items():
            if force:
                os.replace(temp, target)
            else:
                # Exclusive publication: a concurrent creator is never clobbered.
                os.link(temp, target)
            published.append(target)
    except OSError:
        for target in reversed(published):
            if target in backups:
                os.replace(backups.pop(target), target)
            else:
                target.unlink()
        raise
    finally:
        for path in [*staged.values(), *backups.values()]:
            with suppress(FileNotFoundError):
                os.unlink(path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="mediawrench rollup",
        description="Repair conservative adjacent whole-line roll-ups in local VTT; write SRT or preserved VTT + token provenance.")
    result.add_argument("input", type=Path, help="local UTF-8 .vtt (or plain .srt passthrough)")
    result.add_argument("-o", "--output", type=Path, help="caption path (default: input stem.srt, or stem.repaired.vtt for VTT)")
    result.add_argument("--map", dest="map_path", type=Path, help="JSON path (default: output path + .json)")
    result.add_argument("--output-format", choices=("srt", "vtt"), default="srt",
                        help="SRT flattening subset (default) or VTT preserving layout/header/NOTE metadata, balanced b/i/u/c tags with optional class lists, restricted voice scopes with optional class lists and restricted language scopes")
    result.add_argument("--input-format", choices=("vtt", "srt"), help="override filename extension")
    result.add_argument("--max-gap-ms", type=int, default=0,
                        help="maximum gap for whole-line roll-ups, in ms (default 0; ignored by --word-overlap)")
    result.add_argument("--word-overlap", action="store_true",
                        help="opt in to exact word suffix/prefix repair inside or across rewrapped lines; requires strict time overlap and at least two distinct words")
    result.add_argument("--flatten", action="store_true",
                        help="explicitly accept reported loss of supported tags and inline playback timing")
    result.add_argument("--dry-run", action="store_true", help="print JSON report only; do not write any files")
    result.add_argument("--force", action="store_true", help="replace existing regular output files")
    result.add_argument("--version", action="version", version=f"mediawrench rollup {__version__}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = args.output or (args.input.with_name(args.input.stem + ".repaired.vtt")
                                 if args.output_format == "vtt" else args.input.with_suffix(".srt"))
        mapping = args.map_path or Path(str(output) + ".json")
        _validate_paths(args.input, [output, mapping], args.force)
        input_format = args.input_format or args.input.suffix.lower().lstrip(".")
        with args.input.open("r", encoding="utf-8", newline="") as handle:
            result = convert(handle.read(), flatten=args.flatten, max_gap_ms=args.max_gap_ms,
                             input_format=input_format, word_overlap=args.word_overlap,
                             output_format=args.output_format)
        data = result.output.encode("utf-8")
        report = {**result.report, "dry_run": args.dry_run,
                  "output": str(output), "provenance": str(mapping)}
        if not args.dry_run:
            _atomic_write({output: data, mapping: (json.dumps(result.provenance, ensure_ascii=False,
                                                            indent=2) + "\n").encode("utf-8")}, force=args.force)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (CaptionError, OSError, UnicodeError) as exc:
        print(f"mediawrench rollup: {exc}", file=sys.stderr)
        return 2
