"""Pure conversion APIs: no filesystem access, network or subprocesses."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from .formats import (BridgeError, Cue, MAX_MS, decode_input, parse_srt, parse_vtt,
                      render_srt, render_vtt, validate_cues, validate_text,
                      validate_vtt_cue, validate_vtt_metadata)
from .mapping import MAPPING_SCHEMA, reconcile_mapping


SCHEMA = "audacity-subtitle-bridge/2"
LEGACY_SCHEMA = "audacity-subtitle-bridge/1"
ID_PATTERN = r"[A-Za-z0-9_-]{1,16}"
PREFIX = re.compile(rf"^\[cue:({ID_PATTERN})\](?: (.*))?$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BridgeError("Sidecar must contain finite, UTF-8 JSON data") from exc


def _integrity(sidecar: dict) -> str:
    return sha256(_canonical({k: v for k, v in sidecar.items() if k != "integrity_sha256"}))


def visible_text(text: str) -> str:
    """Readable, single-line projection; intentionally not a reversible encoding."""
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", " / ")


def export_subtitles(source: bytes, format: str) -> tuple[str, dict]:
    """Return Audacity label text and an integrity-checked, JSON-ready sidecar."""
    text = decode_input(source)
    if format == "srt":
        cues, metadata = parse_srt(text)
    elif format == "vtt":
        cues, metadata = parse_vtt(text)
    else:
        raise BridgeError("Only SRT and the documented WebVTT subset are supported")
    records = []
    rows = []
    for i, cue in enumerate(cues, 1):
        cue_id = (cue.source_id if format == "srt" and len(cue.source_id) <= 16
                  else f"c{i:x}")
        label = visible_text(cue.text)
        records.append({"id": cue_id, "source_id": cue.source_id,
                        "start_ms": cue.start_ms, "end_ms": cue.end_ms,
                        "text": cue.text, "label_text": label, "metadata": cue.metadata})
        rows.append(f"{cue.start_ms // 1000}.{cue.start_ms % 1000:03d}\t"
                    f"{cue.end_ms // 1000}.{cue.end_ms % 1000:03d}\t[cue:{cue_id}] {label}\n")
    labels = "".join(rows)
    sidecar = {"schema": SCHEMA,
               "source": {"format": format, "sha256": sha256(source), "metadata": metadata},
               "original_labels_sha256": sha256(labels.encode("utf-8")), "cues": records}
    sidecar["integrity_sha256"] = _integrity(sidecar)
    return labels, sidecar


def validate_sidecar(sidecar: dict) -> dict[str, dict]:
    if not isinstance(sidecar, dict) or sidecar.get("schema") not in (SCHEMA, LEGACY_SCHEMA):
        raise BridgeError("Unsupported sidecar schema; expected " + SCHEMA + " or " + LEGACY_SCHEMA)
    if sidecar.get("integrity_sha256") != _integrity(sidecar):
        raise BridgeError("Sidecar integrity hash mismatch: sidecar content changed/corrupted. "
                          "Re-export from the original subtitle; edited label hashes are NOT required.")
    source = sidecar.get("source")
    if (not isinstance(source, dict) or source.get("format") not in ("srt", "vtt")
            or not isinstance(source.get("metadata"), dict)):
        raise BridgeError("Invalid sidecar source metadata")
    for value in (source.get("sha256"), sidecar.get("original_labels_sha256")):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise BridgeError("Invalid sidecar source/label hash")
    records = sidecar.get("cues")
    if not isinstance(records, list) or not records:
        raise BridgeError("Sidecar must contain cue records")
    if source["format"] == "vtt":
        validate_vtt_metadata(source["metadata"], len(records))
    elif source["metadata"]:
        raise BridgeError("Unsupported SRT source metadata")
    by_id = {}
    cues = []
    for record in records:
        if not isinstance(record, dict) or not {"id", "source_id", "start_ms", "end_ms", "text",
                                               "label_text", "metadata"} <= record.keys():
            raise BridgeError("Invalid sidecar cue record")
        cue_id = record["id"]
        if not isinstance(cue_id, str) or not re.fullmatch(ID_PATTERN, cue_id):
            raise BridgeError("Invalid sidecar cue ID")
        if cue_id in by_id:
            raise BridgeError(f"Duplicate sidecar cue ID: {cue_id}")
        if (record["source_id"] is not None and not isinstance(record["source_id"], str)
                or not isinstance(record["metadata"], dict)):
            raise BridgeError("Invalid sidecar cue metadata")
        validate_text(record["text"])
        if record["label_text"] != visible_text(record["text"]):
            raise BridgeError(f"Sidecar cue {cue_id}: label projection disagrees with original text")
        cue = Cue(record["source_id"], record["start_ms"], record["end_ms"],
                  record["text"], record["metadata"])
        if source["format"] == "vtt":
            if sidecar["schema"] == LEGACY_SCHEMA and cue.metadata:
                raise BridgeError("Legacy sidecar schema cannot carry WebVTT cue settings; re-export")
            validate_vtt_cue(cue)
        elif cue.metadata:
            raise BridgeError("Unsupported SRT cue metadata")
        cues.append(cue)
        by_id[cue_id] = record
    validate_cues(cues)
    return by_id


def _seconds(value: str, context: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise BridgeError(f"{context}: invalid seconds {value!r}") from exc
    if not number.is_finite():
        raise BridgeError(f"{context}: time must be finite")
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,12})?", value):
        raise BridgeError(f"{context}: expected decimal seconds (1–9 integer digits, "
                          "at most 12 fractional digits; no sign/exponent)")
    if number < 0 or number > Decimal(MAX_MS) / 1000:
        raise BridgeError(f"{context}: time must be nonnegative and below 100 hours")
    return number


def _milliseconds(value: Decimal) -> int:
    return int((value * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _read_labels(labels: str, point_duration: str | None, report: dict) -> list[dict]:
    if not isinstance(labels, str):
        raise BridgeError("Labels must be a UTF-8 decoded string")
    labels = decode_input(labels.encode("utf-8"))
    duration = None
    if point_duration is not None:
        duration = _seconds(str(point_duration), "point duration")
        if _milliseconds(duration) < 1:
            raise BridgeError("Point duration must be at least 0.001 seconds")
    rows = []
    last = Decimal(-1)
    lines = labels.split("\n")
    for line_no, line in enumerate(lines, 1):
        if line == "" and line_no == len(lines):
            continue  # only the final line terminator is optional
        fields = line.split("\t")
        if fields[0] == "\\":
            raise BridgeError(f"Label line {line_no}: frequency continuation rows are unsupported")
        if len(fields) not in (2, 3):
            raise BridgeError(f"Label line {line_no}: expected 2 or 3 tab-separated fields")
        start = _seconds(fields[0], f"Label line {line_no} start")
        end = _seconds(fields[1], f"Label line {line_no} end")
        if start < last:
            raise BridgeError(f"Label line {line_no}: out of start-time order")
        last = start
        if end < start:
            raise BridgeError(f"Label line {line_no}: reversed times")
        if end == start:
            if duration is None:
                raise BridgeError(f"Label line {line_no}: point label requires --point-duration SECONDS")
            end = start + duration
            report["actions"].append({"kind": "point_duration", "line": line_no,
                                      "seconds": str(duration)})
        start_ms, end_ms = _milliseconds(start), _milliseconds(end)
        if not 0 <= start_ms < end_ms <= MAX_MS:
            raise BridgeError(f"Label line {line_no}: interval collapses at SRT millisecond precision "
                              "or exceeds 100 hours")
        if start * 1000 != start_ms or end * 1000 != end_ms:
            report["actions"].append({"kind": "millisecond_rounding", "line": line_no,
                                      "from_seconds": [str(start), str(end)],
                                      "to_ms": [start_ms, end_ms]})
        rows.append({"line": line_no, "start_ms": start_ms, "end_ms": end_ms,
                     "label": fields[2] if len(fields) == 3 else ""})
    return rows


def import_labels(labels: str, sidecar: dict | None = None, *, text_policy="error",
                  missing_policy="error", new_policy="error", allow_lossy=False,
                  point_duration: str | None = None, source: bytes | None = None,
                  output_format="srt", mapping: dict | None = None,
                  settings_loss_policy="warn", identifier_loss_policy="warn",
                  document_metadata_loss_policy="warn",
                  note_relocation_policy="allow") -> tuple[str, dict]:
    """Reconcile edited labels. Policies are explicit and failures include an audit.

    ``edited`` uses the visible label payload literally as one line: the display
    projection is not decoded. Missing cues may be kept at original times or dropped.
    Duplicate and unknown IDs always fail; they cannot be safely auto-reconciled.
    ``output_format='vtt'`` restores supported VTT metadata; default SRT reports
    unrepresentable metadata in ``losses`` and warnings.
    ``settings_loss_policy='error'`` refuses SRT when retained output cues carry
    WebVTT settings. Independently, ``identifier_loss_policy='error'`` refuses
    original WebVTT identifiers lost to SRT or merged in either output format.
    Both policies check reconciled outputs; dropped originals do not count.
    Independently, ``document_metadata_loss_policy='error'`` refuses SRT when
    a WebVTT header/NOTE loss is reported, even when all cues are dropped.
    VTT retains supported document text; NOTE relocation remains an action.
    Independently, ``note_relocation_policy='error'`` refuses VTT when any
    original NOTE is outside the preamble, even when all cues are dropped.
    ``mapping`` explicitly declares lossless split/merge compositions, bound to
    this sidecar and the decoded labels. Mapped rows never use guessing/policies.
    """
    if text_policy not in ("error", "original", "edited"):
        raise BridgeError("Unknown text policy")
    if missing_policy not in ("error", "keep", "drop"):
        raise BridgeError("Unknown missing policy")
    if new_policy not in ("error", "add", "ignore"):
        raise BridgeError("Unknown new-label policy")
    if output_format not in ("srt", "vtt"):
        raise BridgeError("Output format must be srt or vtt")
    if settings_loss_policy not in ("warn", "error"):
        raise BridgeError("Unknown settings-loss policy; expected warn or error")
    if identifier_loss_policy not in ("warn", "error"):
        raise BridgeError("Unknown identifier-loss policy; expected warn or error")
    if document_metadata_loss_policy not in ("warn", "error"):
        raise BridgeError("Unknown document-metadata-loss policy; expected warn or error")
    if note_relocation_policy not in ("allow", "error"):
        raise BridgeError("Unknown note-relocation policy; expected allow or error")
    report = {"schema": "audacity-subtitle-bridge-report/1", "status": "ok",
              "output_format": output_format, "settings_loss_policy": settings_loss_policy,
              "identifier_loss_policy": identifier_loss_policy,
              "document_metadata_loss_policy": document_metadata_loss_policy,
              "note_relocation_policy": note_relocation_policy,
              "losses": [],
              "warnings": [], "actions": [], "conflicts": [], "output_cues": []}
    try:
        by_id = validate_sidecar(sidecar) if sidecar is not None else None
        if by_id is None:
            if not allow_lossy:
                raise BridgeError("No sidecar: --allow-lossy is required; cue identity, multiline "
                                  "text and metadata cannot be recovered")
            if source is not None:
                raise BridgeError("--source requires a sidecar")
            report["warnings"].append("LOSSY: no sidecar; label text is literal single-line text, "
                                      "and original identity/metadata cannot be recovered")
        else:
            report["source"] = sidecar["source"]
            report["source_verified"] = source is not None
            if source is not None and sha256(source) != sidecar["source"]["sha256"]:
                raise BridgeError("Source hash mismatch: this sidecar belongs to different original "
                                  "subtitle bytes (not a hash of the edited labels)")
            report["labels_changed_since_export"] = (
                sha256(labels.encode("utf-8")) != sidecar["original_labels_sha256"])
        rows = _read_labels(labels, point_duration, report)
        if not rows and by_id is None:
            raise BridgeError("Input contains no labels")
        mapped, seen = ({}, set()) if mapping is None else reconcile_mapping(
            mapping, sidecar, by_id, rows, sha256(labels.encode('utf-8')), visible_text, report)
        output = []
        for row in rows:
            if row['line'] in mapped:
                output.append((mapped[row['line']][0], None, row['line']))
                continue
            label = row["label"]
            match = PREFIX.fullmatch(label) if by_id is not None else None
            if by_id is not None and label.startswith("[cue:") and match is None:
                raise BridgeError(f"Label line {row['line']}: malformed reserved [cue:ID] prefix")
            cue_id = match[1] if match else None
            record = None
            if cue_id is not None:
                if cue_id not in by_id:
                    raise BridgeError(f"Label line {row['line']}: unknown cue ID {cue_id}; "
                                      "check the sidecar/prefix")
                if cue_id in seen:
                    raise BridgeError(f"Duplicate label cue ID: {cue_id}; already returned or consumed by mapping")
                seen.add(cue_id)
                record = by_id[cue_id]
                edited = match[2] or ""
                changed = edited != record["label_text"]
                if changed:
                    detail = {"kind": "changed_text", "id": cue_id, "line": row["line"],
                              "original_text": record["text"], "original_label": record["label_text"],
                              "edited_label": edited, "policy": text_policy}
                    report["actions"].append(detail)
                    if text_policy == "error":
                        report["conflicts"].append(detail)
                text = edited if changed and text_policy == "edited" else record["text"]
                if (row["start_ms"], row["end_ms"]) != (record["start_ms"], record["end_ms"]):
                    report["actions"].append({"kind": "moved", "id": cue_id,
                                              "from_ms": [record["start_ms"], record["end_ms"]],
                                              "to_ms": [row["start_ms"], row["end_ms"]]})
            else:
                if by_id is not None:
                    detail = {"kind": "new_label", "line": row["line"], "text": label,
                              "policy": new_policy}
                    report["actions"].append(detail)
                    if new_policy == "error":
                        report["conflicts"].append(detail)
                        continue
                    if new_policy == "ignore":
                        continue
                text = label
            validate_text(text)
            cue = Cue(record["source_id"] if record else None, row["start_ms"], row["end_ms"],
                      text, dict(record["metadata"]) if record else {})
            output.append((cue, cue_id, row["line"]))
        if by_id is not None:
            for cue_id, record in by_id.items():
                if cue_id not in seen:
                    detail = {"kind": "missing_cue", "id": cue_id, "policy": missing_policy}
                    report["actions"].append(detail)
                    if missing_policy == "error":
                        report["conflicts"].append(detail)
                    elif missing_policy == "keep":
                        output.append((Cue(record["source_id"], record["start_ms"], record["end_ms"],
                                           record["text"], dict(record["metadata"])), cue_id, None))
        if report["conflicts"]:
            raise BridgeError("Unresolved edits: choose --text-policy, --missing-policy and/or "
                              "--new-policy; inspect the JSON conflict report")
        output.sort(key=lambda item: item[0].start_ms)
        report["output_cues"] = [{f"{output_format}_index": i, "id": cue_id, "line": line,
                                   "source_id": cue.source_id,
                                   "start_ms": cue.start_ms, "end_ms": cue.end_ms,
                                   "metadata": dict(cue.metadata),
                                   **(mapped[line][1] if line in mapped else {})}
                                  for i, (cue, cue_id, line) in enumerate(output, 1)]
        report["output_count"] = len(output)
        # Merge losses are recorded in operation order during mapping validation.
        # Bind them to final rows only after kept/new cues have been reconciled
        # and sorted. Each merge operation has exactly one output.
        index_key = f"{output_format}_index"
        mapped_outputs = {cue["mapping_operation"]: cue
                          for cue in report["output_cues"] if "mapping_operation" in cue}
        for loss in report["losses"]:
            if loss["kind"] == "merged_cue_identifiers":
                output_cue = mapped_outputs[loss["mapping_operation"]]
                loss.update({key: output_cue[key] for key in (index_key, "line")})
        vtt_source = by_id is not None and sidecar["source"]["format"] == "vtt"
        cues = [cue for cue, _, _ in output]
        if output_format == "vtt":
            metadata = sidecar["source"]["metadata"] if vtt_source else None
            relocated_notes = [{"note_index": i, "before_cue": note["before_cue"], "text": note["text"]}
                               for i, note in enumerate(metadata["notes"], 1) if note["before_cue"] != 0
                               ] if metadata else []
            if relocated_notes:
                report["actions"].append({"kind": "webvtt_notes_to_preamble",
                                          "reason": "NOTE text retained in source order before cues",
                                          "relocated_notes": relocated_notes})
        if output_format == "srt" and vtt_source:
            report["warnings"].append("WebVTT identifiers/header/notes remain in sidecar/report; "
                                      "SRT does not carry WebVTT metadata")
            metadata = sidecar["source"]["metadata"]
            fields = (["header"] if metadata["header"] != "WEBVTT" else [])
            if metadata["notes"]:
                fields.append("notes")
            if fields:
                report["losses"].append({"kind": "webvtt_document_metadata", "fields": fields,
                                         "reason": "SRT cannot represent WebVTT header/notes",
                                         **({"source_header": metadata["header"]} if "header" in fields else {}),
                                         **({"source_notes": [
                                             {"note_index": i, "before_cue": note["before_cue"], "text": note["text"]}
                                             for i, note in enumerate(metadata["notes"], 1)]}
                                            if "notes" in fields else {})})
            for (cue, cue_id, _), output_cue in zip(output, report["output_cues"]):
                provenance = {key: output_cue[key] for key in
                              ("srt_index", "line", "mapping_operation", "source_ids")
                              if key in output_cue}
                if cue.source_id is not None:
                    report["losses"].append({"kind": "webvtt_cue_identifier", "id": cue_id,
                                             "source_id": cue.source_id,
                                             **provenance,
                                             "reason": "SRT indices replace WebVTT identifiers"})
                if cue.metadata.get("settings"):
                    source_ids = output_cue.get("source_ids", [])
                    report["losses"].append({"kind": "webvtt_cue_settings", "id": cue_id,
                                             "source_id": cue.source_id,
                                             "settings": cue.metadata["settings"],
                                             **provenance,
                                             **({"source_identifiers": [by_id[i]["source_id"] for i in source_ids]}
                                                if len(source_ids) > 1 else {}),
                                             "reason": "SRT cannot represent WebVTT cue settings"})
            if any(cue.metadata.get("settings") for cue in cues):
                report["warnings"].append("LOSS: WebVTT cue settings cannot be represented in SRT; "
                                          "see losses for each cue or choose --format vtt")
                if settings_loss_policy == "error":
                    raise BridgeError("WebVTT cue settings would be lost in SRT; choose --format vtt "
                                      "(library: output_format='vtt') to retain them, or explicitly "
                                      "choose --settings-loss-policy warn to accept the loss")
        if identifier_loss_policy == "error" and any(
                loss["kind"] in ("webvtt_cue_identifier", "merged_cue_identifiers")
                for loss in report["losses"]):
            raise BridgeError("WebVTT cue identifiers would be lost in reconciled output; "
                              "retain separate cues with --format vtt "
                              "(library: output_format='vtt'), or explicitly choose "
                              "--identifier-loss-policy warn to accept the loss")
        if document_metadata_loss_policy == "error" and any(
                loss["kind"] == "webvtt_document_metadata" for loss in report["losses"]):
            raise BridgeError("WebVTT document metadata (header/NOTE text) would be lost in SRT; "
                              "choose --format vtt (library: output_format='vtt') to retain it, "
                              "or explicitly choose --document-metadata-loss-policy warn "
                              "to accept the loss")
        if note_relocation_policy == "error" and any(
                action["kind"] == "webvtt_notes_to_preamble" for action in report["actions"]):
            raise BridgeError("WebVTT NOTE relocation to the output preamble would occur; "
                              "explicitly choose --note-relocation-policy allow to accept it "
                              "(library: note_relocation_policy='allow')")
        return (render_vtt(cues, metadata) if output_format == "vtt" else render_srt(cues)), report
    except BridgeError as exc:
        report["status"] = "error"
        exc.report = report
        raise


def create_mapping_template(labels: str, sidecar: dict, operations: list, *,
                            point_duration: str | None = None,
                            missing_policy="error", new_policy="error",
                            source: bytes | None = None) -> dict:
    """Bind caller-declared operations to inputs after strict reconciliation.

    No IDs, target lines, separators or split texts are inferred. Unmapped rows
    use explicit missing/new policies with error defaults; text stays strict.
    These validation options must be supplied again at import, as they are not
    stored in mapping/1. Validate in the source format so
    template creation does not choose a conversion loss policy for the caller.
    Hash the exact labels string, as import_labels does; CLI decoding normalizes
    BOM/CRLF before calling either function.
    Optional ``source`` (original subtitle bytes) uses the same SHA-256 check as
    import_labels before a mapping is returned. It is an integrity check of the
    pairing, not authentication, and it is not stored in mapping/1.
    """
    validate_sidecar(sidecar)
    if not isinstance(labels, str):
        raise BridgeError("Labels must be a UTF-8 decoded string")
    mapping = {"schema": MAPPING_SCHEMA,
               "sidecar_integrity_sha256": sidecar["integrity_sha256"],
               "labels_sha256": sha256(labels.encode("utf-8")),
               "operations": deepcopy(operations)}
    import_labels(labels, sidecar, mapping=mapping, point_duration=point_duration,
                  missing_policy=missing_policy, new_policy=new_policy, source=source,
                  output_format=sidecar["source"]["format"])
    return mapping
