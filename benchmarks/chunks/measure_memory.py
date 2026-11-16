"""Peak-RSS gradient for `mediawrench chunks split` on 8/32/128 MiB of PCM.

Each measurement runs in a fresh isolated (-I) Python process, so run it with the
interpreter mediawrench is installed into. Data generation happens in the parent
in 64K-frame blocks, outside the measured workers. No silence detector: this
measures PCM copying/hashing plus the in-memory chunk plan. `ru_maxrss` is KiB on
Linux and bytes on macOS. This is a reproducibility measurement, not a memory or
speed claim.

    python benchmarks/chunks/measure_memory.py --output /tmp/chunks-memory --label current
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import wave


WORKER = r'''
import json, resource, sys
import mediawrench.chunks
from mediawrench.chunks import split
source, destination, mode = sys.argv[1:]
options = dict(max_seconds="30", overlap_seconds="1")
if mode != "baseline":
    options["resume"] = True
manifest = split(source, destination, **options)
print(json.dumps(dict(peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      chunk_count=len(manifest["chunks"]),
                      frames=manifest["source"]["total_samples"],
                      source_sha256=manifest["source"]["sha256"],
                      complete=manifest.get("complete"),
                      package_path=mediawrench.chunks.__file__)))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Evidence/data directory")
    parser.add_argument("--label", required=True, help="Unique output label, e.g. before/after")
    parser.add_argument("--baseline", action="store_true", help="Measure ordinary split instead of resumable split and completed-resume validation")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    evidence_file = root / (args.label + ".json")
    if evidence_file.exists():
        parser.error("Label already measured; choose a new label")
    results = []
    # Exactly 8, 32, 128 MiB of 48kHz stereo signed-16-bit PCM.
    block = b"\x01\x00\xff\xff" * 65536
    for mib in (8, 32, 128):
        source = root / f"{mib}MiB.wav"
        frames = mib * 1024 * 1024 // 4
        if not source.exists():
            with wave.open(str(source), "wb") as audio:
                audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                remaining = frames
                while remaining:
                    count = min(65536, remaining)
                    audio.writeframesraw(block[:count * 4])
                    remaining -= count
        destination = root / f"{args.label}-{mib}MiB"
        modes = ["baseline"] if args.baseline else ["fresh", "verify-complete"]
        for mode in modes:
            argv = [sys.executable, "-I", "-c", WORKER, str(source), str(destination), mode]
            process = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            if process.returncode:
                raise RuntimeError(process.stderr)
            result = json.loads(process.stdout)
            assert result["frames"] == frames
            results.append(dict(input_pcm_mib=mib, mode=mode, **result))
    evidence = dict(label=args.label, python=sys.version, platform=sys.platform,
                    metric="resource.getrusage(RUSAGE_SELF).ru_maxrss (KiB on Linux, bytes on macOS); one fresh -I process per row",
                    parameters=dict(rate=48000, channels=2, width=2, max_seconds=30, overlap_seconds=1,
                                    silence_detection=False), results=results)
    evidence_file.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
