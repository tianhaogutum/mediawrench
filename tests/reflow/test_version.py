"""Version output and argparse early-exit contract; no transcript is required."""
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.reflow import __version__
from mediawrench.reflow.cli import main

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = f'mediawrench reflow {__version__}\n'


class VersionTests(unittest.TestCase):
    def test_module_version_without_transcript(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'mediawrench.reflow', '--version'],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, EXPECTED)
        self.assertEqual(result.stderr, '')

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                main(args)
        return error.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_version_matches_packaging_source(self):
        project = (ROOT / 'pyproject.toml').read_text().split('[project]\n', 1)[1].split('\n[', 1)[0]
        version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE).group(1)
        self.assertEqual(__version__, version)
        self.assertEqual(self.invoke(['--version']), (0, f'mediawrench reflow {version}\n', ''))

    def test_version_exits_before_application_work_and_preserves_paths(self):
        temporary_root = ROOT / '.tools' / 'test-tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            root = Path(directory)
            transcript, profile, output, report = [root / p for p in
                ('input.json', 'profile.json', 'output.srt', 'report.json')]
            transcript.write_bytes(b'{invalid transcript')
            profile.write_bytes(b'{invalid profile')
            output.write_bytes(b'previous SRT')
            report.write_bytes(b'previous report')
            (root / 'nested').mkdir()
            (root / 'nested' / 'keep').write_bytes(b'keep')
            (root / 'symlink').symlink_to(transcript)
            os.link(transcript, root / 'hardlink')

            def snapshot():
                return {str(p.relative_to(root)):
                        ('symlink', os.readlink(p)) if p.is_symlink() else
                        ('directory', p.stat().st_ino) if p.is_dir() else
                        ('file', p.stat().st_ino, p.read_bytes())
                        for p in root.rglob('*')}

            before = snapshot()
            paths = (transcript, profile, root / 'symlink', root / 'hardlink',
                     root / 'nested', root / 'absent' / 'missing.json', '', '\0')
            cases = [['--version'], ['--vers'], ['--v'], ['--version', '--version'],
                     ['--version', '--max-chars', 'bad'], ['--version', '--inspect-pair'],
                     ['--unknown', '--version'], ['extra', 'positional', '--version']]
            for path in paths:
                cases.extend([
                    [str(path), '--profile', str(path), '-o', str(output),
                     '--report', str(report), '--strict', '--version'],
                    ['--inspect-profile', str(path), '--inspect-pair', str(path), str(path), '--version'],
                    ['--inspect-pair', str(path), str(path), '--cps-mode', 'codepoint',
                     '--cps-mode', 'grapheme', '--strict', '--version'],
                    [str(path), '--compare-profiles', str(path), str(path),
                     '--compare-profiles', str(path), str(path), '--profile', str(path),
                     '--profile', str(path), '--report', str(report), '--report', str(path),
                     '-o', str(output), '-o', str(path), '--dry-run', '--version']])
            for args in cases:
                with self.subTest(args=args):
                    with contextlib.ExitStack() as stack:
                        for name in ('validate_paths', 'load_json', 'Config.from_profile', 'Config',
                                     'reflow', 'compare_profiles', 'srt_text', 'json_text', 'atomic_write_many'):
                            stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                                side_effect=AssertionError('version reached ' + name)))
                        for name in ('builtins.open', 'pathlib.Path.open', 'pathlib.Path.stat',
                                     'pathlib.Path.resolve', 'pathlib.Path.mkdir', 'os.replace',
                                     'socket.socket', 'importlib.metadata.version'):
                            stack.enter_context(patch(name,
                                side_effect=AssertionError('version reached ' + name)))
                        self.assertEqual(self.invoke(args), (0, EXPECTED, ''))
                    self.assertEqual(snapshot(), before)

    def test_version_keeps_argparse_action_and_error_order(self):
        for args, code, diagnostic in (
            (['--help', '--version'], 0, 'usage:'),
            (['--version', '--help'], 0, EXPECTED),
            (['--max-chars', 'bad', '--version'], 2, 'invalid int value'),
            (['--width-mode', 'bad', '--version'], 2, 'invalid choice'),
            (['--inspect-pair', '--version'], 2, 'expected 2 arguments'),
            (['--profile', '--version'], 2, 'expected one argument'),
            (['--inspect', 'left', '--version'], 2, 'ambiguous option'),
            (['--version=other'], 2, 'ignored explicit argument'),
            (['--', '--version'], 2, '--output and --report are required'),
            ([], 2, 'input transcript is required'),
        ):
            with self.subTest(args=args):
                actual, stdout, stderr = self.invoke(args)
                self.assertEqual(actual, code)
                self.assertIn(diagnostic, stderr if code else stdout)
                self.assertEqual(stdout if code else stderr, '')
