from datetime import timedelta

import pytest
import srt

from mediawrench.cutsync import CutSyncError, Interval, make_timeline, remap


def cue(start, end, text="hello", index=9):
    return srt.Subtitle(index, timedelta(milliseconds=start), timedelta(milliseconds=end), text)


def times(output):
    return [
        (int(c.start.total_seconds() * 1000), int(c.end.total_seconds() * 1000)) for c in output
    ]


def test_join_and_audit_source_identity():
    original = [
        cue(500, 2500, "<i>Hello</i>", 10),
        cue(2800, 3800, index=20),
        cue(4000, 5500, "你好\nworld", 30),
        cue(5800, 8200, index=40),
        cue(9000, 10000, index=50),
    ]
    timeline = make_timeline([Interval(0, 2000), Interval(4000, 6000), Interval(8000, 10000)])
    output, audit = remap(original, timeline)
    assert times(output) == [(500, 2000), (2000, 3500), (3800, 4000), (4000, 4200), (5000, 6000)]
    assert [c["action"] for c in audit["cues"]] == [
        "trimmed",
        "dropped",
        "shifted",
        "split",
        "shifted",
    ]
    assert [c.index for c in output] == [1, 2, 3, 4, 5]
    assert audit["cues"][3]["source_index"] == 40
    assert [p["output_index"] for p in audit["cues"][3]["outputs"]] == [3, 4]
    assert output[0].content == "<i>Hello</i>"
    assert output[1].content == "你好\nworld"
    assert original[0].end == timedelta(milliseconds=2500)
    assert original[0].index == 10


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("split", [(1000, 2000), (2000, 4000)]),
        ("drop", []),
        ("longest", [(2000, 4000)]),
    ],
)
def test_boundary_policies(policy, expected):
    output, audit = remap(
        [cue(1000, 6000)], make_timeline([Interval(0, 2000), Interval(4000, 7000)]), boundary=policy
    )
    assert times(output) == expected
    assert audit["cues"][0]["needs_review"]


def test_longest_tie_uses_earlier_piece():
    output, _ = remap(
        [cue(1000, 5000)],
        make_timeline([Interval(0, 2000), Interval(4000, 6000)]),
        boundary="longest",
    )
    assert times(output) == [(1000, 2000)]


def test_half_open_boundaries_and_touching_intervals():
    timeline = make_timeline([Interval(2000, 3000), Interval(3000, 4000)])
    output, audit = remap([cue(1000, 2000), cue(2000, 4000), cue(4000, 5000)], timeline)
    assert times(output) == [(0, 2000)]
    assert len(audit["segments"]) == 1
    assert audit["segments"][0]["input_rows"] == [1, 2]
    assert audit["cues"][1]["action"] == "shifted"


def test_overlapping_cues_sorted_and_proprietary_preserved():
    first = cue(5000, 9000, "later", 4)
    second = cue(4000, 6000, "earlier", 4)
    second.proprietary = "X1:100 X2:600 Y1:050 Y2:100"
    output, audit = remap([first, second], make_timeline([Interval(4000, 10000)]))
    assert times(output) == [(0, 2000), (1000, 5000)]
    assert output[0].proprietary == second.proprietary
    assert audit["cues"][0]["outputs"][0]["output_index"] == 2


def test_short_fragments_explained():
    output, audit = remap([cue(1999, 2001)], make_timeline([Interval(0, 2000)]), min_duration_ms=2)
    assert output == []
    assert audit["cues"][0]["reason"] == "below_min_duration"
    assert audit["cues"][0]["discarded_fragments"][0]["source"]["start_ms"] == 1999


@pytest.mark.parametrize(
    "intervals",
    [
        [Interval(5, 5)],
        [Interval(5, 4)],
        [Interval(-1, 5)],
        [Interval(0, 5), Interval(4, 8)],
        [Interval(4, 8), Interval(0, 3)],
    ],
)
def test_reject_invalid_keep_intervals(intervals):
    with pytest.raises(CutSyncError):
        make_timeline(intervals)


@pytest.mark.parametrize("bad_cue", [cue(0, 0), cue(3, 2), cue(-1, 2), cue(0, 1, " ")])
def test_reject_invalid_source_even_when_removed(bad_cue):
    with pytest.raises(CutSyncError):
        remap([bad_cue], make_timeline([]))


def test_empty_timeline_and_empty_source():
    output, audit = remap([cue(1, 2)], make_timeline([]))
    assert output == []
    assert audit["summary"]["dropped"] == 1
    assert remap([], make_timeline([]))[1]["duration_ms"] == 0


def test_expansion_budget_applies_even_to_discarded_fragments():
    timeline = make_timeline([Interval(i * 2, i * 2 + 1) for i in range(4)])
    for policy in ("split", "drop", "longest"):
        with pytest.raises(CutSyncError, match="intersections"):
            remap([cue(0, 8), cue(0, 8)], timeline, boundary=policy, max_intersections=7)


def test_many_touching_ranges_preserve_all_rows_without_splitting():
    timeline = make_timeline([Interval(i, i + 1) for i in range(1000)])
    assert timeline.duration_ms == 1000
    assert timeline.segments[0].input_rows == tuple(range(1, 1001))
    assert len(remap([cue(0, 1000)], timeline)[0]) == 1


@pytest.mark.parametrize("long_proprietary", [False, True])
def test_text_or_timing_metadata_expansion_is_bounded_before_serialization(long_proprietary):
    original = cue(0, 2000, "x" if long_proprietary else "x" * 40_000)
    if long_proprietary:
        original.proprietary = "x" * 40_000
    timeline = make_timeline([Interval(i * 2, i * 2 + 1) for i in range(1000)])
    with pytest.raises(CutSyncError, match="output budget"):
        remap([original], timeline)
    assert original.index == 9


def test_output_size_estimate_covers_serialized_utf8_bytes():
    output, audit = remap([cue(0, 10, "🎬你好")], make_timeline([Interval(0, 10)]))
    assert (
        len(srt.compose(output, reindex=False).encode("utf-8"))
        <= audit["summary"]["estimated_srt_bytes"]
    )
