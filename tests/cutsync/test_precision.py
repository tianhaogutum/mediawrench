from datetime import timedelta
from fractions import Fraction

import pytest
import srt

from mediawrench.cutsync import CutSyncError, make_timeline, remap
from mediawrench.cutsync.core import milliseconds, round_ms
from mediawrench.cutsync.cuts import load_keep


@pytest.mark.parametrize("fps", ["30000/1001", "24000/1001", "25"])
def test_ten_thousand_frame_cuts_do_not_accumulate_rounding_drift(fps):
    count = 10_000
    csv = "\n".join(f"{i * 2},{i * 2 + 1}" for i in range(count))
    timeline = make_timeline(load_keep(csv, format="csv-frames", fps=fps))
    exact = Fraction(count * 1000, 1) / Fraction(fps)
    assert timeline.duration_exact_ms == exact
    assert timeline.duration_ms == round_ms(exact)
    # A cue entirely in the last retained frame is shifted by the exact sum,
    # not the sum of ten thousand separately rounded segment durations.
    last = timeline.segments[-1]
    start = (last.source_start.numerator // last.source_start.denominator) + 1
    cue = srt.Subtitle(1, timedelta(milliseconds=start), timedelta(milliseconds=start + 1), "last")
    output, audit = remap([cue], timeline)
    expected_start = round_ms(last.output_start + start - last.source_start)
    assert milliseconds(output[0].start) == expected_start
    assert audit["duration_exact_ms"] == str(exact)


def test_decimal_submillisecond_ranges_preserve_exact_cumulative_duration():
    data = '{"keep":[[0,0.0014],[0.002,0.0034],[0.004,0.0054]]}'
    timeline = make_timeline(load_keep(data, format="json"))
    assert timeline.duration_exact_ms == Fraction(21, 5)
    assert timeline.duration_ms == 4  # independently rounded lengths would sum to 3 ms


def test_submillisecond_overlap_is_rejected_before_it_can_be_rounded_away():
    intervals = load_keep('{"keep":[[0,1.0004],[1.0003,2]]}', format="json")
    with pytest.raises(CutSyncError, match="overlap"):
        make_timeline(intervals)


def test_short_intersection_that_rounds_to_zero_is_explained():
    timeline = make_timeline(load_keep('{"keep":[[0.0019,0.003]]}', format="json"))
    cue = srt.Subtitle(1, timedelta(milliseconds=1), timedelta(milliseconds=2), "tiny piece")
    output, audit = remap([cue], timeline)
    assert output == []
    assert audit["cues"][0]["reason"] == "rounded_to_zero"
    assert audit["cues"][0]["discarded_fragments"][0]["source"]["start_exact_ms"] == "19/10"
