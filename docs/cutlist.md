# mediawrench cutlist

Building a rough cut from an interview transcript usually means picking subtitle
cues by hand and retyping their times into an editor, where overlapping answers,
padding and "drop this aside" decisions silently drift. `mediawrench cutlist` turns
an **explicit** selection of SRT cue IDs into a LosslessCut v2 project (`.llc`), a
headerless seconds CSV and a provenance JSON, all generated from **the same**
interval set. Use it when you want a reviewable, reproducible cut list from a
transcript, optionally checked against the real media duration and frame rate.

It never cuts media, transcribes, guesses a selection or touches the network.
Duration can be declared in the plan or read with an optional, explicitly named
local `ffprobe`; an explicit video stream can also be checked against the declared
frame rate and zero-origin assumption. Two read-only checks verify saved outputs
later: `verify-companions` and `verify-probe-evidence`.

```bash
pip install mediawrench
mediawrench cutlist --help
# Optional, only for --ffprobe features: install FFmpeg (e.g. apt install ffmpeg, brew install ffmpeg)
```

## Quick start

The examples are synthetic text files in [`examples/cutlist/`](../examples/cutlist/).
Run from a clone of the repository.

Find candidates for repeated text. This prints IDs 2 and 4 and **selects nothing**;
even a unique match needs an explicit plan. Queries are case-sensitive literal
substrings of the cue text (including any markup):

```bash
mediawrench cutlist examples/cutlist/interview.srt --query 'Keep this answer.'
```

The selection plan `examples/cutlist/selection.json`:

```json
{
  "keep": [{"from": 1, "to": 4}, 6],
  "drop": [3],
  "padding": 0.1,
  "duration": 10,
  "labels": {"1": "开场", "2": "Answer, first", "4": "Answer, second", "6": "再见"}
}
```

Review the timeline and absolute output paths without writing anything, then export
into an existing directory:

```bash
mediawrench cutlist examples/cutlist/interview.srt --plan examples/cutlist/selection.json \
  --media-file interview.wav --output-prefix out/interview-proj --dry-run

mkdir -p out
mediawrench cutlist examples/cutlist/interview.srt --plan examples/cutlist/selection.json \
  --media-file interview.wav --output-prefix out/interview-proj
```

This writes `interview-proj.llc`, `interview-proj.csv` and
`interview-proj.provenance.json` with cuts `[0, 3.5)`, `[4.5, 6.1)` and `[8.9, 10)`
seconds. The `.llc` has `version: 2`, `mediaFileName` and `cutSegments` with
`start`, `end`, `name`. The CSV has **no header**: `start,end,label` in seconds (three
decimals by default) with standard CSV quoting. `python examples/cutlist/demo.py`
runs the same export in a temporary directory and checks that the three formats
agree.

`--media-file` is a plain file name reference, not a path. Following LosslessCut's
convention, put the project beside the media file (`interview-proj.llc` next to
`interview.wav`) and open it in LosslessCut when you are ready to review. Media
need not exist unless you probe it.

## The selection plan

| Field | Required | Meaning |
| --- | --- | --- |
| `keep` | yes | Cue IDs (integers) or inclusive ranges `{"from": A, "to": B}`; every ID in a range must exist. Duplicates are deduplicated. |
| `duration` | yes, unless `--ffprobe` supplies it | Media duration in seconds (millisecond precision). |
| `drop` | no (default `[]`) | Cue IDs or ranges to exclude; exclusions win, even inside another kept cue. |
| `padding` | no (default `0`) | Seconds added around each kept cue, clamped to `[0, duration]`. |
| `labels` | no (default `{}`) | Selected ID string -> nonempty name (no control characters). |
| `frameRate` | no | Opt-in exact frame grid, as an integer or rational **string** (see below). |

Strings like `"1-4"`, expressions, unknown fields or IDs, duplicate JSON keys and
non-finite numbers fail explicitly.

### How cuts are computed

1. Unique positive SRT IDs are kept independently of file order. IDs are stable for
   the same SRT; an edited or renumbered SRT needs plan review. Provenance records
   the SRT and plan SHA-256.
2. Duration must be positive and every cue, selected or not, must fit
   `[0, duration]`; `0 <= padding <= duration`. Unsafe large values or times that
   would round in seconds JSON fail.
3. Drop IDs are removed from the keep seeds (a cue both kept and dropped leaves no
   padding ghost); effective keeps are padded and clamped.
4. Overlapping or exactly adjacent padded keeps are unioned, then the union of the
   **original unpadded** drop intervals is subtracted. Unselected cues are not
   implicit exclusions, so padding can include them.
5. The result is ascending, positive-length, disjoint half-open `[start, end)`
   intervals. An empty result fails. Padding-only fragments left by subtraction are
   kept and marked as having no source overlap.

Provenance (schema 1 in millisecond mode) records the original plan, expanded IDs,
effective keeps, every source cue's text/timing/ordinal and, per cut, the
contributing IDs with padded interval, retained fragment and original-text overlap
(`null` for padding alone). `overlappingCueIds` lists every original cue
intersecting the cut, selected or not. `*Ms` fields are integer milliseconds;
`start`/`end` are the seconds used in the `.llc` and CSV. Default names are
`cues 1,2`; labels are joined in ascending contributor order and may contain
Unicode, commas and quotes.

### Output safety

Outputs are never overwritten: existing files, dangling symlinks, collisions with
inputs or media, and missing output directories fail in both export and dry-run, so
choose a new prefix for each reviewed version. Files are staged, fsynced and
published with same-directory no-clobber hard links (the filesystem must support
hard links); ordinary failures roll back this invocation's files. Publication is
atomic **per file**, not a three-file transaction.

## Exact frame quantization (opt-in)

Add `"frameRate": "24"` or `"frameRate": "30000/1001"` to the plan. Use an integer or
rational string, never a rounded decimal such as `23.976`. The zero-origin
constant-rate grid has time base `1 / frameRate`; frames are numbered from zero and
interval ends are exclusive boundary indices (not SMPTE or drop-frame timecode).
Components are limited to 1,000,000,000 and the rate to 1,000,000 fps. There is no
rate inference or VFR model.

```bash
mediawrench cutlist examples/cutlist/frames.srt --plan examples/cutlist/frames-24.json \
  --media-file clip.mov --output-prefix out/frame24-proj --dry-run
mediawrench cutlist examples/cutlist/frames.srt --plan examples/cutlist/frames-ntsc.json \
  --media-file clip.mov --output-prefix out/frame-ntsc-proj --dry-run
```

`frames.srt` has cue 1 at `[100, 2400)` ms; both plans declare 2.5 s and no padding:

| Rate | Frame interval | Exact seconds | Start / end error, seconds |
| --- | --- | --- | --- |
| `24` | `[2,58)` | `[1/12, 29/12)` | `-1/60`, `+1/60` |
| `30000/1001` | `[3,72)` | `[1001/10000, 3003/1250)` | `+1/10000`, `+3/1250` |

Quantization starts from the padded effective keeps and unpadded drops:

1. Keep endpoints round to the nearest frame, exact half-frame ties **later**.
2. Exclusions expand outwards (floor start, ceil end), so no retained frame touches
   an original exclusion. The media bound is floored to a full frame and all
   endpoints are clamped to it; zero-length keeps are discarded.
3. Quantized keeps and exclusions are merged separately, then exclusions are
   subtracted again. A short keep may disappear.

Each adjusted endpoint moves **strictly less than one frame**. Frame-mode provenance
uses **schema 2**: `quantization` records the rational rate, rounding policies,
error bounds, media frame floor, discarded keep IDs and a `boundaries` record per
endpoint (original ms, final frame, exact seconds, direction, signed error, clamp
flag); `cutBoundaries` links final cut frames to them. Each cut has `startFrame`,
`endFrame`, `startExactSeconds`, `endExactSeconds` as `{numerator, denominator}`.
The `.llc` and CSV carry a round-trippable numeric seconds value within **1 ns** that
recovers the same frame; export fails if that precision cannot be met. Invalid
`"23.976"`, a duration without a full frame, or a selection that vanishes on the
grid fails with exit **2** and no outputs.

This is a mathematical constant-rate grid, not verification of media timestamps,
decoded frames, keyframes or lossless cutting.

## Duration from a local ffprobe (opt-in)

Pass `--ffprobe PATH` to read the duration of `--media-file`, which is looked up
**beside the output prefix**. The path is explicit: the command never searches
`PATH`, installs binaries, invokes a shell or cuts media. Media and executable must be
regular files; only the `file` protocol is enabled. `--dry-run` also runs the
read-only probe. With a probe, the plan may omit `duration`; if it declares one, it
must equal the probed bound.

```bash
mediawrench cutlist cues.srt --plan plan.json --media-file short.wav \
  --output-prefix work/cuts --ffprobe "$(command -v ffprobe)"
```

ffprobe's JSON `format.duration` is authoritative (stream durations are never used)
and is **floored** to whole milliseconds, so 2.5005 s gives a 2500 ms bound. All cues
must fit it and padding clamps to it. Missing, `N/A` or invalid durations, sub-1 ms
durations, missing media, a non-executable or failing probe and the 15-second
timeout all fail with exit **2** and no outputs; there is no fallback to a declared
duration.

The CLI report and provenance gain a `durationProbe` record: absolute argv,
executable version and SHA-256, media path, size and SHA-256, raw JSON stdout and its
SHA-256, stderr, exit code, raw seconds string and the millisecond bound. It contains
absolute local paths: review it before sharing.

### Check the frame rate of a selected video stream

Add `--verify-frame-rate-stream INDEX` (requires `--ffprobe` and a plan
`frameRate`). INDEX is the **absolute ffprobe stream index**, not a video ordinal.
Both `avg_frame_rate` and `r_frame_rate` must equal the declared rate exactly as
fractions (`48/2` equals `24/1`); there is no decimal tolerance or NTSC alias.
A missing, non-video or nonexistent stream, unusable rates (including `0/0`) or a
mismatch fail with exit **2** and no outputs; no other stream is substituted.
`durationProbe` then uses **schema 2** and adds:

```json
{"streamIndex": 0, "declaredFrameRate": "24", "avgFrameRate": "24/1",
 "rFrameRate": "24/1", "policy": "exact-avg-and-r-frame-rate"}
```

This checks **metadata agreement**, not whether the source really is CFR.

### Check a zero origin

Add `--verify-zero-origin` together with `--ffprobe` and
`--verify-frame-rate-stream`. The selected stream's `start_time` must be a known
plain decimal equal to exactly zero (`-0.000` counts); any nonzero, missing or
malformed value fails with exit **2**. `durationProbe` uses **schema 3** and adds
`{"streamIndex": 0, "startTimeSeconds": "0.000000", "policy":
"exact-zero-stream-start-time"}`. This is demuxer metadata, not proof of the first
decoded frame's timestamp; a nonzero source is never shifted onto the grid.

The test fixtures in [`tests/cutlist/fixtures/`](../tests/cutlist/fixtures/README.md)
show the contract. `origin-zero.mp4` exports frames `[2,58)` while `origin-one.mp4`
(start time 1 s) is rejected. In `origin-multistream.mp4`, index 0 is audio, index 1
video with a 1 s origin and index 2 video with a zero origin, so only
`--verify-frame-rate-stream 2 --verify-zero-origin` succeeds. In
`rates-multistream.mp4`, video 1 is `24/1` and video 2 is `30000/1001`; crossing
them fails with `Video frame rate mismatch for stream INDEX`, and `2997/100` never
matches `30000/1001`.

```bash
mkdir -p work && cp tests/cutlist/fixtures/origin-zero.mp4 work/
mediawrench cutlist examples/cutlist/frames.srt --plan examples/cutlist/frames-24.json \
  --media-file origin-zero.mp4 --output-prefix work/cuts \
  --ffprobe "$(command -v ffprobe)" --verify-frame-rate-stream 0 --verify-zero-origin
mediawrench cutlist verify-probe-evidence work/cuts.provenance.json --require-zero-origin
```

## Verify saved probe evidence: `verify-probe-evidence`

```bash
mediawrench cutlist verify-probe-evidence work/cuts.provenance.json
mediawrench cutlist verify-probe-evidence work/cuts.provenance.json \
  --srt examples/cutlist/frames.srt --plan examples/cutlist/frames-24.json
```

Input is a saved provenance document (schema 1 or 2) with `durationProbe` schema 1,
2 or 3. Documents without probe evidence, bare probe objects and dry-run reports are
unsupported. The command writes nothing and uses the recorded absolute media and
executable paths (moving them requires new evidence). Checks run in this order:

1. Validate the document: no duplicate or non-finite JSON, supported schemas and
   field types, absolute recorded paths, and an argv identical to the recorder's
   exact file-only query (no extra options, shell, `PATH` lookup or path
   overrides).
2. Check the recorded stdout hash and that duration, version, rate, grid and origin
   summaries agree with it and with the saved timeline. Compare any caller
   expectations (below).
3. Optionally hash the current SRT and plan; then check the current media size and
   SHA-256 and the executable's SHA-256, regular-file status and execute permission.
4. Replay the **saved argv** (stdin disabled, 15-second timeout) and require the same
   exit status, raw stdout hash, stderr, version, duration and stream evidence. Even
   a change within the same millisecond or different JSON formatting is stale.

| Exit | Output | Meaning |
| --- | --- | --- |
| **0** | JSON `status: "verified"`, `stale: false`, `replayed: true`, `issues: []` | Recorded inputs and replay match. |
| **0** | JSON `status: "preflight-only"`, `scope: "saved-evidence-and-current-files"`, `replayed: false` | With `--preflight-only`: everything except the replay passed. |
| **1** | JSON `status: "stale"`, `stale: true`, `issues` with `code`, `message` and expected/actual details | Evidence is inconsistent, inputs changed or disappeared, a caller expectation differs, or the replay differs or fails. Regenerate and review. |
| **2** | `mediawrench cutlist: ...` on stderr, no JSON | Unreadable, malformed or unsupported provenance, invalid options or unsafe argv; nothing executed. |

Issue codes include `srt-sha256`, `plan-sha256`, `srt-unavailable`,
`plan-unavailable`, `media-sha256`, `media-size`, `ffprobe-sha256`,
`recorded-frame-rate`, `recorded-zero-origin`, `replay-duration`,
`replay-frame-rate`, `replay-zero-origin`, `replay-failed`, `replay-invalid`,
`replay-timeout` and the `expected-*` codes below. `replayed` means a replay was
attempted.

Options (all optional; checked before any current file is hashed or anything runs):

| Option | Effect |
| --- | --- |
| `--srt PATH --plan PATH` | Must be given together. Compare current file bytes with the saved `source.srtSha256`/`source.planSha256`; the plan is never reapplied. |
| `--require-zero-origin` | Require schema-3 zero-origin evidence; schemas 1 and 2 fail with exit **2**. |
| `--expect-video-stream INDEX` | The saved selected absolute stream must equal INDEX (schema 2/3); otherwise stale `expected-video-stream` without replay. |
| `--expect-frame-rate RATE` | The saved declared rate must equal RATE as an exact fraction (`60000/2002` matches `30000/1001`); otherwise stale `expected-frame-rate`. |
| `--expect-media-sha256 DIGEST` | The saved `mediaSha256` must equal DIGEST (64 lowercase hex); otherwise stale `expected-media-sha256`. |
| `--expect-ffprobe-sha256 DIGEST` | The saved `ffprobeSha256` must equal DIGEST; otherwise stale `expected-ffprobe-sha256`. A replaced binary is never executed. |
| `--preflight-only` | Run every check except executing ffprobe. |

The report also contains `command`, report `schemaVersion: 1`, `provenancePath`, the
saved `durationMs` and, after a successful replay, `replayDurationMs`. Verification
is a point-in-time consistency check: it does not authenticate the provenance or the
executable, validate edited cuts, or gate opening a project in LosslessCut.

## Compare edited companions: `verify-companions`

```bash
mediawrench cutlist verify-companions out/interview-proj.provenance.json \
  --llc out/interview-proj.llc --csv out/interview-proj.csv
```

Reads a provenance document (schema 1 or 2) and one explicit `.llc` and `.csv` (both
required, existing regular files) and compares both with the saved authoritative
cuts. It writes nothing, executes nothing and never reapplies a selection.

- **Saved authority**: schema 1 uses integer `cuts[].startMs`/`endMs`; schema 2 uses
  the exact rational rate with `startFrame`/`endFrame` and
  `startExactSeconds`/`endExactSeconds`. Cut IDs must be `1..n` in order; cuts must be
  nonempty, ordered, disjoint and within the duration.
- **LLC**: `version` exactly `2`, a plain `mediaFileName`, and `cutSegments` objects
  with numeric `start`, `end` and string `name`.
- **CSV**: UTF-8 (optional BOM), LF or CRLF, no header or blank rows; each row is
  `start,end,name`, names may be quoted and contain commas, doubled quotes or line
  breaks.
- **Precision**: schema 1 compares numerically with the millisecond boundary (`0.1`
  matches 100 ms, `0.100000000000000001` does not); schema 2 allows `1/10^9` s and
  requires the value to round to the saved frame.
- **Matching**: the LLC `mediaFileName` must equal the saved reference (the CSV has
  none, reported as `not-represented`); cut count, order, start, end and name must
  match.

Exit **0** prints `"status": "matched"`; exit **1** prints `"status": "mismatch"` with
one issue per difference (`media-file-name`, `cut-count`, `cut-start`, `cut-end`,
`cut-name`, each with `file`, `path`, `field`, `expected`, `actual` and a one-based
`cutIndex` for cut issues); exit **2** means malformed or unsupported input (invalid
JSON/CSV/UTF-8, duplicate keys, non-finite numbers, unknown versions, unreadable
paths) with a stderr message and empty stdout. For example, changing the first end
to `0.901` where 0.9 was saved reports:

```json
{"code": "cut-end", "file": "llc", "field": "end",
 "expected": {"numerator": 9, "denominator": 10}, "actual": "0.901", "cutIndex": 1}
```

This is not authentication: a consistently edited provenance, LLC and CSV report
`matched`.

## Exit codes (export and query)

Successful exports, dry-runs and queries return **0**, including searches with no
matches. Export errors return **2** with a `mediawrench cutlist: ...` message on
stderr. `--query` cannot be combined with export options.

## Python API

```python
from mediawrench.cutlist import (build_probed_timeline, build_timeline, load_plan,
                                 quantize_timeline, read_srt, render_outputs,
                                 verify_companions, verify_probe_evidence)

timeline = build_timeline(read_srt("examples/cutlist/interview.srt"),
                          load_plan("examples/cutlist/selection.json"))
documents = render_outputs(timeline, "interview.wav")  # no writes

framed = quantize_timeline(build_timeline(read_srt("examples/cutlist/frames.srt"),
                                          {"keep": [1], "duration": 2.5}), "30000/1001")

probed = build_probed_timeline(read_srt("cues.srt"), {"keep": [1], "padding": 0.2},
                               media_path="short.wav", ffprobe_path="/usr/bin/ffprobe",
                               video_stream_index=None, verify_zero_origin=False)

report = verify_probe_evidence("cuts.provenance.json", srt_path="cues.srt", plan_path="plan.json")
report = verify_companions("cuts.provenance.json", llc_path="cuts.llc", csv_path="cuts.csv")
```

`FrameCut.start_frame`/`end_frame` are integers and `start_ms`/`end_ms` exact
`Fraction`s. Re-quantizing with an equivalent rate is idempotent; changing the rate
of a quantized timeline fails (start again from the millisecond timeline).
`verify_probe_evidence` also accepts `require_zero_origin`,
`expected_video_stream_index`, `expected_frame_rate`, `expected_media_sha256`,
`expected_ffprobe_sha256` and `preflight_only`, validated strictly before any file
access. Malformed input raises `CutlistError`; file access may raise `OSError`.

## Benchmark

[`benchmarks/cutlist/REPORT.md`](../benchmarks/cutlist/REPORT.md) measures a
5,000-cue export and compares the sweep-based provenance lookup with a nested-loop
baseline (`python benchmarks/cutlist/experiment.py`).

## Relationship to other tools and limitations

[pysubs2](https://github.com/tkarabela/pysubs2) (MIT) parses SRT.
[LosslessCut](https://github.com/mifi/lossless-cut) already imports SRT/CSV and
edits timelines, and [autocut](https://github.com/mli/autocut) offers text-based
editing; this command adds only explicit cue selection, interval algebra and cut
provenance, using the public `.llc` v2 and CSV formats. No implementation from
those projects is copied.

- Supported SRT is strict UTF-8 (optional BOM), LF/CRLF, canonical unique positive
  numeric IDs, `HH:MM:SS,mmm --> HH:MM:SS,mmm` with two-digit hours and nonempty
  text. Unsupported blocks, empty cues, coordinate settings, noncanonical IDs and
  embedded timing arrows are rejected rather than skipped. Ranges refer to IDs, not
  positions; markup is preserved, not rendered.
- No inference, transcription, word alignment, shell expressions, cutting or
  lossless guarantee. Keyframe adjustments may still be needed in the editor.
- Interoperability is validated against the documented formats and real file
  round trips, not a launched LosslessCut GUI.
- Keep inputs unchanged while generating; concurrent modification of sources or the
  probe executable is not supported.
