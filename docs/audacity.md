# mediawrench audacity

Audacity is a convenient place to nudge subtitle timings against a waveform, but its
label tracks hold one line of text per region: multiline cues, repeated identical lines
and WebVTT metadata are flattened or lost on the way back. `mediawrench audacity` exports
SRT or WebVTT as readable Audacity labels with a visible `[cue:ID]` prefix plus an
integrity-checked JSON sidecar, then reconciles your edited labels by ID, restores the
original payloads and writes SRT or WebVTT with a complete JSON audit. Use it whenever
subtitle timing edits go through Audacity and the returned text must be exactly what you
sent out.

All conversion is local and offline; no media, audio engine, FFmpeg or Audacity
installation is needed.

## Quick start

```bash
pip install mediawrench
mkdir -p out
mediawrench audacity export examples/audacity/synthetic.srt \
  --labels out/labels.txt --sidecar out/sidecar.json
```

Cue 7 becomes this single label line (`<TAB>` is a literal tab):

```text
1.000<TAB>2.500<TAB>[cue:7] Hello / world
```

Import `out/labels.txt` into Audacity as a label track, adjust region boundaries, and
export **standard** UTF-8 labels (for example to `out/edited.txt`). Keep each `[cue:ID]`
prefix intact. If you move cues past one another, keep exported rows sorted by start time;
repeated identical text is safe because IDs carry identity. Then:

```bash
mediawrench audacity import out/edited.txt \
  --sidecar out/sidecar.json --source examples/audacity/synthetic.srt \
  --output out/returned.srt --report out/audit.json
```

Changing the first region to `1.250<TAB>2.750<TAB>[cue:7] Hello / world` returns the
original `Hello` and `world` on separate lines at 1.250–2.750 seconds. `--source`
optionally verifies the **original subtitle bytes** (not the edited labels) against the
sidecar.

[`examples/audacity/demo.py`](../examples/audacity/demo.py) performs real file/CLI
round trips: it moves two identical-text cues past each other, checks the exact returned
text and identity order, blocks an unapproved text edit and exercises WebVTT input.
[`examples/audacity/split_merge.py`](../examples/audacity/split_merge.py) walks through an
explicit split/merge mapping (see below). Both clean up their temporary directories.

## Commands

| Command | Purpose |
| --- | --- |
| `export SOURCE --labels L --sidecar S [--format srt\|vtt] [--dry-run]` | Subtitle to readable labels plus lossless JSON sidecar. Format defaults to the source suffix. |
| `mapping-template LABELS --sidecar S --operations OPS --output MAPPING [...]` | Validate caller-declared split/merge operations and write hash-bound mapping JSON for review. |
| `import LABELS --sidecar S --output OUT [...]` | Edited labels plus sidecar to SRT or WebVTT plus JSON audit. |

`import --format` defaults to `srt`, independently of the output filename. Successful
import writes `OUTPUT.report.json` unless `--report` is given and prints the same audit on
stdout.

### Decisions and reports

Nothing ambiguous is resolved silently. Each situation has a strict default and an
explicit choice:

| Situation | Default | Explicit choice |
|---|---|---|
| Visible text changed | Error, with original/edited text in JSON | `--text-policy original` restores the original multiline text; `edited` takes the visible payload literally as one line |
| A source cue is missing | Error | `--missing-policy drop`, or `keep` at original timing |
| A label has no ID prefix | Error | `--new-policy add` or `ignore` |
| Duplicate or unknown ID | Always an error | Correct the prefix/sidecar; no guessing |
| Point label (start = end) | Error | `--point-duration 1.5` supplies a duration in seconds |
| No sidecar | Error | `--allow-lossy` warns explicitly; original multiline text, identity and metadata are unrecoverable |
| Returned WebVTT settings cannot fit SRT | Warn and list losses | `--settings-loss-policy error` refuses conversion; `--format vtt` restores settings |
| Original WebVTT identifiers lost to SRT or merges | Warn and list losses | `--identifier-loss-policy error` refuses output; separate VTT cues restore identifiers |
| WebVTT descriptive header or NOTE text omitted by SRT | Warn and list document losses | `--document-metadata-loss-policy error` refuses, including empty deletion output; VTT retains supported text |
| Inter-cue/trailing WebVTT NOTE moved to the VTT preamble | Allow and record a relocation action | `--note-relocation-policy error` refuses, including empty deletion output |

For example, deliberately accept changed text while dropping deleted cues, or convert
plain labels without a sidecar:

```bash
mediawrench audacity import out/edited.txt --sidecar out/sidecar.json \
  --text-policy edited --missing-policy drop --new-policy add --output out/decided.srt

mediawrench audacity import plain-labels.txt \
  --allow-lossy --point-duration 1.5 --output out/plain.srt
```

Errors exit **2** with JSON on stderr, including all collected policy conflicts, and
create no conversion outputs. Invalid CLI arguments also exit 2 (argparse usage text).
Warnings are printed on stderr and recorded in the audit. Reports include
`output_format`, `losses`, output `srt_index` or `vtt_index` mapped to bridge IDs and
original source IDs, timing moves and rounding, policy decisions and per-cue supported
metadata. SRT indices are regenerated sequentially; source IDs are preserved in the
sidecar and report rather than reused as SRT numbering.

All commands accept `--dry-run`: parse, reconcile, validate output paths and print
results without writing files. Output files must be new and their directories must exist;
the CLI never overwrites. Complete outputs are staged in their destination directories and
atomically created without clobbering. Ordinary I/O errors roll back created files; an
OS/process crash between file creations can leave a subset of complete outputs, so there
is no multi-file transaction guarantee. Choose fresh paths when retrying.

## WebVTT presentation round trip

Supported cue settings stay in the sidecar, attached to the visible bridge ID; editing
times or moving identical-text cues past each other does not reassign their settings or
original WebVTT identifiers. To restore them, choose VTT output:

```bash
mediawrench audacity export examples/audacity/settings.vtt \
  --labels out/vtt-labels.txt --sidecar out/vtt-sidecar.json
# Edit/export the label track in Audacity, keeping each [cue:ID] prefix.
mediawrench audacity import out/vtt-labels.txt \
  --sidecar out/vtt-sidecar.json --source examples/audacity/settings.vtt \
  --format vtt --output out/restored.vtt
```

Changing `[cue:c1]` from `1.000<TAB>2.500` to `1.250<TAB>2.750` restores this cue, with
its original line break:

```text
speaker-a
00:00:01.250 --> 00:00:02.750 line:20% position:35%,line-left size:60% align:start
Hello
world
```

### Loss entries in the audit

SRT output emits a warning and JSON `losses` entries for everything it cannot represent:

- `webvtt_cue_settings`: bridge ID, source identifier and exact settings for each
  returned cue, with its final 1-based `srt_index` (matching the corresponding
  `output_cues` entry) and `line`, the 1-based edited label line (JSON null for a missing
  source restored by `--missing-policy keep`). Mapped outputs also carry
  `mapping_operation` and `source_ids` (bridge IDs); merged entries add
  `source_identifiers`, aligned with `source_ids` in original source order (exact strings,
  JSON null for identifier-free sources).
- `webvtt_cue_identifier`: the same index and optional mapping provenance, with `id`,
  `source_id` and `reason`. Merges keep a separate `merged_cue_identifiers` entry with the
  final `srt_index` or `vtt_index` and aligned `source_identifiers`; fully
  identifier-free merges create no identifier-loss entry. Merged losses are listed in
  mapping operation order, which may differ from output order.
- `webvtt_document_metadata`: `fields` contains `header` and/or `notes`. With `header`,
  `source_header` holds the exact original descriptive header (including `WEBVTT`). With
  `notes`, `source_notes` lists **every** original NOTE in source order with its 1-based
  `note_index`, original `before_cue` and exact `text`. Document losses have no cue line.

Dropped cues do not generate settings losses; intentionally kept missing cues keep their
settings. New cues have no original identifier or settings. Label lines and output indices
can differ after ignoring/adding rows, restoring missing cues or reordering mappings.

In VTT output, NOTE blocks are emitted in source order in the preamble before all cues.
Moving inter-cue or trailing notes there is a `webvtt_notes_to_preamble` action (not a
document text loss) whose `relocated_notes` list originally non-preamble notes with their
original `note_index`, `before_cue` and `text`. Original NOTE attachment and layout are not
restored.

### Strict loss policies

Each policy is independent, checks only the reconciled outputs, also fails under
`--dry-run`, and on refusal exits 2 with JSON on stderr containing the complete proposed
`output_cues`, `losses`, warnings and actions, creating no subtitle or report file.
Success and error audits record the selected policy.

```bash
# Refuse SRT if any returned cue would lose its presentation settings:
mediawrench audacity import out/vtt-labels.txt --sidecar out/vtt-sidecar.json \
  --settings-loss-policy error --output out/strict.srt
# Keep the settings by choosing VTT output; strict mode succeeds:
mediawrench audacity import out/vtt-labels.txt --sidecar out/vtt-sidecar.json \
  --settings-loss-policy error --format vtt --output out/strict.vtt
# Refuse loss of original WebVTT cue identifiers:
mediawrench audacity import out/vtt-labels.txt --sidecar out/vtt-sidecar.json \
  --identifier-loss-policy error --output out/strict-identifiers.srt
# Refuse loss of WebVTT header/NOTE text:
mediawrench audacity import out/vtt-labels.txt --sidecar out/vtt-sidecar.json \
  --document-metadata-loss-policy error --output out/strict-document.srt
# Refuse moving non-preamble NOTE blocks into the VTT preamble:
mediawrench audacity import out/vtt-labels.txt --sidecar out/vtt-sidecar.json \
  --format vtt --note-relocation-policy error --output out/strict-notes.vtt
```

- **Settings** (`--settings-loss-policy`, default `warn`): settings inherited by split or
  merge outputs or restored by `--missing-policy keep` block SRT conversion; settings on
  explicitly dropped cues do not. `--allow-lossy` cannot override strict mode when a
  sidecar supplies retained settings.
- **Identifiers** (`--identifier-loss-policy`, default `warn`): ordinary and split
  identifiers cannot survive SRT, so strict mode refuses those outputs. A merge containing
  at least one original identifier is refused in **either** format, because the merged cue
  has no single original identifier. Dropped originals, new cues and all-null merges do
  not trigger it.
- **Document metadata** (`--document-metadata-loss-policy`, default `warn`): refuses
  header/NOTE text omitted by SRT, even when all cues are deliberately dropped. Bare
  `WEBVTT` without notes, SRT sources and sidecar-free inputs do not trigger it.
- **NOTE relocation** (`--note-relocation-policy`, default `allow`): VTT output only;
  refuses any `webvtt_notes_to_preamble` action when the source has a NOTE outside its
  preamble.

If several policies refuse, the settings error is reported first, then identifiers, then
document metadata, then NOTE relocation, with all computed losses. Earlier parsing or
reconciliation errors take precedence and may have no proposed outputs.

## Explicit split/merge mappings

`import --mapping mapping.json` lets you declare that one source cue was split across
several label rows, or that adjacent source cues were merged into one row. No relation is
inferred from text or timing; without a mapping the strict ID behavior above applies.

For example, source cue `7` at 1–3 s contains `Hello\nworld`, cue `9` at 4–5 s contains
`Again` and cue `11` at 6–7 s contains `Later`. Replace those exported labels with these
rows (literal tabs):

```text
1<TAB>2<TAB>Hello
2<TAB>3<TAB>world
4<TAB>7<TAB>Again / Later
```

Write `operations.json` with **only** the `operations` field, naming bridge IDs, 1-based
target lines, separators and exact split texts:

```json
{"operations": [
  {"kind": "split", "source": "7", "separator": "\n",
   "targets": [{"line": 1, "text": "Hello"}, {"line": 2, "text": "world"}]},
  {"kind": "merge", "sources": ["9", "11"], "separator": "\n", "target": 3}
]}
```

Then let `mapping-template` validate the plan and add the schema and input hashes:

```bash
# Validate and print the mapping JSON for review, writing nothing:
mediawrench audacity mapping-template edited.txt \
  --sidecar sidecar.json --operations operations.json --output mapping.json --dry-run
# Write the same JSON to a fresh file after review (optionally verify the source bytes):
mediawrench audacity mapping-template edited.txt \
  --sidecar sidecar.json --operations operations.json --output mapping.json \
  --source original.srt
# Import using the mapping:
mediawrench audacity import edited.txt --sidecar sidecar.json \
  --mapping mapping.json --output returned.srt
```

The resulting mapping has this structure; `labels_sha256` is the SHA-256 of the
**edited** label text as UTF-8 (the CLI removes an initial BOM and normalizes CRLF to LF):

```json
{
  "schema": "audacity-subtitle-bridge-mapping/1",
  "sidecar_integrity_sha256": "SIDECAR_HASH",
  "labels_sha256": "LABELS_HASH",
  "operations": [
    {
      "kind": "split", "source": "7", "separator": "\n",
      "targets": [{"line": 1, "text": "Hello"}, {"line": 2, "text": "world"}]
    },
    {"kind": "merge", "sources": ["9", "11"], "separator": "\n", "target": 3}
  ]
}
```

The result contains `Hello` at 1–2 s, `world` at 2–3 s and the multiline `Again\nLater`
at 4–7 s. Label rows provide output timing; the mapping provides output text composition.

- `mapping-template` preserves the supplied operations exactly. It never selects source
  IDs or target lines, splits text, decodes the readable projection or chooses separators.
  Exact split texts are required because ` / ` and `\t` in labels are ambiguous. Source
  IDs are bridge IDs from the sidecar (`c1`, ... for WebVTT), not original WebVTT
  identifiers.
- Template creation validates the full plan through import reconciliation. It rejects
  missing source cues and new unmapped rows unless you pass `--missing-policy keep|drop`
  and `--new-policy add|ignore`. These are **validation options**, not stored in the
  mapping: supply them again at import. Point labels need `--point-duration` at both steps.
- `--source` on `mapping-template` checks the original subtitle bytes against the sidecar
  before any mapping is returned or written; a changed word, added BOM, LF/CRLF change or
  empty file exits 2.
- Split requires two or more targets in increasing line order; joining their `text`
  values with `separator` must reproduce the entire source text. Merge requires two or
  more distinct, **adjacent** cues in original order with identical presentation metadata.
  Split outputs inherit source metadata and identifier; merges inherit common metadata and
  record their identifiers in a `merged_cue_identifiers` loss.
- A source belongs to exactly one operation and cannot also appear in an ordinary ID
  label; each target row belongs to exactly one operation. Invalid fields, duplicate
  consumption, nonadjacent merges, text mismatch and stale hashes exit 2 with no outputs.
  Text/new/missing policies never bypass mapping errors.
- Every later label edit, including a timing edit, changes `labels_sha256`: regenerate
  the template and review target lines. Hashes detect stale or mismatched inputs; they do
  not authenticate mappings.

The audit includes the input `mapping`; `split`/`merge` actions contain the complete
original source records, the separator and target texts. Each mapped `output_cues` entry
has `mapping_operation` (1-based), `source_ids` (bridge IDs) and the exact output `text`;
its ordinary `id` is null. Many-to-many rules, nonadjacent merges and conflicting
presentation settings are deliberately unsupported.

## Formats, sidecar integrity and limits

- **SRT:** strict numeric cue IDs, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, nonempty text. UTF-8,
  optional initial BOM, LF or CRLF. Duplicate source IDs, blank payload lines, malformed
  timing/settings and timestamp pairs embedded in text are rejected. Start times must be
  nondecreasing; overlaps and equal starts are fine. Parsing is cross-checked against
  [pysubs2](https://github.com/tkarabela/pysubs2).
- **WebVTT subset:** `WEBVTT` header with optional same-line description, optional cue
  identifiers, `MM:SS.mmm` or `HH:MM:SS.mmm`, plain multiline text and NOTE blocks.
  Supported settings are `vertical:rl|lr`, `line:` (signed integer or 0–100% offset,
  optional `,start|center|end`), `position:` (0–100%, optional
  `,line-left|center|line-right`), `size:` (0–100%) and `align:start|center|end|left|right`,
  following the [W3C WebVTT CR of 4 April 2019, §4.4](https://www.w3.org/TR/2019/CR-webvtt1-20190404/#webvtt-cue-settings).
  Setting order, values and internal whitespace are retained. STYLE and REGION blocks or
  references, markup, entities, timestamp maps, unknown settings and other directives are
  rejected; arbitrary WebVTT presentation, regions, styles or inline timestamps are not
  preserved.
- **Audacity labels:** tab-separated start, end and optional text, one row per region,
  sorted by start. Times are nonnegative finite decimal seconds (no sign or exponent, at
  most nine integer and twelve fractional digits). Output uses millisecond precision,
  rounded half-up to the nearest millisecond and recorded in the audit. Collapsed or
  reversed intervals and times of 100 hours or more fail. Frequency continuation rows
  starting with `\` are rejected.
- **Display projection** doubles backslashes, shows tabs as `\t` and newlines as ` / `.
  It is readable, **not a reversible encoding**: with an unchanged label payload, the exact
  original text (Unicode, whitespace, tabs, line breaks, HTML, literal backslashes) comes
  from the sidecar. Line endings are normalized to LF; original byte layout, BOM, number
  formatting and separators are not reproduced.
- **Sidecar:** versioned schema, original cue/text/metadata records, source SHA-256,
  original-label SHA-256 (informational) and a SHA-256 of its canonical JSON excluding
  `integrity_sha256`. New exports use `audacity-subtitle-bridge/2`; original `/1` sidecars
  without settings are still read. An integrity mismatch is reported as corruption.
  **Edited labels are expected to have a different hash and are accepted.** Integrity is
  not authentication: keep each label file with its own sidecar, and use `--source` to
  check the original bytes.
- Deliberately dropping all cues produces an empty SRT and a zero-count audit; VTT
  deletion output keeps its header/NOTE preamble with no cue blocks.
- Validated as a file-format round trip, not an Audacity GUI session. No GUI automation,
  audio editing, source replacement, network access or external executables.

The reconciliation uses one ID-to-record dictionary and a seen-ID set rather than
matching on text, so parsing and lookup are linear in input size (final ordering is at
most O(n log n)); everything is held in memory.
[`benchmarks/audacity/benchmark.py`](../benchmarks/audacity/benchmark.py) round-trips
1,000 to 10,000 identical multiline cues, each moved by 250 ms, and checks every payload;
on a typical Linux machine 10,000 cues export in about 0.1 s and import in about 0.25 s.

## Library

```python
from mediawrench.audacity import create_mapping_template, export_subtitles, import_labels

labels, sidecar = export_subtitles(srt_bytes, "srt")  # or "vtt"
edited = labels.replace("1.000\t2.500", "1.250\t2.750", 1)
srt, audit = import_labels(edited, sidecar)  # unresolved edits fail by default
vtt, audit = import_labels(edited, sidecar, output_format="vtt")
srt, audit = import_labels(edited, sidecar, settings_loss_policy="error")
srt, audit = import_labels(edited, sidecar, identifier_loss_policy="error")
srt, audit = import_labels(edited, sidecar, document_metadata_loss_policy="error")
vtt, audit = import_labels(edited, sidecar, output_format="vtt", note_relocation_policy="error")
# Caller-authored split/merge operations for the corresponding edited rows:
# mapping = create_mapping_template(edited, sidecar, operations)
# srt, audit = import_labels(edited, sidecar, mapping=mapping)
```

These functions perform no I/O. `BridgeError` carries `.report` on import failure;
filesystem and atomic-write behavior live only in the CLI layer.

## Relationship to Audacity and pysubs2

[Audacity](https://github.com/audacity/audacity) already imports SRT as labels.
`mediawrench audacity` covers the audited return path for timing edits and multiline text
recovery; it interoperates only through the documented label TSV format, and no GPL code
is copied or linked. [pysubs2](https://github.com/tkarabela/pysubs2) (MIT) performs the
actual SRT parsing and output but does not provide sidecar identity reconciliation. See
[THIRD_PARTY.md](../THIRD_PARTY.md).
