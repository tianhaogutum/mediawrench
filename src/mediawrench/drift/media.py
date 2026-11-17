"""FFmpeg format handling, bounded windows, and a timed PCM stream."""

from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import numpy as np

from .storage import ReviewError, finite, fingerprint, integer


def executable(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise ReviewError(f"{name} is required on PATH; install FFmpeg first")
    return found


def run(command: list[str], *, timeout: float = 45) -> bytes:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"{Path(command[0]).name} timed out after {timeout:g} seconds") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace")[-3000:].strip()
        raise ReviewError(f"{Path(command[0]).name} failed: {message}")
    return result.stdout


def probe(path: Path, *, stream: int = 0, channel: int = 0) -> dict:
    path = path.expanduser().resolve()
    identity = fingerprint(path)
    stream = integer(stream, "audio stream", 0, 31)
    channel = integer(channel, "analysis channel", 0, 63)
    raw = run([executable("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
               "-show_entries", "stream=codec_type,sample_rate,channels,duration,duration_ts,time_base,start_time,codec_name:format=duration,start_time",
               "-of", "json", str(path)])
    metadata = json.loads(raw)
    streams = [s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"]
    if stream >= len(streams):
        raise ReviewError(f"audio stream {stream} does not exist in {path}")
    selected = streams[stream]
    rate = integer(int(selected.get("sample_rate", 0)), "sample rate", 1000, 384000)
    channels = integer(selected.get("channels", 0), "channel count", 1, 64)
    if channel >= channels:
        raise ReviewError(f"channel {channel} does not exist (file has {channels} channels)")
    duration = selected.get("duration") or metadata.get("format", {}).get("duration")
    try:
        duration = finite(float(duration), "audio duration", minimum=0.001)
    except (TypeError, ValueError) as exc:
        raise ReviewError(f"cannot determine a finite audio duration: {path}") from exc
    # Prefer stream time-base arithmetic to rounded decimal duration for PCM.
    try:
        duration = float(int(selected["duration_ts"]) * Fraction(selected["time_base"]))
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    duration = finite(duration, "audio duration", minimum=0.001)
    stream_start = finite(float(selected.get("start_time", 0) or 0), "audio start timestamp")
    format_start = finite(float(metadata.get("format", {}).get("start_time", 0) or 0), "container start timestamp")
    time_base = selected.get("time_base", "1/" + str(rate))
    try:
        sample_seek = Fraction(time_base) > Fraction(1, rate)
    except (ValueError, ZeroDivisionError) as exc:
        raise ReviewError("invalid audio stream time base") from exc
    return {"path": str(path), "identity": identity, "sample_rate": rate,
            "channels": channels, "duration_seconds": duration,
            "audio_stream": stream, "analysis_channel": channel,
            "container_start_seconds": format_start,
            "stream_start_seconds": stream_start,
            "time_base": time_base, "sample_count_seek": sample_seek,
            "codec": selected.get("codec_name", "unknown")}


def ffmpeg_base() -> list[str]:
    return [executable("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error",
            "-xerror", "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file,pipe"]


def window(media: dict, start: float, seconds: float, *, rate: int = 8000,
           timeout: float = 45) -> np.ndarray:
    start = finite(start, "window start", minimum=0)
    seconds = finite(seconds, "window duration", minimum=0.05, maximum=30)
    rate = integer(rate, "analysis sample rate", 1000, 48000)
    # Seek with codec preroll, retain original PTS, then trim in libavfilter.
    # Direct accurate input seeking loses/misapplies AAC priming at packet
    # boundaries (reproduced offsets: 24–64 ms) and cold-starts MP3 reservoirs.
    # Two seconds is bounded regardless of recording length.
    stream_offset = max(0.0, media.get("stream_start_seconds", 0)
                        - media.get("container_start_seconds", 0))
    command = ffmpeg_base()
    if media.get("sample_count_seek", False):
        # Matroska can timestamp 48-kHz Opus in whole milliseconds and report
        # a stream start different from its first decoded sample. Counting
        # from the decoder origin is exact and still uses bounded RAM.
        first = round(start * media["sample_rate"])
        last = first + round(seconds * media["sample_rate"])
        trim = f"atrim=start_sample={first}:end_sample={last}"
    else:
        preroll_seek = max(0.0, start + stream_offset - 2.0)
        command += ["-copyts", "-noaccurate_seek"]
        if preroll_seek > 0:
            command += ["-ss", f"{preroll_seek:.12f}"]
        absolute_start = start + media.get("stream_start_seconds", 0)
        trim = f"atrim=start={absolute_start:.12f}:duration={seconds:.12f}"
    filters = f"{trim},asetpts=PTS-STARTPTS,pan=mono|c0=c{media['analysis_channel']}"
    command += ["-i", media["path"], "-t", f"{seconds:.12f}",
              "-map", f"0:a:{media['audio_stream']}",
              "-vn", "-sn", "-dn", "-af", filters,
              "-ar", str(rate), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
    raw = run(command, timeout=timeout)
    if len(raw) > (math.ceil(seconds * rate) + 4096) * 4:
        raise ReviewError("decoder exceeded the requested analysis window")
    if len(raw) % 4:
        raise ReviewError("decoder returned an incomplete float32 PCM frame")
    audio = np.frombuffer(raw, dtype="<f4")
    if not np.all(np.isfinite(audio)):
        raise ReviewError("audio contains NaN or infinite samples")
    return audio


@contextmanager
def pcm_stream(media: dict, *, timeout: float, temp_dir: Path, max_frames: int | None = None,
               block_frames: int = 16384):
    """Yield a frame iterator; guarantee timeout, bounded RAM, and child cleanup."""
    command = ffmpeg_base() + ["-i", media["path"], "-map", f"0:a:{media['audio_stream']}",
              "-vn", "-sn", "-dn"]
    if max_frames is not None:
        max_frames = integer(max_frames, "decode frame limit", 1, 2**63 - 1)
        # Count samples even when audio starts later than the container.
        command += ["-af", f"atrim=end_sample={max_frames}"]
    command += ["-ar", str(media["sample_rate"]), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
    frame_bytes = 4 * media["channels"]
    block_frames = integer(block_frames, "input block frames", 1, 16384)
    timed_out = threading.Event()
    with tempfile.TemporaryFile(dir=temp_dir) as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        def kill_on_timeout():
            timed_out.set()
            process.kill()
        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
        def blocks():
            assert process.stdout is not None
            while True:
                raw = process.stdout.read(block_frames * frame_bytes)
                if not raw:
                    break
                if len(raw) % frame_bytes:
                    raise ReviewError("decoder returned an incomplete multichannel PCM frame")
                block = np.frombuffer(raw, dtype="<f4").reshape(-1, media["channels"])
                if not np.all(np.isfinite(block)):
                    raise ReviewError("audio contains NaN or infinite samples")
                yield block
            process.wait(timeout=5)
            if timed_out.is_set():
                raise ReviewError(f"render decoding timed out after {timeout:g} seconds")
            if process.returncode:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 3000))
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise ReviewError(f"FFmpeg decoding failed: {detail}")
        try:
            yield blocks()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
