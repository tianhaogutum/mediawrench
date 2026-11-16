"""Real offline CLI/file roundtrip; all generated outputs use a temporary directory."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
# Allow `python examples/audacity/demo.py` from a clone without installing.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from mediawrench.audacity.formats import parse_srt


def cli(*args, expected=0):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    process = subprocess.run([sys.executable, "-m", "mediawrench.audacity", *map(str, args)],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
    if process.returncode != expected:
        raise RuntimeError(f"CLI returned {process.returncode}, expected {expected}: {process.stderr}")
    return json.loads(process.stdout if expected == 0 else process.stderr)


def main():
    original, _ = parse_srt((ROOT / "examples/audacity/synthetic.srt").read_text(encoding="utf-8"))
    (ROOT / ".tools").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="asb-demo-", dir=ROOT / ".tools") as directory:
        work = Path(directory)
        labels, sidecar, output = work / "labels.txt", work / "sidecar.json", work / "returned.srt"
        cli("export", ROOT / "examples/audacity/synthetic.srt", "--labels", labels, "--sidecar", sidecar)
        lines = labels.read_text(encoding="utf-8").splitlines()
        # Cross the two identical-text cues and sort as Audacity exports by time.
        lines[0] = lines[0].replace("1.000\t2.500", "4.250\t4.750")
        labels.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n", encoding="utf-8")
        report = cli("import", labels, "--sidecar", sidecar, "--source", ROOT / "examples/audacity/synthetic.srt",
                     "--output", output)
        returned, _ = parse_srt(output.read_text(encoding="utf-8"))
        expected_order = [original[1], original[0], original[2]]
        exact = sum(a.text == b.text for a, b in zip(returned, expected_order))
        if exact != 3 or returned[1].start_ms != 4250 or returned[1].end_ms != 4750:
            raise RuntimeError("SRT timing/text roundtrip failed")
        if [c["id"] for c in report["output_cues"]] != ["9", "7", "12"]:
            raise RuntimeError("Identical-text cue identity was not preserved")
        # Plain flattened-label baseline deliberately discards identity/multiline.
        baseline = sum(c.text == c.text.replace("\n", " / ") for c in original)
        labels.write_text(labels.read_text(encoding="utf-8").replace("[cue:7] Hello / world",
                                                                    "[cue:7] edited"), encoding="utf-8")
        conflict = cli("import", labels, "--sidecar", sidecar,
                       "--output", work / "blocked.srt", expected=2)
        if conflict["report"]["conflicts"][0]["kind"] != "changed_text" or (work / "blocked.srt").exists():
            raise RuntimeError("Changed text was not blocked without a decision")
        vtt_labels, vtt_sidecar = work / "vtt.txt", work / "vtt.json"
        cli("export", ROOT / "examples/audacity/synthetic.vtt", "--labels", vtt_labels, "--sidecar", vtt_sidecar)
        vtt_report = cli("import", vtt_labels, "--sidecar", vtt_sidecar, "--output", work / "vtt.srt")
        if len(parse_srt((work / "vtt.srt").read_text(encoding="utf-8"))[0]) != 2:
            raise RuntimeError("WebVTT-to-labels-to-SRT roundtrip failed")
        print(json.dumps({"srt_cues": 3, "exact_payloads": exact, "identity_order": ["9", "7", "12"],
                          "moved_cue_7_ms": [4250, 4750], "flattened_baseline_exact_payloads": baseline,
                          "text_conflicts_blocked": len(conflict["report"]["conflicts"]),
                          "vtt_cues": vtt_report["output_count"], "temporary_outputs_cleaned": True},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
