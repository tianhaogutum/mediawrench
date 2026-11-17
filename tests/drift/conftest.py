import shutil

import pytest

from .tools import ffmpeg, speech_like


@pytest.fixture(scope="session")
def speech(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("real ffmpeg and ffprobe are required on PATH")
    directory = tmp_path_factory.mktemp("speech")
    reference = speech_like(directory / "reference.wav")
    target = directory / "target.flac"
    # Independently generated with FFmpeg, not the adapter's SoXR renderer.
    # Events obey target = (16000 / 15992) * reference + 0.2.
    ffmpeg("-i", reference, "-af", "asetrate=15992,aresample=16000,adelay=3200S:all=1",
           "-c:a", "flac", target)
    return reference, target
