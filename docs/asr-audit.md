# mediawrench asr-audit

Whisper-style ASR sometimes repeats a phrase, loops inside a segment or "hears" speech
in silence, and finding those spots in a long interview transcript by hand is slow.
`mediawrench asr-audit` is a read-only triage pass: it joins local segment JSON (or native
whisper.cpp JSON) with speech intervals you supply, and exports explainable flags, a CSV and
review ranges for a human to check. Use it before publishing or editing an automatic
transcript. A repeat can be genuine speech: flags are prompts for review, **not
hallucination labels**, and the original transcript bytes are never rewritten.

No models, audio decoding, network calls or external executables are involved;
FFmpeg is not needed.

## Quick start

```bash
pip install mediawrench
mkdir -p out
mediawrench asr-audit examples/asr-audit/transcript.json \
  --vad examples/asr-audit/speech.json \
  --json out/report.json --csv out/flags.csv --review-srt out/review.srt
```

In this synthetic example, segments 0 and 1 (`Thank you.` twice, outside the supplied
speech) are flagged as `adjacent_repeat` plus `outside_speech` and listed in
`speech_corroborated_repeats`; segment 2 (`Yes, yes.` repeated inside speech) is flagged
`adjacent_repeat` only. The review SRT has a cue at `00:00:01,000 --> 00:00:03,000`.
Without `--json PATH` the JSON report goes to stdout. `--dry-run` validates inputs and
destination paths, prints the JSON to stdout and writes nothing.

[`examples/asr-audit/demo.py`](../examples/asr-audit/demo.py) runs the same audit
through the library and the CLI and checks JSON/CSV/SRT contents and the source hash.

## Outputs, safety and exit codes

- **JSON report** (`--json PATH`, default stdout `-`): all thresholds, evidence for
  **all** segments, `flags`, `review_ranges`, `speech_corroborated_repeats`,
  `unplaced_segments`, the complete `source_document`, and (when run by the CLI) `source`,
  `vad_source` and `loop_config_source` with byte counts and SHA-256.
  `unchanged_source: true` describes the read-only contract, not a truth claim.
- **Flag CSV** (`--csv PATH`): flagged segments only. Unknown evidence is an empty cell.
  Text is CSV-quoted and kept exactly, including newlines and formula-like strings; no
  spreadsheet formula escaping is added, so import text columns as text.
- **Review SRT** (`--review-srt PATH`): coalesced navigation cues with reasons, not a
  replacement transcript. Start times round down and end times up to milliseconds; invalid
  timings are omitted with a stderr notice (they stay in JSON/CSV `unplaced_segments`).
- **Token CSV** (`--token-csv PATH`): native whisper.cpp only; see below.

Every output path must be new: existing files, input aliases through symlinks or
hardlinks and duplicate destinations are refused. Parent directories must exist. Files are
staged beside their destination and published atomically without clobbering; a
filesystem error can leave a subset of complete sidecars (the set is not a transaction).
Exit code `0` means the audit completed (flags do not change it); `2` means bad or
unsupported input or an output error.

## Input contract: seconds, never inferred

The default `--input-format segments` expects a UTF-8 JSON **object** with `segments`, an
array of objects with finite numeric `start`, `end` (seconds) and string `text`:

```json
{"time_unit":"seconds","segments":[
  {"start":1,"end":2,"text":"Thank you","no_speech_prob":0.9},
  {"start":2,"end":3,"text":"Thank you","no_speech_prob":0.95}
]}
```

- `time_unit` is optional, but if supplied it must be `"seconds"`. Units are never guessed
  from magnitude.
- `no_speech_prob` must be a finite number in `[0,1]`; `avg_logprob` must be finite and is
  compared directly, without rescaling. Absent or `null` scores are **unknown**.
  Boolean/numeric strings, nonfinite numbers anywhere, duplicate JSON keys, missing fields
  and unsupported roots fail.
- Negative, zero-length or reversed timing is kept with `invalid_timestamp`; its rate and
  VAD evidence stay unknown and it is listed in `unplaced_segments`. Overlapping or
  out-of-order segments stay in source order.
- Extra metadata (IDs, words, top-level text) is preserved in `source_document`. Word
  timing and confidence are not analyzed.

The optional VAD file (`--vad PATH`) is exactly `[[start,end], ...]` in seconds, with
finite numbers and `0 <= start < end`. It lists **speech**, not silence. Unsorted,
overlapping or touching intervals are unioned. `[]` explicitly supplies no speech;
omitting `--vad` means unknown, with no outside-speech evidence. JSON `null`, object
wrappers and malformed intervals are rejected. VAD may be incomplete or wrong; it cannot be
validated against audio.

Whisper segment JSON and faster-whisper records serialized with `start`, `end`, `text`
and optional `no_speech_prob`/`avg_logprob` fit this contract directly. Neither project is
imported.

### Native whisper.cpp JSON

Native whisper.cpp output requires `--input-format whisper.cpp-json-v1.7.6` (library:
`audit(document, input_format="whisper.cpp-json-v1.7.6")`); there is no format detection.
The adapter targets the
[`whisper-cli -oj` / `-ojf` schema of v1.7.6](https://github.com/ggml-org/whisper.cpp/blob/a8d002cfd879315632a579e73f0148d06959de36/examples/cli/cli.cpp#L594-L752)
(commit `a8d002cfd879315632a579e73f0148d06959de36`). Selecting a version declares a schema;
it cannot authenticate which exporter wrote a file, and other versions are not certified.

```json
{"transcription":[{
  "timestamps":{"from":"00:00:00,000","to":"00:00:10,000"},
  "offsets":{"from":0,"to":10000},"text":"one","id":"clip-A/9"
}]}
```

becomes a segment with `start: 0`, `end: 10` seconds and the same text, ID, offsets and
timestamps. Both representations are required: `offsets.from/to` are **integer
milliseconds**, `timestamps.from/to` are `HH:MM:SS,mmm` **strings**, and they must describe
exactly the same milliseconds before offsets are divided by 1000. Changing `offsets.to` to
`10` fails with `transcription[0].offsets.to: ... conflicts with transcription[0].timestamps.to`.
Numeric strings, fractional offsets, missing representations, dot-format timestamps, mixed
`start`/`end`/`time_unit` fields, nonfinite values and precision loss fail with a concrete
field path and exit `2`.

Native reports keep the native input in `source_document` and add `normalized_document`,
`input_format` and `conversion` (pinned source, unit policy, per-segment source/normalized
paths and endpoint evidence). Flags still use zero-based segment indices.

The synthetic format fixtures in [`tests/asraudit/fixtures/`](../tests/asraudit/fixtures/)
(provenance in `whisper_cpp_provenance.json`) show that native and normalized input yield
identical evidence:

```bash
mediawrench asr-audit tests/asraudit/fixtures/whisper_cpp_v1_7_6.json \
  --input-format whisper.cpp-json-v1.7.6 \
  --vad tests/asraudit/fixtures/whisper_cpp_speech.json --outside-speech-fraction 0.2 \
  --json out/native-report.json --csv out/native-flags.csv --review-srt out/native-review.srt
```

Segments 0 and 1 get fraction `0.2` with `adjacent_repeat` and `outside_speech`, both enter
`speech_corroborated_repeats`, with one review range `[0,20]`; segment 2 has fraction `0`
and no flags. Compare with `whisper_cpp_normalized.json` in the default format.

### Token evidence (native only)

With the native format, each flag also carries `token_evidence`, a read-only view of the
`-ojf` token records of that segment, with every original field copied:

```json
"token_evidence": {
  "source_path": "transcription[0].tokens",
  "tokens": [{"source_path":"transcription[0].tokens[0]", "token": {
    "text":" Thank","id":1044,"p":0.125,"t_dtw":-1,
    "timestamps":{"from":"00:00:00,000","to":"00:00:00,500"},
    "offsets":{"from":0,"to":500}
  }}]
}
```

- Paths are zero-based in `source_document`. Tokens keep source order, exact text, special
  tokens, extra metadata and parsed values. Offsets stay integer milliseconds and
  timestamps stay original strings; `t_dtw` stays raw (including `-1`). There is no
  sorting, clipping, alignment or timing inference.
- `p` is **token probability**, not segment confidence: it is never averaged,
  thresholded or used as a segment score, and even `p: 0` cannot flag a segment.
- Missing `tokens` (plain `-oj`) gives `token_evidence: null`; `tokens: []` gives an empty
  list. The default `segments` format always gives `null`.
- A present `tokens` must be an array of objects with string `text`, nonnegative int32
  `id`, finite `p` in `[0,1]` and finite `t_dtw` (`-1` or nonnegative). Timing objects may
  both be absent; if either is present, both must be complete and match like segment
  endpoints. Tokens in **all** segments are validated, including unflagged ones.

`--token-csv PATH` (native format only, even with `--dry-run`) writes one row per token in
flagged segments, with columns `segment`, `token_index`, `source_path`, `text`, `id`, `p`,
`t_dtw`, `offsets`, `timestamps` (timing cells are compact JSON, empty when untimed):

```bash
mediawrench asr-audit tests/asraudit/fixtures/whisper_cpp_token_csv_v1_7_6.json \
  --input-format whisper.cpp-json-v1.7.6 \
  --json out/token-report.json --token-csv out/tokens.csv
```

Flags are segments `0,1,3,4`; CSV rows are `transcription[0].tokens[0]`, `[1]`, `[2]`, then
`transcription[1].tokens[0]`. Two filters narrow only the CSV:

- `--token-csv-timing timed|untimed` (default `all`): timed rows supply both raw `offsets`
  and `timestamps`, untimed rows supply neither. Here `timed` gives
  `transcription[0].tokens[0]`, `transcription[1].tokens[0]`; `untimed` gives
  `transcription[0].tokens[1]`, `transcription[0].tokens[2]`.
- `--token-csv-segment N` (repeatable): zero-based **source segment indices** (not
  upstream IDs). Indices form a set; order and duplicates never reorder rows. Selection
  intersects with the timing filter; empty, unflagged and missing views never produce
  placeholder rows, and a zero-row CSV still has its header.

Both filters require `--token-csv`; invalid values fail with exit `2` before any output.
The library renderer is `mediawrench.asraudit.output.csv_tokens(report, timing="all",
segment_indices=None)`.

Every JSON report includes `token_evidence_summary`, counting **flagged segments only**.
For the fixture above:

```json
{"flagged_segments":4,"unavailable_segments":1,"empty_segments":1,
 "segments_with_tokens":2,"token_rows":4,"timed_token_rows":2,"untimed_token_rows":2,
 "unavailable_segment_indices":[3],"empty_segment_indices":[4],"untimed_only_segment_indices":[],"mixed_timing_segment_indices":[0],"timed_only_segment_indices":[1],
 "segment_token_counts":[
   {"segment":0,"token_rows":3,"timed_token_rows":1,"untimed_token_rows":2},
   {"segment":1,"token_rows":1,"timed_token_rows":1,"untimed_token_rows":0}],
 "untimed_token_source_paths":["transcription[0].tokens[1]","transcription[0].tokens[2]"],
 "timed_token_source_paths":["transcription[0].tokens[0]","transcription[1].tokens[0]"]}
```

- Unavailable (`null` view), empty (`tokens: []`) and populated segments sum to
  `flagged_segments`; `timed_token_rows + untimed_token_rows = token_rows`.
- The timed-only, untimed-only and mixed index lists partition the populated views;
  `segment_token_counts` gives per-view counts that sum to the totals; the two path lists
  are disjoint and cover every flagged token.
- All of this describes **supplied** timing availability only: zero, reversed or
  out-of-segment pairs still count as timed. Nothing implies timing validity, audio
  alignment, confidence or spoken-word coverage. With no flags every count is zero and
  every list empty.

## Rules

All thresholds are recorded in each report. Every flag includes the zero-based segment
index, exact source text, timing, measured values and reasons.

| Reason | Default rule | Option |
| --- | --- | --- |
| `adjacent_repeat` | At least 2 adjacent equal whole-segment phrases, gap <= 1 second | `--min-repeat-segments`, `--repeat-max-gap-seconds` |
| `intra_segment_loop` | At least 3 contiguous copies of a pattern of >= 2 words | `--loop-config` |
| `excessive_character_rate` | More than 25 non-whitespace code points per second | `--max-chars-per-second` |
| `invalid_timestamp` | Negative start, or end <= start | fixed |
| `high_no_speech` | Present `no_speech_prob` >= 0.8 | `--high-no-speech-prob` |
| `low_avg_logprob` | Present `avg_logprob` <= -1.0 | `--low-avg-logprob` |
| `outside_speech` | Positive fraction outside supplied speech >= 0.8 | `--outside-speech-fraction` |

- Phrase equality uses NFKC, Unicode casefold and collapsed whitespace; punctuation is
  kept. Empty phrases and invalid timing break runs; adjacent records need nondecreasing
  starts (overlaps are fine) and a gap above the threshold breaks the run.
- Outside fraction is `(duration - duration(segment ∩ union(speech))) / duration`,
  compared as exact rationals with no tolerance: exactly `2/10` meets `0.2`, and zero
  outside duration never flags, even at threshold `0`. It is rounded to a float only for
  output.
- `speech_corroborated_repeats` is a separate **subset** requiring both `adjacent_repeat`
  and `outside_speech`. It does not suppress other flags.
- `review_ranges` unions flagged valid intervals in time order, coalescing gaps up to
  `--coalesce-gap-seconds` (default 0.25), with segment indices and reason unions.

### Within-segment loops

`intra_segment_loop` finds **contiguous** copies of a text pattern within one segment
(also for invalid timing). With no VAD, a segment from 0 to 10 s with text
`Thank you. Thank you. Thank you.` yields a `medium` loop with `repeat_count: 3`, `pattern_units: 2`,
`pattern_tokens: ["thank","you","."]`, whole range `[0,32)` and occurrences `[0,10)`,
`[11,21)`, `[22,32)`. Offsets are zero-based, end-exclusive Unicode code points in the
original text (`text[start:end]` in Python). JSON segments and flags expose
`intra_segment_loops`, the effective `loop_settings` and `loop_search`; the CSV has them as
JSON cells, and the review SRT uses the whole segment time.

Two copies of `Thank you.`, `yes yes yes yes yes yes` (one-word pattern) and
`Thank you. Thank you! Thank you.` (punctuation differs) do not qualify by default. Word
mode groups letters, numbers, marks and connector punctuation into lexical tokens
(NFKC + casefold) and treats punctuation/symbols as exact separate tokens; character mode
compares exact non-whitespace code points. The minimum pattern length counts lexical
units only.

Configure rules with `--loop-config PATH` (library: `audit(document, loop_config={...})`):

```json
{
  "default": {
    "unit": "words",
    "min_pattern_units": 2,
    "max_pattern_tokens": null,
    "min_repetitions": 3,
    "high_severity_repetitions": 6
  },
  "languages": {"zh": {"unit": "characters", "min_pattern_units": 4}}
}
```

Language rules inherit the resolved default and are selected by the segment's nonempty
string `language`, else the document's, else the default. Keys match exactly (`zh-CN` does
not match `zh`); language is never inferred. With this config and `language: "zh"`,
`欢迎收听，欢迎收听，欢迎收听，` yields ranges `[0,5)`, `[5,10)`, `[10,15)`. `unit` is `words`
or `characters`; `min_pattern_units` is an integer >= 1; `min_repetitions` >= 2;
`high_severity_repetitions` >= `min_repetitions`; `max_pattern_tokens` is `null` (search all
feasible periods) or an integer >= 1. Unknown fields, wrong types (including `2.0`),
duplicate keys and a `null` config fail with a concrete path and exit `2`.

```bash
mediawrench asr-audit tests/asraudit/fixtures/intra_segment_loops.json \
  --loop-config tests/asraudit/fixtures/loop_config.json --dry-run
```

Only segments 0 and 1 flag `intra_segment_loop`; segments 2 and 3 show below-minimum
repeats and punctuation differences. Severity is `medium` below
`high_severity_repetitions` and `high` at or above it; every loop is assessed as
`suspected_text_pattern`. Loops do not enter `speech_corroborated_repeats`, and no text is
deleted. Selection emits leftmost non-overlapping runs, preferring the longest coverage and
then the shortest period; approximate, interrupted and semantic loops are out of scope.

`max_pattern_tokens` caps period scanning on long segments. It counts **all** pattern
tokens including punctuation (`Thank you.` needs 3; `欢迎收听，` in character mode needs 5).
Every segment records `loop_search`, for example with cap 2 on the text above:

```json
{"token_count":9,"max_possible_pattern_tokens":3,
 "searched_max_pattern_tokens":2,"limited_by_config":true}
```

A limited search with no loops does not rule out longer patterns. The report-level
`loop_search_summary` reduces this over all segments, e.g.
`{"total_segments":1,"limited_segments":1,"limited_segment_indices":[0],"max_searched_pattern_tokens":2}`.
Try `--loop-config tests/asraudit/fixtures/loop_config_bounded.json` on the loop fixture:
only segment 1 flags, and the summary is `{"total_segments":4,"limited_segments":2,
"limited_segment_indices":[0,3],"max_searched_pattern_tokens":5}`. Loop comparisons take
O(n·B) time and O(n) working memory for n tokens and searched maximum B; `null` keeps the
quadratic worst case.

## Library

```python
from mediawrench.asraudit import Thresholds, audit

report = audit(
    {"segments": [{"start": 1, "end": 2, "text": "Thank you"}]},
    speech_intervals=[[0, 0.5]],
    thresholds=Thresholds(outside_speech_fraction=0.75),
)
assert report["flags"][0]["reasons"] == ["outside_speech"]
```

The library has no filesystem access and omits file-level provenance.

## Synthetic evaluation

[`benchmarks/asr-audit/`](../benchmarks/asr-audit/RESULTS.md) holds a seeded,
transcript-only experiment (`python benchmarks/asr-audit/run.py --seed 833`, output in
`results.json`). On 84 synthetic segments (32 labeled positives), repetition alone flags
all positives with 28 false positives (precision 0.53, recall 1.0); requiring repetition
**and** outside-speech evidence gives 24 TP / 4 FP / 8 FN (precision 0.86, recall 0.75).
The labels are authored scenario assumptions, not real hallucination annotations, so this
measures the configured rules, not real-world ASR accuracy.

## Limits

Supplied VAD and scores are unverified external evidence. Exact matching misses
paraphrases and interrupted loops, and intentional repeats can be flagged. Character-rate
and loop settings are language dependent. Inputs and reports are held in memory and
segment/VAD intersection is not optimized for very large files. There is no automatic
deletion and no speech detection.

Reported motivation includes [whisper.cpp #1724](https://github.com/ggml-org/whisper.cpp/issues/1724),
[#3744](https://github.com/ggml-org/whisper.cpp/issues/3744) and
[faster-whisper #465](https://github.com/SYSTRAN/faster-whisper/issues/465). Removal tools
such as [whisper-hallucination-remover](https://github.com/godxxy1229/whisper-hallucination-remover)
exist; this command only gathers evidence for review, copies no code from them and deletes
no text.
