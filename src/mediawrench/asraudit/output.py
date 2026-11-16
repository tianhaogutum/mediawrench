"""Human review sidecars and collision-safe, atomic file publication."""

import csv
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import io
import json
import os
from pathlib import Path
import tempfile

from .core import AuditError
from .adapters import WHISPER_CPP_FORMAT


def json_report(report):
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def csv_flags(report):
    output = io.StringIO(newline="")
    fields = ["segment", "start", "end", "text", "reasons", "timestamp_valid",
              "chars_per_second", "no_speech_prob", "avg_logprob",
              "outside_speech_fraction", "speech_corroborated_repeat",
              "loop_settings", "loop_search", "intra_segment_loops", "token_evidence"]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for flag in report["flags"]:
        row = {key: flag[key] for key in fields}
        row["reasons"] = ";".join(flag["reasons"])
        for key in ("loop_settings", "loop_search", "intra_segment_loops"):
            row[key] = json.dumps(row[key], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if row["token_evidence"] is not None:
            row["token_evidence"] = json.dumps(row["token_evidence"], ensure_ascii=False,
                                               allow_nan=False, separators=(",", ":"))
        writer.writerow(row)
    return output.getvalue()


def validate_token_csv_segments(report, segment_indices):
    """Return a source-index set (None means all) for a validated audit report."""
    if segment_indices is None:
        return None
    if isinstance(segment_indices, (str, bytes)):
        raise AuditError("token CSV segments: expected an iterable of source segment indices")
    try:
        indices = iter(segment_indices)
    except TypeError:
        raise AuditError("token CSV segments: expected an iterable of source segment indices") from None
    selected = set()
    for index in indices:
        if type(index) is not int or not 0 <= index < len(report["segments"]):
            raise AuditError("token CSV segment: expected an existing nonnegative integer source segment index")
        selected.add(index)
    return selected


def csv_tokens(report, *, timing="all", segment_indices=None):
    """Validated native flag tokens, filtered by source indices and raw timing."""
    if timing not in ("all", "timed", "untimed"):
        raise AuditError("token CSV timing: expected all, timed or untimed")
    if report.get("input_format") != WHISPER_CPP_FORMAT:
        raise AuditError(f"token CSV requires input_format={WHISPER_CPP_FORMAT}")
    selected = validate_token_csv_segments(report, segment_indices)
    output = io.StringIO(newline="")
    fields = ["segment", "token_index", "source_path", "text", "id", "p", "t_dtw",
              "offsets", "timestamps"]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for flag in report["flags"]:
        if selected is not None and flag["segment"] not in selected:
            continue
        view = flag["token_evidence"]
        if view is None:
            continue
        for index, entry in enumerate(view["tokens"]):
            token = entry["token"]
            timed = "offsets" in token and "timestamps" in token
            if (timing == "timed" and not timed) or (timing == "untimed" and timed):
                continue
            row = {key: token[key] for key in ("text", "id", "p", "t_dtw")}
            row.update(segment=flag["segment"], token_index=index, source_path=entry["source_path"])
            for key in ("offsets", "timestamps"):
                row[key] = (json.dumps(token[key], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                            if key in token else "")
            writer.writerow(row)
    return output.getvalue()


def _srt_time(seconds, rounding):
    value = Decimal(str(seconds))
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + 4)
        millis = int((value * 1000).to_integral_value(rounding=rounding))
    hours, remainder = divmod(millis, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def review_srt(report):
    """Navigation annotations only; invalid timestamps remain in JSON/CSV."""
    cues = []
    for i, item in enumerate(report["review_ranges"], 1):
        start = _srt_time(item["start"], ROUND_FLOOR)
        end = _srt_time(item["end"], ROUND_CEILING)
        indices = ", ".join(map(str, item["segments"]))
        reasons = ", ".join(item["reasons"])
        cues.append(f"{i}\n{start} --> {end}\nReview segments: {indices}\nReasons: {reasons}\n\n")
    return "".join(cues)


def _identity(path):
    path = Path(path)
    try:
        info = path.stat()
        return info.st_dev, info.st_ino
    except FileNotFoundError:
        return None


def validate_destinations(paths, protected):
    """No overwrites at all, including symlink/hardlink aliases of inputs."""
    protected_paths = {Path(p).resolve() for p in protected}
    protected_ids = {_identity(p) for p in protected} - {None}
    seen = set()
    for value in paths:
        path = Path(value)
        resolved = path.resolve()
        if resolved in protected_paths or _identity(path) in protected_ids:
            raise AuditError(f"output aliases an input: {path}")
        if resolved in seen:
            raise AuditError(f"duplicate output destination: {path}")
        seen.add(resolved)
        if path.exists() or path.is_symlink():
            raise AuditError(f"output already exists (choose a new path): {path}")
        if not path.parent.is_dir():
            raise AuditError(f"output directory does not exist: {path.parent}")


def atomic_outputs(outputs, protected):
    """Stage all bytes before publishing; each file is atomic and no-clobber.

    This is not a multi-file transaction. An OS failure during publication can
    leave a subset of complete sidecars; no partial file or input overwrite.
    """
    validate_destinations(list(outputs), protected)
    staged = []
    try:
        for destination, content in outputs.items():
            path = Path(destination)
            fd, temporary = tempfile.mkstemp(prefix=".asr-audit-", dir=path.parent)
            staged.append((temporary, path))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, path in staged:
            # link() publishes a fully written file without replacing a racing
            # destination. Both names are on the same filesystem.
            os.link(temporary, path)
    finally:
        for temporary, _ in staged:
            Path(temporary).unlink(missing_ok=True)
