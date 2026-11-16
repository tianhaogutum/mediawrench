"""Offline ASR evidence triage. No model or media execution."""

__version__ = "0.1.0"

from .core import AuditError, Thresholds, audit, parse_json, validate_transcript

__all__ = ["AuditError", "Thresholds", "audit", "parse_json", "validate_transcript"]
