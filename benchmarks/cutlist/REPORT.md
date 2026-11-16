# `mediawrench cutlist`: synthetic format and execution benchmark

Run from the repository root:

```bash
python benchmarks/cutlist/experiment.py --size 5000 --repeats 3
```

Recorded with Python 3.14, pysubs2 1.8.0, Linux x86_64.
The unedited machine-readable output is [results.json](results.json). Only
synthetic subtitle text is used. Temporary inputs/outputs are removed on exit.
No media programs, models, network requests or parallel workers are used.

## Selection result

The six-cue example has overlapping answers, an excluded aside, repeated literal
text and Unicode/CSV-special labels. Plain import of the four effective selected
cue intervals leaves 1,000 ms overlapping the explicitly excluded aside. The
normalized result has three cuts and zero excluded milliseconds, with padding
clamped to declared media boundaries. This is a hand-defined workflow comparison,
not a claim about a LosslessCut defect or user editing accuracy.

## Execution optimization

Source-to-cut provenance originally required scanning every cue for each final
cut. The implementation now sorts source intervals and sweeps the ordered cuts
with an end-time heap and an active-ID set. Exclusion subtraction also sweeps
sorted unions. Lookup cost follows sorting and actual emitted intersections,
rather than the full cut-count × cue-count scan. Dense overlaps can still produce
quadratic-size provenance when the actual output relationship is that large.

The experiment serializes a real 5,000-cue SRT through pysubs2, keeps all IDs,
drops every fifth cue and pads by 0.1 seconds. It parses the SRT, builds the
timeline, atomically writes three actual formats, then reads and checks them
against independently calculated intervals. There were 4,000 final cuts,
3,607,761 output bytes and complete agreement between the formats. Measured
end-to-end file processing took **0.086791 seconds**.

For separately timed provenance lookup on the same cuts and source spans, the
independent nested-loop baseline performs 20,000,000 overlap checks. Serial
three-run median wall time was **0.297635 seconds** for that lookup and
**0.001301 seconds** for the sweep. Every contributor ID tuple was equal. These
are isolated lookup measurements, not whole-program speedups, media benchmarks
or timing guarantees. Host load affects results; tests assert no timing threshold.
`--size` and `--repeats` allow bounded reruns.

Separately, the unit suite checks 200 fixed-seed small randomized timelines
against an independent millisecond-set oracle, including correctly empty cases.
It verifies interval sets and contributor/overlap mappings beyond the benchmark's
nonoverlapping workload.
