"""Adversarial checks of the preservation invariant, beyond normal editor output."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.xmp import NS, Packet, qname
from .helpers import packet_bytes, subprocess_env


def test_mixed_content_tail_is_part_of_protected_fingerprint():
    tree = etree.fromstring(packet_bytes(rating=1))
    description = tree.find(".//rdf:Description", NS)
    literal = etree.SubElement(description, "{urn:custom}literal")
    literal.text = "before"
    etree.SubElement(literal, "{urn:custom}span").tail = "after"
    before = Packet.parse(etree.tostring(tree))
    literal[0].tail = "changed-after"
    after = Packet.parse(etree.tostring(tree))
    assert before.protected_signature() != after.protected_signature()


def test_processing_instruction_target_is_protected():
    tree = etree.fromstring(packet_bytes())
    description = tree.find(".//rdf:Description", NS)
    description.append(etree.ProcessingInstruction("original", "data"))
    before = Packet.parse(etree.tostring(tree))
    description[-1].target = "different"
    after = Packet.parse(etree.tostring(tree))
    assert before.protected_signature() != after.protected_signature()


def test_outer_xpacket_is_protected():
    before = Packet.parse(b'<?xpacket begin="one"?>\n' + packet_bytes().split(b"?>", 1)[1])
    after = Packet.parse(b'<?xpacket begin="two"?>\n' + packet_bytes().split(b"?>", 1)[1])
    assert before.protected_signature() != after.protected_signature()


def test_fingerprint_is_stable_across_processes(tmp_path):
    data = packet_bytes().replace(b"</rdf:Description>", b"<!-- retained --></rdf:Description>")
    path = tmp_path / "comments.xmp"
    path.write_bytes(data)
    expected = Packet.load(path).protected_signature()
    code = (
        "from pathlib import Path; from mediawrench.cull.xmp import Packet; import sys; "
        "print(Packet.load(Path(sys.argv[1])).protected_signature())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=subprocess_env(),
    )
    assert result.stdout.strip() == expected


def test_deleting_culling_element_preserves_following_literal_text():
    root = etree.fromstring(packet_bytes())
    description = root.find(".//rdf:Description", NS)
    rating = etree.SubElement(description, qname("xmp", "Rating"))
    rating.text = "3"
    rating.tail = "unrelated literal text after culling property"
    packet = Packet.parse(etree.tostring(root))
    result = packet.render(packet.values | {"rating": None})
    assert b"unrelated literal text after culling property" in result


def test_add_first_field_to_whitespace_only_description():
    data = (
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="">  </rdf:Description></rdf:RDF>'
    )
    packet = Packet.parse(data)
    result = Packet.parse(packet.render(packet.values | {"rating": 3}))
    assert result.values["rating"] == 3
    assert packet.protected_signature() == result.protected_signature()


def test_namespace_context_for_unknown_qname_values_is_protected():
    data = b"""<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
       xmlns:unit="urn:units:original" xmlns:v="urn:vendor">
       <r:Description r:about=""><v:setting unit="unit:distance"/></r:Description>
    </r:RDF>"""
    original = Packet.parse(data)
    changed = Packet.parse(data.replace(b"urn:units:original", b"urn:units:changed"))
    assert original.protected_signature() != changed.protected_signature()
    merged = Packet.parse(original.render(original.values | {"rating": 5}))
    assert original.protected_signature() == merged.protected_signature()
