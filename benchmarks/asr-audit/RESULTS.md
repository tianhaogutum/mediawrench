# Reproducible synthetic evidence-triage experiment

Standard library only. Run from the repository root:

```sh
python benchmarks/asr-audit/run.py --seed 833
```

The JSON on stdout matches `results.json` (no timestamps or absolute paths).
To save another run, use `--output NEW_PATH`; existing files are refused.

## Dataset and labels

Generator: `benchmarks/asr-audit/run.py:dataset`, dataset `synthetic-interview-evidence-v1`,
seed **833** (Python `random.Random`, shuffle of cases). Canonical dataset SHA-256:
`0e8940a6d12f61fef62d75e02e551c19c53119972f208cc260fae67b8f69f485`.
The hash covers generated transcript, supplied speech intervals and labels,
encoded with sorted JSON keys, compact separators and UTF-8. The evaluator uses
the default thresholds recorded in `results.json`; no threshold tuning is done.

84 segments: **32 synthetic positives**, **52 negatives**, across 54 cases.
Each repeated case has two identical adjacent two-second segments; cases are
separated by three seconds, with a unique phrase for each case. Ordinary cases
have one segment. No model scores are provided. Category counts, in segments:

- 24 labeled suspicious repeats outside supplied speech intervals.
- 8 labeled suspicious repeats inside supplied speech (deliberate false negatives
  for a rule requiring outside-speech corroboration).
- 24 intentional repeated interview answers inside speech.
- 4 intentional repeated answers whose speech coverage is deliberately missing
  from VAD (deliberate false positives for corroboration).
- 24 ordinary, distinct answers inside speech.

Labels are authored scenario assumptions, **not** real hallucination annotations.
No audio, copyrighted recordings, models, human study or real ASR run was used.

## Measured results (per segment)

| Rule | TP | FP | FN | TN | Precision | Recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline: repetition only | 32 | 28 | 0 | 24 | 0.533333 | 1.000000 |
| Repeat AND outside supplied speech | 24 | 4 | 8 | 48 | 0.857143 | 0.750000 |
| All audit review flags | 32 | 28 | 0 | 24 | 0.533333 | 1.000000 |

Baseline selects `adjacent_repeat` from the same repetition algorithm without
VAD. The corroborated rule selects only `speech_corroborated_repeats`, the
conjunction of `adjacent_repeat` and `outside_speech` (outside fraction >= 0.8).
Precision = TP/(TP+FP); recall = TP/(TP+FN). The saved report includes exact
flagged/false-positive/false-negative segment indices, category counts and all
thresholds; dataset generation recovers the labels for each index.

The corroborated subset has four false positives, all from deliberately omitted
VAD coverage of genuine repeated speech. Its eight false negatives are suspicious
repeats inside speech. All-review flags deliberately retain intentional repeats;
the audit does not silently filter them away or claim that adding VAD improves
the precision of its complete flag set. The score/rate/timestamp rules have
separate unit and CLI tests; this experiment isolates repetition with VAD.

This is a designed test of a conservative review subset and its tradeoff, not a
real-world efficacy estimate. Results depend on arbitrary scenario prevalence,
exact matching and supplied VAD. No automatic truth classification or deletion.
