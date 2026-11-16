"""Strict interchange validation and explainable, deterministic evidence rules."""

from copy import deepcopy
from dataclasses import asdict, dataclass
from fractions import Fraction
import json
import math
import unicodedata

from . import __version__


class AuditError(ValueError):
    """Invalid or unsupported input; never interpreted as silence."""


def _number(value, location):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditError(f"{location}: expected a finite number in seconds (scores are unitless)")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise AuditError(f"{location}: nonfinite or out-of-range number")
    return value


def _json_value(value, location="input"):
    """Reject non-JSON library inputs and nonfinite metadata, too."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        _number(value, location)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(item, f"{location}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError(f"{location}: JSON keys must be strings")
            _json_value(item, f"{location}.{key}")
    else:
        raise AuditError(f"{location}: unsupported JSON value {type(value).__name__}")


def parse_json(data):
    """Parse UTF-8 JSON, rejecting duplicate keys, NaN, Infinity and overflow."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AuditError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        # Defer rejection to the recursive validator so even non-standard
        # NaN/Infinity constants receive a concrete path in the error.
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=float)
        _json_value(result)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"invalid UTF-8 JSON: {exc}") from exc


def validate_transcript(document):
    """Whisper-style seconds only. Numeric bad timing is evidence, not discarded."""
    _json_value(document)
    if not isinstance(document, dict) or not isinstance(document.get("segments"), list):
        raise AuditError("input.segments: expected transcript object with segments array; native whisper.cpp requires explicit input_format='whisper.cpp-json-v1.7.6' / --input-format")
    if "transcription" in document:
        raise AuditError("input.transcription: native whisper.cpp requires explicit input_format='whisper.cpp-json-v1.7.6' / --input-format; mixed schemas are unsupported")
    if document.get("time_unit", "seconds") != "seconds":
        raise AuditError("time_unit must be 'seconds'")
    for i, segment in enumerate(document["segments"]):
        loc = f"segments[{i}]"
        if not isinstance(segment, dict):
            raise AuditError(f"{loc}: expected object")
        for key in ("start", "end", "text"):
            if key not in segment:
                raise AuditError(f"{loc}: missing {key}")
        _number(segment["start"], f"{loc}.start")
        _number(segment["end"], f"{loc}.end")
        if not isinstance(segment["text"], str):
            raise AuditError(f"{loc}.text: expected string")
        for key in ("no_speech_prob", "avg_logprob"):
            if segment.get(key) is not None:
                score = _number(segment[key], f"{loc}.{key}")
                if key == "no_speech_prob" and not 0 <= score <= 1:
                    raise AuditError(f"{loc}.{key}: expected probability in [0, 1]")
        # Extra upstream fields, including words, are retained verbatim, not used
        # as alternative timing/scores or silently substituted for segment text.
    return document


def normalize_intervals(intervals):
    if not isinstance(intervals, list):
        raise AuditError("VAD must be an array of [start, end] pairs in seconds")
    ordered = []
    for i, pair in enumerate(intervals):
        if not isinstance(pair, list) or len(pair) != 2:
            raise AuditError(f"VAD[{i}]: expected [start, end]")
        start, end = (_number(pair[0], f"VAD[{i}].start"), _number(pair[1], f"VAD[{i}].end"))
        if start < 0 or end <= start:
            raise AuditError(f"VAD[{i}]: require 0 <= start < end")
        ordered.append([start, end])
    merged = []
    for start, end in sorted(ordered):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


@dataclass(frozen=True)
class Thresholds:
    min_repeat_segments: int = 2
    repeat_max_gap_seconds: float = 1.0
    max_chars_per_second: float = 25.0
    high_no_speech_prob: float = 0.8
    low_avg_logprob: float = -1.0
    outside_speech_fraction: float = 0.8
    coalesce_gap_seconds: float = 0.25

    def __post_init__(self):
        if (isinstance(self.min_repeat_segments, bool)
                or not isinstance(self.min_repeat_segments, int)
                or self.min_repeat_segments < 2):
            raise AuditError("min_repeat_segments must be an integer >= 2")
        for key, value in asdict(self).items():
            _number(value, f"thresholds.{key}")
        for key in ("repeat_max_gap_seconds", "coalesce_gap_seconds"):
            if getattr(self, key) < 0:
                raise AuditError(f"{key} must be >= 0")
        if self.max_chars_per_second <= 0:
            raise AuditError("max_chars_per_second must be > 0")
        for key in ("high_no_speech_prob", "outside_speech_fraction"):
            if not 0 <= getattr(self, key) <= 1:
                raise AuditError(f"{key} must be in [0, 1]")


def _valid_time(segment):
    return 0 <= segment["start"] < segment["end"]


def _phrase(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _repeat_runs(segments, thresholds):
    runs = []
    current = []

    def finish():
        if len(current) >= thresholds.min_repeat_segments:
            runs.append({"segments": current[:], "normalized_phrase": _phrase(segments[current[0]]["text"])})

    for i, segment in enumerate(segments):
        phrase = _phrase(segment["text"])
        if not _valid_time(segment) or not phrase:
            finish()
            current = []
            continue
        previous = segments[current[-1]] if current else None
        if (previous is not None and phrase == _phrase(previous["text"])
                and segment["start"] >= previous["start"]
                and segment["start"] - previous["end"] <= thresholds.repeat_max_gap_seconds):
            current.append(i)
        else:
            finish()
            current = [i]
    finish()
    return runs


def _outside_fraction(start, end, intervals):
    # Treat the shortest decimal representation of each parsed number as exact.
    # Compare the rational result before rounding for JSON/CSV; no tolerance
    # should promote a genuinely below-threshold fraction into a review flag.
    start, end = Fraction(str(start)), Fraction(str(end))
    overlap = sum(max(0, min(end, right) - max(start, left)) for left, right in intervals)
    duration = end - start
    return (duration - overlap) / duration


def _ranges(flags, gap):
    result = []
    for flag in sorted((f for f in flags if f["timestamp_valid"]), key=lambda f: (f["start"], f["end"], f["segment"])):
        if result and flag["start"] - result[-1]["end"] <= gap:
            item = result[-1]
            item["end"] = max(item["end"], flag["end"])
            item["segments"].append(flag["segment"])
            item["reasons"] = sorted(set(item["reasons"]) | set(flag["reasons"]))
        else:
            result.append({"start": flag["start"], "end": flag["end"],
                           "segments": [flag["segment"]], "reasons": sorted(flag["reasons"])})
    return result


def audit(document, speech_intervals=None, thresholds=None, *, input_format="segments", loop_config=None):
    """Return all segment evidence without mutating input; null means unknown.

    Missing VAD (None) differs from explicitly supplied empty speech intervals.
    Flags are review prompts, never classifications of hallucination or truth.
    Native whisper.cpp conversion requires explicit input_format selection.
    loop_config supplies partial default/languages rules for intra-segment loops.
    """
    from .loops import POLICY, find_loops, loop_settings, normalize_loop_config

    resolved_loop_config = normalize_loop_config(loop_config)
    source_document = document
    conversion = None
    if input_format == "whisper.cpp-json-v1.7.6":
        from .adapters import TOKEN_POLICY, adapt_whisper_cpp, token_evidence
        document, conversion = adapt_whisper_cpp(document)
    elif input_format != "segments":
        raise AuditError(f"input_format: unsupported format {input_format!r}")
    validate_transcript(document)
    config = thresholds if thresholds is not None else Thresholds()
    if not isinstance(config, Thresholds):
        raise AuditError("thresholds must be a Thresholds instance")
    intervals = None if speech_intervals is None else normalize_intervals(speech_intervals)
    exact_intervals = None if intervals is None else [
        [Fraction(str(left)), Fraction(str(right))] for left, right in intervals]
    outside_threshold = Fraction(str(config.outside_speech_fraction))
    segments = document["segments"]
    runs = _repeat_runs(segments, config)
    repeated = {i for run in runs for i in run["segments"]}
    evidence = []
    flags = []
    corroborated = []
    for i, segment in enumerate(segments):
        start, end, text = segment["start"], segment["end"], segment["text"]
        valid = _valid_time(segment)
        # Finite endpoints can still overflow when subtracting. Reject rather
        # than serialize Infinity or silently treat the resulting rate as zero.
        duration = end - start if valid else None
        if duration is not None:
            _number(duration, f"segments[{i}].duration")
        chars = sum(not c.isspace() for c in text)
        rate = chars / duration if valid else None
        if rate is not None:
            _number(rate, f"segments[{i}].chars_per_second")
        outside = _outside_fraction(start, end, exact_intervals) if valid and intervals is not None else None
        settings = loop_settings(document, segment, resolved_loop_config)
        loops, loop_search = find_loops(text, settings)
        reasons = []
        if not valid:
            reasons.append("invalid_timestamp")
        if i in repeated:
            reasons.append("adjacent_repeat")
        if loops:
            reasons.append("intra_segment_loop")
        if rate is not None and rate > config.max_chars_per_second:
            reasons.append("excessive_character_rate")
        if segment.get("no_speech_prob") is not None and segment["no_speech_prob"] >= config.high_no_speech_prob:
            reasons.append("high_no_speech")
        if segment.get("avg_logprob") is not None and segment["avg_logprob"] <= config.low_avg_logprob:
            reasons.append("low_avg_logprob")
        if outside is not None and outside > 0 and outside >= outside_threshold:
            reasons.append("outside_speech")
        supported_repeat = i in repeated and "outside_speech" in reasons
        if supported_repeat:
            corroborated.append(i)
        item = {"segment": i, "start": start, "end": end, "text": text,
               "loop_settings": settings, "loop_search": loop_search, "intra_segment_loops": loops,
                "timestamp_valid": valid, "duration_seconds": duration,
                "character_count": chars, "chars_per_second": rate,
                "no_speech_prob": segment.get("no_speech_prob"),
                "avg_logprob": segment.get("avg_logprob"),
                "outside_speech_fraction": float(outside) if outside is not None else None,
                "speech_corroborated_repeat": supported_repeat if intervals is not None and valid else None,
                "reasons": reasons}
        evidence.append(item)
        if reasons:
            flag = deepcopy(item)
            flag["token_evidence"] = (token_evidence(source_document["transcription"][i], f"transcription[{i}]")
                                      if conversion is not None else None)
            flags.append(flag)
    limited_indices = [item["segment"] for item in evidence if item["loop_search"]["limited_by_config"]]
    report = {"schema_version": 1, "tool_version": __version__, "time_unit": "seconds",
            "loop_config": resolved_loop_config, "loop_policy": dict(POLICY),
            "loop_search_summary": {
                "total_segments": len(evidence), "limited_segments": len(limited_indices),
                "limited_segment_indices": limited_indices,
                "max_searched_pattern_tokens": max(
                    (item["loop_search"]["searched_max_pattern_tokens"] for item in evidence), default=0)},
            "thresholds": asdict(config), "unchanged_source": True,
            "source_document": deepcopy(source_document), "speech_intervals": intervals,
            "segments": evidence, "flags": flags, "repeat_runs": runs,
            "speech_corroborated_repeats": corroborated,
            "review_ranges": _ranges(flags, config.coalesce_gap_seconds),
            "unplaced_segments": [f["segment"] for f in flags if not f["timestamp_valid"]]}
    views = [flag["token_evidence"] for flag in flags]
    unavailable_indices = [flag["segment"] for flag in flags if flag["token_evidence"] is None]
    empty_indices = [flag["segment"] for flag in flags
                     if flag["token_evidence"] is not None and not flag["token_evidence"]["tokens"]]
    unavailable, empty = len(unavailable_indices), len(empty_indices)
    token_rows = sum(len(view["tokens"]) for view in views if view is not None)
    # Native views are already validated: timing is a supplied pair or absent.
    # Presence alone counts, including zero/reversed/out-of-segment intervals.
    timed_rows = sum("offsets" in entry["token"] and "timestamps" in entry["token"]
                     for view in views if view is not None for entry in view["tokens"])
    timed_paths = [entry["source_path"]
                   for view in views if view is not None for entry in view["tokens"]
                   if "offsets" in entry["token"] and "timestamps" in entry["token"]]
    untimed_paths = [entry["source_path"]
                     for view in views if view is not None for entry in view["tokens"]
                     if "offsets" not in entry["token"] and "timestamps" not in entry["token"]]
    untimed_only_indices = [flag["segment"] for flag in flags
                            if (view := flag["token_evidence"]) is not None and view["tokens"]
                            and not any("offsets" in entry["token"] and "timestamps" in entry["token"]
                                        for entry in view["tokens"])]
    mixed_timing_indices = [flag["segment"] for flag in flags
                            if (view := flag["token_evidence"]) is not None
                            and 0 < sum("offsets" in entry["token"] and "timestamps" in entry["token"]
                                        for entry in view["tokens"]) < len(view["tokens"])]
    timed_only_indices = [flag["segment"] for flag in flags
                          if (view := flag["token_evidence"]) is not None and view["tokens"]
                          and all("offsets" in entry["token"] and "timestamps" in entry["token"]
                                  for entry in view["tokens"])]
    segment_token_counts = []
    for flag in flags:
        view = flag["token_evidence"]
        if view is None or not view["tokens"]:
            continue
        rows = len(view["tokens"])
        timed = sum("offsets" in entry["token"] and "timestamps" in entry["token"]
                    for entry in view["tokens"])
        segment_token_counts.append({"segment": flag["segment"], "token_rows": rows,
                                     "timed_token_rows": timed, "untimed_token_rows": rows - timed})
    report["token_evidence_summary"] = {
        "flagged_segments": len(flags), "unavailable_segments": unavailable,
        "empty_segments": empty, "segments_with_tokens": len(flags) - unavailable - empty,
        "token_rows": token_rows, "timed_token_rows": timed_rows,
        "untimed_token_rows": token_rows - timed_rows,
        "unavailable_segment_indices": unavailable_indices, "empty_segment_indices": empty_indices,
        "untimed_only_segment_indices": untimed_only_indices,
        "mixed_timing_segment_indices": mixed_timing_indices,
        "timed_only_segment_indices": timed_only_indices,
        "segment_token_counts": segment_token_counts,
        "untimed_token_source_paths": untimed_paths,
        "timed_token_source_paths": timed_paths}
    if conversion is not None:
        report.update(input_format=input_format, normalized_document=deepcopy(document), conversion=conversion,
                      token_evidence_policy=dict(TOKEN_POLICY))
    return report
