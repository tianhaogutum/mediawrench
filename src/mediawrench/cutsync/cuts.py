"""Small adapters for explicit JSON, LosslessCut JSON5, and CSV keep lists."""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import json5

from .core import MAX_KEEP_INTERVALS, MAX_TIME_MS, CutSyncError, Interval, round_ms

_CLOCK = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)([.,]\d{1,9})?$")
_SECONDS = re.compile(r"^\d+(?:\.\d{1,9})?$")


def parse_fps(value: str | Fraction) -> Fraction:
    """An explicit rational timebase for LosslessCut frame-number CSV."""
    raw = str(value)
    if len(raw) > 100 or not re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+)?", raw):
        raise CutSyncError("FPS must be a positive number or fraction such as 30000/1001")
    try:
        fps = Fraction(raw)
    except (ValueError, ZeroDivisionError) as exc:
        raise CutSyncError("invalid FPS") from exc
    if not 0 < fps <= 1000:
        raise CutSyncError("FPS must be greater than 0 and at most 1000")
    return fps


def _frame_time(value: object, fps: Fraction, context: str) -> Fraction:
    raw = str(value).strip()
    if len(raw) > 20 or not re.fullmatch(r"[0-9]+", raw):
        raise CutSyncError(f"{context}: frame CSV needs nonnegative integer frame numbers")
    exact = Fraction(int(raw) * 1000, 1) / fps
    if exact > MAX_TIME_MS:
        raise CutSyncError(f"{context}: frame time exceeds 99:59:59.999")
    return exact


def parse_time_exact(value: object, *, context: str = "time") -> Fraction:
    """Seconds or a clock, retained as exact rational milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CutSyncError(f"{context}: expected seconds or HH:MM:SS.mmm")
    raw = str(value).strip()
    if len(raw) > 100:
        raise CutSyncError(f"{context}: time is too large or invalid")
    match = _CLOCK.fullmatch(raw)
    try:
        if not isinstance(value, str):
            number = Decimal(str(value))
        elif match:
            hours, minutes, seconds, fraction = match.groups()
            number = Fraction(int(hours) * 3600 + int(minutes) * 60 + int(seconds))
            if fraction:
                number += Fraction(Decimal("0." + fraction[1:]))
        elif _SECONDS.fullmatch(raw):
            number = Decimal(raw)
        else:
            raise CutSyncError(f"{context}: invalid time {raw!r}; use seconds or HH:MM:SS.mmm")
        if isinstance(number, Decimal):
            # Check before Fraction(Decimal): a compact value like 1e1000000000
            # would otherwise request an enormous integer numerator/denominator.
            if not number.is_finite() or number < 0 or number > Decimal("359999.999"):
                raise CutSyncError(f"{context}: time must be between 0 and 99:59:59.999")
            if number.is_zero():
                return Fraction(0)
            parts = number.as_tuple()
            trailing_zeros = 0
            for digit in reversed(parts.digits):
                if digit:
                    break
                trailing_zeros += 1
            if -parts.exponent - trailing_zeros > 9:
                raise CutSyncError(
                    f"{context}: decimal times support at most nine fractional places"
                )
        ms = Fraction(number) * 1000
    except CutSyncError:
        raise
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise CutSyncError(f"{context}: time is too large or invalid") from exc
    if ms < 0 or ms > MAX_TIME_MS:
        raise CutSyncError(f"{context}: time must be between 0 and 99:59:59.999")
    return ms


def parse_time(value: object, *, context: str = "time") -> int:
    """Compatibility helper returning a rounded integer-millisecond display."""
    return round_ms(parse_time_exact(value, context=context))


def _interval(
    start: object,
    end: object,
    label: object,
    row: int,
    duration: int | Fraction | None,
    fps: Fraction | None = None,
) -> Interval:
    def bound(value: object, field: str) -> Fraction:
        context = f"keep row {row} {field}"
        if fps is not None:
            return _frame_time(value, fps, context)
        return parse_time_exact(value, context=context)

    if start is None or start == "":
        start = 0
    if end is None or end == "":
        if duration is None:
            raise CutSyncError(f"keep row {row}: open end needs --duration (source media duration)")
        end_ms = Fraction(duration)
    else:
        end_ms = bound(end, "end")
    start_ms = bound(start, "start")
    if duration is not None and end_ms > duration:
        raise CutSyncError(f"keep row {row}: end exceeds --duration")
    if not isinstance(label, str):
        raise CutSyncError(f"keep row {row}: name must be a string")
    # json5 0.17.3 leaves escaped surrogate pairs uncombined. Use Python's
    # Unicode codec to join valid pairs and reject unpaired surrogates.
    try:
        label = label.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
    except UnicodeError as exc:
        raise CutSyncError(f"keep row {row}: name contains an unpaired Unicode surrogate") from exc
    return Interval(
        round_ms(start_ms),
        round_ms(end_ms),
        label,
        source_row=row,
        exact_start_ms=start_ms,
        exact_end_ms=end_ms,
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def load_keep(
    text: str,
    *,
    format: str,
    duration_ms: int | Fraction | None = None,
    selected_only: bool = False,
    fps: str | Fraction | None = None,
) -> list[Interval]:
    """Read keep intervals, without sorting or silently inverting user edits."""
    if not isinstance(text, str):
        raise CutSyncError("keep list must be text")
    if format not in ("csv", "csv-frames", "json", "llc"):
        raise CutSyncError(f"unknown keep format: {format}")
    if selected_only and format != "llc":
        raise CutSyncError("--selected-only is only supported with .llc projects")
    if format == "csv-frames" and fps is None:
        raise CutSyncError("frame-number CSV requires --fps, for example --fps 30000/1001")
    if format != "csv-frames" and fps is not None:
        raise CutSyncError("--fps requires --keep-format csv-frames")
    frame_rate = parse_fps(fps) if fps is not None else None
    if duration_ms is not None and (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, (int, Fraction))
        or not 0 <= duration_ms <= MAX_TIME_MS
    ):
        raise CutSyncError("source duration must be milliseconds within the supported range")
    text = text.lstrip("\ufeff")
    result = []
    if format in ("csv", "csv-frames"):
        first = True
        try:
            for row in csv.reader(io.StringIO(text, newline=""), strict=True):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if first and [cell.strip().lower() for cell in row[:2]] == ["start", "end"]:
                    first = False
                    continue
                first = False
                row_num = len(result) + 1
                if row_num > MAX_KEEP_INTERVALS:
                    raise CutSyncError(f"input exceeds {MAX_KEEP_INTERVALS:,} keep-range limit")
                if len(row) < 2:
                    raise CutSyncError(f"keep row {row_num}: CSV needs start and end columns")
                result.append(
                    _interval(
                        row[0].strip(),
                        row[1].strip(),
                        row[2] if len(row) > 2 else "",
                        row_num,
                        duration_ms,
                        frame_rate,
                    )
                )
        except csv.Error as exc:
            raise CutSyncError(f"invalid CSV: {exc}") from exc
        if not result:
            raise CutSyncError(
                'CSV has no keep ranges; use JSON {"keep":[]} to intentionally keep none'
            )
        return result
    try:
        if format == "llc":
            obj = json5.loads(text, parse_float=Decimal, allow_duplicate_keys=False)
        else:
            obj = json.loads(text, parse_float=Decimal, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise CutSyncError(f"invalid {format.upper()} keep list: {exc}") from exc
    if format == "llc":
        if (
            not isinstance(obj, dict)
            or type(obj.get("version")) is not int
            or obj["version"] not in (1, 2)
        ):
            raise CutSyncError("LosslessCut project must have version 1 or 2")
        rows = obj.get("cutSegments")
    else:
        if isinstance(obj, dict):
            if set(obj) != {"keep"}:
                raise CutSyncError(
                    'JSON object must have a single "keep" key (these are KEEP ranges)'
                )
            rows = obj["keep"]
        else:
            rows = obj
    if not isinstance(rows, list):
        raise CutSyncError("keep list must be an array")
    if len(rows) > MAX_KEEP_INTERVALS:
        raise CutSyncError(f"input exceeds {MAX_KEEP_INTERVALS:,} keep-range limit")
    for row_num, row in enumerate(rows, 1):
        if isinstance(row, dict):
            if format == "json" and set(row) - {"start", "end", "name"}:
                raise CutSyncError(
                    f"keep row {row_num}: unknown fields; only start/end/name supported"
                )
            if "selected" in row and type(row["selected"]) is not bool:
                raise CutSyncError(f"keep row {row_num}: selected must be a boolean")
            if "start" in row and (row["start"] is None or row["start"] == ""):
                raise CutSyncError(f"keep row {row_num}: start must be explicit, not null or empty")
            if "end" in row and (row["end"] is None or row["end"] == ""):
                raise CutSyncError(f"keep row {row_num}: omit an open end instead of null or empty")
            if selected_only and row.get("selected", True) is False:
                continue
            if "start" not in row and not (format == "llc" and obj["version"] == 1):
                raise CutSyncError(f"keep row {row_num}: missing start")
            result.append(
                _interval(
                    row.get("start"), row.get("end"), row.get("name", ""), row_num, duration_ms
                )
            )
        elif isinstance(row, list) and len(row) == 2 and format != "llc":
            if any(value is None or value == "" for value in row):
                raise CutSyncError(f"keep row {row_num}: array endpoints must be explicit")
            result.append(_interval(row[0], row[1], "", row_num, duration_ms))
        else:
            raise CutSyncError(f"keep row {row_num}: expected start/end object or two-item array")
    return result


def infer_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in ("csv", "json", "llc"):
        raise CutSyncError("keep file extension must be .csv, .json or .llc; or use --keep-format")
    return suffix
