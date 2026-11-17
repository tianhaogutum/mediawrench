import pytest

from mediawrench.cutsync import CutSyncError
from mediawrench.cutsync.subtitles import parse_subtitles


def text(start="00:00:01,1", end="00:00:02,95", dialogue="hello"):
    return f"1\n{start} --> {end}\n{dialogue}\n\n"


def test_ambiguous_short_fraction_rejected_by_default():
    with pytest.raises(CutSyncError, match="ambiguous"):
        parse_subtitles(text())


@pytest.mark.parametrize(
    "policy,start,end", [("decimal", 1.1, 2.95), ("milliseconds", 1.001, 2.095)]
)
def test_explicit_fraction_interpretation_preserves_dialogue(policy, start, end):
    dialogue = "Say 00:00:00,95 --> 00:00:01,1 in the instructions."
    cues, changes = parse_subtitles(text(dialogue=dialogue), short_fractions=policy)
    assert cues[0].start.total_seconds() == start
    assert cues[0].end.total_seconds() == end
    assert cues[0].content == dialogue
    assert len(changes) == 2


@pytest.mark.parametrize("start", ["00:00:00,1234", "00:61:00,000", "00:00:99,000"])
def test_overprecision_and_carrying_fields_rejected(start):
    with pytest.raises(CutSyncError):
        parse_subtitles(text(start=start), short_fractions="decimal")
