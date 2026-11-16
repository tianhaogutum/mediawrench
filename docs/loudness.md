# mediawrench loudness

Two-pass loudness normalization reports what the filter *aimed* for, not what the
exported file actually measures. `mediawrench loudness` measures the input with
FFmpeg `loudnorm`, runs measured two-pass normalization into a private staging
directory, then **remeasures the final encoded WAV in a fresh FFmpeg process**. The
output name appears only if the measured integrated loudness and true-peak ceiling
pass. Use it when an export must provably hit a loudness target (podcasts, audio
books, broadcast-style deliverables) and when you want to verify an existing WAV
without re-encoding.

Inputs: local mono/stereo PCM WAV or FLAC, or one PCM/FLAC audio track of a
Matroska file. Output: a new 48 kHz / 24-bit PCM WAV plus a JSON report. Existing
files are never replaced.

## Requirements

- FFmpeg and ffprobe with the `loudnorm` filter, found on `PATH` or passed with
  `--ffmpeg` / `--ffprobe`. Most distribution and Homebrew builds include it.
  mediawrench never downloads or bundles FFmpeg.
- Python 3.11+ (`pip install mediawrench`). No other runtime dependency.

## Quick start

Make a 12-second test tone (or use any WAV/FLAC of at least 3 seconds), then
normalize it to −16 LUFS with a −1.5 dBTP ceiling:

```sh
ffmpeg -v error -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=12" input.wav
mediawrench loudness input.wav out.wav \
  --target-i -16 --target-tp -1.5 --tolerance 0.5 --report report.json
```

`out.wav` and `report.json` must be new names in existing directories; source,
output and report must be distinct. Existing files, symlinks and concurrent output
writers are never overwritten, and there is deliberately no force flag: choose new
names when rerunning.

Verify an existing WAV without writing anything:

```sh
mediawrench loudness out.wav --verify-only \
  --target-i -16 --target-tp -1.5 --tolerance 0.5 --timeout 5 --total-timeout 10
```

From a clone of the repository, `python examples/loudness/demo.py` generates
synthetic tones, normalizes them, rejects a deliberately altered candidate and keeps
the WAVs and reports under `examples/loudness/generated/run-*/`. The recorded summary
is [`examples/loudness/experiment-results.json`](../examples/loudness/experiment-results.json).

## Options

| Option | Meaning |
| --- | --- |
| `--target-i` | Integrated loudness target in LUFS, −70 to −5 (required) |
| `--target-tp` | Hard true-peak ceiling in dBTP, −9 to 0 (required) |
| `--tolerance` | Allowed absolute LUFS error, > 0 and ≤ 2 (required). It does **not** relax the peak ceiling, and the peak need not equal the ceiling |
| `--target-lra` | loudnorm processing target, 1 to 50, default 11 LU. LRA is not a verified export criterion |
| `--report PATH` | New JSON report path (required for normalization; also kept on failure) |
| `--audio-stream INDEX` | **Absolute ffprobe stream index** (not an audio ordinal); required when a Matroska file has several audio tracks. Give it at most once |
| `--timeout` | Per external process, default 120 s, maximum 3600 |
| `--total-timeout` | One shared job budget, default 600 s, maximum 86400. Every process gets the smaller of `--timeout` and the remaining budget |
| `--ffmpeg`, `--ffprobe` | Executables to run (default: `ffmpeg` / `ffprobe` on PATH) |
| `--list-streams` | Read-only Matroska stream inventory (see below) |
| `--verify-only` | Read-only verification of an existing 48 kHz / 24-bit WAV (see below) |

All profile values must be finite, and the integrated target must be below the peak
ceiling. Targets are requests, not promises: dynamic processing, content,
resampling and very tight tolerances can cause a legitimate `FAIL`.

## Exit codes and report

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | `PASS` (`OK` for `--list-streams`) | Verified and published (or inventory listed) |
| 2 | `FAIL` | The candidate failed final-file verification; nothing published |
| 3 | `UNSUPPORTED` | Unsupported input (format, channels, < 3 s, silence, ambiguous track) |
| 4 | `ERROR` | Missing/corrupt input, missing tools, failed tools, timeouts, cancellation |

Argparse usage errors also exit 2. Results are printed as JSON. Missing executables
and failed decodes/encodes are errors; they never count as passing.

The report contains input and final-file measurements, the exact applied measured
parameters, the actual `normalization_type`, a `dynamic_fallback` flag, per-check
results, source/candidate hashes, executable versions and subprocess argv.
`processing_measurement.output_i` is a filter statistic; **the gate uses
`output_measurement.input_i` and `input_tp`, measured by reading the final WAV**.
Nonfinite loudnorm strings such as `"-inf"` stay visible in failure reports; no
invented LUFS or nonstandard JSON NaN is emitted. `timings` reports `preparation`,
`analysis`, `encoding`, `verification`, `publication` and `cleanup` in integer
milliseconds that sum exactly to `timings.total`.

A `FAIL`, unsupported clip or encode/decode error keeps a complete failure report
and deletes staging audio. If the report destination is invalid, existing or an
alias, no file report can be written and the returned JSON includes `report_error`.
A report write failure rolls back this invocation's new output. Audio and report
are published with atomic, no-overwrite hard links on each destination's
filesystem (which must support hard links). They are two operations, not a
crash-atomic transaction: a crash between them can leave a verified output without
its report.

Failures caught as native `OSError` include a top-level `errno` (the exception's
numeric code, or `null`); a secondary failure while delivering the failure report
adds `report_errno` next to `report_error`. Neither is inferred from message text.

## Bounded jobs and cancellation

The monotonic `--total-timeout` deadline starts on entry and covers validation,
tool-version probes, source copying and hashing, input analysis (including
Matroska track decoding), measured encoding, final-file probing and measurement,
source rechecking, cleanup and publication. It never resets between passes. No new
process starts with an expired budget.

A timeout or CLI Ctrl-C/SIGTERM returns `ERROR` (exit 4), `published: false`,
`error_kind: "deadline_exceeded"` or `"cancelled"` and `failed_stage`. The
subprocess group is killed and reaped, temporary audio/report files are removed,
and this invocation's output is rolled back even if the interruption arrives right
after its publication link. Failure JSON is kept when its destination is safe.

Worker-thread library callers can pass keyword-only `cancel_event`, a
`threading.Event`; set it from the controlling thread and leave it set until the
call returns. The library never clears it or installs signal handlers:

```python
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from mediawrench.loudness import Profile, normalize

stop = Event()
with ThreadPoolExecutor(max_workers=1) as pool:
    pending = pool.submit(normalize, "input.wav", "worker.wav",
                          Profile(-16, -1.5, 0.5), "worker.json", cancel_event=stop)
    stop.set()  # e.g. from a UI cancel callback
    result = pending.result()  # waits for child reaping, rollback and failure JSON
```

Cancellation is cooperative: subprocess waits poll at most every 0.1 s, copying and
hashing check between 1 MiB blocks, and blocked filesystem calls can delay a
response. A cancel after the final checkpoint may observe a completed `PASS`. SIGKILL
or machine failure cannot run cleanup.

## Select a Matroska audio track

Matroska (`.mkv` / `.mka`) inputs may carry several tracks. List them first:

```sh
ffmpeg -v error -nostdin \
  -f lavfi -i 'color=size=16x16:rate=1:duration=10' \
  -f lavfi -i 'sine=frequency=440:duration=6:sample_rate=44100' \
  -f lavfi -i 'sine=frequency=880:duration=8:sample_rate=48000' \
  -map 0:v -map 1:a -map 2:a -c:v ffv1 -c:a pcm_s16le -c:a:1 flac -ac:a:1 2 \
  -metadata:s:a:0 language=eng -metadata:s:a:1 language=jpn \
  -disposition:a:0 default -disposition:a:1 forced two-tracks.mkv
mediawrench loudness two-tracks.mkv --list-streams
mediawrench loudness two-tracks.mkv mono.wav --audio-stream 1 \
  --target-i -16 --target-tp -1.5 --tolerance 0.5 --report mono.json
mediawrench loudness two-tracks.mkv stereo.wav --audio-stream 2 \
  --target-i -16 --target-tp -1.5 --tolerance 0.5 --report stereo.json
```

`--list-streams` runs only ffprobe: no FFmpeg process, staging, audio or report
files, and no writable directory is needed. It accepts `--ffprobe`, `--timeout` and
`--total-timeout`; output paths, profile options, `--report`, `--audio-stream` and
`--ffmpeg` are usage errors in this mode. The JSON has `status: "OK"`,
`published: false`, `format`, the source path, the command argv and one row per
stream, sorted by absolute `stream_index` (the value for `--audio-stream`):

```json
{"stream_index": 1, "codec_type": "audio", "codec": "pcm_s16le", "channels": 1, "sample_rate": 44100,
 "bits_per_raw_sample": null, "channel_layout": null, "language": "eng", "title": null,
 "default": true, "forced": false}
```

- `sample_rate` and `bits_per_raw_sample` are positive integers parsed from
  ffprobe's ASCII decimal strings, or `null` when absent, empty, `"N/A"`,
  `"unknown"` or zero. Malformed values reject the whole inventory with `ERROR`.
  They are metadata only and say nothing about effective quality or eligibility.
  Whether PCM streams or unnamed mono layouts report these fields depends on the
  FFmpeg build.
- `language` and `title` are ffprobe's strings (Unicode, whitespace and empty
  strings preserved) or `null`. `default` and `forced` are independent booleans
  from the 0/1 dispositions, `null` when absent. They are informational: **multiple
  audio tracks still require `--audio-stream`, even when exactly one is default or
  forced**. Exported WAVs do not copy tags or dispositions.
- Every stream type is listed. Valid WAV/FLAC files return `UNSUPPORTED` (exit 3);
  other demuxers are refused by the allowlist. `OK` does not certify decodability,
  duration, loudness or eligibility.

When normalizing, omitting `--audio-stream` on a multi-track file, selecting a video
index or selecting an absent index exits 3 (`UNSUPPORTED`), writes a failure report
and publishes nothing. Reports record `requested_audio_stream`,
`input_media.stream_index`, `input_media.channel_layout` and
`channel_layout_inferred` (true when an unnamed one/two-channel stream is treated as
mono/stereo). The selected track is decoded into a private float64 PCM WAV at its
original rate before the two loudnorm passes; `decoded_input_media` records that
intermediate, and the duration comes from its decoded sample count. Container
timeline gaps are not padded into silence.

## Verify an existing WAV

`--verify-only` calls the same read-only gate as the library's `verify_candidate`:
it probes the WAV, then measures it in a fresh FFmpeg process. The candidate must
already be a single-stream mono/stereo **48 kHz / 24-bit PCM WAV** of at least 3
seconds. It never changes the candidate, stages audio, publishes output or writes a
report, so the candidate's directory may be read-only.

`--target-i`, `--target-tp` and `--tolerance` are required; `--target-lra`,
`--ffmpeg`, `--ffprobe`, `--timeout` and `--total-timeout` keep their limits.
Output paths, `--report`, `--audio-stream` and `--list-streams` are usage errors.
Stdout JSON always includes `schema_version: 1`, the absolute `source` and
`published: false`; completed measurements add `media`, `measurement`, `checks` and
`commands` with `PASS` (exit 0) or `FAIL` (exit 2). Directories, FIFOs and other
special files are rejected before any tool starts; symlinks to regular files are
accepted.

## Library

```python
from mediawrench.loudness import Profile, Toolchain, list_streams, normalize, verify_candidate

profile = Profile(target_i=-16, target_tp=-1.5, tolerance=0.5)
tools = Toolchain(ffmpeg="ffmpeg", ffprobe="ffprobe", timeout=120, total_timeout=600)
inventory = list_streams("two-tracks.mkv", tools)  # read-only; structured OK/error result
result = normalize("input.wav", "out.wav", profile, "report.json", tools)
assert result["status"] == "PASS", result

# Keyword-only absolute source stream index, with the same publication gate:
selected = normalize("two-tracks.mkv", "selected.wav", profile, "selected.json", tools, audio_stream=2)

# Read-only verification of an already encoded 48 kHz/24-bit PCM WAV:
verification = verify_candidate("out.wav", profile, tools)
```

`normalize` and `list_streams` return structured results on expected failures.
`verify_candidate` returns `PASS`/`FAIL` for measurable candidates and raises
`mediawrench.loudness.core.AudioError` (or `OSError`) otherwise; an observed
`cancel_event` raises `JobCancelled`, an `AudioError` subclass. `list_streams` and
`verify_candidate` accept the same keyword-only `cancel_event`. None of these
functions modifies the source or candidate.

## Recorded results

Synthetic clips with FFmpeg 7.0.2, all with a −1.5 dBTP ceiling. The baseline is a
workflow that trusts the requested target after a successful two-pass encode:

| Synthetic case | Requested I / LUFS tolerance | Actual mode | Final I (LUFS) | Final TP (dBTP) | Gate / published |
|---|---|---|---|---|---|
| 12 s modulated tone | −16 / 0.5 | linear | −16.00 | −9.24 | PASS / yes |
| 12 s clipped input | −16 / 0.5 | dynamic | −15.95 | −13.84 | PASS / yes |
| 24 s changing level, LRA 1 | −16 / 0.5 | dynamic | −15.64 | −7.40 | PASS / yes |
| Same dynamic input, strict gate | −16 / 0.1 | dynamic | −15.64 | −7.40 | FAIL / no |
| 12 s tone, subprecision tolerance | −16.123 / 0.001 | linear | −16.12 | −9.37 | FAIL / no |
| Deliberate +6 dB encode after normalization | −16 / 0.5 | separate candidate | −10.00 | −3.24 | FAIL / no publication attempted |

The linear tone's filter-reported output was −16.07 LUFS while the fresh final-file
measurement was −16.00; the dynamic sample reported −15.48 in-filter but measured
−15.64 in the encoded file. These are synthetic observations; other FFmpeg builds
may measure slightly differently.

## Scope and limitations

- Normalization inputs: mono/stereo PCM WAV (8/16/24/32-bit integer or 32/64-bit
  float), FLAC, and one selected PCM/FLAC track in Matroska. Other containers and
  codecs (including AAC/Opus) are unsupported. WAV/FLAC inputs and final candidates
  must contain exactly one audio stream. More than two channels and explicitly
  non-mono/stereo layouts are rejected.
- Clips shorter than 3 seconds, silence and nonfinite measurements are explicitly
  unsupported.
- Mono is measured as mono (no dual-mono compensation). No LRA guarantee, clipping
  restoration, speech-quality assessment or broadcast certification. The final pass
  uses the same FFmpeg loudnorm engine, not an independent calibrated meter.
- loudnorm JSON is rounded to 0.01 LU/dB and comparisons use those values; a
  tolerance below that precision can reject otherwise close audio. No extra
  true-peak allowance is added.
- Tags and chapters are not copied (an audio-only export, recorded in the report).
  A private source snapshot is used for every pass, and source changes detected
  before publication cause rejection. Concurrent hostile filesystem mutation is out
  of scope.
- Subprocesses run without a shell, with one thread, local-file protocols only, a
  WAV/FLAC/Matroska demuxer allowlist, per-process timeouts and the shared deadline.
  Network URLs and playlists are refused. Staging needs disk space for a source copy
  plus uncompressed output; WAV exports near or above 4 GiB are outside the tested
  scope.

[FFmpeg loudnorm](https://ffmpeg.org/ffmpeg-filters.html#loudnorm) does the signal
processing; [ffmpeg-normalize](https://github.com/slhck/ffmpeg-normalize) already
offers two-pass normalization and statistics. This command adds the staged,
independently measured final-file gate and no-overwrite publication. Notices are in
[THIRD_PARTY.md](../THIRD_PARTY.md).
