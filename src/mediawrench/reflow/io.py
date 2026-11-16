"""Strict local JSON and verified pysubs2 SRT serialization; atomic file replacement."""
import html
import json
import os
from pathlib import Path
import tempfile

import pysubs2

from .model import InputError

# Report the parser that actually performed the roundtrip check.
_PYSUBS2_VERSION = str(getattr(pysubs2, "__version__", None) or getattr(pysubs2, "VERSION", "unknown"))

MAX_INPUT_BYTES = 64 * 1024 * 1024


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InputError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def load_json(path):
    with Path(path).open("rb") as fp:
        raw = fp.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("input exceeds 64 MiB limit")
    def invalid_constant(value):
        raise InputError(f"non-finite JSON constant: {value}")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InputError(f"invalid UTF-8 JSON: {exc}") from exc


def srt_text(report):
    subs = pysubs2.SSAFile()
    for cue in report["cues"]:
        # Entity-escape literal markup, ASS override braces, and ASS whitespace
        # sequences. All remain exact plain text after HTML entity decoding.
        escaped = html.escape(cue["text"], quote=False)
        escaped = escaped.replace("{", "&#123;").replace("}", "&#125;").replace("\\", "&#92;")
        subs.append(pysubs2.SSAEvent(start=cue["start_ms"], end=cue["end_ms"], text=escaped.replace("\n", r"\N")))
    encoded = subs.to_string("srt", keep_ssa_tags=True)
    decoded = pysubs2.SSAFile.from_string(encoded, format_="srt", keep_html_tags=True)
    actual = [(s.start, s.end, html.unescape(s.text.replace(r"\N", "\n"))) for s in decoded]
    expected = [(c["start_ms"], c["end_ms"], c["text"]) for c in report["cues"]]
    if actual != expected:
        raise InputError("SRT roundtrip cannot preserve this content (e.g. literal pairs of SRT timestamps); no output written")
    report["srt_roundtrip"] = {"library": "pysubs2", "version": _PYSUBS2_VERSION, "verified": True,
        "literal_text_encoding": "HTML entities; decode entities after reading raw SRT text"}
    return encoded


def json_text(report):
    return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def validate_paths(input_path, *outputs):
    paths = [Path(p) for p in (input_path, *outputs) if p is not None]
    for i, path in enumerate(paths):
        for other in paths[:i]:
            if path.resolve() == other.resolve() or (path.exists() and other.exists() and path.samefile(other)):
                raise InputError("input, profiles, SRT and report must be distinct files (including symlinks/hardlinks)")


def atomic_write_many(outputs):
    """Stage/fsync every file first, then atomically replace each destination.

    The two renames are not a cross-file transaction in the event of power loss.
    Parent directories must already exist; temporary files are always cleaned up.
    """
    outputs = list(outputs)
    for path, _ in outputs:
        if Path(path).is_dir():
            raise InputError(f"output destination is a directory: {path}")
    staged = []
    try:
        for path, text in outputs:
            path = Path(path)
            # Resolve symlinks so replacement updates the intended file and
            # validate_paths has already checked collisions at that destination.
            path = path.resolve()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as fp:
                staged.append((Path(fp.name), path))
                fp.write(text)
                fp.flush()
                os.fsync(fp.fileno())
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
