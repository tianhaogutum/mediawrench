"""Namespace-aware, allowlisted edits; XML parsing/serialization belongs to lxml."""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from lxml import etree

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "x": "adobe:ns:meta/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/",
    "darktable": "http://darktable.sf.net/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
}
MAX_BYTES = 16 * 1024 * 1024
MAX_ELEMENTS = 100_000
MAX_ATTRIBUTES = 300_000
SIDES = ("base", "left", "right")
FINGERPRINT_VERSION = "xml-tree-sha256-v3"


class MergeError(Exception):
    """An input, decision, or preservation invariant prevents a safe output."""


def qname(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def _element_children(node: etree._Element) -> list[etree._Element]:
    return [child for child in node if isinstance(child.tag, str)]


@dataclass(frozen=True)
class Field:
    name: str
    prefix: str
    local: str
    kind: str

    @property
    def tag(self) -> str:
        return qname(self.prefix, self.local)

    @property
    def display(self) -> str:
        return f"{self.prefix}:{self.local}"


FIELDS = (
    Field("rating", "xmp", "Rating", "rating"),
    Field("label", "xmp", "Label", "text"),
    Field("darktable_labels", "darktable", "colorlabels", "colors"),
    Field("pick", "xmpDM", "pick", "pick"),
    Field("digikam_pick", "digiKam", "PickLabel", "digikam_pick"),
    Field("digikam_color", "digiKam", "ColorLabel", "digikam_color"),
)
FIELD_BY_NAME = {field.name: field for field in FIELDS}
ALLOWED_TAGS = frozenset(field.tag for field in FIELDS)
DESCRIPTION = qname("rdf", "Description")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path, max_bytes: int | None = None) -> bytes:
    """Bound memory and detect concurrent modification during the read."""
    try:
        limit = MAX_BYTES if max_bytes is None else max_bytes
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = source.fileno()
            metadata = os.fstat(before)
            if not stat.S_ISREG(metadata.st_mode):
                raise MergeError(f"{path}: expected a regular sidecar file")
            if metadata.st_size > limit:
                raise MergeError(f"{path}: file exceeds the {limit}-byte size limit")
            data = source.read(limit + 1)
            after = os.fstat(before)
            if len(data) > limit:
                raise MergeError(f"{path}: file exceeds the {limit}-byte size limit")
            if (metadata.st_mtime_ns, metadata.st_size) != (after.st_mtime_ns, after.st_size):
                raise MergeError(f"{path}: sidecar changed while being read; try again")
            return data
    except OSError as exc:
        raise MergeError(f"{path}: {exc.strerror}") from exc


def _scalar(raw: str, field: Field) -> Any:
    if field.kind == "text":
        if len(raw) > 4096:
            raise MergeError(f"{field.display}: label exceeds 4096 characters")
        return raw  # Empty and whitespace-only labels are not missing properties.
    value = raw.strip()
    if field.kind == "rating":
        # XMP Basic specifies a Real, not only an integer. Decimal avoids rounding
        # independent culling decisions into a false agreement.
        if len(value) > 128 or not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value
        ):
            raise MergeError(f"{field.display}: expected a finite numeric rating, got {raw!r}")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise MergeError(f"{field.display}: invalid numeric rating {raw!r}") from exc
        if number != -1 and not 0 <= number <= 5:
            raise MergeError(f"{field.display}: expected -1 or a value in [0, 5], got {raw!r}")
        return int(number) if number == number.to_integral_value() else number
    if not re.fullmatch(r"[+-]?[0-9]+", value) or len(value) > 32:
        raise MergeError(f"{field.display}: expected an integer, got {raw!r}")
    try:
        number = int(value)
    except ValueError as exc:
        raise MergeError(f"{field.display}: expected an integer, got {raw!r}") from exc
    valid = {
        "pick": range(-1, 2),
        "digikam_pick": range(0, 4),
        "digikam_color": range(0, 10),
    }.get(field.kind)
    if valid is None or number not in valid:
        raise MergeError(f"{field.display}: unsupported value {raw!r}")
    return number


@dataclass
class Packet:
    path: Path
    data: bytes
    tree: etree._ElementTree
    descriptions: list[etree._Element]
    values: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Packet:
        return cls.parse(read_bytes(path), path)

    @classmethod
    def parse(cls, data: bytes, path: Path = Path("<memory>")) -> Packet:
        if len(data) > MAX_BYTES:
            raise MergeError(f"{path}: sidecar exceeds the size limit")
        # libxml2's pull parser does not autodetect UTF-32 on this platform even
        # though its decoder supports it. Delegate decoding after the XML-standard
        # BOM/initial-'<' byte pattern identifies the byte order.
        encoding = None
        if data.startswith(codecs.BOM_UTF32_BE) or data.startswith(b"\x00\x00\x00<"):
            encoding = "UTF-32BE"
        elif data.startswith(codecs.BOM_UTF32_LE) or data.startswith(b"<\x00\x00\x00"):
            encoding = "UTF-32LE"
        parser = etree.XMLPullParser(
            events=("start",),
            encoding=encoding,
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
            remove_blank_text=False,
            remove_comments=False,
            strip_cdata=False,
        )
        try:
            elements = attributes = 0
            for offset in range(0, len(data), 65536):
                parser.feed(data[offset : offset + 65536])
                for _, element in parser.read_events():
                    elements += 1
                    attributes += len(element.attrib)
                    if elements > MAX_ELEMENTS or attributes > MAX_ATTRIBUTES:
                        raise MergeError(
                            f"{path}: XML complexity exceeds {MAX_ELEMENTS} elements or "
                            f"{MAX_ATTRIBUTES} attributes"
                        )
            root = parser.close()
            tree = root.getroottree()
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise MergeError(f"{path}: invalid XML: {exc}") from exc
        if tree.docinfo.doctype:
            raise MergeError(f"{path}: DTD/entity declarations are not supported")
        if root.tag == qname("rdf", "RDF"):
            rdf = root
        elif root.tag == qname("x", "xmpmeta"):
            candidates = root.findall(qname("rdf", "RDF"))
            if len(candidates) != 1:
                raise MergeError(f"{path}: expected exactly one rdf:RDF packet")
            rdf = candidates[0]
        else:
            raise MergeError(f"{path}: expected x:xmpmeta or rdf:RDF root")
        descriptions = [node for node in rdf if node.tag == DESCRIPTION]
        if not descriptions:
            raise MergeError(f"{path}: missing top-level rdf:Description")
        for node in rdf:
            if isinstance(node.tag, str) and node.tag != DESCRIPTION:
                raise MergeError(f"{path}: unsupported top-level RDF node {node.tag}")
        subjects = {node.get(qname("rdf", "about"), "") for node in descriptions}
        if len(subjects) != 1:
            raise MergeError(f"{path}: multiple RDF subjects are not supported")
        for node in descriptions:
            if node.get(qname("rdf", "nodeID")) or node.get(qname("rdf", "ID")):
                raise MergeError(f"{path}: named/blank-node RDF subjects are not supported")
        packet = cls(path, data, tree, descriptions, {})
        for field in FIELDS:
            packet.values[field.name] = packet._read_field(field)
        return packet

    @property
    def sha256(self) -> str:
        return digest(self.data)

    @property
    def subject(self) -> str:
        return self.descriptions[0].get(qname("rdf", "about"), "")

    def occurrences(self, tag: str) -> list[tuple[etree._Element, bool]]:
        result = []
        for description in self.descriptions:
            if tag in description.attrib:
                result.append((description, True))
            result.extend((child, False) for child in description if child.tag == tag)
        return result

    def _read_field(self, field: Field) -> Any:
        found = self.occurrences(field.tag)
        if not found:
            return None
        if len(found) != 1:
            raise MergeError(f"{self.path}: duplicate {field.display} properties are ambiguous")
        node, attribute = found[0]
        if field.kind == "colors":
            children = _element_children(node)
            if attribute or node.attrib or len(children) != 1 or (node.text or "").strip():
                raise MergeError(f"{self.path}: {field.display} must contain one rdf:Seq")
            if any((child.tail or "").strip() for child in node):
                raise MergeError(f"{self.path}: unexpected text outside the color label sequence")
            seq = children[0]
            if seq.tag != qname("rdf", "Seq") or seq.attrib or (seq.text or "").strip():
                raise MergeError(f"{self.path}: {field.display} must contain an rdf:Seq")
            colors = []
            for item in seq:
                if (item.tail or "").strip():
                    raise MergeError(f"{self.path}: unexpected text between color label items")
                if not isinstance(item.tag, str):
                    continue  # Comments and processing instructions are not RDF list items.
                if item.tag != qname("rdf", "li") or item.attrib or _element_children(item):
                    raise MergeError(f"{self.path}: unsupported color label list item")
                raw = "".join(item.itertext()).strip()
                if len(raw) > 32 or not re.fullmatch(r"[+-]?[0-9]+", raw):
                    raise MergeError(f"{self.path}: color labels must be integers 0–4")
                try:
                    number = int(raw)
                except ValueError as exc:
                    raise MergeError(f"{self.path}: color labels must be integers 0–4") from exc
                if number not in range(5):
                    raise MergeError(f"{self.path}: color labels must be integers 0–4")
                colors.append(number)
            # darktable color labels are a set, even though Exiv2 emits rdf:Seq.
            return sorted(set(colors))
        if attribute:
            return _scalar(node.attrib[field.tag], field)
        if node.attrib or _element_children(node):
            raise MergeError(f"{self.path}: qualified/structured {field.display} is unsupported")
        return _scalar("".join(node.itertext()), field)

    def simple_property(self, tag: str) -> str | None:
        values = []
        for node, attribute in self.occurrences(tag):
            if attribute:
                values.append(node.attrib[tag])
            elif not _element_children(node) and not node.attrib:
                values.append("".join(node.itertext()))
        nonempty = {value.strip() for value in values if value.strip()}
        if len(nonempty) > 1:
            raise MergeError(f"{self.path}: conflicting identity property {tag}")
        return next(iter(nonempty), None)

    def protected_signature(self) -> str:
        """Hash namespace-expanded tree data except the direct allowlisted properties.

        This is intentionally stricter than general RDF semantic equality: unknown
        properties, order, comments, history attributes, and empty containers survive.
        Namespace context is retained because opaque metadata can use QName-valued
        text. Insignificant indentation is ignored, respecting mixed text/xml:space.
        """
        descriptions = set(self.descriptions)
        xml_space = "{http://www.w3.org/XML/1998/namespace}space"
        fingerprint = hashlib.sha256()

        def token(kind: bytes, value: str = "") -> None:
            # Length framing prevents text/boundary collisions without building
            # another complete Python tree or a large serialized JSON string.
            encoded = value.encode("utf-8")
            fingerprint.update(kind)
            fingerprint.update(str(len(encoded)).encode("ascii"))
            fingerprint.update(b":")
            fingerprint.update(encoded)

        def visit(
            node: etree._Element,
            inherited_space: bool = False,
            inherited_namespaces: dict[str | None, str] | None = None,
        ) -> None:
            if isinstance(node, etree._Comment):
                token(b"C", node.text or "")
                return
            if isinstance(node, etree._ProcessingInstruction):
                token(b"P", node.target)
                token(b"D", node.text or "")
                return
            is_description = node in descriptions
            token(b"E", node.tag)
            namespaces = node.nsmap
            previous_namespaces = inherited_namespaces or {}
            prefixes = set(namespaces) | set(previous_namespaces)
            for prefix in sorted(prefixes, key=lambda value: value or ""):
                if namespaces.get(prefix) != previous_namespaces.get(prefix):
                    token(b"N", prefix or "")
                    token(b"U", namespaces.get(prefix) or "")
            token(b"n")
            attrs = sorted(
                (key, value)
                for key, value in node.attrib.items()
                if not (is_description and key in ALLOWED_TAGS)
            )
            for key, value in attrs:
                token(b"A", key)
                token(b"V", value)
            token(b"a")
            space = node.get(xml_space)
            preserve_space = inherited_space if space is None else space == "preserve"
            mixed = bool((node.text or "").strip()) or any(
                (child.tail or "").strip() for child in node
            )
            keep_whitespace = preserve_space or mixed or (not len(node) and not is_description)
            pending_text: list[str] = []

            def append_text(text: str | None) -> None:
                if text is None or not (text.strip() or keep_whitespace):
                    return
                pending_text.append(text)

            def flush_text() -> None:
                if pending_text:
                    token(b"T", "".join(pending_text))
                    pending_text.clear()

            append_text(node.text)
            for child in node:
                if not (is_description and child.tag in ALLOWED_TAGS):
                    flush_text()
                    visit(child, preserve_space, namespaces)
                append_text(child.tail)
            flush_text()
            token(b"e")

        root = self.tree.getroot()
        before = []
        previous = root.getprevious()
        while previous is not None:
            before.append(previous)
            previous = previous.getprevious()
        after = []
        following = root.getnext()
        while following is not None:
            after.append(following)
            following = following.getnext()
        token(b"S", FINGERPRINT_VERSION)
        try:
            for node in [*reversed(before), root, *after]:
                visit(node)
            return fingerprint.hexdigest()
        finally:
            # This recursive closure captures element handles owning native XML
            # trees. Break its self-reference now, rather than waiting for cyclic
            # GC (which cannot account for the native allocation sizes).
            visit = None

    def render(self, values: dict[str, Any]) -> bytes:
        """Apply selected values to a copy and reparse/check every unrelated property."""
        if values == self.values:
            return self.data
        self._check_packet_rewrite()
        data = self._serialize_values(values)
        # The temporary edited tree and its element handles have gone out of
        # scope before reparsing. Large histories otherwise retain five trees.
        result = Packet.parse(data, Path("<merged>"))
        if result.values != values:
            raise MergeError("internal check failed: serialized culling values differ")
        if result.protected_signature() != self.protected_signature():
            raise MergeError("preservation check failed: non-culling XML would change")
        return data

    def _check_packet_rewrite(self) -> None:
        # Old xpacket headers may declare a byte count or character encoding.
        # A tree rewrite changes length/encoding; keeping a stale hint would be
        # misleading, while changing the PI violates the preservation contract.
        root = self.tree.getroot()
        for preceding in (True, False):
            node = root.getprevious() if preceding else root.getnext()
            while node is not None:
                if isinstance(node, etree._ProcessingInstruction) and node.target == "xpacket":
                    try:
                        header = etree.fromstring(
                            f"<packet {node.text or ''}/>".encode(),
                            etree.XMLParser(
                                resolve_entities=False, load_dtd=False, no_network=True
                            ),
                        )
                    except etree.XMLSyntaxError as exc:
                        raise MergeError(
                            "unsupported xpacket header syntax; no output written"
                        ) from exc
                    encoding = header.get("encoding", "UTF-8").replace("-", "").upper()
                    if "bytes" in header.attrib or encoding != "UTF8":
                        raise MergeError(
                            "legacy xpacket byte-count/non-UTF8 encoding hints cannot be preserved "
                            "during rewriting; use normalized sidecars from the editor"
                        )
                node = node.getprevious() if preceding else node.getnext()

    def _serialize_values(self, values: dict[str, Any]) -> bytes:
        clone = Packet.parse(self.data, self.path)
        for field in FIELDS:
            value = values[field.name]
            if value == clone.values[field.name]:
                continue
            found = clone.occurrences(field.tag)
            if found:
                node, attribute = found[0]
                if attribute:
                    if value is None:
                        del node.attrib[field.tag]
                    else:
                        node.set(field.tag, str(value))
                    continue
                if value is None:
                    # An element's tail belongs to the surrounding XML, not to
                    # the removed culling property. lxml otherwise removes it.
                    previous = node.getprevious()
                    parent = node.getparent()
                    if previous is not None:
                        previous.tail = (previous.tail or "") + (node.tail or "")
                    else:
                        parent.text = (parent.text or "") + (node.tail or "")
                    parent.remove(node)
                    continue
                node.clear(keep_tail=True)
            elif value is None:
                continue
            else:
                # Local namespace declarations avoid changing any protected element.
                node = etree.SubElement(
                    clone.descriptions[0], field.tag, nsmap={field.prefix: NS[field.prefix]}
                )
            if field.kind == "colors":
                seq = etree.SubElement(node, qname("rdf", "Seq"))
                for number in value:
                    etree.SubElement(seq, qname("rdf", "li")).text = str(number)
            else:
                node.text = str(value)
        return etree.tostring(
            clone.tree, encoding="UTF-8", xml_declaration=True, pretty_print=False
        )


def check_identity(packets: dict[str, Packet]) -> list[str]:
    warnings = []
    subjects = {packet.subject for packet in packets.values()}
    if len(subjects) != 1:
        raise MergeError("RDF subjects differ: these sidecars may belong to different images")
    common_strong_evidence = False
    for prefix, local in (
        ("xmpMM", "OriginalDocumentID"),
        ("exif", "ImageUniqueID"),
    ):
        values = {
            side: packet.simple_property(qname(prefix, local)) for side, packet in packets.items()
        }
        present = {value for value in values.values() if value is not None}
        if len(present) > 1:
            raise MergeError(f"{prefix}:{local} differs between inputs: {values}")
        if all(value is not None for value in values.values()):
            common_strong_evidence = True
    common_filename = False
    for prefix, local in (("crs", "RawFileName"), ("xmpMM", "DerivedFrom")):
        values = {
            side: packet.simple_property(qname(prefix, local)) for side, packet in packets.items()
        }
        names = {side: _filename_hint(value) for side, value in values.items()}
        present = {value for value in names.values() if value is not None}
        if len(present) > 1:
            if not common_strong_evidence:
                raise MergeError(f"{prefix}:{local} photo names differ between inputs: {values}")
            warnings.append(
                f"{prefix}:{local} names differ, but all inputs share a stable original ID. "
                "Retaining the selected history source's identity metadata."
            )
        elif all(value is not None for value in names.values()):
            common_filename = True
    if not common_strong_evidence and not common_filename:
        warnings.append(
            "No shared stable photo identifier; verify all three sidecars name the same photo."
        )
    elif not common_strong_evidence:
        warnings.append(
            "Only photo filename hints match; no shared stable original ID proves image identity."
        )
    return warnings


def _filename_hint(value: str | None) -> str | None:
    if value is None:
        return None
    # Different computers may serialize the same photo using different folders
    # or Windows/POSIX separators. A filename is evidence, never a content hash.
    if value.lower().startswith("file:"):
        try:
            value = unquote(urlsplit(value).path, errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise MergeError(f"invalid photo file URL {value!r}") from exc
    name = PurePosixPath(value.replace("\\", "/")).name
    return unicodedata.normalize("NFC", name) or None


def representation_warnings(packets: dict[str, Packet], history_source: str) -> list[str]:
    aliases = {
        "{http://ns.acdsee.com/iptc/1.0/}rating": "acdsee:rating",
        "{http://ns.microsoft.com/photo/1.0/}Rating": "MicrosoftPhoto:Rating",
    }
    if any(packet.values["digikam_color"] is not None for packet in packets.values()):
        aliases["{http://ns.adobe.com/photoshop/1.0/}Urgency"] = "photoshop:Urgency"
    warnings = []
    for tag, display in aliases.items():
        sides = [side for side, packet in packets.items() if packet.occurrences(tag)]
        if sides:
            warnings.append(
                f"{display} is present in {', '.join(sides)} and can be a vendor culling "
                f"representation. It is outside the allowlist and retained from {history_source}; "
                "no alias synchronization was applied."
            )
    return warnings
