"""Three-way field decisions and a no-overwrite local output bundle."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import simplejson as json

from . import __version__
from .jsonio import load_document
from .xmp import (
    FIELD_BY_NAME,
    FIELDS,
    FINGERPRINT_VERSION,
    SIDES,
    MergeError,
    Packet,
    check_identity,
    digest,
    qname,
    read_bytes,
    representation_warnings,
)


@dataclass
class MergePlan:
    packets: dict[str, Packet]
    history_source: str
    fields: dict[str, dict[str, Any]]
    warnings: list[str]
    rating_absence_policy: str = "portable_unrated"

    @classmethod
    def create(cls, paths: dict[str, Path], history_source: str) -> MergePlan:
        if set(paths) != set(SIDES) or history_source not in SIDES:
            raise MergeError("expected base, left, right inputs and a valid history source")
        packets = {side: Packet.load(paths[side]) for side in SIDES}
        warnings = check_identity(packets)
        warnings.extend(representation_warnings(packets, history_source))
        darktable_rating = any(
            packet.occurrences(qname("darktable", "xmp_version")) for packet in packets.values()
        )
        if darktable_rating and any(packet.values["rating"] is None for packet in packets.values()):
            warnings.append(
                "darktable uses a configurable import rating when xmp:Rating is absent. "
                "Absence is compared separately from explicit zero for these inputs."
            )
        fields = {}
        for field in FIELDS:
            actual = {side: packets[side].values[field.name] for side in SIDES}
            comparable = {
                side: 0
                if field.kind == "rating" and value is None and not darktable_rating
                else value
                for side, value in actual.items()
            }
            base, left, right = (comparable[side] for side in SIDES)
            selected = None
            if left == right:
                status = "unchanged" if left == base else "agreed"
                selected = "left"
            elif left == base:
                status, selected = "right_changed", "right"
            elif right == base:
                status, selected = "left_changed", "left"
            else:
                status = "conflict"
            # Keep the chosen history source's representation if it has the same
            # culling meaning under the selected portable/darktable rating policy.
            if selected is not None and comparable[history_source] == comparable[selected]:
                selected = history_source
            fields[field.name] = {
                "property": field.display,
                **actual,
                "status": status,
                "selected": selected,
            }
        policy = "darktable_import_default" if darktable_rating else "portable_unrated"
        return cls(packets, history_source, fields, warnings, policy)

    @property
    def conflicts(self) -> list[str]:
        return [name for name, field in self.fields.items() if field["status"] == "conflict"]

    def document(self) -> dict[str, Any]:
        signatures = {side: packet.protected_signature() for side, packet in self.packets.items()}
        return {
            "schema_version": 1,
            # Saved plan/audit value kept from the standalone tool for compatibility.
            "tool": f"cullmerge {__version__}",
            "fingerprint_version": FINGERPRINT_VERSION,
            "inputs": {
                side: {"path": str(packet.path.resolve()), "sha256": packet.sha256}
                for side, packet in self.packets.items()
            },
            "history_source": self.history_source,
            "rating_absence_policy": self.rating_absence_policy,
            "protected_sha256": signatures,
            "other_metadata_differ": len(set(signatures.values())) > 1,
            "fields": self.fields,
            "choices": dict.fromkeys(self.conflicts),
            "warnings": self.warnings,
            "note": "Only culling fields are merged. All other XML comes from history_source.",
        }

    def load_decisions(self, path: Path) -> dict[str, str]:
        document = load_document(path)
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise MergeError(f"{path}: expected a schema_version 1 plan")
        if document.get("history_source") != self.history_source:
            raise MergeError(
                "decisions were made for another history source; generate a fresh plan"
            )
        inputs = document.get("inputs")
        if not isinstance(inputs, dict) or set(inputs) != set(SIDES):
            raise MergeError("decisions plan must contain base, left, right input hashes")
        for side, packet in self.packets.items():
            item = inputs[side]
            if not isinstance(item, dict) or item.get("sha256") != packet.sha256:
                raise MergeError(f"{side} input changed since this plan; review a fresh plan")
        choices = document.get("choices")
        if not isinstance(choices, dict):
            raise MergeError("decisions plan must contain a choices object")
        for name in choices:
            if name not in FIELD_BY_NAME:
                raise MergeError(f"unknown culling field {name!r}")
        return {key: value for key, value in choices.items() if value is not None}

    def validate_choices(self, choices: dict[str, str], require_all: bool = True) -> None:
        for name, side in choices.items():
            if name not in FIELD_BY_NAME:
                raise MergeError(f"unknown culling field {name!r}")
            if side not in SIDES:
                raise MergeError(f"{name}: choose base, left, or right")
            if name not in self.conflicts:
                raise MergeError(f"{name} is not a conflict; remove its explicit choice")
        missing = [name for name in self.conflicts if name not in choices]
        if require_all and missing:
            raise MergeError("unresolved conflicts: " + ", ".join(missing))

    def resolve(self, choices: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
        self.validate_choices(choices)
        selections = {
            name: choices[name] if field["status"] == "conflict" else field["selected"]
            for name, field in self.fields.items()
        }
        values = {name: self.fields[name][side] for name, side in selections.items()}
        return values, selections

    def result_warnings(self, values: dict[str, Any]) -> list[str]:
        warnings = list(self.warnings)
        # darktable reads xmp:Label preferentially, ignoring its own color set.
        if values["label"] is not None and values["darktable_labels"]:
            warnings.append(
                "Both xmp:Label and darktable:colorlabels are present. darktable gives "
                "xmp:Label precedence; its color set may be hidden. No translation was applied."
            )
        if values["rating"] == -1 and values["pick"] == 1:
            warnings.append(
                "Rating is rejected (-1) while xmpDM:pick is picked (1). Applications may "
                "interpret these independent fields differently."
            )
        known_colors = {
            0: "None",
            1: "Red",
            2: "Orange",
            3: "Yellow",
            4: "Green",
            5: "Blue",
            6: "Magenta",
            7: "Gray",
            8: "Black",
            9: "White",
        }
        digikam = values["digikam_color"]
        label = values["label"]
        known_color_labels = set(known_colors.values()) | {"Purple"}
        if (
            digikam in known_colors
            and label in known_color_labels
            and known_colors[digikam] != label
        ):
            warnings.append(
                "digiKam:ColorLabel and xmp:Label name different known colors. "
                "Both decisions were retained; no cross-application conversion was applied."
            )
        if values["digikam_pick"] == 3 and (values["rating"] == -1 or values["pick"] == -1):
            warnings.append(
                "digiKam marks this photo accepted while an Adobe-compatible field marks it "
                "rejected. Vendor pick encodings were retained independently."
            )
        elif values["digikam_pick"] == 1 and values["pick"] == 1:
            warnings.append(
                "digiKam marks this photo rejected while xmpDM:pick marks it picked. "
                "Vendor pick encodings were retained independently."
            )
        rating = values["rating"]
        if (
            self.rating_absence_policy == "darktable_import_default"
            and rating is not None
            and rating != int(rating)
        ):
            warnings.append(
                "Fractional xmp:Rating was retained exactly; darktable reads whole-star ratings "
                "and may display a different value."
            )
        return warnings

    def write_bundle(self, destination: Path, choices: dict[str, str]) -> dict[str, Any]:
        values, selections = self.resolve(choices)
        source = self.packets[self.history_source]
        merged = source.render(values)
        # Detect an editor/sync client modifying originals while the user reviews.
        for side, packet in self.packets.items():
            if digest(read_bytes(packet.path)) != packet.sha256:
                raise MergeError(f"{side} input changed during review; generate a fresh plan")
        audit = self.document()
        audit["warnings"] = self.result_warnings(values)
        audit["created_utc"] = datetime.now(UTC).isoformat()
        audit["choices"] = choices
        audit["result"] = {
            "file": "merged.xmp",
            "sha256": digest(merged),
            "values": values,
            "selections": selections,
            "protected_sha256": source.protected_signature(),
            "preservation": "Non-culling XML structure checked after serialization.",
        }
        try:
            destination.mkdir(mode=0o700)  # Exclusive reservation, including empty directories.
        except FileExistsError as exc:
            raise MergeError(
                f"{destination}: destination already exists; choose a fresh directory"
            ) from exc
        except OSError as exc:
            raise MergeError(f"{destination}: {exc.strerror}") from exc
        try:
            (destination / "originals").mkdir(mode=0o700)
            _write_new(
                destination / ".incomplete", b"Output is incomplete until audit.json is written.\n"
            )
            for side, packet in self.packets.items():
                _write_new(destination / "originals" / f"{side}.xmp", packet.data)
            _write_new(destination / "merged.xmp", merged)
            _sync_directory(destination / "originals")
            _sync_directory(destination)
            # Audit is the final commit marker, following all data writes.
            _write_new(
                destination / "audit.json",
                (json.dumps(audit, indent=2, ensure_ascii=True) + "\n").encode(),
            )
            (destination / ".incomplete").unlink()
            _sync_directory(destination)
            _sync_directory(destination.parent)
            return audit
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path: Path) -> None:
    # POSIX persists directory entries separately from file data. Windows does
    # not support directory fsync through this API; file fsync still applies.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
