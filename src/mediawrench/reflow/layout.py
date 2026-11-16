"""At most two lines, with grapheme-safe breaks and selectable width units."""
import re

from wcwidth import iter_graphemes, wcswidth

from .model import InputError


def is_cjk(char: str) -> bool:
    cp = ord(char)
    return (0x2E80 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7FF
            or 0x1100 <= cp <= 0x11FF or 0xA960 <= cp <= 0xA97F
            or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x323AF
            or 0x3040 <= cp <= 0x30FF)


def display_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def grapheme_unit_stops(texts: list[str]) -> list[int]:
    """Return unit indices whose text offsets are extended grapheme boundaries."""
    ends = {}
    offset = 0
    for i, text in enumerate(texts, 1):
        offset += len(text)
        ends[offset] = i
    points = [0]
    offset = 0
    for cluster in iter_graphemes("".join(texts)):
        offset += len(cluster)
        if offset in ends:
            points.append(ends[offset])
    return points


def _cluster_width(cluster: str, width_mode: str) -> int:
    if width_mode == "codepoint":
        return len(cluster)
    if width_mode != "display":
        raise InputError("width_mode must be codepoint or display")
    # Explicit ambiguous-width policy; no terminal/font/environment detection.
    width = wcswidth(cluster, ambiguous_width=1)
    if width < 0:
        raise InputError("display width is undefined for control characters")
    return width


def measure_width(text: str, width_mode: str = "codepoint") -> int:
    """Measure rendered text; callers perform whitespace normalization first."""
    if width_mode == "codepoint":
        return len(text)
    return sum(_cluster_width(cluster, width_mode) for cluster in iter_graphemes(text))


def layout(text: str, max_chars: int, width_mode: str = "codepoint") -> tuple[list[str], int]:
    text = display_text(text)
    # Prefix widths are defined only at extended grapheme boundaries. In display
    # mode each cluster contributes its pinned wcwidth cell estimate.
    clusters = list(iter_graphemes(text))
    offsets = [0]
    widths = [0]
    for cluster in clusters:
        offsets.append(offsets[-1] + len(cluster))
        widths.append(widths[-1] + _cluster_width(cluster, width_mode))
    total = widths[-1]
    if total <= max_chars:
        return [text], 0
    best = None
    split = None
    # Evaluate widths without allocating every possible pair of substrings.
    for i in range(1, len(clusters)):
        if clusters[i] == " ":
            left_end, right_start = offsets[i], offsets[i + 1]
            left_width, right_width = widths[i], total - widths[i + 1]
        elif is_cjk(clusters[i - 1][0]) and is_cjk(clusters[i][0]):
            left_end = right_start = offsets[i]
            left_width, right_width = widths[i], total - widths[i]
        else:
            continue
        if left_end and right_start < len(text):
            overflow = max(0, left_width - max_chars) + max(0, right_width - max_chars)
            score = (overflow, abs(left_width - right_width), offsets[i])
            if best is None or score < best:
                best, split = score, (left_end, right_start)
    if split is not None:
        return [text[:split[0]], text[split[1]:]], best[0]
    return [text], max(0, total - max_chars)
