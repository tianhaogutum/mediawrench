"""Strict input boundaries; pysubs2 supplies real SRT parsing and formatting.

Raw payloads are kept separately because subtitle libraries normalize whitespace
and interpret escape sequences. No upstream implementation is copied here.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import re

import pysubs2

# The pattern pysubs2's SRT reader uses to find timing lines. pysubs2 1.8 exposes it as
# SubripFormat.TIMESTAMP; fall back to the same expression if a later release moves it.
try:
    _SRT_TIMESTAMP = pysubs2.formats.subrip.SubripFormat.TIMESTAMP
except AttributeError:  # pragma: no cover - depends on the installed pysubs2
    _SRT_TIMESTAMP = re.compile(r"(\d{1,2}):(\d{1,2}):(\d{1,2})[.,](\d{1,3})")


class BridgeError(ValueError):
    """An unsupported input or an unresolved reconciliation decision."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


MAX_MS = 100 * 3600 * 1000 - 1  # pysubs2/SRT representable ceiling
SRT_TIME = r"[0-9]{2}:[0-5][0-9]:[0-5][0-9],[0-9]{3}"
VTT_TIME = r"(?:[0-9]{2}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"
SRT_TIMING = re.compile(rf"({SRT_TIME}) --> ({SRT_TIME})")
VTT_TIMING = re.compile(rf"({VTT_TIME}) --> ({VTT_TIME})(.*)")


@dataclass
class Cue:
    source_id: str | None
    start_ms: int
    end_ms: int
    text: str
    metadata: dict = field(default_factory=dict)


def decode_input(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BridgeError("Input must be UTF-8 (an initial BOM is allowed)") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text:
        raise BridgeError("Lone carriage returns and NUL characters are unsupported")
    return text


def validate_text(text: str) -> None:
    if not isinstance(text, str) or not text or not text.strip():
        raise BridgeError("Empty/whitespace-only subtitle text is unsupported")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise BridgeError("Subtitle text contains unsupported control characters")
    if any(not line.strip() for line in text.split("\n")):
        raise BridgeError("Blank payload lines are ambiguous in SRT and unsupported")
    # pysubs2 uses timing lines to find cues; reject ambiguous embedded timing.
    if any(len(_SRT_TIMESTAMP.findall(line)) >= 2
           for line in text.split("\n")):
        raise BridgeError("Subtitle text contains an ambiguous timestamp pair")


def validate_cues(cues: list[Cue], *, ordered=True) -> None:
    last = -1
    for cue in cues:
        if (type(cue.start_ms) is not int or type(cue.end_ms) is not int
                or not 0 <= cue.start_ms < cue.end_ms <= MAX_MS):
            raise BridgeError("Cue times must satisfy 0 <= start < end < 100 hours")
        if ordered and cue.start_ms < last:
            raise BridgeError("Cues are out of start-time order")
        last = cue.start_ms
        validate_text(cue.text)


def _stamp_ms(stamp: str) -> int:
    fields = stamp.replace(",", ".").split(":")
    if len(fields) == 2:
        fields.insert(0, "0")
    hours, minutes, sec = fields
    seconds, ms = sec.split(".")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(ms)


def _blocks(text: str) -> list[str]:
    # Only line terminators/separators are removed, never payload spaces or tabs.
    return re.split(r"\n\n+", text.rstrip("\n")) if text.rstrip("\n") else []


def parse_srt(text: str) -> tuple[list[Cue], dict]:
    cues = []
    ids = set()
    for number, block in enumerate(_blocks(text), 1):
        lines = block.split("\n")
        if len(lines) < 3 or not re.fullmatch(r"[0-9]+", lines[0]):
            raise BridgeError(f"SRT block {number}: expected numeric ID, timing and text")
        match = SRT_TIMING.fullmatch(lines[1])
        if not match:
            raise BridgeError(f"SRT block {number}: unsupported timing syntax/settings")
        if lines[0] in ids:
            raise BridgeError(f"SRT duplicate cue ID: {lines[0]}")
        ids.add(lines[0])
        cues.append(Cue(lines[0], _stamp_ms(match[1]), _stamp_ms(match[2]),
                        "\n".join(lines[2:])))
    if not cues:
        raise BridgeError("SRT contains no cues")
    validate_cues(cues)
    # Use the actual library parser, but retain raw text rather than its normalized
    # SSA representation. Cross-check that both parsers agree on cue boundaries.
    parsed = pysubs2.SSAFile.from_string(text, format_="srt", keep_html_tags=True)
    if [(c.start_ms, c.end_ms) for c in cues] != [(c.start, c.end) for c in parsed]:
        raise BridgeError("SRT cue boundaries disagree with pysubs2; input is ambiguous")
    return cues, {}


def validate_vtt_settings(settings: str) -> None:
    """Strict authoring syntax from W3C WebVTT CR 2019-04-04, section 4.4.

    Preserve spelling/order rather than applying browser defaults. Regions need
    definitions that this subset does not support, so references also fail.
    """
    if not isinstance(settings, str):
        raise BridgeError("WebVTT cue settings must be a string")
    if not settings:
        return
    seen = set()
    for token in re.split(r"[ \t]+", settings.strip(" \t")):
        name, sep, value = token.partition(":")
        if name in seen:
            raise BridgeError(f"Duplicate WebVTT cue setting: {name}")
        seen.add(name)
        valid = False
        if name == "region":
            raise BridgeError("WebVTT REGION references are unsupported; refusing silent loss")
        if sep and name == "vertical":
            valid = value in ("rl", "lr")
        elif sep and name == "align":
            valid = value in ("start", "center", "end", "left", "right")
        elif sep and name in ("line", "position", "size"):
            parts = value.split(",")
            offset = parts[0]
            percentage = re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", offset)
            valid = bool(percentage and Decimal(offset[:-1]) <= 100)
            if name == "line" and re.fullmatch(r"-?[0-9]+", offset):
                valid = True
            alignments = {"line": ("start", "center", "end"),
                          "position": ("line-left", "center", "line-right"), "size": ()}
            valid = valid and (len(parts) == 1 or
                              len(parts) == 2 and parts[1] in alignments[name])
        if not valid:
            raise BridgeError(f"Invalid or unsupported WebVTT cue setting: {token!r}")


def _vtt_controls(text: str) -> bool:
    return any(ord(c) < 32 and c not in "\n\t" for c in text)


def validate_vtt_identifier(identifier: str | None) -> None:
    if identifier is not None and (not isinstance(identifier, str) or not identifier
            or any(c in identifier for c in ("\n", "\r", "-->"))
            or _vtt_controls(identifier) or identifier in ("STYLE", "REGION", "NOTE")
            or identifier.startswith(("NOTE ", "NOTE\t"))):
        raise BridgeError("Invalid or unsupported WebVTT cue identifier")


def validate_vtt_metadata(metadata: dict, cue_count: int | None = None) -> None:
    if not isinstance(metadata, dict) or set(metadata) != {"header", "notes"}:
        raise BridgeError("Invalid or unsupported WebVTT document metadata")
    header, notes = metadata["header"], metadata["notes"]
    if (not isinstance(header, str) or not re.fullmatch(r"WEBVTT(?:[ \t][^\n]*)?", header)
            or "-->" in header or _vtt_controls(header)):
        raise BridgeError("Invalid WebVTT header metadata")
    if not isinstance(notes, list):
        raise BridgeError("Invalid WebVTT NOTE metadata")
    for note in notes:
        if (not isinstance(note, dict) or set(note) != {"before_cue", "text"}
                or type(note["before_cue"]) is not int or note["before_cue"] < 0
                or cue_count is not None and note["before_cue"] > cue_count):
            raise BridgeError("Invalid WebVTT NOTE position metadata")
        text = note["text"]
        if (not isinstance(text, str) or not re.match(r"NOTE(?:[ \t\n]|$)", text)
                or "-->" in text or "\n\n" in text or _vtt_controls(text)):
            raise BridgeError("Invalid or unsupported WebVTT NOTE text metadata")


def validate_vtt_payload(payload: str) -> None:
    validate_text(payload)
    if "<" in payload or re.search(r"&(?:[A-Za-z0-9#]+);", payload) or "-->" in payload:
        raise BridgeError("WebVTT markup, entities and embedded --> are unsupported")


def validate_vtt_cue(cue: Cue) -> None:
    validate_vtt_identifier(cue.source_id)
    validate_vtt_payload(cue.text)
    if not isinstance(cue.metadata, dict) or set(cue.metadata) - {"settings"}:
        raise BridgeError("Invalid or unsupported WebVTT cue metadata")
    validate_vtt_settings(cue.metadata.get("settings", ""))


def parse_vtt(text: str) -> tuple[list[Cue], dict]:
    blocks = _blocks(text)
    if not blocks or not re.fullmatch(r"WEBVTT(?:[ \t][^\n]*)?", blocks[0]):
        raise BridgeError("Expected WEBVTT header and blank line; header directives unsupported")
    if "-->" in blocks[0]:
        raise BridgeError("WEBVTT header cannot contain -->")
    metadata = {"header": blocks[0], "notes": []}
    cues = []
    for number, block in enumerate(blocks[1:], 1):
        lines = block.split("\n")
        if lines[0] == "NOTE" or lines[0].startswith(("NOTE ", "NOTE\t")):
            if "-->" in block:
                raise BridgeError("WebVTT NOTE cannot contain -->")
            metadata["notes"].append({"before_cue": len(cues), "text": block})
            continue
        if lines[0] in ("STYLE", "REGION"):
            raise BridgeError(f"WebVTT {lines[0]} blocks are unsupported; refusing silent loss")
        source_id = None
        if "-->" not in lines[0]:
            source_id = lines.pop(0)
        if len(lines) < 2:
            raise BridgeError(f"WebVTT block {number}: expected timing and text")
        match = VTT_TIMING.fullmatch(lines[0])
        if not match:
            raise BridgeError(f"WebVTT block {number}: invalid timing")
        if match[3] and match[3][0] not in " \t":
            raise BridgeError("WebVTT cue settings require a space/tab after the end timestamp")
        settings = match[3].strip(" \t")
        payload = "\n".join(lines[1:])
        cue = Cue(source_id, _stamp_ms(match[1]), _stamp_ms(match[2]), payload,
                  {"settings": settings} if settings else {})
        validate_vtt_cue(cue)
        cues.append(cue)
    if not cues:
        raise BridgeError("WebVTT contains no cues")
    validate_cues(cues)
    validate_vtt_metadata(metadata, len(cues))
    return cues, metadata


def render_vtt(cues: list[Cue], metadata: dict | None = None) -> str:
    validate_cues(cues)
    metadata = metadata if metadata is not None else {"header": "WEBVTT", "notes": []}
    validate_vtt_metadata(metadata)
    # NOTE text is retained in source order in a preamble: timing edits, crossing
    # cues and deletions do not imply a safe new positional attachment for notes.
    blocks = [metadata["header"], *(note["text"] for note in metadata["notes"])]

    def stamp(ms):
        hours, rest = divmod(ms, 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"

    for cue in cues:
        validate_vtt_cue(cue)
        settings = cue.metadata.get("settings", "")
        timing = f"{stamp(cue.start_ms)} --> {stamp(cue.end_ms)}"
        if settings:
            timing += " " + settings
        blocks.append((cue.source_id + "\n" if cue.source_id is not None else "")
                      + timing + "\n" + cue.text)
    return "\n\n".join(blocks) + "\n\n"


def render_srt(cues: list[Cue]) -> str:
    validate_cues(cues)
    if not cues:
        return ""
    subs = pysubs2.SSAFile()
    for cue in cues:
        subs.append(pysubs2.SSAEvent(start=cue.start_ms, end=cue.end_ms, text="BRIDGE_PAYLOAD"))
    # The library produces the real SRT numbering/timing scaffold. Insert raw
    # payloads afterward to avoid interpreting literal \\N, braces or HTML tags.
    scaffold = subs.to_string("srt").rstrip("\n").split("\n\n")
    if len(scaffold) != len(cues):
        raise BridgeError("Unexpected pysubs2 serializer result")
    rendered = []
    for i, (block, cue) in enumerate(zip(scaffold, cues), 1):
        # Output is sequential; original source identifiers stay in the sidecar
        # and report because WebVTT identifiers cannot always become SRT numbers.
        rendered.append(f"{i}\n{block.splitlines()[1]}\n{cue.text}\n\n")
    output = "".join(rendered)
    parsed = pysubs2.SSAFile.from_string(output, format_="srt", keep_html_tags=True)
    if [(c.start, c.end) for c in parsed] != [(c.start_ms, c.end_ms) for c in cues]:
        raise BridgeError("Output cannot be parsed unambiguously as SRT")
    return output
