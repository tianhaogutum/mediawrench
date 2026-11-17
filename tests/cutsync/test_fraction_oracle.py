"""A 0.1 ms cell oracle independent of Fraction-based interval arithmetic."""

import json
from datetime import timedelta
from itertools import groupby

import pytest
import srt

# Property-based oracles; skipped when the optional hypothesis package is absent.
pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st

from mediawrench.cutsync import make_timeline, remap
from mediawrench.cutsync.core import milliseconds
from mediawrench.cutsync.cuts import load_keep


def rounded(ticks):
    return (ticks + 5) // 10


def expected(grid, start_ms, end_ms, policy, minimum):
    mapping = {source: out for out, source in enumerate(i for i, keep in enumerate(grid) if keep)}
    cells = [i for i in range(start_ms * 10, end_ms * 10) if i in mapping]
    runs = []
    for _, group in groupby(enumerate(cells), key=lambda pair: pair[1] - pair[0]):
        run = [source for _, source in group]
        runs.append((mapping[run[0]], mapping[run[-1]] + 1))
    if policy == "drop" and len(cells) != (end_ms - start_ms) * 10:
        runs = []
    elif policy == "longest" and runs:
        runs = [max(runs, key=lambda run: run[1] - run[0])]
    return [
        (rounded(left), rounded(right))
        for left, right in runs
        if rounded(right) - rounded(left) >= minimum
    ]


@settings(max_examples=2000, deadline=None, derandomize=True)
@given(
    pieces=st.lists(st.tuples(st.integers(0, 19), st.integers(10, 100)), min_size=1, max_size=10),
    cue_start=st.integers(0, 100),
    cue_length=st.integers(1, 100),
    minimum=st.integers(1, 10),
)
def test_fractional_mapping_matches_discrete_tenth_ms_cells(pieces, cue_start, cue_length, minimum):
    grid = []
    intervals = []
    for gap, length in pieces:
        grid.extend([False] * gap)
        left = len(grid)
        grid.extend([True] * length)
        right = len(grid)
        # String seconds avoid binary floats. Four decimal places = 0.1 ms.
        intervals.append(
            [f"{left // 10000}.{left % 10000:04d}", f"{right // 10000}.{right % 10000:04d}"]
        )
    timeline = make_timeline(load_keep(json.dumps({"keep": intervals}), format="json"))
    assert timeline.duration_ms == rounded(sum(grid))
    cue = srt.Subtitle(
        1,
        timedelta(milliseconds=cue_start),
        timedelta(milliseconds=cue_start + cue_length),
        "Across sub-ms cuts",
    )
    for policy in ("split", "drop", "longest"):
        output, _ = remap([cue], timeline, boundary=policy, min_duration_ms=minimum)
        actual = [(milliseconds(c.start), milliseconds(c.end)) for c in output]
        assert actual == expected(grid, cue_start, cue_start + cue_length, policy, minimum)
