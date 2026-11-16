"""Original chapter model and exact conversions; no upstream implementation used."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import re

MICROS = 1_000_000
MAX_TIME = 2**63 - 1
FORMATS = ("json", "ffmetadata", "text", "matroska")


class ChapterError(ValueError):
    """Invalid input or a conversion that requires explicit loss approval."""


class LossError(ChapterError):
    def __init__(self, losses):
        self.losses = losses
        super().__init__("export would lose information: " + "; ".join(
            f"{loss['path']}: {loss['reason']}" for loss in losses
        ) + "; use --allow-lossy with --report to retain the source in a sidecar")


def json_text(value):
    """Serialize JSON decimals exactly, including unknown numeric metadata."""
    def encode(v, level):
        indent = "  " * level
        child = "  " * (level + 1)
        if isinstance(v, dict):
            if not all(isinstance(k, str) for k in v):
                raise ChapterError("metadata keys must be strings")
            for key in v:
                if any(0xD800 <= ord(c) <= 0xDFFF for c in key):
                    raise ChapterError("metadata key contains an unpaired Unicode surrogate")
            return "{}" if not v else "{\n" + ",\n".join(
                child + json.dumps(k, ensure_ascii=False) + ": " + encode(x, level + 1)
                for k, x in v.items()) + "\n" + indent + "}"
        if isinstance(v, (list, tuple)):
            return "[]" if not v else "[\n" + ",\n".join(
                child + encode(x, level + 1) for x in v) + "\n" + indent + "]"
        if isinstance(v, Decimal):
            if not v.is_finite():
                raise ChapterError("nonfinite JSON number")
            return str(v)
        if isinstance(v, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            raise ChapterError("metadata contains an unpaired Unicode surrogate")
        if v is None or isinstance(v, (str, bool, int)):
            return json.dumps(v, ensure_ascii=False)
        raise ChapterError(f"unsupported JSON metadata type: {type(v).__name__}")
    return encode(value, 0) + "\n"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ChapterError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_json(text):
    def constant(value):
        raise ChapterError(f"nonfinite JSON number: {value}")
    try:
        return json.loads(text, parse_float=Decimal, object_pairs_hook=_pairs,
                          parse_constant=constant)
    except (ValueError, RecursionError) as exc:
        raise ChapterError(f"invalid JSON: {exc}") from exc


def seconds_us(value, label="time"):
    """No rounding: reject times that cannot be represented in microseconds."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal, Fraction)):
        raise ChapterError(f"{label} must be an exact decimal number")
    try:
        exact = Fraction(value) * MICROS
    except (ValueError, ZeroDivisionError, InvalidOperation, OverflowError) as exc:
        raise ChapterError(f"invalid {label}: {value!r}") from exc
    if exact.denominator != 1:
        raise ChapterError(f"{label} has sub-microsecond precision; rounding is unsupported")
    return _time(exact.numerator, label)


def _time(value, label):
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise ChapterError(f"{label} must be integer microseconds in [0, {MAX_TIME}]")
    return value


def seconds_number(value):
    # Decimal arithmetic obeys the caller's context; constructing a string does not.
    whole, part = divmod(value, MICROS)
    return Decimal(f"{whole}.{part:06d}")


@dataclass(eq=True)
class Chapter:
    start_us: int
    end_us: int
    title: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(eq=True)
class Document:
    chapters: list[Chapter]
    duration_us: int
    metadata: dict = field(default_factory=dict)

    def normalized(self):
        validate(self)
        return {"duration_us": self.duration_us, "metadata": self.metadata,
                "chapters": [{"start_us": c.start_us, "end_us": c.end_us,
                              "title": c.title, "metadata": c.metadata}
                             for c in self.chapters]}


def _safe_string(value, label):
    if not isinstance(value, str):
        raise ChapterError(f"{label} must be a string")
    if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ChapterError(f"{label} contains NUL or an unpaired Unicode surrogate")


def validate(doc):
    _time(doc.duration_us, "duration")
    if not doc.chapters:
        raise ChapterError("at least one chapter is required")
    if not isinstance(doc.metadata, dict):
        raise ChapterError("global metadata must be an object")
    json_text(doc.metadata)
    for i, c in enumerate(doc.chapters):
        _time(c.start_us, f"chapter {i} start")
        _time(c.end_us, f"chapter {i} end")
        _safe_string(c.title, f"chapter {i} title")
        if not isinstance(c.metadata, dict):
            raise ChapterError(f"chapter {i} metadata must be an object")
        json_text(c.metadata)
        if c.end_us <= c.start_us:
            raise ChapterError(f"chapter {i} end must be greater than start")
        if i and c.start_us <= doc.chapters[i - 1].start_us:
            raise ChapterError(f"chapter {i} starts must be strictly increasing (never sorted)")
        if i and doc.chapters[i - 1].end_us > c.start_us:
            raise ChapterError(f"chapter {i - 1} overlaps chapter {i}")
        if c.end_us > doc.duration_us:
            raise ChapterError(f"chapter {i} exceeds duration")
    return doc


def _finish(rows, metadata, duration):
    if not rows:
        raise ChapterError("at least one chapter is required")
    # Check order before inference so diagnostics never depend on sorting.
    for i, row in enumerate(rows):
        if i and row[0] <= rows[i - 1][0]:
            raise ChapterError(f"chapter {i} starts must be strictly increasing (never sorted)")
    chapters = []
    for i, (start, end, title, extra) in enumerate(rows):
        if end is None:
            end = rows[i + 1][0] if i + 1 < len(rows) else duration
        if end is None:
            raise ChapterError("missing last end and duration")
        chapters.append(Chapter(start, end, title, extra))
    # Without a supplied boundary, the last explicit end defines chapter extent,
    # not a claim about the duration of the actual media.
    return validate(Document(chapters, chapters[-1].end_us if duration is None else duration,
                             metadata))


def _parse_json(text, duration):
    data = read_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("chapters"), list):
        raise ChapterError("JSON must be an object with a chapters array")
    declared = seconds_us(data["duration"], "duration") if "duration" in data else None
    if duration is not None and declared is not None and duration != declared:
        raise ChapterError("supplied duration conflicts with JSON duration")
    duration = declared if duration is None else duration
    rows = []
    for i, c in enumerate(data["chapters"]):
        if not isinstance(c, dict) or "start_time" not in c:
            raise ChapterError(f"chapter {i} requires start_time")
        # Numeric strings are deliberately not accepted as info.json numbers.
        for key in ("start_time", "end_time"):
            if key in c and (isinstance(c[key], bool) or not isinstance(c[key], (int, Decimal))):
                raise ChapterError(f"chapter {i} {key} must be a JSON number")
        start = seconds_us(c["start_time"], f"chapter {i} start")
        end = seconds_us(c["end_time"], f"chapter {i} end") if "end_time" in c else None
        title = c.get("title", "")
        _safe_string(title, f"chapter {i} title")
        rows.append((start, end, title, {k: v for k, v in c.items()
                                      if k not in ("start_time", "end_time", "title")}))
    return _finish(rows, {k: v for k, v in data.items() if k not in ("chapters", "duration")},
                   duration)


def _ff_lines(text):
    """Yield logical lines, retaining escape sequences for the tag decoder."""
    text = text.replace("\r\n", "\n")
    if "\r" in text or "\x00" in text:
        raise ChapterError("FFmetadata contains unsupported CR or NUL")
    buf = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 == len(text):
                raise ChapterError("dangling FFmetadata escape")
            buf.extend((ch, text[i + 1]))
            i += 2
            continue
        if ch == "\n":
            yield "".join(buf)
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        yield "".join(buf)


def _ff_tag(line):
    parts = [[], []]
    side = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 1
            if i == len(line) or line[i] not in "=;#\\\n":
                raise ChapterError("malformed or unsupported FFmetadata escape")
            parts[side].append(line[i])
        elif ch == "=" and side == 0:
            side = 1
        elif ch in "=;#":
            raise ChapterError("unescaped FFmetadata delimiter")
        else:
            parts[side].append(ch)
        i += 1
    key, value = map("".join, parts)
    if side == 0 or not key:
        raise ChapterError("FFmetadata requires a nonempty key=value tag")
    return key, value


def _parse_ff(text, duration):
    lines = iter(_ff_lines(text))
    if next(lines, None) != ";FFMETADATA1":
        raise ChapterError("expected exact ;FFMETADATA1 header")
    metadata = {}
    blocks = []
    current = metadata
    for line in lines:
        if not line or line.startswith((";", "#")):
            continue
        if line == "[CHAPTER]":
            current = {}
            blocks.append(current)
            continue
        if line.startswith("[") and "=" not in line:
            raise ChapterError(f"unsupported FFmetadata section: {line!r}")
        key, value = _ff_tag(line)
        if key in current:
            raise ChapterError(f"duplicate FFmetadata key: {key!r}")
        current[key] = value
    rows = []
    for i, block in enumerate(blocks):
        raw_base = block.pop("TIMEBASE", "1/1000000000")
        if not re.fullmatch(r"[0-9]+/[0-9]+", raw_base):
            raise ChapterError(f"chapter {i} malformed timebase")
        numerator, denominator = map(int, raw_base.split("/"))
        if numerator <= 0 or denominator <= 0:
            raise ChapterError(f"chapter {i} timebase must be positive")
        base = Fraction(numerator, denominator)
        times = []
        for key in ("START", "END"):
            raw = block.pop(key, None)
            if raw is None:
                if key == "START":
                    raise ChapterError(f"chapter {i} missing START")
                times.append(None)
            elif not re.fullmatch(r"[0-9]+", raw):
                raise ChapterError(f"chapter {i} {key} must be an unsigned integer")
            else:
                times.append(seconds_us(int(raw) * base, f"chapter {i} {key}"))
        rows.append((*times, block.pop("title", ""), block))
    return _finish(rows, metadata, duration)


STAMP = re.compile(r"([0-9]{2,}):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{1,6}))?([\t ])(.*)")


def _parse_text(text, duration):
    rows = []
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise ChapterError("text contains unsupported carriage return")
    for i, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        match = STAMP.fullmatch(line)
        if not match:
            raise ChapterError(f"text line {i}: expected HH:MM:SS[.ffffff] TAB title")
        h, m, s, fraction, _, title = match.groups()
        start = (int(h) * 3600 + int(m) * 60 + int(s)) * MICROS
        start += int((fraction or "").ljust(6, "0"))
        _safe_string(title, f"text line {i} title")
        rows.append((_time(start, "text timestamp"), None, title, {}))
    return _finish(rows, {}, duration)


def loads(text, format, *, duration=None):
    """Read an explicit format. duration is exact seconds, never a binary float."""
    if not isinstance(text, str):
        raise ChapterError("input must be UTF-8 text")
    _safe_string(text, "input")
    boundary = None if duration is None else seconds_us(duration, "duration")
    from .matroska import parse as parse_matroska
    readers = {"matroska": parse_matroska, "json": _parse_json, "ffmetadata": _parse_ff, "text": _parse_text}
    if format not in readers:
        raise ChapterError(f"unsupported format: {format!r}")
    return readers[format](text, boundary)


def _escape(value):
    return "".join("\\" + c if c in "=;#\\\n" else c for c in value)


def _stamp(value):
    seconds, micros = divmod(value, MICROS)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}"


@dataclass
class Export:
    text: str
    report: dict


def compare(left, right):
    """Compare normalized content; chapter order is significant."""
    a, b = left.normalized(), right.normalized()
    differences = []
    def visit(x, y, path):
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(x.keys() | y.keys()):
                if key not in x or key not in y:
                    differences.append({"path": f"{path}/{_pointer(key)}", "kind": "missing",
                                        "left": x.get(key), "right": y.get(key)})
                else:
                    visit(x[key], y[key], f"{path}/{_pointer(key)}")
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                differences.append({"path": path, "kind": "length", "left": len(x),
                                    "right": len(y)})
            for i, (first, second) in enumerate(zip(x, y)):
                visit(first, second, f"{path}/{i}")
        elif x != y or isinstance(x, bool) != isinstance(y, bool):
            differences.append({"path": path, "kind": "changed", "left": x, "right": y})
    visit(a, b, "")
    return {"equal": not differences, "differences": differences, "left": a, "right": b}


def _pointer(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def dumps(doc, format, *, allow_lossy=False):
    """Export with a source-preserving report. Loss is rejected by default."""
    validate(doc)
    if format not in FORMATS:
        raise ChapterError(f"unsupported format: {format!r}")
    losses = []
    def loss(path, reason, value):
        losses.append({"path": path, "reason": reason, "value": value})
    def tags(metadata, path, reserved):
        result = {}
        for key, value in metadata.items():
            tagpath = f"{path}/{_pointer(key)}"
            if key in reserved:
                loss(tagpath, "key collides with destination structural field", value)
            elif format == "ffmetadata" and (not key or not isinstance(value, str)
                    or "\r" in key or (isinstance(value, str) and "\r" in value)
                    or "\x00" in key or (isinstance(value, str) and "\x00" in value)
                    or any(0xD800 <= ord(c) <= 0xDFFF for c in key + (value if isinstance(value, str) else ""))):
                loss(tagpath, "FFmetadata tags require nonempty UTF-8 keys and string values without CR/NUL", value)
            else:
                result[key] = value
        return result

    if format == "matroska":
        from .matroska import render
        output = render(doc, loss)
    elif format == "json":
        data = tags(doc.metadata, "/metadata", {"duration", "chapters"})
        data.update(duration=seconds_number(doc.duration_us), chapters=[])
        for i, c in enumerate(doc.chapters):
            row = tags(c.metadata, f"/chapters/{i}/metadata", {"start_time", "end_time", "title"})
            row.update(start_time=seconds_number(c.start_us), end_time=seconds_number(c.end_us),
                       title=c.title)
            data["chapters"].append(row)
        output = json_text(data)
    elif format == "ffmetadata":
        lines = [";FFMETADATA1"]
        for key, value in tags(doc.metadata, "/metadata", set()).items():
            lines.append(f"{_escape(key)}={_escape(value)}")
        for i, c in enumerate(doc.chapters):
            lines.extend(("[CHAPTER]", "TIMEBASE=1/1000000", f"START={c.start_us}",
                          f"END={c.end_us}"))
            title = c.title
            if "\r" in title:
                loss(f"/chapters/{i}/title", "FFmetadata cannot preserve CR", title)
                title = title.replace("\r", "")
            lines.append("title=" + _escape(title))
            for key, value in tags(c.metadata, f"/chapters/{i}/metadata",
                                   {"TIMEBASE", "START", "END", "title"}).items():
                lines.append(f"{_escape(key)}={_escape(value)}")
        if doc.duration_us != doc.chapters[-1].end_us:
            loss("/duration_us", "FFmetadata has no media duration boundary", doc.duration_us)
        output = "\n".join(lines) + "\n"
    else:
        if doc.metadata:
            loss("/metadata", "text has no global metadata", doc.metadata)
        # A standalone timestamp/title file cannot encode its final end.
        loss("/duration_us", "text requires this duration when read back", doc.duration_us)
        lines = []
        for i, c in enumerate(doc.chapters):
            if c.metadata:
                loss(f"/chapters/{i}/metadata", "text has no chapter metadata", c.metadata)
            inferred = doc.chapters[i + 1].start_us if i + 1 < len(doc.chapters) else doc.duration_us
            if c.end_us != inferred:
                loss(f"/chapters/{i}/end_us", "text infers end from next start or duration", c.end_us)
            title = c.title
            if any(ch in title for ch in "\r\n"):
                loss(f"/chapters/{i}/title", "text cannot preserve multiline titles", title)
                title = title.replace("\r", " ").replace("\n", " ")
            lines.append(f"{_stamp(c.start_us)}\t{title}")
        output = "\n".join(lines) + "\n"
    if losses and not allow_lossy:
        raise LossError(losses)
    # Verify the actual generated representation, not an independently rebuilt model.
    roundtrip = loads(output, format, duration=seconds_number(doc.duration_us) if format == "text" else None)
    comparison = compare(doc, roundtrip)
    if not comparison["equal"] and not losses:
        raise ChapterError("export changed normalized data without a loss diagnosis")
    report = {"schema_version": 1, "target_format": format, "lossless": not losses,
              "losses": losses, "source": doc.normalized(),
              "readback_duration_seconds": seconds_number(doc.duration_us) if format == "text" else None,
              "comparison": comparison}
    return Export(output, report)


def restore(report):
    """Recover the complete normalized source from a saved conversion report."""
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1):
        raise ChapterError("unsupported report schema")
    try:
        source = report["source"]
        if (not isinstance(source, dict) or not isinstance(source.get("chapters"), list)
                or not all(isinstance(c, dict) for c in source["chapters"])):
            raise ChapterError("malformed normalized source in report")
        chapters = [Chapter(c["start_us"], c["end_us"], c["title"], c["metadata"])
                    for c in source["chapters"]]
        return validate(Document(chapters, source["duration_us"], source["metadata"]))
    except (KeyError, TypeError) as exc:
        raise ChapterError("malformed normalized source in report") from exc


def check_export(report, text):
    """Compare supplied sidecar text with saved exported normalized expectations.

    Only this audit projection is validated; source and historical diagnostics
    are not consulted or authenticated. Callers parsing saved JSON should use
    read_json. Text boundaries come exclusively from the saved readback value.
    """
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1):
        raise ChapterError("unsupported report schema")
    target = report.get("target_format")
    if not isinstance(target, str) or target not in FORMATS:
        raise ChapterError("conversion report requires a supported target_format")
    try:
        expected = restore({"schema_version": 1, "source": report["comparison"]["right"]})
    except (ChapterError, KeyError, TypeError) as exc:
        raise ChapterError(f"invalid comparison.right exported expectations: {exc}") from exc
    boundary = report.get("readback_duration_seconds")
    if target == "text":
        if isinstance(boundary, bool) or not isinstance(boundary, (int, Decimal)):
            raise ChapterError("text requires numeric readback_duration_seconds")
        if seconds_us(boundary, "readback_duration_seconds") != expected.duration_us:
            raise ChapterError("readback_duration_seconds conflicts with comparison.right duration_us")
    elif boundary is not None:
        raise ChapterError("readback_duration_seconds must be null for non-text exports")
    actual = loads(text, target, duration=boundary)
    comparison = compare(expected, actual)
    equal = comparison["equal"]
    return {"schema_version": 1, "kind": "exported-sidecar-normalized",
            "scope": "normalized saved export equality only; no source, byte or authenticity verification",
            "target_format": target, "readback_duration_seconds": boundary,
            "status": "passed" if equal else "different", "equal": equal,
            "comparison": comparison}
