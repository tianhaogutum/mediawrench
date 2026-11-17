"""Editor-facing inspection and local listening clips for the review ledger."""

from __future__ import annotations

from pathlib import Path
import tempfile

import numpy as np
import soundfile as sf

from .media import window
from .review import validate_session
from .storage import ReviewError, check_identity, finite, integer, json_text


def inspect_text(session: dict) -> str:
    validate_session(session)
    lines = [f"Reference: {session['reference']['path']}",
             f"Target:    {session['target']['path']}",
             "Times are seconds from each selected audio stream's first decoded sample.",
             "", f"{'anchor':<16} {'state':<8} {'reference':>12} {'target':>12} {'delay ms':>12} {'auto conf':>11}"]
    for anchor in session["anchors"]:
        target = anchor["target_seconds"]
        reference = anchor["reference_seconds"]
        delay = (target - reference) * 1000 if target is not None else None
        measurement = anchor.get("measurement", {})
        confidence = measurement.get("confidence")
        state = "accepted" if anchor["accepted"] else "pending"
        target_text = f"{target:.6f}" if target is not None else "missing"
        delay_text = f"{delay:+.3f}" if delay is not None else "missing"
        confidence_text = (f"{confidence:.3f}" if anchor.get("origin") != "manual"
                           and isinstance(confidence, (int, float)) else "manual")
        lines.append(f"{anchor['id']:<16} {state:<8} {reference:12.6f} {target_text:>12} {delay_text:>12} {confidence_text:>11}")
        if target is None:
            lines.append("  Enter a manual target time for this anchor before accepting it.")
        elif not anchor.get("suggested_usable", False) and anchor.get("origin") == "automatic":
            lines.append("  Weak automatic evidence; verify the matching event.")
        for warning in measurement.get("warnings", []):
            lines.append(f"  Original measurement: {warning}")
        if anchor.get("note"):
            lines.append(f"  Note: {anchor['note']}")
    lines.extend(["", "Only accepted pairs affect the linear fit. Listening clips can help review proposals."])
    return "\n".join(lines) + "\n"


def _centered_audio(media: dict, center: float, seconds: float, rate: int) -> np.ndarray:
    frames = round(seconds * rate)
    start = center - seconds / 2
    left = min(frames, max(0, round(-start * rate)))
    remaining = frames - left
    if remaining <= 0:
        return np.zeros(frames, dtype=np.float32)
    audio = window(media, max(0, start), remaining / rate, rate=rate)
    result = np.zeros(frames, dtype=np.float32)
    length = min(remaining, len(audio))
    result[left:left + length] = audio[:length]
    return result


def export_clips(session: dict, destination: Path, *, seconds: float = 2,
                 rate: int = 16000) -> dict:
    """Pair each proposed event as left=reference/right=target; never approve it."""
    validate_session(session)
    seconds = finite(seconds, "clip duration", minimum=0.5, maximum=10)
    rate = integer(rate, "clip sample rate", 1000, 48000)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ReviewError(f"clip directory already exists; choose a new directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix=".drift-review-clips-", dir=destination.parent) as directory:
        temp = Path(directory)
        for anchor in session["anchors"]:
            row = {"id": anchor["id"], "accepted": anchor["accepted"],
                   "reference_seconds": anchor["reference_seconds"],
                   "target_seconds": anchor["target_seconds"]}
            if anchor["target_seconds"] is None:
                row.update({"file": None, "status": "skipped: manual target time is missing"})
            else:
                left = _centered_audio(session["reference"], anchor["reference_seconds"], seconds, rate)
                right = _centered_audio(session["target"], anchor["target_seconds"], seconds, rate)
                filename = f"{anchor['id']}.wav"
                sf.write(temp / filename, np.column_stack([left, right]), rate, format="WAV", subtype="FLOAT")
                row.update({"file": filename, "status": "listening pair"})
            rows.append(row)
        for label in ("reference", "target"):
            check_identity(session[label])
        result = {"schema": "chronosync-drift-clips/v1", "sample_rate": rate,
                  "frames_per_clip": round(seconds * rate), "channels": ["reference", "target"],
                  "policy": "pairs centered at proposed events; no drift correction or automatic acceptance",
                  "clips": rows}
        (temp / "index.json").write_text(json_text(result), encoding="utf-8")
        if destination.exists():
            raise ReviewError("clip directory appeared during export; choose a new directory")
        temp.rename(destination)
    return result
