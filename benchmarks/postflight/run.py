#!/usr/bin/env python3
"""Reproduce the header-only vs decoded-presence experiment on tiny local media."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from examples.postflight.synthetic import generate
from mediawrench.postflight import compare, write_report
from mediawrench.postflight.core import FORMATS, resolve_tool


def baseline(path):
    # generate() checks every FFmpeg encoder exit. Truncation is a subsequent
    # injected fault; the other bad outputs are produced by successful encoders.
    result = subprocess.run([resolve_tool("ffprobe"), "-v", "error", "-threads", "1",
                             "-protocol_whitelist", "file,pipe", "-format_whitelist", FORMATS,
                             "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    probe = json.loads(result.stdout) if result.returncode == 0 else {}
    types = [s["codec_type"] for s in probe.get("streams", [])]
    return {"encoder_exit": 0, "nonempty": path.stat().st_size > 0,
            "probe_exit": result.returncode, "stream_types": types,
            "counts_preserved": types.count("audio") == 1 and types.count("video") == 1,
            "accepted": result.returncode == 0 and path.stat().st_size > 0 and
                        any(kind in ("video", "audio") for kind in types)}


def confusion(rows, key):
    # Positive = rejects a seeded faulty output. UNKNOWN is a conservative
    # rejection, separately recorded per row, not counted as proof of a fault.
    table = {"true_positive": 0, "false_negative": 0, "false_positive": 0, "true_negative": 0}
    for row in rows:
        if key == "baseline":
            rejected = not row["baseline"]["accepted"]
        elif key == "header_counts":
            rejected = not (row["baseline"]["accepted"] and row["baseline"]["counts_preserved"])
        else:
            rejected = row["postflight"]["verdict"] != "PASS"
        label = ("true_positive" if rejected else "false_negative") if row["faulty"] else ("false_positive" if rejected else "true_negative")
        table[label] += 1
    return table


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "benchmarks/postflight/results.json"
    with tempfile.TemporaryDirectory(prefix="postflight-experiment-") as directory:
        files = generate(Path(directory))
        rows = []
        for name in ("healthy", "audio_only", "truncated", "shortened", "zero_video", "short_audio"):
            report = compare(files["source"], files[name])
            rows.append({"case": name, "faulty": name != "healthy", "bytes": files[name].stat().st_size,
                         "fixture_sha256": hashlib.sha256(files[name].read_bytes()).hexdigest(),
                         "baseline": baseline(files[name]),
                         "postflight": {"verdict": report["verdict"], "coverage": report["coverage"],
                                        "checks": report["checks"]}})
        result = {"python": platform.python_version(), "platform": f"{platform.system()}-{platform.machine()}",
                  "tool_versions": {name: tool["version"] for name, tool in report["tools"].items()},
                  "reproduce": ["python", "benchmarks/postflight/run.py"],
                  "method": "2s 96x64 12fps blue video + 16kHz 440Hz tone. All encoders exit 0; truncate healthy bytes afterward for truncation. Baseline accepts nonempty outputs with any AV header. Positive means rejection of a faulty output.",
                  "policy": {"absolute_tolerance": 0.25, "relative_tolerance": 0.02, "full_decode": True},
                  "rows": rows,
                  "confusion": {key: confusion(rows, key) for key in ("baseline", "header_counts", "postflight")},
                  "limitations": ["Six synthetic cases, not population accuracy or a perceptual-quality benchmark.",
                                  "Container IDs and hashes may vary between runs; verdicts and confusion table are the reproducibility target."]}
        write_report(result, target, files["source"], files["healthy"])
        print(json.dumps(result["confusion"], indent=2))
        return 0 if all(row["postflight"]["verdict"] == ("FAIL" if row["faulty"] else "PASS") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
