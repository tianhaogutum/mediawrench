import sys

from mediawrench.cli import main

raise SystemExit(main(["cutsync", *sys.argv[1:]]))
