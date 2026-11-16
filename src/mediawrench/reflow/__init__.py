"""Offline, deterministic ASR subtitle reflow. Original implementation under MIT."""
from .model import Config, InputError
from .normalize import normalize
from .optimize import reflow
from .compare import compare_profiles

__all__ = ["Config", "InputError", "normalize", "reflow", "compare_profiles"]
__version__ = "0.1.0"
