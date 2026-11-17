from __future__ import annotations

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.merge import MergePlan
from mediawrench.cull.xmp import NS, MergeError, Packet, check_identity, qname
from .helpers import packet_bytes


def with_filename(filename, *, stable=True):
    root = etree.fromstring(packet_bytes())
    description = root.find(".//rdf:Description", NS)
    description.set(qname("xmpMM", "DerivedFrom"), filename)
    if not stable:
        del description.attrib[qname("xmpMM", "OriginalDocumentID")]
    return Packet.parse(etree.tostring(root))


def test_same_original_id_allows_legitimate_renames_with_warning():
    packets = {
        side: with_filename(name)
        for side, name in (("base", "old.CR3"), ("left", "new.CR3"), ("right", "old.CR3"))
    }
    warnings = check_identity(packets)
    assert len(warnings) == 1
    assert "names differ" in warnings[0]
    assert "stable original ID" in warnings[0]


def test_cross_computer_paths_are_only_filename_hints():
    packets = {
        "base": with_filename("IMG 0001.CR3", stable=False),
        "left": with_filename("C:\\Photos\\IMG 0001.CR3", stable=False),
        "right": with_filename("file:///mnt/photos/IMG%200001.CR3", stable=False),
    }
    assert "Only photo filename hints match" in check_identity(packets)[0]


def test_unmerged_vendor_alias_is_named_in_review(make_paths):
    paths = make_paths()
    root = etree.fromstring(paths["right"].read_bytes())
    root.find(".//rdf:Description", NS).set("{http://ns.acdsee.com/iptc/1.0/}rating", "5")
    paths["right"].write_bytes(etree.tostring(root))
    plan = MergePlan.create(paths, "left")
    assert any("acdsee:rating is present in right" in warning for warning in plan.warnings)


def test_inconsistent_vendor_color_choices_are_reported(make_paths):
    plan = MergePlan.create(
        make_paths(
            {"label": "Green", "digikam_color": 5},
            {"label": "Green", "digikam_color": 5},
            {"label": "Green", "digikam_color": 5},
        ),
        "left",
    )
    values, _ = plan.resolve({})
    assert any("different known colors" in warning for warning in plan.result_warnings(values))


@pytest.mark.parametrize(
    ("label", "color"),
    [
        ("None", 0),
        ("Red", 1),
        ("Orange", 2),
        ("Yellow", 3),
        ("Green", 4),
        ("Blue", 5),
        ("Magenta", 6),
        ("Gray", 7),
        ("Black", 8),
        ("White", 9),
    ],
)
def test_digikam_color_names_match_documented_values(make_paths, label, color):
    plan = MergePlan.create(
        make_paths(
            {"label": label, "digikam_color": color},
            {"label": label, "digikam_color": color},
            {"label": label, "digikam_color": color},
        ),
        "left",
    )
    values, _ = plan.resolve({})
    assert not any(
        "different known colors" in warning for warning in plan.result_warnings(values)
    )


def test_digikam_magenta_is_not_mistaken_for_purple(make_paths):
    plan = MergePlan.create(
        make_paths(
            {"label": "Purple", "digikam_color": 6},
            {"label": "Purple", "digikam_color": 6},
            {"label": "Purple", "digikam_color": 6},
        ),
        "left",
    )
    values, _ = plan.resolve({})
    assert any(
        "different known colors" in warning for warning in plan.result_warnings(values)
    )


@pytest.mark.parametrize("filename", ["file://[invalid/photo.CR3", "file:///photo%FF.CR3"])
def test_malformed_photo_file_url_is_a_clean_error(filename):
    packet = with_filename(filename)
    with pytest.raises(MergeError, match="invalid photo file URL"):
        check_identity(dict.fromkeys(("base", "left", "right"), packet))
