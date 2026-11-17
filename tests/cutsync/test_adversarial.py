from decimal import Decimal

import pytest

from mediawrench.cutsync import CutSyncError, make_timeline
from mediawrench.cutsync.cuts import load_keep, parse_time


@pytest.mark.parametrize(
    "data,format",
    [
        ("{version:true,cutSegments:[{start:0,end:1}]}", "llc"),
        ("{version:2,cutSegments:[{start:null,end:1}]}", "llc"),
        ("{version:2,cutSegments:[{start:0,end:1,selected:0}]}", "llc"),
        ('{"keep":[{"start":null,"end":1}]}', "json"),
        ('{"keep":[{"start":0,"end":null}]}', "json"),
        ('{"keep":[{"start":0,"end":1,"speed":2}]}', "json"),
        ('{"keep":[[null,1]]}', "json"),
        ('{"keep":[[0,""]]}', "json"),
        ('{"keep":[[0,1]], // comment\n}', "json"),
    ],
)
def test_no_silent_coercions_or_ignored_timeline_semantics(data, format):
    with pytest.raises(CutSyncError):
        load_keep(data, format=format, duration_ms=1000)


def test_selected_llc_audit_retains_original_row_number():
    intervals = load_keep(
        "{version:2,cutSegments:[{start:0,end:1,selected:false},{start:2,end:3}]}",
        format="llc",
        selected_only=True,
    )
    assert make_timeline(intervals).segments[0].input_rows == (2,)


def test_small_numeric_json_times_round_to_ms():
    assert parse_time(Decimal("0.000000001")) == 0
    assert parse_time(0.000001) == 0
    assert load_keep('{"keep":[[0.000000001,1.0005]]}', format="json")[0].end_ms == 1001


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN"), Decimal("-1e10000")])
def test_nonfinite_and_extreme_numeric_values(value):
    with pytest.raises(CutSyncError):
        parse_time(value)


@pytest.mark.parametrize("number", ["1e1000000000", "1e-1000000000", "-1e1000000000"])
def test_extreme_decimal_exponents_rejected_before_fraction_allocation(number):
    with pytest.raises(CutSyncError):
        parse_time(Decimal(number))
    with pytest.raises(CutSyncError):
        load_keep('{"keep":[[0,' + number + "]]}", format="json")


def test_extreme_zero_exponent_and_redundant_fractional_zeros_are_safe():
    assert parse_time(Decimal("0e-1000000000")) == 0
    assert parse_time(Decimal("1.000000000000000000000")) == 1000
