from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.verify import verify_bundle

from .helpers import subprocess_env


def test_two_writers_cannot_overwrite_the_same_destination(make_paths, tmp_path):
    paths = make_paths({"rating": 1}, {"rating": 1}, {"rating": 1})
    destination = tmp_path / "race"
    arguments = [
        sys.executable,
        "-m",
        "mediawrench",
        "cull",
        "merge",
        *(str(paths[side]) for side in ("base", "left", "right")),
        "--history-source",
        "left",
        "--output-dir",
        str(destination),
    ]
    processes = [
        subprocess.Popen(
            arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=subprocess_env()
        )
        for _ in range(2)
    ]
    results = []
    try:
        for process in processes:
            _, error = process.communicate(timeout=15)
            results.append((process.returncode, error))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    assert sorted(code for code, _ in results) == [0, 2]
    assert any(b"already exists" in error for _, error in results)
    assert verify_bundle(destination)["verified"] is True
