"""Shared helpers for the cull tests (not a test module): synthetic XMP packets and a
subprocess environment that imports the repository's ``src``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.xmp import FIELD_BY_NAME, NS, qname

ROOT = Path(__file__).resolve().parents[2]


def subprocess_env(**extra: str) -> dict[str, str]:
    """Environment for ``python -m mediawrench cull`` child processes."""
    path = os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))
    return {**os.environ, "PYTHONPATH": path, **extra}


def packet_bytes(**values):
    root = etree.Element(qname("x", "xmpmeta"), nsmap=NS)
    rdf = etree.SubElement(root, qname("rdf", "RDF"))
    description = etree.SubElement(rdf, qname("rdf", "Description"))
    description.set(qname("rdf", "about"), "")
    description.set(qname("xmpMM", "OriginalDocumentID"), "uuid:photo-001")
    history = etree.SubElement(description, qname("darktable", "history"))
    seq = etree.SubElement(history, qname("rdf", "Seq"))
    item = etree.SubElement(seq, qname("rdf", "li"))
    item.set(qname("darktable", "operation"), "exposure")
    item.set(qname("darktable", "params"), "gz11eJxjYGBgkGAAgRNODGiAEV0AJ2iw==")
    for name, value in values.items():
        field = FIELD_BY_NAME[name]
        if value is None:
            continue
        if field.kind == "colors":
            node = etree.SubElement(description, field.tag)
            labels = etree.SubElement(node, qname("rdf", "Seq"))
            for number in value:
                etree.SubElement(labels, qname("rdf", "li")).text = str(number)
        else:
            description.set(field.tag, str(value))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
