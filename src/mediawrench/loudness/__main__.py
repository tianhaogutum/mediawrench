import sys

from mediawrench.cli import main

raise SystemExit(main(["loudness", *sys.argv[1:]]))
