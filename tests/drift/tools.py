"""Shared helpers for the ``mediawrench drift`` suites.

The suites need the ``drift`` extra (NumPy, SciPy, soxr, SoundFile) and real
``ffmpeg``/``ffprobe`` on PATH; modules call ``require_runtime()`` first and are
skipped (never faked) when either is missing. Test audio is generated with
FFmpeg's ``anoisesrc``: seeded pink noise gated by an irregular envelope, so it
has broadband, non-repeating content and pauses like speech. Nothing is
downloaded and no recorded media is committed.
"""
import shutil
import subprocess

import pytest

RUNTIME = ("numpy", "scipy", "soxr", "soundfile")

# 45 s, 16 kHz mono, irregular on/off envelope (three incommensurate rates).
SPEECH_LIKE = ("anoisesrc=d={seconds}:c=pink:r={rate}:a=0.5:s={seed}",
               "aeval='val(0)*(0.12+0.88*gt(sin(2*PI*2.7*t)+0.6*sin(2*PI*0.41*t+1.3)"
               "+0.3*sin(2*PI*5.3*t),0.15))',lowpass=f=6000")


def require_runtime():
    for name in RUNTIME:
        pytest.importorskip(name, reason="needs the drift extra: pip install 'mediawrench[drift]'")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("real ffmpeg and ffprobe are required on PATH", allow_module_level=True)


def ffmpeg(*args):
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                           "-filter_threads", "1", *map(str, args)],
                          check=True, capture_output=True, timeout=45)


def speech_like(path, *, seconds=45, rate=16000, channels=1, seed=20261117, codec=("-c:a", "pcm_s16le")):
    """Write deterministic speech-like test audio to ``path`` with FFmpeg."""
    source, shape = SPEECH_LIKE
    ffmpeg("-f", "lavfi", "-i", source.format(seconds=seconds, rate=48000, seed=seed),
           "-af", shape, "-ar", rate, "-ac", channels, *codec, path)
    return path
