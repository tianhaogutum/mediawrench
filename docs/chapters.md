# mediawrench chapters

Chapter markers get silently damaged when they move between tools: timestamps are
rounded, final ends and durations disappear, titles with `;`, `=` or newlines are
mangled, and Matroska UIDs or multilingual names are dropped. `mediawrench chapters`
converts local chapter sidecars between yt-dlp-shaped JSON, FFmetadata, plain
timestamp/title text and a bounded single-edition Matroska chapter XML subset,
normalizing every time to **integer microseconds** and refusing any loss you have not
explicitly approved. Use it when archiving media with chapters, when preparing
chapters to mux with FFmpeg or MKVToolNix, and to check afterwards that a media file
really carries the chapters you exported.

Sidecar commands are fully offline and run no external programs. Only
`verify-media` calls a local `ffprobe`, and only when you pass `--ffprobe`.

```sh
pip install mediawrench
mediawrench chapters --help
# Only for verify-media: install FFmpeg (ffprobe) separately, e.g. apt install ffmpeg / brew install ffmpeg
```

## Quick start

Run from a clone of the repository; outputs go to a new directory and existing files
are refused unless you pass `--force`:

```sh
mkdir -p out
mediawrench chapters convert examples/chapters/chapters.info.json out/chapters.ffmetadata \
  --from json --to ffmetadata --report out/ff-audit.json
mediawrench chapters convert out/chapters.ffmetadata out/restored.json --from ffmetadata --to json
mediawrench chapters compare examples/chapters/chapters.info.json out/restored.json \
  --left-format json --right-format json
mediawrench chapters inspect examples/chapters/fractional.ffmetadata --from ffmetadata
mediawrench chapters inspect examples/chapters/chapters.txt --from text --duration 60
```

The FFmetadata export starts with:

```text
;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000000
START=0
END=30000000
title=Intro\; A\=B \#1 \\ archive
```

Text cannot carry a final end or a duration, so even a contiguous text export needs
explicit loss approval, and a lossy write always needs a report:

```sh
mediawrench chapters convert examples/chapters/chapters.info.json out/chapters.txt \
  --from json --to text --allow-lossy --report out/text-audit.json
mediawrench chapters compare examples/chapters/chapters.info.json out/chapters.txt \
  --left-format json --right-format text --right-duration 60
# Preview the complete audit without writing anything:
mediawrench chapters convert examples/chapters/chapters.info.json out/unused.txt \
  --from json --to text --allow-lossy --dry-run
```

[`examples/chapters/demo.py`](../examples/chapters/demo.py) runs these roundtrips as
a library demo. Its deliberately difficult input (a gap, nested metadata, a
multiline title and a 12-second boundary) shows the difference an audit makes: a
naive timestamp/title text projection reread with the known duration has 5
normalized differences, the audited text export records 6 loss events, and
restoring from the saved report gives zero differences from the original.

## Commands

| Command | Purpose | Exit codes |
| --- | --- | --- |
| `convert INPUT OUTPUT --from F --to F [--duration S] [--report PATH] [--allow-lossy] [--force] [--dry-run]` | Convert a sidecar; refuse unapproved loss | 0, 2 |
| `inspect INPUT --from F [--duration S]` | Validate and print the normalized document | 0, 2 |
| `compare LEFT RIGHT --left-format F --right-format F [--left-duration S] [--right-duration S]` | Compare normalized content | 0 equal, 1 different, 2 |
| `verify-media MEDIA --report AUDIT --ffprobe EXE [...]` | Probe a local media file against the exported chapters | 0, 1, 2, 3 |
| `check-inputs MEDIA --report AUDIT --verification RESULT [--output PATH] [--force]` | Re-check saved SHA-256/size bindings, no probe | 0, 1, 2 |
| `restore-source AUDIT OUTPUT [--force]` | Recover the original source from a saved audit | 0, 2 |
| `check-export AUDIT EXPORTED` | Compare an exported sidecar with the audit's expectation | 0, 1, 2 |

`F` is one of `json`, `ffmetadata`, `text`, `matroska`; there is no format
autodetection and no stdin/stdout aliasing. Exit codes: **0** success/equal; **1**
differences found; **2** invalid input, unsupported format, unapproved loss, probe
failure or timeout, or I/O failure; **3** `verify-media` was skipped because the
selected ffprobe or the requested media duration is unavailable (known chapter
differences still return 1). Validation failures never create the conversion
output.

## Conversion audits

`--dry-run` writes no files. For real lossy writes `--report` is mandatory. A report
(schema version 1) contains the full normalized source, every loss with its path,
reason and value, the destination format, the readback duration when needed, and a
normalized comparison (`comparison.right` is what was actually exported).
`lossless: false` can coexist with `comparison.equal: true` for text: equality then
depends on supplying the duration stored in the report. Multiline titles are
flattened to spaces **only** in approved lossy text exports, and gaps become implicit
next-start ends; the original values stay in the report.

Keep the report with the archive: it may contain all of the source metadata.

## Supported formats

| Format | Input | Output and limitations |
| --- | --- | --- |
| `json` | Object with a nonempty `chapters` array: numeric seconds `start_time`, optional `end_time`, optional string `title`; optional numeric `duration`. A missing title becomes an empty string. Unknown root/chapter fields may hold arbitrary finite JSON. Duplicate keys fail. | yt-dlp-shaped object with `duration`, explicit ends and titles. Unknown fields keep their JSON types and decimal precision. This is a sidecar, not a complete downloader info file. |
| `ffmetadata` | Exact `;FFMETADATA1` header, global tags and `[CHAPTER]` blocks. Positive integer-ratio `TIMEBASE` (default `1/1000000000`); unsigned `START`/`END` ticks. Comments and blank lines ignored; tag whitespace is significant. | `TIMEBASE=1/1000000`. Escapes `= ; #` and backslash; embedded newlines use backslash + LF. String tags are kept. Non-string tags, structural key collisions, CR and an unencodable duration boundary need approved loss. `[STREAM]` and other sections always fail. |
| `matroska` | MKVToolNix chapter XML: one `EditionEntry`, flat enabled `ChapterAtom`s, optional UIDs, chapter-hidden and edition hidden/default flags, exact times and multilingual displays. | Escaped XML with six-digit microseconds. UIDs, languages and flags kept in `matroska` metadata (see below). Arbitrary metadata and a duration beyond the last end need approved loss. Complex layouts always fail. |
| `text` | UTF-8 `HH:MM:SS[.ffffff]` + TAB (or one space) + title, one chapter per line; at least two hour digits, minutes/seconds 00–59; empty lines ignored; no comments. Needs `--duration` for the final end. | Six-digit microseconds, TAB separator. No metadata, explicit ends or duration; every omission is audited; multiline titles and gaps need approval. |

LF and CRLF input is accepted; text does not support bare CR and FFmetadata cannot
keep CR in tag content. NUL in titles and unpaired surrogates are rejected. Input
times must be exactly representable in microseconds: `1001/30000` with 30 ticks is
exactly 1.001 s, while `1/3` with 1 tick is rejected rather than rounded. The
range is 0 to `2**63-1` microseconds; starts strictly increase, ends exceed their
starts and must not overlap the next chapter or exceed the boundary. Gaps and an
initial offset are allowed.

An omitted end is inferred **only** from the next start, or from the supplied
duration for the last chapter. A `--duration` that conflicts with the JSON
duration fails. Nothing is sorted, clamped, rounded or given an arbitrary final
end. Normalization keeps semantic values, not JSON whitespace or number spelling,
timebase spelling, comments, line endings, or whether a title or end was omitted.

## Matroska chapter XML

The adapter follows the element names of the
[MKVToolNix 92.0 chapter DTD](https://sources.debian.org/src/mkvtoolnix/92.0-1/examples/matroskachapters.dtd/)
and the identity/time semantics of
[RFC 9559 §5.1.7](https://www.rfc-editor.org/rfc/rfc9559.html#section-5.1.7). It
handles chapter sidecars, not MKV files, and does not run MKVToolNix.

```sh
mediawrench chapters convert examples/chapters/multilingual.matroska.xml out/matroska.json --from matroska --to json
mediawrench chapters convert out/matroska.json out/restored.xml --from json --to matroska
mediawrench chapters compare examples/chapters/multilingual.matroska.xml out/restored.xml \
  --left-format matroska --right-format matroska
mediawrench chapters convert examples/chapters/multilingual.matroska.xml out/matroska.ffmetadata \
  --from matroska --to ffmetadata --allow-lossy --report out/matroska-audit.json
```

The first three commands preserve edition UID 42, chapter UIDs 101/102, intervals
`[1, 1000001)` / `[1500000, 2000001)` microseconds and all English, Chinese and
French displays including XML special characters; the comparison is equal. The
FFmetadata export keeps the first title per chapter and needs approval for the
omitted UIDs and displays, which the audit retains in full.

More fixtures, each with its expected JSON next to it:

- [`default-edition.matroska.xml`](../examples/chapters/default-edition.matroska.xml):
  `EditionFlagDefault=1`, one chapter `Default & chosen`, no UIDs
  ([expected JSON](../examples/chapters/default-edition.chapters.json)). An FFmetadata
  export reports exactly one loss at `/metadata/matroska` with `{"edition_default": 1}`.
- [`hidden-edition.matroska.xml`](../examples/chapters/hidden-edition.matroska.xml):
  edition UID 42 with both hidden and default flags
  ([expected JSON](../examples/chapters/hidden-edition.chapters.json): root
  `matroska` is `{"edition_uid": 42, "edition_hidden": 1, "edition_default": 1}`).
- [`hidden-chapters.matroska.xml`](../examples/chapters/hidden-chapters.matroska.xml):
  a hidden chapter with UID 101 and English/Chinese displays followed by a visible
  chapter without UID ([expected JSON](../examples/chapters/hidden-chapters.chapters.json)).
  Only the first chapter gets `matroska.hidden: 1`.

JSON-to-XML-to-JSON roundtrips of all of them compare equal; lossy exports without
approval or without a report exit 2 before anything is written, and `restore-source`
recovers the flags, UIDs and displays.

### The `matroska` metadata contract

The metadata key `matroska` is reserved when exporting XML. Root metadata allows
`edition_uid`, `edition_hidden` and `edition_default`; chapter metadata allows
`uid`, `hidden` and `displays`:

```json
{
  "matroska": {
    "uid": 101,
    "hidden": 1,
    "displays": [
      {"string": "Intro & <archive> \"A\"", "language": "eng", "language_ietf": "en"},
      {"string": "开场 & <档案> '甲'", "language": "zho", "language_ietf": "zh-Hans"}
    ]
  }
}
```

- UIDs are optional positive unsigned 64-bit integers and are never invented.
  Repeated UID elements and duplicate chapter UIDs (including `1` versus `01`) fail;
  edition and chapter UID scopes are separate.
- `EditionFlagHidden`, `EditionFlagDefault` and `ChapterFlagHidden` accept only `0`
  or `1` (surrounding whitespace trimmed). `1` maps to the integer `1` in metadata
  and is emitted on export; `0` or absence normalizes to no key and no element. For
  XML export the reserved values must be exactly integer `1`: booleans, strings,
  decimals and `0` fail even with loss approval. A hidden chapter must still be
  enabled. Malformed or duplicate flags fail.
- The first display is the canonical chapter `title`, and export requires them to
  match. Display order, strings and languages are kept. Each display has one
  `ChapterString`, at most one `ChapterLanguage` (three lowercase ASCII letters) and
  at most one `ChapLanguageIETF` (a bounded lexical subset: 2–8 letters, or `x`
  with a subtag, then 1–8 alphanumeric subtags; no registry lookup or case
  folding). Duplicate `(language, case-insensitive language_ietf)` pairs fail.
- An omitted `ChapterLanguage` means `eng`. A single English display without an IETF
  tag is represented by `title` alone; no displays means an empty title.
- Times are `HH:MM:SS[.fraction]` (at least two hour digits, 1–9 fraction digits),
  exact microseconds within unsigned 64-bit nanoseconds, never rounded. XML has no
  media duration field, so a boundary beyond the last end needs approved loss.
- Exactly one edition with nonempty flat chapters is required. Multiple or nested
  editions, nested, ordered or linked chapters, edition displays, track
  restrictions, chapter processes, country elements, unknown elements, attributes,
  namespaces and repeated singletons always fail, even with loss approval.
- UTF-8 XML 1.0 with predefined/numeric character references and comments is
  accepted. DTDs (including an external DOCTYPE), custom entities, processing
  instructions and XML-forbidden characters fail; nothing is fetched.

## Verify chapters in a media file

After muxing an exported sidecar into a media file, check the file against the
conversion report:

```sh
mediawrench chapters verify-media archive.mkv --report out/ff-audit.json --ffprobe ffprobe --tolerance-us 1000
```

`--ffprobe` is required and takes a PATH name or an executable path; the media and
audit are only read. Expected chapters come from the report's `comparison.right`
(the actual export, including approved changes). The check covers chapter count,
title strings and order, and **both** endpoints of every chapter; it does not
compare arbitrary metadata or Matroska UIDs/displays, authenticate which report
produced the file, or prove that the audio/video is unchanged.

The JSON result has `status`, `equal`, `expected`, `actual`, per-chapter `checks`
and field-specific `differences`: `/chapters/1` with `kind: missing`,
`/chapters/1/title` with `kind: changed`, `/chapters/1/start_us` with
`kind: outside_tolerance` and a signed delta. Matching chapters return `passed`
(exit 0); differences return `different` (exit 1). Chapters stay in probe order.

- **Tolerance.** `--tolerance-us` is inclusive (default 1000; use 0 for exact).
  Arithmetic uses integer ticks and the rational `time_base` from ffprobe's chapter
  JSON; rounded `start_time`/`end_time` strings are ignored. Non-integer
  microseconds appear as exact ratio strings such as `"1/3"`. Converted endpoints
  above 9223372036854775807 microseconds exit 2. Negative starts,
  empty/reversed intervals, nonincreasing starts and overlapping adjacent chapters
  are differences regardless of tolerance, e.g.
  `{"path":"/chapters/1/start_us","kind":"overlap","previous_path":"/chapters/0/end_us","previous_end_us":1000001,"actual_us":1000000,"overlap_us":1}`.
- **Probe.** `--timeout` is 1–300 seconds (default 15). Only existing local files are
  accepted; ffprobe runs with an argv list, no stdin and the file protocol only.
  Probe failure, malformed output or timeout exit 2. If the executable is missing,
  the result is `status: skipped`, `equal: null` with empty `checks` and exit 3.
- **Duration.** `--verify-duration` also compares the audit's
  `comparison.right.duration_us` with the duration ffprobe reports (format duration
  first, else the longest audio/video stream). `--verify-duration
  --expected-duration 3` supplies an independent expected length in exact decimal
  seconds (it must be at least the last chapter end). Every duration is an
  `estimated` header value: no packet scan or decode is done, so stale headers can
  still match after physical damage. A mismatch adds `/duration_us`
  (`outside_tolerance`, exit 1); if no duration is available, matching chapters give
  `skipped`, exit 3.
- **Saving results.** `--output PATH` saves exactly the stdout JSON for exits 0, 1 and
  3, atomically and without overwriting unless `--force` (which requires
  `--output`). The output must differ from the media and audit, including aliases,
  hardlinks and symlinks; output symlinks are refused.
- **Fingerprints.** `--hash-inputs` adds `input_fingerprints.media` and `.report`
  (`sha256` and `size_bytes`) covering the complete media bytes and the exact audit
  bytes parsed. These unkeyed digests identify bytes; they do not authenticate them.

A fully synthetic run with FFmpeg installed:

```sh
python -c 'import wave; w = wave.open("out/silence.wav", "wb"); w.setparams((1, 2, 8000, 0, "NONE", "not compressed")); w.writeframes(b"\0\0" * 16000); w.close()'
mediawrench chapters convert examples/chapters/media.chapters.json out/media.ffmetadata \
  --from json --to ffmetadata --report out/media-audit.json
ffmpeg -hide_banner -loglevel error -nostdin -i out/silence.wav -f ffmetadata -i out/media.ffmetadata \
  -map 0:a:0 -map_metadata 1 -map_chapters 1 -c:a copy out/archive.mkv
mediawrench chapters verify-media out/archive.mkv --report out/media-audit.json --ffprobe ffprobe --tolerance-us 0
mediawrench chapters verify-media out/archive.mkv --report out/media-audit.json --ffprobe ffprobe \
  --verify-duration --tolerance-us 0
```

Expected: two chapters `Intro; A=B #1 \ archive` and `章节` with intervals
`[0,1000250)` and `[1000250,2000000)` microseconds, `status: passed`, exit 0. The
Matroska muxer keeps the non-millisecond boundary exactly. Muxing only one second
of audio with the same chapters still passes the chapter check, but
`--verify-duration` then exits 1 with `actual_us: 1000000`, `delta_us: -1000000`.

### Re-check saved input bytes

```sh
mediawrench chapters verify-media archive.mkv --report audit.json --ffprobe ffprobe \
  --output verification.json --hash-inputs
# Later, without ffprobe:
mediawrench chapters check-inputs archive.mkv --report audit.json --verification verification.json
```

`check-inputs` hashes the supplied media and audit (binary, 1 MiB chunks) and
compares SHA-256 **and** byte length with the saved fingerprints. Recorded paths
are ignored, so moved but identical files pass; a whitespace-only audit change
differs. The result has `kind: "input-byte-binding"`, `status`
(`passed`/`different`), `equal`, per-input `checks` and `saved_verification`, which
keeps the earlier chapter verdict; a byte match never promotes an earlier skipped
or different verification to passed. `--output` saves the same JSON (refusing to
alias any of the three inputs; `--force` to replace).

## Restore the source from a saved audit

```sh
mediawrench chapters restore-source out/text-audit.json out/recovered-source.json
```

`restore-source` reads only the audit. Lossless and approved lossy audits both
recover the complete original normalized source (order, exact times, final ends,
duration, JSON metadata, Matroska UIDs/flags/displays) as a JSON sidecar usable with
`inspect`, `compare` or `convert --from json`. Edits to the exported file are
**not** merged, and the export may be absent. Metadata keys that collide with JSON
structural fields cannot be written losslessly and are refused. Exit 0 prints a
summary such as `{"chapters": 2, "duration_us": 60000000}`.

## Check an exported sidecar

```sh
mediawrench chapters check-export out/text-audit.json out/chapters.txt
mediawrench chapters check-export out/ff-audit.json out/chapters.ffmetadata
```

`check-export` reads the sidecar in the audit's `target_format` and compares it with
the intended export in `comparison.right`, so approved lossy exports pass while later
title, time, count, duration and metadata edits produce JSON Pointer differences.
The result has `kind: "exported-sidecar-normalized"`, `status`, `equal`,
`target_format`, `readback_duration_seconds` and `comparison`. It checks normalized
values, not byte identity, source fidelity or authenticity.

## Output safety

Outputs are staged in the destination directory, fsynced and published per file:
no-clobber by default, atomic replacement with `--force`. A conversion publishes the
report before the output, so an I/O failure can leave an audit without its output.
Input/output/report aliases (normalized paths, parent symlinks, hardlinks) and
destination symlinks are refused even with `--force`. Parent directories must exist.
There is no cross-file transaction, directory-fsync power-loss guarantee or
protection against hostile concurrent path changes; keep inputs stable while a
command runs.

## Library

```python
from mediawrench.chapters import loads, dumps, compare, json_text, read_json, restore

doc = loads('{"duration":2,"chapters":[{"start_time":0,"title":"Intro; A=B"}]}', "json")
export = dumps(doc, "ffmetadata")  # raises LossError for unapproved loss
assert compare(doc, loads(export.text, "ffmetadata"))["equal"]

text = dumps(doc, "text", allow_lossy=True)
original = restore(read_json(json_text(text.report)))
assert compare(doc, original)["equal"]
```

`Document`/`Chapter` expose integer `duration_us`, `start_us` and `end_us` plus
metadata dictionaries. `loads(..., duration=...)` takes exact decimal seconds (use
strings, `Decimal` or `Fraction`, not binary floats). Media verification is
`mediawrench.chapters.media.verify_media(media, report, ffprobe="ffprobe",
tolerance_us=1000, timeout=15, verify_duration=False, expected_duration=None)` and
`check_export(read_json(audit_text), exported_text)` mirrors `check-export`.

## Scope

[FFmpeg's metadata format](https://ffmpeg.org/ffmpeg-formats.html#Metadata) and
locally saved [yt-dlp](https://github.com/yt-dlp/yt-dlp) data define the interfaces;
[LosslessCut](https://github.com/mifi/lossless-cut) is interoperability context.
[ChapterTool](https://github.com/tautcony/ChapterTool) already offers broad
conversion; this command is a small sidecar converter focused on exactness and
explicit loss audits. It does not edit, mux, download or embed media, detect formats,
or parse LosslessCut projects. `verify-media` was developed against FFmpeg 7.1's
ffprobe chapter JSON; other versions and containers depend on the installed
ffprobe.
