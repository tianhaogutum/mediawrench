"""Strict supported WebVTT subset and conservative adjacent-cue repair.

All code is original. Whitespace-delimited tokens are deliberately not linguistic
words. Time intervals describe source display/inline segments, not word alignment.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import groupby
import re
from typing import Any
import unicodedata

import pysubs2


class CaptionError(ValueError):
    """Invalid or unsupported input; no output should be published."""


STAMP = r"(?:[0-9]{2,}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3}"
TIMING = re.compile(rf"^({STAMP})[ \t]+-->[ \t]+({STAMP})(.*)$")
SRT_TIMING = re.compile(r"^([0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3}) --> "
                        r"([0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3})$")
ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "nbsp": "\u00a0", "lrm": "\u200e", "rlm": "\u200f"}
MAX_MS = 359_999_999  # pysubs2 SRT writer's 99:59:59,999 ceiling


def timestamp(value: str) -> int:
    if not re.fullmatch(STAMP, value):
        raise CaptionError(f"Malformed timestamp: {value!r}")
    parts = value.split(":")
    hours, minutes, seconds = (0, *parts) if len(parts) == 2 else parts
    sec, milli = str(seconds).split(".")
    if int(minutes) > 59 or int(sec) > 59:
        raise CaptionError(f"Timestamp component out of range: {value!r}")
    result = ((int(hours) * 60 + int(minutes)) * 60 + int(sec)) * 1000 + int(milli)
    if result > MAX_MS:
        raise CaptionError("Timestamp exceeds SRT limit 99:59:59.999")
    return result


@dataclass(frozen=True)
class Token:
    id: str
    text: str
    line: int
    char_start: int
    char_end: int
    start_ms: int
    end_ms: int
    contexts: tuple[tuple[str, ...], ...] = ()
    timing_kind: str = "cue"
    # Preserved VTT additionally needs each decoded character's ordered stack:
    # distinct token contexts alone cannot distinguish <b>H</b>ello/ <b>He</b>llo.
    glyph_contexts: tuple[tuple[str, ...], ...] = ()

    @property
    def key(self) -> tuple:
        return self.text, self.contexts, self.glyph_contexts


@dataclass
class Cue:
    index: int
    identifier: str | None
    start_ms: int
    end_ms: int
    raw: str
    plain: str
    tokens: list[Token]
    settings: str = ""


@dataclass
class OutputCue:
    start_ms: int
    end_ms: int
    tokens: list[Token]

    @property
    def text(self) -> str:
        # Preserve input line boundaries, normalize horizontal whitespace.
        result = ""
        for i, token in enumerate(self.tokens):
            if i:
                result += "\n" if token.line != self.tokens[i - 1].line else " "
            result += token.text
        return result


@dataclass
class Result:
    output: str
    provenance: dict[str, Any]
    output_format: str = "srt"

    @property
    def srt(self) -> str:
        if self.output_format != "srt":
            raise CaptionError("This result contains VTT; use result.vtt or result.output")
        return self.output

    @property
    def vtt(self) -> str:
        if self.output_format != "vtt":
            raise CaptionError("This result contains SRT; use result.srt or result.output")
        return self.output

    @property
    def report(self) -> dict[str, Any]:
        keys = ["mode", "summary", "warnings", "ambiguities"]
        if "loss_policy" in self.provenance:
            keys += ["output_format", "loss_policy"]
        return {key: self.provenance[key] for key in keys}


def _preserved_voice(tag: str) -> bool:
    """Opaque L/M/ASCII classes and existing speaker words; preserved VTT only."""
    opening, sep, label = tag.partition(" ")
    base, *classes = opening.split(".")
    if not sep or base != "v":
        return False
    # Dot separates classes. Preserve codepoints/order/duplicates exactly, with
    # no speaker punctuation alternatives or Unicode normalization in classes.
    for segment in classes:
        if not segment:
            return False
        for i, char in enumerate(segment):
            if char not in "0123456789_-" and unicodedata.category(char)[0] not in ("L" if i == 0 else "LM"):
                return False
    # Split only on ASCII space so edge/repeated/other whitespace stays invalid.
    # Categories come from Python's Unicode database; never normalize the label.
    for word in label.split(" "):
        # Dotted words are either single-letter initials (L M*) or multi-letter
        # abbreviations (L then L/M, at least two L total), then one ASCII dot.
        # This union is L followed by L/M; it never permits other punctuation or
        # the digits/underscores/hyphens allowed by the ordinary-word alternative.
        if word.endswith("."):
            dotted = word[:-1]
            if (not dotted or unicodedata.category(dotted[0])[0] != "L" or
                    any(unicodedata.category(char)[0] not in "LM" for char in dotted[1:])):
                return False
            continue
        joined = "'" in word or "’" in word or "‑" in word
        # The additional alternative permits only letter/mark runs, each starting
        # L, joined by apostrophes, ASCII hyphens or U+2011 nonbreaking hyphens.
        # At least one apostrophe or U+2011 is required; ordinary ASCII-hyphen
        # words stay unchanged. Each separator has L/M left and L right.
        for run in re.split("['’‑-]", word) if joined else [word]:
            if not run:
                return False
            for i, char in enumerate(run):
                if not joined and char in "0123456789_-":
                    continue
                if unicodedata.category(char)[0] not in ("L" if i == 0 else "LM"):
                    return False
    return True


def _payload(raw: str, index: int, start: int, end: int, flatten: bool,
             warnings: list[dict], preserve: bool = False) -> tuple[str, list[Token]]:
    # Glyph records retain raw offsets even across tags and entity decoding.
    glyphs: list[tuple[str, int, int, int, int, tuple[str, ...]]] = []
    stack: list[str] = []
    times = [start]
    line = 1
    pos = 0
    losses: set[str] = set()
    while pos < len(raw):
        char = raw[pos]
        if char == "<":
            close = raw.find(">", pos + 1)
            if close < 0:
                raise CaptionError(f"Cue {index}: unclosed markup")
            tag = raw[pos + 1:close]
            # Language labels are opaque ASCII contexts, not BCP47 validation.
            # Preservation requires explicit closure; SRT keeps its lossy grammar.
            if preserve and not (tag in ("/b", "/i", "/u", "/c", "/v", "/lang") or
                                 re.fullmatch(r"(?:b|i|u|c)(?:\.[A-Za-z0-9_-]+)*", tag) or
                                 _preserved_voice(tag) or
                                 re.fullmatch(r"lang(?:\.[A-Za-z0-9_-]+)* [A-Za-z]+(?:-[A-Za-z0-9]+)*", tag)):
                raise CaptionError(f"Cue {index}: VTT output preserves only balanced b/i/u/c tags with optional class lists, restricted voice and language scopes with optional class lists; "
                                   "inline timestamps/other markup unsupported; refusing to flatten")
            if re.fullmatch(STAMP, tag):
                time = timestamp(tag)
                if not times[-1] < time < end:
                    raise CaptionError(f"Cue {index}: inline timestamps must increase inside cue")
                times.append(time)
                losses.add("inline timestamp playback")
            elif tag.startswith("/"):
                if not stack or tag[1:] != stack[-1].split(".")[0].split(" ")[0]:
                    raise CaptionError(f"Cue {index}: mismatched closing tag <{tag}>")
                stack.pop()
                losses.add("inline formatting/annotations")
            elif (re.fullmatch(r"(?:b|i|u|c)(?:\.[A-Za-z0-9_-]+)*", tag)
                  or (preserve and _preserved_voice(tag))
                  or re.fullmatch(r"(?:v|lang)(?:\.[A-Za-z0-9_-]+)* [^<>\n]+", tag)):
                stack.append(tag)
                losses.add("inline formatting/annotations")
            else:
                raise CaptionError(f"Cue {index}: unsupported tag <{tag}>")
            pos = close + 1
            continue
        raw_end = pos + 1
        if char == "&":
            match = re.match(r"&([A-Za-z]+);", raw[pos:])
            if not match or match[1] not in ENTITIES:
                raise CaptionError(f"Cue {index}: unsupported or malformed entity at offset {pos}")
            char = ENTITIES[match[1]]
            raw_end = pos + len(match[0])
        if char == "\x00" or (ord(char) < 32 and char not in "\t\n"):
            raise CaptionError(f"Cue {index}: unsupported control character")
        glyphs.append((char, pos, raw_end, line, len(times) - 1, tuple(stack)))
        if char == "\n":
            line += 1
        pos = raw_end
    # SRT's lossy subset permits cue-terminated voice/language annotations.
    # Preserved VTT requires every accepted scope to close explicitly.
    if (preserve and stack) or any(tag.split(" ")[0].split(".")[0] not in ("v", "lang") for tag in stack):
        raise CaptionError(f"Cue {index}: unclosed formatting tag")
    if losses and not preserve:
        if not flatten:
            raise CaptionError(f"Cue {index}: use --flatten to explicitly accept loss of {', '.join(sorted(losses))}")
        warnings.append({"cue": index, "kind": "lossy_flattening", "details": sorted(losses)})
    plain = "".join(g[0] for g in glyphs)
    boundaries = times[1:] + [end]
    tokens = []
    for n, match in enumerate(re.finditer(r"\S+", plain), 1):
        selected = glyphs[match.start():match.end()]
        contexts = tuple(dict.fromkeys(g[5] for g in selected))
        tokens.append(Token(f"c{index}:t{n}", match[0], selected[0][3],
                            selected[0][1], selected[-1][2],
                            min(times[g[4]] for g in selected),
                            max(boundaries[g[4]] for g in selected), contexts,
                            "inline" if len(times) > 1 else "cue",
                            tuple(g[5] for g in selected) if preserve else ()))
    if not tokens:
        raise CaptionError(f"Cue {index}: empty cue text is unsupported")
    return plain, tokens


def _normal_text(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class _VttDocument:
    cues: list[Cue]
    warnings: list[dict]
    header: str
    notes: list[dict]


def _validate_settings(settings: str) -> None:
    """Strict layout subset; unknown/region and duplicate settings are errors."""
    seen = set()
    for setting in re.split(r"[ \t]+", settings):
        key, sep, value = setting.partition(":")
        valid = False
        if key == "align":
            valid = value in {"start", "center", "end", "left", "right"}
        elif key == "vertical":
            valid = value in {"rl", "lr"}
        elif key in {"position", "size", "line"}:
            number, comma, alignment = value.partition(",")
            percent = re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", number)
            valid = bool(percent and 0 <= float(number[:-1]) <= 100)
            if key == "line":
                valid = valid or bool(re.fullmatch(r"-?[0-9]+", number))
            if comma:
                valid = valid and alignment in ({"start", "center", "end"} if key == "line"
                                                else {"line-left", "center", "line-right"})
            if key == "size" and comma:
                valid = False
        if not sep or not valid or key in seen:
            raise CaptionError(f"Unsupported or invalid cue settings: {settings}")
        seen.add(key)


def _parse_vtt_document(text: str, *, flatten: bool = False,
                        preserve_metadata: bool = False) -> _VttDocument:
    text = _normal_text(text)
    if preserve_metadata and any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise CaptionError("Unsupported control character in preserved WebVTT")
    lines = text.split("\n")
    if not re.fullmatch(r"WEBVTT(?:[ \t][^\n]*)?", lines[0]) or "-->" in lines[0]:
        raise CaptionError("Expected WEBVTT header")
    header_end = next((i for i in range(1, len(lines)) if not lines[i].strip()), len(lines))
    if header_end == len(lines) or (header_end != 1 and not preserve_metadata):
        raise CaptionError("WEBVTT header must be followed by a blank line; header metadata unsupported for SRT")
    header = "\n".join(lines[:header_end])
    if preserve_metadata and "-->" in header:
        raise CaptionError("Invalid WebVTT header metadata")
    blocks = re.split(r"\n[ \t]*\n", "\n".join(lines[header_end+1:]).strip("\n"))
    cues: list[Cue] = []
    warnings: list[dict] = []
    notes: list[dict] = []
    for block in blocks:
        if not block.strip():
            continue
        rows = block.split("\n")
        if rows[0] == "NOTE" or rows[0].startswith(("NOTE ", "NOTE\t")):
            if preserve_metadata:
                if "-->" in block:
                    raise CaptionError("Invalid WebVTT NOTE block")
                notes.append({"before_source_cue": len(cues) + 1, "text": block})
            else:
                warnings.append({"kind": "comment_omitted", "text": block})
            continue
        if rows[0] in ("STYLE", "REGION"):
            raise CaptionError(f"Unsupported {rows[0]} block; refusing to discard it")
        identifier = None
        if "-->" not in rows[0]:
            identifier = rows.pop(0)
        if not rows:
            raise CaptionError("Missing cue timing")
        match = TIMING.fullmatch(rows.pop(0))
        if not match:
            raise CaptionError(f"Malformed cue timing near cue {len(cues) + 1}")
        start, end = timestamp(match[1]), timestamp(match[2])
        if end <= start:
            raise CaptionError("Cue must have strictly positive duration")
        if cues and start < cues[-1].start_ms:
            raise CaptionError("Cues must be ordered by nondecreasing start time")
        settings = match[3].strip(" \t")
        if settings:
            if not preserve_metadata:
                raise CaptionError(f"Unsupported cue settings: {settings}")
            if not match[3].startswith((" ", "\t")):
                raise CaptionError("Invalid WebVTT cue settings separator")
            _validate_settings(settings)
        raw = "\n".join(rows)
        if preserve_metadata and "-->" in raw:
            raise CaptionError("Literal --> is invalid WebVTT cue text; encode > as &gt;")
        plain, tokens = _payload(raw, len(cues) + 1, start, end, flatten, warnings,
                                 preserve=preserve_metadata)
        cues.append(Cue(len(cues) + 1, identifier, start, end, raw, plain, tokens, settings))
    if not cues:
        raise CaptionError("No caption cues found")
    return _VttDocument(cues, warnings, header, notes)


def parse_vtt(text: str, *, flatten: bool = False,
              preserve_metadata: bool = False) -> tuple[list[Cue], list[dict]]:
    """Parse the SRT-safe subset, or validate preserved VTT layout/metadata."""
    document = _parse_vtt_document(text, flatten=flatten, preserve_metadata=preserve_metadata)
    return document.cues, document.warnings


def _parse_srt(text: str) -> list[Cue]:
    # SRT input is canonicalization only: we never heuristically deduplicate SRT.
    # Validate before pysubs2, whose permissive parser can silently drop bad cues.
    blocks = re.split(r"\n[ \t]*\n", _normal_text(text).strip("\n"))
    cues = []
    for block in blocks:
        rows = block.split("\n")
        if len(rows) < 3 or not rows[0].isdigit():
            raise CaptionError("Expected an SRT index, timing line and text")
        match = SRT_TIMING.fullmatch(rows[1])
        if not match:
            raise CaptionError("Malformed SRT timing")
        start, end = (timestamp(value.replace(",", ".")) for value in match.groups())
        if end <= start or (cues and start < cues[-1].start_ms):
            raise CaptionError("Invalid SRT duration or cue order")
        raw = "\n".join(rows[2:])
        # Only plain SRT is supported; special ASS sequences are unsafe in pysubs2.
        if any(char in raw for char in "<>\\{}"):
            raise CaptionError("Only plain SRT passthrough is supported; markup/ASS escapes unsupported")
        tokens = [Token(f"c{len(cues)+1}:t{n}", m[0], raw[:m.start()].count("\n") + 1,
                        m.start(), m.end(), start, end)
                  for n, m in enumerate(re.finditer(r"\S+", raw), 1)]
        if not tokens:
            raise CaptionError("Empty SRT cue")
        cues.append(Cue(len(cues)+1, rows[0], start, end, raw, raw, tokens))
    # Exercise the actual dependency's parser too; it must agree on time and text.
    loaded = pysubs2.SSAFile.from_string(text, format_="srt")
    if len(loaded) != len(cues) or any((s.start, s.end, s.plaintext) !=
            (c.start_ms, c.end_ms, c.plain) for s, c in zip(loaded, cues)):
        raise CaptionError("SRT cannot be represented losslessly by pysubs2")
    return cues


def _occurrences(haystack: list[Any], needle: list[Any]) -> int:
    return sum(haystack[i:i+len(needle)] == needle for i in range(len(haystack)-len(needle)+1))


def _overlap(previous: Cue, current: Cue, gap: int,
             word_overlap: bool = False) -> tuple[int, str | None]:
    if not word_overlap and current.start_ms - previous.end_ms > gap:
        return 0, None
    # The opt-in mode also diagnoses matching text whose annotation contexts
    # disagree. Count text alignments before comparing contexts so annotations
    # cannot hide an otherwise ambiguous repeated phrase.
    a, b = ([t.text if word_overlap else t.key for t in cue.tokens]
            for cue in (previous, current))
    candidates = [n for n in range(1, min(len(a), len(b)) + 1) if a[-n:] == b[:n]]
    if not candidates:
        if word_overlap:
            old_text = " ".join(t.text for t in previous.tokens)
            new_text = " ".join(t.text for t in current.tokens)
            if any(old_text[-n:] == new_text[:n]
                   for n in range(1, min(len(old_text), len(new_text)) + 1)):
                return 0, "text overlap lacks exact whitespace-delimited word boundaries"
        return 0, None
    if previous.settings != current.settings:
        return 0, "matching text has different cue settings; preserving both displays"
    n = candidates[-1]
    if word_overlap and current.start_ms >= previous.end_ms:
        return 0, "word-overlap mode requires strict temporal overlap; possible paused repeated speech"
    if current.end_ms < previous.end_ms and a != b:
        return 0, "nested cue: uncertain rolling order"
    if current.start_ms >= previous.end_ms and a == b:
        return 0, "identical text without temporal overlap"
    if len(candidates) != 1 or _occurrences(a, b[:n]) != 1 or _occurrences(b, b[:n]) != 1:
        return 0, "repeated phrase has multiple possible alignments"
    if n < 2 or len({t.text for t in current.tokens[:n]}) < 2:
        return 0, "short/repetitive match is ambiguous"
    old_tokens, new_tokens = previous.tokens[-n:], current.tokens[:n]
    if word_overlap:
        if len({t.text for t in new_tokens if any(c.isalnum() for c in t.text)}) < 2:
            return 0, "insufficient distinct word evidence; punctuation is not word evidence"
        if any(old.key != new.key for old, new in zip(old_tokens, new_tokens)):
            return 0, "matching text has different formatting/speaker/language contexts"
    # Explicit word mode compares token/context evidence across display rewraps.
    # Retained fragments still render their own source token line boundaries.
    start_a = len(a) - n
    cuts_line = ((start_a and previous.tokens[start_a-1].line == previous.tokens[start_a].line)
                 or (n < len(b) and current.tokens[n-1].line == current.tokens[n].line))
    if cuts_line and not word_overlap:
        return 0, "match cuts through a line; possible intentional repeated speech"
    if not word_overlap and any((old_tokens[i].line == old_tokens[i-1].line) !=
                                (new_tokens[i].line == new_tokens[i-1].line) for i in range(1, n)):
        return 0, "matching text has different internal line boundaries"
    if any((old.timing_kind == "inline" or new.timing_kind == "inline") and
           max(old.start_ms, new.start_ms) >= min(old.end_ms, new.end_ms)
           for old, new in zip(old_tokens, new_tokens)):
        return 0, "inline timing indicates separate speech intervals"
    if a != b and n == len(b):
        return 0, "suffix-only cue without new text is ambiguous"
    return n, None


def _render(outputs: list[OutputCue]) -> str:
    subs = pysubs2.SSAFile()
    for output in outputs:
        value = output.text
        # pysubs2 uses ASS internally; refuse literals that its SRT writer changes.
        if any(char in value for char in "<>\\{}"):
            raise CaptionError("Text contains literal markup/ASS characters unsafe for pysubs2 SRT roundtrip")
        event = pysubs2.SSAEvent(start=output.start_ms, end=output.end_ms)
        event.plaintext = value
        subs.append(event)
    srt = subs.to_string("srt")
    loaded = pysubs2.SSAFile.from_string(srt, format_="srt")
    if len(loaded) != len(outputs) or any((s.start, s.end, s.plaintext) !=
            (o.start_ms, o.end_ms, o.text) for s, o in zip(loaded, outputs)):
        raise CaptionError("Generated SRT failed lossless pysubs2 roundtrip")
    return srt


def _vtt_stamp(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def _vtt_payload(output: OutputCue) -> str:
    """Balance a retained span's tags without copying removed source markup."""
    parts: list[str] = []
    active: tuple[str, ...] = ()

    def transition(target: tuple[str, ...]) -> None:
        nonlocal active
        common = 0
        while common < min(len(active), len(target)) and active[common] == target[common]:
            common += 1
        # Reopen full class/speaker/language contexts; close only the base name:
        # <c.a.b> -> </c>, <v Ada Lovelace> -> </v>, <lang.em en-US> -> </lang>.
        parts.extend(f"</{tag.split(' ')[0].split('.')[0]}>" for tag in reversed(active[common:]))
        parts.extend(f"<{tag}>" for tag in target[common:])
        active = target

    for i, token in enumerate(output.tokens):
        if i:
            # Whitespace is canonicalized. Keep only shared enclosing tags over
            # the separator; formatting of non-whitespace glyphs stays exact.
            next_context = token.glyph_contexts[0]
            common = 0
            while common < min(len(active), len(next_context)) and active[common] == next_context[common]:
                common += 1
            transition(active[:common])
            parts.append("\n" if token.line != output.tokens[i - 1].line else " ")
        for char, context in zip(token.text, token.glyph_contexts):
            transition(context)
            parts.append({"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(char, char))
    transition(())
    return "".join(parts)


def _render_vtt(outputs: list[OutputCue], document: _VttDocument) -> str:
    # Each fragment has one retained source cue, even after selective extension.
    # NOTE anchors for fully removed cues advance to the next retained source cue
    # (or EOF). NOTE content/order remains exact; it has no playback semantics.
    blocks = [document.header]
    source_by_token = {t.id: c for c in document.cues for t in c.tokens}
    expected = []
    note_index = 0
    for output in outputs:
        source = source_by_token[output.tokens[0].id]
        while (note_index < len(document.notes) and
               document.notes[note_index]["before_source_cue"] <= source.index):
            blocks.append(document.notes[note_index]["text"])
            note_index += 1
        expected.append((output.start_ms, output.end_ms, output.text, source.settings, source.identifier,
                         [t.key for t in output.tokens]))
        value = _vtt_payload(output)
        timing = f"{_vtt_stamp(output.start_ms)} --> {_vtt_stamp(output.end_ms)}"
        if source.settings:
            timing += " " + source.settings
        blocks.append((source.identifier + "\n" if source.identifier is not None else "") + timing + "\n" + value)
    blocks.extend(note["text"] for note in document.notes[note_index:])
    rendered = "\n\n".join(blocks) + "\n"
    loaded = _parse_vtt_document(rendered, preserve_metadata=True)
    actual = [(c.start_ms, c.end_ms, c.plain, c.settings, c.identifier,
               [t.key for t in c.tokens]) for c in loaded.cues]
    if (actual != expected or loaded.header != document.header or
            [n["text"] for n in loaded.notes] != [n["text"] for n in document.notes]):
        raise CaptionError("Generated VTT failed lossless text/formatting/timing/metadata roundtrip")
    return rendered


def convert(text: str, *, flatten: bool = False, max_gap_ms: int = 0,
            input_format: str = "vtt", word_overlap: bool = False,
            output_format: str = "srt") -> Result:
    """Repair VTT, or canonicalize plain SRT without deduplication.

    Positive max_gap_ms explicitly allows matching partial roll-ups across short
    gaps; identical cues always require real overlap. Every source token appears
    once in provenance, including tokens removed as aliases of retained tokens.
    word_overlap explicitly permits partial-line and line-rewrapped matches with
    at least two distinct word tokens, but requires strict temporal overlap regardless of
    max_gap_ms. SRT always remains passthrough. output_format="vtt" preserves
    header/NOTE metadata, supported cue settings, balanced b/i/u/c tags with optional class lists
    and restricted explicitly balanced voice and language scopes (optional classes),
    rejects inline timestamps/other annotations,
    and exposes result.output/result.vtt; the default exposes result.srt as before.
    """
    if isinstance(max_gap_ms, bool) or not isinstance(max_gap_ms, int) or max_gap_ms < 0:
        raise CaptionError("max_gap_ms must be a nonnegative integer")
    if not isinstance(word_overlap, bool):
        raise CaptionError("word_overlap must be a boolean")
    if output_format not in ("srt", "vtt"):
        raise CaptionError("output_format must be srt or vtt")
    if output_format == "vtt" and input_format != "vtt":
        raise CaptionError("Preserved VTT output requires VTT input")
    if input_format == "vtt":
        document = _parse_vtt_document(text, flatten=flatten, preserve_metadata=output_format == "vtt")
        cues, warnings = document.cues, document.warnings
    elif input_format == "srt":
        cues, warnings = _parse_srt(text), []
    else:
        raise CaptionError("input_format must be vtt or srt")
    outputs: list[OutputCue] = []
    aliases: dict[str, str] = {}
    edits = []
    ambiguities = []
    records = []
    for i, cue in enumerate(cues):
        n, reason = (_overlap(cues[i-1], cue, max_gap_ms, word_overlap)
                     if i and input_format == "vtt" else (0, None))
        if reason:
            ambiguities.append({"previous_cue": cues[i-1].index, "cue": cue.index, "reason": reason,
                                "action": "retained_all"})
        if n:
            previous = cues[i-1]
            for old, token in zip(previous.tokens[-n:], cue.tokens[:n]):
                aliases[token.id] = aliases[old.id]
            edits.append({"cue": cue.index, "previous_cue": previous.index, "removed_tokens": n,
                          "reason": ("identical_overlapping_cues" if n == len(cue.tokens)
                                     else "adjacent_word_rollup" if word_overlap
                                     else "adjacent_whole_line_rollup")})
            # Split when necessary: extending the whole event would also prolong
            # earlier lines/words that are absent from the new rolling cue.
            roots = {aliases[t.id] for t in cue.tokens[:n]}
            extended = []
            for output in outputs:
                if cue.end_ms <= output.end_ms or not any(t.id in roots for t in output.tokens):
                    extended.append(output)
                    continue
                for matched, segment in groupby(output.tokens, key=lambda token: token.id in roots):
                    extended.append(OutputCue(output.start_ms,
                                              cue.end_ms if matched else output.end_ms, list(segment)))
            outputs = extended
        if n < len(cue.tokens):
            outputs.append(OutputCue(cue.start_ms, cue.end_ms, cue.tokens[n:]))
            for token in cue.tokens[n:]:
                aliases[token.id] = token.id
        for token in cue.tokens:
            records.append({"id": token.id, "text": token.text, "source_cue": cue.index,
                            "source_cue_id": cue.identifier, "source_line": token.line,
                            "source_char_start": token.char_start, "source_char_end": token.char_end,
                            "source_start_ms": token.start_ms, "source_end_ms": token.end_ms,
                            "timing_kind": token.timing_kind,
                            "action": "retained" if aliases[token.id] == token.id else "removed_overlap",
                            "representative": aliases[token.id]})
    locations = {t.id: {"cue": i, "token": j} for i, o in enumerate(outputs, 1)
                 for j, t in enumerate(o.tokens, 1)}
    for record in records:
        record["output"] = locations[record["representative"]]
    removed = sum(r["action"] == "removed_overlap" for r in records)
    provenance = {
        "schema_version": 1, "tool_version": "0.1.0",
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "mode": "vtt_repair" if input_format == "vtt" else "srt_passthrough",
        "options": {"flatten": flatten, "max_gap_ms": max_gap_ms},
        "tokenization": "Unicode whitespace-delimited, case/punctuation-sensitive; no normalization",
        "source_text_normalization": "BOM removed; CRLF/CR become LF; offsets refer to raw_text in this map",
        "timing": "source display/inline segment intervals, not inferred word alignment",
        "summary": {"input_cues": len(cues), "output_cues": len(outputs), "input_tokens": len(records),
                    "retained_tokens": len(records)-removed, "removed_tokens": removed,
                    "ambiguous_pairs": len(ambiguities)},
        "source_cues": [{"index": c.index, "id": c.identifier, "start_ms": c.start_ms,
                         "end_ms": c.end_ms, "raw_text": c.raw, "plain_text": c.plain} for c in cues],
        "output_cues": [{"index": i, "start_ms": o.start_ms, "end_ms": o.end_ms, "text": o.text}
                        for i, o in enumerate(outputs, 1)],
        "tokens": records, "edits": edits, "warnings": warnings, "ambiguities": ambiguities,
    }
    if word_overlap or output_format == "vtt":
        # Keep default-mode JSON unchanged. Opted-in output fragments additionally
        # expose contiguous 1-based, half-open source token ranges.
        if word_overlap:
            provenance["options"]["word_overlap"] = True
        source_positions = {t.id: (c.index, j) for c in cues for j, t in enumerate(c.tokens, 1)}
        for output, record in zip(outputs, provenance["output_cues"]):
            source_cue, first = source_positions[output.tokens[0].id]
            record["source_span"] = {"cue": source_cue, "token_start": first,
                                     "token_end": first + len(output.tokens)}
    if output_format == "vtt":
        rendered = _render_vtt(outputs, document)
        provenance["output_format"] = "vtt"
        provenance["options"]["output_format"] = "vtt"
        provenance["loss_policy"] = {
            "metadata": "preserve",
            "inline_markup_timestamps": "preserve balanced b/i/u/c tags with optional class lists, restricted voice and language scopes with optional class lists per retained span; reject inline timestamps/other markup even with --flatten",
            "unsupported_layout": "reject STYLE, REGION and unknown/invalid settings",
            "display": "repaired fragments inherit source settings; original visual layout is not guaranteed",
            "notes": "preserve content/order; removed-cue anchors advance to next retained cue or EOF",
        }
        provenance["webvtt"] = {"header": document.header, "notes": document.notes}
        for cue, record in zip(cues, provenance["source_cues"]):
            record["settings"] = cue.settings
        for output, record in zip(outputs, provenance["output_cues"]):
            source = cues[record["source_span"]["cue"] - 1]
            record.update({"settings": source.settings, "id": source.identifier})
    else:
        rendered = _render(outputs)
    provenance[output_format + "_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return Result(rendered, provenance, output_format)
