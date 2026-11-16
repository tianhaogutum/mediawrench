"""``mediawrench qc``: one read-only pre-delivery gate for a finished audio/video file.

Every check calls the library of another subcommand (postflight, loudness, chapters,
cutlist/rollup subtitle parsers, reflow reading limits). Nothing is written.
"""
from .. import __version__

SCHEMA = "mediawrench.qc/v1"

__all__ = ["SCHEMA", "__version__"]
