"""Exact-decimal JSON with duplicate-key rejection for human review files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .xmp import MergeError, read_bytes


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MergeError(f"duplicate JSON key {key!r}; each decision must be unambiguous")
        result[key] = value
    return result


def load_document(path: Path) -> dict[str, Any]:
    try:
        # The standard decoder recognizes UTF-8 BOM and UTF-16/32 review files
        # emitted by Windows text editors, with exact Decimal values retained.
        document = json.loads(
            read_bytes(path, max_bytes=1024 * 1024),
            parse_float=Decimal,
            object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError, ArithmeticError) as exc:
        raise MergeError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise MergeError(f"{path}: expected a JSON object")
    return document


def _invalid_constant(value: str) -> None:
    raise MergeError(f"non-finite JSON number {value!r} is not supported")
