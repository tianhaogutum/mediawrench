import sys

from mediawrench.cli import main

raise SystemExit(main(["asr-audit", *sys.argv[1:]]))
