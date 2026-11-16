"""Offline deterministic conformance + indexed-vs-full-scan timing experiment.

Run serially from the repository root: python benchmarks/speakers/compare.py
No network, models, subprocesses or external datasets; files are written only with --output.
"""
import argparse
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mediawrench.speakers import merge, parse_rttm, write_atomic  # noqa: E402
from mediawrench.speakers.core import OverlapIndex  # noqa: E402


def full_scan(diarization, queries):
    results = []
    for start, end in queries:
        evidence = {}
        for speaker, spans in diarization.intervals.items():
            overlap = math.fsum(max(0.0, min(end, b) - max(start, a)) for a, b in spans)
            if overlap:
                evidence[speaker] = overlap
        results.append(evidence)
    return results


def indexed(index, queries):
    return [{e["speaker"]: e["overlap"] for e in index.evidence(start, end)[0]} for start, end in queries]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="also save the measured result atomically")
    args = parser.parse_args()
    # One known-label duplicate trap, one true tie, one gap and one ordinary word.
    rows = [(0, .6, "A"), (0, .6, "A"), (0, 1, "B"),
            (2, 1, "A"), (2, 1, "B"), (6, 1, "A")]
    raw = "\n".join(f"SPEAKER synthetic 1 {a} {d} <NA> <NA> {s} <NA> <NA>" for a, d, s in rows)
    queries = [(0, 1), (2, 3), (4, 5), (6, 7)]
    asr = {"words": [dict(word=f"word{i}", start=a, end=b) for i, (a, b) in enumerate(queries)]}
    merged = merge(asr, parse_rttm(raw))
    actual = [i["speaker"] for i in merged["items"]]
    expected = ["B", "AMBIGUOUS", "UNKNOWN", "A"]
    assert actual == expected, (actual, expected)
    naive = []
    for start, end in queries:
        scores = {s: sum(max(0, min(end, a + d) - max(start, a)) for a, d, who in rows if who == s) for s in ("A", "B")}
        # Deliberately flawed baseline: duplicate rows summed, ties choose sorted first.
        naive.append(max(sorted(scores), key=scores.get) if max(scores.values()) else "UNKNOWN")

    # 2,000 disjoint union intervals; 2,000 short word queries. Duplicates
    # in the RTTM are removed before both timing paths for a fair comparison.
    lines = []
    for i in range(2000):
        line = f"SPEAKER benchmark 1 {i * 2} 1 <NA> <NA> {'A' if i % 2 == 0 else 'B'} <NA> <NA>"
        lines.append(line)
        if i % 5 == 0:
            lines.append(line)
    diar = parse_rttm("\n".join(lines))
    queries = [(i * 2 + .25, i * 2 + .75) for i in range(2000)]
    index = OverlapIndex(diar)
    timings = {"full_scan": [], "indexed": []}
    for _ in range(3):
        start = time.perf_counter()
        reference = full_scan(diar, queries)
        timings["full_scan"].append(time.perf_counter() - start)
        start = time.perf_counter()
        candidate = indexed(index, queries)
        timings["indexed"].append(time.perf_counter() - start)
        assert candidate == reference
    medians = {k: statistics.median(v) for k, v in timings.items()}
    output = json.dumps({
        "python": platform.python_version(), "platform": platform.system(),
        "conformance": {"expected": expected, "bridge": actual, "flawed_row_sum_argmax": naive,
                        "bridge_correct": sum(a == b for a, b in zip(actual, expected)),
                        "baseline_correct": sum(a == b for a, b in zip(naive, expected)), "cases": 4},
        "benchmark": {"raw_rttm_rows": len(lines), "union_intervals": 2000, "queries": len(queries),
                      "repeats": 3, "equal_overlap_results": True, "seconds": timings,
                      "median_seconds": medians, "median_speedup": medians["full_scan"] / medians["indexed"]},
        "limits": "Synthetic known-policy conformance and lookup timing only; not human attribution accuracy. Index construction, normalization and export excluded from timing."
    }, indent=2) + "\n"
    if args.output:
        write_atomic(args.output, output)
    print(output, end="")


if __name__ == "__main__":
    main()
