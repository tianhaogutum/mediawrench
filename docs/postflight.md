# mediawrench postflight

A batch transcode can exit 0, produce a non-empty file with the right stream
headers, and still contain no decodable video, a truncated tail or a shortened
track. `mediawrench postflight` compares an original and a candidate **by actually
decoding every audio/video stream** and reports `PASS`, `FAIL` or `UNKNOWN`. Use it
as the gate in a transcoding workflow before you delete, replace or archive the
original.

It is strictly read-only: no command replaces, renames or deletes media. The
optional JSON report (and opt-in checkpoint file) is the only persistent output.
Use the verdict to inform a separately managed workflow; keep your originals and
backups.

## Requirements

- FFmpeg and ffprobe, found on `PATH` or passed with `--ffmpeg` / `--ffprobe`. The
  build must include the `framehash` muxer and decoders for your codecs.
  mediawrench never downloads or bundles FFmpeg.
- Python 3.11+ (`pip install mediawrench`). No other runtime dependency.

## Quick start

Create a tiny source and three candidates with FFmpeg (any local files work):

```sh
ffmpeg -v error -f lavfi -i color=c=blue:s=96x64:r=12:d=2 \
  -f lavfi -i sine=frequency=440:sample_rate=16000:duration=2 \
  -map 0:v -map 1:a -c:v ffv1 -c:a pcm_s16le source.mkv
ffmpeg -v error -i source.mkv -map 0:v -map 0:a -c:v mpeg4 -q:v 3 -c:a flac healthy.mkv
ffmpeg -v error -i source.mkv -map 0:a -c:a flac audio-only.mkv
ffmpeg -v error -i source.mkv -map 0:v -map 0:a -vf select=0 -c:v mpeg4 -c:a flac zero-video.mkv
```

Then compare:

```sh
mediawrench postflight source.mkv healthy.mkv --report report.json   # PASS, exit 0
mediawrench postflight source.mkv audio-only.mkv                     # FAIL, exit 1 (video dropped)
mediawrench postflight source.mkv zero-video.mkv                     # FAIL, exit 1 (header but no frames)
mediawrench postflight source.mkv audio-only.mkv --allow-drop video  # PASS: the drop is intended
```

From a clone of the repository, `python examples/postflight/demo.py` generates
media in a temporary directory, runs these comparisons through the library and
cleans up.

More options:

```sh
# Bound the entire comparison, including versions, both probes and every decode.
mediawrench postflight source.mkv output.mkv --timeout 30 --deadline 60 --report report.json

# Keep probe inventories and completed-stream evidence even if the process is killed.
mediawrench postflight source.mkv output.mkv --checkpoint-report progress.json

# Reordered tracks: absolute ffprobe indices, one pair per retained AV stream.
mediawrench postflight source.mkv reordered.mkv \
  --stream-map 0:1 --stream-map 1:0 --stream-map 2:2 --report mapped.json

# Also require exact exported language tags on the mapped AV pairs.
mediawrench postflight source.mkv reordered.mkv \
  --stream-map 0:1 --stream-map 1:0 --stream-map 2:2 --language-tags preserve

# Subtitle count preservation is opt-in.
mediawrench postflight source.mkv output.mkv --subtitles preserve

# Decode only the first 0.5 seconds of each stream; coverage becomes "partial".
mediawrench postflight source.mkv output.mkv --quick 0.5
```

## Exit codes and output

JSON goes to stdout and, with `--report PATH`, to an atomically replaced report
file (which may not alias either media file or be a symlink).

| Exit | Verdict | Meaning |
| --- | --- | --- |
| 0 | `PASS` | Every check passed |
| 1 | `FAIL` | A proven failure (missing stream, undecodable stream, duration mismatch, ...) |
| 2 | `UNKNOWN` | Infrastructure problem, ambiguous identity, timeout, cancellation or report-write failure |

Argument usage errors also exit 2. Missing executables, unreadable paths, timeouts
and an unknown required duration can never pass. Media probe/decode errors reject;
recognizably unavailable decoders produce `UNKNOWN`. If one check fails and another
is unknown, the overall verdict is `FAIL`. A report-write failure exits 2 even when
the media checks finished.

An abbreviated failure (real reports also contain commands, tool versions,
coverage, policy, per-stream facts and individual statuses):

```json
{"verdict":"FAIL","coverage":"full","checks":[{"name":"video_stream_count","status":"FAIL","actual":0,"expected":1}]}
```

### Library

```python
from mediawrench.postflight import Options, compare, exit_code, write_report

report = compare("source.mkv", "output.mkv", Options(timeout=30, deadline=60))
write_report(report, "report.json", "source.mkv", "output.mkv")
print(report["verdict"], exit_code(report))
```

## What is checked

1. Both paths must be distinct readable regular local files. Same-path, symlink
   and hardlink aliases are rejected. The report cannot overwrite either file or
   an alias, and a symlink report destination is refused.
2. ffprobe JSON establishes stream headers and container duration. By default,
   audio and video stream counts must match exactly. `--allow-drop audio` and/or
   `--allow-drop video` permit reductions, never additions. Both files still need
   at least one audio/video stream. All original AV streams are decoded even when
   their removal is allowed.
3. Each AV stream is mapped separately into FFmpeg, decoded with strict error
   handling (`-xerror -err_detect explode`) and emitted as raw-video/PCM
   `framehash` records. Each must produce a positive frame count and decoded byte
   count. The framehash checksum is only an output format; it is **not** a
   source/output fidelity score.
4. The declared container duration and each retained stream's decoded timestamp
   span must agree within `absolute_tolerance + relative_tolerance * source_duration`
   (defaults 0.25 s and 0.02; `--absolute-tolerance`, `--relative-tolerance`). If
   finite tolerance options overflow their product or sum, the affected duration
   check is `UNKNOWN` with a diagnostic and `tolerance_seconds: null`. A missing,
   nonfinite, nonpositive or out-of-float-range declared duration gives `UNKNOWN`
   even when decoded timestamps exist.
5. Subtitles are ignored unless `--subtitles preserve` requests equal counts; this
   does not certify subtitle content. Attachments, data streams and other metadata
   are outside the policy. AV language tags can be compared on explicit pairs with
   `--language-tags preserve`; disposition is reported only as a hint. Video
   cover-art streams count as video streams.

Full decode is the default for both files. `--quick [SECONDS]` (5 seconds when the
value is omitted) checks a prefix and labels `coverage: "partial"`; even a quick
`PASS` does **not** certify the unsampled remainder. Container duration is still
checked. Only a full `PASS` is a full-coverage gate.

Supported demuxers are limited to self-contained local containers: Matroska/WebM,
MOV/MP4/M4A/3GP, AVI, Ogg, WAV, FLAC, MP3, AAC, MPEG/TS, ASF and FLV. Codec support
depends on your FFmpeg build. Playlists, image sequences, devices, DRM, network URLs
and unsupported formats never silently pass. FFmpeg protocols are restricted to
`file,pipe`; this is not a sandbox for hostile files or binaries.

## Explicit stream identity

`--stream-map SOURCE:OUTPUT` uses the **absolute `index` values from ffprobe**,
not within-type ordinals or FFmpeg specifiers such as `0:a:1`. Repeat it for
**every output audio/video stream** and every source AV stream except those whose
type has an explicit `--allow-drop`. In the library use
`Options(stream_map=((0, 1), (1, 0), (2, 2)))`; `None` means no explicit map and an
empty tuple is an explicit empty plan that rejects any retained AV stream.

For example, source indices 0 (English audio, 2 s), 1 (German audio, 0.5 s) and 2
(video, 2 s), reordered to German/English/video, require `0:1`, `1:0`, `2:2`.
Those pairs compare equal decoded spans and PASS. If output stream 1 is shortened
to 0.5 s, the mapped `0:1` duration check FAILs even though the container is still
2 s long. To keep only the long audio and the video (now output indices 0 and 1),
use `--stream-map 0:0 --stream-map 2:1 --allow-drop audio`.

Without a map, only a sole source/output pair of a type is paired automatically.
Multiple possible tracks of a retained type produce `UNKNOWN` (exit 2), even when
order, durations, language and disposition match: identity is never inferred from
those hints. An independent proven media failure still yields `FAIL`.

Duplicate source or output indices, nonexistent indices, AV type mismatches,
non-AV mappings and uncovered required streams give a `stream_mapping` FAIL
(exit 1); a failed plan authorizes no duration pairs. Malformed CLI syntax is a
usage error (exit 2); malformed library pairs give `UNKNOWN` before any tool runs.
A valid mapping does not waive stream-count, decode or container checks.

`mapping` reports the mode, status, resolved `pairs` and any `errors`; the requested
plan stays in `policy.stream_map`. `stream_validation.source` and `.output` list
**every probed stream** with `stream_index`, `codec_type`, `language` (`null` when
absent), `disposition`, `peer_stream`, `mapping_status`, `decode_coverage`
(`full`, `partial` or `none`), `decode_status`, `duration_status`,
`subtitle_count_status` and `language_tag_status`. Unknown inventories are `null`.

### Mapped language tags

`--language-tags preserve` (`Options(language_tags="preserve", stream_map=...)`)
requires an explicit AV map, even for a sole source/output pair; without one the
result is `UNKNOWN` before any tool runs. The default `ignore` performs no tag
check. Each valid pair compares the exact, case-sensitive `tags.language` exported
by ffprobe: `eng` mapped to `deu` FAILs even with equal durations; adding or
removing a tag FAILs; both absent PASS. There is no alias or case normalization
(`eng`, `en` and `ENG` differ). ffprobe may omit default values (Matroska `und` is
commonly suppressed), so the gate cannot distinguish that value from an absent tag.
It certifies exported tags, not the spoken language. Reports record
`policy.language_tags` and one `language_tag` check per valid pair. Tag checks run
after mapping and before decoding; decodes, counts and durations still run.

## Deadlines and cancellation

`--deadline SECONDS` / `Options(deadline=...)` is a finite positive budget for the
whole comparison, measured from entry to `compare`; the default is unlimited. Every
subprocess, including version discovery, shares the remaining budget while still
obeying the per-invocation `--timeout` (default 30 s). On expiry the result is
`UNKNOWN` (exit 2) with `termination.reason: "deadline"` and `coverage: "partial"`;
an already proven FAIL still dominates. Cleanup and the diagnostic report may
finish after the budget.

SIGINT (Ctrl-C) and SIGTERM in the CLI request the same orderly shutdown with
`termination.reason: "cancelled"`; JSON is printed and `--report` is still written.
Library callers can pass `compare(source, output, options, cancel_event=event)`
with a `threading.Event` set from another thread; a normal `KeyboardInterrupt` is
also handled. The library installs no signal handlers; the CLI restores its own.

Reports keep completed stream facts in `decoded`. Each side's `progress` gives
`probe_completed`, AV `discovered_streams`, `attempted_streams`,
`completed_streams` and `pending_streams`. In quick mode completed streams mean
completed **prefixes**. No frames from an interrupted decode are certified. Command
records keep elapsed time, remaining job budget, exit status, stop reason and a
bounded stderr excerpt.

Every subprocess uses argv without a shell, disconnected stdin, one codec/filter
thread and single-thread environment limits. Timeouts and cancellation kill the
process group on POSIX and reap the direct child; remaining same-group descendants
are also killed when a tool exits. Non-POSIX systems kill only the direct child.
Invocations are sequential, so without `--deadline` the total runtime can be several
invocation timeouts. No temporary media is created: frame records and diagnostics
go to private temporary spool files, so a full decode needs CPU and temporary disk
space proportional to the record count.

## Opt-in progress checkpoints

`--checkpoint-report PATH` (`compare(..., checkpoint_report="progress.json")`)
atomically replaces one JSON file before starting tools, after each probe outcome,
after resolving the mapping and optional language-tag checks, after each AV decode
outcome, and with the final result. It works without `--report`; when both are
given, `--report` stays a final-only copy and may use the same path. Checkpointing
is off by default.

- Intermediate snapshots have `report_state: "in_progress"`, `coverage: "partial"`
  and verdict `UNKNOWN` unless a proven failure already makes them `FAIL`. They
  never `PASS`, even right after the last decode, because count and duration
  comparisons may still be pending.
- Probe snapshots only contain inventories whose ffprobe call, JSON parsing and
  validation succeeded. Malformed probe output (non-object `tags` or `format`,
  non-string `language`, nonfinite numbers such as `1e999` or `NaN`) rejects that
  whole side as `UNKNOWN` before any inventory is accepted.
- Once both inventories are valid, a separate checkpoint stores the resolved
  mapping and any language-tag result **before the first decode starts**.
- Failed probes and failed decodes are persisted, with the finished command and its
  bounded stderr, before the next tool starts. Uninterpretable framehash output
  (unexpected stream identifiers, conflicting or nonpositive timebases, malformed
  DTS, timestamps not representable as float seconds) returns decode `UNKNOWN`;
  none of that stream's facts are certified.
- Normal completion, deadline expiry and orderly cancellation replace the
  checkpoint with `report_state: "complete"` and exactly the JSON that was printed.
  "Complete" means the comparison returned; it can accompany `FAIL` or `UNKNOWN`.

For example, if source video 0 finishes and source audio 1 blocks, SIGKILL leaves a
readable in-progress checkpoint with source `completed_streams: [0]`,
`pending_streams: [1]` and the video facts; `termination` is still `null`, because
the snapshot cannot know the process was killed afterwards.

Checkpoint paths have the same media-alias and symlink protections as `--report`.
An unsafe or unwritable initial destination yields `UNKNOWN`/exit 2 without
starting tools. A later write failure stops further tools, returns `UNKNOWN`/2 even
if the media already failed, and records `checkpoint_write` in checks and
termination; the failed sink is not retried and a failed atomic replacement leaves
the previous whole JSON in place.

These snapshots are evidence, not a resume feature. They add a synchronous JSON
write, fsync and rename after each stage. SIGKILL during a replacement leaves the
old or new whole report and possibly a `.postflight-*.tmp` file; it also prevents
tool-group cleanup. Same-directory replacement gives atomic visibility; power-loss
durability is not promised (the parent directory is not fsynced). Use a stable
local destination and a single writer.

## Benchmark

[`benchmarks/postflight/`](../benchmarks/postflight/README.md) compares a
header-only baseline with full postflight on six synthetic cases (healthy,
audio-only, truncated, shortened, zero-video, short audio). The header-only
baseline accepted all five faulty outputs; postflight rejected all five and passed
the healthy control. Six designed cases show detection of these faults, not
statistical accuracy or perceptual fidelity.

## Limitations

- Decoding proves presence and decodability of every frame, not visual or audible
  fidelity. Use a quality metric (for example VMAF) for that.
- Subtitle content, attachments, chapters and most metadata are not compared.
- Detection depends on the FFmpeg build you supply; a build without a decoder
  yields `UNKNOWN`, never `PASS`.

Third-party notices (FFmpeg licensing and references) are in
[THIRD_PARTY.md](../THIRD_PARTY.md).
