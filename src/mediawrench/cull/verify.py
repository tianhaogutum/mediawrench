"""Read-only verification of a saved merge bundle, without original input paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import simplejson

from .jsonio import load_document
from .merge import MergePlan
from .xmp import FINGERPRINT_VERSION, SIDES, MergeError, Packet


def verify_bundle(directory: Path) -> dict[str, Any]:
    if not directory.is_dir() or directory.is_symlink():
        raise MergeError(f"{directory}: expected a bundle directory, not a symlink")
    marker = directory / ".incomplete"
    if marker.exists() or marker.is_symlink():
        raise MergeError(f"{directory}: incomplete output; retain originals and retry")
    fixed_paths = [directory / "audit.json", directory / "merged.xmp", directory / "originals"]
    fixed_paths.extend(directory / "originals" / f"{side}.xmp" for side in SIDES)
    if any(path.is_symlink() for path in fixed_paths):
        raise MergeError("bundle files must be self-contained; symlinks are not accepted")
    audit = load_document(directory / "audit.json")
    if type(audit.get("schema_version")) is not int or audit["schema_version"] != 1:
        raise MergeError("unsupported audit schema; expected schema_version 1")
    if audit.get("fingerprint_version") != FINGERPRINT_VERSION:
        raise MergeError("unsupported preservation fingerprint version")
    history_source = audit.get("history_source")
    if history_source not in SIDES:
        raise MergeError("audit has an invalid history source")
    plan = MergePlan.create(
        {side: directory / "originals" / f"{side}.xmp" for side in SIDES}, history_source
    )
    if audit.get("rating_absence_policy") != plan.rating_absence_policy:
        raise MergeError("recorded rating absence policy differs from the original inputs")
    inputs = audit.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(SIDES):
        raise MergeError("audit must identify all three original copies")
    for side, packet in plan.packets.items():
        if not isinstance(inputs[side], dict) or inputs[side].get("sha256") != packet.sha256:
            raise MergeError(f"originals/{side}.xmp: SHA-256 differs from the audit")
    choices = audit.get("choices")
    if not isinstance(choices, dict):
        raise MergeError("audit choices must be an object")
    values, selections = plan.resolve(choices)
    result = audit.get("result")
    if not isinstance(result, dict) or result.get("file") != "merged.xmp":
        raise MergeError("audit must name the fixed merged.xmp output")
    merged = Packet.load(directory / "merged.xmp")
    if merged.sha256 != result.get("sha256"):
        raise MergeError("merged.xmp: SHA-256 differs from the audit")
    if not _same_json(result.get("values"), values) or merged.values != values:
        raise MergeError("merged culling fields do not match the recorded three-way decisions")
    if not _same_json(result.get("selections"), selections):
        raise MergeError("recorded field selections do not match the three-way decisions")
    expected = plan.packets[history_source].protected_signature()
    if merged.protected_signature() != expected or result.get("protected_sha256") != expected:
        raise MergeError("merged non-culling XML differs from the selected history source")
    fingerprints = {side: packet.protected_signature() for side, packet in plan.packets.items()}
    if audit.get("protected_sha256") != fingerprints:
        raise MergeError("original preservation fingerprints differ from the audit")
    if not _same_json(audit.get("fields"), plan.fields):
        raise MergeError("recorded field differences do not match the original copies")
    other_metadata_differ = len(set(fingerprints.values())) > 1
    if type(audit.get("other_metadata_differ")) is not bool or (
        audit["other_metadata_differ"] != other_metadata_differ
    ):
        raise MergeError("recorded other-metadata summary differs from the original copies")
    return {
        "verified": True,
        "bundle": str(directory.resolve()),
        "history_source": history_source,
        "merged_sha256": merged.sha256,
        "originals_checked": 3,
        "warnings": plan.result_warnings(values),
    }


def _same_json(left: Any, right: Any) -> bool:
    # Python considers True == 1. Canonical JSON distinguishes metadata numbers
    # from booleans and uses the existing exact-decimal serializer.
    try:
        return simplejson.dumps(left, sort_keys=True) == simplejson.dumps(right, sort_keys=True)
    except (ValueError, TypeError, ArithmeticError, RecursionError) as exc:
        raise MergeError(f"invalid recorded audit value: {exc}") from exc
