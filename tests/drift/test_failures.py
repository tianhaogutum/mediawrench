from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from mediawrench.drift.cli import main
from mediawrench.drift.render import render
from mediawrench.drift.review import analyze, edit_anchor, fit, load_session
from mediawrench.drift.storage import ReviewError, read_json, write_json


@pytest.fixture
def approved(speech):
    session = analyze(*speech)
    for anchor in session["anchors"]:
        edit_anchor(session, anchor["id"],
                    target_seconds=16000 / 15992 * anchor["reference_seconds"] + 0.2,
                    accepted=True)
    return session


def fake_ffmpeg(tmp_path, monkeypatch, body):
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "ffmpeg"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])


def test_stalled_decoder_is_killed_reaped_and_not_published(approved, tmp_path, monkeypatch):
    fake_ffmpeg(tmp_path, monkeypatch, "exec /bin/sleep 20")
    # Record the OS child PID at creation; do not depend on the stalled
    # executable being scheduled early enough to write its own PID file.
    original_popen = subprocess.Popen
    spawned = []
    def record_child(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process.pid)
        return process
    monkeypatch.setattr(subprocess, "Popen", record_child)
    destination = tmp_path / "stalled.wav"
    with pytest.raises(ReviewError, match="timed out"):
        render(approved, destination, timeout=0.15, verify=False)
    assert not destination.exists()
    assert not list(tmp_path.glob(".drift-review-*"))
    assert len(spawned) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_decoder_error_after_partial_pcm_discards_output(approved, tmp_path, monkeypatch):
    fake_ffmpeg(tmp_path, monkeypatch,
                "/bin/dd if=/dev/zero bs=4 count=128 2>/dev/null\necho broken-decoder >&2\nexit 7")
    destination = tmp_path / "partial.wav"
    with pytest.raises(ReviewError, match="broken-decoder"):
        render(approved, destination, verify=False)
    assert not destination.exists()
    assert not list(tmp_path.glob(".drift-review-*"))


def test_existing_report_blocks_render_before_destination_is_created(approved, tmp_path):
    report = tmp_path / "existing.json"
    report.write_text("keep this")
    destination = tmp_path / "new.wav"
    with pytest.raises(ReviewError, match="output exists"):
        render(approved, destination, report=report)
    assert not destination.exists()
    assert report.read_text() == "keep this"


def test_force_does_not_replace_hardlinked_input(approved, tmp_path):
    alias = tmp_path / "alias.wav"
    os.link(approved["target"]["path"], alias)
    with pytest.raises(ReviewError, match="replace an input"):
        render(approved, alias, overwrite=True)


def test_reference_metadata_cannot_be_silently_edited(approved, tmp_path):
    path = tmp_path / "review.json"
    approved["reference"]["duration_seconds"] = 1000000000
    write_json(path, approved)
    with pytest.raises(ReviewError, match="metadata changed"):
        load_session(path)


@pytest.mark.parametrize("bad_json", ['{"schema":1,"schema":2}', '{"value":NaN}', '{"unknown":1e999}', '[1,2,3]'])
def test_json_rejects_duplicate_keys_nonfinite_and_wrong_root(tmp_path, bad_json):
    path = tmp_path / "review.json"
    path.write_text(bad_json)
    with pytest.raises(ReviewError):
        read_json(path)


def test_oversized_review_is_rejected_before_parsing(tmp_path):
    path = tmp_path / "large.json"
    with path.open("wb") as output:
        output.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ReviewError, match="document limit"):
        read_json(path)


def test_duplicate_and_backwards_anchors_are_rejected(approved):
    duplicate = copy.deepcopy(approved)
    duplicate["anchors"][1]["id"] = duplicate["anchors"][0]["id"]
    with pytest.raises(ReviewError, match="duplicate anchor"):
        fit(duplicate)
    approved["anchors"][2]["target_seconds"] = approved["anchors"][0]["target_seconds"] - 0.1
    with pytest.raises(ReviewError, match="increase"):
        fit(approved)


def test_large_clock_rate_is_blocked_with_diagnostic(approved):
    for anchor in approved["anchors"]:
        anchor["target_seconds"] = 1.04 * anchor["reference_seconds"]
    result = fit(approved)
    assert not result["renderable"]
    assert result["drift_ppm"] == pytest.approx(40000)
    assert any("clock-rate" in blocker for blocker in result["blockers"])
    assert fit(approved, max_drift_ppm=50000)["renderable"]


def test_relative_media_paths_resolve_against_review_directory(approved, tmp_path):
    path = tmp_path / "review.json"
    for label in ("reference", "target"):
        approved[label]["path"] = os.path.relpath(approved[label]["path"], tmp_path)
    write_json(path, approved)
    loaded = load_session(path)
    assert all(Path(loaded[label]["path"]).is_absolute() for label in ("reference", "target"))


def test_fit_cannot_overwrite_review_even_with_force(approved, tmp_path, capsys):
    path = tmp_path / "review.json"
    write_json(path, approved)
    before = path.read_bytes()
    assert main(["fit", str(path), "--out", str(path), "--force"]) == 2
    assert path.read_bytes() == before
    assert "replace an input" in capsys.readouterr().err


def test_origin_is_not_an_unchecked_csv_formula(approved):
    approved["anchors"][0]["origin"] = "=1+1"
    with pytest.raises(ReviewError, match="origin"):
        fit(approved)


def test_rejected_edit_leaves_the_in_memory_review_unchanged(approved):
    before = copy.deepcopy(approved)
    with pytest.raises(ReviewError):
        edit_anchor(approved, "middle", target_seconds=-1, accepted=True)
    assert approved == before


def test_conflicting_time_convention_is_not_silently_misreported(approved):
    approved["convention"] = "reference = scale * target + offset"
    with pytest.raises(ReviewError, match="time convention"):
        fit(approved)


def test_huge_json_integer_is_a_clean_cli_error(approved, tmp_path, capsys):
    path = tmp_path / "huge-integer.json"
    approved["anchors"][0]["target_seconds"] = 10 ** 400
    write_json(path, approved)
    assert main(["fit", str(path)]) == 2
    assert "finite floating-point range" in capsys.readouterr().err
