import sys

from mediawrench.cli import main

raise SystemExit(main(["reflow", *sys.argv[1:]]))
