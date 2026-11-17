from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import math

import pytest
import soundfile as sf

from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor


def test_short_reference_does_not_decode_a_long_unused_target_tail(speech, tmp_path):
    reference = tmp_path / "short.wav"
    ffmpeg("-i", speech[0], "-t", "5", "-c:a", "pcm_s16le", reference)
    session = analyze(reference, speech[1], window_seconds=1, search_seconds=0.3)
    scale = 16000 / 15992
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"],
                    target_seconds=scale * anchor["reference_seconds"] + 0.2, accepted=True)
    result = render(session, tmp_path / "short-corrected.wav")
    expected = math.ceil((scale * 5 + 0.2 + 2) * 16000)
    assert result["render"]["source_decode_limit_frames"] == expected
    assert result["output"]["decoded_source_frames"] == expected
    assert result["output"]["decoded_source_frames"] < 8 * 16000
    assert sf.info(tmp_path / "short-corrected.wav").frames == 5 * 16000
    assert result["verification"]["max_abs_residual_ms"] < 0.1


def test_corrupt_unused_tail_does_not_invalidate_a_good_short_output(speech, tmp_path):
    reference = tmp_path / "short.wav"
    target = tmp_path / "bad-unused-tail.flac"
    ffmpeg("-i", speech[0], "-t", "5", "-c:a", "pcm_s16le", reference)
    data = bytearray(speech[1].read_bytes())
    data[-4096:] = bytes(4096)
    target.write_bytes(data)
    session = analyze(reference, target, window_seconds=1, search_seconds=0.3)
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"],
                    target_seconds=16000 / 15992 * anchor["reference_seconds"] + 0.2, accepted=True)
    result = render(session, tmp_path / "good-short.wav")
    assert result["render"]["source_decode_limit_frames"] is not None
    assert result["verification"]["max_abs_residual_ms"] < 0.1


def test_short_decode_limit_counts_samples_for_delayed_aac(speech, tmp_path):
    reference, target = tmp_path / "short.wav", tmp_path / "late.mp4"
    ffmpeg("-i", speech[0], "-t", "5", "-c:a", "pcm_s16le", reference)
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=14",
           "-itsoffset", "2", "-i", speech[0], "-map", "0:v", "-map", "1:a", "-t", "14",
           "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", target)
    session = analyze(reference, target, window_seconds=1, search_seconds=0.3)
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"], target_seconds=anchor["reference_seconds"] + 1024 / 16000,
                    accepted=True)
    result = render(session, tmp_path / "short-aac.wav")
    assert result["output"]["decoded_source_frames"] == math.ceil((5 + 1024 / 16000 + 2) * 16000)
    assert result["verification"]["max_abs_residual_ms"] < 0.2
