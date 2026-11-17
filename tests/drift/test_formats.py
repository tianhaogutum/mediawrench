from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import numpy as np
import pytest
import soundfile as sf

from mediawrench.drift.media import probe, window
from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor, fit


def accept_truth(session, scale=1.0, offset=0.0):
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"],
                    target_seconds=scale * anchor["reference_seconds"] + offset, accepted=True)
    return session


def test_negative_offset_and_negative_drift(speech, tmp_path):
    target = tmp_path / "earlier.flac"
    ffmpeg("-i", speech[0], "-af", "asetrate=16008,aresample=16000,atrim=start=0.35,asetpts=PTS-STARTPTS",
           "-c:a", "flac", target)
    scale, offset = 16000 / 16008, -0.35
    session = accept_truth(analyze(speech[0], target, offset_hint=-0.35), scale, offset)
    model = fit(session)
    assert model["drift_ppm"] < 0
    assert model["fixed_target_delay_seconds"] == pytest.approx(offset)
    output = tmp_path / "aligned.wav"
    result = render(session, output)
    leading = round(-offset / scale * 16000)
    assert result["output"]["leading_silence_frames"] == leading
    with sf.SoundFile(output) as audio:
        assert np.max(np.abs(audio.read(leading))) == 0
    assert result["verification"]["max_abs_residual_ms"] < 0.1


def test_different_rates_and_opposite_polarity_stereo(speech, tmp_path):
    target = tmp_path / "stereo.flac"
    ffmpeg("-i", speech[0], "-af",
           "asetrate=15992,aresample=44100,adelay=8820S:all=1,pan=stereo|c0=c0|c1=-1*c0",
           "-c:a", "flac", target)
    session = accept_truth(analyze(speech[0], target, target_channel=1), 16000 / 15992, 0.2)
    assert session["target"]["sample_rate"] == 44100
    assert all(a["measurement"]["polarity"] == -1 for a in session["anchors"])
    output = tmp_path / "stereo.wav"
    result = render(session, output, subtype="PCM_24")
    assert result["output"]["sample_rate"] == 16000
    assert result["output"]["channels"] == 2
    assert sf.info(output).subtype == "PCM_24"
    audio, _ = sf.read(output, frames=160000, always_2d=True)
    np.testing.assert_allclose(audio[:, 0], -audio[:, 1], atol=2e-7)
    assert result["verification"]["usable_probe_count"] == 5
    assert result["verification"]["max_abs_residual_ms"] < 0.15


def test_ogg_source_and_flac_roundtrip(tmp_path):
    # 7.1 s of 44.1 kHz stereo Ogg Vorbis (FFmpeg's built-in encoder), generated here.
    source = speech_like(tmp_path / "radio.ogg", seconds=7.1, rate=44100, channels=2, seed=7,
                         codec=("-c:a", "vorbis", "-strict", "-2"))
    target = tmp_path / "radio.flac"
    ffmpeg("-i", source, "-ar", "48000", "-c:a", "flac", target)
    session = accept_truth(analyze(source, target, window_seconds=1,
                                  search_seconds=0.3))
    output = tmp_path / "radio.wav"
    result = render(session, output)
    assert result["output"]["sample_rate"] == 44100
    assert result["output"]["channels"] == 2
    assert result["verification"]["max_abs_residual_ms"] < 0.2


def test_camera_mp4_audio_stream_selection(speech, tmp_path):
    camera = tmp_path / "camera.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=12",
           "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
           "-i", speech[0], "-map", "0:v", "-map", "1:a", "-map", "2:a",
           "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
           "-c:a", "aac", "-b:a", "96k", camera)
    silent = analyze(speech[0], camera, window_seconds=2, search_seconds=0.5)
    assert not any(a["suggested_usable"] for a in silent["anchors"])
    assert all(a["target_seconds"] is None for a in silent["anchors"])
    session = analyze(speech[0], camera, target_stream=1, window_seconds=2, search_seconds=0.5)
    assert all(abs(a["measurement"]["delay_seconds"]) < 0.001 for a in session["anchors"])
    accept_truth(session)
    result = render(session, tmp_path / "camera.wav")
    assert result["output"]["trailing_silence_frames"] > 30 * 16000
    assert result["verification"]["max_abs_residual_ms"] < 0.2


def test_audio_stream_start_timestamp_is_not_a_manual_offset(speech, tmp_path):
    camera = tmp_path / "late-audio.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=14",
           "-itsoffset", "2", "-i", speech[0], "-map", "0:v", "-map", "1:a",
           "-t", "14", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
           "-c:a", "aac", camera)
    metadata = probe(camera)
    assert metadata["stream_start_seconds"] > 1.8
    # The offset AAC encode retains one 1024-sample priming frame (64 ms),
    # unlike an ordinary zero-origin MP4. A complete independent FFmpeg
    # decode defines sample zero. Seeking must agree with that exact decode.
    decoded = tmp_path / "decoded.wav"
    ffmpeg("-i", camera, "-map", "0:a:0", "-c:a", "pcm_f32le", decoded)
    reference = window(probe(speech[0]), 4, 2)
    target = window(metadata, 4, 2)
    from mediawrench.drift.review import matched_window
    full = window(probe(decoded), 4, 2)
    assert matched_window(full, target, 8000, 0.5)["delay_seconds"] == pytest.approx(0, abs=1e-8)
    match = matched_window(reference, target, 8000, 0.5)
    assert match["delay_seconds"] == pytest.approx(1024 / 16000, abs=0.002)
    session = accept_truth(analyze(speech[0], camera, window_seconds=2, search_seconds=0.5),
                           offset=1024 / 16000)
    result = render(session, tmp_path / "late-corrected.wav")
    assert result["verification"]["max_abs_residual_ms"] < 0.2


def test_tiny_clock_correction_moves_real_impulse_samples(tmp_path):
    rate, scale = 8000, 1.000005
    reference, target = tmp_path / "impulse-ref.wav", tmp_path / "impulse-target.wav"
    audio = np.zeros(120 * rate, dtype=np.float32)
    audio[100 * rate] = 0.5
    sf.write(reference, audio, rate, subtype="FLOAT")
    audio[100 * rate] = 0
    audio[100 * rate + 4] = 0.5  # exactly 5 ppm of accumulated error at 100 s
    sf.write(target, audio, rate, subtype="FLOAT")
    session = accept_truth(analyze(reference, target), scale)
    output = tmp_path / "five-ppm.wav"
    result = render(session, output, verify=False)
    corrected, _ = sf.read(output)
    assert np.argmax(np.abs(corrected)) == 100 * rate
    assert result["render"]["effective_input_rate"] == pytest.approx(8000.04)
