from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import copy
from collections import namedtuple

import numpy as np
import pytest
import soundfile as sf

from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor, validate_session
from mediawrench.drift.media import probe
from mediawrench.drift.storage import ReviewError


def accept_known(session, scale=16000 / 15992, offset=0.2):
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"],
                    target_seconds=scale * anchor["reference_seconds"] + offset, accepted=True)
    return session


def test_real_corrupt_flac_retains_failed_anchor_and_refuses_partial_render(speech, tmp_path):
    target = tmp_path / "corrupted.flac"
    data = bytearray(speech[1].read_bytes())
    middle = len(data) // 2
    data[middle:middle + 4096] = bytes(4096)
    target.write_bytes(data)
    session = analyze(speech[0], target)
    failed = [anchor for anchor in session["anchors"] if anchor["target_seconds"] is None]
    assert failed
    assert all("could not be measured" in a["measurement"]["warnings"][0] for a in failed)
    assert not any(anchor["accepted"] for anchor in session["anchors"])
    accept_known(session)
    with pytest.raises(ReviewError, match="FFmpeg decoding failed"):
        render(session, tmp_path / "not-published.wav", verify=False)
    assert not (tmp_path / "not-published.wav").exists()
    assert not list(tmp_path.glob(".drift-review-*"))


def test_nonfinite_float_audio_is_not_fed_to_the_resampler(tmp_path):
    reference, target = tmp_path / "ref.wav", tmp_path / "nonfinite.wav"
    sf.write(reference, np.ones(10 * 8000, dtype=np.float32) * 0.1, 8000, subtype="FLOAT")
    sf.write(target, np.full(10 * 8000, np.nan, dtype=np.float32), 8000, subtype="FLOAT")
    session = analyze(reference, target, window_seconds=1, search_seconds=0.2)
    assert all(a["target_seconds"] is None for a in session["anchors"])
    accept_known(session, 1, 0)
    with pytest.raises(ReviewError, match="NaN or infinite"):
        render(session, tmp_path / "bad.wav", verify=False)
    assert not (tmp_path / "bad.wav").exists()


def test_integer_output_refuses_clipping_but_float_preserves_it(speech, tmp_path):
    target = tmp_path / "hot.wav"
    data, rate = sf.read(speech[0], dtype="float32")
    sf.write(target, data * 10, rate, subtype="FLOAT")
    session = accept_known(analyze(speech[0], target), 1, 0)
    with pytest.raises(ReviewError, match="integer PCM range"):
        render(session, tmp_path / "clipped.wav", subtype="PCM_24", verify=False)
    assert not (tmp_path / "clipped.wav").exists()
    result = render(session, tmp_path / "preserved.wav", verify=False)
    assert result["output"]["peak_absolute"] > 1


def test_disk_space_is_checked_before_decoding(speech, tmp_path, monkeypatch):
    session = accept_known(analyze(*speech))
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("mediawrench.drift.render.shutil.disk_usage", lambda path: Usage(100, 99, 1))
    with pytest.raises(ReviewError, match="insufficient free disk"):
        render(session, tmp_path / "no-space.wav")
    assert not (tmp_path / "no-space.wav").exists()


def test_finite_extreme_input_cannot_overflow_into_published_float_audio(tmp_path):
    from mediawrench.drift.review import SCHEMA
    reference, target = tmp_path / "ref.wav", tmp_path / "huge.wav"
    time = np.arange(80000) / 8000
    audio = (np.sign(np.sin(2 * np.pi * 1000 * time)) * np.float32(3e38)).astype("float32")
    assert np.isfinite(audio).all()
    sf.write(target, audio, 8000, subtype="FLOAT")
    sf.write(reference, np.zeros(160000, dtype="float32"), 16000, subtype="FLOAT")
    session = {"schema": SCHEMA, "reference": probe(reference), "target": probe(target),
               "anchors": [{"id": "start", "reference_seconds": 1, "target_seconds": 1, "accepted": True},
                           {"id": "end", "reference_seconds": 8, "target_seconds": 8, "accepted": True}]}
    with pytest.raises(ReviewError, match="resampling produced non-finite"):
        render(session, tmp_path / "invalid.wav", report=tmp_path / "invalid.json", verify=False)
    assert not (tmp_path / "invalid.wav").exists()
    assert not (tmp_path / "invalid.json").exists()


@pytest.mark.parametrize("field,value", [("measurement", 2), ("note", {}), ("origin", []),
                                        ("measurement", {"confidence": float("inf")}),
                                        ("measurement", {"warnings": "not a list"})])
def test_malformed_optional_review_fields_are_rejected(speech, field, value):
    session = analyze(*speech)
    bad = copy.deepcopy(session)
    bad["anchors"][0][field] = value
    with pytest.raises(ReviewError):
        validate_session(bad)
