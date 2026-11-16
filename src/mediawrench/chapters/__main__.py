import sys

from mediawrench.cli import main

raise SystemExit(main(["chapters", *sys.argv[1:]]))
