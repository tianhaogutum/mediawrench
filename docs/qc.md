# mediawrench qc

An export can finish with exit code 0, open in every player's file dialog and still be
wrong: the audio stops two seconds before the picture, the loudness was never
normalized, the chapters were dropped by the remux, or the subtitle file was timed to a
longer cut. `mediawrench qc` is a read-only gate you run on the finished file before it
goes out. It compares the deliverable with the master it was made from and prints one
line per check, then exits non-zero if anything failed.

Every check is the library of another mediawrench command, not a second implementation:

| Check | What it verifies | Reuses | Needs |
| --- | --- | --- | --- |
| `streams` | The deliverable has as many audio and video streams as the master (`--allow-drop` relaxes this) | [`postflight`](postflight.md) | FFmpeg |
| `decode` | Every audio/video stream in both files decodes without an error and yields frames | [`postflight`](postflight.md) | FFmpeg |
| `duration` | Container and per-stream decoded durations match the master within 0.25 s + 2% | [`postflight`](postflight.md) | FFmpeg |
| `loudness` | Integrated loudness within `--tolerance` LU of `--target-i`, true peak at or below `--target-tp` | [`loudness`](loudness.md) measurement and gate | FFmpeg |
| `chapters` | The deliverable's chapters have the expected titles, order, starts and ends (±1 ms) | [`chapters verify-media`](chapters.md) | ffprobe |
| `subtitles` | The SRT/WebVTT parses strictly, cues are in order, none starts after the deliverable ends; warns on overlaps, cues running past the end, reading speed over 20 characters/second, lines over 42 characters or more than 2 lines | [`cutlist`](cutlist.md) SRT reader, [`rollup`](rollup.md) WebVTT reader, [`reflow`](reflow.md) reading limits | nothing (FFmpeg only for the end-of-file comparison) |

## Quick start

```sh
pip install mediawrench        # or: uvx mediawrench qc ...
mediawrench qc master.mov episode.mp4
mediawrench qc master.mov episode.mp4 --target-i -16 --target-tp -1.5 --tolerance 1 \
  --chapters chapters.json --subtitles episode.srt
```

Only the checks you ask for run: without `--target-i` the loudness check is `SKIPPED`,
without `--chapters` the chapter check is `SKIPPED`, and so on. The stream, decode and
duration checks always run when FFmpeg is installed.

### The bundled demo

[`examples/qc/`](../examples/qc/) has an expected chapter list, two SRT files and a script
that makes an 8-second synthetic master plus two exports with FFmpeg's built-in encoders.
`episode.mp4` has three faults seeded by the script (audio trimmed to 6 s, no loudness
gain, chapters dropped), and every FFmpeg call that made it exited 0:

```sh
git clone https://github.com/tianhaogutum/mediawrench && cd mediawrench/examples/qc
python make_media.py                       # writes output/ (git-ignored); needs FFmpeg
mediawrench qc output/master.mkv output/episode.mp4 --target-i -16 --target-tp -1.5 \
  --tolerance 1 --chapters chapters.json --subtitles episode.srt
```

```text
  ✓ PASS     streams    video 1 → 1, audio 1 → 1
  ✓ PASS     decode     deliverable decodes without errors: video 192 frames, audio 282 frames
  ✗ FAIL     duration   audio 6.000 s vs master 8.000 s (off by 2.000 s, allowed 0.410 s)
  ✗ FAIL     loudness   -21.8 LUFS (target -16 ±1), true peak -16.3 dBTP (ceiling -1.5)
  ✗ FAIL     chapters   no chapters in the deliverable, expected 3 (Intro, Interview, Credits)
  ! WARN     subtitles  episode.srt: 4 cues; cue 2 reads at 27.2 chars/s (limit 20)

2 passed · 1 warning · 3 failed
--fail-on unknown: FAIL (exit 1)
```

(Detail lines are trimmed here; the full captured run is
[`assets/qc-demo.svg`](../assets/qc-demo.svg), made by
[`assets/render_terminal.py`](../assets/render_terminal.py).) The same command on
`output/episode-fixed.mp4` with `--subtitles episode-fixed.srt` passes all six checks
and exits 0. Exact LUFS values can differ slightly between FFmpeg builds.

## Statuses and exit codes

| Status | Meaning |
| --- | --- |
| `PASS` | Checked and fine |
| `WARN` | A subtitle style limit was exceeded (reading speed, line length, line count, overlap, cue running past the end) |
| `FAIL` | A proven defect |
| `UNKNOWN` | The check ran but could not decide, for example a codec FFmpeg cannot decode or a WebVTT feature the reader does not support |
| `SKIPPED` | Not requested, or FFmpeg/ffprobe is not installed |

Exit status: `0` when no check is at or above `--fail-on`, `1` when one is, `2` for usage
or input errors (missing file, master and deliverable are the same file, an unreadable
chapter list, an incomplete loudness target). `--fail-on` takes `fail`, `unknown`
(default: FAIL or UNKNOWN exit 1), `warn` (also WARN) or `skipped` (every check must have
actually run and passed).

Without FFmpeg, `qc` does not error out: the media checks are `SKIPPED` with the reason,
the subtitle file is still parsed and linted, and the exit status is 0 unless you pass
`--fail-on skipped`. In CI, install FFmpeg and use `--fail-on skipped` so a missing tool
cannot turn into a silent pass.

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `--format text\|json` | `text` | `json` prints a `mediawrench.qc/v1` document for CI |
| `--fail-on LEVEL` | `unknown` | See above |
| `--quick [SECONDS]` | full decode | Decode only the first 5 s (or SECONDS); faster, but a truncated tail can pass |
| `--allow-drop audio\|video` | none | The deliverable may have fewer streams of this type, e.g. an audio-only podcast from a video master |
| `--ffmpeg PATH`, `--ffprobe PATH` | `PATH` lookup | Explicit executables |
| `--timeout SECONDS` | 60 | Per FFmpeg process |
| `--target-i LUFS --target-tp DBTP --tolerance LU` | off | Loudness target; all three are required together |
| `--chapters FILE` | off | Expected chapters: yt-dlp-style JSON, FFmetadata, Matroska XML or text (`HH:MM:SS title` per line) |
| `--chapters-format FORMAT` | from the file name | `json`, `ffmetadata`, `text` or `matroska` |
| `--chapter-tolerance-ms MS` | 1 | Allowed start/end difference |
| `--subtitles FILE` | off | SRT or WebVTT; repeat for several files |
| `--max-cps N`, `--max-chars N`, `--max-lines N` | 20, 42, 2 | Subtitle reading limits (the first two are `reflow`'s defaults) |

A text chapter list has no end times, so its last chapter is taken to end at the
deliverable's probed duration.

## JSON output

`--format json` prints one object: `schema` (`mediawrench.qc/v1`), `master`,
`deliverable`, `deliverable_duration_seconds`, `tools` (resolved executables and the
FFmpeg version), `checks` (each with `name`, `status`, `summary`, `details`, `source` and
the raw `facts`), `summary` (`counts` per status, `fail_on`, `failed`, `exit_code`) and
`postflight`, the complete [`postflight`](postflight.md) report with every command run.

## Limits

- `qc` reports; it never modifies, renames or deletes either file.
- The decode check proves the streams decode and have the right length. It does not
  compare picture or sound quality, sync within a stream, or codec settings.
- Loudness is measured on the deliverable's first audio stream with FFmpeg `loudnorm`
  (the same filter, parser and pass/fail rule as `mediawrench loudness --verify-only`), in
  any container `postflight` accepts. It is not a certified broadcast loudness meter.
- The WebVTT reader covers `rollup`'s documented subset (`b`/`i`/`u`/`c`/`v`/`lang` tags,
  cue settings, NOTE blocks). Inline timestamps, STYLE and REGION blocks give `UNKNOWN`,
  not `FAIL`. SRT markup (`<i>`, `{\an8}`) is ignored when measuring reading speed.
- Reading-speed and line-length limits are style guidance, so they warn instead of fail;
  use `--fail-on warn` to enforce them.
