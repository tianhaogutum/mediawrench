# mediawrench drift

Two devices that record the same event (a camera and a separate audio recorder, two
podcast hosts recording locally) start in sync and slowly drift apart, because their
clocks run at slightly different rates. `mediawrench drift` proposes sync points near
the start, middle and end, makes you approve each time pair explicitly, fits one
clock-rate correction and streams a corrected WAV onto the reference recording's
timeline. Use it when a single offset is not enough because the second recording is a
few hundred milliseconds off by the end, and you want every sync point reviewed
rather than guessed.

It is a small, local review and rendering adapter around
[ChronoSync](https://github.com/Eureka175/ChronoSync)'s GCC-PHAT matcher and linear
fit, not a new correlation or resampling engine. Failed automatic matches stay in the
review file as slots for manual times. Nothing is uploaded; no model is used.

```bash
pip install 'mediawrench[drift]'     # NumPy, SciPy, soxr and SoundFile
mediawrench drift --help
# Required: FFmpeg (ffmpeg and ffprobe on PATH), e.g. apt install ffmpeg, brew install ffmpeg
```

Without the `drift` extra, `mediawrench drift --help` prints the subcommands and the
install hint, and every other invocation exits 2 with that hint.

## Quick start

The demo generator writes deterministic synthetic audio with FFmpeg: a 45-second
speech-like reference (seeded pink noise gated by an irregular envelope) and a second
"recorder" that is 0.2 s late and about +500 ppm fast. Run from a clone of the
repository:

```bash
cd examples/drift
python make_demo.py                        # writes output/ (git-ignored)
mediawrench drift analyze output/reference.wav output/target.flac --out output/review.json
mediawrench drift inspect output/review.json
mediawrench drift clips output/review.json --out output/listening-clips

# Listen to / check the proposed pairs in review.json before accepting them.
mediawrench drift anchor output/review.json --id start --accept
mediawrench drift anchor output/review.json --id middle --accept
mediawrench drift anchor output/review.json --id end --accept

mediawrench drift fit output/review.json --out output/fit.json --csv output/residuals.csv
mediawrench drift render output/review.json --out output/corrected.wav
```

`output/ground-truth.json` holds the transform the generator applied, so you can
compare it with `fit.json`.

Outputs are `corrected.wav` and `corrected.report.json`. The output has the
reference's nominal sample rate and timeline length, and **all target channels**.
Unavailable leading/trailing material is silence, recorded as frame counts. The
default 32-bit float WAV avoids integer clipping; `--subtype PCM_24` or `PCM_16` is
available. Large files use RF64 automatically.

For camera footage, pass the media file directly:

```bash
mediawrench drift analyze camera.mp4 recorder.wav --out review.json \
  --offset-hint -12.5 --reference-channel 0 --target-channel 1
```

`--reference-stream` / `--target-stream` choose zero-based **audio** stream ordinals;
channel numbers are also zero-based. Analysis uses a selected channel rather than
downmixing, so opposite-polarity stereo cannot cancel itself out. Rendering keeps
channel order and does not flip polarity.

`inspect` shows pending and accepted pairs, missing times, and confidence. `clips`
writes short stereo WAVs with the reference on the left and the target on the right,
centred on each proposed event. Listen to each side to confirm it is the same event.
These are local review excerpts, with no drift correction or automatic approval.
Missing matches are listed as skipped in `index.json`; `--seconds 4` changes the
excerpt duration (0.5–10 s) and `--rate` its sample rate (default 16000). Choose a new
directory for each export.

## Subcommands

| Subcommand | What it does | Writes |
| --- | --- | --- |
| `analyze REFERENCE TARGET --out REVIEW` | Propose anchors at `--positions` (default 0.1, 0.5, 0.9 of the hinted overlap); none is accepted | review JSON (`--force` to replace) |
| `anchor REVIEW --id ID` | `--accept`, `--reject`, replace `--reference-seconds` / `--target-seconds`, or add `--note` | the review JSON, in place |
| `inspect REVIEW` | Print pending/accepted pairs, missing times and confidence | nothing |
| `clips REVIEW --out DIR` | Export stereo listening pairs (reference left, target right) | a new directory |
| `fit REVIEW` | Fit accepted anchors only; print JSON; `--out` JSON and `--csv` residual plot data | optional files |
| `render REVIEW --out WAV` | Stream the corrected WAV and verify the rendered audio | WAV + `--report` (default `WAV.report.json`) |

`analyze` options: `--offset-hint SECONDS` (target minus reference, default 0),
`--window` (0.25–30 s, default 4), `--search` (at most 0.45 × window, default 1),
`--rate` (analysis rate 1000–48000 Hz, default 8000), `--positions` (2–101 fractions),
`--reference-stream`, `--target-stream`, `--reference-channel`, `--target-channel`.
`fit` and `render` share `--max-residual-ms` (default 10) and `--max-drift-ppm`
(default 20000). `render` also takes `--subtype {FLOAT,PCM_24,PCM_16}`, `--timeout`
(total FFmpeg stream seconds, default 300), `--no-verify` and `--force`.

Exit status: `0` success, `2` invalid input, refused output or failed processing
(message on stderr, prefixed `mediawrench drift:`), `130` interrupted (an unfinished
render is discarded).

## Time and sign conventions

All times are seconds from the first decoded sample of the selected audio stream;
container timestamps and LTC are not the synchronization reference. Codec priming
that survives decoding is part of the measured fixed delay.

```text
target_event_time = scale * reference_event_time + fixed_target_delay
drift_ppm = (scale - 1) * 1,000,000
corrected_event_time = (target_event_time - fixed_target_delay) / scale
```

A positive fixed delay means the same event is later in the target file. Positive
drift means target events get progressively later, requiring the target to play
faster. Offset and drift are reported separately, including drift in milliseconds
per hour. Different nominal sample rates alone do not establish clock drift. This is
a relative mapping; it does not identify which device's clock is accurate. Changing
microphone distance can also look like timing drift.

`--offset-hint` is an approximate **target minus reference** time difference. The
search is bounded around it (default ±1 s); it is not a whole-recording search.
Adjust the hint or use manual pairs if there is no common audio near the predicted
overlap. `--positions 0.05 0.25 0.5 0.75 0.95` adds coverage (anchor ids are then
`anchor-001` …; with exactly three positions they are `start`, `middle`, `end`).

## Manual anchors and residuals

Every proposal starts with `accepted: false`. `suggested_usable` is a heuristic based
on upstream confidence and waveform similarity, not an approval. Silence and
ambiguous material may provide no reliable automatic time. Enter a pair directly or
edit the JSON:

```bash
mediawrench drift anchor review.json --id middle \
  --reference-seconds 1800.25 --target-seconds 1801.91 \
  --note 'same clap checked in editor' --accept
mediawrench drift anchor review.json --id end --reject
```

Changing a time revokes a previous approval unless that command also includes
`--accept`. The original automatic measurement remains in the ledger. Relative media
paths in a hand-edited review resolve against the review file's directory. The quick
source identity checks file size, modification time, and hashes of the first/last
64 KiB; it is change detection, **not a full-file content hash**.

Only accepted pairs affect the fit. Rejected proposals still appear in CSV/JSON
residual plot data so a bad match is visible. Equal weights preserve manual
decisions. Two points can define a map, but their zero fit residual proves nothing;
prefer at least three spread across the recording.

`fit.json` / `residuals.csv` contain **anchor-fit residuals**, in target-clock
milliseconds (observed target time minus fitted target time). `corrected.report.json`
separately contains **rendered-audio residuals** measured against actual output
samples at five positions. Failed or weak verification probes remain in that report;
missing measurements are `null`. Neither result is independent ground truth. Listen
at the beginning, middle and end.

Rendering is blocked if accepted-anchor residuals exceed 10 ms or the fitted drift
exceeds 20,000 ppm. Diagnose mismatches or discontinuities first; the limits can be
changed with `--max-residual-ms` and `--max-drift-ppm`. This command fits **one
linear map only** and never repairs local dropouts.

## Saved files

The review, fit, render-report and clip-index JSON keep the schema ids of the former
standalone tool: `chronosync-drift-review/v1`, `chronosync-drift-fit/v1`,
`chronosync-drift-render/v1` and `chronosync-drift-clips/v1`. Reviews written by it
load unchanged. The `software` object records `drift_review` (the mediawrench
version) and `chronosync` (the version of the vendored ChronoSync code, `0.1.0b0`).

## Resources and failure behavior

Analysis retains one bounded window at a time (8 kHz default). Formats with precise
timestamps use two seconds of codec preroll and trim retained timestamps after
seeking, avoiding AAC priming offsets and MP3 cold starts. For a container whose
audio timestamps are coarser than one sample (for example Matroska/Opus), windows
count decoded samples from the beginning. This preserves the first-sample time
convention with bounded RAM, but scans long prefixes and may hit the 45-second
per-window timeout. Failed scans remain manual-anchor slots. The media metadata
records this fallback.

Rendering decodes blocks of at most 16,384 frames, reduced for high sample-rate
expansion or many channels, and uses `soxr.ResampleStream` at a floating input rate,
so a 5-ppm correction is not rounded to a 48-kHz integer rate. It is clock
correction, including the tiny corresponding pitch change, rather than
pitch-preserving time stretch.

FFmpeg stops on decoder errors; a damaged analysis window becomes an explicit failed
anchor that can be entered manually. Decode errors in required target audio still
block rendering, which must not silently skip bad samples. Unused target material
after the reference ends is skipped, keeping two seconds of filter context; the
report records the decoding limit. FFmpeg children have explicit timeouts and are
reaped on failure. Use `--timeout 1800` for long renders. WAV publication is atomic
after successful decoding and checking; failed renders leave no partial destination.
Existing outputs require `--force`, and an output cannot replace input media or its
review file. A render report is a separate atomic file; an I/O failure publishing
that report can leave the completed WAV available without its report.

### FFmpeg notes

- **AAC seeking.** Direct input `-ss` gave seek-dependent AAC timing on one FFmpeg
  build (−64/−32/−24 ms at 0/4/9 s against a complete PCM decode of the same M4A).
  The adapter therefore uses `-copyts -noaccurate_seek`, two seconds of input preroll
  and `atrim` on original timestamps; the tests compare seeked windows with a full
  decode across a dozen container/codec variants.
- **Coarse timestamps.** Matroska/Opus reported a 13 ms stream start while the first
  decoded frame began at 0, and a millisecond time base cannot express 48 kHz samples
  exactly. For audio time bases coarser than one sample the adapter counts samples
  from the decoder origin instead.
- **Tiny first FLAC frame.** When generating test inputs, an FFmpeg build refused to
  encode FLAC after a delay of a few samples (`invalid block size`). Adding
  `asetnsamples=n=1024:p=0` after the delay works around it.

## Current limits

- Two continuous, overlapping recordings; no cuts, nonlinear clock wander,
  disconnected segments, variable-rate resampling or multicamera graph solving.
- No GUI, automatic human approval, video remux, LTC, BWF/iXML preservation, loudness
  processing, denoising or pitch-preserving stretch. Use upstream ChronoSync, Align or
  double-ender-sync for their broader workflows.
- Echoes, repeated music, room movement and poor shared sound can defeat the matcher.
  Manual anchors are kept for exactly those cases.
- Compressed container durations may include codec padding; PCM/FLAC references
  provide the most exact output length. Windows has not been tested.

## Related tools

| Tool | Covers | Why `drift` exists next to it |
| --- | --- | --- |
| [ChronoSync](https://github.com/Eureka175/ChronoSync) | GCC-PHAT with polarity/peak diagnostics, clock-drift fits, time maps, multi-track processing | `drift` reuses its matcher and regression; its CLI reads whole recordings and has no manual-anchor review |
| [double-ender-sync](https://github.com/ogra/double-ender-sync) | Podcast anchor selection, linear and nonlinear fits, corrected WAVs, GUI | No explicit approved manual-pair input |
| [Align](https://github.com/FedyaLight/align) | Desktop/CLI multicamera sync with validated time mappings | A full desktop workflow; `drift` is a small review ledger |
| [alignaudio](https://github.com/norihiro/alignaudio) | Correlation alignment with clock compensation | No manual approval ledger |
| [audio-offset-finder](https://github.com/bbc/audio-offset-finder) | MFCC fixed-offset search with confidence | Offset only, no drift fit or render |
| [Audalign](https://github.com/benfmiller/audalign) | Fingerprint/correlation alignment of many files | No linear clock fit with manual anchors |

Attribution and licenses (ChronoSync is vendored under MIT; soxr/libsoxr and
libsndfile are LGPL) are in [THIRD_PARTY.md](../THIRD_PARTY.md#mediawrench-drift).
