"""Small, strict JSON documents and atomic local output publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any


class ReviewError(ValueError):
    """An actionable input, media, or review error."""


def finite(value: Any, label: str, *, minimum: float | None = None,
           maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ReviewError(f"{label} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ReviewError(f"{label} exceeds the finite floating-point range") from exc
    if not math.isfinite(value):
        raise ReviewError(f"{label} must be a finite number")
    if minimum is not None and value < minimum:
        raise ReviewError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ReviewError(f"{label} must be <= {maximum}")
    return value


def integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    number = finite(value, label, minimum=minimum, maximum=maximum)
    if number != int(number):
        raise ReviewError(f"{label} must be an integer")
    return int(number)


def fingerprint(path: Path) -> dict:
    """Fast change detection, not a full-file cryptographic identity."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ReviewError(f"media is not a local regular file: {path}")
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        digest.update(source.read(65536))
        if stat.st_size > 65536:
            source.seek(max(65536, stat.st_size - 65536))
            digest.update(source.read(65536))
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "edge_sha256": digest.hexdigest()}


def check_identity(media: dict) -> None:
    if fingerprint(Path(media["path"])) != media["identity"]:
        raise ReviewError(f"media changed since analysis; analyze again: {media['path']}")


def read_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ReviewError("review JSON exceeds the 8 MiB document limit")
    def reject_constant(value: str):
        raise ReviewError(f"non-finite JSON constant is not allowed: {value}")
    def finite_float(value: str):
        number = float(value)
        if not math.isfinite(number):
            raise ReviewError(f"JSON number exceeds the finite floating-point range: {value[:80]}")
        return number
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReviewError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    with path.open(encoding="utf-8") as source:
        data = json.load(source, parse_constant=reject_constant, parse_float=finite_float,
                         object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ReviewError("review JSON must contain an object")
    return data


def json_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def check_output(path: Path, *, protected=(), overwrite: bool = False) -> Path:
    path = path.expanduser().resolve()
    for source in protected:
        source = Path(source).expanduser().resolve()
        if path == source or (path.exists() and source.exists() and os.path.samefile(path, source)):
            raise ReviewError(f"output would replace an input: {path}")
    if path.exists() and not overwrite:
        raise ReviewError(f"output exists (use --force to replace it): {path}")
    if path.exists() and not path.is_file():
        raise ReviewError(f"output is not a regular file: {path}")
    return path


def publish(temp: Path, destination: Path, *, overwrite: bool = False) -> None:
    if overwrite:
        os.replace(temp, destination)
    else:
        # An exclusive hard link avoids silently clobbering a file created
        # after preflight. The temporary file lives on the same filesystem.
        try:
            os.link(temp, destination)
        except FileExistsError as exc:
            raise ReviewError(f"output appeared during processing: {destination}") from exc
        temp.unlink()


def write_text(path: Path, text: str, *, overwrite: bool = False) -> None:
    path = check_output(path, overwrite=overwrite)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".drift-review-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        publish(temp, path, overwrite=overwrite)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, data: dict, *, overwrite: bool = False) -> None:
    write_text(path, json_text(data), overwrite=overwrite)
