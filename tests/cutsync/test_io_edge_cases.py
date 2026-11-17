import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Run through the top-level dispatcher; prefer this source tree over any installed copy.
CLI = [sys.executable, "-m", "mediawrench", "cutsync"]
ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
}


def command(source, output, *extra):
    return [
        *CLI,
        str(source),
        "--keep",
        str(ROOT / "examples/cutsync/keep.csv"),
        "-o",
        str(output),
        *extra,
    ]


@pytest.mark.parametrize("dry_run", [True, False])
def test_ascii_console_does_not_turn_success_into_failure(tmp_path, dry_run):
    output = tmp_path / "output.srt"
    result = subprocess.run(
        command(ROOT / "examples/cutsync/interview.srt", output, *(["--dry-run"] if dry_run else [])),
        env={**ENV, "PYTHONIOENCODING": "ascii"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    if dry_run:
        assert json.loads(result.stdout)["summary"]["output_cues"] == 5
        assert not output.exists()
    else:
        assert output.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipe check")
def test_named_pipe_input_rejected_without_waiting_for_a_writer(tmp_path):
    source = tmp_path / "pipe.srt"
    os.mkfifo(source)
    result = subprocess.run(
        command(source, tmp_path / "output.srt"),
        env=ENV,
        capture_output=True,
        text=True,
        # Bounds a blocking open of the pipe; generous enough for a cold interpreter start.
        timeout=30,
    )
    assert result.returncode == 2
    assert "regular file" in result.stderr


def test_input_symlink_loop_has_clean_error(tmp_path):
    source = tmp_path / "loop.srt"
    source.symlink_to(source)
    result = subprocess.run(
        command(source, tmp_path / "output.srt"), env=ENV, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
