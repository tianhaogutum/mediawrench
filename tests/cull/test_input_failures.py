from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.merge import MergePlan
from mediawrench.cull.xmp import MergeError, Packet

from .helpers import subprocess_env


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named-pipe check")
def test_named_pipe_is_rejected_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "input.xmp"
    os.mkfifo(path)
    code = (
        "from pathlib import Path; from mediawrench.cull.xmp import Packet, MergeError; import sys\n"
        "try: Packet.load(Path(sys.argv[1]))\n"
        "except MergeError as e: print(e); sys.exit(2)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=2,
        env=subprocess_env(),
    )
    assert process.returncode == 2
    assert "regular" in process.stdout


@pytest.mark.parametrize("choice", [[], {}, 1, True, "unknown"])
def test_malformed_decision_values_are_clean_errors(make_paths, tmp_path, choice):
    plan = MergePlan.create(make_paths({"rating": 0}, {"rating": 1}, {"rating": 2}), "left")
    document = plan.document()
    document["choices"]["rating"] = choice
    path = tmp_path / "choices.json"
    path.write_text(json.dumps(document))
    with pytest.raises(MergeError, match="choose base, left, or right"):
        plan.resolve(plan.load_decisions(path))


def test_unknown_null_choice_is_not_silently_discarded(make_paths, tmp_path):
    plan = MergePlan.create(make_paths(), "left")
    document = plan.document()
    document["choices"]["ratting"] = None
    path = tmp_path / "choices.json"
    path.write_text(json.dumps(document))
    with pytest.raises(MergeError, match="unknown culling field"):
        plan.load_decisions(path)


def test_resolving_repeatedly_does_not_duplicate_warnings(make_paths):
    paths = make_paths(
        {"label": "Red", "darktable_labels": [1]},
        {"label": "Red", "darktable_labels": [1]},
        {"label": "Red", "darktable_labels": [1]},
    )
    plan = MergePlan.create(paths, "left")
    initial = list(plan.warnings)
    plan.resolve({})
    plan.resolve({})
    assert plan.warnings == initial


def test_duplicate_json_keys_are_rejected(make_paths, tmp_path):
    plan = MergePlan.create(make_paths(), "left")
    document = plan.document()
    serialized = json.dumps(document)
    path = tmp_path / "duplicate.json"
    path.write_text(serialized.replace('"choices": {}', '"choices": {}, "choices": {}'))
    with pytest.raises(MergeError, match="duplicate JSON key"):
        plan.load_decisions(path)


def test_excessively_nested_json_is_a_clean_error(make_paths, tmp_path):
    plan = MergePlan.create(make_paths(), "left")
    path = tmp_path / "nested.json"
    path.write_text("{" + '"x":{' * 2000 + '"x":0' + "}" * 2001)
    with pytest.raises(MergeError, match="invalid JSON|schema_version"):
        plan.load_decisions(path)


@pytest.mark.parametrize("limit", ["MAX_ELEMENTS", "MAX_ATTRIBUTES"])
def test_xml_complexity_is_bounded_before_full_tree_growth(limit, monkeypatch):
    from .helpers import packet_bytes

    import mediawrench.cull.xmp as implementation

    monkeypatch.setattr(implementation, limit, 2)
    with pytest.raises(MergeError, match="complexity exceeds"):
        Packet.parse(packet_bytes(rating=1))


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_windows_editor_decision_encodings(make_paths, tmp_path, encoding):
    plan = MergePlan.create(make_paths({"rating": 1}, {"rating": 3}, {"rating": 5}), "left")
    document = plan.document()
    document["choices"]["rating"] = "right"
    path = tmp_path / "windows.json"
    path.write_bytes(json.dumps(document).encode(encoding))
    assert plan.load_decisions(path) == {"rating": "right"}


def test_unrepresentable_decimal_exponent_is_a_clean_json_error(make_paths, tmp_path):
    plan = MergePlan.create(make_paths(), "left")
    path = tmp_path / "huge-number.json"
    path.write_text('{"ignored":1e99999999999999999999999999999999999}')
    with pytest.raises(MergeError, match="invalid JSON"):
        plan.load_decisions(path)


def test_review_file_size_is_bounded(make_paths, tmp_path):
    plan = MergePlan.create(make_paths(), "left")
    path = tmp_path / "huge-review.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(MergeError, match="size limit"):
        plan.load_decisions(path)
