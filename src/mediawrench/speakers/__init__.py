"""Existing-file speaker assignment; no model or network dependencies."""

from .core import BridgeError, Diarization, merge, parse_rttm
from .export import render_json, render_markdown, render_srt, write_atomic
from .manifest import (diagnose_manifest_input_coverage, diagnose_manifest_options,
                       run_manifest, validate_manifest)

__version__ = "0.1.0"
__all__ = [
    "BridgeError", "Diarization", "merge", "parse_rttm", "render_json",
    "render_markdown", "render_srt", "write_atomic", "run_manifest", "validate_manifest",
    "diagnose_manifest_options", "diagnose_manifest_input_coverage",
]
