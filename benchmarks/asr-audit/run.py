"""Seeded, transcript-only synthetic triage evaluation; no real ASR claims."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from mediawrench.asraudit import audit
from mediawrench.asraudit.output import atomic_outputs

SEED = 833


def dataset(seed=SEED):
    rng = random.Random(seed)
    cases = (["suspicious_outside"] * 12 + ["suspicious_inside"] * 4
             + ["intentional_inside"] * 12 + ["intentional_missing_vad_coverage"] * 2
             + ["ordinary_inside"] * 24)
    rng.shuffle(cases)
    segments, speech, labels = [], [], []
    time = 0
    for case_id, category in enumerate(cases):
        repeat = category != "ordinary_inside"
        inside = category in ("suspicious_inside", "intentional_inside", "ordinary_inside")
        count = 2 if repeat else 1
        phrase = f"Case {case_id}: synthetic reply."
        for index in range(count):
            start, end = time + index * 2, time + index * 2 + 2
            segments.append({"start": start, "end": end, "text": phrase})
            labels.append({"segment": len(segments) - 1, "case": case_id, "category": category,
                           "suspicious": category.startswith("suspicious_")})
            if inside:
                speech.append([start, end])
        time += count * 2 + 3
    return {"transcript": {"time_unit": "seconds", "segments": segments},
            "speech_intervals": speech, "labels": labels}


def metrics(predictions, labels):
    positives = {label["segment"] for label in labels if label["suspicious"]}
    predictions = set(predictions)
    tp = len(predictions & positives)
    fp = len(predictions - positives)
    fn = len(positives - predictions)
    tn = len(labels) - tp - fp - fn
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn, "true_negatives": tn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "flagged_segments": sorted(predictions),
            "false_positive_segments": sorted(predictions - positives),
            "false_negative_segments": sorted(positives - predictions)}


def evaluate(seed=SEED):
    data = dataset(seed)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    baseline = audit(data["transcript"])
    joined = audit(data["transcript"], data["speech_intervals"])
    repeat_only = [flag["segment"] for flag in baseline["flags"] if "adjacent_repeat" in flag["reasons"]]
    return {"dataset": "synthetic-interview-evidence-v1", "seed": seed,
            "dataset_sha256": hashlib.sha256(canonical).hexdigest(),
            "segments": len(data["labels"]),
            "positive_segments": sum(label["suspicious"] for label in data["labels"]),
            "negative_segments": sum(not label["suspicious"] for label in data["labels"]),
            "category_segment_counts": dict(sorted(Counter(label["category"] for label in data["labels"]).items())),
            "thresholds": joined["thresholds"],
            "baseline_repetition_only": metrics(repeat_only, data["labels"]),
            "repeat_and_outside_speech": metrics(joined["speech_corroborated_repeats"], data["labels"]),
            "all_review_flags": metrics([flag["segment"] for flag in joined["flags"]], data["labels"]),
            "limitations": [
                "Hand-labeled synthetic transcript scenarios; no audio, models or real-world accuracy measurement.",
                "Corroborated subset requires both repeat and outside_speech; all review flags still retain intentional repeats.",
                "VAD omissions deliberately cause false positives; suspicious repeats inside speech deliberately cause false negatives.",
                "Dataset is designed to exercise these rules, not estimate population precision or recall."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, help="new JSON path; existing files are never overwritten")
    args = parser.parse_args()
    content = json.dumps(evaluate(args.seed), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output is None:
        print(content, end="")
    else:
        atomic_outputs({args.output: content}, protected=[])


if __name__ == "__main__":
    main()
