"""Text exports; SRT is genuinely serialized and reparsed by pysubs2."""

import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat

import pysubs2

try:
    from pysubs2.formats.subrip import MAX_REPRESENTABLE_TIME
except ImportError:  # pragma: no cover - depends on the installed pysubs2
    # pysubs2 1.8's value: the largest SRT timestamp, 99:59:59,999, in milliseconds.
    MAX_REPRESENTABLE_TIME = 100 * 3600 * 1000 - 1

from .core import BridgeError


def render_json(result: dict) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except (ValueError, TypeError) as exc:
        raise BridgeError(f"result cannot be serialized as strict JSON: {exc}") from exc


def _markdown(text: str) -> str:
    text = html.escape(" ".join(text.split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def render_markdown(result: dict) -> str:
    lines = ["# Speaker transcript", "", "Times are seconds. Segment precision is coarse attribution.", ""]
    for turn in result["turns"]:
        lines.append(f"- [{turn['start']:.3f}–{turn['end']:.3f}] "
                     f"**{_markdown(turn['speaker_label'])}** ({turn['precision']}): "
                     f"{_markdown(turn['text'])}")
    if result["warnings"]:
        lines.extend(["", "Warnings:", ""])
        lines.extend(f"- {_markdown(warning)}" for warning in result["warnings"])
    return "\n".join(lines) + "\n"


def _srt_literal(text: str) -> str:
    # Avoid ASS commands/tags and timestamp-like subtitle text being interpreted
    # by pysubs2 or SRT readers. JSON remains the exact text preservation format.
    text = html.escape(" ".join(text.split()), quote=False)
    for char, entity in (("\\", "&#92;"), ("{", "&#123;"), ("}", "&#125;"), (":", "&#58;")):
        text = text.replace(char, entity)
    return text


def render_srt(result: dict) -> str:
    subs = pysubs2.SSAFile()
    expected = []
    for turn in result["turns"]:
        # Round outward; zero-duration/very short items get a visible 1 ms cue.
        if turn["end"] > MAX_REPRESENTABLE_TIME / 1000:
            raise BridgeError("SRT timestamp exceeds 99:59:59.999; use JSON/Markdown")
        start = math.floor(turn["start"] * 1000)
        end = max(start + 1, math.ceil(turn["end"] * 1000))
        if end > MAX_REPRESENTABLE_TIME:
            raise BridgeError("SRT timestamp exceeds its representable range")
        precision = " [segment]" if turn["precision"] == "segment" else ""
        text = f"{_srt_literal(turn['speaker_label'])}{precision}: {_srt_literal(turn['text'])}"
        subs.append(pysubs2.SSAEvent(start=start, end=end, text=text))
        expected.append((start, end, text))
    output = subs.to_string("srt", keep_ssa_tags=True)
    reparsed = pysubs2.SSAFile.from_string(output, "srt", keep_html_tags=True)
    actual = [(event.start, event.end, event.text) for event in reparsed]
    if actual != expected:
        raise BridgeError("SRT failed pysubs2 roundtrip; use JSON to preserve this text")
    return output


def write_atomic(path: str | Path, text: str) -> None:
    """Replace one output only after a successful, flushed write in the same directory."""
    path = Path(path)
    temporary = None
    try:
        # O_EXCL on a fresh name in the same directory; mode 0o666 lets the umask apply
        # as with a plain open(), instead of mkstemp's private 0600.
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Keep the mode of a file being replaced, as an in-place truncating write would.
            os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
