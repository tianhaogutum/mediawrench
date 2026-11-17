"""Exercise real terminal review, with a deadline and no background processes."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time

import pytest

pytest.importorskip("lxml", reason="mediawrench cull needs the 'cull' extra (lxml)")
pytest.importorskip("simplejson", reason="mediawrench cull needs the 'cull' extra (simplejson)")

from mediawrench.cull.xmp import Packet

from .helpers import subprocess_env

pytestmark = pytest.mark.skipif(os.name != "posix", reason="PTY review test requires POSIX")


def terminal_review(paths, destination, answers):
    import pty

    master, slave = pty.openpty()
    arguments = [
        sys.executable,
        "-m",
        "mediawrench",
        "cull",
        "merge",
        *(str(paths[side]) for side in ("base", "left", "right")),
        "--history-source",
        "left",
        "--interactive",
        "--output-dir",
        str(destination),
    ]
    process = subprocess.Popen(
        arguments, stdin=slave, stderr=slave, stdout=subprocess.PIPE, env=subprocess_env()
    )
    os.close(slave)
    transcript = bytearray()
    prompt_buffer = bytearray()
    pending = iter(answers)
    deadline = time.monotonic() + 10
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                raise AssertionError("terminal review exceeded its ten-second deadline")
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            transcript.extend(data)
            prompt_buffer.extend(data)
            while b"(or quit): " in prompt_buffer:
                _, _, remainder = prompt_buffer.partition(b"(or quit): ")
                prompt_buffer = bytearray(remainder)
                os.write(master, next(pending).encode() + b"\n")
        output, _ = process.communicate(timeout=2)
        return process.returncode, output, transcript.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        os.close(master)


def test_real_terminal_reviews_each_conflict(make_paths, tmp_path):
    paths = make_paths(
        {"rating": 1, "pick": 0}, {"rating": 3, "pick": 1}, {"rating": 5, "pick": -1}
    )
    destination = tmp_path / "out"
    code, output, transcript = terminal_review(paths, destination, ["right", "left"])
    assert code == 0, transcript
    assert "Conflict: xmp:Rating" in transcript
    assert "Conflict: xmpDM:pick" in transcript
    assert b'"rating": 5' in output
    assert Packet.load(destination / "merged.xmp").values["pick"] == 1


def test_real_terminal_invalid_answer_then_cancel(make_paths, tmp_path):
    paths = make_paths({"rating": 1}, {"rating": 3}, {"rating": 5})
    destination = tmp_path / "cancelled"
    code, _, transcript = terminal_review(paths, destination, ["not-a-side", "quit"])
    assert code == 2
    assert "Enter base, left, right, or quit." in transcript
    assert "review cancelled" in transcript
    assert not destination.exists()
