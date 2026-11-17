from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from lxml import etree

from mediawrench.cull.merge import MergePlan
from mediawrench.cull.xmp import NS, Packet, qname


def test_real_darktable_history_and_masks_retained(upstream_packet, tmp_path):
    paths = {}
    for side, rating in (("base", 1), ("left", 3), ("right", 5)):
        # Independently edit upstream data for the two computers' culls.
        tree = etree.fromstring(upstream_packet.data)
        description = tree.find(".//rdf:Description", NS)
        description.set(qname("xmp", "Rating"), str(rating))
        if side == "right":
            description.set(qname("xmp", "Label"), "Purple")
            description.set(
                qname("darktable", "history_current_hash"), "right-has-different-history"
            )
        path = tmp_path / f"{side}.xmp"
        path.write_bytes(etree.tostring(tree))
        paths[side] = path
    plan = MergePlan.create(paths, "left")
    assert plan.document()["other_metadata_differ"] is True
    destination = tmp_path / "merged"
    plan.write_bundle(destination, {"rating": "right"})
    result = Packet.load(destination / "merged.xmp")
    assert result.values["rating"] == 5
    assert result.values["label"] == "Purple"
    for tag in ("history", "masks_history"):
        old = plan.packets["left"].tree.find(".//" + qname("darktable", tag))
        new = result.tree.find(".//" + qname("darktable", tag))
        assert etree.tostring(old, method="c14n") == etree.tostring(new, method="c14n")
    assert result.protected_signature() == plan.packets["left"].protected_signature()
    for side, packet in plan.packets.items():
        assert (destination / "originals" / f"{side}.xmp").read_bytes() == packet.data


def test_exiftool_generates_and_reads_merged_packet(tmp_path):
    executable = os.environ.get("EXIFTOOL") or shutil.which("exiftool")
    if not executable:
        pytest.skip("set EXIFTOOL for independent ExifTool interoperability checks")
    paths = {}
    for side, rating, label in (("base", 1, "Red"), ("left", 4, "Red"), ("right", 1, "Green")):
        path = tmp_path / f"{side}.xmp"
        subprocess.run(
            [
                executable,
                "-config",
                "",
                f"-XMP-xmp:Rating={rating}",
                f"-XMP-xmp:Label={label}",
                "-XMP-xmpDM:Pick=1",
                "-XMP-xmpMM:OriginalDocumentID=uuid:our-exiftool-fixture",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        paths[side] = path
    destination = tmp_path / "out"
    MergePlan.create(paths, "left").write_bundle(destination, {})
    read = subprocess.run(
        [
            executable,
            "-config",
            "",
            "-j",
            "-G1",
            "-n",
            "-XMP-xmp:Rating",
            "-XMP-xmp:Label",
            "-XMP-xmpDM:Pick",
            str(destination / "merged.xmp"),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    values = json.loads(read.stdout)[0]
    assert values["XMP-xmp:Rating"] == 4
    assert values["XMP-xmp:Label"] == "Green"
    assert values["XMP-xmpDM:Pick"] == 1


@pytest.mark.parametrize("fixture", ["digikam-raw.xmp", "digikam-jpeg.xmp"])
def test_legacy_bibble_darktable_namespaces_from_digikam_tests(fixture, tmp_path):
    path = Path(__file__).parent / "fixtures" / "downloaded" / fixture
    if not path.exists():
        pytest.skip("download the digiKam sidecars listed in tests/cull/fixtures/SOURCES.md")
    original = Packet.load(path)
    paths = {}
    for side, pick, color in (("base", 0, 0), ("left", 3, 0), ("right", 0, 5)):
        tree = etree.parse(io.BytesIO(original.data))
        description = tree.find(".//rdf:Description", NS)
        description.set(qname("digiKam", "PickLabel"), str(pick))
        description.set(qname("digiKam", "ColorLabel"), str(color))
        target = tmp_path / f"{side}.xmp"
        target.write_bytes(etree.tostring(tree, encoding="UTF-8"))
        paths[side] = target
    plan = MergePlan.create(paths, "right")
    destination = tmp_path / "out"
    audit = plan.write_bundle(destination, {})
    result = Packet.load(destination / "merged.xmp")
    assert result.values["digikam_pick"] == 3
    assert result.values["digikam_color"] == 5
    # The independent fixture writer added a digiKam namespace declaration.
    # Preserve the selected input's full namespace context, as well as its data.
    assert result.protected_signature() == plan.packets["right"].protected_signature()
    assert audit["result"]["selections"]["digikam_pick"] == "left"


def test_exiftool_roundtrip_vendor_flags_and_fractional_rating(tmp_path):
    executable = os.environ.get("EXIFTOOL") or shutil.which("exiftool")
    if not executable:
        pytest.skip("set EXIFTOOL for independent ExifTool interoperability checks")
    target = tmp_path / "vendor.xmp"
    subprocess.run(
        [
            executable,
            "-config",
            "",
            "-XMP-digiKam:PickLabel=2",
            "-XMP-digiKam:ColorLabel=9",
            "-XMP-xmp:Rating=3.125",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    packet = Packet.load(target)
    values = packet.values | {"digikam_pick": 3, "digikam_color": 5, "label": "Blue"}
    merged = tmp_path / "merged.xmp"
    merged.write_bytes(packet.render(values))
    result = subprocess.run(
        [executable, "-config", "", "-j", "-n", "-G1", "-XMP:all", str(merged)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    metadata = json.loads(result.stdout)[0]
    assert metadata["XMP-digiKam:PickLabel"] == 3
    assert metadata["XMP-digiKam:ColorLabel"] == 5
    assert metadata["XMP-xmp:Rating"] == 3.125


def test_exiftool_independently_reads_darktable_color_sequence(tmp_path):
    executable = os.environ.get("EXIFTOOL") or shutil.which("exiftool")
    if not executable:
        pytest.skip("set EXIFTOOL for independent darktable array interoperability")
    config = Path(__file__).parent / "fixtures" / "exiftool-darktable.config"
    target = tmp_path / "colors.xmp"
    subprocess.run(
        [
            executable,
            "-config",
            str(config),
            "-XMP-darktable:ColorLabels=0",
            "-XMP-darktable:ColorLabels+=4",
            "-XMP-crs:Exposure2012=0.25",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    packet = Packet.load(target)
    assert packet.values["darktable_labels"] == [0, 4]
    merged = tmp_path / "merged.xmp"
    merged.write_bytes(packet.render(packet.values | {"darktable_labels": [1, 3]}))
    result = subprocess.run(
        [executable, "-config", str(config), "-j", "-n", "-G1", "-XMP:all", str(merged)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    metadata = json.loads(result.stdout)[0]
    assert metadata["XMP-darktable:ColorLabels"] == [1, 3]
    assert metadata["XMP-crs:Exposure2012"] == 0.25


@pytest.mark.parametrize(
    "encoding", ["UTF-8", "UTF-16", "UTF-16LE", "UTF-16BE", "UTF-32BE", "UTF-32LE"]
)
def test_real_history_packet_in_xml_encoding_variants(upstream_packet, encoding):
    data = etree.tostring(upstream_packet.tree, encoding=encoding, xml_declaration=True)
    packet = Packet.parse(data)
    assert packet.render(packet.values) == data
    result = Packet.parse(packet.render(packet.values | {"rating": 5, "label": "绿 & Blue"}))
    assert result.values["rating"] == 5
    assert result.values["label"] == "绿 & Blue"
    assert result.protected_signature() == packet.protected_signature()
