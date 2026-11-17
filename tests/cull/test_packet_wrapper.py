from __future__ import annotations

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")


from mediawrench.cull.xmp import MergeError, Packet
from .helpers import packet_bytes


@pytest.mark.parametrize("hint", ['bytes="2048"', 'encoding="UTF-16"'])
def test_legacy_packet_hints_are_not_silently_staled(hint):
    data = (
        f'<?xpacket begin="" id="example" {hint}?>'.encode()
        + packet_bytes(rating=1).split(b"?>", 1)[1]
        + b'<?xpacket end="w"?>'
    )
    packet = Packet.parse(data)
    assert packet.render(packet.values) == data
    with pytest.raises(MergeError, match="legacy xpacket"):
        packet.render(packet.values | {"rating": 5})


def test_utf8_packet_hint_and_attribute_like_text_are_preserved():
    data = (
        b'<?xpacket begin="" id="text encoding=example" encoding="UTF-8"?>'
        + packet_bytes(rating=1).split(b"?>", 1)[1]
        + b'<?xpacket end="w"?>'
    )
    packet = Packet.parse(data)
    result = Packet.parse(packet.render(packet.values | {"rating": 5}))
    assert result.values["rating"] == 5
    assert result.protected_signature() == packet.protected_signature()
