# mediawrench chunks

Long recordings often have to be cut into pieces before ASR, and the pieces'
transcripts then have to be put back together on the original timeline without
duplicating the words spoken in the overlaps. `mediawrench chunks split` cuts an
integer PCM WAV into sample-exact, overlapping chunks (optionally at silences) and
writes a checksummed manifest; `mediawrench chunks stitch` verifies the audio and
joins the per-chunk ASR JSON back into source-timed JSON, SRT and a trace report.
Use it when you run Whisper, faster-whisper or any other ASR yourself and want the
chunk-to-source mapping and the overlap deduplication to be auditable.

No ASR, model, FFmpeg or network access is involved; everything runs on local files
with the Python standard library.

```sh
pip install mediawrench
mediawrench chunks --help
```

## Quick start

The bundled demo generates 12 seconds of synthetic mono 8 kHz audio, splits it at
silences, writes synthetic Whisper-shaped transcripts with identity fields, stitches
them and checks every result against known ground truth:

```sh
python examples/chunks/demo.py --output chunks-demo
```

Use a new or empty output directory; without `--output` everything happens in a
temporary directory. A recorded run is in
[`examples/chunks/experiment-result.json`](../examples/chunks/experiment-result.json):
96000 source samples become four chunks `[0,28000)`, `[20000,60000)`,
`[52000,92000)`, `[84000,96000)` (at most 40000 samples each, 8000-sample
overlaps). Five input segments become four: one boundary duplicate is removed, a
later genuine repeat survives, there are zero conflicts and zero samples of source
mapping error. Plain concatenation would keep one extra segment.

With your own recording:

```sh
# 1. Split (chunks/ must not exist yet)
mediawrench chunks split input.wav chunks --max-seconds 30 --overlap-seconds 1 --detect-silence

# 2. Run your ASR on chunks/chunk-000001.wav, chunk-000002.wav, ... and save one
#    JSON per chunk as transcripts/chunk-000001.json, transcripts/chunk-000002.json, ...

# 3. Stitch into merged.json, merged.srt and merged.report.json
mediawrench chunks stitch chunks/manifest.json transcripts merged.json
```

## Split a recording

```text
mediawrench chunks split SOURCE OUTPUT_DIR [--max-seconds 30] [--overlap-seconds 1]
    [--search-seconds 5] [--silence-json PATH | --detect-silence]
    [--threshold-db -40] [--window-seconds 0.02] [--min-silence-seconds 0.1]
    [--plan-only] [--resume]
```

Without `--resume`, `OUTPUT_DIR` must be a new directory. It receives
`manifest.json` and files such as `chunk-000001.wav`.

**Supported input:** nonempty, uncompressed 8-bit unsigned or 16/24/32-bit signed
little-endian integer PCM WAV, including multichannel audio accepted by Python's
`wave` module. Floating-point WAV, compressed WAV, RF64 and other containers are
rejected; 32-bit float must not be passed off as 32-bit PCM.

**Boundaries.** Without silence options, chunks are cut at the maximum duration.
With `--detect-silence`, each channel's RMS is measured in non-overlapping windows
(`--window-seconds`, default 20 ms); **every** channel must be at or below
`--threshold-db` (default -40 dBFS) for at least `--min-silence-seconds` (default
0.1 s). This is a simple energy heuristic, not VAD; stereo channels cannot cancel
each other, and a partial final window is evaluated too.

Alternatively, supply ordered, disjoint intervals in source seconds, as in
[`examples/chunks/silences.json`](../examples/chunks/silences.json):

```json
{"intervals": [{"start": 3, "end": 4}, {"start": 7, "end": 8}]}
```

```sh
mediawrench chunks split input.wav supplied-chunks \
  --max-seconds 5 --overlap-seconds 1 --silence-json examples/chunks/silences.json
mediawrench chunks split input.wav plan \
  --max-seconds 30 --overlap-seconds 1 --detect-silence --plan-only
```

(The example intervals need audio at least 8 seconds long.) Silence intervals must
lie inside the recording, and their inward sample rounding must leave at least one
sample. `--search-seconds` (default 5) is how far before each hard duration limit
the planner looks for silence; the latest feasible silence midpoint, clamped to that
region, wins, otherwise the hard limit is used. Each next chunk starts at the
previous end minus the overlap. A silence boundary is only used if the next chunk
then starts at least half of `max - overlap` (minimum one sample) after the current
start, so every chunk advances meaningfully; only a near-maximum overlap can still
produce many chunks.

**Sample arithmetic.** All intervals are half-open `[start_sample, end_sample)`,
where a sample is one frame across all channels. The decimal maximum duration
rounds **down**, the overlap rounds **up** and the search width rounds down at the
source rate. The overlap must be smaller than the rounded maximum. Coverage starts
at 0, has no gaps, ends at the exact frame count, and no chunk exceeds
`max_samples`. PCM is copied without resampling; WAV headers are newly written and
non-audio RIFF metadata is not copied. Full-file hashes include headers.

**`--plan-only`** inspects and hashes the input and writes **only** a manifest into
a new directory. Chunk hashes are `null` and `stitch` refuses such a plan. It
writes no temporary audio. Run a realized split separately into a new directory.

### Manifest (version 1)

- `source`: path relative to the manifest directory, full-file SHA-256, sample
  rate, channels, byte sample width and total frame count.
- `max_samples`, `overlap_samples`, the silence policy and detected or supplied
  sample intervals; each chunk has a sequential ID, filename, full-WAV SHA-256,
  source start/end, actual overlap with the previous chunk and boundary reason.
- `plan_only`, `schema_version` and `complete`; realized chunks have non-null
  digests. `complete` is false for plans and interrupted resumable exports.
- `"tool": "audio-chunk-manifest"` identifies the manifest format (kept for
  compatibility with manifests written by the original standalone tool).

Keep the original source at its recorded relative path when stitching; moving the
whole source/chunks tree together is fine.

### Recover an interrupted split (`--resume`)

Opt in **on the first invocation**, then repeat the identical command after an I/O
error or interruption:

```sh
mediawrench chunks split input.wav chunks --max-seconds 30 --overlap-seconds 1 --resume
```

This needs a local POSIX filesystem with directory `flock`, atomic rename and
fsync (developed on Linux). The directory gets an immutable incomplete
`manifest.json` (`resumable: true`, `complete: false`,
`checkpoint_format: "jsonl-v1"`, null chunk hashes). PCM is copied in blocks of at
most 65,536 frames into one temporary WAV at a time; after each chunk is closed,
synced, hashed, renamed and its directory entry synced, one bounded commit record is
appended and fsynced to `checkpoint.jsonl`. Only after all chunks and a final source
hash check is the manifest atomically replaced with all digests and
`complete: true`. `stitch` rejects incomplete manifests even if all WAVs exist. The
final manifest is self-contained; the journal may be deleted after completion.

The journal is ASCII JSON Lines. The first line binds it to the SHA-256 of the exact
incomplete manifest bytes; each following line commits one chunk:

```jsonl
{"format":"audio-chunk-manifest-journal-v1","manifest_sha256":"<64 lowercase hex digits>"}
{"chunk_id":"chunk-000001","sha256":"<64 lowercase hex digits>"}
```

Every record ends with a newline and is at most 512 bytes. Recovery:

- rehashes the whole source, rebuilds the plan and requires the same source
  path/PCM metadata, requested durations (`30` and `30.0` match; different values
  that round to the same frame count do not), silence policy, supplied-silence file
  hash and chunk ranges;
- replays complete journal records in order. Only an unterminated final tail
  shorter than 512 bytes counts as an interrupted append; it is truncated and that
  chunk rewritten. Malformed lines, duplicate keys, invalid hashes, skipped,
  reordered or extra IDs, oversized tails, a missing/symlinked journal, a wrong
  header binding or an unknown format fail with exit 2 **before any mutation**;
- verifies every committed chunk (regular file, SHA-256, PCM metadata, frame count)
  before writing anything. Verified chunks keep their bytes, inode and mtime; an
  uncommitted chunk or `.partial` file is always rewritten, never trusted;
- returns without writes when the split is already complete.

Changed source or parameters, corrupt or missing committed chunks, non-checkpoint
directories and a concurrent resumable writer fail with exit 2 and preserve the
checkpoint. The directory lock is released by process exit, including SIGKILL.
`--resume --plan-only` is unsupported, and ordinary splits cannot be converted into
checkpoints. Older checkpoints written before the journal format are still
recoverable in their original format.

## Stitch transcripts

```text
mediawrench chunks stitch MANIFEST TRANSCRIPT_DIR OUTPUT_JSON [--native-json]
```

Put exactly one UTF-8 JSON file per chunk in a dedicated directory, named after the
chunk ID (`chunk-000001.json`, ...). Missing or extra `.json` files fail.

**Strict identity is the default.** Each file is an object with `segments` plus four
required fields, so a transcript can only be stitched onto the audio it came from:

- `recording_sha256`: the source WAV SHA-256 (`source.sha256` in the manifest);
- `manifest_sha256`: SHA-256 of the **exact final manifest.json bytes** (compute it
  after the split completes);
- `chunk_id`: the chunk's ID, matching the filename;
- `audio_sha256`: the chunk WAV SHA-256 (`chunks[i].sha256`).

Digests are 64 lowercase hex digits. `sample_rate` is optional but must match the
source when present. Wrap Whisper output or serialized faster-whisper segments like
this:

```json
{
  "recording_sha256": "COPY_THE_SOURCE_WAV_SHA256_FROM_THE_MANIFEST",
  "manifest_sha256": "COPY_THE_SHA256_OF_THE_EXACT_FINAL_MANIFEST_FILE",
  "chunk_id": "chunk-000002",
  "sample_rate": 48000,
  "audio_sha256": "COPY_THE_64_HEX_DIGITS_FROM_THIS_CHUNK_IN_THE_MANIFEST",
  "segments": [
    {"start": 0, "end": 1, "text": "Hello.",
     "words": [{"start": 0, "end": 1, "word": "Hello.", "probability": 0.99}]}
  ]
}
```

A missing identity field, swapped transcripts, a changed source or chunk WAV, or an
edited manifest (even whitespace) fails with exit 2 before anything is written.
Strict provenance records `binding: "recording_manifest_chunk_sha256"` and
`report.identity.mode: "strict"`. Hashes detect accidental mix-ups; they do not
authenticate the producer or prove that ASR ran on that audio, so capture them from
the files you actually transcribe.

**`--native-json`** explicitly accepts raw Whisper objects, faster-whisper segment
lists or partial identity envelopes, with weaker guarantees. Identity fields become
optional, but every present field is still validated and all audio/manifest checks
still run. A warning goes to stderr, stdout, merged JSON's `report` and the report
file (`identity: {"mode": "native_json", ...}`); each transcript's `binding` is
`recording_manifest_chunk_sha256`, the legacy `id_rate_sha256`, or `filename_only`.

**Times** are chunk-local seconds (JSON numbers or exact decimal strings), parsed as
exact decimals without a binary-float step. For a chunk starting at sample 480000 at
48 kHz, local `[0,1]` maps to source samples `[480000,528000]` and seconds `[10,11]`.
Starts round down and ends round up to samples; original timestamps are kept.
Nonfinite, negative, reversed or out-of-chunk times fail; segments need positive
duration and nonempty text; words may have zero duration but must be ordered and
inside their segment. Duplicate JSON keys and nonfinite values are rejected
anywhere.

**Deduplication.** A segment is removed only if it comes from a different chunk,
lies entirely inside the shared audio overlap, and has **identical source times**
and identical text after whitespace normalization (case and punctuation count). If
either segment has words, both need matching word texts and exact times. Both
originals survive in `origins` and the trace. Overlapping text with different
times or words, or only a matching prefix/suffix, is kept and flagged as a
conflict; no semantic merge is guessed. Overlaps spanning more than two chunks are
handled.

Before publishing, both modes verify the source and **all** chunk hashes, WAV
metadata, IDs, maximum lengths, exact coverage and overlaps, then write:

- `merged.json` (your `OUTPUT_JSON`): source-mapped segments and words with exact
  integer sample bounds, convenience seconds, original chunk/segment indices and
  fields, transcript metadata and hashes, and the full report;
- `merged.srt`: source timestamps rounded outward to milliseconds, whitespace
  flattened and `&<>` escaped. Text containing two or more timestamp-like
  sequences (for example `Meet at 00:00:08,000 then 00:00:09,000`) is refused with
  `unsupported SRT text` (exit 2) so permissive SRT readers cannot mistake it for
  timing lines;
- `merged.report.json`: duplicate decisions and conflicts.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | invalid input, I/O error, identity mismatch or usage error; nothing published |
| 3 | stitch found overlap conflicts: JSON, SRT and report are written with **both** segments, so review the report before using the subtitles |

An empty `segments` list is a valid silent chunk. Unrecognized ASR metadata is
kept, not interpreted.

## Safety guarantees

- Outputs never overwrite existing paths. An ordinary split stages a complete
  sibling directory and renames it into place; a resumable split only updates its
  validated checkpoint and uncommitted chunks; stitch stages all three files and
  publishes each atomically without clobbering (the three files are not one crash
  transaction; an ordinary failure rolls back this call's outputs).
- Manifest, transcript and supplied-silence JSON are each read once into a byte
  snapshot that is parsed and hashed together, so a file replaced mid-run cannot mix
  old content with a new digest.
- Source and chunk WAVs are guarded by filesystem revision checks (device/inode,
  mode, size, nanosecond mtime and ctime) at every validation and publication
  boundary of split, resume and stitch. An exposed change, even one that restores
  the original bytes, fails with exit 2 before output is published. These are
  conservative metadata checks, not byte snapshots or locks: they depend on
  filesystem timestamps and are not atomic with a non-cooperating writer, so keep
  exclusive ownership of the source, chunks and journal during a run.

## Library

```python
from mediawrench.chunks import split, stitch

manifest = split("input.wav", "chunks", max_seconds="30", overlap_seconds="1")
# Use resume=True on every invocation to keep recovery checkpoints.
merged = stitch("chunks/manifest.json", "transcripts", "merged.json")
# For native ASR JSON pass native_json=True and inspect merged["report"]["identity"].
assert merged["report"]["status"] == "ok"
```

The library raises `ManifestError` for invalid content and may raise `OSError` for
missing files or permissions. Exact input decimals in returned fields use
`decimal.Decimal`; serialize results with `mediawrench.chunks.common.json_bytes` to
keep full precision.

## Limits

Integer PCM WAV only: no resampling, denoising, model or API calls, ASR quality
scoring or semantic conflict resolution. SRT has millisecond precision and cannot
carry JSON metadata. A moved or deleted source must be restored to its recorded
path before stitching. The plan, manifest and transcripts are held in memory, so
huge chunk counts or transcripts are a practical limit; PCM copying and hashing use
bounded blocks and energy detection holds one window at a time.
[`benchmarks/chunks/measure_memory.py`](../benchmarks/chunks/measure_memory.py)
measures peak RSS for 8/32/128 MiB inputs. Run it only on trusted local files.

## Related projects

[pydub](https://github.com/jiaaro/pydub) provides audio slicing and silence
utilities and [faster-whisper](https://github.com/SYSTRAN/faster-whisper) provides
ASR and VAD. This command adds a reusable sample/checksum manifest and conservative
stitching of externally produced transcripts. [Whisper](https://github.com/openai/whisper)
and faster-whisper JSON are format references, not dependencies; FFmpeg's
[silencedetect](https://ffmpeg.org/ffmpeg-filters.html#silencedetect) is a concept
reference and is not needed.
