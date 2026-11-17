from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.cli import main
from mediawrench.cull.verify import verify_bundle

from .helpers import subprocess_env


def test_unicode_decisions_can_be_redirected_to_an_ascii_console(make_paths):
    paths = make_paths({"label": "绿"}, {"label": "绿"}, {"label": "绿"})
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mediawrench",
            "cull",
            "plan",
            *(str(paths[side]) for side in ("base", "left", "right")),
            "--history-source",
            "left",
        ],
        env=subprocess_env(PYTHONIOENCODING="ascii"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("ascii")
    assert json.loads(result.stdout)["fields"]["label"]["left"] == "绿"


def test_unicode_input_filename_is_preserved_in_ascii_escaped_audit(make_paths, tmp_path, capsys):
    paths = make_paths({"rating": 1}, {"rating": 1}, {"rating": 1})
    renamed = tmp_path / "left-绿.xmp"
    paths["left"].rename(renamed)
    paths["left"] = renamed
    destination = tmp_path / "out"
    code = main(
        [
            "merge",
            *(str(paths[side]) for side in ("base", "left", "right")),
            "--history-source",
            "left",
            "--output-dir",
            str(destination),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert verify_bundle(destination)["verified"] is True
    assert b"\\u7eff" in (destination / "audit.json").read_bytes()
