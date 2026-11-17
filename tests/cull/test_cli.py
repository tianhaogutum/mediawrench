from __future__ import annotations

import io
import json

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.cli import main


def arguments(paths, command="plan"):
    return [
        command,
        *(str(paths[side]) for side in ("base", "left", "right")),
        "--history-source",
        "left",
    ]


def test_plan_then_merge(make_paths, tmp_path, capsys):
    paths = make_paths({"rating": 0}, {"rating": 1}, {"rating": 2})
    assert main(arguments(paths)) == 0
    plan = json.loads(capsys.readouterr().out)
    plan["choices"]["rating"] = "base"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(plan))
    assert (
        main(
            arguments(paths, "merge")
            + ["--decisions", str(decisions), "--output-dir", str(tmp_path / "out")]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["values"]["rating"] == 0


def test_conflicts_never_silently_default(make_paths, tmp_path, capsys):
    paths = make_paths({"rating": 0}, {"rating": 1}, {"rating": 2})
    assert main(arguments(paths, "merge") + ["--output-dir", str(tmp_path / "out")]) == 2
    assert "unresolved conflicts: rating" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_nonterminal_interactive_rejected(make_paths, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("left\n"))
    assert (
        main(
            arguments(make_paths(), "merge")
            + ["--interactive", "--output-dir", str(tmp_path / "out")]
        )
        == 2
    )
    assert "requires a terminal" in capsys.readouterr().err


def test_text_review_is_readable_and_names_each_conflict(make_paths, capsys):
    paths = make_paths({"rating": 1}, {"rating": 3}, {"rating": 5})
    assert main(arguments(paths) + ["--format", "text"]) == 0
    review = capsys.readouterr().out
    assert "rating [CONFLICT]: base=1  left=3  right=5" in review
    assert "Choices required: rating" in review
