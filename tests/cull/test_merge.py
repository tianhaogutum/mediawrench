from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")


from mediawrench.cull.merge import MergePlan
from mediawrench.cull.xmp import MergeError, Packet


def test_independent_changes_and_conflicts(make_paths, tmp_path):
    paths = make_paths(
        {"rating": 1, "label": "Red", "pick": 0},
        {"rating": 3, "label": "Red", "pick": 1},
        {"rating": 5, "label": "Green", "pick": -1},
    )
    plan = MergePlan.create(paths, "left")
    assert plan.conflicts == ["rating", "pick"]
    with pytest.raises(MergeError, match="unresolved conflicts"):
        plan.write_bundle(tmp_path / "out", {})
    assert not (tmp_path / "out").exists()
    audit = plan.write_bundle(tmp_path / "out", {"rating": "right", "pick": "left"})
    output = Packet.load(tmp_path / "out" / "merged.xmp")
    assert output.values["rating"] == 5
    assert output.values["label"] == "Green"
    assert output.values["pick"] == 1
    assert output.protected_signature() == plan.packets["left"].protected_signature()
    assert audit["result"]["selections"]["rating"] == "right"
    assert not (tmp_path / "out" / ".incomplete").exists()
    for side, path in paths.items():
        assert path.read_bytes() == (tmp_path / "out" / "originals" / f"{side}.xmp").read_bytes()
    assert json.loads((tmp_path / "out" / "audit.json").read_text())["schema_version"] == 1


def test_missing_rating_is_unrated_but_empty_label_is_distinct(make_paths):
    plan = MergePlan.create(
        make_paths({"rating": 2, "label": "Red"}, {"rating": 0, "label": ""}, {}), "base"
    )
    assert plan.conflicts == ["label"]
    values, _ = plan.resolve({"label": "left"})
    result = Packet.parse(plan.packets["base"].render(values))
    assert result.values["rating"] == 0
    assert result.values["label"] == ""


def test_set_semantics_and_clear(make_paths):
    plan = MergePlan.create(
        make_paths(
            {"darktable_labels": [0, 4]},
            {"darktable_labels": [4, 0, 0]},
            {"darktable_labels": []},
        ),
        "left",
    )
    assert plan.fields["darktable_labels"]["status"] == "right_changed"
    values, _ = plan.resolve({})
    assert Packet.parse(plan.packets["left"].render(values)).values["darktable_labels"] == []


def test_decisions_bound_to_inputs_and_history(make_paths, tmp_path):
    paths = make_paths({"rating": 0}, {"rating": 1}, {"rating": 2})
    plan = MergePlan.create(paths, "left")
    document = plan.document()
    document["choices"]["rating"] = "base"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(document))
    assert plan.load_decisions(decisions) == {"rating": "base"}
    with pytest.raises(MergeError, match="history source"):
        MergePlan.create(paths, "right").load_decisions(decisions)
    paths["right"].write_bytes(paths["base"].read_bytes())
    with pytest.raises(MergeError, match="changed since"):
        MergePlan.create(paths, "left").load_decisions(decisions)


def test_changed_input_during_review_is_rejected(make_paths, tmp_path):
    paths = make_paths()
    plan = MergePlan.create(paths, "left")
    paths["left"].write_bytes(paths["left"].read_bytes() + b"\n")
    with pytest.raises(MergeError, match="changed during review"):
        plan.write_bundle(tmp_path / "out", {})
    assert not (tmp_path / "out").exists()


def test_existing_output_never_touched(make_paths, tmp_path):
    destination = tmp_path / "out"
    destination.mkdir()
    plan = MergePlan.create(make_paths(), "left")
    with pytest.raises(MergeError, match="already exists"):
        plan.write_bundle(destination, {})
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "failing_file", [".incomplete", "base.xmp", "left.xmp", "right.xmp", "merged.xmp", "audit.json"]
)
def test_failed_write_removes_owned_output(make_paths, tmp_path, monkeypatch, failing_file):
    import mediawrench.cull.merge as implementation

    plan = MergePlan.create(make_paths(), "left")
    original = implementation._write_new

    def fail(path: Path, content: bytes):
        if path.name == failing_file:
            raise OSError("injected disk failure")
        original(path, content)

    monkeypatch.setattr(implementation, "_write_new", fail)
    with pytest.raises(OSError, match="injected"):
        plan.write_bundle(tmp_path / "out", {})
    assert not (tmp_path / "out").exists()
    assert all(packet.path.exists() for packet in plan.packets.values())


def test_fractional_ratings_do_not_round_into_agreement(make_paths, tmp_path):
    from decimal import Decimal

    paths = make_paths(
        {"rating": "1"},
        {"rating": "3.000000000000000001"},
        {"rating": "3.000000000000000002"},
    )
    plan = MergePlan.create(paths, "left")
    assert plan.conflicts == ["rating"]
    audit = plan.write_bundle(tmp_path / "decimal", {"rating": "right"})
    assert audit["result"]["values"]["rating"] == Decimal("3.000000000000000002")
    assert b"3.000000000000000002" in (tmp_path / "decimal" / "audit.json").read_bytes()
    assert Packet.load(tmp_path / "decimal" / "merged.xmp").values["rating"] == Decimal(
        "3.000000000000000002"
    )


def test_darktable_missing_rating_does_not_erase_explicit_unrated_choice(make_paths):
    from lxml import etree

    from mediawrench.cull.xmp import NS, qname

    paths = make_paths({}, {}, {"rating": 0})
    for path in paths.values():
        root = etree.fromstring(path.read_bytes())
        root.find(".//rdf:Description", NS).set(qname("darktable", "xmp_version"), "5")
        path.write_bytes(etree.tostring(root))
    plan = MergePlan.create(paths, "left")
    values, selections = plan.resolve({})
    assert values["rating"] == 0
    assert selections["rating"] == "right"
    assert plan.rating_absence_policy == "darktable_import_default"
    assert any("configurable import rating" in warning for warning in plan.warnings)
