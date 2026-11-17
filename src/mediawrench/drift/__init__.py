"""``mediawrench drift``: review sync anchors between two drifting recordings.

A local adapter around ChronoSync's GCC-PHAT and linear fit (vendored in
``_chronosync``): propose anchors, require explicit approval, fit one clock-rate
correction and stream a corrected WAV onto the reference timeline. Needs the
``drift`` extra (NumPy, SciPy, soxr, SoundFile) and FFmpeg; this module imports
none of them so the dispatcher can print help without the extra.
"""
from .. import __version__

SUBCOMMANDS = {
    "analyze": "propose start/middle/end anchors; none are accepted automatically",
    "anchor": "accept, reject, or replace a time pair in a review JSON",
    "inspect": "show proposed/accepted pairs, confidence and missing manual times",
    "clips": "export local stereo listening pairs: reference left, target right",
    "fit": "fit only accepted anchors and export residual plot data",
    "render": "stream corrected WAV to the reference timeline and check actual audio residuals",
}

__all__ = ["SUBCOMMANDS", "__version__"]
