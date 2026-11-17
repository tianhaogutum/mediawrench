"""Use srt's own grammar, guarding ambiguous noncanonical fractional fields."""

from __future__ import annotations

import math
import multiprocessing
import signal
from multiprocessing.connection import Connection
from typing import Literal, cast

import srt

from .core import CutSyncError, Interval

MAX_CUES = 100_000


def parse_subtitles(
    text: str,
    *,
    short_fractions: Literal["reject", "decimal", "milliseconds"] = "reject",
    max_cues: int = MAX_CUES,
) -> tuple[list[srt.Subtitle], list[dict]]:
    """Normalize only timestamp spans recognized by the upstream SRT grammar.

    `srt` and FFmpeg treat ,95 as 95 ms; pysubs2 treats it as 950 ms. SRT's
    canonical millisecond field has three digits. Do not make this decision
    silently, and never rewrite timestamp-like dialogue text.
    """
    if short_fractions not in ("reject", "decimal", "milliseconds"):
        raise CutSyncError(f"unknown short fraction policy: {short_fractions}")
    if isinstance(max_cues, bool) or not isinstance(max_cues, int) or max_cues <= 0:
        raise CutSyncError("maximum cue count must be a positive integer")
    if "\x00" in text:
        raise CutSyncError("SRT contains NUL characters; check --input-encoding")
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return [], []
    edits = []
    normalizations = []
    for position, cue_match in enumerate(srt.SRT_REGEX.finditer(text), 1):
        if position > max_cues:
            raise CutSyncError(f"input exceeds {max_cues:,}-cue limit")
        raw_index = cue_match.group(1)
        if raw_index is not None and "." in raw_index:
            raise CutSyncError(f"cue {position}: fractional cue numbering is not supported")
        for group, field in ((2, "start"), (3, "end")):
            raw = cue_match.group(group)
            if len(raw) > 100:
                raise CutSyncError(f"cue {position} {field}: timestamp is too long")
            timestamp_match = srt.TS_REGEX.fullmatch(raw)
            assert timestamp_match is not None  # Both expressions are supplied by srt.
            minutes, seconds, fraction = timestamp_match.group(2, 3, 4)
            if int(minutes) >= 60 or int(seconds) >= 60:
                raise CutSyncError(f"cue {position} {field}: minutes and seconds must be below 60")
            if not fraction or len(fraction) == 3:
                continue
            if len(fraction) > 3:
                raise CutSyncError(f"cue {position} {field}: more than three fractional digits")
            if short_fractions == "reject":
                raise CutSyncError(
                    f"cue {position} {field}: ambiguous timestamp {raw!r}; "
                    "use --short-fractions decimal or milliseconds explicitly"
                )
            digits = fraction.ljust(3, "0") if short_fractions == "decimal" else fraction.zfill(3)
            normalized = raw[: timestamp_match.start(4)] + digits
            edits.append((cue_match.start(group), cue_match.end(group), normalized))
            normalizations.append(
                {
                    "source_position": position,
                    "field": field,
                    "original": raw,
                    "normalized": normalized,
                    "interpretation": short_fractions,
                }
            )
    if edits:
        chunks = []
        last = 0
        for start, end, normalized in edits:
            chunks.extend((text[last:start], normalized))
            last = end
        chunks.append(text[last:])
        text = "".join(chunks)
    try:
        return list(srt.parse(text)), normalizations
    except (srt.SRTParseError, ValueError, OverflowError) as exc:
        raise CutSyncError(f"invalid SRT: {exc}") from exc


def _parse_worker(sender: Connection, job: str, text: str, options: dict) -> None:
    # The parent handles Ctrl-C and always reaps this worker.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if job == "SRT":
            result = parse_subtitles(text, **options)
        elif job == "keep-list":
            from .cuts import load_keep

            result = load_keep(text, **options)
        else:
            raise CutSyncError("unknown parser job")
        sender.send(("ok", result))
    except Exception as exc:
        sender.send(("error", f"{type(exc).__name__}: {str(exc)[:1500]}"))
    finally:
        sender.close()


def _parse_in_worker(
    job: str,
    text: str,
    options: dict,
    timeout: float,
) -> object:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise CutSyncError("parser timeout must be greater than 0 and at most 60 seconds")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_parse_worker, args=(sender, job, text, options), daemon=True)
    try:
        worker.start()
        sender.close()
        if not receiver.poll(timeout):
            raise CutSyncError(
                f"{job} parser exceeded {timeout:g} seconds; input may be malformed; "
                "no output was published"
            )
        try:
            result = receiver.recv()
        except EOFError as exc:
            raise CutSyncError(f"{job} parser process exited without a result") from exc
        if result[0] == "error":
            raise CutSyncError(result[1])
        return result[1]
    finally:
        sender.close()
        if worker.pid is not None:
            worker.join(timeout=0.2)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=2)
            worker.close()
        receiver.close()


def parse_for_cli(
    text: str,
    *,
    short_fractions: str = "reject",
    timeout: float = 10,
) -> tuple[list[srt.Subtitle], list[dict]]:
    """Bound the unchanged upstream regex parser in a disposable local process.

    A byte limit alone does not prevent catastrophic regex backtracking. No
    child survives success, failure, timeout, or a parent-handled interrupt.
    This helper is used from the CLI's guarded main entry point.
    """
    return cast(
        tuple[list[srt.Subtitle], list[dict]],
        _parse_in_worker(
            "SRT",
            text,
            {"short_fractions": short_fractions},
            timeout,
        ),
    )


def parse_keep_for_cli(text: str, *, timeout: float = 10, **options: object) -> list[Interval]:
    """Apply the same isolation/time budget to CSV, JSON and native JSON5."""
    return cast(list[Interval], _parse_in_worker("keep-list", text, options, timeout))
