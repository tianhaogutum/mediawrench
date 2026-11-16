"""Small immutable internal records; all public times in the report name their units."""
from dataclasses import dataclass
from typing import Any


class InputError(ValueError):
    """Unsupported, ambiguous, or unsafe-to-render input."""


@dataclass(frozen=True)
class Config:
    max_chars: int = 42
    target_duration: float = 3.0
    max_duration: float = 6.0
    max_cps: float = 20.0
    pause: float = 0.5
    width_mode: str = "codepoint"
    min_duration: float = 0.0
    min_gap: float = 0.0
    profile_name: str | None = None
    cps_mode: str = "codepoint"

    @classmethod
    def from_profile(cls, profile, **layout_options):
        """Resolve bounds and optional saved rate units; reject conflicting units."""
        required = {"name", "max_cps", "min_duration", "min_gap"}
        if (not isinstance(profile, dict) or not required <= profile.keys()
                or profile.keys() - required - {"cps_mode"}):
            raise InputError("reading profile requires name, max_cps, min_duration, min_gap; only cps_mode is optional")
        if profile["name"] is None:
            raise InputError("reading profile name must be a nonblank printable string")
        if {"profile_name", "max_cps", "min_duration", "min_gap"} & layout_options.keys():
            raise InputError("cannot override reading profile bounds/name; edit or copy the profile instead")
        if "cps_mode" in profile:
            mode = profile["cps_mode"]
            if mode not in ("codepoint", "grapheme"):
                raise InputError("profile cps_mode must be codepoint or grapheme")
            if "cps_mode" in layout_options and layout_options["cps_mode"] != mode:
                raise InputError("explicit cps_mode conflicts with saved profile cps_mode; edit or copy the profile instead")
            layout_options["cps_mode"] = mode
        return cls(profile_name=profile["name"], max_cps=profile["max_cps"],
                   min_duration=profile["min_duration"], min_gap=profile["min_gap"], **layout_options)

    def __post_init__(self):
        import math
        if self.width_mode not in ("codepoint", "display"):
            raise InputError("width_mode must be codepoint or display")
        if self.cps_mode not in ("codepoint", "grapheme"):
            raise InputError("cps_mode must be codepoint or grapheme")
        if self.profile_name is not None and (not isinstance(self.profile_name, str)
                or not self.profile_name.strip() or len(self.profile_name) > 80
                or any(not c.isprintable() for c in self.profile_name)):
            raise InputError("profile_name must be a nonblank printable string of at most 80 characters")
        if isinstance(self.max_chars, bool) or not isinstance(self.max_chars, int) or not 1 <= self.max_chars <= 500:
            raise InputError("max_chars must be an integer between 1 and 500")
        for name in ("target_duration", "max_duration", "max_cps", "pause"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 < value <= 10000 or not math.isfinite(value)):
                raise InputError(f"{name} must be finite and positive")
        for name in ("min_duration", "min_gap"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 3600 or not math.isfinite(value)
                    or 0 < value < .001):
                raise InputError(f"{name} must be zero (disabled) or between .001 and 3600 seconds")
        if self.target_duration > self.max_duration:
            raise InputError("target_duration cannot exceed max_duration")
        if self.max_duration > 3600 or self.max_cps > 10000 or self.pause > 3600:
            raise InputError("max_duration/pause <= 3600 seconds and max_cps <= 10000 required")
        if self.target_duration < 0.001 or self.max_duration < 0.001 or self.pause < 0.001:
            raise InputError("durations and pause must be at least one millisecond")


@dataclass(frozen=True)
class Unit:
    id: str
    text: str
    start_ms: int
    end_ms: int
    word_ids: tuple[str, ...]
    source: dict[str, Any]
    fallback: bool = False


@dataclass
class Normalized:
    schema: str
    words: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    units: list[Unit]
    diagnostics: list[dict[str, Any]]
    top_level_text: str | None = None
