"""Bounded MKVToolNix 92.0 chapter XML adapter; no DTD or network loading.

Only one unordered edition of flat enabled chapters is supported.
The canonical model keeps format-specific identities/displays, chapter hidden
state and the explicit hidden/default edition flags in metadata.
"""
import re
import xml.etree.ElementTree as ET

from .core import ChapterError, _finish, _pointer, _stamp

UINT64 = 2**64 - 1
TIME = re.compile(r"([0-9]{2,}):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{1,9}))?")
# A bounded lexical subset, not a language-registry lookup or normalization.
IETF = re.compile(r"(?:[A-Za-z]{2,8}|[xX])(?:-[A-Za-z0-9]{1,8})*")


class _TreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ChapterError("Matroska XML DTD declarations are unsupported")

    def pi(self, target, text):
        raise ChapterError("Matroska XML processing instructions are unsupported")


def _children(node, allowed, repeated=()):
    if node.attrib:
        raise ChapterError(f"XML attributes are unsupported on {node.tag}")
    if (node.text or "").strip() or any((child.tail or "").strip() for child in node):
        raise ChapterError(f"unexpected mixed text in {node.tag}")
    result = {}
    for child in node:
        if child.tag not in allowed:
            raise ChapterError(f"unsupported {node.tag} child: {child.tag}")
        if child.tag in result and child.tag not in repeated:
            raise ChapterError(f"duplicate {child.tag}")
        result.setdefault(child.tag, []).append(child)
    return result


def _scalar(node):
    if node.attrib or len(node):
        raise ChapterError(f"{node.tag} must contain plain text without attributes")
    return node.text or ""


def _value(children, name, default=None):
    return _scalar(children[name][0]).strip() if name in children else default


def _uid(value, label):
    if type(value) is not int or not 1 <= value <= UINT64:
        raise ChapterError(f"{label} must be an integer in [1, {UINT64}]")
    return value


def _read_uid(raw, label):
    if not re.fullmatch(r"[0-9]+", raw):
        raise ChapterError(f"{label} must be an unsigned integer")
    significant = raw.lstrip("0") or "0"
    if len(significant) > 20:
        raise ChapterError(f"{label} exceeds unsigned 64-bit range")
    return _uid(int(significant), label)


def _time(raw):
    if raw is None or not (match := TIME.fullmatch(raw)):
        raise ChapterError("Matroska time requires HH:MM:SS[.nnnnnnnnn]")
    h, m, s, fraction = match.groups()
    h = h.lstrip("0") or "0"
    if len(h) > 7:
        raise ChapterError("Matroska time exceeds unsigned 64-bit nanoseconds")
    ns = (int(h) * 3600 + int(m) * 60 + int(s)) * 1_000_000_000
    ns += int((fraction or "").ljust(9, "0"))
    if ns > UINT64:
        raise ChapterError("Matroska time exceeds unsigned 64-bit nanoseconds")
    if ns % 1000:
        raise ChapterError("Matroska time has sub-microsecond precision; rounding is unsupported")
    return ns // 1000


def _default_displays(title):
    return [{"string": title, "language": "eng"}]


def _xml_string(value):
    if not isinstance(value, str) or any(not (c in "\t\n\r" or
            0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
            0x10000 <= ord(c) <= 0x10FFFF) for c in value):
        raise ChapterError("Matroska display string must contain only XML 1.0 characters")


def _displays(displays):
    if not isinstance(displays, list) or not displays:
        raise ChapterError("matroska displays must be a nonempty list")
    seen = set()
    for display in displays:
        if not isinstance(display, dict) or not {"string", "language"} <= display.keys() or (
                display.keys() - {"string", "language", "language_ietf"}):
            raise ChapterError("display requires string/language and optional language_ietf only")
        _xml_string(display["string"])
        language = display["language"]
        ietf = display.get("language_ietf")
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{3}", language):
            raise ChapterError("ChapterLanguage requires three lowercase ASCII letters")
        if "language_ietf" in display and (not isinstance(ietf, str) or not IETF.fullmatch(ietf)
                                 or ietf.lower() == "x"):
            raise ChapterError("ChapLanguageIETF is outside the supported language-tag syntax")
        identity = (language, ietf.lower() if ietf else None)
        if identity in seen:
            raise ChapterError("duplicate display language identity")
        seen.add(identity)


def parse(text, duration):
    try:
        root = ET.fromstring(text, parser=ET.XMLParser(target=_TreeBuilder()))
    except ET.ParseError as exc:
        raise ChapterError(f"invalid Matroska XML: {exc}") from exc
    if root.tag != "Chapters":
        raise ChapterError("expected unnamespaced Chapters root")
    editions = _children(root, {"EditionEntry"}, {"EditionEntry"}).get("EditionEntry", [])
    if len(editions) != 1:
        raise ChapterError("Matroska XML requires exactly one EditionEntry")
    edition = _children(editions[0], {"EditionUID", "EditionFlagHidden", "EditionFlagOrdered",
                                    "EditionFlagDefault", "ChapterAtom"}, {"ChapterAtom"})
    if _value(edition, "EditionFlagOrdered", "0") != "0":
        raise ChapterError("unsupported EditionFlagOrdered; only default 0 is supported")
    extra = {}
    if "EditionUID" in edition:
        extra["edition_uid"] = _read_uid(_value(edition, "EditionUID"), "EditionUID")
    for flag, key in (("EditionFlagHidden", "edition_hidden"),
                      ("EditionFlagDefault", "edition_default")):
        value = _value(edition, flag, "0")
        if value not in ("0", "1"):
            raise ChapterError(f"{flag} must be 0 or 1")
        if value == "1":
            extra[key] = 1
    rows, seen = [], set()
    for node in edition.get("ChapterAtom", []):
        fields = _children(node, {"ChapterTimeStart", "ChapterTimeEnd", "ChapterUID",
                                 "ChapterFlagHidden", "ChapterFlagEnabled", "ChapterDisplay"},
                           {"ChapterDisplay"})
        if _value(fields, "ChapterFlagEnabled", "1") != "1":
            raise ChapterError("unsupported ChapterFlagEnabled; only default 1 is supported")
        info = {}
        hidden = _value(fields, "ChapterFlagHidden", "0")
        if hidden not in ("0", "1"):
            raise ChapterError("ChapterFlagHidden must be 0 or 1")
        if hidden == "1":
            info["hidden"] = 1
        if "ChapterUID" in fields:
            uid = _read_uid(_value(fields, "ChapterUID"), "ChapterUID")
            if uid in seen:
                raise ChapterError(f"duplicate ChapterUID: {uid}")
            seen.add(uid)
            info["uid"] = uid
        displays = []
        for display in fields.get("ChapterDisplay", []):
            parts = _children(display, {"ChapterString", "ChapterLanguage", "ChapLanguageIETF"})
            if "ChapterString" not in parts:
                raise ChapterError("ChapterDisplay requires ChapterString")
            entry = {"string": _scalar(parts["ChapterString"][0]),
                     "language": _value(parts, "ChapterLanguage", "eng")}
            if "ChapLanguageIETF" in parts:
                entry["language_ietf"] = _value(parts, "ChapLanguageIETF")
            displays.append(entry)
        if displays:
            _displays(displays)
        title = displays[0]["string"] if displays else ""
        if displays and displays != _default_displays(title):
            info["displays"] = displays
        start = _time(_value(fields, "ChapterTimeStart"))
        end = _time(_value(fields, "ChapterTimeEnd")) if "ChapterTimeEnd" in fields else None
        rows.append((start, end, title, {"matroska": info} if info else {}))
    doc = _finish(rows, {"matroska": extra} if extra else {}, duration)
    # Inferred ends must also fit the actual upstream time range.
    if any(c.end_us * 1000 > UINT64 for c in doc.chapters):
        raise ChapterError("Matroska time exceeds unsigned 64-bit nanoseconds")
    return doc


def _scope(metadata, allowed, path, loss):
    for key, value in metadata.items():
        if key != "matroska":
            loss(f"{path}/{_pointer(key)}", "Matroska chapter XML has no arbitrary metadata tags", value)
    info = metadata.get("matroska", {})
    if not isinstance(info, dict) or info.keys() - allowed:
        raise ChapterError(f"unsupported matroska metadata fields at {path}")
    if "matroska" in metadata and not info:
        raise ChapterError(f"omit empty matroska metadata at {path}")
    return info


def render(doc, loss):
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")
    info = _scope(doc.metadata, {"edition_uid", "edition_hidden", "edition_default"}, "/metadata", loss)
    if "edition_uid" in info:
        ET.SubElement(edition, "EditionUID").text = str(_uid(info["edition_uid"], "EditionUID"))
    for key, flag in (("edition_hidden", "EditionFlagHidden"),
                      ("edition_default", "EditionFlagDefault")):
        if key in info:
            if type(info[key]) is not int or info[key] != 1:
                raise ChapterError(f"matroska {key} must be integer 1; omit it for default 0")
            ET.SubElement(edition, flag).text = "1"
    seen = set()
    for i, chapter in enumerate(doc.chapters):
        path = f"/chapters/{i}/metadata"
        info = _scope(chapter.metadata, {"uid", "hidden", "displays"}, path, loss)
        if chapter.end_us * 1000 > UINT64:
            raise ChapterError("Matroska time exceeds unsigned 64-bit nanoseconds")
        node = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(node, "ChapterTimeStart").text = _stamp(chapter.start_us)
        ET.SubElement(node, "ChapterTimeEnd").text = _stamp(chapter.end_us)
        if "uid" in info:
            uid = _uid(info["uid"], "ChapterUID")
            if uid in seen:
                raise ChapterError(f"duplicate ChapterUID: {uid}")
            seen.add(uid)
            ET.SubElement(node, "ChapterUID").text = str(uid)
        if "hidden" in info:
            if type(info["hidden"]) is not int or info["hidden"] != 1:
                raise ChapterError("matroska hidden must be integer 1; omit it for default 0")
            ET.SubElement(node, "ChapterFlagHidden").text = "1"
        displays = info.get("displays", _default_displays(chapter.title))
        _displays(displays)
        if displays[0]["string"] != chapter.title:
            raise ChapterError("first matroska display string must equal chapter title")
        if "displays" in info and displays == _default_displays(chapter.title):
            raise ChapterError("omit default single English matroska displays in normalized metadata")
        for display in displays:
            entry = ET.SubElement(node, "ChapterDisplay")
            ET.SubElement(entry, "ChapterString").text = display["string"]
            ET.SubElement(entry, "ChapterLanguage").text = display["language"]
            if "language_ietf" in display:
                ET.SubElement(entry, "ChapLanguageIETF").text = display["language_ietf"]
    if doc.duration_us != doc.chapters[-1].end_us:
        loss("/duration_us", "Matroska chapter XML has no media duration boundary", doc.duration_us)
    ET.indent(root, space="  ")
    # XML normalizes literal CR on read; preserve it through a numeric reference.
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode", short_empty_elements=False).replace("\r", "&#13;") + "\n"
