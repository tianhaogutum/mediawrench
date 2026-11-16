# mediawrench rollup

Rolling ("roll-up") WebVTT captions, such as those saved from live streams or
auto-captioned videos, repeat the previous line in every new cue. Imported as-is
they double or triple the transcript. `mediawrench rollup` removes only the
repeats it can prove from local evidence. It writes clean SRT, or WebVTT that
keeps its layout, header and NOTE metadata, plus a JSON map that accounts for
every token it kept or removed.

Use it before importing saved captions into an editor, a transcript tool or a
subtitle pipeline. When the evidence is ambiguous, it keeps the text and tells
you why. It runs offline and does not download, transcode or call anything else; FFmpeg
is not needed.

## Quick start

```bash
pip install mediawrench

# Read-only: validate and print the proposed edit counts and warnings.
mediawrench rollup examples/rollup/rolling.vtt -o caption-example.srt --dry-run

# Write caption-example.srt and caption-example.srt.json (the token map).
mediawrench rollup examples/rollup/rolling.vtt -o caption-example.srt

# Opt in for captions that grow word by word or rewrap their lines.
mediawrench rollup examples/rollup/word-growing.vtt -o caption-words.srt --word-overlap
mediawrench rollup examples/rollup/word-rewrapped.vtt -o caption-rewrapped.srt --word-overlap

# Keep supported WebVTT settings, header metadata and NOTE comments.
mediawrench rollup examples/rollup/preserve-layout.vtt --output-format vtt -o caption-preserved.vtt

# Keep balanced b/i/u tags (with optional classes) on each retained fragment.
mediawrench rollup examples/rollup/formatted-rollup.vtt --output-format vtt -o caption-formatted.vtt

# A custom map path and an explicitly permitted 100 ms roll-up gap.
mediawrench rollup local.vtt -o repaired.srt --map repaired.provenance.json --max-gap-ms 100

# Accept flattening of supported markup and inline timestamps (the loss is reported).
mediawrench rollup tagged.vtt -o tagged.srt --flatten

# Rerunning on generated SRT is byte-idempotent (use a different output path).
mediawrench rollup caption-example.srt -o caption-rerun.srt
```

The sample [`rolling.vtt`](../examples/rollup/rolling.vtt) keeps `Hello there`,
`New line`, then a later, independent `Hello there`: 8 input tokens become 6.

## Options

| Option | Meaning |
| --- | --- |
| `input` | Local UTF-8 `.vtt` (or plain `.srt`, which is validated and passed through) |
| `-o`, `--output` | Caption path. Default: `<stem>.srt`, or `<stem>.repaired.vtt` with `--output-format vtt` |
| `--map PATH` | JSON provenance path. Default: output path + `.json` |
| `--output-format srt\|vtt` | SRT (default, flattening subset) or WebVTT preserving supported layout and metadata |
| `--input-format vtt\|srt` | Override detection from the file extension |
| `--max-gap-ms N` | Largest gap between cues still treated as a whole-line roll-up (default `0`; ignored by `--word-overlap`) |
| `--word-overlap` | Also repair exact word suffix/prefix overlap inside or across rewrapped lines; requires strict time overlap |
| `--flatten` | Explicitly accept the reported loss of supported tags and inline timestamps |
| `--dry-run` | Print the JSON report only; write no files |
| `--force` | Replace existing regular output files |
| `--version` | Print `mediawrench rollup <version>` |

Exit codes: **0** success (including conservative conversions with ambiguities;
inspect `ambiguities` in the report), **2** invalid or unsupported input, or an
unsafe output path. Errors go to stderr; the JSON report goes to stdout.

## Safety guarantees

- Existing caption **or** map paths require `--force`, including during a dry run.
  The input can never be overwritten, including through symlinks or hard links.
  Parent directories must already exist.
- Each generated SRT is read back through pysubs2 (each VTT through a strict local
  reader) before any file is written.
- Outputs are staged, flushed and fsynced in their destination directories, then
  published atomically one by one. Ordinary failures roll back files already
  published. The two files are **not** one crash-atomic transaction: after a
  crash or concurrent forced writers, compare the map's `srt_sha256` (or
  `vtt_sha256`) with the caption file's hash.
- Without `--force`, publication is exclusive: a file created concurrently is
  never clobbered. A dry run writes nothing.

## Repair rules and timing

Only the immediately preceding **source cue** is compared. Tokens are exact,
Unicode-whitespace-delimited strings; case and punctuation are significant.
Horizontal whitespace is normalized to single spaces; retained line boundaries
are preserved. There is no paraphrase detection, fuzzy matching or global
deduplication.

By default, a suffix/prefix match is removed only when:

- the cue pair overlaps or touches (or falls within an explicitly increased
  `--max-gap-ms`, default **0**);
- the match contains at least two tokens with at least two distinct spellings,
  has one possible alignment and occurs only once in each cue;
- both outer boundaries and internal line breaks agree on whole lines, and the
  formatting, speaker and language contexts agree as well;
- the next cue adds text or is an identical overlapping cue. Partial nested or
  suffix-only cues are ambiguous; inline timing must not show separate speech.

Identical cues require **strict temporal overlap**, even with a positive gap
threshold; identical nested cues merge by interval union. Single-word matches,
repetitive matches (such as `go now go now`), line-fragment matches and other
uncertain candidates are kept, with an explicit ambiguity reason. After a real
gap, default settings preserve all text. Raising `--max-gap-ms` is your assertion
that the source contains partial roll-ups; it can make a separate utterance look
like a roll-up, so keep the default unless you know the source.

### Word-level overlap (`--word-overlap`)

Use `--word-overlap` (library: `convert(text, word_overlap=True)`) when you know
the saved VTT displays a growing line or a sliding window of words. It permits
exact suffix/prefix matches inside a line and across changed display line breaks.
It requires **strict temporal overlap**, even if `--max-gap-ms` is set: touching
cues and pauses keep the text and report an ambiguity. A match needs at least two
distinct tokens containing letters or numbers, a unique alignment and occurrence
in both cues, agreeing formatting/voice/language contexts and cue settings, and
compatible inline timing. Nested growth, suffix-only displays, single-word starts,
ambiguous stutters, punctuation-only evidence and character-only matches (such as
unspaced CJK growth) are kept, with diagnostics. Tokenization is still exact
whitespace splitting; no words or speech intent are inferred. Overlapping
intentional repetition can still resemble a roll-up, so check the map.

[`word-growing.vtt`](../examples/rollup/word-growing.vtt) contains:

| Source cue | Display interval | Text |
| --- | --- | --- |
| 1 (`seed`) | 0–2 s | We are |
| 2 (`growth`) | 1–3 s | We are ready |
| 3 (`complete`) | 2–4 s | We are ready now |

With `--word-overlap` the output fragments are `We are` (0–4 s), `ready` (1–4 s)
and `now` (2–4 s): 9 source tokens become 4. Each fragment maps to its source cue
and token range; all 5 removed tokens map to retained representatives. Without
the flag all 9 tokens remain. Failing to establish evidence is a successful
conversion (exit 0) with `action: retained_all` in `ambiguities`; for example,
`yes` → `yes please` and `你好世界` → `你好世界今天` are kept as they are.

[`word-rewrapped.vtt`](../examples/rollup/word-rewrapped.vtt) has `We\nare ready`
at 0–2 s followed by `We are\nready now` at 1–3 s (`\n` is a display line break).
With `--word-overlap`, the output is `We\nare ready` at 0–3 s plus `now` at
1–3 s: 7 source tokens become 4. Retained fragments keep their own line breaks.
Identical overlapping word sequences merge despite different wrapping.

### Timing of retained text

Retained chunks start at their source cue start. A retained representative's end
is extended to cover later proven duplicate displays. Multi-line chunks are split
when only some lines extend, so unrelated earlier lines are not prolonged; in word
mode the same applies to individual words of sliding windows. Output cues can
therefore **overlap**, just like the input; no word-level alignment or
non-overlapping layout is invented. All intervals satisfy `0 <= start < end`.
Some players handle overlapping SRT poorly: the target is transcript import and
metadata retention, not faithful visual playback.

**Idempotence covers the emitted SRT text and timing.** Plain SRT input is
validated and canonicalized with identity token provenance and
`mode: srt_passthrough`; it is not subjected to another roll-up pass. The new map
describes the new input, so the map itself is not byte-idempotent.

## Preserved WebVTT output (`--output-format vtt`)

VTT output (library: `convert(text, output_format="vtt")`) requires VTT input. It
repairs supported roll-ups while preserving balanced `b`/`i`/`u`/`c` tags with
optional class lists, restricted explicitly balanced voice and language scopes
with optional class lists, header text, opaque header metadata lines, NOTE
content and order, IDs of retained fragments and supported cue settings. Metadata
is kept as text, without interpretation or clock shifts. BOM, line endings,
timestamp spelling, entity spelling and whitespace are canonicalized.

[`preserve-layout.vtt`](../examples/rollup/preserve-layout.vtt) has the header
`WEBVTT Local captions`, `Kind: captions`, `Language: en`, three NOTE blocks and
two cues with `align:start position:20%`: `Hello there` at 0–2 s, then
`Hello there` / `New line` at 1–3 s. The output is `Hello there` at 0–3 s and
`New line` at 1–3 s, with the exact header, NOTE text and settings.

| Policy | Default SRT | Explicit VTT |
| --- | --- | --- |
| Header metadata lines / cue settings | Reject | Preserve supported values |
| NOTE blocks | Omit, with original text in warning/map | Preserve content and order |
| Balanced `b` / `i` / `u` tags (optional classes) | Require `--flatten`, report loss | Preserve per retained span |
| Balanced `c` / class annotations | Require `--flatten`, report loss | Preserve per retained span |
| Explicitly balanced voice scopes (restricted names) | Require `--flatten`, report loss | Preserve per retained span |
| Explicitly balanced language scopes (restricted labels) | Require `--flatten`, report loss | Preserve per retained span |
| Other voice/language syntax, inline timestamps | Require `--flatten` for the supported subset, report loss | Reject, even with `--flatten` |
| STYLE / REGION / region setting / unknown settings | Reject | Reject |

Supported settings are `align:start|center|end|left|right`, `vertical:rl|lr`,
`size:0%..100%`, `position:0%..100%` (optional `,line-left|center|line-right`)
and integer or percentage `line` (optional `,start|center|end`). Percentages may
have decimal fractions. Duplicate settings, unsupported values and malformed
structures exit 2 before either output is written. Cue settings must agree
exactly (including order) for a repair; changing one cue to `align:end` keeps
both displays with a settings ambiguity.

Every retained fragment reopens its source tag stack and closes all tags at its
end, including fragments split by timing extension. The exact ordered formatting
stack must agree for every matched non-whitespace character, so `<b>H</b>ello`
and `<b>He</b>llo` cannot alias. Empty tags and formatting that applies only to
whitespace are dropped. For example, with `--word-overlap`,
`<b>We\nare ready</b>` → `<b>We are\nready now</b>` emits `<b>We\nare ready</b>`
plus `<b>now</b>`.

Output fragments inherit their source cue's ID and settings; splitting can repeat
a cue ID (legal in WebVTT). NOTEs anchored before a fully removed cue move to the
next retained cue or the end of file. The report's `loss_policy` records these
choices. Layout values are preserved, but splitting text can still change visual
positioning and wrapping. VTT reruns apply repair again; only SRT passthrough is
byte-idempotent.

### Tag, class, voice and language grammar

- **Classes on `b`, `i`, `u`, `c` and `lang`** are dot-separated ASCII
  `[A-Za-z0-9_-]+` segments: `<b.em.strong>` closes with `</b>`, never
  `</b.em.strong>`. Class case, order and duplicates are preserved as opaque
  context, never sorted or interpreted as CSS. Empty class names, classed closing
  tags, attributes and malformed nesting are errors. STYLE stays unsupported.
- **Voice scopes** are `<v Ada>`, `<v Ada Lovelace>` or `<v.em.strong Ada>` and
  must close explicitly with `</v>`, including at cue end. A label is nonempty
  words separated by exactly one ASCII space. Accepted word forms:
  - ordinary words: start with a Unicode letter (`L*`) or ASCII `[0-9_-]`, then
    letters, combining marks (`M*`) or ASCII `[0-9_-]` (`Adá`, `李 明`, `A_1 -x 2`);
  - apostrophe words: letter/mark runs joined by single `'`, `’` or `-`, with at
    least one apostrophe (`O'Neil`, `O’Neil-Smith`); no digits or `_`;
  - words with a U+2011 non-breaking hyphen: letter/mark runs joined by U+2011,
    `-`, `'` or `’` (`Jean‑Luc`); no digits, `_` or periods;
  - initials: one letter, optional marks, one terminal `.` (`J.`, `É.`, `李.`);
  - abbreviations: at least two letters plus marks and one terminal `.`
    (`Dr.`, `Éva.`).

  Separators need a letter or mark on the left and a letter on the right; edge,
  repeated or adjacent separators, leading marks, U+2010, dashes and other
  punctuation are rejected. Categories come from Python's `unicodedata`.
  Voice classes may use Unicode letters and marks (`<v.声 Ada>`); other class
  names and language labels stay ASCII.
- **Language scopes** are `<lang LABEL>` or `<lang.class LABEL>` and close only
  with `</lang>`. Labels match ASCII `[A-Za-z]+(?:-[A-Za-z0-9]+)*` (`en`,
  `EN-us`, `zh-Hant-TW`, `x-123`). They are opaque exact labels, not BCP 47
  validation or canonicalization.

Labels and classes are **exact opaque code point sequences**: no Unicode
normalization, case folding, reordering or speaker identity inference.
Composed/decomposed spellings, `'` versus `’`, `-` versus U+2011, `Dr.` versus
`Dr`, case and stack order must agree exactly for a repair; otherwise both
displays are kept. Unclosed scopes, empty class segments, entities, non-ASCII
spaces, tabs and newlines in labels are rejected for VTT, even with `--flatten`.
SRT keeps a broader, lossy voice/language subset where scopes may end with the cue.

Each grammar feature has a synthetic sample under [`examples/rollup/`](../examples/rollup/),
all meant for `--word-overlap --output-format vtt`: `classed-formatting.vtt`,
`class-rewrapped.vtt`, `voice-rewrapped.vtt`, `classed-voice-rewrapped.vtt`,
`unicode-voice-rewrapped.vtt`, `unicode-voice-class-rewrapped.vtt`,
`apostrophe-voice-rewrapped.vtt`, `mixed-separator-voice-rewrapped.vtt`,
`nonbreaking-voice-rewrapped.vtt`, `initial-voice-rewrapped.vtt`,
`abbreviation-voice-rewrapped.vtt`, `language-rewrapped.vtt` and
`classed-language-rewrapped.vtt`. Each holds a two-cue sliding display such as
`<lang en><v.em Ada>We\nare ready</v></lang>` (0–2 s) followed by the same
scopes around `We are\nready now` (1–3 s). The output keeps the first payload at
0–3 s and emits the same scopes around `now` at 1–3 s.

## Supported input and limitations

- UTF-8 WebVTT with optional BOM/CRLF, optional cue IDs (not assumed unique) and
  ordered start times, as `MM:SS.mmm` or `HH:MM:SS.mmm` within a **99:59:59.999**
  ceiling. Durations must be positive; unsorted cues and malformed, negative or
  reversed timestamps are errors.
- `b`, `i`, `u`, `c`, `v` and `lang` are parsed with nesting validation. For SRT
  they **require `--flatten`**; the original markup stays in the map and the loss
  is reported. Inline timestamps must strictly increase within a cue; `--flatten`
  reports the loss of inline timing while keeping source time ranges per token.
- WebVTT escapes `amp`, `lt`, `gt`, `nbsp`, `lrm` and `rlm` are decoded. Literal
  `<`, `>`, `{`, `}` or backslash in decoded SRT output are rejected because
  pysubs2's ASS-based representation could change their meaning (VTT output
  escapes them safely). Unknown tags, ruby, malformed tags or entities and
  control characters are errors.
- **SRT output rejects all cue settings, REGION, STYLE and header metadata**,
  even with `--flatten`. NOTE comments are omitted from SRT but kept in the
  map and report warnings.
- SRT passthrough supports plain text only, with strict structural validation.
- Tokenization does not segment unspaced CJK text into words; a single unspaced
  line is conservatively one token.
- Input is processed in memory; the overlap search favors clarity over scale.
- The included cases are synthetic, not a real-world accuracy benchmark.

## Provenance map and library

```python
from mediawrench.rollup import convert

result = convert("WEBVTT\n\n00:00.000 --> 00:02.000\nHello there\n")
print(result.srt)            # result.vtt with output_format="vtt"; result.output for either
print(result.provenance["tokens"])
```

Accessing the wrong format property raises `CaptionError`.
`parse_vtt(text, preserve_metadata=True)` validates the VTT subset and returns
cues with their settings and per-token `glyph_contexts` (one ordered tag stack per
decoded non-whitespace character).

The map (JSON schema version 1) contains the original cue IDs, raw text and
timing. `tokens` lists **every** source token once, with `action`
(`retained` or `removed_overlap`), a `representative` retained token ID and its
1-based output cue/token location. Raw character offsets are 0-based, half-open
code point offsets within the cue's `raw_text` (CRLF/CR normalized to LF).
`source_start_ms`/`source_end_ms` are cue or inline-segment intervals, labelled
by `timing_kind`; they are not estimated word alignments. `source_line` is
1-based. `output_cues` gives the emitted intervals and text; `edits`, `warnings`,
`ambiguities`, options and source/output hashes make the repair auditable.

With `--word-overlap`, `options.word_overlap` records the opt-in, and each
`output_cues` entry adds `source_span`: `cue` is the numeric source cue index and
`[token_start, token_end)` a **1-based, half-open** source token range (`ready`
above has `{"cue": 2, "token_start": 3, "token_end": 4}`). All VTT output
fragments carry `source_span`. The map contains caption text: store it wherever
you would store the transcript.

## Labelled experiment

[`examples/rollup/cases.json`](../examples/rollup/cases.json) holds ten
hand-labelled synthetic cases: four rolling cases and six counterexamples.
[`examples/rollup/experiment.py`](../examples/rollup/experiment.py) builds a plain
cue-to-SRT baseline without repair, runs the real CLI, reads both outputs back
with pysubs2 and checks the expected token sequence, intentional-token
preservation and every output duration. Its output is recorded in
[`experiment-results.json`](../examples/rollup/experiment-results.json):

| Measure | Baseline | Repaired |
| --- | ---: | ---: |
| Total tokens, all 10 cases | 73 | 58 |
| Hand-labelled duplicate tokens, 4 rolling cases | 15 | 0 |
| Lost intentional tokens, 6 counterexamples | 0 | 0 |
| Cases matching the expected repaired sequence | — | 10/10 |
| Cases with valid timing | 10/10 | 10/10 |

These are labelled fixture results, not claims about arbitrary files or speed.
[`examples/rollup/demo.py`](../examples/rollup/demo.py) runs the CLI on
`rolling.vtt` and verifies the result with pysubs2.

## Related projects

[yt-dlp](https://github.com/yt-dlp/yt-dlp) can save VTT captions locally;
mediawrench never downloads or invokes it.
[srt_fix](https://github.com/bindestriche/srt_fix) already removes duplicate SRT
lines as a postprocessor and GUI, so duplicate removal itself is not new. This
command focuses on conservative local WebVTT adjacency, ambiguity reporting and
per-token provenance. The [WebVTT specification](https://www.w3.org/TR/webvtt1/)
describes the format; only the subset above is supported. See
[THIRD_PARTY.md](../THIRD_PARTY.md) for attribution.
