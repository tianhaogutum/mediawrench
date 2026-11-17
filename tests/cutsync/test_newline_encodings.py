import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import srt

ROOT = Path(__file__).resolve().parents[2]
# Run through the top-level dispatcher; prefer this source tree over any installed copy.
CLI = [sys.executable, "-m", "mediawrench", "cutsync"]
ENV = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_real_ffmpeg_fixture_in_encoded_newline_variants(tmp_path, newline, encoding):
    original = (ROOT / "tests/cutsync/fixtures/ffmpeg-export.srt").read_text()
    source = tmp_path / "original.srt"
    raw = original.replace("\n", newline).encode(encoding)
    source.write_bytes(raw)
    keep = tmp_path / "keep.csv"
    keep.write_bytes(
        (ROOT / "examples/cutsync/keep.csv").read_text().replace("\n", newline).encode("utf-8-sig")
    )
    output = tmp_path / "edited.srt"
    result = subprocess.run(
        [
            *CLI,
            str(source),
            "--keep",
            str(keep),
            "-o",
            str(output),
            "--input-encoding",
            encoding,
        ],
        env=ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    cues = list(srt.parse(output.read_text()))
    assert len(cues) == 5
    assert cues[-1].end.total_seconds() == 6
    assert b"\r" not in output.read_bytes()
    audit = json.loads(output.with_suffix(".changes.json").read_text())
    assert audit["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert source.read_bytes() == raw
