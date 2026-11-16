# mediawrench documentation

One page per command. Each page starts with the problem the command solves and a quick
start, followed by the full reference. Run `mediawrench <command> --help` for the flags.
Quick starts use the sample inputs under [`examples/`](../examples/), so run them from a
clone of the repository (`git clone https://github.com/tianhaogutum/mediawrench`).
Commands marked with FFmpeg need `ffmpeg`/`ffprobe` installed separately (for example
`apt install ffmpeg` or `brew install ffmpeg`); mediawrench never downloads them.

## Subtitles and captions

| Command | Use it to | FFmpeg |
| --- | --- | --- |
| [`mediawrench rollup`](rollup.md) | Repair roll-up WebVTT captions into clean SRT/VTT with token provenance |  |
| [`mediawrench reflow`](reflow.md) | Reflow ASR word timestamps into readable SRT cues (width, duration, reading speed) |  |
| [`mediawrench audacity`](audacity.md) | Round-trip SRT/WebVTT timing edits through Audacity labels with a loss audit |  |

## Transcripts and speech

| Command | Use it to | FFmpeg |
| --- | --- | --- |
| [`mediawrench asr-audit`](asr-audit.md) | Flag suspicious ASR segments (repeats, loops, off-VAD speech) for human review |  |
| [`mediawrench speakers`](speakers.md) | Join diarization RTTM with ASR JSON into speaker-labelled JSON, Markdown or SRT |  |
| [`mediawrench cutlist`](cutlist.md) | Turn SRT cue selections into auditable LosslessCut cutlists, CSV and provenance | optional (`--ffprobe`) |
| [`mediawrench chunks`](chunks.md) | Split PCM WAV into sample-exact ASR chunks and stitch chunk transcripts back |  |

## Audio and video files

| Command | Use it to | FFmpeg |
| --- | --- | --- |
| [`mediawrench qc`](qc.md) | Check a finished file against its master before delivery: decode, duration, loudness, chapters, subtitles | required for media checks |
| [`mediawrench loudness`](loudness.md) | Loudness-normalize audio; publish only after the final file passes LUFS/true peak | required |
| [`mediawrench postflight`](postflight.md) | Decode a transcode against its source stream by stream: PASS, FAIL or UNKNOWN | required |
| [`mediawrench chapters`](chapters.md) | Convert chapters between JSON, FFmetadata, text and Matroska XML, loss-audited | optional (`verify-media`) |

Third-party attribution is in [THIRD_PARTY.md](../THIRD_PARTY.md).
