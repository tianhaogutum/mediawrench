"""Adversarial valid XML representations and field values."""

from __future__ import annotations

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.xmp import NS, Packet, qname
from .helpers import packet_bytes


def test_new_culling_namespace_does_not_rebind_existing_vendor_prefix():
    data = b"""<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:xmp="urn:other-application">
      <r:Description r:about=""><xmp:Rating>foreign rating</xmp:Rating></r:Description>
    </r:RDF>"""
    packet = Packet.parse(data)
    result = Packet.parse(packet.render(packet.values | {"rating": 3}))
    assert result.values["rating"] == 3
    assert result.tree.find(".//{urn:other-application}Rating").text == "foreign rating"
    assert result.protected_signature() == packet.protected_signature()


def test_comments_inside_simple_scalar_are_not_a_structured_property():
    root = etree.fromstring(packet_bytes())
    description = root.find(".//rdf:Description", NS)
    label = etree.SubElement(description, qname("xmp", "Label"))
    label.text = "Re"
    label.append(etree.Comment("a legal XML comment"))
    label[-1].tail = "d"
    packet = Packet.parse(etree.tostring(root))
    assert packet.values["label"] == "Red"
    result = Packet.parse(packet.render(packet.values | {"rating": 4}))
    assert result.values["label"] == "Red"


def test_comments_in_color_sequence_and_values_are_accepted():
    root = etree.fromstring(packet_bytes(darktable_labels=[0, 4]))
    sequence = root.find(".//darktable:colorlabels/rdf:Seq", NS)
    sequence.insert(0, etree.Comment("red and purple"))
    sequence[-1].append(etree.Comment("purple"))
    packet = Packet.parse(etree.tostring(root))
    assert packet.values["darktable_labels"] == [0, 4]
