from pathlib import Path

import pytest

from mediawrench.cutsync import CutSyncError, Interval, make_timeline
from mediawrench.cutsync.cuts import load_keep, parse_time

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        ("1.2345", 1235),
        ("00:01:02,003", 62003),
        ("0.00049", 0),
        ("99:59:59.999", 359999999),
    ],
)
def test_time_parsing(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        None,
        "NaN",
        "Infinity",
        "-1",
        "1e3",
        "1:99:00",
        "1:2",
        [],
        "100:00:00",
        "1.0000000001",
        "1,5",
    ],
)
def test_bad_time(value):
    with pytest.raises(CutSyncError):
        parse_time(value)


def test_upstream_csv_variants():
    first = load_keep((FIXTURES / "losslesscut/test1.csv").read_text(), format="csv")
    assert first[0] == Interval(104612, 189053, "label 1")
    assert make_timeline(first).duration_ms == 84441 + 175746 + 288380
    second = load_keep((FIXTURES / "losslesscut/test2.csv").read_text(), format="csv")
    assert len(make_timeline(second).segments) == 1
    assert load_keep((FIXTURES / "losslesscut/test3.csv").read_text(), format="csv")[0].label == "A"


def test_current_csv_header_tags_quoting_bom():
    data = '\ufeffStart,End,Name,tag\r\n0,1,"a, b",blue\r\n2,3,"line\nlabel",red\r\n'
    assert load_keep(data, format="csv") == [
        Interval(0, 1000, "a, b"),
        Interval(2000, 3000, "line\nlabel"),
    ]


def test_json_and_llc_native():
    assert load_keep('{"keep":[[0,1],{"start":"2","end":"3"}]}', format="json") == [
        Interval(0, 1000),
        Interval(2000, 3000),
    ]
    data = (
        "{version:2, cutSegments:[{start:0,end:1,name:'first',selected:false}, "
        "{start:2,end:3,name:'second'},],}"
    )
    assert len(load_keep(data, format="llc")) == 2
    assert load_keep(data, format="llc", selected_only=True) == [Interval(2000, 3000, "second")]


def test_open_end_requires_explicit_duration():
    with pytest.raises(CutSyncError, match="--duration"):
        load_keep("0,,whole", format="csv")
    assert load_keep("0,,whole", format="csv", duration_ms=12000) == [Interval(0, 12000, "whole")]
    assert load_keep("{version:1,cutSegments:[{name:'all'}]}", format="llc", duration_ms=1000) == [
        Interval(0, 1000, "all")
    ]


@pytest.mark.parametrize(
    "data,format",
    [
        ('{"keep":[[0,1]],"keep":[[2,3]]}', "json"),
        ('{"remove":[[0,1]]}', "json"),
        ('{"keep":"not a list"}', "json"),
        ("{version:3,cutSegments:[]}", "llc"),
        ("0", "csv"),
        ("", "csv"),
        ('"unfinished', "csv"),
    ],
)
def test_invalid_keep(data, format):
    with pytest.raises(CutSyncError):
        load_keep(data, format=format)
