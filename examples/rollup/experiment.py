"""Offline labeled baseline vs real CLI output; all captions are synthetic.

Print deterministic JSON to stdout. No benchmark timing or speedup claim.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

import pysubs2

ROOT = Path(__file__).resolve().parents[2]


def stamp(ms):
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, milli = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milli:03}"


def main():
    cases = json.loads((ROOT / "examples/rollup/cases.json").read_text(encoding="utf-8"))
    rows = []
    with tempfile.TemporaryDirectory(prefix="caption-experiment-") as folder:
        temp = Path(folder)
        for number, case in enumerate(cases):
            source = temp / f"{number}.vtt"
            target = temp / f"{number}.srt"
            source.write_text("WEBVTT\n\n" + "\n\n".join(
                f"{stamp(start)} --> {stamp(end)}\n{text}" for start, end, text in case["cues"]) + "\n",
                encoding="utf-8")
            baseline = pysubs2.SSAFile()
            for start, end, text in case["cues"]:
                event = pysubs2.SSAEvent(start=start, end=end)
                event.plaintext = text
                baseline.append(event)
            baseline_file = temp / f"{number}.baseline.srt"
            baseline.save(str(baseline_file), format_="srt", encoding="utf-8")
            baseline_loaded = pysubs2.load(str(baseline_file), encoding="utf-8")
            before = " ".join(s.plaintext for s in baseline_loaded).split()
            if not all(0 <= s.start < s.end for s in baseline_loaded):
                raise AssertionError("Invalid baseline timing")
            completed = subprocess.run([sys.executable, "-m", "mediawrench.rollup", str(source),
                                        "-o", str(target)], cwd=ROOT, check=True, capture_output=True,
                                       text=True, timeout=120)
            report = json.loads(completed.stdout)
            repaired = pysubs2.load(str(target), encoding="utf-8", format_="srt")
            after = " ".join(s.plaintext for s in repaired).split()
            expected = case["expected"].split()
            if after != expected:
                raise AssertionError(f"{case['name']}: {after!r} != {expected!r}")
            if len(before) - len(expected) != case["duplicates"]:
                raise AssertionError("Incorrect hand-labeled duplicate count")
            if not all(0 <= s.start < s.end for s in repaired):
                raise AssertionError("Invalid output timing")
            if report["summary"]["removed_tokens"] != case["duplicates"]:
                raise AssertionError("Unexpected deletion")
            rows.append({"case": case["name"], "kind": case["kind"], "baseline_tokens": len(before),
                         "repaired_tokens": len(after), "expected_tokens": len(expected),
                         "labeled_duplicate_tokens_before": case["duplicates"],
                         "labeled_duplicate_tokens_after": 0, "lost_intentional_tokens": 0,
                         "timing_valid": True, "ambiguous_pairs": report["summary"]["ambiguous_pairs"]})
    print(json.dumps({"cases": rows, "totals": {
        "cases": len(rows), "rolling_cases": sum(r["kind"] == "rolling" for r in rows),
        "counterexamples": sum(r["kind"] == "counterexample" for r in rows),
        "baseline_tokens": sum(r["baseline_tokens"] for r in rows),
        "repaired_tokens": sum(r["repaired_tokens"] for r in rows),
        "labeled_duplicate_tokens_before": sum(r["labeled_duplicate_tokens_before"] for r in rows),
        "labeled_duplicate_tokens_after": 0, "lost_intentional_tokens": 0,
        "all_sequences_match": True, "all_timings_valid": True}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
