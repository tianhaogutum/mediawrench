from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import copy
import json

import numpy as np
import pytest
import soundfile as sf

import mediawrench
from mediawrench.drift.cli import main
from mediawrench.drift.media import probe
from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor, fit, fit_csv, load_session
from mediawrench.drift.storage import ReviewError, write_json


def approved_truth(reference, target, scale=16000 / 15992, offset=0.2):
    session = analyze(reference, target)
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"], target_seconds=scale * anchor["reference_seconds"] + offset,
                    accepted=True, note="known FFmpeg fixture ground truth")
    return session


def test_speech_like_proposals_fit_and_render(speech, tmp_path):
    reference, target = speech
    session = analyze(reference, target)
    # The vendored ChronoSync subset records the upstream version it was copied from.
    assert session["software"] == {"drift_review": mediawrench.__version__, "chronosync": "0.1.0b0"}
    assert [a["id"] for a in session["anchors"]] == ["start", "middle", "end"]
    assert not any(a["accepted"] for a in session["anchors"])
    with pytest.raises(ReviewError, match="accepted"):
        fit(session)
    for anchor in session["anchors"]:
        assert anchor["target_seconds"] is not None
        assert anchor["target_seconds"] - anchor["reference_seconds"] == pytest.approx(
            0.2 + (16000 / 15992 - 1) * anchor["reference_seconds"], abs=0.002)
        edit_anchor(session, anchor["id"], accepted=True)
    model = fit(session)
    assert model["fixed_target_delay_seconds"] == pytest.approx(0.2, abs=0.002)
    assert model["drift_ppm"] == pytest.approx((16000 / 15992 - 1) * 1e6, abs=60)
    assert "residual_ms" in fit_csv(model)
    output = tmp_path / "corrected.wav"
    result = render(session, output, report=tmp_path / "report.json")
    assert sf.info(output).frames == sf.info(reference).frames
    assert result["verification"]["usable_probe_count"] >= 4
    assert result["verification"]["max_abs_residual_ms"] < 2


def test_manual_truth_clock_rate_and_signed_offset(speech, tmp_path):
    session = approved_truth(*speech)
    model = fit(session)
    assert model["scale"] == pytest.approx(16000 / 15992, abs=1e-12)
    assert model["fixed_target_delay_seconds"] == pytest.approx(0.2, abs=1e-12)
    assert model["fit_residual_max_ms"] < 1e-8
    output = tmp_path / "truth.wav"
    report = render(session, output)
    assert report["render"]["effective_input_rate"] == pytest.approx(16000 * model["scale"])
    assert report["verification"]["max_abs_residual_ms"] < 0.1


def test_unaccepted_bad_anchor_does_not_move_fit(speech):
    session = approved_truth(*speech)
    session["anchors"][1]["accepted"] = False
    session["anchors"][1]["target_seconds"] += 0.5
    model = fit(session)
    assert model["accepted_anchor_count"] == 2
    assert model["scale"] == pytest.approx(16000 / 15992)
    assert abs(model["anchors"][1]["residual_ms"]) > 490
    assert any("two points" in warning for warning in model["warnings"])


def test_nonlinear_or_mismatched_anchor_blocks_linear_render(speech, tmp_path):
    session = approved_truth(*speech)
    session["anchors"][1]["target_seconds"] += 0.1
    model = fit(session)
    assert not model["renderable"]
    assert model["fit_residual_max_ms"] > 60
    with pytest.raises(ReviewError, match="cannot render"):
        render(session, tmp_path / "bad.wav")
    assert not (tmp_path / "bad.wav").exists()


def test_editing_time_revokes_previous_approval(speech):
    session = approved_truth(*speech)
    edit_anchor(session, "start", target_seconds=6.4)
    assert not session["anchors"][0]["accepted"]
    assert session["anchors"][0]["origin"] == "manual"


@pytest.mark.parametrize("field,value", [("accepted", "true"), ("reference_seconds", True),
                                        ("target_seconds", float("nan"))])
def test_invalid_review_types_are_rejected(speech, field, value):
    session = approved_truth(*speech)
    session["anchors"][0][field] = value
    with pytest.raises(ReviewError):
        fit(session)


def test_manual_entry_recovers_failed_match(speech):
    session = analyze(*speech)
    session["anchors"][0]["target_seconds"] = None
    with pytest.raises(ReviewError, match="manual target time"):
        edit_anchor(copy.deepcopy(session), "start", accepted=True)
    anchor = session["anchors"][0]
    edit_anchor(session, "start", target_seconds=anchor["reference_seconds"] + 0.2, accepted=True)
    assert anchor["accepted"]


def test_cli_approval_export_and_no_overwrite(speech, tmp_path, capsys):
    review = tmp_path / "review.json"
    assert main(["analyze", *map(str, speech), "--out", str(review)]) == 0
    assert main(["fit", str(review)]) == 2
    for anchor_id in ("start", "middle", "end"):
        assert main(["anchor", str(review), "--id", anchor_id, "--accept"]) == 0
    assert main(["fit", str(review), "--out", str(tmp_path / "fit.json"),
                 "--csv", str(tmp_path / "fit.csv")]) == 0
    data = json.loads((tmp_path / "fit.json").read_text())
    assert data["renderable"]
    before = review.read_bytes()
    assert main(["analyze", *map(str, speech), "--out", str(review)]) == 2
    assert review.read_bytes() == before
    assert main(["render", str(review), "--out", str(speech[1]), "--force"]) == 2
    capsys.readouterr()


def test_media_mutation_invalidates_review(speech, tmp_path):
    target = tmp_path / "mutable.flac"
    target.write_bytes(speech[1].read_bytes())
    session = analyze(speech[0], target)
    path = tmp_path / "review.json"
    write_json(path, session)
    with target.open("ab") as sink:
        sink.write(b"changed")
    with pytest.raises(ReviewError, match="changed since analysis"):
        load_session(path)


def test_invalid_input_media_and_options(speech, tmp_path):
    bad = tmp_path / "not-audio.wav"
    bad.write_text("not a WAV")
    with pytest.raises(ReviewError, match="ffprobe failed"):
        probe(bad)
    with pytest.raises(ReviewError, match="channel"):
        analyze(*speech, target_channel=2)
    with pytest.raises(ReviewError, match="search seconds"):
        analyze(*speech, search_seconds=3)
    with pytest.raises(ReviewError, match="overlap"):
        analyze(*speech, offset_hint=200)


def test_unicode_spaces_quotes_and_shell_characters_are_literal_paths(speech, tmp_path):
    directory = tmp_path / "剪辑's take $HOME"
    directory.mkdir()
    reference, target = directory / "相机 音频.wav", directory / "录音 (take 1).flac"
    reference.write_bytes(speech[0].read_bytes())
    target.write_bytes(speech[1].read_bytes())
    session = approved_truth(reference, target)
    edit_anchor(session, "middle", note="人工同步：同一次拍手 💡")
    review = directory / "同步 review.json"
    write_json(review, session)
    loaded = load_session(review)
    assert "人工同步" in loaded["anchors"][1]["note"]
    result = render(loaded, directory / "校正 audio.wav", report=directory / "残差 report.json")
    assert result["verification"]["max_abs_residual_ms"] < 0.1
