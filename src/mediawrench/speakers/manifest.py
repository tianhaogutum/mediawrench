"""Explicit offline file pairing; every recording/channel gets a fresh merge."""

import hashlib
import os
from pathlib import Path
import shutil

from .core import BridgeError, _validate_merge_options, load_json, merge, parse_rttm
from .export import render_json, render_markdown, render_srt, write_atomic


OPTIONS = frozenset({"min_coverage", "tie_epsilon", "max_gap", "rttm_offset_seconds",
                     "rttm_drift_ppm", "recording_duration", "rttm_out_of_range_policy"})
EXPORT_SUFFIXES = {"srt": "srt", "markdown": "md"}


def _object(value: object, allowed: set | frozenset, where: str) -> dict:
    if not isinstance(value, dict):
        raise BridgeError(f"{where}: expected an object")
    extra = value.keys() - allowed
    if extra:
        raise BridgeError(f"{where}: unknown keys: {', '.join(sorted(extra))}")
    return value


def _path(value: object, base: Path, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise BridgeError(f"{where}: expected a nonempty file path without NUL")
    return str((base / value).absolute())


def _entries(path: Path, data: bytes) -> list[dict]:
    manifest = _object(load_json(data.decode("utf-8")),
                       {"schema_version", "entries"}, "manifest")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise BridgeError("manifest.schema_version: expected integer 1")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BridgeError("manifest.entries: expected a nonempty array")
    identities = {}
    validated = []
    for index, raw in enumerate(entries):
        where = f"entries[{index}]"
        entry = _object(raw, {"recording", "channel", "asr", "rttm", "aliases", "options", "exports",
                              "input_sha256"}, where)
        recording = entry.get("recording")
        if (not isinstance(recording, str) or not recording or
                any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in recording)):
            raise BridgeError(f"{where}.recording: explicit nonempty RTTM recording token required")
        channel = entry.get("channel")
        if type(channel) is int:
            channel = str(channel)
        if (not isinstance(channel, str) or not channel.isascii() or not channel.isdigit() or
                not channel.strip("0")):
            raise BridgeError(f"{where}.channel: explicit positive integer channel required")
        channel = channel.lstrip("0")
        identity = (recording, channel)
        if identity in identities:
            raise BridgeError(f"{where}: duplicate recording/channel {recording!r}/{channel}; "
                              f"already bound by entries[{identities[identity]}]")
        identities[identity] = index
        item = dict(index=index, recording=recording, channel=channel)
        exports = entry.get("exports", [])
        if (not isinstance(exports, list) or
                any(not isinstance(export, str) or export not in EXPORT_SUFFIXES for export in exports) or
                len(set(exports)) != len(exports)):
            raise BridgeError(f"{where}.exports: expected an array of unique formats: srt, markdown")
        if exports:
            item["exports"] = {export: dict(status="skipped", file=None) for export in exports}
        for key in ("asr", "rttm"):
            item[key] = _path(entry.get(key), path.parent, f"{where}.{key}")
        if "aliases" in entry:
            item["aliases"] = _path(entry["aliases"], path.parent, f"{where}.aliases")
        if "input_sha256" in entry:
            digests = _object(entry["input_sha256"], {"asr", "rttm", "aliases"},
                              f"{where}.input_sha256")
            for key, digest in digests.items():
                if key not in item:
                    raise BridgeError(f"{where}.input_sha256.{key}: requires a {key} path")
                if (not isinstance(digest, str) or len(digest) != 64 or
                        any(c not in "0123456789abcdef" for c in digest)):
                    raise BridgeError(f"{where}.input_sha256.{key}: expected 64 lowercase hex characters")
            item["input_sha256"] = dict(digests)
        item["options"] = dict(_object(entry.get("options", {}), OPTIONS, f"{where}.options"))
        # JSON exponents such as 1e309 can overflow the decoder's float without
        # using a NaN/Infinity token. Reject them during manifest preflight too,
        # so retained options cannot break summary serialization after merges.
        try:
            render_json(item["options"])
        except BridgeError as exc:
            raise BridgeError(f"{where}.options: nonfinite or unsupported JSON value") from exc
        validated.append(item)
    return validated


def _entry_error(entry: dict, stage: str, exc: Exception) -> dict:
    return dict(stage=stage, message=(
        f"entries[{entry['index']}] ({entry['recording']}/{entry['channel']}) {stage}: {exc}"))


def _read_input(entry: dict, key: str) -> str:
    # Hash and parse the same read, so a later path mutation cannot bypass a check.
    data = Path(entry[key]).read_bytes()
    expected = entry.get("input_sha256", {}).get(key)
    if expected is not None or "observed_inputs" in entry:
        actual = hashlib.sha256(data).hexdigest()
        if "observed_inputs" in entry:
            entry["observed_inputs"][key] = dict(sha256=actual, size_bytes=len(data))
    if expected is not None:
        if actual != expected:
            raise BridgeError(f"SHA-256 mismatch for {entry[key]}: expected {expected}, actual {actual}")
    return data.decode("utf-8")


def _validate_manifest_sha256(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 64 or
            any(c not in "0123456789abcdef" for c in value)):
        raise BridgeError("expected_manifest_sha256: expected 64 lowercase ASCII hex characters")
    return value


def _preflight_manifest(manifest_path: str | Path, expected_manifest_sha256: str | None,
                        require_input_sha256: bool, observe_input_sha256: bool) -> tuple:
    """Read/check/parse one raw snapshot; never inspect entry files or destinations."""
    if type(observe_input_sha256) is not bool:
        raise BridgeError("observe_input_sha256: expected a boolean")
    if type(require_input_sha256) is not bool:
        raise BridgeError("require_input_sha256: expected a boolean")
    if expected_manifest_sha256 is not None:
        _validate_manifest_sha256(expected_manifest_sha256)
    path = Path(manifest_path).absolute()
    manifest_bytes = path.read_bytes()
    if expected_manifest_sha256 is not None or observe_input_sha256:
        actual = hashlib.sha256(manifest_bytes).hexdigest()
        if expected_manifest_sha256 is not None and actual != expected_manifest_sha256:
            raise BridgeError(f"manifest SHA-256 mismatch for {path}: "
                              f"expected {expected_manifest_sha256}, actual {actual}")
    entries = _entries(path, manifest_bytes)  # Preflight ALL identities before any merge or write.
    if require_input_sha256:
        for entry in entries:
            for key in ("asr", "rttm", "aliases"):
                if key in entry and key not in entry.get("input_sha256", {}):
                    raise BridgeError(
                        f"entries[{entry['index']}] ({entry['recording']}/{entry['channel']}) "
                        f"input_sha256.{key}: required expected SHA-256 for {entry[key]}")
    observed_manifest = (dict(sha256=actual,
                              size_bytes=len(manifest_bytes)) if observe_input_sha256 else None)
    return path, entries, observed_manifest


def validate_manifest(manifest_path: str | Path, *,
                      expected_manifest_sha256: str | None = None,
                      require_input_sha256: bool = False,
                      validate_options: bool = False) -> dict:
    """Validate schema/digest policies, optionally input-independent option values.

    Read the manifest once; hash, check and parse that same raw buffer. Return a
    separate manifest_validation report with its snapshot and resolved bindings.
    Entry files need not exist: no entry reads, filesystem lookup, merge, content
    validation, digest matching, export rendering or output directory is involved.
    Strict boolean validate_options defaults to False: options are structural
    only and the report is unchanged. True checks shared merge configuration
    rules after all schema/digest syntax and required coverage, then adds
    validation_scope="structure_and_option_values". Supplied options are retained
    verbatim; no defaults or normalized/inferred values are added to the report.
    It does not check corrected intervals, ASR bounds, aliases or merge success.
    Type/hash syntax and external manifest mismatch retain preflight precedence.
    Validation/IO/decode errors raise; no report is returned on failure.
    """
    if type(validate_options) is not bool:
        raise BridgeError("validate_options: expected a boolean")
    path, entries, snapshot = _preflight_manifest(
        manifest_path, expected_manifest_sha256, require_input_sha256, True)
    for entry in entries:
        if validate_options:
            try:
                _validate_merge_options(**entry["options"])
            except BridgeError as exc:
                raise BridgeError(
                    f"entries[{entry['index']}] ({entry['recording']}/{entry['channel']}) "
                    f"options: {exc}") from exc
        # Preflight keeps processing placeholders; reports describe requests only.
        if "exports" in entry:
            entry["exports"] = list(entry["exports"])
    report = dict(
        schema_version=1, report_type="manifest_validation", status="validated",
        manifest=str(path), manifest_snapshot=snapshot,
        expected_manifest_sha256=expected_manifest_sha256,
        require_input_sha256=require_input_sha256,
        input_contents_checked=False, input_digests_checked=False, outputs_written=False,
        counts=dict(entries=len(entries),
                    referenced_inputs=sum(key in entry for entry in entries
                                          for key in ("asr", "rttm", "aliases")),
                    expected_inputs=sum(len(entry.get("input_sha256", {})) for entry in entries)),
        entries=entries)
    if validate_options:
        report["validation_scope"] = "structure_and_option_values"
    return report


def diagnose_manifest_options(manifest_path: str | Path, *,
                              expected_manifest_sha256: str | None = None,
                              require_input_sha256: bool = False) -> dict:
    """Collect one input-independent configuration error per invalid entry.

    Separate from validate_manifest's first-error contract. Complete structural,
    hash and required-coverage preflight must pass before option collection.
    Return a manifest_option_diagnostics report even for invalid option values;
    preflight/IO/decode failures still raise without a report. Ordered option_errors
    retain the shared merge rules' first error per entry. Supplied values and
    bindings remain verbatim; no defaults or inferred values are reported.
    No entry lookup/read, merge or output is involved. options_valid describes
    configuration only, never input contents/digest matches or processing success.
    """
    report = validate_manifest(manifest_path,
                               expected_manifest_sha256=expected_manifest_sha256,
                               require_input_sha256=require_input_sha256)
    errors = []
    for entry in report["entries"]:
        try:
            _validate_merge_options(**entry["options"])
        except BridgeError as exc:
            errors.append(dict(index=entry["index"], recording=entry["recording"],
                               channel=entry["channel"], **_entry_error(entry, "options", exc)))
    report.update(report_type="manifest_option_diagnostics",
                  validation_scope="structure_and_option_values",
                  status="options_invalid" if errors else "options_valid", option_errors=errors)
    report["counts"]["invalid_option_entries"] = len(errors)
    return report


def diagnose_manifest_input_coverage(manifest_path: str | Path, *,
                                     expected_manifest_sha256: str | None = None) -> dict:
    """List all referenced slots lacking supplied expected SHA-256 values.

    Complete schema/digest-syntax and optional external manifest-hash checks
    precede collection. No required-coverage policy is accepted: this separate
    diagnostic reports missing slots instead of raising at the first one.
    Ordinary validation, option diagnostics and processing retain that policy.
    Each slot appears once in manifest then asr/rttm/aliases order, even when
    paths are shared. No entry lookup/read, option-value checks, merge or output
    is involved. coverage_complete describes supplied expectations only, never
    actual digest matches, valid configuration/content or processing success.
    Supplied values remain unchanged; no hashes or defaults are inferred.
    """
    report = validate_manifest(manifest_path,
                               expected_manifest_sha256=expected_manifest_sha256)
    missing = []
    for entry in report["entries"]:
        for key in ("asr", "rttm", "aliases"):
            if key in entry and key not in entry.get("input_sha256", {}):
                missing.append(dict(index=entry["index"], recording=entry["recording"],
                                    channel=entry["channel"], input=key, path=entry[key]))
    del report["require_input_sha256"]
    report.update(report_type="manifest_input_coverage",
                  validation_scope="expected_input_coverage", option_values_checked=False,
                  status="coverage_incomplete" if missing else "coverage_complete",
                  missing_input_sha256=missing)
    report["counts"]["missing_inputs"] = len(missing)
    return report


def run_manifest(manifest_path: str | Path, output_dir: str | Path, *, dry_run: bool = False,
                 observe_input_sha256: bool = False,
                 expected_manifest_sha256: str | None = None,
                 require_input_sha256: bool = False) -> dict:
    """Write JSON evidence, requested SRT/Markdown and a summary to a NEW directory.

    Missing/duplicate identities and invalid manifest structure raise BridgeError
    before any entry is processed. Input/merge failures are instead recorded per
    entry, allowing subsequent entries to run. The returned summary explicitly
    distinguishes complete, partial and all-failed runs. Each export is attempted
    independently after JSON succeeds; render failures retain all other outputs,
    mark the entry partial and record contextual errors by format. The existing
    entry.error field holds the first failure for compatibility.
    Counts succeeded/failed describe entire entries, items/turns all retained JSON.
    Requested companions also have aggregate requested/succeeded/failed/skipped
    export_counts; JSON-only summaries omit this field for compatibility.
    Optional input_sha256 expectations bind file bytes before decoding/parsing;
    mismatches are input-stage failures and skip all requested companions.
    Opt-in observe_input_sha256 adds observed_inputs per entry: sha256/size_bytes
    for each completed byte read, even on mismatch/decode/parse failure. Unread
    inputs are absent; observations do not mean successful validation.
    It also adds observed_manifest for the single raw manifest snapshot parsed.
    Optional expected_manifest_sha256 requires 64 lowercase ASCII hex characters
    and checks that snapshot before decoding/preflight. A mismatch rejects the
    whole run before entry reads/output; a match adds no summary fields and does
    not enable observation. None preserves the default unchecked manifest.
    Strict boolean require_input_sha256 defaults to False (partial/omitted input
    expectations allowed). True requires an expected digest for every referenced
    ASR/RTTM/aliases input after schema validation, before any entry read/output.
    Missing coverage rejects the whole run; complete coverage adds no fields and
    leaves content mismatches as entry failures. Hashes are never inferred.
    Invalid manifests raise before any summary is returned or written.
    Dry-run renders all requested formats without writing. IDs and speakers are
    local to each result; paths are relative to the manifest, never the cwd.
    """
    path, entries, observed_manifest = _preflight_manifest(
        manifest_path, expected_manifest_sha256, require_input_sha256, observe_input_sha256)
    output = Path(output_dir)
    if os.path.lexists(output):
        raise BridgeError(f"output directory already exists: {output}; choose a new directory")
    if not output.parent.is_dir():
        raise BridgeError(f"output parent directory must exist: {output.parent}")
    results = {}
    renderers = {"srt": render_srt, "markdown": render_markdown}
    succeeded = json_succeeded = items = turns = 0
    for entry in entries:
        if observe_input_sha256:
            entry["observed_inputs"] = {}
        entry["result"] = None
        stage = "asr"
        try:
            asr = load_json(_read_input(entry, "asr"))
            stage = "aliases"
            aliases = (load_json(_read_input(entry, "aliases"))
                       if "aliases" in entry else None)
            stage = "rttm"
            diarization = parse_rttm(_read_input(entry, "rttm"),
                                     entry["recording"], entry["channel"])
            stage = "merge"
            result = merge(asr, diarization, aliases=aliases, **entry["options"])
            stage = "render"
            filename = f"entry-{entry['index']:04d}.json"
            results[filename] = render_json(result)
            entry.update(status="ok", result=filename, items=len(result["items"]),
                         turns=len(result["turns"]), warnings=result["warnings"])
            json_succeeded += 1
            items += entry["items"]
            turns += entry["turns"]
        except (BridgeError, OSError, UnicodeError) as exc:
            entry.update(status="error", error=_entry_error(entry, stage, exc))
            continue
        for format_, outcome in entry.get("exports", {}).items():
            try:
                filename = f"entry-{entry['index']:04d}.{EXPORT_SUFFIXES[format_]}"
                results[filename] = renderers[format_](result)
                outcome.update(status="ok", file=filename)
            except (BridgeError, OSError, UnicodeError) as exc:
                error = _entry_error(entry, f"export:{format_}", exc)
                outcome["status"] = "error"
                entry["status"] = "partial"
                entry.setdefault("error", error)
                entry.setdefault("export_errors", {})[format_] = error
        if entry["status"] == "ok":
            succeeded += 1
    failed = len(entries) - succeeded
    summary = dict(schema_version=1, manifest=str(path), dry_run=dry_run,
                   status="complete" if not failed else ("partial" if json_succeeded else "failed"),
                   partial_results=bool(failed and json_succeeded),
                   counts=dict(total=len(entries), succeeded=succeeded, failed=failed,
                               items=items, turns=turns), entries=entries)
    if observe_input_sha256:
        summary["observed_manifest"] = observed_manifest
    for format_ in EXPORT_SUFFIXES:
        outcomes = [entry["exports"][format_]["status"] for entry in entries
                    if format_ in entry.get("exports", {})]
        if outcomes:
            summary.setdefault("export_counts", {})[format_] = dict(
                requested=len(outcomes), succeeded=outcomes.count("ok"),
                failed=outcomes.count("error"), skipped=outcomes.count("skipped"))
    summary_text = render_json(summary)
    if not dry_run:
        # Exclusive creation prevents replacing input/output directories, including
        # symlinks. The summary is the completion marker, written after results.
        output.mkdir()
        try:
            for filename, text in results.items():
                write_atomic(output / filename, text)
            write_atomic(output / "summary.json", summary_text)
        except (OSError, UnicodeError, BridgeError):
            shutil.rmtree(output)
            raise
    return summary
