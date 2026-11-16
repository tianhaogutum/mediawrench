import sys

from mediawrench.cli import main

raise SystemExit(main(["cutlist", *sys.argv[1:]]))
