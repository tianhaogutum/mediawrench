"""Deterministic constrained shortest path, plus a deliberately simple baseline."""
from dataclasses import asdict
import re

from wcwidth import iter_graphemes

from .layout import display_text, grapheme_unit_stops, layout, measure_width
from .model import Config, InputError, Normalized

_PUNCT = re.compile(r'[.!?。！？;；:：][\s\"\'”’）)]*$')


def _candidate(units, start, stop, config):
    chosen = units[start:stop]
    original = "".join(u.text for u in chosen)
    lines, overflow = layout(original, config.max_chars, config.width_mode)
    duration = chosen[-1].end_ms - chosen[0].start_ms
    # Count normalized display text before wrapping: a layout newline must not
    # remove a source space or insert a speaking-rate unit between CJK clusters.
    rendered = display_text(original)
    count = len(rendered) if config.cps_mode == "codepoint" else sum(1 for _ in iter_graphemes(rendered))
    cps = count * 1000 / duration
    count_unit = "codepoints" if config.cps_mode == "codepoint" else "graphemes"
    speaking_rate = {"cps_mode": config.cps_mode, "count": count, "count_unit": count_unit,
                     "cps": round(cps, 6), "rate_unit": count_unit + "/second"}
    violations = []
    if overflow:
        violations.append({"code": "line_width", "overflow_chars": overflow, "max_chars": config.max_chars,
                           "width_mode": config.width_mode})
    max_ms = round(config.max_duration * 1000)
    if duration > max_ms:
        violations.append({"code": "duration", "excess_ms": duration - max_ms, "max_ms": max_ms})
    if cps > config.max_cps:
        violations.append({"code": "speaking_rate", **speaking_rate, "max_cps": config.max_cps})
        if config.profile_name is not None:
            units_name = "code points" if config.cps_mode == "codepoint" else "graphemes"
            violations[-1]["reason"] = f"Displayed {units_name} exceed the profile's CPS bound at fixed effective source times"
    min_ms = round(config.min_duration * 1000)
    duration_shortfall = max(0, min_ms - duration)
    if duration_shortfall:
        violations.append({"code": "minimum_duration", "duration_ms": duration, "min_ms": min_ms,
            "shortfall_ms": duration_shortfall,
            "reason": "Effective source interval is shorter than the minimum; display times stay fixed to source units"})
    gap_shortfall = 0
    if stop < len(units):
        gap = units[stop].start_ms - chosen[-1].end_ms
        gap_min_ms = round(config.min_gap * 1000)
        gap_shortfall = max(0, gap_min_ms - gap)
        if gap_shortfall:
            violations.append({"code": "minimum_gap", "gap_ms": gap, "min_ms": gap_min_ms,
                "shortfall_ms": gap_shortfall, "next_unit_id": units[stop].id,
                "reason": "Insufficient source silence at this cue boundary; times stay fixed and source segment boundaries are preserved"})
    target = round(config.target_duration * 1000)
    duration_cost = (duration - target) ** 2 * 1000 // (target ** 2)
    internal_pause_cost = sum(3 * (b.start_ms - a.end_ms) for a, b in zip(chosen, chosen[1:])
                              if b.start_ms - a.end_ms >= round(config.pause * 1000))
    boundary_cost = 0
    if stop < len(units):
        gap = units[stop].start_ms - chosen[-1].end_ms
        boundary_cost = max(0, 1000 - min(gap, 1000) - (600 if _PUNCT.search(original) else 0))
    soft_cost = 400 + duration_cost + internal_pause_cost + boundary_cost
    excess = (overflow * 1000 + max(0, duration - max_ms) + max(0, round((cps - config.max_cps) * 1000))
              + duration_shortfall + gap_shortfall)
    return {"start_ms": chosen[0].start_ms, "end_ms": chosen[-1].end_ms,
        "original_text": original, "lines": lines, "text": "\n".join(lines),
        "line_widths": [measure_width(line, config.width_mode) for line in lines],
        "speaking_rate": speaking_rate,
        "unit_ids": [u.id for u in chosen], "word_ids": [w for u in chosen for w in u.word_ids],
        "fallback": any(u.fallback for u in chosen), "violations": violations,
        "timing": {"basis": "effective_source_units", "source_start_ms": chosen[0].start_ms,
                   "source_end_ms": chosen[-1].end_ms, "start_delta_ms": 0, "end_delta_ms": 0,
                   "duration_delta_ms": 0},
        "cost": {"soft": soft_cost, "duration": duration_cost,
                 "internal_pause": internal_pause_cost, "boundary": boundary_cost},
        "score": [len(violations), excess, soft_cost]}


def _atomic_stops(normalized):
    """Keep ASR word boundaries inside one grapheme out of the candidate graph."""
    units = normalized.units
    points = grapheme_unit_stops([u.text for u in units])
    for a, b in zip(points, points[1:]):
        if b - a <= 1:
            continue
        group = units[a:b]
        if any(u.fallback for u in group) or any(
                u.source.get("segment_id") != group[0].source.get("segment_id") for u in group):
            raise InputError("grapheme crosses a source segment/fallback boundary; cannot preserve segment boundaries")
        normalized.diagnostics.append({"code": "grapheme_boundary_join",
            "unit_ids": [u.id for u in group],
            "reason": "ASR word boundary inside an extended grapheme; keep these units in one cue"})
    return points


def partition(normalized: Normalized, config: Config, algorithm="dp", group_words=3):
    units = normalized.units
    points = _atomic_stops(normalized)
    n = len(points) - 1
    # Atomic groups retain the original unit IDs, strings, spans and timestamps.
    first = [units[points[i]] for i in range(n)]
    last = [units[points[i + 1] - 1] for i in range(n)]
    if algorithm == "fixed":
        if isinstance(group_words, bool) or not isinstance(group_words, int) or group_words < 1:
            raise InputError("group_words must be a positive integer")
        spans = []
        i = 0
        while i < n:
            j = i + 1
            if not first[i].fallback:
                segment = first[i].source.get("segment_id")
                while (j < n and points[j] < points[i] + group_words
                       and not first[j].fallback and first[j].source.get("segment_id") == segment):
                    j += 1
            spans.append((i, j))
            i = j
    elif algorithm == "dp":
        # A lower bound excludes spaces trimmed from each atomic group's edges.
        # Counting raw code points here would incorrectly prune display-width
        # candidates containing many combining marks or emoji sequences.
        lower_widths = [measure_width(display_text("".join(u.text for u in units[a:b])), config.width_mode)
                        for a, b in zip(points, points[1:])]
        best = [None] * (n + 1)
        best[0] = (0, 0, 0)
        predecessors = [None] * (n + 1)
        for i in range(n):
            if best[i] is None:
                continue
            text_width = 0
            for j in range(i + 1, n + 1):
                if j > i + 1 and (first[i].fallback or last[j - 1].fallback or
                        first[i].source.get("segment_id") != last[j - 1].source.get("segment_id")):
                    break
                text_width += lower_widths[j - 1]
                if j > i + 1 and (text_width > 2 * config.max_chars + 1 or
                        last[j - 1].end_ms - first[i].start_ms > round(config.max_duration * 1000)):
                    break
                cue = _candidate(units, points[i], points[j], config)
                # Relax width/duration only for one indivisible atomic group.
                if j > i + 1 and any(v["code"] in ("line_width", "duration") for v in cue["violations"]):
                    continue
                score = tuple(a + b for a, b in zip(best[i], cue["score"]))
                if best[j] is None or score < best[j]:
                    best[j] = score
                    predecessors[j] = i
        spans = []
        stop = n
        while stop:
            start = predecessors[stop]
            if start is None:
                raise InputError("no segmentation path; internal invariant failed")
            spans.append((start, stop))
            stop = start
        spans.reverse()
    else:
        raise InputError(f"unknown algorithm: {algorithm}")
    cues = [_candidate(units, points[a], points[b], config) for a, b in spans]
    for index, cue in enumerate(cues, 1):
        cue["id"] = index
    return cues


def reflow(data, config=None, *, algorithm="dp", group_words=3):
    from .normalize import normalize
    normalized = normalize(data)
    config = config or Config()
    cues = partition(normalized, config, algorithm, group_words)
    violations = [dict(v, cue_id=c["id"], unit_ids=c["unit_ids"]) for c in cues for v in c["violations"]]
    source_word_ids = [w["id"] for w in normalized.words]
    mapped_word_ids = [w for c in cues for w in c["word_ids"]]
    source_unit_ids = [u.id for u in normalized.units]
    mapped_unit_ids = [u for c in cues for u in c["unit_ids"]]
    if source_word_ids != mapped_word_ids or source_unit_ids != mapped_unit_ids:
        raise InputError("internal token conservation check failed")
    return {"format_version": 1, "tool": "wordcue-reflow", "algorithm": algorithm,
        "baseline_group_words": group_words if algorithm == "fixed" else None,
        "schema": normalized.schema, "config": asdict(config),
        "feasible": not violations,
        "constraint_policy": "No zero-violation partition exists in the documented candidate graph when feasible=false; individual violations may trade off. Fallback units and words joined across grapheme boundaries are indivisible; width/duration relaxation is limited to one such atomic group.",
        "timing_policy": "Cue start/end equal first/last effective source-unit times; no profile retiming or extension. Normalization timing_repair diagnostics retain original bounds and signed deviations. Minimum gaps apply between cues, including across source segments; no gap is required after the final cue.",
        "diagnostics": normalized.diagnostics,
        "constraints": violations, "normalized_words": normalized.words,
        "source_segments": normalized.segments, "source_text": normalized.top_level_text,
        "source_units": [asdict(u) for u in normalized.units], "cues": cues,
        "objective": [sum(c["score"][i] for c in cues) for i in range(3)],
        "metrics": {"cue_count": len(cues), "constraint_violations": len(violations),
            "pause_boundary_penalty": sum(c["cost"]["internal_pause"] + c["cost"]["boundary"] for c in cues),
            "source_words": len(source_word_ids), "mapped_words": len(mapped_word_ids),
            "source_units": len(source_unit_ids), "mapped_units": len(mapped_unit_ids),
            "tokens_conserved": True, "original_text_conserved":
                "".join(u.text for u in normalized.units) == "".join(c["original_text"] for c in cues)}}
