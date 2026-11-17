"""An explicit anchor ledger around ChronoSync's existing algorithms (vendored in ``_chronosync``)."""

from __future__ import annotations

import csv
import copy
import io
from pathlib import Path
import re

import numpy as np
from . import __version__
from ._chronosync import __version__ as CHRONOSYNC_VERSION
from ._chronosync.gcc_phat import gcc_phat
from ._chronosync.regression import weighted_linear_fit
from .media import executable, probe, window
from .storage import ReviewError, check_identity, finite, integer, read_json


SCHEMA = "chronosync-drift-review/v1"
CONVENTION = "target_seconds = scale * reference_seconds + offset_seconds"
ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")


def matched_window(reference: np.ndarray, target: np.ndarray, rate: int,
                   search: float) -> dict:
    """Delegate correlation/peak/polarity handling to the vendored upstream."""
    length = min(len(reference), len(target))
    if length < rate // 20:
        return {"success": False, "delay_seconds": None, "confidence": 0.0,
                "similarity": None, "warnings": ["insufficient decoded audio"]}
    reference = reference[:length]
    target = target[:length]
    radius = min(round(search * rate), length - 1)
    result = gcc_phat(reference, target, rate, search_min=-radius, search_max=radius)
    delay = float(result.delay_seconds) if result.success else None
    similarity = None
    if result.success:
        lag = round(result.delay_samples)
        if lag >= 0:
            left, right = reference[:length - lag], target[lag:]
        else:
            left, right = reference[-lag:], target[:length + lag]
        left = left.astype(np.float64) - float(np.mean(left))
        right = right.astype(np.float64) - float(np.mean(right))
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        similarity = float(np.dot(left, right) / denominator) if denominator > 0 else 0.0
    warnings = list(result.warnings)
    if delay is not None and abs(delay) >= search - 2 / rate:
        warnings.append("peak is at the search boundary; widen search or adjust the hint")
    return {"success": bool(result.success), "delay_seconds": delay,
            "confidence": float(result.confidence), "similarity": similarity,
            "polarity": result.polarity, "peak": float(result.peak_value),
            "warnings": warnings}


def analyze(reference: Path, target: Path, *, offset_hint: float = 0,
            window_seconds: float = 4, search_seconds: float = 1,
            positions=(0.1, 0.5, 0.9), rate: int = 8000,
            reference_stream: int = 0, target_stream: int = 0,
            reference_channel: int = 0, target_channel: int = 0) -> dict:
    offset_hint = finite(offset_hint, "offset hint")
    window_seconds = finite(window_seconds, "window seconds", minimum=0.25, maximum=30)
    search_seconds = finite(search_seconds, "search seconds", minimum=0.001,
                            maximum=window_seconds * 0.45)
    rate = integer(rate, "analysis sample rate", 1000, 48000)
    executable("ffmpeg")
    if not 2 <= len(positions) <= 101:
        raise ReviewError("provide 2 to 101 fractional positions")
    positions = sorted(finite(p, "position", minimum=0, maximum=1) for p in positions)
    if len(set(positions)) != len(positions):
        raise ReviewError("positions must be distinct")
    ref = probe(reference, stream=reference_stream, channel=reference_channel)
    tgt = probe(target, stream=target_stream, channel=target_channel)
    low = max(0.0, -offset_hint)
    high = min(ref["duration_seconds"], tgt["duration_seconds"] - offset_hint)
    if high - low < window_seconds + 0.01:
        raise ReviewError("hinted overlap is too short for this window; use a smaller --window or correct --offset-hint")
    centers = [low + window_seconds / 2 + p * (high - low - window_seconds) for p in positions]
    anchors = []
    for index, center in enumerate(centers):
        start = max(0.0, center - window_seconds / 2)
        try:
            ref_audio = window(ref, start, window_seconds, rate=rate)
            tgt_audio = window(tgt, max(0.0, start + offset_hint), window_seconds, rate=rate)
            match = matched_window(ref_audio, tgt_audio, rate, search_seconds)
        except ReviewError as exc:
            match = {"success": False, "delay_seconds": None, "confidence": 0.0,
                     "similarity": None, "warnings": [f"window could not be measured: {exc}"]}
        if len(centers) == 3:
            anchor_id = ("start", "middle", "end")[index]
        else:
            anchor_id = f"anchor-{index + 1:03d}"
        candidate = center + offset_hint + match["delay_seconds"] if match["success"] else None
        if candidate is not None and not 0 <= candidate < tgt["duration_seconds"]:
            match["warnings"].append("proposed event falls outside the target; enter a manual pair")
            candidate = None
        usable = (match["success"] and match["confidence"] >= 0.6
                  and abs(match["similarity"] or 0) >= 0.2
                  and candidate is not None)
        anchors.append({"id": anchor_id, "reference_seconds": center,
                        "target_seconds": candidate, "accepted": False,
                        "origin": "automatic", "suggested_usable": bool(usable),
                        "measurement": match, "note": ""})
    check_identity(ref)
    check_identity(tgt)
    return {"schema": SCHEMA, "reference": ref, "target": tgt,
            "software": {"drift_review": __version__, "chronosync": CHRONOSYNC_VERSION},
            "convention": CONVENTION,
            "analysis": {"sample_rate": rate, "window_seconds": window_seconds,
                         "search_seconds": search_seconds, "offset_hint_seconds": offset_hint,
                         "positions": positions,
                         "channel_policy": "one explicitly selected channel; rendering keeps all target channels"},
            "anchors": anchors}


def validate_session(session: dict, *, base: Path | None = None, identities: bool = True) -> dict:
    if session.get("schema") != SCHEMA:
        raise ReviewError(f"unsupported review schema; expected {SCHEMA}")
    if session.get("convention", CONVENTION) != CONVENTION:
        raise ReviewError("unsupported time convention; times must map reference seconds to target seconds")
    for label in ("reference", "target"):
        media = session.get(label)
        if not isinstance(media, dict) or not isinstance(media.get("path"), str):
            raise ReviewError(f"{label} must include a local media path")
        path = Path(media["path"]).expanduser()
        if not path.is_absolute():
            path = (base or Path.cwd()) / path
        media["path"] = str(path.resolve())
        media["duration_seconds"] = finite(media.get("duration_seconds"), f"{label} duration", minimum=0.001)
        media["sample_rate"] = integer(media.get("sample_rate"), f"{label} rate", 1000, 384000)
        channels = integer(media.get("channels"), f"{label} channels", 1, 64)
        media["channels"] = channels
        media["audio_stream"] = integer(media.get("audio_stream"), f"{label} stream", 0, 31)
        media["analysis_channel"] = integer(media.get("analysis_channel"), f"{label} channel", 0, channels - 1)
        for key in ("container_start_seconds", "stream_start_seconds"):
            media[key] = finite(media.get(key, 0), f"{label} {key}")
        if not isinstance(media.get("identity"), dict):
            raise ReviewError(f"{label} has no source identity; analyze again")
        if identities:
            check_identity(media)
    anchors = session.get("anchors")
    if not isinstance(anchors, list) or not 2 <= len(anchors) <= 101:
        raise ReviewError("review must contain 2 to 101 anchors")
    seen = set()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ReviewError("each anchor must be an object")
        anchor_id = anchor.get("id")
        if not isinstance(anchor_id, str) or not ID_PATTERN.fullmatch(anchor_id):
            raise ReviewError("anchor id must start with a letter and use 1–40 letters/digits/_/-")
        if anchor_id in seen:
            raise ReviewError(f"duplicate anchor id: {anchor_id}")
        seen.add(anchor_id)
        if not isinstance(anchor.get("accepted"), bool):
            raise ReviewError(f"{anchor_id}: accepted must be true or false")
        origin = anchor.get("origin", "manual")
        if not isinstance(origin, str) or origin not in {"manual", "automatic"}:
            raise ReviewError(f"{anchor_id}: origin must be manual or automatic")
        if not isinstance(anchor.get("note", ""), str):
            raise ReviewError(f"{anchor_id}: note must be text")
        measurement = anchor.get("measurement", {})
        if not isinstance(measurement, dict):
            raise ReviewError(f"{anchor_id}: measurement must be an object")
        if "confidence" in measurement:
            finite(measurement["confidence"], f"{anchor_id} confidence", minimum=0, maximum=1)
        warnings = measurement.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(warning, str) for warning in warnings):
            raise ReviewError(f"{anchor_id}: measurement warnings must be a list of text")
        ref_time = finite(anchor.get("reference_seconds"), f"{anchor_id} reference time", minimum=0)
        if ref_time >= session["reference"]["duration_seconds"]:
            raise ReviewError(f"{anchor_id}: reference time is outside the recording")
        target_time = anchor.get("target_seconds")
        if target_time is not None:
            target_time = finite(target_time, f"{anchor_id} target time", minimum=0)
            if target_time >= session["target"]["duration_seconds"]:
                raise ReviewError(f"{anchor_id}: target time is outside the recording")
        if anchor["accepted"] and target_time is None:
            raise ReviewError(f"{anchor_id}: enter a manual target time before accepting a failed match")
    analysis = session.get("analysis", {})
    if not isinstance(analysis, dict):
        raise ReviewError("analysis settings must be an object")
    finite(analysis.get("window_seconds", 4), "analysis window", minimum=0.25, maximum=30)
    return session


def load_session(path: Path, *, identities: bool = True) -> dict:
    session = validate_session(read_json(path), base=path.resolve().parent, identities=identities)
    if identities:
        for label in ("reference", "target"):
            media = session[label]
            current = probe(Path(media["path"]), stream=media["audio_stream"],
                            channel=media["analysis_channel"])
            for key in ("sample_rate", "channels", "duration_seconds",
                        "stream_start_seconds", "container_start_seconds"):
                if current[key] != media[key]:
                    raise ReviewError(f"{label} metadata changed ({key}); analyze again rather than editing media metadata")
            # Derived I/O policy also upgrades older v1 review documents.
            media["time_base"] = current["time_base"]
            media["sample_count_seek"] = current["sample_count_seek"]
    return session


def edit_anchor(session: dict, anchor_id: str, *, reference_seconds=None,
                target_seconds=None, accepted: bool | None = None, note: str | None = None) -> dict:
    candidate = copy.deepcopy(session)
    validate_session(candidate)
    matches = [a for a in candidate["anchors"] if a["id"] == anchor_id]
    if not matches:
        raise ReviewError(f"unknown anchor id: {anchor_id}")
    anchor = matches[0]
    if reference_seconds is not None or target_seconds is not None:
        if reference_seconds is not None:
            anchor["reference_seconds"] = reference_seconds
        if target_seconds is not None:
            anchor["target_seconds"] = target_seconds
        anchor["origin"] = "manual"
        # Editing an approved time invalidates its previous approval unless the
        # same invocation deliberately accepts the replacement pair.
        anchor["accepted"] = False
    if accepted is not None:
        anchor["accepted"] = accepted
    if note is not None:
        anchor["note"] = note
    validate_session(candidate)
    original = next(a for a in session["anchors"] if a["id"] == anchor_id)
    original.clear()
    original.update(anchor)
    for label in ("reference", "target"):
        session[label].update(candidate[label])
    return session


def fit(session: dict, *, max_residual_ms: float = 10,
        max_drift_ppm: float = 20000) -> dict:
    max_residual_ms = finite(max_residual_ms, "residual limit", minimum=0)
    max_drift_ppm = finite(max_drift_ppm, "drift limit", minimum=0, maximum=500000)
    validate_session(session)
    accepted = sorted((a for a in session["anchors"] if a["accepted"]),
                      key=lambda a: a["reference_seconds"])
    if len(accepted) < 2:
        raise ReviewError("at least two accepted anchors are required; review and accept time pairs first")
    ref = np.array([a["reference_seconds"] for a in accepted], dtype=np.float64)
    tgt = np.array([a["target_seconds"] for a in accepted], dtype=np.float64)
    if np.any(np.diff(ref) <= 1e-6):
        raise ReviewError("accepted reference times must be distinct (more than 1 microsecond apart)")
    if np.any(np.diff(tgt) <= 0):
        raise ReviewError("accepted target times must increase with reference time")
    # Equal weights: approval is explicit; low-confidence automatic evidence
    # must not silently override a manually corrected pair.
    slope, offset, r2, residuals = weighted_linear_fit(ref, tgt - ref)
    scale = 1 + slope
    if not np.isfinite(scale) or scale <= 0 or not np.isfinite(offset):
        raise ReviewError("accepted anchors do not define a finite forward time map")
    ppm = slope * 1_000_000
    residual_ms = residuals * 1000
    max_error = float(np.max(np.abs(residual_ms)))
    span = float(ref[-1] - ref[0])
    warnings = []
    blockers = []
    if len(accepted) == 2:
        warnings.append("two points always fit exactly; there is no independent residual evidence")
    if span < session["reference"]["duration_seconds"] * 0.5:
        warnings.append("accepted anchors span less than half the reference; extrapolated drift is weakly constrained")
    if max_error > max_residual_ms + 1e-9:
        blockers.append("accepted anchors exceed the linear residual limit; check mismatches, dropouts or nonlinear drift")
    if abs(ppm) > max_drift_ppm + 1e-6:
        blockers.append("fitted drift exceeds the configured clock-rate limit")
    if max_error > 1:
        warnings.append("fit residuals exceed 1 ms; inspect residual data before using the correction")
    rows = []
    for anchor in session["anchors"]:
        r = anchor["reference_seconds"]
        t = anchor["target_seconds"]
        predicted = scale * r + offset
        rows.append({"id": anchor["id"], "accepted": anchor["accepted"],
                     "origin": anchor.get("origin", "manual"), "reference_seconds": r,
                     "target_seconds": t, "measured_delay_seconds": None if t is None else t - r,
                     "fitted_delay_seconds": predicted - r,
                     "residual_ms": None if t is None else (t - predicted) * 1000})
    return {"schema": "chronosync-drift-fit/v1", "mapping": CONVENTION,
            "software": {"drift_review": __version__, "chronosync": CHRONOSYNC_VERSION},
            "scale": scale, "fixed_target_delay_seconds": offset, "drift_ppm": ppm,
            "drift_ms_per_hour": slope * 3_600_000,
            "delay_at_reference_end_seconds": offset + slope * session["reference"]["duration_seconds"],
            "fit_r_squared": r2, "accepted_anchor_count": len(accepted),
            "accepted_span_seconds": span, "accepted_start_seconds": float(ref[0]),
            "accepted_end_seconds": float(ref[-1]),
            "fit_residual_rms_ms": float(np.sqrt(np.mean(residual_ms ** 2))),
            "fit_residual_max_ms": max_error, "residual_kind": "anchor fit, not rendered-audio verification",
            "fit_residual_clock": "target",
            "renderable": not blockers, "blockers": blockers, "warnings": warnings,
            "limits": {"max_residual_ms": max_residual_ms, "max_drift_ppm": max_drift_ppm},
            "anchors": rows}


def fit_csv(result: dict) -> str:
    output = io.StringIO(newline="")
    fields = ["id", "accepted", "origin", "reference_seconds", "target_seconds",
              "measured_delay_seconds", "fitted_delay_seconds", "residual_ms"]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(result["anchors"])
    return output.getvalue()
