"""``mediawrench cull``: review three-way XMP culling conflicts (stars, colour labels,
pick flags) while retaining every other property from one chosen sidecar.

This module deliberately imports nothing heavy: lxml and simplejson are only loaded by
``cli`` and the modules it uses, so the dispatcher can show ``SUBCOMMANDS`` in its help
summary when the optional ``cull`` extra is not installed.
"""

__version__ = "0.1.0"

# One-line help per subcommand, shared with the argparse parser in ``cli``.
SUBCOMMANDS = {
    "plan": "inspect differences; print a reviewable JSON decision plan",
    "merge": "write a new sidecar, audit, and three original copies",
    "verify": "check a saved bundle without the original input paths",
}

__all__ = ["SUBCOMMANDS", "__version__"]
