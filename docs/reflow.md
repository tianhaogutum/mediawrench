# mediawrench reflow

ASR tools such as Whisper, faster-whisper and WhisperX often emit paragraph-sized
segments that are far too long to read as subtitles, even though they already
contain word timestamps. `mediawrench reflow` splits those **existing word
timestamps** into readable cues using line width, duration, speaking rate, pauses
and punctuation, and writes SRT plus a JSON report.

Use it when you already have a transcript JSON with word times and need subtitles
that respect reading limits. The report keeps the original word strings, source
IDs, text spans, timing repairs, costs and every constraint that could not be met.
It never loads an ASR or alignment model, runs offline and never drops text to
satisfy a bound. FFmpeg is not needed.

## Quick start

```bash
pip install mediawrench

mkdir -p out
mediawrench reflow examples/reflow/pause.json -o out/pause.srt --report out/pause.report.json
mediawrench reflow examples/reflow/fallback.json -o out/fallback.srt --report out/fallback.report.json
# Validate and print the full report without writing anything:
mediawrench reflow examples/reflow/pause.json --dry-run
```

The pause sample becomes `Hello there.` at 0–1.2 s, then `Next topic.` at 2–3 s.
Output directories must already exist. [`examples/reflow/demo.py`](../examples/reflow/demo.py)
runs the library on both samples, verifies the SRT round trip and writes the
results to `examples/reflow/output/`.

## Options

| Option | Meaning |
| --- | --- |
| `input` | UTF-8 Whisper / faster-whisper / WhisperX transcript JSON (not needed for profile inspection) |
| `-o`, `--output PATH` | SRT destination (required unless `--dry-run`; not allowed with `--compare-profiles`) |
| `--report PATH` | JSON report or comparison destination (required unless `--dry-run`) |
| `--max-chars N` | Width units per line (default `42`; at most two lines per cue) |
| `--width-mode codepoint\|display` | Count line width in code points (default) or grapheme display cells |
| `--target-duration S` | Preferred cue duration in seconds (default `3`) |
| `--max-duration S` | Maximum cue duration in seconds (default `6`) |
| `--max-cps N` | Maximum speaking rate (default `20`; cannot override `--profile`) |
| `--cps-mode codepoint\|grapheme` | Speaking-rate units; defaults to the profile's saved units, else `codepoint` |
| `--profile JSON` | Named reading profile (see below) |
| `--compare-profiles LEFT RIGHT` | Compare two named profiles on the same transcript; JSON only |
| `--inspect-profile JSON` | Validate one profile without a transcript |
| `--inspect-pair LEFT RIGHT` | Validate two distinct profiles without a transcript |
| `--pause S` | Internal pauses at least this long incur a cost (default `0.5`) |
| `--algorithm dp\|fixed` | Optimizer (default `dp`); `fixed` is the N-word benchmark baseline |
| `--group-words N` | N for the fixed baseline (default `3`) |
| `--dry-run` | Validate, run the SRT round trip and print the full report; write no files |
| `--strict` | Exit 3 after emitting results if the constraints are infeasible |
| `--version` | Print `mediawrench reflow <version>` and exit |

| Exit | Meaning |
| --- | --- |
| 0 | Results produced; reflow/comparison reports include `feasible`, `constraints` and `diagnostics`; inspection only validates configuration |
| 2 | Invalid or unsupported input, configuration, or I/O error |
| 3 | `--strict` and the constraints are infeasible; complete outputs and report are still written |

An impossible bound never truncates text to succeed; use `--strict` to make
infeasibility visible to a calling script. Missing word alignment is always
reported, independently of feasibility.

### Files and option rules

- Bad inputs leave outputs untouched. Files are staged and fsynced before
  replacement; each replacement is atomic, but the SRT/JSON pair is **not** a
  power-loss-safe two-file transaction. Existing destinations are replaced.
- Input, profile, SRT and report paths must be distinct, including through
  symlinks and hard links, also during dry runs.
- `--output`, `--report`, `--profile` and `--compare-profiles` must each be
  supplied **at most once** (counting abbreviations such as `--rep`, `--out`,
  `--prof`, `--comp`, equals forms and `-oPATH`). A repeat exits 2 with
  `... must be supplied exactly once` before any path check, read or write, so a
  later valid path can never hide an earlier invalid one.
- `--version` exits immediately when argparse reaches it: `--version --max-chars bad`
  prints the version, `--max-chars bad --version` fails, and after `--` the word
  `--version` is a positional path.
- Error order: argparse syntax/type/choice errors, inspection guards, repeated
  `--compare-profiles`, `--profile`, `--report`, then `--output`; then missing
  arguments, path aliases, `--max-cps` conflicts, profile validation, transcript
  reading, reflow and SRT validation, and finally output.

### Library

```python
from mediawrench.reflow import Config, reflow
from mediawrench.reflow.io import load_json, srt_text

report = reflow(load_json("examples/reflow/pause.json"), Config(max_chars=42))
srt = srt_text(report)  # real pysubs2 serialize + parse + content/time comparison
assert report["metrics"]["tokens_conserved"]
```

## Named reading profiles

`--profile PATH.json` applies a reusable, named combination of reading
constraints. Four fields are required (durations in seconds); `cps_mode` is optional:

```json
{"name": "patient", "max_cps": 12, "min_duration": 1, "min_gap": 0.12}
```

Save `"cps_mode": "codepoint"` or `"cps_mode": "grapheme"` to keep the profile's
speaking-rate units. `max_cps` counts displayed code points or extended grapheme
clusters, including normalized spaces, before line wrapping. Rate units are
independent of `--width-mode`.

| Profile units | CLI `--cps-mode` | Resolved units |
| --- | --- | --- |
| Omitted (legacy four-field profile) | Omitted | `codepoint` |
| Omitted | Explicit valid mode | CLI mode |
| Saved mode | Omitted or the same mode | Saved mode |
| Saved mode | Different mode | Exit 2 before writes |

Numeric bounds are never converted between units. `min_duration` bounds each
cue's display interval; `min_gap` bounds the silence between consecutive cues,
including across source paragraphs (not after the final cue). Zero disables
either minimum; positive minimums must be 0.001–3600 s. CPS must be positive and
at most 10000. Names are nonblank printable strings of at most 80 characters.
Duplicate or unknown fields, invalid values and `--profile` together with
`--max-cps` exit 2 before writing. Layout options (`--max-chars`,
`--target-duration`, `--max-duration`) stay independent of the profile.
Profiles are local files; there is no style-guide certification.

### Inspect profiles without a transcript

```bash
mediawrench reflow --inspect-profile examples/reflow/profiles/rate-grapheme.json
```

This prints deterministic JSON with the validated name and bounds and the
resolved `cps_mode` and `rate_unit` (for this sample: `max_cps` 3, `grapheme`,
`graphemes/second`). Inspect two profiles together:

```bash
mediawrench reflow --inspect-pair examples/reflow/profiles/rate-codepoint.json examples/reflow/profiles/rate-grapheme.json
```

Output (indented UTF-8 with a final newline and empty stderr):

```json
{
  "format_version": 1,
  "tool": "wordcue-reflow",
  "mode": "profile_pair_inspection",
  "profiles": {
    "left": {
      "name": "rate-codepoint",
      "max_cps": 3,
      "min_duration": 0,
      "min_gap": 0,
      "cps_mode": "codepoint",
      "rate_unit": "codepoints/second"
    },
    "right": {
      "name": "rate-grapheme",
      "max_cps": 3,
      "min_duration": 0,
      "min_gap": 0,
      "cps_mode": "grapheme",
      "rate_unit": "graphemes/second"
    }
  }
}
```

Each side has the same fields as single-profile inspection, in argument order.
Names must differ by exact string comparison (no trimming, case folding or
normalization); the same file twice fails. The precedence table applies to each
side independently. Profiles are validated **left before right**, stopping at the
first failure, and errors name the argument side:

```text
mediawrench reflow: error: right profile: max_cps must be finite and positive
```

Only `--cps-mode` (at most once) and `--help` may accompany `--inspect-profile`
or `--inspect-pair`; any transcript, output, profile, comparison, layout,
algorithm, `--dry-run` or `--strict` option exits 2 before profiles are read.
Inspection writes nothing and runs no reflow. Valid bounds do not guarantee
feasibility for a particular transcript. (`--inspect-prof` still selects
single-profile inspection.)

### Profiles in action

With `--target-duration 0.5`, the synthetic word times in
[`reading-profile.json`](../examples/reflow/reading-profile.json) produce:

| Profile | CPS / minimum duration / gap | Cues (seconds) |
| --- | --- | --- |
| [brief](../examples/reflow/profiles/brief.json) | 20 / 0.3 / 0 | `One. Two.` (0–0.8), `Three.` (1–1.4) |
| [patient](../examples/reflow/profiles/patient.json) | 12 / 1 / 0.12 | `One. Two. Three.` (0–1.4) |

```bash
mediawrench reflow examples/reflow/reading-profile.json \
  --profile examples/reflow/profiles/brief.json --target-duration 0.5 --dry-run
mediawrench reflow examples/reflow/reading-profile.json \
  --profile examples/reflow/profiles/patient.json --target-duration 0.5 --dry-run
mediawrench reflow examples/reflow/profile-conflict.json \
  --profile examples/reflow/profiles/patient.json --strict \
  -o out/conflict.srt --report out/conflict.report.json
```

The last command exits **3** with complete outputs: the separate paragraphs
`One.` (0–0.4) and `Two.` (0.4–0.8) stay separate; each is 600 ms short of the
minimum duration and their zero gap is 120 ms short. Every `minimum_duration` /
`minimum_gap` violation includes the actual and required milliseconds,
`shortfall_ms`, a reason and cue/source IDs (gaps also give `next_unit_id`).
Speaking-rate violations give the actual and maximum CPS. The optimizer may group
words **within** a segment to remove a boundary, but it never moves a cue into
another paragraph or drops text. Incompatible minimum and maximum durations are
reported as infeasible rather than silently relaxed.

The report's `config` keeps the profile name, resolved bounds and `cps_mode`.
Profiles never retime or extend cues: cue `timing` records the first and last
effective source times with zero deviations. Normalization repairs stay separate
`timing_repair` diagnostics with source IDs, `from_ms`, `to_ms`, `delta_ms` and
`duration_delta_ms`.

For the Unicode sample [`grapheme-rate.json`](../examples/reflow/grapheme-rate.json)
(`é 👩🏽‍💻` at 0–1 s), [rate-codepoint](../examples/reflow/profiles/rate-codepoint.json)
reports **7 codepoints/second** and [rate-grapheme](../examples/reflow/profiles/rate-grapheme.json)
**3 graphemes/second**. Both bounds are 3, so strict mode exits 3 for the former
and 0 for the latter:

```bash
mediawrench reflow examples/reflow/grapheme-rate.json \
  --profile examples/reflow/profiles/rate-grapheme.json --width-mode display --strict --dry-run
mediawrench reflow examples/reflow/grapheme-rate.json \
  --compare-profiles examples/reflow/profiles/rate-codepoint.json examples/reflow/profiles/rate-grapheme.json \
  --strict --dry-run
# comparison exits 3: only the codepoint profile violates the rate bound
```

In the library, `Config.from_profile(profile, target_duration=0.5)` applies the
same rules and rejects overrides of the profile's bounds. Without a profile both
minimums default to zero; `Config(min_duration=1, min_gap=0.12)` sets them directly.

## Compare two profiles

`--compare-profiles LEFT.json RIGHT.json` reflows the same transcript with two
named profiles and reports the partitions and constraint trade-offs in one JSON
document. Both sides share the input, layout and algorithm options, must have
distinct names and resolve their own rate units; a shared explicit `--cps-mode`
must agree with every profile that saves units.

```bash
mediawrench reflow examples/reflow/reading-profile.json \
  --compare-profiles examples/reflow/profiles/brief.json examples/reflow/profiles/patient.json \
  --target-duration 0.5 --report out/comparison.json
mediawrench reflow examples/reflow/profile-conflict.json \
  --compare-profiles examples/reflow/profiles/brief.json examples/reflow/profiles/patient.json \
  --strict --dry-run
# exits 3: patient has two 600 ms duration shortfalls and one 120 ms gap shortfall
```

Comparison writes only JSON: `--report` is required unless `--dry-run`, and
`--output`, `--profile` and `--max-cps` are not allowed. Both results still pass
the SRT round-trip check. Non-strict comparison exits 0 even when infeasible;
`--strict` exits 3 if **either** profile is infeasible. Profiles are validated
left before right before the transcript is read, with side-labelled errors;
duplicate names fail with `comparison profiles must have distinct names` after
the transcript JSON is decoded.

The report (`format_version` 1, `mode: "profile_comparison"`) contains:

- `profiles.left` / `profiles.right`: name, resolved config, feasibility,
  objective, metrics, compact cue lists, full constraints and SRT verification.
  Cues carry local ID, source unit/word IDs, text, milliseconds, widths, speaking
  rate and fallback flag. Source provenance and diagnostics appear once at the top.
- `boundary_differences`: changed internal boundaries in source order, keyed by
  `after_unit_id` / `before_unit_id`; `left` / `right` is a boundary object
  (`cue_id`, `next_cue_id`, `end_ms`, `next_start_ms`, `gap_ms`) or `null`.
- `constraint_differences`: violations matched by `code`, ordered `unit_ids` and
  `next_unit_id`, with full payloads or `null` and a status of `left_only`,
  `right_only` or `changed`. Identical payloads are omitted.

For the first command, brief has two cues and patient one: the only boundary
difference is after `/words/1` (left ends at 800 ms, the next starts at 1000 ms,
gap 200 ms; right is `null`). For the conflict command both partitions share the
boundary and all three constraint differences are `right_only`. Matching uses
generated JSON-pointer source IDs, so repeated upstream IDs and cue renumbering
cannot create false differences; profiles are not ranked by readability.
In the library, `compare_profiles(data, left_profile, right_profile, target_duration=0.5)`
returns the same structure.

## Supported JSON and preservation rules

- Whisper / faster-whisper: `{"segments": [{"start": 0, "end": 3, "text": "...",
  "words": [{"word": " Hello", "start": 0, "end": 0.7}]}]}`. Segment `text` or
  times may be omitted when every word is timed.
- WhisperX `segments[].words`, or flat `{"word_segments": [...]}` / `{"words": [...]}`
  with the same fields. A redundant WhisperX `word_segments` next to `segments` is
  validated against the segment words and must agree.
- Extra metadata (probability, score, speaker, language, tokens) is accepted but
  not used. Word and segment `id` values are kept as `original_id`; generated JSON
  pointers stay unambiguous even when upstream IDs repeat.

Each word's original string and seconds are in `normalized_words`;
`source_units` holds the effective millisecond intervals; `cues[].unit_ids` and
`word_ids` link output back to them; segment text and IDs are in
`source_segments`. An optional root `text` is kept as `source_text` and must
agree with the segments (ignoring whitespace).

When words match the segment text in order, whitespace belongs to the next word.
Text spans are Python code point offsets into the original segment string. Any
unaligned text, an untimed word or a segment without words forces an
**indivisible whole-segment fallback**, which needs explicit segment start/end;
all text and IDs survive. Word content that cannot be found in the segment text
is rejected rather than silently choosing one version. For flat words, an ASCII
space is inserted between neighbors only when neither side is whitespace or CJK,
never inside a grapheme cluster, and each insertion is diagnosed.

Times must be finite, nonnegative and at most 359999.999 s; reversed intervals
fail. Milliseconds use decimal half-up rounding. In input order, the effective
start is `max(raw_start, previous_effective_end)` and the end
`max(raw_end, effective_start + 1 ms)`; every change is reported and the
original times are kept. The tool does not infer correct timing from audio.

SRT display collapses whitespace; each cue's `original_text` keeps the exact
source substring. Lines break at whitespace or between CJK graphemes, never
inside a Latin word or grapheme cluster. Literal markup, braces and backslashes
are written as HTML entities, verified after pysubs2 parsing (player handling of
entities varies). Text that cannot survive the round trip (such as a pair of SRT
timestamp strings) exits 2. UTF-8 BOM is accepted; duplicate JSON keys,
NaN/infinity, control characters, unknown or mixed schemas and inputs over 64 MiB
fail. The report records the pysubs2 version that performed the round trip.

## Unicode line widths

`--width-mode display` (library: `Config(width_mode="display")`) estimates cells
by summing `wcwidth.wcswidth` over extended grapheme clusters from
`wcwidth.iter_graphemes`, with ambiguous characters fixed at one cell. There is
no terminal detection or font measurement, so this estimates subtitle layout, not
pixel width; results follow the Unicode tables of the installed wcwidth. The
default `codepoint` mode counts code points.

At `--max-chars 4`, [`unicode-width.json`](../examples/reflow/unicode-width.json) gives:

| Text | `codepoint` lines (widths) | `display` lines (widths) |
| --- | --- | --- |
| `你好世界` | `你好世界` (4) | `你好` / `世界` (4, 4) |
| `é 👩🏽‍💻` | `é` / `👩🏽‍💻` (2, 4) | `é 👩🏽‍💻` (4) |

```bash
mediawrench reflow examples/reflow/unicode-width.json --width-mode display \
  --max-chars 4 -o out/unicode.srt --report out/unicode.report.json
```

Decomposed `é` is one cell; `👩🏽‍💻`, `🇨🇳`, `1️⃣` and fullwidth `Ａ` are two
each. Every cue reports `line_widths`, and width violations name their
`width_mode` (`overflow_chars` measures the excess in those units). Text is never
removed to meet a width: with a one-cell bound, `👩🏽‍💻` stays intact, width 2,
`feasible=false`. Display mode rejects characters with undefined width (such as
C1 controls) with exit 2. Line and cue boundaries never fall inside a grapheme
cluster; if ASR words split a cluster, those units stay together and
`grapheme_boundary_join` is reported. A cluster spanning separate segments is
rejected.

## Speaking-rate units

`--cps-mode grapheme` (library: `Config(cps_mode="grapheme")`) counts extended
grapheme clusters instead of code points for `max_cps`. The count covers the
whole cue text after whitespace collapse and **before** line wrapping: each kept
space counts once and line breaks add nothing. Combining marks, emoji modifiers
and joiners inside a cluster add nothing. Grapheme counts estimate text units,
not syllables or measured readability.

For [`grapheme-rate.json`](../examples/reflow/grapheme-rate.json):

| CPS mode | Count | Rate | Feasible at `--max-cps 3` |
| --- | ---: | --- | --- |
| `codepoint` (default) | 7 | 7 codepoints/second | No |
| `grapheme` | 3 | 3 graphemes/second | Yes |

```bash
mediawrench reflow examples/reflow/grapheme-rate.json --max-cps 3 --strict --dry-run
# exits 3: code-point speaking-rate violation
mediawrench reflow examples/reflow/grapheme-rate.json --max-cps 3 --cps-mode grapheme --strict --dry-run
# exits 0: same text and timing, grapheme rate equals the bound
```

`config.cps_mode` names the policy. Every cue has `speaking_rate` with `count`,
`count_unit`, `cps`, `cps_mode` and `rate_unit`; violations add `max_cps` and
source/cue links. CPS is reported to six decimals, but feasibility compares the
unrounded rate (equality is allowed). The rate mode affects both the DP scoring
and the fixed baseline, so partitions can change; source strings, spans and
effective times are preserved. Unknown modes exit 2.

## Deterministic optimization

The candidate graph keeps source order and segment boundaries; fallback units
are indivisible islands and words joined across a grapheme boundary form one
atomic group. A candidate may hold consecutive words that fit two lines and the
maximum duration; only a single atomic group may exceed width or duration (for
example one long word). Speaking-rate violations stay candidates so infeasibility
is reported without dropping words. Cue times are never extended.

Dynamic programming minimizes, **lexicographically**:

1. the number of violations (width, maximum duration, speaking rate, minimum
   duration, minimum gap);
2. excess severity: `1000 * overflow_chars + excess_duration_ms +
   max(0, round(1000 * (cps - max_cps))) + minimum_duration_shortfall_ms +
   minimum_gap_shortfall_ms`;
3. an integer soft cost: 400 per cue, plus `floor(1000 * (duration_ms - target_ms)^2 /
   target_ms^2)`, plus three times the milliseconds of every internal pause of at
   least `--pause`, plus the boundary cost.

A non-final boundary costs `max(0, 1000 - min(next_gap_ms, 1000) - P)`, where
`P=600` after `. ! ? 。 ！ ？ ; ； : ：` (closing quotes and brackets allowed),
otherwise 0. Width fitting prefers the least overflow, then the most balanced two
lines, then the earlier split; ties keep the first predecessor. `feasible=false`
means no zero-violation partition exists **in this candidate graph**, not a proof
about every subtitle style.

## Benchmark

[`benchmarks/reflow/benchmark.py`](../benchmarks/reflow/benchmark.py) compares the
optimizer with a fixed **N=3 word grouping** baseline on two synthetic texts
(16 chars/line, target 1.5 s, maximum 3 s, 20 cps, pause 0.5 s), using identical
normalization, rendering and timing repair. Full inputs, boundaries and
measurements are in [`benchmark-results.json`](../benchmarks/reflow/benchmark-results.json);
regenerate them with `python benchmarks/reflow/benchmark.py --output benchmarks/reflow/benchmark-results.json`.

| Synthetic fixture | Algorithm | Violations | Pause/boundary penalty | Objective | Words conserved |
| --- | --- | ---: | ---: | --- | --- |
| Pause vs midphrase | Fixed 3 | 0 | 3400 | [0, 0, 4920] | 4/4 |
| Pause vs midphrase | DP | 0 | 0 | [0, 0, 951] | 4/4 |
| Dense speech, infeasible | Fixed 3 | 1 | 1000 | [1, 5000, 2057] | 5/5 |
| Dense speech, infeasible | DP | 1 | 1000 | [1, 5000, 2057] | 5/5 |

These are algorithm measurements on synthetic text, not measured readability or
a claim about real media; the dense fixture stays infeasible and says so.

## Scope and related projects

[Whisper](https://github.com/openai/whisper) (MIT),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) and
[WhisperX](https://github.com/m-bain/whisperX) (BSD-2-Clause) produce compatible
JSON; none is a dependency. [stable-ts](https://github.com/jianfch/stable-ts)
already offers regrouping and subtitle export, so segmentation itself is not new.
This command is a small, model-free file transformer with an explicit
deterministic objective, a conservative fallback for unaligned text and a
provenance/infeasibility report. It does no ASR, forced alignment, diarization or
media processing and promises no commercial style-guide compliance. See
[THIRD_PARTY.md](../THIRD_PARTY.md) for attribution.
