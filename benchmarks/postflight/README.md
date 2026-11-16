# `mediawrench postflight`: decoded-presence benchmark

Does a header-only check catch broken transcodes? This benchmark compares a
header-only baseline with `mediawrench postflight` on six tiny synthetic cases.

Run `python benchmarks/postflight/run.py` from the repository root (FFmpeg and
ffprobe must be on PATH). It generates synthetic media in a temporary directory,
writes `benchmarks/postflight/results.json`, and exits nonzero if any expected
verdict differs. No network, copyrighted media, model, server or replacement step
is involved. All encoder commands are in
[`examples/postflight/synthetic.py`](../../examples/postflight/synthetic.py); each
checks a real successful FFmpeg return code.

Source: 2 seconds of 96×64 blue video at 12 fps, FFV1, plus a 440 Hz / 16 kHz PCM
mono tone in Matroska. Healthy output: MPEG-4 video + FLAC audio. Faults:

- `audio_only`: intentionally map only audio, successful encoder exit.
- `truncated`: retain the first half of a healthy file **after** successful encode;
  this is an injected filesystem fault, not a claim that FFmpeg emitted truncation.
- `shortened`: encode only 0.5 seconds, successful encoder exit.
- `zero_video`: `select=0` removes all video frames while Matroska retains the video
  header and audio. The actual encoder succeeds; header duration remains 2 seconds.
- `short_audio`: trim only audio to 0.5 seconds; full video keeps container duration
  at 2 seconds, successful encoder exit.

The specified baseline accepts successful-encode, nonempty, probe-readable output
with at least one AV stream header. A stronger extra baseline also requires exact
source/output AV header counts. Neither baseline decodes. Postflight uses default
full decode and 0.25 s + 2% source-duration tolerance.

Measured with Python 3.14 and FFmpeg/ffprobe 7.0.2 on Linux x86_64:

| Candidate | Header baseline | + exact AV counts | Postflight | Truth |
|---|---|---|---|---|
| healthy | accept | accept | PASS | healthy |
| audio_only | accept | reject | FAIL | faulty |
| truncated | accept | accept | FAIL | faulty |
| shortened | accept | accept | FAIL | faulty |
| zero_video | accept | accept | FAIL | faulty |
| short_audio | accept | accept | FAIL | faulty |

Positive means **reject a seeded fault**. No UNKNOWN verdict occurred:

| Method | TP | FN | FP | TN |
|---|---:|---:|---:|---:|
| Header baseline | 0 | 5 | 0 | 1 |
| Exact AV header counts | 1 | 4 | 0 | 1 |
| Full postflight | 5 | 0 | 0 | 1 |

The JSON records actual versions, each candidate's bytes/hash, all check facts,
thresholds and confusion counts. Temporary paths and process logs are deliberately
not persisted there. Matroska segment IDs make hashes vary across runs; verdicts,
stream/decoded durations and confusion counts are the reproduction targets.

Six designed cases show detection of these faults, not statistical accuracy,
speedup, security, perceptual fidelity or proof of full content equivalence. The
healthy candidate is the only negative control. A stricter format-specific
baseline could detect some faults by other means; the added count-only baseline
makes that distinction explicit.
