import sys

from mediawrench.cli import main

raise SystemExit(main(["audacity", *sys.argv[1:]]))
