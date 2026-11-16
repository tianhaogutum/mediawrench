"""Run the real CLI in a temporary directory and roundtrip its SRT offline."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

import pysubs2

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="caption-demo-") as folder:
        output = Path(folder) / "repaired.srt"
        completed = subprocess.run(
            [sys.executable, "-m", "mediawrench.rollup", str(ROOT / "examples/rollup/rolling.vtt"),
             "-o", str(output)], cwd=ROOT, check=True, capture_output=True, text=True, timeout=120)
        report = json.loads(completed.stdout)
        subtitles = pysubs2.load(str(output), encoding="utf-8", format_="srt")
        actual = " ".join(event.plaintext for event in subtitles).split()
        expected = "Hello there New line Hello there".split()
        assert actual == expected, (actual, expected)
        provenance = json.loads(Path(str(output) + ".json").read_text(encoding="utf-8"))
        assert len(provenance["tokens"]) == 8
        assert report["summary"]["removed_tokens"] == 2
        print(output.read_text(encoding="utf-8"), end="")
        print("PASS: actual CLI + pysubs2 roundtrip, 8 -> 6 tokens; later intentional repeat preserved.")


if __name__ == "__main__":
    main()
