"""Validation, exact decimal time conversion, and atomic output helpers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from fractions import Fraction
import tempfile
import wave


class ManifestError(ValueError):
    """Input cannot be processed without guessing or losing data."""


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ManifestError(f"{name} must be an integer >= {minimum}")
    return value


def seconds(value, name="time"):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ManifestError(f"{name} must be finite decimal seconds")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ManifestError(f"{name} must be finite decimal seconds") from exc
    if result < 0:
        raise ManifestError(f"{name} must be nonnegative")
    return result


def floor_samples(value, rate, name="time"):
    return math.floor(seconds(value, name) * rate)


def ceil_samples(value, rate, name="time"):
    return math.ceil(seconds(value, name) * rate)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_revision(path):
    """Bind separate streaming source reads to a conservative file revision."""
    try:
        current = path.lstat()
    except OSError as exc:
        raise ManifestError("Source missing or changed since validation") from exc
    if not stat.S_ISREG(current.st_mode):
        raise ManifestError("Source must remain a regular file during validation")
    # Ignore reader atime; metadata is not an immutable PCM byte snapshot.
    return (current.st_dev, current.st_ino, current.st_mode, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns)


def _check_source(path, revision):
    if _source_revision(path) != revision:
        raise ManifestError("Source identity or metadata changed since validation")


def _no_constant(value):
    raise ManifestError(f"Nonfinite JSON value: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json_snapshot(path):
    """Return exact parsed values and the SHA256 of the same UTF-8 bytes.

    A pathname may be replaced after this read. Never reopen it to obtain the
    digest, and never hash reserialized values (whitespace is part of identity).
    """
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_constant=_no_constant,
                           object_pairs_hook=_unique_object)
        # Check Unicode and numeric overflow anywhere, including unused metadata.
        json_bytes(value)
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, InvalidOperation) as exc:
        raise ManifestError(f"Cannot read JSON {path}: {exc}") from exc


def read_json(path):
    """Retain decimal numbers exactly, including timestamps and ASR metadata."""
    return read_json_snapshot(path)[0]


def _json_text(value, level=0):
    """Encode JSON containers with exact Decimal numbers, without float coercion.

    The stdlib handles strings/escaping and ordinary scalars. Decimal tokens
    remain JSON numbers in provenance; converting them to strings or floats
    would change metadata types or silently lose the precision just retained.
    """
    if isinstance(value, Decimal):
        # Preserve the existing rejection of overflow (e.g. 1e999) as well as
        # NaN/Infinity. Underflowing finite decimals still retain their value.
        if not value.is_finite() or not math.isfinite(float(value)):
            raise ValueError("Nonfinite or overflowing JSON decimal")
        return str(value)
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            items.append(json.dumps(key, ensure_ascii=False) + ": " + _json_text(item, level + 1))
        opening, closing = "{", "}"
    elif isinstance(value, (list, tuple)):
        items = [_json_text(item, level + 1) for item in value]
        opening, closing = "[", "]"
    else:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if not items:
        return opening + closing
    indent = "  " * (level + 1)
    return opening + "\n" + indent + (",\n" + indent).join(items) + "\n" + "  " * level + closing


def json_bytes(value):
    return (_json_text(value) + "\n").encode("utf-8")


def wav_info(path):
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getcomptype() != "NONE":
                raise ManifestError("Only uncompressed integer PCM WAV is supported")
            info = {"sample_rate": audio.getframerate(), "channels": audio.getnchannels(),
                    "sample_width": audio.getsampwidth(), "total_samples": audio.getnframes()}
            if info["sample_width"] not in (1, 2, 3, 4):
                raise ManifestError("Only 8/16/24/32-bit integer PCM WAV is supported")
            for key in ("sample_rate", "channels", "total_samples"):
                integer(info[key], key, 1)
            # Read in bounded blocks: wave accepts some truncated data chunks.
            remaining = info["total_samples"]
            frame_bytes = info["channels"] * info["sample_width"]
            while remaining:
                count = min(65536, remaining)
                if len(audio.readframes(count)) != count * frame_bytes:
                    raise ManifestError(f"Truncated PCM data: {path}")
                remaining -= count
            # Non-frame-aligned data is not a complete PCM sample frame.
            if audio.readframes(1):
                raise ManifestError(f"Partial PCM frame: {path}")
            return info
    except (wave.Error, EOFError, OSError) as exc:
        raise ManifestError(f"Cannot read PCM WAV {path}: {exc}") from exc


def publish_files(outputs):
    """Stage all bytes, then publish each atomically without overwriting a file.

    Hard links keep an existing destination safe even under a concurrent writer.
    Roll back files published by this call on an ordinary I/O error.
    """
    paths = [Path(path) for path in outputs]
    if len({str(p.absolute()) for p in paths}) != len(paths):
        raise ManifestError("Output paths must be distinct")
    for path in paths:
        if path.exists() or path.is_symlink():
            raise ManifestError(f"Refusing to overwrite output: {path}")
        if not path.parent.is_dir():
            raise ManifestError(f"Output parent does not exist: {path.parent}")
    staged, published = [], []
    try:
        for path, data in outputs.items():
            fd, temporary = tempfile.mkstemp(prefix=".acm-", dir=Path(path).parent)
            staged.append((Path(temporary), Path(path)))
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, path in staged:
            os.link(temporary, path)
            published.append(path)
    except OSError as exc:
        for path in published:
            path.unlink(missing_ok=True)
        raise ManifestError(f"Cannot publish outputs: {exc}") from exc
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
