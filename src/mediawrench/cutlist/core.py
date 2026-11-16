"""Strict inputs and integer-millisecond interval algebra; no media execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
from fractions import Fraction
import hashlib
import heapq
import json
from pathlib import Path
import re
from typing import Any

import pysubs2


class CutlistError(ValueError):
    """An invalid or unsupported input; callers should not write outputs."""


@dataclass(frozen=True)
class Cue:
    id: int
    ordinal: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Cut:
    start_ms: int
    end_ms: int
    cue_ids: tuple[int, ...]
    name: str


@dataclass(frozen=True)
class FrameCut:
    """An exact half-open interval on a constant-rate, zero-origin frame grid."""

    start_frame: int
    end_frame: int
    frame_rate: Fraction
    cue_ids: tuple[int, ...]
    name: str

    @property
    def start_ms(self) -> Fraction:
        return self.start_frame * 1000 / self.frame_rate

    @property
    def end_ms(self) -> Fraction:
        return self.end_frame * 1000 / self.frame_rate


@dataclass(frozen=True)
class Timeline:
    cues: tuple[Cue, ...]
    cuts: tuple[Cut | FrameCut, ...]
    plan_json: str
    keep_ids: tuple[int, ...]
    drop_ids: tuple[int, ...]
    padding_ms: int
    duration_ms: int
    duration_probe_json: str | None = None
    quantization_json: str | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise CutlistError(f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def load_plan(path: str | Path) -> dict:
    def invalid_number(value: str) -> None:
        raise CutlistError(f"Non-finite JSON number: {value}")

    def decimal_seconds(value: str) -> float:
        exact = Decimal(value)
        ms = exact * 1000
        converted = float(exact)
        if (not exact.is_finite() or ms != ms.to_integral_value()
                or Decimal(str(converted)) != exact):
            raise CutlistError("JSON numbers require millisecond precision without float rounding")
        return converted

    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object, parse_constant=invalid_number,
                          parse_float=decimal_seconds)
    except CutlistError:
        raise
    except (UnicodeError, ValueError, DecimalException) as exc:
        raise CutlistError(f"Invalid UTF-8 selection JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise CutlistError("Selection plan must be a JSON object")
    return plan


_STAMP = r"[0-9]{2}:[0-5][0-9]:[0-5][0-9],[0-9]{3}"
_TIMING = re.compile(rf"({_STAMP}) --> ({_STAMP})")


def read_srt(path: str | Path) -> tuple[Cue, ...]:
    """Keep original unique numeric IDs, including nonsequential/out-of-order IDs.

    Strict framing prevents pysubs2's permissive reader from skipping malformed
    material. pysubs2 performs the actual SRT timestamp parsing; original text
    stays verbatim in provenance, including markup and numeric last lines.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise CutlistError("SRT must be UTF-8 (optional BOM)") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text:
        raise CutlistError("SRT is empty")
    frames: list[tuple[int, str]] = []
    seen: set[int] = set()
    for ordinal, block in enumerate(re.split(r"\n[ \t]*\n+", text), 1):
        lines = block.split("\n")
        if len(lines) < 3 or not re.fullmatch(r"[1-9][0-9]*", lines[0]):
            raise CutlistError(f"SRT block {ordinal}: expected positive numeric ID, timing and text")
        cue_id = int(lines[0])
        if cue_id in seen:
            raise CutlistError(f"Duplicate SRT cue ID: {cue_id}")
        seen.add(cue_id)
        if not _TIMING.fullmatch(lines[1]):
            raise CutlistError(f"Cue {cue_id}: unsupported SRT timestamp/settings")
        body = "\n".join(lines[2:])
        if not body.strip() or "-->" in body:
            raise CutlistError(f"Cue {cue_id}: empty text or embedded timing arrow is unsupported")
        frames.append((cue_id, body))
    parsed = pysubs2.SSAFile.from_string(text, format_="srt", keep_html_tags=True)
    if len(parsed) != len(frames):
        raise CutlistError("SRT parsing did not preserve every cue")
    cues: list[Cue] = []
    for ordinal, ((cue_id, body), event) in enumerate(zip(frames, parsed), 1):
        if event.start < 0 or event.end <= event.start:
            raise CutlistError(f"Cue {cue_id}: end must be strictly after start")
        cues.append(Cue(cue_id, ordinal, event.start, event.end, body))
    return tuple(cues)


def _milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CutlistError(f"{field} must be a finite number of seconds")
    try:
        ms = Decimal(str(value)) * 1000
        if not ms.is_finite() or ms != ms.to_integral_value() or abs(ms) > 2**53 - 1:
            raise CutlistError(f"{field} must have millisecond precision and fit a safe integer")
        return int(ms)
    except DecimalException as exc:
        raise CutlistError(f"Invalid {field}") from exc


def _ids(value: Any, field: str, known: set[int]) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise CutlistError(f"{field} must be an array of cue IDs or {{\"from\": ID, \"to\": ID}} ranges")
    result: set[int] = set()
    for item in value:
        if type(item) is int:
            ids = [item]
        elif isinstance(item, dict) and set(item) == {"from", "to"}:
            low, high = item["from"], item["to"]
            if type(low) is not int or type(high) is not int or low <= 0 or high < low:
                raise CutlistError(f"Invalid inclusive {field} range: {item}")
            if high - low + 1 > len(known):
                raise CutlistError(f"{field} range includes unknown cue IDs: {item}")
            ids = range(low, high + 1)
        else:
            raise CutlistError(f"Unsupported {field} selection syntax: {item!r}")
        for cue_id in ids:
            if cue_id not in known:
                raise CutlistError(f"Unknown {field} cue ID: {cue_id}")
            result.add(cue_id)
    return tuple(sorted(result))


def _union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(kept: list[tuple[int, int]], drops: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sweep two sorted disjoint lists in O(kept + drops + output)."""
    result: list[tuple[int, int]] = []
    j = 0
    for start, end in kept:
        while j < len(drops) and drops[j][1] <= start:
            j += 1
        cursor, k = start, j
        while k < len(drops) and drops[k][0] < end:
            a, b = drops[k]
            if cursor < a:
                result.append((cursor, a))
            cursor = max(cursor, b)
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            result.append((cursor, end))
        j = k
    return result


def _overlap_ids(intervals: list[tuple[int | Fraction, int | Fraction]],
                 spans: list[tuple[int, int, int]]) -> list[tuple[int, ...]]:
    """Sweep sorted cuts; enumerate only the active source spans for each cut."""
    ordered = sorted(spans)
    cursor = 0
    active: set[int] = set()
    endings: list[tuple[int, int]] = []
    result = []
    for start, end in intervals:
        while cursor < len(ordered) and ordered[cursor][0] < end:
            _, b, cue_id = ordered[cursor]
            if b > start:
                active.add(cue_id)
                heapq.heappush(endings, (b, cue_id))
            cursor += 1
        while endings and endings[0][0] <= start:
            _, cue_id = heapq.heappop(endings)
            active.remove(cue_id)
        result.append(tuple(sorted(active)))
    return result


def build_timeline(cues: tuple[Cue, ...], plan: dict) -> Timeline:
    if not cues or len({cue.id for cue in cues}) != len(cues):
        raise CutlistError("Cues must be nonempty with unique IDs")
    if not isinstance(plan, dict) or set(plan) - {"keep", "drop", "padding", "duration", "labels", "frameRate"}:
        raise CutlistError("Unknown selection fields; supported: keep, drop, padding, duration, labels, frameRate")
    if "duration" not in plan or "keep" not in plan:
        raise CutlistError("Selection requires keep and duration")
    duration = _milliseconds(plan["duration"], "duration")
    padding = _milliseconds(plan.get("padding", 0), "padding")
    if duration <= 0 or not 0 <= padding <= duration:
        raise CutlistError("duration must be positive; padding must be between zero and duration")
    known = {cue.id for cue in cues}
    keep = _ids(plan["keep"], "keep", known)
    drop = _ids(plan.get("drop", []), "drop", known)
    keep_set, drop_set = set(keep), set(drop)
    labels = plan.get("labels", {})
    if not isinstance(labels, dict):
        raise CutlistError("labels must map selected cue ID strings to nonempty text")
    for key, value in labels.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key)
                or int(key) not in keep_set or not isinstance(value, str) or not value.strip()
                or any(ord(char) < 32 for char in value)):
            raise CutlistError("labels require selected cue ID keys and nonempty text without control characters")
    for cue in cues:
        if (type(cue.id) is not int or cue.id <= 0 or type(cue.start_ms) is not int
                or type(cue.end_ms) is not int or not 0 <= cue.start_ms < cue.end_ms <= duration):
            raise CutlistError(f"Cue {cue.id}: timing outside duration [0, {duration / 1000}] or invalid ID")
    effective_keep = keep_set - drop_set
    selected = [cue for cue in cues if cue.id in effective_keep]
    padded = [(max(0, cue.start_ms - padding), min(duration, cue.end_ms + padding), cue.id)
              for cue in selected]
    merged = _union([(a, b) for a, b, _ in padded])
    exclusions = _union([(cue.start_ms, cue.end_ms) for cue in cues if cue.id in drop_set])
    intervals = _subtract(merged, exclusions)
    if not intervals:
        raise CutlistError("Empty selection after applying keep/drop")
    cuts: list[Cut] = []
    for (start, end), ids in zip(intervals, _overlap_ids(intervals, padded)):
        if labels:
            name = " / ".join(labels.get(str(cue_id), f"cue {cue_id}") for cue_id in ids)
        else:
            name = "cues " + ",".join(map(str, ids))
        cuts.append(Cut(start, end, ids, name))
    timeline = Timeline(tuple(cues), tuple(cuts), json.dumps(plan, ensure_ascii=False, sort_keys=True,
                        allow_nan=False), keep, drop, padding, duration)
    if "frameRate" in plan:
        from .frames import quantize_timeline
        return quantize_timeline(timeline, plan["frameRate"])
    return timeline


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
