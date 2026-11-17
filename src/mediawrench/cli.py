"""Top-level ``mediawrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``mediawrench --help`` stays fast and a broken or missing
optional dependency only affects the command that needs it.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "mediawrench"
REPO_URL = "https://github.com/tianhaogutum/mediawrench"


@dataclass(frozen=True)
class Command:
    name: str
    module: str
    summary: str
    group: str
    extra: str | None = None
    requires: tuple[str, ...] = ()
    # External executables the command runs (never installed by mediawrench).
    binaries: tuple[str, ...] = ()


GROUPS = ("Subtitles & captions", "Transcripts & speech", "Audio & video files", "Photos")

COMMANDS: tuple[Command, ...] = (
    Command("rollup", "mediawrench.rollup.cli",
            "Repair roll-up WebVTT captions into clean SRT/VTT with token provenance",
            "Subtitles & captions"),
    Command("reflow", "mediawrench.reflow.cli",
            "Reflow ASR word timestamps into readable SRT cues (width, duration, reading speed)",
            "Subtitles & captions"),
    Command("audacity", "mediawrench.audacity.cli",
            "Round-trip SRT/WebVTT timing edits through Audacity labels with a loss audit",
            "Subtitles & captions"),
    Command("cutsync", "mediawrench.cutsync.cli",
            "Apply a video's keep ranges to its sidecar SRT; new SRT plus a per-cue change list",
            "Subtitles & captions"),
    Command("asr-audit", "mediawrench.asraudit.cli",
            "Flag suspicious ASR segments (repeats, loops, off-VAD speech) for human review",
            "Transcripts & speech"),
    Command("speakers", "mediawrench.speakers.cli",
            "Join diarization RTTM with ASR JSON into speaker-labelled JSON, Markdown or SRT",
            "Transcripts & speech"),
    Command("cutlist", "mediawrench.cutlist.cli",
            "Turn SRT cue selections into auditable LosslessCut cutlists, CSV and provenance",
            "Transcripts & speech", binaries=("ffprobe",)),
    Command("chunks", "mediawrench.chunks.cli",
            "Split PCM WAV into sample-exact ASR chunks and stitch chunk transcripts back",
            "Transcripts & speech"),
    Command("qc", "mediawrench.qc.cli",
            "Pre-delivery QC gate: decode, duration, loudness, chapters and subtitles vs the master",
            "Audio & video files", binaries=("ffmpeg", "ffprobe")),
    Command("loudness", "mediawrench.loudness.cli",
            "Loudness-normalize audio; publish only after the final file passes LUFS/true peak",
            "Audio & video files", binaries=("ffmpeg", "ffprobe")),
    Command("postflight", "mediawrench.postflight.cli",
            "Decode a transcode against its source stream by stream: PASS, FAIL or UNKNOWN",
            "Audio & video files", binaries=("ffmpeg", "ffprobe")),
    Command("chapters", "mediawrench.chapters.cli",
            "Convert chapters between JSON, FFmetadata, text and Matroska XML, loss-audited",
            "Audio & video files", binaries=("ffprobe",)),
    Command("drift", "mediawrench.drift.cli",
            "Review sync points between two drifting recordings; render a clock-corrected WAV",
            "Audio & video files", extra="drift", requires=("numpy", "scipy", "soxr", "soundfile"),
            binaries=("ffmpeg", "ffprobe")),
    Command("cull", "mediawrench.cull.cli",
            "Three-way merge of photo XMP sidecars: stars, colour labels and pick flags only",
            "Photos", extra="cull", requires=("lxml", "simplejson")),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "caption-rollup-repair": "rollup", "wordcue-reflow": "reflow", "audacity-subtitle-bridge": "audacity",
    "asr-evidence-audit": "asr-audit", "interview-speaker-bridge": "speakers",
    "transcript-cutlist": "cutlist", "audio-chunk-manifest": "chunks", "loudnorm-verify": "loudness",
    "transcode-postflight": "postflight", "chapter-roundtrip": "chapters",
    "srt-cut-sync": "cutsync", "drift-review": "drift", "cullmerge": "cull",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def _missing_requirements(command: Command) -> bool:
    return any(not _present(name) for name in command.requires)


def _install_hint(command: Command) -> str:
    return (f"{PROG} {command.name}: missing optional dependencies; "
            f"install them with: pip install '{PROG}[{command.extra}]'")


def _help_without_extra(command: Command) -> str:
    """A usage summary that needs only the command's package, not its optional extra."""
    lines = [f"usage: {PROG} {command.name} <subcommand> [args...]", "", command.summary + "."]
    try:
        subcommands = getattr(importlib.import_module(command.module.rpartition(".")[0]), "SUBCOMMANDS", {})
    except ImportError:
        subcommands = {}
    if subcommands:
        width = max(map(len, subcommands)) + 2
        lines += ["", "subcommands:", *(f"  {name.ljust(width)}{text}" for name, text in subcommands.items())]
    return "\n".join(lines) + "\n\n"


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline, auditable tools for subtitles, transcripts, audio, video and photo culls",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = ""
            if not _installed(command):
                note = "  (coming soon)"
            elif command.extra and _missing_requirements(command):
                note = f"  (needs {PROG}[{command.extra}])"
            marker = "*" if command.binaries else " "
            lines.append(f" {marker}{command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        "* uses external FFmpeg executables (ffmpeg/ffprobe) for some or all operations;",
        "  install FFmpeg separately, mediawrench never downloads it.",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    if command.extra and _missing_requirements(command):
        if any(arg in ("-h", "--help") for arg in argv):
            sys.stdout.write(_help_without_extra(command))
            sys.stdout.write(_install_hint(command) + "\n")
            return 0
        sys.stderr.write(_install_hint(command) + "\n")
        return 2
    try:
        module = importlib.import_module(command.module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if command.extra and any(req == missing or req.startswith(missing + ".") for req in command.requires):
            sys.stderr.write(_install_hint(command) + "\n")
            return 2
        raise
    result = module.main(argv)
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
