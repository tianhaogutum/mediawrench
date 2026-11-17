import multiprocessing
from multiprocessing.connection import Connection

import pytest

from mediawrench.cutsync import CutSyncError, Interval, make_timeline, parse_subtitles
from mediawrench.cutsync.cuts import load_keep
from mediawrench.cutsync.subtitles import parse_for_cli, parse_keep_for_cli


def test_regex_timeout_reaps_child_and_next_parse_succeeds():
    with pytest.raises(CutSyncError, match="exceeded"):
        parse_for_cli("1" * 50_000 + " x", timeout=0.2)
    assert not multiprocessing.active_children()
    cues, changes = parse_for_cli("1\n00:00:00,000 --> 00:00:01,000\nWorks afterward\n")
    assert len(cues) == 1
    assert changes == []
    assert not multiprocessing.active_children()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 61, True])
def test_invalid_parser_budget(timeout):
    with pytest.raises(CutSyncError, match="timeout"):
        parse_for_cli("", timeout=timeout)


def test_cue_limit_applied_before_returning_a_large_list():
    text = "1\n00:00:00,000 --> 00:00:01,000\nA\n\n" * 3
    with pytest.raises(CutSyncError, match="2-cue limit"):
        parse_subtitles(text, max_cues=2)


def test_empty_whitespace_source_is_fast_and_valid():
    assert parse_subtitles(" \n" * 100_000) == ([], [])


def test_keep_worker_and_range_limit():
    assert parse_keep_for_cli('{"keep":[[0,1]]}', format="json") == [Interval(0, 1000)]
    assert not multiprocessing.active_children()
    intervals = [Interval(i, i + 1) for i in range(20_001)]
    with pytest.raises(CutSyncError, match="20,000 keep-range"):
        make_timeline(intervals)
    with pytest.raises(CutSyncError, match="20,000 keep-range"):
        load_keep("0,1\n" * 20_001, format="csv")


def test_parent_handled_interrupt_reaps_parser_worker(monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Connection, "poll", interrupt)
    with pytest.raises(KeyboardInterrupt):
        parse_for_cli("1" * 10_000 + " x")
    assert not multiprocessing.active_children()
