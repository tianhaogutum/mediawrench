"""Local subtitle repair. No network or executable invocation at runtime."""
from .core import CaptionError, Result, convert, parse_vtt

__all__ = ["CaptionError", "Result", "convert", "parse_vtt"]
__version__ = "0.1.0"
