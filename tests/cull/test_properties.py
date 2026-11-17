"""Property-based checks (hypothesis): three-way truth table and XML round trips.

Kept apart so the rest of the cull suite runs without hypothesis installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")
pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st
from lxml import etree

from mediawrench.cull.merge import MergePlan
from mediawrench.cull.xmp import NS, Packet, qname

from .helpers import packet_bytes


@given(st.sampled_from([None, -1, 0, 1, 2, 3, 4, 5]), st.integers(-1, 5), st.integers(-1, 5))
@settings(max_examples=150, deadline=None)
def test_three_way_truth_table(base, left, right):
    # Pure decisions over real parsed packets; enumerate ancestor/equality cases.
    packets = {
        side: Packet.parse(packet_bytes(rating=value))
        for side, value in (("base", base), ("left", left), ("right", right))
    }
    for side in ("base", "left", "right"):
        assert packets[side].values["rating"] == {"base": base, "left": left, "right": right}[side]
    # Use the same public plan constructor as the CLI, without persistent fixture writes.
    import unittest.mock

    with unittest.mock.patch("mediawrench.cull.merge.Packet.load", side_effect=packets.values()):
        plan = MergePlan.create({side: Path(side) for side in packets}, "left")
    fields = plan.fields["rating"]
    comparable_base = 0 if base is None else base
    if left == right or left == comparable_base or right == comparable_base:
        assert fields["status"] != "conflict"
        expected = left if left == right or right == comparable_base else right
        values, _ = plan.resolve({})
        assert values["rating"] == expected
    else:
        assert fields["status"] == "conflict"
        for side in packets:
            values, _ = plan.resolve({"rating": side})
            assert values["rating"] == packets[side].values["rating"]


@given(
    st.text(alphabet=st.characters(categories=("L", "N", "P", "S", "Zs")), max_size=120),
    st.booleans(),
    st.sampled_from([None, -1, 0, 1, 2, 3, 4, 5]),
)
@settings(max_examples=250, deadline=None)
def test_culling_roundtrip_arbitrary_unicode_and_scalar_syntax(label, child_syntax, rating):
    root = etree.fromstring(packet_bytes(rating=1, label="Red"))
    description = root.find(".//rdf:Description", NS)
    if child_syntax:
        for local in ("Rating", "Label"):
            tag = qname("xmp", local)
            value = description.attrib.pop(tag)
            etree.SubElement(description, tag).text = value
    opaque = etree.SubElement(description, "{urn:other-app}parameters")
    opaque.set("mode", "opaque")
    opaque.text = "parameter text with spaces  \n and & <markers>"
    packet = Packet.parse(etree.tostring(root))
    values = packet.values | {"label": label, "rating": rating}
    result = Packet.parse(packet.render(values))
    assert result.values == values
    assert result.protected_signature() == packet.protected_signature()
