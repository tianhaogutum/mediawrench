import sys

from mediawrench.cli import main

raise SystemExit(main(["postflight", *sys.argv[1:]]))
