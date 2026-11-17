"""An independent millisecond-grid oracle, rather than another interval mapper."""

from datetime import timedelta
from itertools import groupby

import pytest
import srt

# Property-based oracles; skipped when the optional hypothesis package is absent.
pytest.importorskip("hypothesis")

from hypothesis import given, settings
from hypothesis import strategies as st

from mediawrench.cutsync import Interval, make_timeline, remap
from mediawrench.cutsync.core import milliseconds


def grid_oracle(grid, start, end, boundary, minimum):
    output_cells = {
        source: output for output, source in enumerate(i for i, keep in enumerate(grid) if keep)
    }
    cells = [i for i in range(start, end) if i in output_cells]
    # A run of retained original cells constitutes one surviving text fragment.
    runs = []
    for _, group in groupby(enumerate(cells), key=lambda pair: pair[1] - pair[0]):
        source = [cell for _, cell in group]
        runs.append((output_cells[source[0]], output_cells[source[-1]] + 1))
    if boundary == "drop" and len(cells) != end - start:
        runs = []
    elif boundary == "longest" and runs:
        runs = [max(runs, key=lambda pair: pair[1] - pair[0])]
    return [run for run in runs if run[1] - run[0] >= minimum]


@settings(max_examples=2500, deadline=None, derandomize=True)
@given(
    grid=st.lists(st.booleans(), min_size=1, max_size=100),
    start=st.integers(0, 110),
    length=st.integers(1, 110),
    minimum=st.integers(1, 12),
)
def test_mapping_matches_cell_enumeration(grid, start, length, minimum):
    intervals = []
    for keep, group in groupby(enumerate(grid), key=lambda item: item[1]):
        cells = [position for position, _ in group]
        if keep:
            intervals.append(Interval(cells[0], cells[-1] + 1))
    timeline = make_timeline(intervals)
    assert timeline.duration_ms == sum(grid)
    cue = srt.Subtitle(
        81,
        timedelta(milliseconds=start),
        timedelta(milliseconds=start + length),
        "<i>你好</i>\nSecond line",
    )
    for boundary in ("split", "drop", "longest"):
        output, audit = remap([cue], timeline, boundary=boundary, min_duration_ms=minimum)
        actual = [(milliseconds(c.start), milliseconds(c.end)) for c in output]
        expected = grid_oracle(grid, start, start + length, boundary, minimum)
        assert actual == expected
        assert all(0 <= left < right <= timeline.duration_ms for left, right in actual)
        assert len(output) == audit["summary"]["output_cues"]
        assert [part["output_index"] for part in audit["cues"][0]["outputs"]] == list(
            range(1, len(output) + 1)
        )
        assert all(c.content == cue.content for c in output)
    assert cue.index == 81
    assert milliseconds(cue.start) == start
