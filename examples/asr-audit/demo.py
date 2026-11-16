"""Real offline core + CLI demonstration, rerunnable without persisted sidecars."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mediawrench.asraudit import audit, parse_json


def main():
    transcript = ROOT / "examples" / "asr-audit" / "transcript.json"
    vad = ROOT / "examples" / "asr-audit" / "speech.json"
    before = hashlib.sha256(transcript.read_bytes()).hexdigest()
    report = audit(parse_json(transcript.read_bytes()), parse_json(vad.read_bytes()))
    assert report["speech_corroborated_repeats"] == [0, 1]
    assert report["segments"][2]["reasons"] == ["adjacent_repeat"]
    assert report["segments"][2]["no_speech_prob"] is None
    scratch = ROOT / ".tools"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="asr-evidence-demo-", dir=scratch) as directory:
        out = Path(directory)
        subprocess.run([sys.executable, "-m", "mediawrench.asraudit", str(transcript),
                        "--vad", str(vad), "--json", str(out / "report.json"),
                        "--csv", str(out / "flags.csv"), "--review-srt", str(out / "review.srt")],
                       cwd=ROOT, check=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(
                           filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))})
        cli_report = parse_json((out / "report.json").read_bytes())
        assert cli_report["flags"] == report["flags"]
        assert "outside_speech" in (out / "flags.csv").read_text()
        assert "00:00:01,000 --> 00:00:03,000" in (out / "review.srt").read_text()
        assert cli_report["source"]["sha256"] == before
    assert hashlib.sha256(transcript.read_bytes()).hexdigest() == before
    print(f"Demo passed: {len(report['flags'])} flags, {len(report['review_ranges'])} ranges; "
          f"corroborated repeats {report['speech_corroborated_repeats']}; original SHA-256 {before}")


if __name__ == "__main__":
    main()
