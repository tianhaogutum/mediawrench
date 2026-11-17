"""Fixtures for the cull tests only.

Nothing here is autouse, changes the working directory or environment, or adds
pytest hooks, so other test packages are unaffected. lxml/simplejson (the optional
``cull`` extra) are imported lazily: when they are missing, each cull test module
skips itself instead of breaking collection of the whole suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def make_paths(tmp_path):
    from .helpers import packet_bytes

    def create(base=None, left=None, right=None):
        result = {}
        for side, values in (("base", base), ("left", left), ("right", right)):
            path = tmp_path / f"{side}.xmp"
            path.write_bytes(packet_bytes(**(values or {})))
            result[side] = path
        return result

    return create


@pytest.fixture(params=["darktable-v4.xmp", "darktable-v5.xmp"])
def upstream_packet(request):
    from mediawrench.cull.xmp import Packet

    path = FIXTURES / "downloaded" / request.param
    if not path.exists():
        pytest.skip("download the darktable sidecars listed in tests/cull/fixtures/SOURCES.md")
    return Packet.load(path)
