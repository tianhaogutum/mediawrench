"""Reproducible synthetic comparison, no network or random inputs."""
import json
from pathlib import Path
import sys

# Also runnable from a clone without installing mediawrench.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mediawrench.reflow import Config, reflow
from mediawrench.reflow.io import atomic_write_many, json_text, load_json

ROOT = Path(__file__).resolve().parents[2]


def benchmark():
    pause = load_json(ROOT / "examples" / "reflow" / "pause.json")
    dense = {"segments": [{"start": 0, "end": 2, "text": "Extraordinary speed overwhelms these subtitles.",
        "words": [{"word": word, "start": i * 0.4, "end": (i + 1) * 0.4}
                  for i, word in enumerate(["Extraordinary", " speed", " overwhelms", " these", " subtitles."])]}]}
    results = {"fixture_provenance": "original synthetic text; no media", "baseline": "fixed N=3 words; same normalization and timing repair",
        "config": {"max_chars": 16, "target_duration": 1.5, "max_duration": 3.0, "max_cps": 20.0, "pause": 0.5}, "fixtures": []}
    config = Config(**results["config"])
    for name, fixture in [("pause-vs-midphrase", pause), ("dense-infeasible", dense)]:
        row = {"name": name, "input": fixture, "algorithms": {}}
        for algorithm in ("fixed", "dp"):
            report = reflow(fixture, config, algorithm=algorithm, group_words=3)
            row["algorithms"][algorithm] = {"objective": report["objective"], "metrics": report["metrics"],
                "boundaries": [c["word_ids"] for c in report["cues"]], "constraints": report["constraints"]}
        results["fixtures"].append(row)
    return results


if __name__ == "__main__":
    result = benchmark()
    if len(sys.argv) == 3 and sys.argv[1] == "--output":
        atomic_write_many([(sys.argv[2], json_text(result))])
    elif len(sys.argv) != 1:
        raise SystemExit("usage: python benchmarks/reflow/benchmark.py [--output PATH]")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
