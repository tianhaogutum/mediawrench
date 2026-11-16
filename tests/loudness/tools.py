"""Real FFmpeg discovery for the loudness suites.

Tests use ``FFMPEG``/``FFPROBE`` from the environment when set, otherwise ``ffmpeg``
and ``ffprobe`` from PATH. Suites that need them are skipped (never mocked into
passing) when either executable is missing.
"""
import os
import shutil
import unittest


def _find(variable, name):
    configured = os.environ.get(variable)
    return shutil.which(configured) if configured else shutil.which(name)


FFMPEG = _find("FFMPEG", "ffmpeg")
FFPROBE = _find("FFPROBE", "ffprobe")
MISSING = "real ffmpeg and ffprobe are required (on PATH, or set FFMPEG/FFPROBE)"
requires_ffmpeg = unittest.skipUnless(FFMPEG and FFPROBE, MISSING)
