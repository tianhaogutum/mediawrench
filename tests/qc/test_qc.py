"""`mediawrench qc`: argument validation, each check's adapter, rendering and exit codes.

Tests that need real FFmpeg generate the examples/qc media and skip (never mock) when
ffmpeg/ffprobe are not on PATH.
"""
import json
from pathlib import Path
import re
import shutil

import pytest

from examples.postflight.synthetic import FFMPEG_AVAILABLE, FFMPEG_MISSING
from mediawrench import cli as top
from mediawrench.qc import SCHEMA
from mediawrench.qc import cli, core
from mediawrench.qc.core import Options, QCInputError

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "qc"
real_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason=FFMPEG_MISSING)


@pytest.fixture
def pair(tmp_path):
    master, deliverable = tmp_path / "master.mkv", tmp_path / "episode.mp4"
    master.write_bytes(b"not really media")
    deliverable.write_bytes(b"not really media either")
    return master, deliverable


@pytest.fixture
def no_ffmpeg(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(core.shutil, "which", lambda name, *a, **k: None if Path(str(name)).name in ("ffmpeg", "ffprobe") else real(name, *a, **k))


def by_name(doc, name):
    return [c for c in doc["checks"] if c["name"] == name]


# --- validation: exit 2 before any check runs ---------------------------------------------

def test_missing_inputs_are_usage_errors(pair, capsys):
    master, deliverable = pair
    assert cli.main([str(master), str(deliverable.with_name("nope.mp4"))]) == 2
    assert "deliverable" in capsys.readouterr().err
    assert cli.main([str(master), str(master)]) == 2
    assert "same file" in capsys.readouterr().err
    assert cli.main([str(master), str(deliverable), "--subtitles", "missing.srt"]) == 2


def test_loudness_needs_all_three_targets(pair):
    with pytest.raises(QCInputError, match="all of"):
        core.validate(*pair, Options(target_i=-16))
    with pytest.raises(QCInputError, match="loudness target"):
        core.validate(*pair, Options(target_i=-16, target_tp=-30, tolerance=1))


def test_chapters_file_is_validated_up_front(pair, tmp_path):
    odd = tmp_path / "chapters.data"
    odd.write_text("{}", encoding="utf-8")
    with pytest.raises(QCInputError, match="--chapters-format"):
        core.validate(*pair, Options(chapters=odd))
    with pytest.raises(QCInputError, match="chapters"):
        core.validate(*pair, Options(chapters=odd, chapters_format="json"))
    text = tmp_path / "chapters.txt"
    text.write_text("00:00:00 Intro\n00:00:30 Talk\n", encoding="utf-8")
    options = Options(chapters=text)
    core.validate(*pair, options)
    assert options.chapters_format == "text"
    options = Options(chapters=EXAMPLES / "chapters.json")
    core.validate(*pair, options)
    assert options.chapters_format == "json"


def test_explicit_missing_executable_is_an_input_error(pair):
    with pytest.raises(QCInputError, match="--ffmpeg"):
        core.validate(*pair, Options(ffmpeg="/no/such/ffmpeg"))


# --- without FFmpeg every media check is SKIPPED, never an error --------------------------

def test_without_ffmpeg_media_checks_are_skipped(pair, no_ffmpeg):
    doc = core.run(*pair, Options(target_i=-16, target_tp=-1.5, tolerance=1,
                                  chapters=EXAMPLES / "chapters.json"))
    statuses = {c["name"]: c["status"] for c in doc["checks"]}
    assert statuses == {"streams": "SKIPPED", "decode": "SKIPPED", "duration": "SKIPPED",
                        "loudness": "SKIPPED", "chapters": "SKIPPED", "subtitles": "SKIPPED"}
    assert "ffmpeg" in by_name(doc, "streams")[0]["summary"]
    assert doc["summary"]["failed"] is False and doc["summary"]["exit_code"] == 0
    assert doc["schema"] == SCHEMA and doc["postflight"] is None


def test_fail_on_skipped_turns_skips_into_exit_1(pair, no_ffmpeg, capsys):
    master, deliverable = pair
    assert cli.main([str(master), str(deliverable)]) == 0
    assert "ship it (exit 0)" in capsys.readouterr().out
    assert cli.main([str(master), str(deliverable), "--fail-on", "skipped"]) == 1


def test_json_output_is_machine_readable(pair, no_ffmpeg, capsys):
    master, deliverable = pair
    assert cli.main([str(master), str(deliverable), "--format", "json",
                     "--subtitles", str(EXAMPLES / "episode.srt")]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == SCHEMA
    assert [c["name"] for c in doc["checks"]] == ["streams", "decode", "duration", "loudness", "chapters", "subtitles"]
    assert set(doc["summary"]["counts"]) == set(core.STATUSES)
    assert all({"name", "status", "summary", "details", "source", "facts"} <= set(c) for c in doc["checks"])


def test_text_output_has_one_line_per_check_and_no_color_when_piped(pair, no_ffmpeg, capsys):
    master, deliverable = pair
    cli.main([str(master), str(deliverable)])
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    for name in ("streams", "decode", "duration", "loudness", "chapters", "subtitles"):
        assert f" {name} " in out
    assert "FFmpeg not found" in out


# --- subtitles ------------------------------------------------------------------------------

def test_example_subtitles_warn_on_speed_and_overrun():
    check = core.subtitle_check(EXAMPLES / "episode.srt", Options(), 8.0)
    assert check["status"] == "WARN"
    text = " ".join([check["summary"], *check["details"]])
    assert "cue 2 reads at 27.2 chars/s" in text and "cue 4 ends at 00:00:08.400" in text
    fixed = core.subtitle_check(EXAMPLES / "episode-fixed.srt", Options(), 8.0)
    assert fixed["status"] == "PASS" and fixed["facts"]["cues"] == 4


def test_subtitle_limits_come_from_reflow_defaults_and_can_change():
    assert core.subtitle_check(EXAMPLES / "episode.srt", Options(max_cps=30), None)["status"] == "PASS"
    narrow = core.subtitle_check(EXAMPLES / "episode-fixed.srt", Options(max_chars=20), 8.0)
    assert narrow["status"] == "WARN" and "line" in narrow["summary"]


def test_cue_after_the_end_and_disorder_fail(tmp_path):
    late = tmp_path / "late.srt"
    late.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n00:00:09,000 --> 00:00:09,500\nGone\n", encoding="utf-8")
    check = core.subtitle_check(late, Options(), 8.0)
    assert check["status"] == "FAIL" and "after the deliverable ends" in check["summary"]
    disorder = tmp_path / "disorder.srt"
    disorder.write_text("1\n00:00:03,000 --> 00:00:04,000\nB\n\n2\n00:00:01,000 --> 00:00:02,000\nA\n", encoding="utf-8")
    assert core.subtitle_check(disorder, Options(), None)["status"] == "FAIL"


def test_malformed_srt_fails_and_unsupported_vtt_is_unknown(tmp_path):
    bad = tmp_path / "bad.srt"
    bad.write_text("1\n00:00:01,000 -> 00:00:02,000\nHi\n", encoding="utf-8")
    check = core.subtitle_check(bad, Options(), None)
    assert check["status"] == "FAIL" and "does not parse" in check["summary"]
    styled = tmp_path / "styled.vtt"
    styled.write_text("WEBVTT\n\nSTYLE\n::cue { color: red }\n\n00:01.000 --> 00:02.000\nHi\n", encoding="utf-8")
    assert core.subtitle_check(styled, Options(), None)["status"] == "UNKNOWN"
    latin1 = tmp_path / "latin1.srt"
    latin1.write_bytes("1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9\n".encode("latin-1"))
    assert core.subtitle_check(latin1, Options(), None)["status"] == "FAIL"


def test_vtt_markup_and_settings_are_read(tmp_path):
    vtt = tmp_path / "ok.vtt"
    vtt.write_text("WEBVTT\n\n00:00.500 --> 00:02.000 line:90%\n<v Ana>Welcome.</v>\n\n"
                   "00:02.200 --> 00:04.000\nFish &amp; chips\n", encoding="utf-8")
    check = core.subtitle_check(vtt, Options(), 8.0)
    assert check["status"] == "PASS" and check["facts"]["cues"] == 2


# --- chapters and postflight adapters --------------------------------------------------------

def test_chapter_differences_read_like_sentences():
    assert core._chapter_difference({"path": "/chapters", "kind": "length", "expected": 3, "actual": 2}) == \
        "2 chapters in the deliverable, expected 3"
    missing = {"path": "/chapters/2", "kind": "missing", "actual": None,
               "expected": {"title": "Credits", "start_us": 6_500_000, "end_us": 8_000_000}}
    assert core._chapter_difference(missing) == 'missing chapter 3 "Credits" at 00:00:06.500'
    shifted = {"path": "/chapters/1/start_us", "kind": "outside_tolerance", "expected_us": 2_000_000,
               "actual_us": 2_250_000, "delta_us": 250_000, "within_tolerance": False, "tolerance_us": 1000}
    assert core._chapter_difference(shifted) == "chapter 2 start 00:00:02.250, expected 00:00:02.000 (+0.250 s)"


def test_postflight_checks_are_bucketed():
    assert core._bucket("probe") == core._bucket("video_stream_count") == "streams"
    assert core._bucket("audio_decoded_frames") == core._bucket("video_decode") == "decode"
    assert core._bucket("container_duration") == core._bucket("audio_decoded_duration") == "duration"
    assert core._bucket("preflight") is None
    assert core._worst(["PASS", "UNKNOWN", "WARN"]) == "UNKNOWN" and core._worst([]) == "UNKNOWN"


def test_registered_first_in_its_group():
    group = [c.name for c in top.COMMANDS if c.group == "Audio & video files"]
    assert group[0] == "qc"
    assert top.BY_NAME["qc"].binaries == ("ffmpeg", "ffprobe")


# --- real FFmpeg: the README demo ---------------------------------------------------------

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from examples.qc.make_media import main as make_media
    return make_media(tmp_path_factory.mktemp("qc-media"))


def demo_args(media, deliverable, subtitles):
    return [str(media["master.mkv"]), str(media[deliverable]), "--target-i", "-16", "--target-tp", "-1.5",
            "--tolerance", "1", "--chapters", str(EXAMPLES / "chapters.json"),
            "--subtitles", str(EXAMPLES / subtitles), "--format", "json"]


@real_ffmpeg
def test_broken_export_is_caught(media, capsys):
    assert cli.main(demo_args(media, "episode.mp4", "episode.srt")) == 1
    doc = json.loads(capsys.readouterr().out)
    statuses = {c["name"]: c["status"] for c in doc["checks"]}
    assert statuses == {"streams": "PASS", "decode": "PASS", "duration": "FAIL",
                        "loudness": "FAIL", "chapters": "FAIL", "subtitles": "WARN"}
    # AAC priming can shift the decoded length by a few milliseconds between FFmpeg builds.
    assert re.match(r"audio 6\.0\d\d s vs master 8\.0\d\d s", by_name(doc, "duration")[0]["summary"])
    assert by_name(doc, "loudness")[0]["facts"]["lufs_pass"] is False
    assert by_name(doc, "chapters")[0]["facts"]["actual"] == 0
    assert doc["postflight"]["verdict"] == "FAIL"


@real_ffmpeg
def test_fixed_export_ships(media, capsys):
    assert cli.main(demo_args(media, "episode-fixed.mp4", "episode-fixed.srt")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {c["status"] for c in doc["checks"]} == {"PASS"}
    assert doc["deliverable_duration_seconds"] == pytest.approx(8.0, abs=0.05)


@real_ffmpeg
def test_text_chapters_end_at_the_deliverable(media, tmp_path, capsys):
    text = tmp_path / "chapters.txt"
    text.write_text("00:00:00 Intro\n00:00:02 Interview\n00:00:06.5 Credits\n", encoding="utf-8")
    # The last chapter ends at the probed container duration; AAC padding may differ by build.
    assert cli.main([str(media["master.mkv"]), str(media["episode-fixed.mp4"]), "--chapters", str(text),
                     "--chapter-tolerance-ms", "100", "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert by_name(doc, "chapters")[0]["status"] == "PASS"
