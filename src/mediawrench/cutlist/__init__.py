"""Offline transcript selection and LosslessCut format interoperability."""

from .companions import verify_companions
from .core import Cue, Cut, FrameCut, CutlistError, Timeline, build_timeline, load_plan, read_srt
from .export import output_paths, render_outputs, write_outputs
from .probe import build_probed_timeline, verify_probe_evidence
from .frames import quantize_timeline

__all__ = [
    "verify_companions", "Cue", "Cut", "FrameCut", "quantize_timeline", "CutlistError", "Timeline", "build_timeline", "load_plan",
    "read_srt", "output_paths", "render_outputs", "write_outputs", "build_probed_timeline", "verify_probe_evidence",
]
__version__ = "0.1.0"
