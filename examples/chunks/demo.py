"""Reproducible real WAV -> chunks -> offline JSON -> source JSON/SRT experiment.

No ASR is run: transcript strings and timestamps are synthetic ground truth.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave

# Also runs from a source clone without installing mediawrench.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mediawrench.chunks.common import json_bytes, read_json, sha256  # noqa: E402


def generate(path, *, rate=8000, duration=12):
    """16-bit mono tone, with synthetic silence at [3,4] and [7,8] seconds."""
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        for start in range(0, rate * duration, rate):
            block = bytearray()
            for frame in range(start, min(start + rate, rate * duration)):
                t = frame / rate
                amplitude = 0 if 3 <= t < 4 or 7 <= t < 8 else int(10000 * math.sin(2 * math.pi * 220 * t))
                block.extend(struct.pack("<h", amplitude))
            audio.writeframesraw(block)


def command(*args):
    root = Path(__file__).resolve().parents[2]
    # Prefer the source tree's package when run from a clone; an installed copy also works.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, [str(root / "src"), os.environ.get("PYTHONPATH")]))}
    result = subprocess.run([sys.executable, "-m", "mediawrench.chunks", *map(str, args)],
                            cwd=root, env=env, capture_output=True,
                            text=True, timeout=120, check=True)
    return json.loads(result.stdout)


def run(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "synthetic.wav"
    generate(source)
    started = time.perf_counter()
    command("split", source, directory / "chunks", "--max-seconds", "5", "--overlap-seconds", "1",
            "--detect-silence", "--search-seconds", "2")
    manifest = read_json(directory / "chunks/manifest.json")
    # Ground truth includes a duplicated overlap utterance and a real later repeat.
    truth = [(1, 2, "Hello synthetic world."), (3, 3.5, "Boundary phrase."),
             (6, 6.5, "Boundary phrase."), (9, 10, "Goodbye.")]
    transcripts = directory / "transcripts"
    transcripts.mkdir()
    input_segments = 0
    for chunk in manifest["chunks"]:
        offset = chunk["start_sample"] / 8000
        end = chunk["end_sample"] / 8000
        segments = []
        for start, stop, text in truth:
            if offset <= start < stop <= end:
                segments.append({"start": start - offset, "end": stop - offset, "text": text,
                                 "words": [{"start": start - offset, "end": stop - offset, "word": text,
                                            "probability": 1.0}]})
        input_segments += len(segments)
        (transcripts / (chunk["id"] + ".json")).write_bytes(json_bytes(
            {"chunk_id": chunk["id"], "sample_rate": 8000, "audio_sha256": chunk["sha256"],
             "recording_sha256": manifest["source"]["sha256"],
             "manifest_sha256": sha256(directory / "chunks/manifest.json"), "segments": segments}))
    result = command("stitch", directory / "chunks/manifest.json", transcripts, directory / "merged.json")
    merged = read_json(directory / "merged.json")
    expected = [(int(start * 8000), int(end * 8000), text) for start, end, text in truth]
    actual = [(s["start_sample"], s["end_sample"], s["text"]) for s in merged["segments"]]
    assert actual == expected, (actual, expected)
    # Check real emitted WAV frames against source bytes, not a mock codec.
    with wave.open(str(source), "rb") as audio:
        frames = audio.readframes(audio.getnframes())
    for chunk in manifest["chunks"]:
        path = directory / "chunks" / chunk["file"]
        with wave.open(str(path), "rb") as audio:
            assert audio.readframes(audio.getnframes()) == frames[chunk["start_sample"] * 2:chunk["end_sample"] * 2]
        assert sha256(path) == chunk["sha256"]
    metrics = {"source_samples": len(frames) // 2, "chunk_count": len(manifest["chunks"]),
               "chunk_ranges": [[c["start_sample"], c["end_sample"]] for c in manifest["chunks"]],
               "input_segments": input_segments, "output_segments": len(actual),
               "removed_duplicates": result["duplicate_count"], "conflicts": result["conflict_count"],
               "max_source_mapping_error_samples": 0,
               "baseline_concatenation_extra_segments": input_segments - len(truth),
               "wall_seconds": round(time.perf_counter() - started, 6)}
    assert metrics["removed_duplicates"] > 0
    (directory / "experiment.json").write_bytes(json_bytes(metrics))
    print(json.dumps(metrics, indent=2))
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Keep artifacts in a new/empty directory")
    args = parser.parse_args()
    if args.output:
        if args.output.exists() and any(args.output.iterdir()):
            parser.error("--output directory must be new or empty")
        run(args.output.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="mediawrench-chunks-demo-") as directory:
            run(directory)


if __name__ == "__main__":
    main()
