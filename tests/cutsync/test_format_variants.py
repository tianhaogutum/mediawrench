from fractions import Fraction
from pathlib import Path

import pytest

from mediawrench.cutsync import CutSyncError, Interval
from mediawrench.cutsync.cuts import load_keep, parse_fps
from mediawrench.cutsync.subtitles import parse_subtitles


def test_frames_use_exact_rational_timebase():
    ranges = load_keep(
        "Start,End,Name\n0,30,first\n60,90,second\n", format="csv-frames", fps="30000/1001"
    )
    assert ranges == [Interval(0, 1001, "first"), Interval(2002, 3003, "second")]
    assert parse_fps("29.97") == Fraction(2997, 100)


def test_human_csv_and_frame_csv_match_seconds_example():
    root = Path(__file__).resolve().parents[2]
    frames = load_keep(
        (root / "examples/cutsync/keep-frames.csv").read_text(), format="csv-frames", fps="25"
    )
    seconds = load_keep((root / "examples/cutsync/keep.csv").read_text(), format="csv")
    assert frames == seconds
    assert load_keep('Start,End,Name\n00:00:00.000,"00:00:02,500",human', format="csv") == [
        Interval(0, 2500, "human")
    ]


@pytest.mark.parametrize("fps", ["0", "-25", "30000/0", "nan", "1001", "25/", "1e3"])
def test_bad_fps(fps):
    with pytest.raises(CutSyncError):
        parse_fps(fps)


@pytest.mark.parametrize(
    "data,kwargs",
    [
        ("0,25", {"format": "csv-frames"}),
        ("0,25", {"format": "csv", "fps": "25"}),
        ("0,25.5", {"format": "csv-frames", "fps": "25"}),
        ("00:00:00,25", {"format": "csv-frames", "fps": "25"}),
        ("Start,End,Name\n", {"format": "csv"}),
    ],
)
def test_ambiguous_or_empty_csv_rejected(data, kwargs):
    with pytest.raises(CutSyncError):
        load_keep(data, **kwargs)


def test_json5_surrogate_pair_is_a_valid_emoji_label():
    data = r"{version:2,cutSegments:[{start:0,end:1,name:'\ud83d\ude00'}]}"
    assert load_keep(data, format="llc")[0].label == "😀"


def test_unpaired_json5_surrogate_rejected_before_output():
    data = r"{version:2,cutSegments:[{start:0,end:1,name:'\ud800'}]}"
    with pytest.raises(CutSyncError, match="surrogate"):
        load_keep(data, format="llc")


def test_nonconsecutive_missing_and_duplicate_cue_numbers_are_preserved():
    data = (
        "00:00:00,000 --> 00:00:01,000\nMissing number\n\n"
        "9\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "9\n00:00:02,000 --> 00:00:03,000\nB\n"
    )
    cues, _ = parse_subtitles(data)
    assert [cue.index for cue in cues] == [None, 9, 9]


@pytest.mark.parametrize(
    "data",
    [
        "1.5\n00:00:00,000 --> 00:00:01,000\nwrong index\n",
        "1\n00:00:00,000 --> 00:00:01,000\nNUL\x00text\n",
    ],
)
def test_ambiguous_numbering_and_nul_rejected(data):
    with pytest.raises(CutSyncError):
        parse_subtitles(data)
