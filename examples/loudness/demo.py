#!/usr/bin/env python3
"""Real, offline synthetic experiment; leaves reports/audio under examples/loudness/generated/."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from examples.loudness.synthetic import write_tone
from mediawrench.loudness import Profile, Toolchain, normalize, verify_candidate


def toolchain():
    # FFMPEG/FFPROBE override the executables found on PATH.
    return Toolchain(os.environ.get("FFMPEG", "ffmpeg"), os.environ.get("FFPROBE", "ffprobe"))


def main():
    tools = toolchain()
    if not shutil.which(tools.ffmpeg) or not shutil.which(tools.ffprobe):
        raise SystemExit("FFmpeg/ffprobe missing: install FFmpeg (ffmpeg and ffprobe on PATH) or set FFMPEG/FFPROBE.")
    base = ROOT / "examples/loudness/generated"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    profile = Profile(-16, -1.5, 0.5)
    rows = []
    for name, kind, seconds, active_profile in (
        ("modulated", "modulated", 12, profile),
        ("clipping", "clipped", 12, profile),
        ("dynamic", "dynamic", 24, Profile(-16, -1.5, 0.5, 1)),
        ("dynamic_strict", "dynamic", 24, Profile(-16, -1.5, 0.1, 1)),
        ("tight_tolerance", "modulated", 12, Profile(-16.123, -1.5, 0.001)),
    ):
        source = directory / f"{name}-input.wav"
        output = directory / f"{name}-output.wav"
        write_tone(source, seconds, kind=kind)
        result = normalize(source, output, active_profile, directory / f"{name}.json", tools)
        if result["status"] not in {"PASS", "FAIL"}:
            raise RuntimeError(json.dumps(result, indent=2))
        rows.append({"case": name, "profile": result["profile"], "status": result["status"],
                     "input_i": result["input_measurement"]["input_i"],
                     "input_tp": result["input_measurement"]["input_tp"],
                     "mode": result["normalization_type"],
                     "filter_reported_output_i": result["processing_measurement"]["output_i"],
                     "final_i": result["output_measurement"]["input_i"],
                     "final_tp": result["output_measurement"]["input_tp"],
                     "checks": result["checks"], "published": output.exists()})
    # Deliberately nonconforming real PCM encode: trusting the old target/report is unsafe.
    verified = directory / "modulated-output.wav"
    bad = directory / "deliberate-plus6db.wav"
    subprocess.run([tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
                    "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file",
                    "-i", str(verified), "-af", "volume=6dB", "-c:a", "pcm_s24le",
                    "-threads", "1", "-n", str(bad)], check=True, timeout=120)
    rejected = verify_candidate(bad, profile, tools)
    if rejected["status"] != "FAIL":
        raise AssertionError("Deliberate +6dB candidate unexpectedly passed")
    rejected_report = directory / "deliberate-candidate-verification.json"
    rejected_report.write_text(json.dumps(rejected, indent=2) + "\n")
    summary = {
        "experiment": "Trust requested two-pass targets versus independently measure the encoded artifact",
        "ffmpeg_version": result["ffmpeg_version"],
        "baseline": "A target-trusting workflow accepts every successfully encoded candidate; no final-file gate.",
        "cases": rows,
        "deliberate_candidate": {"change": "+6 dB after normalization, encoded as 24-bit PCM",
                                 "baseline_target_claim": -16,
                                 "status": rejected["status"], "checks": rejected["checks"]},
        "limitations": ["Synthetic clips only; not a speech corpus or broadcast certification",
                        "FFmpeg loudnorm JSON rounds measurements to 0.01; very tight tolerances can reject",
                        "Dynamic fallback can miss targets; it is surfaced and the final gate still applies",
                        "True peak is a maximum ceiling, not an equality target"],
    }
    (directory / "experiment.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Artifacts retained: {directory}")
    assert rows[0]["status"] == "PASS"
    assert rows[2]["mode"] == "dynamic"
    assert rows[3]["status"] == "FAIL" and not rows[3]["published"]
    assert rows[4]["status"] == "FAIL" and not rows[4]["published"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
