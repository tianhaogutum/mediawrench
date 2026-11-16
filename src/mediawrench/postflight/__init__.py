"""Read-only, local-media postflight checks. No replacement operation exists."""
from .core import Options, compare, exit_code, write_report

__all__ = ["Options", "compare", "exit_code", "write_report"]
__version__ = "0.1.0"
