"""Exact rational frame quantization; exclusions win before numeric export."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
import re

from .core import CutlistError, FrameCut, Timeline, _overlap_ids, _subtract, _union


SECONDS_SERIALIZATION_ERROR = Fraction(1, 10**9)


def _frame_rate(value: str) -> Fraction:
    if (not isinstance(value, str) or len(value) > 21
            or not re.fullmatch(r'[1-9][0-9]*(?:/[1-9][0-9]*)?', value)):
        raise CutlistError('frameRate must be a positive integer or rational string, e.g. "24" or "30000/1001"')
    parts = list(map(int, value.split('/')))
    rate = Fraction(value)
    if max(parts) > 10**9 or rate > 10**6:
        raise CutlistError('frameRate components must be <= 1000000000 and rate <= 1000000 fps')
    return rate


def exact_ratio(value: Fraction | int) -> dict[str, int]:
    value = Fraction(value)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def quantize_timeline(timeline: Timeline, frame_rate: str) -> Timeline:
    """Quantize original padded keeps and unpadded exclusions, then redo algebra.

    Keeps round nearest (ties later); drops expand outwards. Clamp all boundaries
    to [0, floor(duration * rate)] and discard collapsed keeps. Every boundary
    moves by strictly less than one frame. Original cues/plan remain immutable.
    Applying the same rational rate again returns the same Timeline. A different
    rate requires the original millisecond timeline, avoiding accumulated error.
    """
    rate = _frame_rate(frame_rate)
    if timeline.quantization_json is not None:
        previous = json.loads(timeline.quantization_json)['frameRate']
        if rate != Fraction(previous['numerator'], previous['denominator']):
            raise CutlistError('Use the original millisecond timeline to change frameRate')
        return timeline
    limit = int(Fraction(timeline.duration_ms, 1000) * rate)
    if limit < 1:
        raise CutlistError('Duration contains no full frame at frameRate')
    if limit > 2**53 - 1:
        raise CutlistError('Duration at frameRate exceeds safe frame numbering')
    boundaries = []
    collapsed = []
    kept = []
    dropped = []
    selected = set(timeline.keep_ids) - set(timeline.drop_ids)
    exclusions = set(timeline.drop_ids)

    def boundary(ms: int, cue_id: int, role: str, edge: str) -> int:
        position = Fraction(ms, 1000) * rate
        if role == 'keep':
            rounded = int(position + Fraction(1, 2))
            policy = 'nearest-ties-later'
        elif edge == 'start':
            rounded = position.numerator // position.denominator
            policy = 'floor-exclusion-start'
        else:
            rounded = -(-position.numerator // position.denominator)
            policy = 'ceil-exclusion-end'
        frame = max(0, min(limit, rounded))
        error = frame / rate - Fraction(ms, 1000)
        boundaries.append({
            'boundaryId': len(boundaries) + 1, 'cueId': cue_id, 'role': role, 'edge': edge,
            'originalMs': ms, 'frame': frame, 'exactSeconds': exact_ratio(frame / rate),
            'direction': 'later' if error > 0 else 'earlier' if error < 0 else 'unchanged',
            'errorSeconds': exact_ratio(error), 'policy': policy, 'clamped': frame != rounded,
        })
        return frame

    for cue in sorted(timeline.cues, key=lambda cue: cue.id):
        if cue.id in selected:
            a = boundary(max(0, cue.start_ms - timeline.padding_ms), cue.id, 'keep', 'start')
            b = boundary(min(timeline.duration_ms, cue.end_ms + timeline.padding_ms), cue.id, 'keep', 'end')
            if a < b:
                kept.append((a, b, cue.id))
            else:
                collapsed.append(cue.id)
        if cue.id in exclusions:
            a = boundary(cue.start_ms, cue.id, 'drop', 'start')
            b = boundary(cue.end_ms, cue.id, 'drop', 'end')
            if a < b:
                dropped.append((a, b))
    intervals = _subtract(_union([(a, b) for a, b, _ in kept]), _union(dropped))
    if not intervals:
        raise CutlistError('Empty selection after frame quantization and exclusions')
    labels = json.loads(timeline.plan_json).get('labels', {})
    cuts = []
    for (start, end), ids in zip(intervals, _overlap_ids(intervals, kept)):
        name = (' / '.join(labels.get(str(cue_id), f'cue {cue_id}') for cue_id in ids)
                if labels else 'cues ' + ','.join(map(str, ids)))
        cuts.append(FrameCut(start, end, rate, ids, name))
    boundary_ids: dict[int, list[int]] = {}
    for entry in boundaries:
        boundary_ids.setdefault(entry['frame'], []).append(entry['boundaryId'])
    evidence = {
        'frameRate': exact_ratio(rate), 'timeBase': exact_ratio(1 / rate), 'originSeconds': 0,
        'keepPolicy': 'nearest-ties-later', 'dropPolicy': 'outward',
        'durationPolicy': 'floor-to-frame', 'durationFrameFloor': limit,
        'maxEndpointErrorSecondsExclusive': exact_ratio(1 / rate),
        'nearestUnclampedMaxErrorSecondsInclusive': exact_ratio(1 / (2 * rate)),
        'secondsSerializationMaxErrorInclusive': exact_ratio(SECONDS_SERIALIZATION_ERROR),
        'boundaries': boundaries, 'collapsedKeepCueIds': collapsed,
        'cutBoundaries': [{'cutId': index, 'startFrame': cut.start_frame, 'endFrame': cut.end_frame,
                           'startBoundaryIds': boundary_ids[cut.start_frame],
                           'endBoundaryIds': boundary_ids[cut.end_frame]}
                          for index, cut in enumerate(cuts, 1)],
    }
    return replace(timeline, cuts=tuple(cuts), quantization_json=json.dumps(evidence, sort_keys=True))
