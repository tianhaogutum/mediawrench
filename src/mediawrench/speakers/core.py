"""Original interval arithmetic and conservative transcript normalization."""

from bisect import bisect_right
from dataclasses import dataclass
import copy
import json
import math
from typing import Any


class BridgeError(ValueError):
    """An unsupported or invalid input; callers must not silently skip it."""


RESERVED = {"UNKNOWN", "AMBIGUOUS"}


def number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise BridgeError(f"{where}: expected a finite number in seconds")
    try:
        result = float(value)
    except OverflowError as exc:
        raise BridgeError(f"{where}: timestamp is too large") from exc
    if not math.isfinite(result) or result < 0:
        raise BridgeError(f"{where}: expected a finite nonnegative number")
    return result


def bounds(obj: dict, where: str) -> tuple[float, float]:
    start = number(obj.get("start"), f"{where}.start")
    end = number(obj.get("end"), f"{where}.end")
    if end < start:
        raise BridgeError(f"{where}: end precedes start")
    return start, end


def units(obj: dict, where: str) -> None:
    for key in ("time_unit", "time_units"):
        if key in obj and obj[key] not in ("s", "seconds"):
            raise BridgeError(f"{where}.{key}: only seconds are supported; convert units explicitly")


def label(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BridgeError(f"{where}: expected a nonempty label without edge whitespace")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BridgeError(f"{where}: control characters are not allowed")
    if value in RESERVED:
        raise BridgeError(f"{where}: {value} is reserved")
    return value


def _pairs(pairs: list) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise BridgeError(f"duplicate JSON key: {key!r}")
        obj[key] = value
    return obj


def load_json(text: str) -> Any:
    def bad_constant(value: str) -> None:
        raise BridgeError(f"nonfinite JSON number: {value}")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=bad_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError(f"invalid JSON: {exc}") from exc


def union(intervals: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass(frozen=True)
class Diarization:
    recording: str
    channel: str
    intervals: dict[str, tuple[tuple[float, float], ...]]
    source_records: tuple[dict, ...]


def _signed_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise BridgeError(f"{where}: expected a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise BridgeError(f"{where}: number is too large") from exc
    if not math.isfinite(result):
        raise BridgeError(f"{where}: expected a finite number")
    return result


def _validate_merge_options(*, min_coverage: float = 0.5, tie_epsilon: float = 1e-6,
                            max_gap: float = 1.0, rttm_offset_seconds: float = 0.0,
                            rttm_drift_ppm: float = 0.0, recording_duration: float | None = None,
                            rttm_out_of_range_policy: str = "error") -> tuple[float, float, float, dict]:
    """Validate input-independent configuration; infer nothing from entry files.

    Shared by merge and opt-in manifest validation. Keep rule order and error
    messages stable so processing retains its existing failure precedence.
    """
    min_coverage = number(min_coverage, "min_coverage")
    tie_epsilon = number(tie_epsilon, "tie_epsilon")
    max_gap = number(max_gap, "max_gap")
    if min_coverage > 1 or tie_epsilon > 1:
        raise BridgeError("min_coverage and tie_epsilon must be between 0 and 1")
    offset, ppm = rttm_offset_seconds, rttm_drift_ppm
    duration, policy = recording_duration, rttm_out_of_range_policy
    if policy not in ("error", "clip"):
        raise BridgeError("rttm_out_of_range_policy must be 'error' or 'clip'")
    offset = _signed_number(offset, "rttm_offset_seconds")
    ppm = _signed_number(ppm, "rttm_drift_ppm")
    scale = 1 + ppm / 1000000
    if scale <= 0:
        raise BridgeError("rttm_drift_ppm must be greater than -1000000; "
                          "scale must be positive to avoid reversed or collapsed intervals")
    if duration is not None:
        duration = number(duration, "recording_duration")
        if duration == 0:
            raise BridgeError("recording_duration must be positive")
    elif policy == "clip":
        raise BridgeError("recording_duration is required for rttm_out_of_range_policy='clip'")
    correction = dict(offset_seconds=offset, drift_ppm=ppm, scale=scale,
                      formula="corrected = original * (1 + drift_ppm / 1000000) + offset_seconds",
                      direction="RTTM to ASR", recording_duration=duration,
                      out_of_range_policy=policy)
    return min_coverage, tie_epsilon, max_gap, correction


def _correct_rttm(diarization: Diarization, correction: dict) -> tuple[Diarization, list[dict], dict]:
    """Transform selected source rows once, then union on the ASR clock."""
    offset, scale = correction["offset_seconds"], correction["scale"]
    duration, policy = correction["recording_duration"], correction["out_of_range_policy"]
    corrected = []
    speakers: dict[str, list] = {}
    for record in diarization.source_records:
        start = record["start"] * scale + offset
        end = record["end"] * scale + offset
        where = (f"RTTM line {record['line']}: original [{record['start']}, {record['end']}] "
                 f"-> corrected [{start}, {end}]")
        if not math.isfinite(start) or not math.isfinite(end):
            raise BridgeError(f"{where}: nonfinite corrected time")
        if policy == "error" and (start < 0 or end < 0):
            raise BridgeError(f"{where}: negative corrected time; no clipping is performed")
        if end <= start:
            raise BridgeError(f"{where}: reversed or collapsed corrected interval")
        if policy == "error" and duration is not None and end > duration:
            raise BridgeError(f"{where}: outside recording range [0, {duration}]; no clipping is performed")
        unclipped_start, unclipped_end = start, end
        status = "unchanged"
        diagnostics = []
        if policy == "clip":
            start = max(0.0, min(duration, start))
            end = max(0.0, min(duration, end))
            if end == start:
                status = "dropped"
            elif (start, end) != (unclipped_start, unclipped_end):
                status = "clipped"
            if status != "unchanged":
                action = "excluded from assignment (empty intersection)" if status == "dropped" else "used for assignment"
                diagnostics.append(f"{where} -> effective [{start}, {end}]: {status} "
                                   f"to recording range [0, {duration}]; {action}")
        corrected.append(dict(record, original_start=record["start"], original_end=record["end"],
                              unclipped_start=unclipped_start, unclipped_end=unclipped_end,
                              start=start, end=end, clipping_status=status, diagnostics=diagnostics))
        # Preserve the selected speaker vocabulary (and valid aliases), even when
        # clipping removes every interval for a speaker. Empty spans add no evidence.
        spans = speakers.setdefault(record["speaker"], [])
        if end > start:
            spans.append((start, end))
    result = Diarization(diarization.recording, diarization.channel,
                         {s: union(v) for s, v in sorted(speakers.items())}, diarization.source_records)
    return result, corrected, correction


def parse_rttm(text: str, recording: str | None = None, channel: str | None = None) -> Diarization:
    """Parse ten-field SPEAKER lines; explicitly select a recording/channel."""
    records = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 10 or fields[0] != "SPEAKER":
            raise BridgeError(f"RTTM line {lineno}: expected a ten-field SPEAKER record")
        _, file_id, ch, raw_start, raw_duration, _, _, speaker, _, _ = fields
        label(speaker, f"RTTM line {lineno} speaker")
        if not ch.isascii() or not ch.isdigit() or not ch.strip("0"):
            raise BridgeError(f"RTTM line {lineno}: channel must be a positive integer")
        try:
            start = number(float(raw_start), f"RTTM line {lineno} start")
            duration = number(float(raw_duration), f"RTTM line {lineno} duration")
        except ValueError as exc:
            raise BridgeError(f"RTTM line {lineno}: invalid seconds ({exc})") from exc
        end = start + duration
        if duration <= 0 or not math.isfinite(end) or end <= start:
            raise BridgeError(f"RTTM line {lineno}: duration must give a finite positive interval")
        records.append(dict(recording=file_id, channel=ch.lstrip("0"), start=start,
                            end=end, speaker=speaker, line=lineno))
    if not records:
        raise BridgeError("RTTM contains no SPEAKER records")
    recordings = {r["recording"] for r in records}
    if recording is None:
        if len(recordings) != 1:
            raise BridgeError("RTTM has multiple recordings; use --recording")
        recording = next(iter(recordings))
    if recording not in recordings:
        raise BridgeError(f"RTTM recording not found: {recording}")
    selected = [r for r in records if r["recording"] == recording]
    channels = {r["channel"] for r in selected}
    if channel is None:
        if len(channels) != 1:
            raise BridgeError("RTTM has multiple channels; use --channel")
        channel = next(iter(channels))
    if channel not in channels:
        raise BridgeError(f"RTTM channel not found: {channel}")
    selected = [r for r in selected if r["channel"] == channel]
    speakers: dict[str, list] = {}
    for record in selected:
        speakers.setdefault(record["speaker"], []).append((record["start"], record["end"]))
    return Diarization(recording, channel,
                       {s: union(v) for s, v in sorted(speakers.items())}, tuple(selected))


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise BridgeError(f"{where}: expected an object")
    units(value, where)
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{where}: expected nonempty text")
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in value):
        raise BridgeError(f"{where}: unsupported control characters")
    return value


def normalize(asr: dict) -> tuple[list[dict], list[str]]:
    """Keep source order and exact segment text; gaps become segment-precision items."""
    _object(asr, "ASR")
    warnings = []
    if "segments" in asr:
        segments = asr["segments"]
        if "words" in asr:
            raise BridgeError("ASR: choose segments or root words, not both")
        # WhisperX's word_segments is a redundant flattened alignment view.
        if "word_segments" in asr:
            warnings.append("word_segments retained in source_asr; segments is the authoritative view")
    else:
        keys = [k for k in ("words", "word_segments") if k in asr]
        if len(keys) != 1:
            raise BridgeError("ASR: expected segments, words, or word_segments")
        segments = [dict(words=asr[keys[0]])]
    if not isinstance(segments, list) or not segments:
        raise BridgeError("ASR: segments must be a nonempty array")
    items = []
    source_word = 0
    previous_segment = -1.0
    for si, raw_segment in enumerate(segments):
        segment = _object(raw_segment, f"segment {si}")
        has_bounds = "start" in segment or "end" in segment
        span = bounds(segment, f"segment {si}") if has_bounds else None
        if span is not None:
            if span[0] < previous_segment:
                raise BridgeError("ASR: segments must be ordered by start time")
            previous_segment = span[0]
        text = segment.get("text")
        if text is not None:
            _text(text, f"segment {si}.text")
        raw_words = segment.get("words", [])
        if not isinstance(raw_words, list):
            raise BridgeError(f"segment {si}.words: expected an array")
        words = []
        previous_word = -1.0
        for wi, raw_word in enumerate(raw_words):
            word = _object(raw_word, f"segment {si} word {wi}")
            word_text = _text(word.get("word"), f"segment {si} word {wi}.word")
            for key in ("start", "end"):
                if key in word and word[key] is not None:
                    boundary = number(word[key], f"segment {si} word {wi}.{key}")
                    if span and (boundary < span[0] - 1e-9 or boundary > span[1] + 1e-9):
                        raise BridgeError(f"segment {si} word {wi}: timing outside segment; check units")
            timed = word.get("start") is not None and word.get("end") is not None
            word_span = bounds(word, f"segment {si} word {wi}") if timed else span
            if word_span is None:
                raise BridgeError(f"segment {si} word {wi}: missing timing and no segment fallback")
            if timed:
                if word_span[0] < previous_word:
                    raise BridgeError(f"segment {si}: words must be ordered by start time")
                previous_word = word_span[0]
                if span and (word_span[0] < span[0] - 1e-9 or word_span[1] > span[1] + 1e-9):
                    raise BridgeError(f"segment {si} word {wi}: timing outside segment; check units")
            words.append(dict(word=word_text, text=word_text, start=word_span[0], end=word_span[1],
                              precision="word" if timed else "segment", source_segment=si,
                              source_word=source_word, source_word_in_segment=wi))
            source_word += 1
        if not words:
            if text is None or span is None:
                raise BridgeError(f"segment {si}: text and segment timing required when words are absent")
            items.append(_residual(text, span, si))
            continue
        if text is None:
            # Word-only exports have no authoritative whitespace; keep word fields
            # exactly, and insert a separator only between bare word strings.
            for wi, word in enumerate(words):
                if wi and not words[wi - 1]["text"][-1].isspace() and not word["text"][0].isspace():
                    word["text"] = " " + word["text"]
            items.extend(words)
            continue
        # Match ignoring whitespace only (never spelling/punctuation); each source
        # word must appear monotonically in segment.text, otherwise fail explicitly.
        positions = [i for i, char in enumerate(text) if not char.isspace()]
        compact = "".join(text[i] for i in positions)
        cursor = 0
        char_cursor = 0
        segment_items = []
        for word in words:
            needle = "".join(word["word"].split())
            found = compact.find(needle, cursor)
            if found < 0:
                raise BridgeError(f"segment {si}: word {word['source_word_in_segment']} does not match text in order")
            first = positions[found]
            last = positions[found + len(needle) - 1] + 1
            gap = text[char_cursor:first]
            if gap.strip():
                if span is None:
                    raise BridgeError(f"segment {si}: unaligned text needs segment timing")
                segment_items.append(_residual(gap, span, si))
                word["text"] = text[first:last]
            else:
                word["text"] = text[char_cursor:last]
            segment_items.append(word)
            char_cursor, cursor = last, found + len(needle)
        suffix = text[char_cursor:]
        if suffix.strip():
            if span is None:
                raise BridgeError(f"segment {si}: unaligned text needs segment timing")
            segment_items.append(_residual(suffix, span, si))
        else:
            segment_items[-1]["text"] += suffix
        items.extend(segment_items)
    if "text" in asr:
        transcript = _text(asr["text"], "ASR.text")
        if "".join(transcript.split()) != "".join("".join(i["text"] for i in items).split()):
            raise BridgeError("ASR.text differs from segment/word text; cannot preserve unlocated transcript text")
    for i, item in enumerate(items):
        item["id"] = i
    fallback = sum(i["precision"] == "segment" for i in items)
    if fallback:
        warnings.append(f"{fallback} item(s) use segment timing; this is coarse attribution, not word alignment")
    return items, warnings


def _residual(text: str, span: tuple[float, float], si: int) -> dict:
    return dict(word=text, text=text, start=span[0], end=span[1], precision="segment",
                source_segment=si, source_word=None, source_word_in_segment=None)


class OverlapIndex:
    """Bisect unioned intervals so distant intervals need not be rescanned per word."""

    def __init__(self, diarization: Diarization):
        self.intervals = diarization.intervals
        self.ends = {s: tuple(b for _, b in spans) for s, spans in self.intervals.items()}

    def evidence(self, start: float, end: float) -> tuple[list[dict], bool]:
        evidence = []
        events = []
        for speaker, intervals in self.intervals.items():
            clips = []
            index = bisect_right(self.ends[speaker], start)
            while index < len(intervals) and intervals[index][0] < end:
                a, b = intervals[index]
                left, right = max(start, a), min(end, b)
                if right > left:
                    clips.append([left, right])
                    events.extend(((left, 1), (right, -1)))
                index += 1
            if clips:
                overlap = math.fsum(b - a for a, b in clips)
                evidence.append(dict(speaker=speaker, overlap=overlap,
                                     coverage=min(1.0, overlap / (end - start)), intervals=clips))
        active = 0
        simultaneous = False
        for _, delta in sorted(events):  # End before start: touching is not overlap.
            active += delta
            simultaneous |= active > 1
        return evidence, simultaneous


def merge(asr: dict, diarization: Diarization, *, min_coverage: float = 0.5,
          tie_epsilon: float = 1e-6, max_gap: float = 1.0,
          aliases: dict[str, str] | None = None, rttm_offset_seconds: float = 0.0,
          rttm_drift_ppm: float = 0.0, recording_duration: float | None = None,
          rttm_out_of_range_policy: str = "error") -> dict:
    """Join on the ASR clock with an explicit, never inferred RTTM affine correction.

    corrected = original * (1 + rttm_drift_ppm / 1000000) + rttm_offset_seconds.
    Invalid corrected intervals fail. The default 'error' policy rejects bounds
    outside the recording; 'clip' requires recording_duration and retains all
    rows in the audit, including empty intersections excluded from assignment.
    ASR items must stay in range under either policy and are never transformed.
    """
    min_coverage, tie_epsilon, max_gap, correction = _validate_merge_options(
        min_coverage=min_coverage, tie_epsilon=tie_epsilon, max_gap=max_gap,
        rttm_offset_seconds=rttm_offset_seconds, rttm_drift_ppm=rttm_drift_ppm,
        recording_duration=recording_duration, rttm_out_of_range_policy=rttm_out_of_range_policy)
    diarization, corrected_rttm, correction = _correct_rttm(diarization, correction)
    aliases = {} if aliases is None else aliases
    if not isinstance(aliases, dict):
        raise BridgeError("aliases must be an object mapping speaker IDs to display names")
    for speaker, alias in aliases.items():
        if speaker not in diarization.intervals:
            raise BridgeError(f"alias references unknown speaker: {speaker}")
        label(alias, f"alias {speaker}")
    displays = {s: aliases.get(s, s) for s in diarization.intervals}
    if len(set(displays.values())) != len(displays):
        raise BridgeError("duplicate aliases or alias colliding with an unchanged speaker ID")
    items, warnings = normalize(asr)
    warnings.extend(diagnostic for row in corrected_rttm for diagnostic in row["diagnostics"])
    index = OverlapIndex(diarization)
    for item in items:
        duration = correction["recording_duration"]
        if duration is not None and item["end"] > duration:
            raise BridgeError(f"ASR item {item['id']} (segment {item['source_segment']}, "
                              f"word {item['source_word_in_segment']}): "
                              f"[{item['start']}, {item['end']}] outside recording range [0, {duration}]")
        evidence, simultaneous = index.evidence(item["start"], item["end"])
        best = max((e["coverage"] for e in evidence), default=0.0)
        candidates = [e["speaker"] for e in evidence if best - e["coverage"] <= tie_epsilon]
        if item["start"] == item["end"]:
            speaker, reason = "UNKNOWN", "zero_duration"
        elif not evidence:
            speaker, reason = "UNKNOWN", "no_intersection"
        elif best < min_coverage:
            speaker, reason = "UNKNOWN", "insufficient_coverage"
        elif len(candidates) > 1:
            speaker, reason = "AMBIGUOUS", "coverage_tie"
        else:
            speaker, reason = candidates[0], "largest_overlap"
        item.update(speaker=speaker, speaker_label=displays.get(speaker, speaker), coverage=best,
                    reason=reason, candidates=candidates, evidence=evidence,
                    simultaneous_speech=simultaneous)
    turns = group_turns(items, max_gap)
    return dict(schema_version=1, recording=diarization.recording, channel=diarization.channel,
                time_unit="seconds", config=dict(min_coverage=min_coverage, tie_epsilon=tie_epsilon,
                max_gap=max_gap, aliases=dict(aliases), rttm_time_correction=correction),
                items=items, turns=turns, warnings=warnings, corrected_rttm=copy.deepcopy(corrected_rttm),
                source_asr=copy.deepcopy(asr), source_rttm=list(copy.deepcopy(diarization.source_records)),
                unioned_intervals={s: [list(i) for i in spans] for s, spans in diarization.intervals.items()})


def group_turns(items: list[dict], max_gap: float) -> list[dict]:
    turns: list[dict] = []
    for item in items:
        # Keep input order. Never merge across source segments or precision levels:
        # a coarse segment interval must not hide a timed word's boundary.
        if (turns and turns[-1]["speaker"] == item["speaker"]
                and turns[-1]["source_segment"] == item["source_segment"]
                and turns[-1]["precision"] == item["precision"]
                and item["start"] >= turns[-1]["start"]
                and item["start"] <= turns[-1]["end"] + max_gap):
            turn = turns[-1]
            turn["end"] = max(turn["end"], item["end"])
            turn["text"] += item["text"]
            turn["item_ids"].append(item["id"])
        else:
            turns.append({k: item[k] for k in ("start", "end", "speaker", "speaker_label",
                                               "precision", "source_segment")})
            turns[-1].update(text=item["text"], item_ids=[item["id"]])
    return turns
