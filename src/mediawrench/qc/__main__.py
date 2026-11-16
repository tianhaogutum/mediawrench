import sys

from mediawrench.cli import main

raise SystemExit(main(["qc", *sys.argv[1:]]))
