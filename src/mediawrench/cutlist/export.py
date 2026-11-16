"""Serialize one timeline and publish complete UTF-8 files without overwrites."""

from __future__ import annotations

import csv
from decimal import Decimal
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import tempfile

from .core import CutlistError, Timeline, _overlap_ids
from .frames import SECONDS_SERIALIZATION_ERROR, exact_ratio


def output_paths(prefix: str | Path) -> dict[str, Path]:
    prefix = Path(prefix)
    return {"llc": Path(str(prefix) + ".llc"), "csv": Path(str(prefix) + ".csv"),
            "provenance": Path(str(prefix) + ".provenance.json")}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _seconds(ms: int) -> float:
    value = ms / 1000
    if Decimal(str(value)) * 1000 != ms:
        raise CutlistError("Cut time is too large to export seconds without millisecond rounding")
    return value


def render_outputs(timeline: Timeline, media_file_name: str, *, source: dict | None = None) -> dict[str, str]:
    if (not isinstance(media_file_name, str) or not media_file_name.strip()
            or media_file_name in {".", ".."} or any(c in media_file_name for c in "/\\")
            or any(ord(c) < 32 for c in media_file_name)):
        raise CutlistError("mediaFileName must be a plain filename; place the project beside that media")
    if timeline.quantization_json is not None:
        return _render_frame_outputs(timeline, media_file_name, source)
    segments = [{"start": _seconds(cut.start_ms), "end": _seconds(cut.end_ms), "name": cut.name}
                for cut in timeline.cuts]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    # LosslessCut seconds CSV has no header: start, end, label.
    for cut in timeline.cuts:
        writer.writerow([f"{cut.start_ms // 1000}.{cut.start_ms % 1000:03d}",
                         f"{cut.end_ms // 1000}.{cut.end_ms % 1000:03d}", cut.name])
    selected = set(timeline.keep_ids) - set(timeline.drop_ids)
    by_id = {cue.id: cue for cue in timeline.cues}
    overlaps = _overlap_ids([(cut.start_ms, cut.end_ms) for cut in timeline.cuts],
                            [(cue.start_ms, cue.end_ms, cue.id) for cue in timeline.cues])
    cut_maps = []
    for index, (cut, segment, overlap_ids) in enumerate(zip(timeline.cuts, segments, overlaps), 1):
        contributions = []
        for cue_id in cut.cue_ids:
            cue = by_id[cue_id]
            a, b = max(0, cue.start_ms - timeline.padding_ms), min(timeline.duration_ms, cue.end_ms + timeline.padding_ms)
            overlap = (max(cut.start_ms, cue.start_ms), min(cut.end_ms, cue.end_ms))
            contributions.append({"cueId": cue.id, "paddedIntervalMs": [a, b],
                                  "retainedIntervalMs": [max(cut.start_ms, a), min(cut.end_ms, b)],
                                  "sourceOverlapMs": list(overlap) if overlap[0] < overlap[1] else None})
        cut_maps.append({"cutId": index, **segment, "startMs": cut.start_ms, "endMs": cut.end_ms,
                         "cueIds": list(cut.cue_ids), "contributions": contributions,
                         "overlappingCueIds": list(overlap_ids)})
    provenance = {
        "schemaVersion": 1, "generator": "transcript-cutlist/0.1.0", "source": source or {},
        "mediaFileName": media_file_name, "timeUnit": "seconds; *Ms fields are integer milliseconds",
        "intervalConvention": "[start, end)", "selectionPlan": json.loads(timeline.plan_json),
        "normalizedSelection": {"keep": list(timeline.keep_ids), "drop": list(timeline.drop_ids),
                                "effectiveKeep": sorted(selected), "paddingMs": timeline.padding_ms,
                                "durationMs": timeline.duration_ms},
        "cues": [{"id": cue.id, "ordinal": cue.ordinal, "startMs": cue.start_ms,
                  "endMs": cue.end_ms, "text": cue.text} for cue in timeline.cues],
        "cuts": cut_maps,
    }
    if timeline.duration_probe_json is not None:
        provenance['durationProbe'] = json.loads(timeline.duration_probe_json)
    return {"llc": _json({"version": 2, "mediaFileName": media_file_name, "cutSegments": segments}),
            "csv": stream.getvalue(), "provenance": _json(provenance)}


def _frame_seconds(frame: int, rate: Fraction) -> float:
    exact = frame / rate
    value = float(exact)
    # LLC/CSV use numeric seconds, so repeating rational boundaries need a
    # declared serialization tolerance. Reject values too coarse for that grid.
    position = Fraction(str(value)) * rate
    if (abs(Fraction(str(value)) - exact) > SECONDS_SERIALIZATION_ERROR
            or int(position + Fraction(1, 2)) != frame):
        raise CutlistError("Frame seconds exceed serialization precision; use a shorter timeline")
    return value


def _render_frame_outputs(timeline: Timeline, media_file_name: str, source: dict | None) -> dict[str, str]:
    quantization = json.loads(timeline.quantization_json)
    padded_frames: dict[int, dict[str, int]] = {}
    for entry in quantization['boundaries']:
        if entry['role'] == 'keep':
            padded_frames.setdefault(entry['cueId'], {})[entry['edge']] = entry['frame']
    by_id = {cue.id: cue for cue in timeline.cues}
    overlaps = _overlap_ids([(cut.start_ms, cut.end_ms) for cut in timeline.cuts],
                            [(cue.start_ms, cue.end_ms, cue.id) for cue in timeline.cues])
    segments = []
    cut_maps = []
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for index, (cut, overlap_ids) in enumerate(zip(timeline.cuts, overlaps), 1):
        rate = cut.frame_rate
        segment = {'start': _frame_seconds(cut.start_frame, rate),
                   'end': _frame_seconds(cut.end_frame, rate), 'name': cut.name}
        segments.append(segment)
        # repr is the same round-trippable numeric value as the JSON writer.
        writer.writerow([repr(segment['start']), repr(segment['end']), cut.name])
        contributions = []
        for cue_id in cut.cue_ids:
            cue = by_id[cue_id]
            span = padded_frames[cue_id]
            overlap = (max(cut.start_ms, cue.start_ms), min(cut.end_ms, cue.end_ms))
            contributions.append({
                'cueId': cue_id,
                'paddedIntervalMs': [max(0, cue.start_ms - timeline.padding_ms),
                                     min(timeline.duration_ms, cue.end_ms + timeline.padding_ms)],
                'quantizedPaddedFrameInterval': [span['start'], span['end']],
                'retainedFrameInterval': [max(cut.start_frame, span['start']), min(cut.end_frame, span['end'])],
                'sourceOverlapExactSeconds': [exact_ratio(Fraction(ms, 1000)) for ms in overlap]
                                            if overlap[0] < overlap[1] else None,
            })
        cut_maps.append({
            **quantization['cutBoundaries'][index - 1], **segment,
            'startExactSeconds': exact_ratio(cut.start_frame / rate),
            'endExactSeconds': exact_ratio(cut.end_frame / rate),
            'cueIds': list(cut.cue_ids), 'contributions': contributions, 'overlappingCueIds': list(overlap_ids),
        })
    provenance = {
        'schemaVersion': 2, 'generator': 'transcript-cutlist/0.1.0', 'source': source or {},
        'mediaFileName': media_file_name,
        'timeUnit': 'numeric seconds; exact seconds are rational; *Ms fields are original integer milliseconds',
        'intervalConvention': '[start, end)', 'selectionPlan': json.loads(timeline.plan_json),
        'normalizedSelection': {'keep': list(timeline.keep_ids), 'drop': list(timeline.drop_ids),
                                'effectiveKeep': sorted(set(timeline.keep_ids) - set(timeline.drop_ids)),
                                'paddingMs': timeline.padding_ms, 'durationMs': timeline.duration_ms},
        'cues': [{'id': cue.id, 'ordinal': cue.ordinal, 'startMs': cue.start_ms,
                  'endMs': cue.end_ms, 'text': cue.text} for cue in timeline.cues],
        'quantization': quantization, 'cuts': cut_maps,
    }
    if timeline.duration_probe_json is not None:
        provenance['durationProbe'] = json.loads(timeline.duration_probe_json)
    return {'llc': _json({'version': 2, 'mediaFileName': media_file_name, 'cutSegments': segments}),
            'csv': stream.getvalue(), 'provenance': _json(provenance)}


def validate_paths(paths: dict[str, Path], protected: tuple[Path, ...] = ()) -> None:
    if set(paths) != {"llc", "csv", "provenance"}:
        raise CutlistError("Exactly llc, csv and provenance output paths are required")
    resolved = [Path(path).resolve() for path in paths.values()]
    if len(set(resolved)) != len(resolved):
        raise CutlistError("Output path collision")
    protected_paths = {Path(path).resolve() for path in protected}
    for path in map(Path, paths.values()):
        if path.resolve() in protected_paths:
            raise CutlistError(f"Output collides with input/media: {path}")
        if os.path.lexists(path):
            raise CutlistError(f"Output already exists (never overwritten): {path}")
        if not path.parent.is_dir():
            raise CutlistError(f"Output directory must already exist: {path.parent}")


def write_outputs(paths: dict[str, Path], contents: dict[str, str], *, protected: tuple[Path, ...] = ()) -> None:
    """Stage/fsync all files, hard-link with no clobber, roll back on exceptions.

    Publication is atomic per file, not a crash-atomic multi-file transaction.
    No media, input or existing output is ever overwritten.
    """
    validate_paths(paths, protected)
    if set(contents) != set(paths):
        raise CutlistError("Contents must match all output paths")
    staged: dict[str, Path] = {}
    published: list[Path] = []
    try:
        for key, target in paths.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                             prefix=".transcript-cutlist-", dir=Path(target).parent,
                                             delete=False) as handle:
                staged[key] = Path(handle.name)
                handle.write(contents[key])
                handle.flush()
                os.fsync(handle.fileno())
        for key, target in paths.items():
            os.link(staged[key], target)
            published.append(Path(target))
    except BaseException:
        for target in reversed(published):
            target.unlink()
        raise
    finally:
        for temp in staged.values():
            temp.unlink(missing_ok=True)
