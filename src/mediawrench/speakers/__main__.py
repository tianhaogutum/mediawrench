import sys

from mediawrench.cli import main

raise SystemExit(main(["speakers", *sys.argv[1:]]))
