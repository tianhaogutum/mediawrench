from __future__ import annotations

import json
import shutil

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.cli import main
from mediawrench.cull.merge import MergePlan
from mediawrench.cull.verify import verify_bundle
from mediawrench.cull.xmp import MergeError, digest


@pytest.fixture
def bundle(make_paths, tmp_path):
    paths = make_paths(
        {"rating": 1, "label": "Red"},
        {"rating": 3, "label": "Red"},
        {"rating": 5, "label": "Green"},
    )
    destination = tmp_path / "bundle"
    MergePlan.create(paths, "left").write_bundle(destination, {"rating": "right"})
    return destination


def test_verify_works_after_bundle_is_moved_and_inputs_removed(bundle, tmp_path, capsys):
    moved = tmp_path / "moved"
    shutil.move(bundle, moved)
    for side in ("base", "left", "right"):
        (tmp_path / f"{side}.xmp").unlink()
    assert verify_bundle(moved)["verified"] is True
    assert main(["verify", str(moved)]) == 0
    assert json.loads(capsys.readouterr().out)["originals_checked"] == 3


@pytest.mark.parametrize("filename", ["merged.xmp", "originals/base.xmp", "originals/left.xmp"])
def test_changed_bytes_are_detected(bundle, filename):
    path = bundle / filename
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(MergeError, match="SHA-256 differs"):
        verify_bundle(bundle)


def test_incomplete_bundle_refused(bundle):
    (bundle / ".incomplete").write_text("interrupted")
    with pytest.raises(MergeError, match="incomplete output"):
        verify_bundle(bundle)


def test_non_culling_change_detected_even_if_result_hash_is_updated(bundle):
    output = bundle / "merged.xmp"
    output.write_bytes(output.read_bytes().replace(b"exposure", b"contrast"))
    path = bundle / "audit.json"
    audit = json.loads(path.read_text())
    audit["result"]["sha256"] = digest(output.read_bytes())
    path.write_text(json.dumps(audit))
    with pytest.raises(MergeError, match="non-culling XML differs"):
        verify_bundle(bundle)


def test_culling_change_detected_even_if_result_hash_is_updated(bundle):
    output = bundle / "merged.xmp"
    output.write_bytes(output.read_bytes().replace(b'Rating="5"', b'Rating="4"'))
    path = bundle / "audit.json"
    audit = json.loads(path.read_text())
    audit["result"]["sha256"] = digest(output.read_bytes())
    path.write_text(json.dumps(audit))
    with pytest.raises(MergeError, match="culling fields"):
        verify_bundle(bundle)


def test_symlinked_bundle_file_refused(bundle, tmp_path):
    original = bundle / "originals" / "base.xmp"
    moved = tmp_path / "external.xmp"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(MergeError, match="symlinks"):
        verify_bundle(bundle)


def test_invalid_audit_fails_cleanly(bundle, capsys):
    (bundle / "audit.json").write_text("[]")
    assert main(["verify", str(bundle)]) == 2
    assert "JSON object" in capsys.readouterr().err


def test_dangling_incomplete_marker_is_still_incomplete(bundle):
    (bundle / ".incomplete").symlink_to(bundle / "missing-marker-target")
    with pytest.raises(MergeError, match="incomplete output"):
        verify_bundle(bundle)


def test_boolean_cannot_impersonate_a_numeric_field_difference(bundle):
    path = bundle / "audit.json"
    audit = json.loads(path.read_text())
    audit["fields"]["rating"]["base"] = True
    path.write_text(json.dumps(audit))
    with pytest.raises(MergeError, match="field differences"):
        verify_bundle(bundle)


def test_boolean_cannot_impersonate_a_result_star_rating(make_paths, tmp_path):
    paths = make_paths({"rating": 1}, {"rating": 1}, {"rating": 1})
    directory = tmp_path / "one-star"
    MergePlan.create(paths, "left").write_bundle(directory, {})
    path = directory / "audit.json"
    audit = json.loads(path.read_text())
    audit["result"]["values"]["rating"] = True
    path.write_text(json.dumps(audit))
    with pytest.raises(MergeError, match="culling fields"):
        verify_bundle(directory)


def test_incorrect_other_metadata_summary_is_detected(bundle):
    path = bundle / "audit.json"
    audit = json.loads(path.read_text())
    audit["other_metadata_differ"] = not audit["other_metadata_differ"]
    path.write_text(json.dumps(audit))
    with pytest.raises(MergeError, match="metadata summary"):
        verify_bundle(bundle)
