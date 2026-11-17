from __future__ import annotations

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.xmp import NS, MergeError, Packet, check_identity, qname
from .helpers import packet_bytes


def test_element_properties_and_alternative_prefixes():
    data = """<?xml version="1.0"?>
    <r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
           xmlns:stars="http://ns.adobe.com/xap/1.0/">
      <r:Description r:about=""><stars:Rating>3</stars:Rating></r:Description>
      <r:Description r:about=""><stars:Label>绿</stars:Label></r:Description>
    </r:RDF>""".encode()
    packet = Packet.parse(data)
    assert packet.values["rating"] == 3
    assert packet.values["label"] == "绿"
    values = packet.values | {"rating": 5, "label": "自定义 & <tag>"}
    result = Packet.parse(packet.render(values))
    assert result.values == values


@pytest.mark.parametrize("value", ["bad", "-2", "6", "NaN", "Infinity", "-0.5", "1_0"])
def test_invalid_star_ratings(value):
    with pytest.raises(MergeError, match="Rating"):
        Packet.parse(packet_bytes(rating=value))


@pytest.mark.parametrize("value", ["3.0", "3.5", "0.125", "5e0", "+2.00"])
def test_xmp_real_ratings_are_supported_exactly(value):
    from decimal import Decimal

    packet = Packet.parse(packet_bytes(rating=value))
    assert packet.values["rating"] == Decimal(value)
    values = packet.values | {"label": "Changed"}
    assert Packet.parse(packet.render(values)).values == values


def test_duplicate_and_qualified_fields_rejected():
    tree = etree.fromstring(packet_bytes(rating=3))
    description = tree.find(".//rdf:Description", NS)
    etree.SubElement(description, qname("xmp", "Rating")).text = "4"
    with pytest.raises(MergeError, match="duplicate"):
        Packet.parse(etree.tostring(tree))
    del description.attrib[qname("xmp", "Rating")]
    description[-1].set(qname("rdf", "resource"), "4")
    with pytest.raises(MergeError, match="qualified/structured"):
        Packet.parse(etree.tostring(tree))


def test_dtd_and_malformed_xml_rejected():
    with pytest.raises(MergeError, match="DTD/entity"):
        Packet.parse(
            b'<!DOCTYPE x [<!ENTITY file SYSTEM "file:///etc/passwd">]>'
            + packet_bytes().split(b"?>", 1)[1]
        )
    with pytest.raises(MergeError, match="invalid XML"):
        Packet.parse(b"<broken>")


def test_unrelated_rdf_subject_rejected():
    tree = etree.fromstring(packet_bytes())
    rdf = tree.find(qname("rdf", "RDF"))
    etree.SubElement(rdf, qname("rdf", "Description")).set(qname("rdf", "about"), "other")
    with pytest.raises(MergeError, match="multiple RDF subjects"):
        Packet.parse(etree.tostring(tree))


def test_identity_mismatch_detected():
    left = Packet.parse(packet_bytes())
    right = Packet.parse(packet_bytes().replace(b"photo-001", b"photo-002"))
    with pytest.raises(MergeError, match="OriginalDocumentID differs"):
        check_identity({"base": left, "left": left, "right": right})


def test_nested_same_name_and_unknown_namespaces_preserved():
    tree = etree.fromstring(packet_bytes(rating=1))
    description = tree.find(".//rdf:Description", NS)
    unknown = etree.SubElement(description, "{urn:vendor:custom}record")
    etree.SubElement(unknown, qname("xmp", "Rating")).text = "historical rating, not ours"
    etree.SubElement(unknown, "{urn:vendor:custom}empty")
    description.append(etree.Comment("retained comment"))
    packet = Packet.parse(etree.tostring(tree))
    result = Packet.parse(packet.render(packet.values | {"rating": 5}))
    assert result.protected_signature() == packet.protected_signature()
    assert (
        result.tree.find(".//{urn:vendor:custom}record/" + qname("xmp", "Rating")).text
        == "historical rating, not ours"
    )


def test_size_limit(tmp_path, monkeypatch):
    import mediawrench.cull.xmp as implementation

    monkeypatch.setattr(implementation, "MAX_BYTES", 100)
    source = tmp_path / "large.xmp"
    source.write_bytes(b"x" * 101)
    with pytest.raises(MergeError, match="exceeds"):
        Packet.load(source)


def test_no_change_is_byte_identical():
    data = packet_bytes(rating=3)
    packet = Packet.parse(data)
    assert packet.render(packet.values) == data


def test_no_shared_identifier_warns():
    data = packet_bytes().replace(b' xmpMM:OriginalDocumentID="uuid:photo-001"', b"")
    packet = Packet.parse(data)
    assert check_identity(dict.fromkeys(("base", "left", "right"), packet))
