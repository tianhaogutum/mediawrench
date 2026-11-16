#!/usr/bin/env python3
"""Generate tiny media and exercise the library; no network or persistent output."""
import json
from pathlib import Path
import sys
import tempfile

# Also works from a clone without installing (repo root for examples/, src/ for mediawrench).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from examples.postflight.synthetic import generate
from mediawrench.postflight import Options, compare


def main():
    with tempfile.TemporaryDirectory(prefix="postflight-demo-") as directory:
        files = generate(Path(directory))
        outcomes = {}
        for case in ("healthy", "audio_only", "truncated", "shortened"):
            report = compare(files["source"], files[case])
            outcomes[case] = report["verdict"]
            if report["verdict"] != ("PASS" if case == "healthy" else "FAIL"):
                print(json.dumps(report, indent=2))
                return 1
        report = compare(files["source"], files["audio_only"], Options(allow_drop=("video",)))
        outcomes["explicit_video_removal"] = report["verdict"]
        if report["verdict"] != "PASS":
            print(json.dumps(report, indent=2))
            return 1
        print(json.dumps({"outcomes": outcomes, "tools": report["tools"], "coverage": report["coverage"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
