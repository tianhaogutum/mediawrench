import sys

from mediawrench.cli import main

raise SystemExit(main(["rollup", *sys.argv[1:]]))
