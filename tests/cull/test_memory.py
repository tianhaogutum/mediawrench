"""Check native XML lifetime, not noisy wall-clock or RSS thresholds."""

from __future__ import annotations

import gc

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.xmp import Packet
from .helpers import packet_bytes


def test_hashing_does_not_retain_trees_until_cyclic_gc():
    data = packet_bytes(rating=3)
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = sum(isinstance(value, etree._Element) for value in gc.get_objects())
        for _ in range(30):
            packet = Packet.parse(data)
            packet.protected_signature()
            del packet
        after = sum(isinstance(value, etree._Element) for value in gc.get_objects())
        assert after <= before + 1
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()
