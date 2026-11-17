"""Stream FFmpeg PCM through libsoxr; verify the actual rendered samples."""

from __future__ import annotations

from importlib.metadata import version
import math
from pathlib import Path
import shutil
import tempfile
import time

import numpy as np
import soundfile as sf
import soxr

from .media import pcm_stream, probe, window
from .review import fit, matched_window
from .storage import (ReviewError, check_identity, check_output, finite,
                      publish, write_json)


def verify_audio(session: dict, corrected: dict, model: dict, *, count: int = 5) -> dict:
    """Check separate windows against output audio, preserving failed probes."""
    rate = 8000
    seconds = min(2.0, float(session.get("analysis", {}).get("window_seconds", 4)))
    low = max(seconds / 2, model["accepted_start_seconds"],
              -model["fixed_target_delay_seconds"] / model["scale"] + seconds / 2)
    high = min(session["reference"]["duration_seconds"] - seconds / 2,
               model["accepted_end_seconds"],
               (session["target"]["duration_seconds"] - model["fixed_target_delay_seconds"])
               / model["scale"] - seconds / 2)
    if high <= low:
        return {"kind": "rendered audio GCC-PHAT", "status": "unmeasurable",
                "probes": [], "usable_probe_count": 0,
                "max_abs_residual_ms": None, "rms_residual_ms": None,
                "warnings": ["accepted overlap is too short for verification windows"]}
    rows = []
    for center in np.linspace(low, high, count):
        try:
            ref_audio = window(session["reference"], float(center - seconds / 2), seconds, rate=rate)
            tgt_audio = window(corrected, float(center - seconds / 2), seconds, rate=rate)
            match = matched_window(ref_audio, tgt_audio, rate, 0.25)
        except ReviewError as exc:
            match = {"success": False, "delay_seconds": None, "confidence": 0.0,
                     "similarity": None, "warnings": [f"verification window failed: {exc}"]}
        usable = (match["success"] and match["confidence"] >= 0.6
                  and abs(match["similarity"] or 0) >= 0.2)
        rows.append({"reference_seconds": float(center), "usable": bool(usable),
                     "residual_ms": None if match["delay_seconds"] is None else match["delay_seconds"] * 1000,
                     "measurement": match})
    residuals = [row["residual_ms"] for row in rows if row["usable"]]
    maximum = float(np.max(np.abs(residuals))) if residuals else None
    rms = float(np.sqrt(np.mean(np.square(residuals)))) if residuals else None
    warnings = []
    if len(residuals) < count:
        warnings.append("some verification windows were silent, ambiguous or weak; failed probes remain in the report")
    if maximum is not None and maximum > 5:
        warnings.append("rendered-audio residual exceeds 5 ms; review the anchors and recording continuity")
    status = "unmeasurable" if not residuals else "review" if warnings else "measured"
    return {"kind": "rendered audio GCC-PHAT; not independent ground truth", "status": status,
            "probes": rows, "usable_probe_count": len(residuals),
            "max_abs_residual_ms": maximum, "rms_residual_ms": rms, "warnings": warnings}


def render(session: dict, output: Path, *, report: Path | None = None,
           overwrite: bool = False, subtype: str = "FLOAT", timeout: float = 300,
           verify: bool = True, max_residual_ms: float = 10,
           max_drift_ppm: float = 20000, protected=()) -> dict:
    timeout = finite(timeout, "render timeout", minimum=0.05, maximum=86400)
    if subtype not in {"FLOAT", "PCM_24", "PCM_16"}:
        raise ReviewError("output subtype must be FLOAT, PCM_24, or PCM_16")
    model = fit(session, max_residual_ms=max_residual_ms, max_drift_ppm=max_drift_ppm)
    if not model["renderable"]:
        raise ReviewError("cannot render this linear fit: " + "; ".join(model["blockers"]))
    inputs = [session["reference"]["path"], session["target"]["path"], *protected]
    output = check_output(output, protected=inputs, overwrite=overwrite)
    if output.suffix.lower() != ".wav":
        raise ReviewError("corrected audio output must use the .wav extension")
    if report is not None:
        report = check_output(report, protected=[*inputs, output], overwrite=overwrite)
    output.parent.mkdir(parents=True, exist_ok=True)
    ref = session["reference"]
    tgt = session["target"]
    output_rate = ref["sample_rate"]
    channels = tgt["channels"]
    total_frames = round(ref["duration_seconds"] * output_rate)
    offset_frames = round(-model["fixed_target_delay_seconds"] / model["scale"] * output_rate)
    skip = max(0, -offset_frames)
    leading = min(total_frames, max(0, offset_frames))
    written = 0
    source_frames = 0
    peak = 0.0
    start = time.monotonic()
    expected_bytes = total_frames * channels * {"FLOAT": 4, "PCM_24": 3, "PCM_16": 2}[subtype] + 4096
    if shutil.disk_usage(output.parent).free < expected_bytes + 1024 * 1024:
        raise ReviewError(f"insufficient free disk for an atomic render (requires about {expected_bytes / 1048576:.1f} MiB)")
    container = "RF64" if expected_bytes > 4_000_000_000 else "WAV"
    # Preserve resampling phase from sample zero, while excluding an unrelated
    # long tail. Two seconds of right context precede the VHQ filter flush.
    needed_seconds = model["scale"] * ref["duration_seconds"] + model["fixed_target_delay_seconds"] + 2.0
    decode_limit = (math.ceil(needed_seconds * tgt["sample_rate"])
                    if needed_seconds < tgt["duration_seconds"] else None)
    # Bound expansion as well as input: low-rate multichannel input can
    # otherwise yield gigabytes in one call when the reference rate is high.
    nominal_output_frames = max(1, 262144 // channels)  # about 1 MiB of float32
    input_block_frames = max(1, min(16384, math.floor(
        nominal_output_frames * tgt["sample_rate"] * model["scale"] / output_rate)))
    with tempfile.TemporaryDirectory(prefix=".drift-review-", dir=output.parent) as directory:
        temp = Path(directory) / "corrected.wav"
        resampler = soxr.ResampleStream(tgt["sample_rate"] * model["scale"], output_rate,
                                        channels, dtype="float32", quality="VHQ")
        with sf.SoundFile(temp, mode="w", samplerate=output_rate, channels=channels,
                          subtype=subtype, format=container) as sink:
            def emit(block):
                nonlocal written, skip, peak
                block = np.asarray(block, dtype=np.float32).reshape(-1, channels)
                if skip:
                    count = min(skip, len(block))
                    skip -= count
                    block = block[count:]
                block = block[:max(0, total_frames - written)]
                if block.size:
                    if not np.all(np.isfinite(block)):
                        raise ReviewError("resampling produced non-finite samples; reduce the source's extreme amplitude")
                    local_peak = float(np.max(np.abs(block)))
                    if subtype != "FLOAT" and local_peak > 1.0:
                        raise ReviewError("resampled peaks exceed integer PCM range; use --subtype FLOAT to preserve them")
                    peak = max(peak, local_peak)
                    sink.write(block)
                    written += len(block)
            zero_block = np.zeros((16384, channels), dtype=np.float32)
            while written < leading:
                count = min(16384, leading - written)
                sink.write(zero_block[:count])
                written += count
            with pcm_stream(tgt, timeout=timeout, temp_dir=Path(directory), max_frames=decode_limit,
                            block_frames=input_block_frames) as blocks:
                for block in blocks:
                    source_frames += len(block)
                    emit(resampler.resample_chunk(block, last=False))
                emit(resampler.resample_chunk(np.empty((0, channels), dtype=np.float32), last=True))
            tail = total_frames - written
            while written < total_frames:
                count = min(16384, total_frames - written)
                sink.write(zero_block[:count])
                written += count
        check_identity(ref)
        check_identity(tgt)
        corrected = probe(temp, channel=tgt["analysis_channel"])
        if sf.info(temp).frames != total_frames:
            raise ReviewError("rendered frame count does not match the reference timeline")
        validation = verify_audio(session, corrected, model) if verify else {"status": "skipped"}
        result = {"schema": "chronosync-drift-render/v1", "fit": model,
                  "reference": ref, "target": tgt,
                  "anchor_review": session["anchors"],
                  "analysis_settings": session.get("analysis", {}),
                  "output": {"path": str(output), "sample_rate": output_rate,
                             "channels": channels, "frames": total_frames,
                             "duration_seconds": total_frames / output_rate,
                             "subtype": subtype, "container": container,
                             "leading_silence_frames": leading, "trailing_silence_frames": tail,
                             "decoded_source_frames": source_frames, "peak_absolute": peak},
                  "render": {"method": "FFmpeg PCM -> SoXR VHQ ResampleStream -> libsndfile",
                             "soxr_version": version("soxr"),
                             "effective_input_rate": tgt["sample_rate"] * model["scale"],
                             "source_decode_limit_frames": decode_limit,
                             "input_block_frames": input_block_frames,
                             "pitch_policy": "sample-clock correction changes pitch with the clock rate",
                             "offset_rounding_max_seconds": 0.5 / output_rate,
                             "elapsed_seconds": time.monotonic() - start},
                  "verification": validation}
        publish(temp, output, overwrite=overwrite)
    if report is not None:
        write_json(report, result, overwrite=overwrite)
    return result
