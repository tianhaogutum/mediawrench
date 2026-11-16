"""The documented --inspect-pair JSON must be byte-identical to the CLI output."""
import contextlib
import io
from pathlib import Path
import unittest

from mediawrench.reflow.cli import main

ROOT = Path(__file__).resolve().parents[2]


class ReadmeExampleTests(unittest.TestCase):
    def test_inspect_pair_example_matches_actual_output(self):
        lines = (ROOT / 'docs' / 'reflow.md').read_text(encoding='utf-8').split('\n')
        start = next(i for i, line in enumerate(lines) if line.startswith('mediawrench reflow --inspect-pair'))
        begin = next(i for i in range(start, len(lines)) if lines[i] == '```json')
        end = next(i for i in range(begin + 1, len(lines)) if lines[i] == '```')
        documented = '\n'.join(lines[begin + 1:end]) + '\n'
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(['--inspect-pair',
                           str(ROOT / 'examples' / 'reflow' / 'profiles' / 'rate-codepoint.json'),
                           str(ROOT / 'examples' / 'reflow' / 'profiles' / 'rate-grapheme.json')])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), documented)
