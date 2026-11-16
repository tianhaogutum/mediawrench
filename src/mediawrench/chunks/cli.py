"""Command-line interface. No external commands, models, or network access."""
import argparse
import json
import sys
import wave

from .common import ManifestError
from .split import split
from .stitch import stitch


def parser():
    root = argparse.ArgumentParser(prog="mediawrench chunks", description="Sample-exact PCM WAV chunks and guarded offline transcript stitching.")
    root.add_argument("--version", action="version", version="mediawrench chunks 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    split_cmd = commands.add_parser("split", help="Write chunks plus manifest, or a plan without audio", description="Write chunks plus manifest, or a plan without audio")
    split_cmd.add_argument("source", help="Integer PCM WAV recording")
    split_cmd.add_argument("output_dir", help="New output directory, or matching checkpoint with --resume")
    split_cmd.add_argument("--max-seconds", default="30", help="Maximum chunk length in seconds (default: 30)")
    split_cmd.add_argument("--overlap-seconds", default="1", help="Overlap between neighbouring chunks in seconds (default: 1)")
    split_cmd.add_argument("--search-seconds", default="5", help="Search this far before each hard limit for silence")
    silence = split_cmd.add_mutually_exclusive_group()
    silence.add_argument("--silence-json", help="Ordered intervals object with start/end decimal seconds")
    silence.add_argument("--detect-silence", action="store_true", help="Detect windows quiet on every channel")
    split_cmd.add_argument("--threshold-db", default="-40", help="Silence threshold in dBFS for --detect-silence (default: -40)")
    split_cmd.add_argument("--window-seconds", default="0.02", help="Analysis window for --detect-silence (default: 0.02)")
    split_cmd.add_argument("--min-silence-seconds", default="0.1", help="Shortest silence accepted as a cut point (default: 0.1)")
    split_cmd.add_argument("--plan-only", action="store_true", help="Write manifest only; no audio or external execution")
    split_cmd.add_argument("--resume", action="store_true", help="Checkpoint chunks; recover matching interrupted splits (Linux/POSIX)")
    join_cmd = commands.add_parser("stitch", help="Verify audio and join chunk-id-named JSON files", description="Verify audio and join chunk-id-named JSON files")
    join_cmd.add_argument("manifest_path", help="manifest.json written by split")
    join_cmd.add_argument("transcript_dir", help="Directory with one chunk-NNNNNN.json ASR transcript per chunk")
    join_cmd.add_argument("output_json", help="New merged.json; also writes merged.srt and merged.report.json")
    join_cmd.add_argument("--native-json", action="store_true",
                          help="Allow unbound native ASR JSON with weaker identity guarantees (reported)")
    return root


def main(argv=None):
    args = vars(parser().parse_args(argv))
    command = args.pop("command")
    try:
        if command == "split":
            manifest = split(**args)
            print(json.dumps({"chunks": len(manifest["chunks"]), "plan_only": manifest["plan_only"],
                              "source_samples": manifest["source"]["total_samples"]}))
            return 0
        result = stitch(**args)
        report = result["report"]
        print(json.dumps({key: report[key] for key in ("status", "input_segments", "output_segments", "duplicate_count", "conflict_count", "identity")}))
        for warning in report["identity"]["warnings"]:
            print(warning, file=sys.stderr)
        if report["conflicts"]:
            print("Conflicting overlap transcripts preserved; inspect .report.json before using subtitles.", file=sys.stderr)
            return 3
        return 0
    except (ManifestError, OSError, wave.Error, EOFError) as exc:
        print(f"mediawrench chunks: {exc}", file=sys.stderr)
        return 2
