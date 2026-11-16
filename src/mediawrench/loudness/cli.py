"""Command line entry point. No downloads or network activity."""
import argparse
import json
from pathlib import Path
import signal
import threading

from .core import (AudioError, DeadlineExceeded, JobCancelled, Profile, Toolchain,
                   list_streams, normalize, verify_candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mediawrench loudness", description=(
        "Normalize local mono/stereo PCM WAV, FLAC or a PCM/FLAC track in Matroska; publish a 48 kHz/24-bit WAV "
        "only after fresh final-file LUFS and true-peak verification. Existing files are never replaced."))
    parser.add_argument("input", type=Path, help="Local WAV, FLAC or Matroska input (the WAV to check with --verify-only)")
    parser.add_argument("output", type=Path, nargs="?", help="New WAV path (normalization only)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-streams", action="store_true",
                        help="Read-only Matroska stream inventory as JSON; no output/profile/report arguments")
    modes.add_argument("--verify-only", action="store_true",
                       help="Read-only verification of an existing 48 kHz/24-bit PCM WAV; no output/report")
    parser.add_argument("--audio-stream", type=int, action="append", metavar="INDEX",
                        help="Absolute ffprobe stream index (not audio ordinal); required for multiple audio tracks")
    parser.add_argument("--target-i", type=float, help="Required for normalization/verification: integrated LUFS, -70 to -5")
    parser.add_argument("--target-tp", type=float, help="Required for normalization/verification: hard true-peak ceiling dBTP, -9 to 0")
    parser.add_argument("--tolerance", type=float, help="Required for normalization/verification: LUFS tolerance, >0 to 2; no peak allowance")
    parser.add_argument("--target-lra", type=float, help="Loudnorm target LRA, 1 to 50 (default: 11)")
    parser.add_argument("--report", type=Path, help="Required for normalization: new JSON report path (also retained on failure)")
    parser.add_argument("--ffmpeg", help="External FFmpeg executable path (default: ffmpeg; normalization/verification)")
    parser.add_argument("--ffprobe", default="ffprobe", help="External ffprobe executable path")
    parser.add_argument("--timeout", default=120.0, type=float, help="Per-process timeout seconds (default: 120)")
    parser.add_argument("--total-timeout", default=600.0, type=float,
                        help="Total job budget seconds, >0 to 86400 (default: 600); includes all passes and publication")
    args = parser.parse_args(argv)
    normalization_args = ("output", "target_i", "target_tp", "tolerance", "target_lra",
                          "report", "audio_stream", "ffmpeg")
    if args.list_streams:
        if any(getattr(args, name) is not None for name in normalization_args):
            parser.error("--list-streams cannot be combined with output, profile, report, --audio-stream or --ffmpeg")
    elif args.verify_only:
        if any(getattr(args, name) is not None for name in ("output", "report", "audio_stream")):
            parser.error("--verify-only cannot be combined with output, --report or --audio-stream")
        missing = [name.replace("_", "-") for name in ("target_i", "target_tp", "tolerance")
                   if getattr(args, name) is None]
        if missing:
            parser.error("--verify-only requires: " + ", ".join(missing))
    else:
        required = ("output", "target_i", "target_tp", "tolerance", "report")
        missing = [name.replace("_", "-") for name in required if getattr(args, name) is None]
        if missing:
            parser.error("normalization requires: " + ", ".join(missing))
    if args.audio_stream is not None and len(args.audio_stream) != 1:
        parser.error("--audio-stream must be supplied exactly once when used")
    previous = {}
    def cancel(signum, frame):
        # One cancellation is enough; let process reaping, rollback and reporting finish.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, cancel)
        tools = Toolchain(args.ffmpeg or "ffmpeg", args.ffprobe, args.timeout, args.total_timeout)
        if args.list_streams:
            result = list_streams(args.input, tools)
        elif args.verify_only:
            result = {"schema_version": 1, "source": str(args.input.absolute()), "published": False}
            try:
                result.update(verify_candidate(
                    args.input, Profile(args.target_i, args.target_tp, args.tolerance,
                                        args.target_lra if args.target_lra is not None else 11.0), tools))
            except (AudioError, OSError, ValueError, KeyboardInterrupt) as exc:
                result["status"] = exc.status if isinstance(exc, AudioError) else "ERROR"
                result["error"] = "Job cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc)
                if isinstance(exc, OSError):
                    result["errno"] = exc.errno
                if isinstance(exc, DeadlineExceeded):
                    result["error_kind"] = "deadline_exceeded"
                elif isinstance(exc, (JobCancelled, KeyboardInterrupt)):
                    result["error_kind"] = "cancelled"
        else:
            result = normalize(args.input, args.output,
                               Profile(args.target_i, args.target_tp, args.tolerance,
                                       args.target_lra if args.target_lra is not None else 11.0),
                               args.report, tools,
                               audio_stream=args.audio_stream[0] if args.audio_stream else None)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return {"OK": 0, "PASS": 0, "FAIL": 2, "UNSUPPORTED": 3, "ERROR": 4}[result["status"]]
