from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import json

import pytest
import soundfile as sf

from mediawrench.drift.cli import main
from mediawrench.drift.inspection import export_clips, inspect_text
from mediawrench.drift.review import analyze, edit_anchor
from mediawrench.drift.storage import ReviewError, write_json


def test_inspection_and_local_listening_pairs(speech, tmp_path):
    session = analyze(*speech)
    session["anchors"][1]["target_seconds"] = None
    text = inspect_text(session)
    assert "missing" in text and "pending" in text
    assert "accepted" in text
    destination = tmp_path / "clips"
    result = export_clips(session, destination)
    assert not any(a["accepted"] for a in session["anchors"])
    assert result["clips"][1]["file"] is None
    assert (destination / "start.wav").is_file()
    assert not (destination / "middle.wav").exists()
    info = sf.info(destination / "start.wav")
    assert info.channels == 2 and info.samplerate == 16000 and info.frames == 32000
    assert json.loads((destination / "index.json").read_text())["channels"] == ["reference", "target"]
    with pytest.raises(ReviewError, match="already exists"):
        export_clips(session, destination)


def test_clips_at_recording_edges_are_padded(speech, tmp_path):
    session = analyze(*speech)
    edit_anchor(session, "start", reference_seconds=0, target_seconds=0)
    edit_anchor(session, "end", reference_seconds=44.9, target_seconds=45.1)
    export_clips(session, tmp_path / "edge-clips", seconds=2, rate=8000)
    start, _ = sf.read(tmp_path / "edge-clips/start.wav", always_2d=True)
    assert (start[:8000] == 0).all()
    assert start.shape == (16000, 2)
    end, _ = sf.read(tmp_path / "edge-clips/end.wav", always_2d=True)
    assert (end[-6000:] == 0).all()


def test_inspect_and_clips_cli(speech, tmp_path, capsys):
    path = tmp_path / "review.json"
    write_json(path, analyze(*speech))
    assert main(["inspect", str(path)]) == 0
    assert "reference" in capsys.readouterr().out
    assert main(["clips", str(path), "--out", str(tmp_path / "listen")]) == 0
    assert "listening pair" in capsys.readouterr().out
