"""Offline fixture generation shared by the demo, benchmark and real E2E tests.

Needs real FFmpeg and ffprobe on PATH (with the ffv1, mpeg4, flac and pcm_s16le
encoders and the lavfi color/sine sources); mediawrench never downloads them.
"""
from pathlib import Path
import shutil
import subprocess

from mediawrench.postflight.core import resolve_tool

# Real-media tests skip (never pass vacuously) when FFmpeg is not installed.
FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FFMPEG_MISSING = "real ffmpeg and ffprobe are not both on PATH"


def ffmpeg(arguments, *, expect_success=True):
    command = [resolve_tool("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-y",
               "-filter_threads", "1", "-filter_complex_threads", "1", "-threads", "1"]
    result = subprocess.run(command + list(map(str, arguments)), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=30, check=False)
    if expect_success and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result


def generate(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {name: directory / (name + ".mkv") for name in
             ("source", "healthy", "audio_only", "shortened", "truncated", "zero_video", "short_audio")}
    ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=96x64:r=12:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-threads", "1", paths["source"]])
    ffmpeg(["-i", paths["source"], "-map", "0:v", "-map", "0:a", "-c:v", "mpeg4", "-q:v", "3",
            "-c:a", "flac", "-threads", "1", paths["healthy"]])
    ffmpeg(["-i", paths["source"], "-map", "0:a", "-c:a", "flac", "-threads", "1", paths["audio_only"]])
    ffmpeg(["-i", paths["source"], "-t", "0.5", "-map", "0:v", "-map", "0:a", "-c:v", "mpeg4",
            "-c:a", "flac", "-threads", "1", paths["shortened"]])
    ffmpeg(["-i", paths["source"], "-map", "0:v", "-map", "0:a", "-vf", "select=0",
            "-c:v", "mpeg4", "-c:a", "flac", "-threads", "1", paths["zero_video"]])
    ffmpeg(["-i", paths["source"], "-map", "0:v", "-map", "0:a", "-af", "atrim=duration=0.5",
            "-c:v", "mpeg4", "-c:a", "flac", "-threads", "1", paths["short_audio"]])
    # A seeded file-level fault applied AFTER a successful encoder exit. Keep
    # headers and enough initial media for a header-only baseline to pass.
    encoded = paths["healthy"].read_bytes()
    paths["truncated"].write_bytes(encoded[:len(encoded) // 2])
    return paths


def generate_mapping(directory: Path) -> dict[str, Path]:
    """Unequal audio spans, metadata hints and a third, video stream (FFmpeg 7)."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {name: directory / f"{name}.mkv" for name in
             ("source", "reordered", "kept_long", "shortened")}
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=16000:duration=0.5",
            "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=8:d=2",
            "-map", "0:a", "-map", "1:a", "-map", "2:v",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=deu",
            "-disposition:a:0", "default", "-disposition:a:1", "forced",
            "-c:a", "pcm_s16le", "-c:v", "ffv1", "-threads", "1", paths["source"]])
    ffmpeg(["-i", paths["source"], "-map", "0:1", "-map", "0:0", "-map", "0:2",
            "-c", "copy", paths["reordered"]])
    ffmpeg(["-i", paths["source"], "-map", "0:0", "-map", "0:2",
            "-c", "copy", paths["kept_long"]])
    ffmpeg(["-i", paths["reordered"], "-map", "0", "-filter:a:1", "atrim=duration=0.5",
            "-c:v", "copy", "-c:a", "pcm_s16le", "-threads", "1", paths["shortened"]])
    return paths
