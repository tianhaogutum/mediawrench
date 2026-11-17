import sys

from mediawrench.cli import main

raise SystemExit(main(["drift", *sys.argv[1:]]))
