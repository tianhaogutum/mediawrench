#!/usr/bin/env python3
"""Generate a deterministic two-recording demo for `mediawrench drift` (needs FFmpeg).

Writes examples/drift/output/ by default (git-ignored), or an empty directory you name:

  reference.wav       45 s, 16 kHz mono: seeded pink noise gated by an irregular,
                      speech-like envelope (FFmpeg's lavfi `anoisesrc`; nothing is downloaded)
  target.flac         the same audio as a second "recorder": 0.2 s later and about
                      +500 ppm fast (FFmpeg `asetrate`/`aresample`/`adelay`)
  ground-truth.json   the transform, so you can compare it with the fitted correction

Then, from examples/drift/:

  mediawrench drift analyze output/reference.wav output/target.flac --out output/review.json
  mediawrench drift inspect output/review.json
"""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
NOISE = "anoisesrc=d=45:c=pink:r=48000:a=0.5:s=20261117"
ENVELOPE = ("aeval='val(0)*(0.12+0.88*gt(sin(2*PI*2.7*t)+0.6*sin(2*PI*0.41*t+1.3)"
            "+0.3*sin(2*PI*5.3*t),0.15))',lowpass=f=6000")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, nargs="?", default=HERE / "output",
                        help="empty output directory (default: examples/drift/output)")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("choose an empty output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    reference = args.output / "reference.wav"
    target = args.output / "target.flac"
    base = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1"]
    subprocess.run([*base, "-f", "lavfi", "-i", NOISE, "-af", ENVELOPE, "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(reference)], check=True, timeout=45)
    subprocess.run([*base, "-i", str(reference), "-af",
                    "asetrate=15992,aresample=16000,adelay=3200S:all=1", "-c:a", "flac", str(target)],
                   check=True, timeout=45)
    (args.output / "ground-truth.json").write_text(json.dumps({
        "scale": 16000 / 15992, "fixed_target_delay_seconds": 0.2,
        "drift_ppm": (16000 / 15992 - 1) * 1e6,
        "source": "synthetic: FFmpeg anoisesrc pink noise, seed 20261117, speech-like envelope",
    }, indent=2) + "\n")
    print(f"Created {reference} and {target}")


if __name__ == "__main__":
    main()
