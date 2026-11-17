import sys

from mediawrench.cli import main

raise SystemExit(main(["cull", *sys.argv[1:]]))
