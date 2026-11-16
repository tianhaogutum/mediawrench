"""Normalize JSON without guessing alignment or silently discarding segment text."""
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any

from .model import InputError, Normalized, Unit
from .layout import grapheme_unit_stops, is_cjk

MAX_MS = 359_999_999  # 99:59:59,999; leave the SRT time range explicit.


def _time(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{path}: time must be a JSON number or null")
    if value < 0 or value > MAX_MS / 1000 or not math.isfinite(value):
        raise InputError(f"{path}: time must be finite, nonnegative and <= {MAX_MS / 1000}")
    return int((Decimal(str(value)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _interval(obj: dict, path: str) -> tuple[int | None, int | None]:
    start = _time(obj.get("start"), path + "/start")
    end = _time(obj.get("end"), path + "/end")
    if obj.get("start") is not None and obj.get("end") is not None and obj["end"] < obj["start"]:
        raise InputError(f"{path}: end precedes start")
    return start, end


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise InputError(f"{path}: expected string")
    # SRT cannot safely carry NUL/control characters or unpaired JSON surrogates.
    if any((ord(c) < 32 and c not in "\t\n\r") or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise InputError(f"{path}: unsupported control character or unpaired surrogate")
    return value


def normalize(data: Any) -> Normalized:
    if not isinstance(data, dict):
        raise InputError("expected JSON object with segments, word_segments, or words")
    present = [key for key in ("segments", "word_segments", "words") if key in data]
    if not present or ("words" in present and len(present) > 1):
        raise InputError("unknown or mixed root schema (words cannot accompany segments/word_segments)")
    # WhisperX commonly includes BOTH segments and a flattened mirror word_segments.
    primary = "segments" if "segments" in present else present[0]
    values = data[primary]
    if not isinstance(values, list):
        raise InputError(f"/{primary}: expected array")
    result = Normalized(primary, [], [], [], [])
    previous_end = 0

    def add_unit(unit_id, text, start, end, word_ids, source, fallback=False):
        nonlocal previous_end
        repaired_start = max(start, previous_end)
        repaired_end = max(end, repaired_start + 1)
        if repaired_end > MAX_MS:
            raise InputError(f"{unit_id}: monotone timing repair exceeds SRT time range")
        if (repaired_start, repaired_end) != (start, end):
            result.diagnostics.append({"code": "timing_repair", "source_id": unit_id,
                "from_ms": [start, end], "to_ms": [repaired_start, repaired_end],
                "delta_ms": [repaired_start - start, repaired_end - end],
                "duration_delta_ms": (repaired_end - repaired_start) - (end - start),
                "reason": "clamp start to previous end; ensure at least 1 ms"})
        result.units.append(Unit(unit_id, text, repaired_start, repaired_end, tuple(word_ids), source, fallback))
        previous_end = repaired_end

    def words_list(raw_words, path):
        if not isinstance(raw_words, list):
            raise InputError(f"{path}: expected array")
        records = []
        for wi, raw in enumerate(raw_words):
            wp = f"{path}/{wi}"
            if not isinstance(raw, dict) or "word" not in raw or "text" in raw or "words" in raw:
                raise InputError(f"{wp}: expected word record with 'word', not mixed text/words schema")
            text = _text(raw["word"], wp + "/word")
            if not text.strip():
                raise InputError(f"{wp}: word must contain non-whitespace text")
            start, end = _interval(raw, wp)
            record = {"id": wp, "original_id": raw.get("id"), "word": text,
                "start": raw.get("start"), "end": raw.get("end"),
                "start_ms": start, "end_ms": end}
            records.append(record)
            result.words.append(record)
        return records

    if primary != "segments":
        records = words_list(values, "/" + primary)
        safe_separators = set(grapheme_unit_stops([r["word"] for r in records]))
        for wi, r in enumerate(records):
            if r["start_ms"] is None or r["end_ms"] is None:
                raise InputError(f"{r['id']}: untimed flat word needs a segment interval; use segments schema")
            # Flat records have no authoritative separator. Preserve supplied whitespace;
            # infer a space only between adjacent non-CJK strings without whitespace.
            text = r["word"]
            if result.units:
                before = result.units[-1].text
                if wi in safe_separators and not before[-1].isspace() and not text[0].isspace():
                    if not is_cjk(before[-1]) and not is_cjk(text[0]):
                        text = " " + text
                        result.diagnostics.append({"code": "inferred_separator", "source_id": r["id"], "separator": " "})
            add_unit(r["id"], text, r["start_ms"], r["end_ms"], [r["id"]], {"path": r["id"]})
    else:
        for si, raw in enumerate(values):
            sp = f"/segments/{si}"
            if not isinstance(raw, dict) or "word" in raw or "word_segments" in raw:
                raise InputError(f"{sp}: expected segment record")
            start, end = _interval(raw, sp)
            records = words_list(raw.get("words", []), sp + "/words")
            text = _text(raw["text"], sp + "/text") if "text" in raw else None
            result.segments.append({"id": sp, "original_id": raw.get("id"), "text": text,
                "start": raw.get("start"), "end": raw.get("end")})
            if not records and (text is None or not text.strip()):
                result.diagnostics.append({"code": "empty_segment", "source_id": sp, "text": text})
                continue
            # Match literal non-whitespace word content in order. Only whitespace
            # between matches may be assigned to a word; any other residual forces
            # whole-segment fallback, retaining both original text and word records.
            rendered = []
            reason = None
            if text is not None:
                cursor = 0
                for r in records:
                    core = r["word"].strip()
                    pos = text.find(core, cursor)
                    if pos < 0:
                        raise InputError(f"{sp}: word {r['id']} conflicts with segment text; cannot preserve both in SRT")
                    if text[cursor:pos].strip():
                        reason = "unaligned_segment_text"
                    stop = pos + len(core)
                    rendered.append((text[cursor:stop], cursor, stop))
                    cursor = stop
                if not reason and text[cursor:].strip():
                    reason = "unaligned_segment_text"
                if rendered and not reason:
                    part, a, _ = rendered[-1]
                    rendered[-1] = (part + text[cursor:], a, len(text))
            else:
                # No segment text: word strings are authoritative; same separator
                # policy as a flat list, never modify their original report strings.
                previous = ""
                safe_separators = set(grapheme_unit_stops([r["word"] for r in records]))
                for wi, r in enumerate(records):
                    part = r["word"]
                    if previous and wi in safe_separators and not previous[-1].isspace() and not part[0].isspace() and not is_cjk(previous[-1]) and not is_cjk(part[0]):
                        part = " " + part
                        result.diagnostics.append({"code": "inferred_separator", "source_id": r["id"], "separator": " "})
                    rendered.append((part, None, None))
                    previous = part
            if not records:
                reason = "no_words"
            elif any(r["start_ms"] is None or r["end_ms"] is None for r in records):
                reason = "untimed_word"
            if reason:
                synthesized = text is None
                if synthesized:
                    text = "".join(part for part, _, _ in rendered)
                    result.diagnostics.append({"code": "synthesized_segment_text", "source_id": sp,
                                               "reason": "segment text absent; join original words with explicit separator policy"})
                if not text.strip():
                    raise InputError(f"{sp}: fallback text is empty but words contain text; cannot preserve both")
                if start is None or end is None:
                    raise InputError(f"{sp}: {reason} requires explicit segment start and end")
                result.diagnostics.append({"code": "segment_fallback", "source_id": sp, "reason": reason})
                source = {"path": sp, "text_origin": "synthesized_from_words"} if synthesized else {
                    "path": sp + "/text", "text_span": [0, len(text)]}
                add_unit(sp + "/text", text, start, end, [r["id"] for r in records], source, True)
            else:
                for r, (part, a, b) in zip(records, rendered):
                    source = {"path": r["id"], "segment_id": sp}
                    if a is not None:
                        source["text_span"] = [a, b]
                    add_unit(r["id"], part, r["start_ms"], r["end_ms"], [r["id"]], source)
        if "word_segments" in data:
            # Validate the redundant mirror instead of processing it a second time.
            original_count = len(result.words)
            mirror_words = words_list(data["word_segments"], "/word_segments")
            del result.words[original_count:]
            def signature(r):
                return (r["word"], r["start"], r["end"], r["original_id"])
            if [signature(r) for r in mirror_words] != [signature(r) for r in result.words]:
                raise InputError("/word_segments: flattened mirror disagrees with /segments words")
            result.diagnostics.append({"code": "validated_word_segments_mirror", "count": len(result.words)})
    if "text" in data:
        import re
        result.top_level_text = _text(data["text"], "/text")
        def compact(text):
            return re.sub(r"\s+", "", text)
        if compact(result.top_level_text) != compact("".join(u.text for u in result.units)):
            raise InputError("/text: top-level transcript disagrees with segment/word text")
        result.diagnostics.append({"code": "validated_top_level_text_mirror",
                                   "whitespace_comparison": "ignored; exact original retained in source_text"})
    if not result.units:
        raise InputError("input contains no renderable text with timestamps")
    return result
