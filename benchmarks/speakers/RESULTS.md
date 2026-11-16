# `mediawrench speakers`: synthetic conformance and lookup benchmark

All inputs are generated locally; there is no media, model, API or human-attribution
reference dataset. The saved [results.json](results.json) was measured with CPython
3.14 on Linux and records the three raw timing samples. Times vary by machine and
load; the correctness assertions must hold on every run.

Reproduce serially from the repository root:

```bash
python benchmarks/speakers/compare.py --output benchmarks/speakers/results.json
python examples/speakers/demo.py
```

## Conformance

The four known-policy cases are a duplicated partial interval versus a fully
covering second speaker, an exact tie, a gap and an ordinary single-speaker word.
`mediawrench speakers` returns `[B, AMBIGUOUS, UNKNOWN, A]`, matching all **4/4**
expected labels. A deliberately flawed baseline that sums raw rows and resolves ties
by first ID returns `[A, A, UNKNOWN, A]` (**2/4**). This exposes specific arithmetic
and policy defects; it is not a comparison with WhisperX or whisper-diarization and
does not measure human speaker accuracy.

## Lookup timing

2,400 RTTM rows union into 2,000 intervals across two speakers, queried with 2,000
short words. A full scan and the bisection index return **identical per-speaker
overlap durations for every query in all 3 runs**. Median full-scan time was
**0.252 s**, median indexed time **0.0025 s** (about **100x** on this sparse
synthetic input). Both paths start from the same already-unioned intervals; index
construction, normalization, grouping, source retention and export are excluded.
Dense overlap or many speakers can reduce the advantage; this is not an end-to-end
throughput guarantee.

## Demo

`examples/speakers/demo.py` conserves **7 source words**, keeps **2 text-only spans**,
marks **3 items** with segment precision, and produces **9 turns / 9 reparsed SRT
cues**. The CLI tests additionally read the synthetic Whisper, serialized
faster-whisper and WhisperX fixtures and export JSON, Markdown and SRT through real
files. No models are run, so compatibility is with the documented JSON fields, not
a validation of every upstream version or exporter.
