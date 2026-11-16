#!/usr/bin/env python3
"""Generate the tiny synthetic media for the `mediawrench qc` demo (needs FFmpeg).

Writes examples/qc/output/ (git-ignored):

  master.mkv          8 s test pattern + 440 Hz tone, FFV1/FLAC, chapters Intro/Interview/Credits
  episode.mp4         a "finished" export with three seeded faults, every FFmpeg call exits 0:
                      audio cut 2 s early, loudness left at the master level, chapters dropped
  episode-fixed.mp4   the same export done right: full audio, gain to -16 LUFS, chapters kept

Only FFmpeg's built-in encoders (ffv1, flac, mpeg4, aac) and lavfi sources are used.
Nothing is downloaded. Then, from examples/qc/:

  mediawrench qc output/master.mkv output/episode.mp4 --target-i -16 --target-tp -1.5 \\
      --tolerance 1 --chapters chapters.json --subtitles episode.srt
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from examples.postflight.synthetic import ffmpeg  # noqa: E402  (runs real FFmpeg, checks exit 0)
from mediawrench.loudness.core import Profile, Runner, Toolchain, parse_loudnorm  # noqa: E402
from mediawrench.postflight.core import resolve_tool  # noqa: E402

HERE = Path(__file__).resolve().parent
CHAPTERS = """;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=2000
title=Intro
[CHAPTER]
TIMEBASE=1/1000
START=2000
END=6500
title=Interview
[CHAPTER]
TIMEBASE=1/1000
START=6500
END=8000
title=Credits
"""
TARGET_LUFS = -16.0


def measured_lufs(path: Path) -> float:
    """Integrated loudness of the first audio stream, via the loudness command's own measurement."""
    runner = Runner(Toolchain(resolve_tool("ffmpeg"), resolve_tool("ffprobe")))
    result = runner.run(runner.ffmpeg_prefix() + ["-i", str(path), "-map", "0:a:0", "-af",
                        Profile(TARGET_LUFS, -1.5, 1).filter() + ":print_format=json", "-f", "null", "-"])
    return float(parse_loudnorm(result.stderr)["input_i"])


def main(out: Path = HERE / "output") -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    meta = out / "chapters.ffmetadata"
    meta.write_text(CHAPTERS, encoding="utf-8")
    paths = {name: out / name for name in ("master.mkv", "episode.mp4", "episode-fixed.mp4")}
    ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=8",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
            "-i", meta, "-map", "0:v", "-map", "1:a", "-map_metadata", "2", "-map_chapters", "2",
            "-c:v", "ffv1", "-c:a", "flac", paths["master.mkv"]])
    common = ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-b:a", "96k"]
    # Faults: audio trimmed to 6 s, no loudness gain, chapters dropped.
    ffmpeg(["-i", paths["master.mkv"], "-map", "0:v", "-map", "0:a", "-map_chapters", "-1",
            "-af", "atrim=0:6", *common, paths["episode.mp4"]])
    gain = TARGET_LUFS - measured_lufs(paths["master.mkv"])
    ffmpeg(["-i", paths["master.mkv"], "-map", "0:v", "-map", "0:a",
            "-af", f"volume={gain:.2f}dB", *common, paths["episode-fixed.mp4"]])
    meta.unlink()
    for path in paths.values():
        shown = path.relative_to(HERE) if path.is_relative_to(HERE) else path
        print(f"wrote {shown} ({path.stat().st_size:,} bytes)")
    return paths


if __name__ == "__main__":
    main()
