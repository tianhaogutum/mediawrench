import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import srt

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/cutsync/fixtures"
# Run through the top-level dispatcher; prefer this source tree over any installed copy.
CLI = [sys.executable, "-m", "mediawrench", "cutsync"]
ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
}


def invoke(*args):
    return subprocess.run(
        [*CLI, *map(str, args)],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_real_ffmpeg_srt_end_to_end(tmp_path):
    output = tmp_path / "edited.srt"
    result = invoke(
        FIXTURES / "ffmpeg-export.srt", "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output
    )
    assert result.returncode == 0, result.stderr
    parsed = list(srt.parse(output.read_text()))
    audit = json.loads(output.with_suffix(".changes.json").read_text())
    assert len(parsed) == 5
    assert parsed[-1].end.total_seconds() == 6
    assert audit["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert audit["summary"]["dropped"] == 1
    if shutil.which("ffprobe"):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_packets", "-of", "json", str(output)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert probe.returncode == 0, probe.stderr
        assert len(json.loads(probe.stdout)["packets"]) == 5


@pytest.mark.parametrize("extension", ["csv", "json", "llc"])
def test_examples_agree(tmp_path, extension):
    output = tmp_path / f"{extension}.srt"
    result = invoke(
        ROOT / "examples/cutsync/interview.srt", "--keep", ROOT / f"examples/cutsync/keep.{extension}", "-o", output
    )
    assert result.returncode == 0, result.stderr
    parsed = list(srt.parse(output.read_text()))
    assert [p.start.total_seconds() for p in parsed] == [0.5, 2, 3.8, 4, 5]
    assert '<font color="#ffff00">Thanks for watching.</font>' in parsed[-1].content


def test_upstream_srt_with_mixed_fraction_and_crlf(tmp_path):
    output = tmp_path / "llc.srt"
    result = invoke(
        FIXTURES / "losslesscut/sample.srt",
        "--keep",
        FIXTURES / "losslesscut/test1.csv",
        "-o",
        output,
        "--short-fractions",
        "decimal",
    )
    assert result.returncode == 0, result.stderr
    audit = json.loads(output.with_suffix(".changes.json").read_text())
    assert audit["summary"]["input_cues"] == 2
    assert audit["summary"]["output_cues"] == 3
    assert audit["cues"][0]["action"] == "dropped"
    assert audit["cues"][1]["source"]["end_ms"] == 5430950
    assert audit["cues"][1]["timestamp_normalizations"][0]["original"] == "01:30:30.95"
    assert "\r" not in output.read_text()


def test_dry_run_writes_nothing_and_is_json(tmp_path):
    output = tmp_path / "dry.srt"
    result = invoke(
        ROOT / "examples/cutsync/interview.srt",
        "--keep",
        ROOT / "examples/cutsync/keep.csv",
        "-o",
        output,
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["summary"]["output_cues"] == 5
    assert list(tmp_path.iterdir()) == []


def test_refuse_existing_report_without_partial_output(tmp_path):
    output = tmp_path / "edited.srt"
    report = output.with_suffix(".changes.json")
    report.write_text("do not touch")
    result = invoke(
        ROOT / "examples/cutsync/interview.srt", "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output
    )
    assert result.returncode == 2
    assert report.read_text() == "do not touch"
    assert not output.exists()


def test_force_cannot_replace_source_through_hardlink(tmp_path):
    original = tmp_path / "source.srt"
    original.write_bytes((ROOT / "examples/cutsync/interview.srt").read_bytes())
    output = tmp_path / "alias.srt"
    output.hardlink_to(original)
    result = invoke(original, "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output, "--force")
    assert result.returncode == 2
    assert "input" in result.stderr
    assert original.read_bytes() == (ROOT / "examples/cutsync/interview.srt").read_bytes()


def test_legacy_encoding(tmp_path):
    source = tmp_path / "utf16.srt"
    source.write_text((ROOT / "examples/cutsync/interview.srt").read_text(), encoding="utf-16")
    output = tmp_path / "edited.srt"
    result = invoke(
        source, "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output, "--input-encoding", "utf-16"
    )
    assert result.returncode == 0, result.stderr
    assert "你好" in output.read_text()


def test_frame_csv_cli_matches_seconds_and_audits_fps(tmp_path):
    output = tmp_path / "frames.srt"
    result = invoke(
        ROOT / "examples/cutsync/interview.srt",
        "--keep",
        ROOT / "examples/cutsync/keep-frames.csv",
        "--keep-format",
        "csv-frames",
        "--fps",
        "25",
        "-o",
        output,
    )
    assert result.returncode == 0, result.stderr
    audit = json.loads(output.with_suffix(".changes.json").read_text())
    assert audit["duration_ms"] == 6000
    assert audit["keep_input"]["fps"] == "25"
    assert len(list(srt.parse(output.read_text()))) == 5


def test_json5_emoji_labels_write_valid_utf8_report(tmp_path):
    keep = tmp_path / "emoji.llc"
    keep.write_text(r"{version:2,cutSegments:[{start:0,end:10,name:'\ud83d\ude00'}]}")
    output = tmp_path / "emoji.srt"
    result = invoke(ROOT / "examples/cutsync/interview.srt", "--keep", keep, "-o", output)
    assert result.returncode == 0, result.stderr
    audit = json.loads(output.with_suffix(".changes.json").read_text())
    assert audit["segments"][0]["labels"] == ["😀"]


def test_cli_regex_timeout_publishes_no_files_or_traceback(tmp_path):
    source = tmp_path / "slow.srt"
    source.write_text("1" * 50_000 + " x")
    output = tmp_path / "output.srt"
    result = invoke(
        source, "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output, "--parse-timeout", "0.2"
    )
    assert result.returncode == 2
    assert "exceeded" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()
    assert not output.with_suffix(".changes.json").exists()


def test_expanded_text_budget_fails_before_output_publication(tmp_path):
    source = tmp_path / "long.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:02,000\n" + "x" * 40_000 + "\n")
    keep = tmp_path / "many.csv"
    keep.write_text("".join(f"{i * 2 / 1000:.3f},{(i * 2 + 1) / 1000:.3f}\n" for i in range(1000)))
    output = tmp_path / "output.srt"
    result = invoke(source, "--keep", keep, "-o", output)
    assert result.returncode == 2
    assert "output budget" in result.stderr
    assert not output.exists()
    assert not output.with_suffix(".changes.json").exists()


@pytest.mark.parametrize(
    "content", [b"not a subtitle", b"\xff\xfe\xfd", b"1\n00:00:02,000 --> 00:00:01,000\nwrong\n"]
)
def test_invalid_source_leaves_no_outputs(tmp_path, content):
    source = tmp_path / "broken.srt"
    source.write_bytes(content)
    output = tmp_path / "edited.srt"
    result = invoke(source, "--keep", ROOT / "examples/cutsync/keep.csv", "-o", output)
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert not output.exists()
    assert not output.with_suffix(".changes.json").exists()
