"""Offline PCM chunk planning and conservative transcript stitching."""
from .common import ManifestError
from .split import split
from .stitch import stitch

__all__ = ["ManifestError", "split", "stitch"]
__version__ = "0.1.0"
