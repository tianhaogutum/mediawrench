from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import pytest

from mediawrench.drift.media import probe, window
from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor, fit, matched_window
from mediawrench.drift.storage import ReviewError


@pytest.mark.parametrize("suffix,options", [
    ("rf64.wav", ["-c:a", "pcm_s24le", "-rf64", "always", "-write_bext", "1",
                  "-metadata", "time_reference=123456789"]),
    ("float.wav", ["-c:a", "pcm_f32le"]),
    ("sony.w64", ["-c:a", "pcm_s24le", "-f", "w64"]),
    ("recorder.aiff", ["-c:a", "pcm_s24be"]),
    ("compressed.opus", ["-c:a", "libopus", "-b:a", "48k"]),
    ("compressed.m4a", ["-c:a", "aac", "-b:a", "96k"]),
    ("compressed.mp3", ["-c:a", "libmp3lame", "-q:a", "3"]),
    ("opus.mkv", ["-c:a", "libopus", "-b:a", "64k"]),
    ("aac.mkv", ["-c:a", "aac", "-b:a", "96k"]),
    ("audio.mov", ["-c:a", "aac", "-b:a", "96k"]),
    ("transport.ts", ["-c:a", "aac", "-b:a", "96k", "-f", "mpegts"]),
    ("raw.aac", ["-c:a", "aac", "-b:a", "96k", "-f", "adts"]),
])
def test_seeked_windows_match_full_ffmpeg_decode_across_real_formats(speech, tmp_path, suffix, options):
    source = tmp_path / suffix
    decoded = tmp_path / "decoded.wav"
    ffmpeg("-i", speech[0], "-t", "12", *options, source)
    ffmpeg("-i", source, "-map", "0:a:0", "-c:a", "pcm_f32le", decoded)
    direct, full = probe(source), probe(decoded)
    for start in (0.0, 4.0, 9.0):
        result = matched_window(window(full, start, 1), window(direct, start, 1), 8000, 0.2)
        assert result["success"]
        assert result["delay_seconds"] == pytest.approx(0, abs=0.00015)
        assert abs(result["similarity"]) > 0.99


def test_real_removed_section_is_rejected_as_non_linear(speech, tmp_path):
    target = tmp_path / "cut.flac"
    graph = ("[0:a]asplit=2[a][b];[a]atrim=end=20,asetpts=PTS-STARTPTS[a1];"
             "[b]atrim=start=20.1,asetpts=PTS-STARTPTS[b1];"
             "[a1][b1]concat=n=2:v=0:a=1[out]")
    ffmpeg("-i", speech[0], "-filter_complex", graph, "-map", "[out]", "-c:a", "flac", target)
    session = analyze(speech[0], target)
    for anchor in session["anchors"]:
        assert anchor["target_seconds"] is not None
        edit_anchor(session, anchor["id"], accepted=True)
    model = fit(session)
    assert not model["renderable"]
    assert model["fit_residual_max_ms"] > 20
    with pytest.raises(ReviewError, match="linear residual"):
        render(session, tmp_path / "cannot-fix-cut.wav")
    assert not (tmp_path / "cannot-fix-cut.wav").exists()
