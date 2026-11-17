"""Actual upstream packets, including explicit rejection cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.xmp import MergeError, Packet

FIXTURES = Path(__file__).parent / "fixtures"
ENTRIES = json.loads((FIXTURES / "exiftool-corpus.json").read_text())["files"]


@pytest.mark.parametrize("entry", ENTRIES, ids=[entry["name"] for entry in ENTRIES])
def test_actual_exiftool_xmp_packets(entry):
    path = FIXTURES / "downloaded" / entry["name"]
    if not path.exists():
        pytest.skip("download the ExifTool packets listed in tests/cull/fixtures/exiftool-corpus.json")
    if entry["expected"] == "invalid_xml":
        # Upstream deliberately includes a forbidden XML control byte in xpacket.
        with pytest.raises(MergeError, match="invalid XML"):
            Packet.load(path)
        return
    if entry["expected"] == "multiple_subjects":
        # The fixture itself says it is general RDF/XML, not strictly XMP.
        with pytest.raises(MergeError, match="multiple RDF subjects"):
            Packet.load(path)
        return
    packet = Packet.load(path)
    values = {
        "rating": 5,
        "label": "自定义 & <label>",
        "darktable_labels": [0, 4],
        "pick": 1,
        "digikam_pick": 3,
        "digikam_color": 5,
    }
    result = Packet.parse(packet.render(values))
    assert result.values == values
    assert result.protected_signature() == packet.protected_signature()
