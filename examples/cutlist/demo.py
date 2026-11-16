"""Real CLI SRT -> selection -> three formats, checked in a temporary directory."""

import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cutlist-demo-") as directory:
        prefix = Path(directory) / "interview-proj"
        argv = [sys.executable, "-m", "mediawrench.cutlist", str(ROOT / "examples/cutlist/interview.srt"),
                "--plan", str(ROOT / "examples/cutlist/selection.json"), "--media-file", "interview.wav",
                "--output-prefix", str(prefix)]
        run = subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8",
                             cwd=ROOT, timeout=120)
        report = json.loads(run.stdout)
        llc = json.loads(prefix.with_suffix(".llc").read_text(encoding="utf-8"))
        csv_text = prefix.with_suffix(".csv").read_text(encoding="utf-8")
        rows = list(csv.reader(io.StringIO(csv_text)))
        mapping = json.loads(Path(str(prefix) + ".provenance.json").read_text(encoding="utf-8"))
        expected = [(0, 3.5), (4.5, 6.1), (8.9, 10)]
        assert [(cut["start"], cut["end"]) for cut in llc["cutSegments"]] == expected
        assert [(float(row[0]), float(row[1])) for row in rows] == expected
        assert [(cut["start"], cut["end"]) for cut in mapping["cuts"]] == expected
        assert [cut["cueIds"] for cut in mapping["cuts"]] == [[1, 2], [4], [6]]
        assert llc["version"] == 2 and llc["mediaFileName"] == "interview.wav"
        assert rows[0][2] == "开场 / Answer, first"
        assert mapping["source"]["srtSha256"] == hashlib.sha256((ROOT / "examples/cutlist/interview.srt").read_bytes()).hexdigest()
        print(json.dumps({"inputCues": 6, "explicitKeptCues": 5, "effectiveKeptCues": 4,
                          "finalCuts": len(rows), "intervalsSeconds": expected,
                          "excludedOverlapMs": 0, "threeFormatsAgree": True,
                          "note": "Synthetic text only; media filename is a reference, no media was executed."},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
