# mediawrench speakers

Diarization tools emit RTTM speaker intervals, ASR tools emit word or segment
timestamps, and the two rarely line up cleanly: words straddle speaker changes,
speakers overlap, and the two clocks can drift. `mediawrench speakers` joins an
existing RTTM file with existing Whisper-style ASR JSON and labels each word with a
speaker, **marking uncertain words `AMBIGUOUS` or `UNKNOWN` instead of guessing**,
and keeps the overlap evidence behind every decision. Use it when you already have
a transcript and a diarization for an interview and want a speaker-labelled JSON,
Markdown or SRT you can audit.

Everything runs offline on local files: no audio, models, diarization service,
FFmpeg, tokens or network access, and no speaker identity is inferred.

```bash
pip install mediawrench
mediawrench speakers --help
```

## Quick start

All inputs are synthetic and ship in [`examples/speakers/`](../examples/speakers/).
Run from a clone of the repository:

```bash
# Speaker-labelled JSON with display names for the RTTM speaker IDs
mediawrench speakers --asr examples/speakers/whisper.json --rttm examples/speakers/interview.rttm \
  --aliases examples/speakers/aliases.json --format json --output interview.json

# Speaker-labelled subtitles
mediawrench speakers --asr examples/speakers/whisper.json --rttm examples/speakers/interview.rttm \
  --format srt --output interview.srt

# WhisperX input, Markdown output
mediawrench speakers --asr examples/speakers/whisperx.json --rttm examples/speakers/interview.rttm \
  --format markdown --output interview.md

# Validate, merge and render with custom thresholds, but write nothing
mediawrench speakers --asr examples/speakers/faster-whisper.json --rttm examples/speakers/interview.rttm \
  --min-coverage 0.6 --tie-epsilon 0.001 --max-gap 0.8 --dry-run

# Library demo: merge, conservation checks and an SRT round trip through pysubs2
python examples/speakers/demo.py
```

Output defaults to stdout; warnings go to stderr. `--output` writes a single file
atomically after validation and rendering; its parent directory must exist and it
refuses to overwrite an input (including via symlinks or hard links). `--dry-run`
performs the same validation, merge and rendering, prints counts and writes nothing.
Invalid or unsupported input returns exit **2**.

The demo writes JSON, Markdown and SRT under `examples/speakers/output/`. Its expected
result is 7 original words plus 2 text-only spans: 9 items, 9 turns, 3 items with
segment precision and 9 reparsed SRT cues. It exercises `AMBIGUOUS`, `UNKNOWN`,
aliases and missing word timing.

## How speakers are assigned

1. Apply the explicit RTTM clock correction (identity by default) and the boundary
   policy (strict rejection by default), then sort and **union each speaker's
   intervals**, merging duplicates, nesting and adjacency. Repeated RTTM rows cannot
   inflate a speaker's score.
2. Intersect each word's `[start, end)` with those unions. Coverage is the
   intersection duration divided by the word duration. The speaker with the largest
   coverage wins only if it meets `--min-coverage` (default **0.5**, inclusive).
3. When two or more positive-overlap speakers are within `--tie-epsilon` (default
   **0.000001**) of the best coverage, the word is `AMBIGUOUS`. The epsilon is an
   **absolute coverage fraction**, not seconds. Low coverage or no intersection
   gives `UNKNOWN`. There is no nearest-speaker guessing.
4. Consecutive items are grouped into turns only when speaker, precision and source
   segment agree, starts do not move backwards, and the gap is at most `--max-gap`
   seconds (default **1**). Input text order is preserved and no word is dropped.

## Input formats

### RTTM

Ten whitespace-separated fields per `SPEAKER` record:

```text
SPEAKER interview 1 0.000 2.000 <NA> <NA> Alice <NA> <NA>
```

The recording, channel, start, duration and speaker fields are used. Start and
duration are finite seconds, start is nonnegative, duration is positive and channel
is a positive integer. Blank lines and `#` comments are allowed; other record types,
malformed rows and an empty file fail. Exactly one recording/channel is merged: use
`--recording NAME` and/or `--channel N` to select from a mixed file. Every row is
validated, even rows the selection excludes. Speaker IDs `UNKNOWN` and `AMBIGUOUS`
are reserved.

### ASR JSON

A JSON object with one of these views:

- Whisper-style `segments`: each has `start`, `end`, `text` and optional `words`
  with `{word, start, end}`. Metadata such as IDs, probabilities and language is
  retained. Segment-only transcripts are supported.
- Serialized faster-whisper segments and words in the same shape. faster-whisper's
  Python API returns objects and generators; this command accepts their **JSON
  serialization** only (see `examples/speakers/faster-whisper.json`).
- WhisperX aligned `segments[].words`, including words without timestamps. If the
  redundant `word_segments` is also present, `segments` wins with a warning; the
  redundant view stays in `source_asr`.
- A root `words` or `word_segments` array. Every word needs start and end because
  this view has no segment fallback. Bare word strings get a separating space for
  presentation; the original `word` strings stay unchanged.

Times are JSON numbers in seconds (never strings, booleans, NaN or Infinity).
Optional `time_unit`/`time_units` at root, segment or word level may only be `s` or
`seconds`; convert milliseconds first. A timed word outside its segment, reversed
intervals, unordered start times, empty text and unsupported schemas fail.
Zero-duration words are kept as `UNKNOWN` with reason `zero_duration`.

For each segment, words are matched monotonically to `segment.text`, ignoring only
whitespace, and the exact original text (whitespace included) is partitioned among
the output items. Missing or partial word timing falls back to the whole segment
span with `precision: "segment"`. Non-whitespace text the word list omits becomes
its own segment-precision item. A word that cannot be matched in order is an error
rather than being dropped. If a root `text` exists it must agree with the combined
text, ignoring whitespace.

### Aliases

A JSON object such as `{"Alice": "Interviewer", "Bob": "Participant"}`. Aliases
affect display only. Unknown speaker keys, duplicate keys or display names,
collisions with unchanged IDs, control characters and reserved labels fail.

## Output

JSON (schema version 1) is the lossless evidence format:

- `items`: `word` (original word or unaligned text), `text` (exact presentation
  slice), times, precision, global `source_word` (null for text-only spans),
  `source_word_in_segment`, `source_segment`, chosen speaker ID and display label,
  coverage, reason, candidates, clipped union intervals and per-speaker overlap.
- `simultaneous_speech` on each item: true only when a positive-duration portion
  has more than one speaker active. A word straddling sequential speakers can be
  ambiguous without simultaneous speech, and per-speaker coverages can sum above 1
  when speakers overlap.
- `turns`: display labels, precision and `item_ids` referencing every item exactly
  once, in order.
- `source_asr`, the selected parsed `source_rttm` with line numbers,
  `unioned_intervals`, `corrected_rttm`, configuration, recording/channel and
  warnings, so every assignment can be inspected or reproduced.

Markdown labels each turn and its precision, flattens whitespace and escapes
formatting characters. SRT labels every cue, adds `[segment]` for coarse items,
rounds bounds outward to milliseconds and gives zero-duration words a 1 ms cue. SRT
is generated **and reparsed** with pysubs2; times beyond `99:59:59.999` fail rather
than being clamped. Subtitle text is flattened to one line, with HTML/ASS
metacharacters and timestamp-like colons encoded as HTML entities (player support
for entities varies). SRT and Markdown are presentation views; keep the JSON when
you need the evidence.

## Clock correction

If the RTTM clock differs from the ASR clock, supply the correction yourself:

```bash
mediawrench speakers --asr examples/speakers/clock-asr.json --rttm examples/speakers/clock-shifted.rttm \
  --rttm-offset-seconds -2 --rttm-drift-ppm 250000 --recording-duration 6 --output corrected.json
```

Both parameters default to zero. Each selected RTTM row is transformed **before
union and assignment**, anchored at RTTM time zero:

```text
scale = 1 + drift_ppm / 1000000
corrected = original * scale + offset_seconds
```

A positive offset moves speaker activity later; positive ppm expands elapsed RTTM
time (`1000 ppm` is scale `1.001`). The parameters map **RTTM to ASR** and are never
estimated: no drift, offset, anchor, duration or identity is inferred, and ASR and
subtitle times stay on their own clock. The example uses an exaggerated drift for
exact arithmetic: A `[2,4]` becomes `[0.5,3]` and B `[4,6]` becomes `[3,5.5]`, so the
four words receive `A`, `AMBIGUOUS`, `B`, `UNKNOWN`, exactly as with
`examples/speakers/clock-baseline.rttm`.

`--recording-duration` is an optional positive duration on the ASR clock. With the
default `--rttm-out-of-range-policy error`, every corrected RTTM row and every ASR
assignment item must lie within `[0, duration]` (endpoints allowed). Negative or
nonfinite corrected times, reversed or collapsed intervals (including
floating-point collapse) and times beyond the duration fail with exit **2**, also in
`--dry-run`, and existing output is preserved. Drift must be greater than
`-1000000` ppm so the scale stays positive. Diagnostics name the RTTM line or ASR
item.

JSON keeps `source_asr` and `source_rttm` unchanged and adds `corrected_rttm` (one
entry per selected row with source line, speaker, `original_start`/`original_end`
and corrected `start`/`end`) plus `config.rttm_time_correction` (formula, direction,
offset, ppm, scale, duration and policy), so every endpoint can be recomputed.

### Clipping to the recording

To intersect corrected RTTM with a known recording range instead of rejecting it:

```bash
mediawrench speakers --asr examples/speakers/clipping-asr.json --rttm examples/speakers/clipping.rttm \
  --rttm-offset-seconds -2 --rttm-drift-ppm 1000000 \
  --recording-duration 4 --rttm-out-of-range-policy clip --output clipped.json
```

`clip` requires `--recording-duration`. Each selected row is corrected, clamped to
`[0, duration]`, and positive-duration results are unioned per speaker. Rows outside
or only touching the range become zero-length, are excluded from assignment and stay
in the audit:

| RTTM line / speaker | Raw | Unclipped corrected | Effective | `clipping_status` |
| --- | --- | --- | --- | --- |
| 2 / Early | [0,0.5] | [-2,-1] | [0,0] | dropped |
| 3 / A | [0.5,2] | [-1,2] | [0,2] | clipped |
| 4 / A | [1,1.5] | [0,1] | [0,1] | unchanged |
| 5 / B | [2,4] | [2,6] | [2,4] | clipped |
| 6 / Late | [4,5] | [6,8] | [4,4] | dropped |

The words receive `A`, `AMBIGUOUS`, `B`, `UNKNOWN`. `corrected_rttm` keeps the
original, unclipped and effective endpoints, `clipping_status` and a `diagnostics`
array per row; changed rows also appear in `warnings`. Clipping never repairs
malformed RTTM, invalid drift or ASR items outside the recording; those still fail.

## Many recordings and channels: manifests

Process independent recording/channel pairs in one run with an explicit manifest:

```bash
mediawrench speakers --manifest examples/speakers/batch/manifest.json --output-dir batch-out
```

```json
{
  "schema_version": 1,
  "entries": [
    {"recording": "room-a", "channel": "1", "asr": "room-a.json", "rttm": "rooms.rttm"},
    {"recording": "room-b", "channel": "1", "asr": "room-b.json", "rttm": "rooms.rttm"}
  ]
}
```

- The output directory must be **new** (its parent must exist); existing
  directories, files and symlinks are refused, also in `--dry-run`.
- Input paths are relative to the manifest's directory (absolute paths also work).
  Each entry needs `recording` (an exact RTTM token), `channel` (positive integer or
  digit string; `1`, `"1"` and `"01"` are the same) and `asr`/`rttm` paths. The pair
  `(recording, channel)` must be unique. Nothing is guessed from file names or list
  positions; the manifest is your explicit ASR-to-RTTM binding.
- Optional per-entry fields: `aliases` (path), `options` (any of `min_coverage`,
  `tie_epsilon`, `max_gap`, `rttm_offset_seconds`, `rttm_drift_ppm`,
  `recording_duration`, `rttm_out_of_range_policy`, with the same rules as the
  flags), `exports` (`["srt"]`, `["markdown"]` or both) and `input_sha256` (see
  below).
- Missing identities, duplicate pairs or JSON keys, nonfinite options, unknown
  fields and unsupported schema versions reject the whole manifest before anything
  is read or written (exit **2**).

Output is one `entry-NNNN.json` per entry (unchanged single-pair schema), optional
`entry-NNNN.srt`/`entry-NNNN.md` companions, and `summary.json`, written last as
the completion marker. For the example, `entry-0000.json` has `Hello` by **Alice**
(`room-a/1`), `entry-0001.json` has `Goodbye` by **Bob** (`room-b/1`), and the
summary reports `status: "complete"` with counts `{total: 2, succeeded: 2, failed: 0,
items: 2, turns: 2}`. The shared RTTM also has `room-a/2` (Carol) at the same times;
it never leaks into either result. Each pair is merged independently: speaker IDs
in different results are local labels only.

In manifest mode only `--manifest`, `--output-dir`, `--dry-run`,
`--summary-format text|json`, `--observe-input-sha256`, `--manifest-sha256` and
`--require-input-sha256` are accepted; single-recording flags cannot be mixed in.

**Failures and exit codes.** An entry that fails (input, merge, render) does not
stop later entries. Exit **0** means every entry and requested export succeeded;
**1** means any entry or export failed (status `partial` if at least one JSON result
survives, `failed` otherwise); **2** means a usage, preflight or output I/O error, in
which case no summary is printed. A failed entry has `result: null` and an `error`
with its `stage` and a diagnostic naming `entries[i] (recording/channel)`.
Requested companions report `{"status": "ok" | "error" | "skipped", "file": ...}`,
and `export_counts` totals `requested = succeeded + failed + skipped` per format.
For example, `examples/speakers/batch/manifest-presentations.json` returns **1**:
entry 0's SRT overflows `99:59:59.999` while its JSON and Markdown survive.

`--dry-run` reads, merges and renders everything and returns the same 0/1 outcome
without creating the directory. Handled write errors remove the incomplete
directory; a directory without `summary.json` is not a finished run. Inputs are
never modified. `--summary-format json` prints the same object as `summary.json`
on stdout (diagnostics stay on stderr).

### Byte bindings for manifests

All digests are 64 lowercase hexadecimal SHA-256 characters. They bind bytes; they
are not signatures and do not prove a recording's identity.

- `input_sha256` in an entry (`{"asr": ..., "rttm": ..., "aliases": ...}`, each
  optional) checks the exact bytes of that input before parsing the same buffer. A
  mismatch fails that entry (exit **1**), skips its companions and lets later
  entries run.
- `--require-input-sha256` rejects the whole run (exit **2**, nothing read or
  written) unless every referenced input has an expectation. The tool never fills
  gaps by hashing current files.
- `--manifest-sha256 DIGEST` requires the exact manifest bytes before decoding;
  a mismatch rejects the run with exit **2**.
- `--observe-input-sha256` records `observed_manifest` and per-entry
  `observed_inputs` (`{sha256, size_bytes}` of the bytes actually read), even when
  a later step fails.

### Checking a manifest without processing it

`--validate-manifest` checks structure, digest syntax and hash policies **without
reading any entry input** or choosing an output directory:

```bash
mediawrench speakers --manifest examples/speakers/batch/manifest-presentations.json --validate-manifest
mediawrench speakers --manifest examples/speakers/batch/manifest-presentations.json --validate-manifest \
  --validate-options --summary-format json
```

It prints `Manifest validated: N entries, E/R input expectations; input files not
read; no output written` (or a JSON report with `report_type:
"manifest_validation"`) and exits **0**, or exits **2** with a diagnostic. Combine
it with `--manifest-sha256` and `--require-input-sha256`, and optionally:

- `--validate-options`: also check the input-independent option values (ranges,
  types, `clip` needing a duration); stops at the first error with exit **2**.
- `--diagnose-options`: report the first option error of **every** invalid entry
  (exit **1** if any, **0** otherwise); conflicts with `--validate-options`.
- `--diagnose-input-coverage`: list every referenced input lacking an expected
  SHA-256 (exit **1** if any are missing); cannot be combined with
  `--require-input-sha256` or the option checks.

None of these modes looks up, reads or merges entry inputs, so a manifest that
validates can still fail every entry when processed.

## Python API

```python
from mediawrench.speakers import merge, parse_rttm, render_srt, run_manifest, validate_manifest

diarization = parse_rttm("SPEAKER interview 1 0 2 <NA> <NA> Alice <NA> <NA>")
result = merge({"words": [{"word": "Hello", "start": 0.5, "end": 1.0}]}, diarization)
assert result["items"][0]["speaker"] == "Alice"
assert result["items"][0]["coverage"] == 1.0
srt = render_srt(result)
```

`merge` accepts the same controls as keyword arguments (`min_coverage`,
`tie_epsilon`, `max_gap`, `rttm_offset_seconds`, `rttm_drift_ppm`,
`recording_duration`, `rttm_out_of_range_policy`). `run_manifest(manifest,
output_dir, *, dry_run=False, observe_input_sha256=False, expected_manifest_sha256=None,
require_input_sha256=False)` returns the summary; `validate_manifest`,
`diagnose_manifest_options` and `diagnose_manifest_input_coverage` return the
reports described above. Errors raise `BridgeError`, `OSError` or `UnicodeError`.

## Benchmark

[`benchmarks/speakers/`](../benchmarks/speakers/RESULTS.md) compares the assignment
policy with a deliberately flawed row-sum baseline (4/4 versus 2/4 expected labels)
and checks that the bisection index returns the same overlaps as a full scan.

## Relationship to other tools and limitations

[Whisper](https://github.com/openai/whisper) and
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) produce the ASR
JSON; this command only consumes existing files.
[WhisperX](https://github.com/m-bain/whisperX) (BSD-2-Clause) already provides
alignment, diarization and speaker assignment, and
[whisper-diarization](https://github.com/MahmoudAshraf97/whisper-diarization) is an
existing pipeline. `mediawrench speakers` is a small existing-file join with
explicit union arithmetic, an ambiguity policy and assignment provenance; it does
not claim better speaker accuracy than those pipelines.
[pysubs2](https://github.com/tkarabela/pysubs2) (MIT) handles SRT I/O.

- Each result covers one explicitly selected recording/channel; manifests process
  pairs serially with no global speaker mapping.
- Inputs must use seconds and share a clock or have a known linear RTTM-to-ASR
  correction. Undeclared milliseconds or a wrong recording cannot be detected
  reliably. There is no automatic synchronization, nonlinear drift or per-speaker
  correction. Correction uses Python floats and rejects intervals that collapse.
- Segment fallback is coarse and may overlap or precede adjacent timed words; SRT
  keeps transcript order and may contain overlapping cues.
- Text matching is literal, not linguistic realignment.
- The whole source JSON and evidence are held in memory (manifest mode also holds
  rendered outputs until the run finishes): built for interview exports, not
  unbounded streams.
