import sys

from mediawrench.cli import main

raise SystemExit(main(["chunks", *sys.argv[1:]]))
