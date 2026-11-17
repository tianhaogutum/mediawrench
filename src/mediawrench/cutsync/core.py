"""Piecewise mapping only. SRT parsing and rendering belong to the `srt` library."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from fractions import Fraction
from typing import Literal

import srt

Boundary = Literal["split", "drop", "longest"]
MAX_TIME_MS = 359_999_999  # 99:59:59,999; explicit, bounded SRT output domain.
MAX_INTERSECTIONS = 100_000
MAX_KEEP_INTERVALS = 20_000
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


class CutSyncError(ValueError):
    """Invalid input or an unsafe output request."""


@dataclass(frozen=True)
class Interval:
    start_ms: int
    end_ms: int
    label: str = ""
    source_row: int | None = field(default=None, compare=False)
    exact_start_ms: Fraction | None = field(default=None, compare=False)
    exact_end_ms: Fraction | None = field(default=None, compare=False)


@dataclass(frozen=True)
class MappedInterval:
    source_start: Fraction
    source_end: Fraction
    output_start: Fraction
    input_rows: tuple[int, ...]
    labels: tuple[str, ...]

    @property
    def output_end(self) -> Fraction:
        return self.output_start + self.source_end - self.source_start


@dataclass(frozen=True)
class Timeline:
    segments: tuple[MappedInterval, ...]

    @property
    def duration_ms(self) -> int:
        return round_ms(self.duration_exact_ms)

    @property
    def duration_exact_ms(self) -> Fraction:
        return self.segments[-1].output_end if self.segments else Fraction(0)


def round_ms(value: int | Fraction) -> int:
    """Nearest millisecond, half up, with exact arithmetic for nonnegative times."""
    numerator, denominator = value.numerator, value.denominator
    return (2 * numerator + denominator) // (2 * denominator)


def _integer_ms(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CutSyncError(f"{name} must be an integer number of milliseconds")
    if value < (1 if positive else 0) or value > MAX_TIME_MS:
        raise CutSyncError(f"{name} must be between {1 if positive else 0} and {MAX_TIME_MS} ms")
    return value


def _positive_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CutSyncError(f"{name} must be a positive integer")
    return value


def make_timeline(intervals: list[Interval]) -> Timeline:
    """Validate sorted nonoverlapping intervals; coalesce touching intervals.

    Coalescing avoids a fake split when two keep ranges share a boundary and
    no source time was removed. Input row numbers stay available for review.
    """
    segments: list[MappedInterval] = []
    group_start: Fraction | None = None
    group_end = Fraction(0)
    output_start = Fraction(0)
    rows: list[int] = []
    labels: list[str] = []
    for row, interval in enumerate(intervals, 1):
        if row > MAX_KEEP_INTERVALS:
            raise CutSyncError(f"input exceeds {MAX_KEEP_INTERVALS:,} keep-range limit")
        if not isinstance(interval, Interval):
            raise CutSyncError(f"keep row {row}: expected an Interval")
        source_row = interval.source_row if interval.source_row is not None else row
        _positive_count(source_row, "input row number")
        start = _integer_ms(interval.start_ms, f"keep row {row} start")
        end = _integer_ms(interval.end_ms, f"keep row {row} end")
        if start >= end:
            raise CutSyncError(f"keep row {row}: start must be before end after ms rounding")
        exact_start = (
            interval.exact_start_ms if interval.exact_start_ms is not None else Fraction(start)
        )
        exact_end = interval.exact_end_ms if interval.exact_end_ms is not None else Fraction(end)
        if not isinstance(exact_start, Fraction) or not isinstance(exact_end, Fraction):
            raise CutSyncError(f"keep row {row}: exact endpoints must be Fraction milliseconds")
        if (
            not 0 <= exact_start < exact_end <= MAX_TIME_MS
            or round_ms(exact_start) != start
            or round_ms(exact_end) != end
        ):
            raise CutSyncError(
                f"keep row {row}: invalid exact endpoints or inconsistent ms display"
            )
        if not isinstance(interval.label, str):
            raise CutSyncError(f"keep row {row}: label must be a string")
        if group_start is not None and exact_start < group_end:
            raise CutSyncError(f"keep row {row}: intervals must be ordered and must not overlap")
        if group_start is not None and exact_start != group_end:
            segments.append(
                MappedInterval(
                    group_start,
                    group_end,
                    output_start,
                    tuple(rows),
                    tuple(labels),
                )
            )
            output_start += group_end - group_start
            group_start = None
            rows, labels = [], []
        if group_start is None:
            group_start = exact_start
        group_end = exact_end
        rows.append(source_row)
        labels.append(interval.label)
    if group_start is not None:
        segments.append(
            MappedInterval(
                group_start,
                group_end,
                output_start,
                tuple(rows),
                tuple(labels),
            )
        )
    return Timeline(tuple(segments))


def milliseconds(value: timedelta) -> int:
    """Avoid float total_seconds() rounding, even on long recordings."""
    if value.microseconds % 1000:
        raise CutSyncError("SRT timestamps must resolve to whole milliseconds")
    return (value.days * 86400 + value.seconds) * 1000 + value.microseconds // 1000


def stamp(ms: int) -> str:
    return srt.timedelta_to_srt_timestamp(timedelta(milliseconds=ms))


def span(start: int | Fraction, end: int | Fraction) -> dict:
    left, right = round_ms(start), round_ms(end)
    result = {"start_ms": left, "end_ms": right, "start": stamp(left), "end": stamp(right)}
    if start.denominator != 1 or end.denominator != 1:
        result["start_exact_ms"] = str(start)
        result["end_exact_ms"] = str(end)
    return result


def remap(
    subtitles: list[srt.Subtitle],
    timeline: Timeline,
    *,
    boundary: Boundary = "split",
    min_duration_ms: int = 1,
    max_intersections: int = MAX_INTERSECTIONS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[list[srt.Subtitle], dict]:
    """Return new subtitle objects and one audit entry per input cue.

    Intervals are half-open: a cue ending at a keep start or starting at a
    keep end has no intersection. All choices operate on the entire cue text;
    there is no word alignment or inference about what was spoken.
    """
    if boundary not in ("split", "drop", "longest"):
        raise CutSyncError(f"unknown boundary policy: {boundary}")
    _integer_ms(min_duration_ms, "minimum duration", positive=True)
    _positive_count(max_intersections, "maximum intersections")
    _positive_count(max_output_bytes, "maximum output bytes")
    ends = [seg.source_end for seg in timeline.segments]
    entries: list[dict] = []
    pending: list[tuple[srt.Subtitle, dict, int, int]] = []
    intersection_count = 0
    estimated_output_bytes = 0
    for position, cue in enumerate(subtitles, 1):
        start, end = milliseconds(cue.start), milliseconds(cue.end)
        _integer_ms(start, f"cue {position} start")
        _integer_ms(end, f"cue {position} end")
        if end <= start:
            raise CutSyncError(f"cue {position}: end must be after start")
        text = srt.make_legal_content(cue.content)
        if not text.strip():
            raise CutSyncError(f"cue {position}: empty subtitle text")
        # Count the body once per source cue, not once per fragment. Repeated
        # full text is cheap as shared objects but can explode during compose().
        text_bytes = len(text.encode("utf-8")) + len(cue.proprietary.encode("utf-8"))
        entry = {
            "source_position": position,
            "source_index": cue.index,
            "source": {**span(start, end), "text": cue.content, "proprietary": cue.proprietary},
            "text_normalized": text != cue.content,
            "outputs": [],
            "discarded_fragments": [],
        }
        intersections = []
        seg_index = bisect_right(ends, start)
        while seg_index < len(timeline.segments):
            seg = timeline.segments[seg_index]
            if seg.source_start >= end:
                break
            intersection_count += 1
            if intersection_count > max_intersections:
                raise CutSyncError(
                    f"mapping exceeds {max_intersections:,} cue/segment intersections; "
                    "split the job or simplify the keep list"
                )
            left, right = max(start, seg.source_start), min(end, seg.source_end)
            intersections.append((seg_index + 1, left, right, seg))
            seg_index += 1
        crossing = bool(intersections) and (
            len(intersections) != 1 or intersections[0][1] != start or intersections[0][2] != end
        )
        entry["crosses_boundary"] = crossing
        if boundary == "drop" and crossing:
            selected = []
            reason = "crosses_boundary"
        elif boundary == "longest" and intersections:
            selected = [max(intersections, key=lambda part: part[2] - part[1])]
            reason = "outside_keep_intervals"
        else:
            selected = intersections
            reason = "outside_keep_intervals"
        selected_segments = {part[0] for part in selected}
        for part in intersections:
            index, left, right, seg = part
            if index not in selected_segments:
                entry["discarded_fragments"].append(
                    {
                        "segment": index,
                        "source": span(left, right),
                        "reason": "boundary_policy",
                    }
                )
                continue
            out_start_exact = seg.output_start + left - seg.source_start
            out_end_exact = seg.output_start + right - seg.source_start
            out_start, out_end = round_ms(out_start_exact), round_ms(out_end_exact)
            if out_end - out_start < min_duration_ms:
                discard_reason = "rounded_to_zero" if out_start == out_end else "below_min_duration"
                entry["discarded_fragments"].append(
                    {
                        "segment": index,
                        "source": span(left, right),
                        "reason": discard_reason,
                    }
                )
                reason = discard_reason
                continue
            # 100 bytes conservatively cover numbering, timestamps, metadata
            # separators and newlines within the supported time/cue domain.
            estimated_output_bytes += text_bytes + 100
            if estimated_output_bytes > max_output_bytes:
                raise CutSyncError(
                    f"estimated expanded SRT exceeds {max_output_bytes:,}-byte output budget; "
                    "split long cues, simplify the keep list, or choose longest/drop"
                )
            fragment = {
                "segment": index,
                "source": span(left, right),
                "output": span(out_start_exact, out_end_exact),
                "offset_ms": out_start - round_ms(left),
                "offset_exact_ms": str(seg.output_start - seg.source_start),
            }
            entry["outputs"].append(fragment)
            pending.append(
                (
                    srt.Subtitle(
                        index=0,
                        start=timedelta(milliseconds=out_start),
                        end=timedelta(milliseconds=out_end),
                        content=text,
                        proprietary=cue.proprietary,
                    ),
                    fragment,
                    position,
                    len(entry["outputs"]),
                )
            )
        outputs = entry["outputs"]
        if not outputs:
            action = "dropped"
        elif len(outputs) > 1:
            action = "split"
        elif crossing:
            action = "trimmed"
        elif outputs[0]["offset_ms"]:
            action = "shifted"
        else:
            action = "unchanged"
        entry["action"] = action
        entry["reason"] = (
            reason if not outputs else ("boundary_intersection" if crossing else "time_mapping")
        )
        entry["needs_review"] = crossing or entry["text_normalized"]
        entries.append(entry)
    # Keep overlapping captions and preserve source order for exactly equal times.
    pending.sort(key=lambda item: (item[0].start, item[0].end, item[2], item[3]))
    output = []
    for output_index, (cue, fragment, _, _) in enumerate(pending, 1):
        cue.index = output_index
        fragment["output_index"] = output_index
        output.append(cue)
    counts = Counter(entry["action"] for entry in entries)
    audit = {
        "schema_version": 1,
        "boundary_policy": boundary,
        "min_duration_ms": min_duration_ms,
        "max_intersections": max_intersections,
        "max_output_bytes": max_output_bytes,
        "interval_semantics": "half-open [start, end); touching keep intervals coalesced",
        "time_unit": "milliseconds",
        "duration_ms": timeline.duration_ms,
        "duration_exact_ms": str(timeline.duration_exact_ms),
        "segments": [
            {
                "segment": i,
                "source": span(seg.source_start, seg.source_end),
                "output": span(seg.output_start, seg.output_end),
                "input_rows": list(seg.input_rows),
                "labels": list(seg.labels),
            }
            for i, seg in enumerate(timeline.segments, 1)
        ],
        "summary": {
            "input_cues": len(subtitles),
            "output_cues": len(output),
            "estimated_srt_bytes": estimated_output_bytes,
            **{key: counts[key] for key in ("unchanged", "shifted", "trimmed", "split", "dropped")},
            "review_cues": sum(entry["needs_review"] for entry in entries),
            "normalized_text_cues": sum(entry["text_normalized"] for entry in entries),
        },
        "cues": entries,
    }
    return output, audit
