# mediawrench cutsync

When you cut a video in LosslessCut (or any editor that exports its kept ranges), the
external `.srt` that belonged to the original video no longer lines up: removed
passages leave their cues behind and everything after the first cut drifts.
`mediawrench cutsync` applies the **same explicit keep intervals** you cut the video
with to that sidecar SRT. It writes a new SRT plus a JSON change list that accounts for
every original cue, including deleted cues, clipped fragments, output cue numbers and
time offsets. No video is opened, re-encoded, uploaded or analyzed.

**cutsync or cutlist?** [`mediawrench cutlist`](cutlist.md) works *before* cutting: it
turns a selection of transcript cues into a cut list (`.llc`, CSV, provenance).
`cutsync` works *after* cutting: it takes the keep list you actually cut with
(LosslessCut CSV, `.llc` or JSON) and carries the original sidecar SRT onto the edited
timeline. A `.llc` or CSV written by `cutlist` in its default millisecond mode can be
passed to `cutsync --keep` unchanged; frame-quantized cutlist exports (plan
`frameRate`) are accepted only when every boundary has at most nine decimal places
(for example at 25 fps), so 24 or `30000/1001` fps grids are usually rejected with an
error rather than rounded. If you adjusted the cuts in LosslessCut, feed the saved or
exported final keep list, not the original cutlist output.

```bash
pip install mediawrench
mediawrench cutsync --help
```

It runs offline and calls no external program; FFmpeg is not needed.

## Quick start

The examples are synthetic text files in [`examples/cutsync/`](../examples/cutsync/).
Run from a clone of the repository.

```bash
mkdir -p out
mediawrench cutsync examples/cutsync/interview.srt --keep examples/cutsync/keep.csv \
  -o out/interview.srt
```

This writes `out/interview.srt` and the change list `out/interview.changes.json`
(override with `--report`). To review before writing anything, print the complete
change list instead:

```bash
mediawrench cutsync examples/cutsync/interview.srt --keep examples/cutsync/keep.llc \
  -o out/interview.srt --dry-run > out/preview.json
```

`keep.csv`, `keep.json` and `keep.llc` all keep source `[0,2)`, `[4,6)` and `[8,10)`
seconds, joining them into a six-second output. The committed
[edited SRT](../examples/cutsync/edited.srt) and
[full change list](../examples/cutsync/edited.changes.json) show the default `split`
policy on these inputs, including original-to-output cue links:

| Original cue | Source seconds | Output seconds | Action |
| --- | --- | --- | --- |
| 10 | 0.5–2.5 | 0.5–2 | trimmed |
| 20 | 2.8–3.8 | — | dropped |
| 30 | 4–5.5 | 2–3.5 | shifted |
| 40 | 5.8–8.2 | 3.8–4 and 4–4.2 | split; full text in both fragments |
| 50 | 9–10 | 5–6 | shifted |

A LosslessCut frame-number CSV needs its format and frame rate named explicitly:

```bash
mediawrench cutsync examples/cutsync/interview.srt --keep examples/cutsync/keep-frames.csv \
  --keep-format csv-frames --fps 25 -o out/from-frames.srt
```

## Command line

```text
mediawrench cutsync INPUT --keep KEEP -o OUTPUT [options]
```

| Option | Meaning |
| --- | --- |
| `INPUT` | Original SRT file. |
| `--keep KEEP` | Keep ranges: `.csv`, `.json` or `.llc` (required). |
| `-o, --output OUTPUT` | New SRT file (required). |
| `--report REPORT` | Change-list JSON (default: `OUTPUT` with suffix `.changes.json`). |
| `--keep-format {csv,csv-frames,json,llc}` | Override the format inferred from the `--keep` extension. `csv-frames` must be chosen explicitly. |
| `--fps FPS` | Frame CSV only: source frame rate, e.g. `25` or `30000/1001`. |
| `--boundary {split,drop,longest}` | Policy for a cue that crosses a cut (default `split`; see below). |
| `--duration DURATION` | Source media duration, seconds or `HH:MM:SS.mmm`; needed for open keep ends and checks every end. |
| `--selected-only` | `.llc` only: omit segments explicitly marked `selected:false`. |
| `--min-duration-ms N` | Discard output fragments shorter than N milliseconds (default `1`). |
| `--input-encoding ENC` | Encoding of the original SRT (default `utf-8-sig`). |
| `--short-fractions {reject,decimal,milliseconds}` | How to read one- or two-digit fractions such as `,95` (default `reject`). |
| `--parse-timeout SECONDS` | Time budget for each parser, greater than 0 and at most 60 (default `10`). |
| `--force` | Replace existing output/report files (never the inputs). |
| `--dry-run` | Print the complete change list JSON to stdout; write no files. |
| `--version` | Print `mediawrench cutsync <version>`. |

The output is UTF-8 with LF line endings. Existing files are refused unless `--force`
is given. Input files cannot be overwritten, even with `--force`, including
symlink/hard-link aliases. A one-line summary goes to stderr.

| Exit | Meaning |
| --- | --- |
| **0** | Success (or a successful `--dry-run`). |
| **2** | Invalid input, invalid options or filesystem failure; `mediawrench cutsync: error: ...` on stderr and nothing published. |
| **130** | Interrupted. |

The keep list and the SRT are parsed in disposable local worker processes, one at a
time, each with a ten-second budget to contain pathological malformed input (the
`srt` parser's regular expression can backtrack badly on some invalid text). Raise it
with `--parse-timeout 30` (maximum 60 seconds per parser). Workers are reaped on
success, failure, timeout and interruption, before mapping begins.

## Boundary policy

All ranges are half-open `[start,end)`. A cue ending exactly where a kept range begins
contributes nothing to that range; a cue starting at its end is also excluded.
Touching keep intervals are coalesced because they remove no source time, so they do
not cause an artificial split. Overlapping subtitle cues remain overlapping. Output
cues are sorted chronologically and numbered from 1.

| `--boundary` | A cue that crosses a cut |
| --- | --- |
| `split` (default) | Intersect with each keep range; duplicate the **whole text** into every surviving fragment. |
| `drop` | Drop the entire cue unless it fits wholly inside one coalesced keep range. |
| `longest` | Keep only its longest intersection. Equal lengths choose the earlier source fragment. |

The command does not know which words were spoken on each side of a cut. Review
entries with `needs_review: true` in the report; split text is never shortened or
rewritten. `--min-duration-ms 80` can discard tiny fragments; the default keeps every
positive millisecond. This threshold is applied **after** the boundary policy selects
its fragments, and every discarded intersection has its own reason in the report.

## Keep formats

These are **KEEP ranges, never removal ranges**. Nothing is silently sorted,
inverted, unioned or inferred from the last subtitle: intervals must be in
chronological source order and must not overlap.

- **CSV**: LosslessCut's current `Start,End,Name` header with optional tag columns, or
  the historical headerless `start,end,label`. Quoted labels and LF/CRLF/CR are
  supported. Times are decimal **seconds**, or `HH:MM:SS.mmm` / `HH:MM:SS,mmm` (quote
  a field containing a comma).
- **CSV (frames)**: select `--keep-format csv-frames --fps 30000/1001`; frame numbers
  must be integers. FPS is required because the CSV does not identify its timebase. A
  decimal such as `29.97` is accepted, but use the source's exact rational rate when
  known.
- **JSON**: `{"keep": [[0,2], [4,6]]}` or
  `{"keep": [{"start":0,"end":2,"name":"Opening"}]}`; a root array is also accepted.
  JSON is strict: duplicate keys and unknown interval fields are rejected.
- **`.llc`**: LosslessCut's native JSON5 project, version 1 or 2. Its `cutSegments`
  are the keep ranges. `--selected-only` omits segments explicitly marked
  `selected:false`; segments without selection metadata are included. Otherwise all
  stored segments are used. See [`examples/cutsync/keep.llc`](../examples/cutsync/keep.llc).

An empty start in CSV, or an omitted start in a v1 `.llc`, means zero, matching
historical LosslessCut exports. An empty or omitted end requires the source duration,
for example `--duration 00:42:15.360`; when given, the duration also checks that every
keep end lies within the source. For JSON and `.llc` objects, omit an open end:
`null` and empty-string endpoints are rejected. An empty JSON keep array (`{"keep":[]}`)
intentionally removes all cues; an empty CSV is an error. The report's `input_rows`
identify data rows in the original keep list, also when unselected `.llc` rows are
omitted.

### Precision

Cut points and joined durations keep exact rational milliseconds. Only the final
subtitle timestamps are rounded to the nearest millisecond, halves up, so many short
frame cuts do not accumulate per-segment rounding drift. Report spans include
`start_exact_ms` / `end_exact_ms` when fractional, and each output fragment includes
`offset_exact_ms` as a rational string such as `1001/30` (milliseconds, not seconds).
`duration_exact_ms` records the exact joined duration; display timestamps and `*_ms`
integer fields are rounded.

Keep ranges that collapse when displayed in source milliseconds are rejected; cues
whose mapped intersection rounds to zero are discarded with reason `rounded_to_zero`.
Exact overlaps are rejected even if rounding would hide them. The supported time domain
is 0–99:59:59.999. Decimal seconds support up to nine significant fractional places
(trailing zeros do not count); magnitude and precision are checked before rational
numbers are built. Frame/FPS fractions stay exact.

## LosslessCut workflow

1. Finish the video cut points so they describe what will actually be retained.
   Export the keep segments as **CSV (seconds)**, or save the `.llc`. Frame CSV also
   works with `--keep-format csv-frames --fps ...`.
2. Run `mediawrench cutsync original.srt --keep edit.csv -o edited.srt` with the
   matching original sidecar SRT and that keep list.
3. Review the report entries marked `needs_review`, then check the new subtitle against
   the exported video's beginning, joins and ending in your player.

Project files do not encode every export setting. Global invert mode, export
selection, segment reordering, keyframe snapping, Smart Cut behavior and muxer offsets
may change the actual video timeline. `cutsync` maps exactly the supplied intervals;
it cannot verify the resulting video's timestamps. Supply the final adjusted
intervals, not merely the desired cut points, for exact synchronization.

## The change list (report)

Schema version 1 (`schema_version: 1`) contains `segments`, `summary` and one `cues`
entry per input cue. `source_position` is always unique, even if the original SRT
numbering is missing or repeated. Each retained fragment links to its final
`output_index`, source span, output span and offset. Each cue has an `action`
(`unchanged`, `shifted`, `trimmed`, `split` or `dropped`) and a `reason`; dropped cues
keep their source text and say why (`outside_keep_intervals`, `crosses_boundary`,
`below_min_duration` or `rounded_to_zero`), and every discarded fragment is listed in
`discarded_fragments` (`boundary_policy`, `below_min_duration` or `rounded_to_zero`).
SHA-256 hashes bind the report to the
original SRT, the keep file and the output SRT bytes; only file names are recorded,
never absolute paths. The `tool` record keeps the original tool name,
`{"name": "srt-cut-sync", "version": ..., "srt_version": ...}`, so reports written
before and after the move to mediawrench stay comparable.

## Encodings, limits and recovery

Only SRT sidecars and a single source at normal speed are supported. There is no
automatic audio alignment, speech recognition, word timing, FPS conversion, video
rendering, CMX EDL import or multi-file timeline. Convert other subtitle formats to
SRT first (for example with [pysubs2](https://github.com/tkarabela/pysubs2)), within
that converter's format-specific preservation limits.

- **Encodings**: the SRT default is `utf-8-sig` (UTF-8 with optional BOM); use
  `--input-encoding utf-16` or another Python codec name otherwise. Keep lists are
  UTF-8 with optional BOM. Decoding is strict. A leading BOM is removed and LF/CRLF/CR
  newlines are normalized; this preprocessing is recorded in the report's input
  metadata.
- **SRT parsing** uses the `srt` library's tolerant grammar; invalid intervals and
  empty cue text fail before any output. `srt` preserves HTML/ASS text and proprietary
  timing-line metadata; output numbering and whitespace follow canonical SRT. Its
  `make_legal_content` removes illegal blank lines and outer blank whitespace; the
  affected source text is kept in the report and `text_normalized` is set for review.
- **Short fractions**: `,95` means 95 ms to `srt` and FFmpeg but 950 ms to pysubs2, so
  one- or two-digit fractions are rejected by default. Choose
  `--short-fractions decimal` (950 ms) or `--short-fractions milliseconds` (95 ms)
  explicitly; every such field is recorded in `timestamp_normalizations` and marked for
  review. More than three fractional digits, and minute/second fields of 60 or more,
  are rejected.
- **Limits**: 8 MiB per input file, 100,000 cues, 20,000 keep ranges and 100,000
  cue/keep-range intersections (discarded fragments included). A conservative
  **32 MiB expanded-SRT budget** also counts repeated full text and timing metadata
  before serialization, so a single long cue spanning many cuts cannot allocate an
  enormous output; split long cues or use `--boundary longest` / `drop` for such a
  job. The report records the budget and its estimate.
- **Publication**: both files are staged and flushed before publication. Ordinary
  failures roll back published files, and `--force` keeps temporary backups until
  both writes succeed. Two separate files cannot be published atomically across power
  loss or a killed process: if publication was interrupted externally, inspect hidden
  `.cut-sync-*.tmp` / `.bak` files in the output directory. The report's output hash
  detects a mismatched SRT/report pair. Cleanup errors keep the original failure and
  name leftover temporary files; if publication succeeded but cleanup failed, the
  error says the outputs were published. No-overwrite publication and overwrite
  backups need a filesystem with hard-link support (otherwise the command fails
  without replacing anything).

## Python API

```python
import srt
from mediawrench.cutsync import Interval, make_timeline, parse_subtitles, remap

with open("examples/cutsync/interview.srt", encoding="utf-8") as source:
    original, timestamp_changes = parse_subtitles(source.read())
timeline = make_timeline([Interval(0, 2000), Interval(4000, 6000)])  # integer ms
output, audit = remap(original, timeline, boundary="split")
print(srt.compose(output, reindex=False))
```

Input cue objects are not mutated. `mediawrench.cutsync.cuts.load_keep(text,
format="csv")` reads keep lists. Invalid input raises `CutSyncError`. The library's
`parse_subtitles` runs in the caller's process: callers handling untrusted text should
apply their own process/time budget (the CLI does this automatically).

## Related tools

`cutsync` is a thin integration around [srt](https://github.com/cdown/srt) (SRT
parsing and serialization) and [json5](https://github.com/dpranke/pyjson5) (LosslessCut
projects); the only new algorithm is the piecewise time mapping and its audit.

- [LosslessCut](https://github.com/mifi/lossless-cut) writes the keep lists this
  command reads (CSV with `Start,End,Name` plus tag columns; `.llc` JSON5 projects,
  versions 1 and 2). Exporting an external SRT through a cut is requested in its
  [issue 3209](https://github.com/mifi/lossless-cut/issues/3209).
- [pysubs2](https://github.com/tkarabela/pysubs2) converts and shifts many subtitle
  formats but has no keep-list concatenation with boundary policies and a per-cue
  audit.
- [srt-cut](https://github.com/overlookmotel/srt-cut) (Node) splits an SRT into parts at
  start points and assigns cues by start time only; it does not clip at boundaries or
  concatenate kept ranges.
- FFmpeg's `-ss`/`-to` trimming of a subtitle stream keeps cues extending beyond the
  requested range, so a plain stream trim is not a cue-intersection policy.
- [Auto-Editor](https://github.com/WyattBlue/auto-editor) already intersects subtitle
  packets with clip windows as part of its full video editing, and
  [yt-dlp](https://github.com/yt-dlp/yt-dlp) processes subtitle sidecars when it
  removes chapters. Use them when they already drive your edit; `cutsync` covers the
  small offline LosslessCut/SRT step with a cue-level audit and an explicit
  `split`/`drop`/`longest` choice, without rendering video.

Known upstream behaviors handled here: `srt`'s regular expression can take seconds on
short malformed inputs such as `"1" * 1000 + " x"` (hence the timed parser workers),
and `json5` leaves escaped UTF-16 surrogate pairs uncombined in `.llc` labels (they are
joined with Python's UTF-16 codec, and unpaired surrogates are rejected before
publication).

The tests use LosslessCut's own CSV/SRT parser fixtures and a real FFmpeg SRT export;
see [`tests/cutsync/fixtures/PROVENANCE.md`](../tests/cutsync/fixtures/PROVENANCE.md).
