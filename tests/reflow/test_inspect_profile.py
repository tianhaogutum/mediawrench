"""Standalone profile validation resolves rate units without processing a transcript."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.reflow.cli import main

ROOT = Path(__file__).resolve().parents[2]


class InspectProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / 'profile.json'
        self.bounds = dict(name='读者 é', max_cps=3, min_duration=1, min_gap=.12)
        self.profile.write_text(json.dumps(self.bounds), encoding='utf-8')
        (self.root / 'existing.srt').write_bytes(b'previous SRT')
        (self.root / 'existing.json').write_bytes(b'previous report')

    def run_cli(self, *args):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, '-m', 'mediawrench.reflow', *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def inspect(self, *args):
        return self.run_cli('--inspect-profile', self.profile, *args)

    def test_inspect_without_transcript_resolves_legacy_units(self):
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {
            'format_version': 1, 'tool': 'wordcue-reflow', 'mode': 'profile_inspection',
            'profile': dict(self.bounds, cps_mode='codepoint', rate_unit='codepoints/second')})
        self.assertTrue(result.stdout.endswith('\n'))
        self.assertIn('读者 é', result.stdout)

    def test_saved_and_explicit_units_are_deterministic_and_retain_bounds(self):
        for saved in (None, 'codepoint', 'grapheme'):
            data = dict(self.bounds)
            if saved is not None:
                data['cps_mode'] = saved
            self.profile.write_text(json.dumps(data), encoding='utf-8')
            for explicit in (None, 'codepoint', 'grapheme'):
                with self.subTest(saved=saved, explicit=explicit):
                    args = [] if explicit is None else ['--cps-mode', explicit]
                    result = self.inspect(*args)
                    if saved and explicit and saved != explicit:
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('conflict', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, '')
                        mode = saved or explicit or 'codepoint'
                        rate_unit = 'graphemes/second' if mode == 'grapheme' else 'codepoints/second'
                        self.assertEqual(json.loads(result.stdout)['profile'],
                                         dict(self.bounds, cps_mode=mode, rate_unit=rate_unit))
                        # JSON field order in the source cannot change inspection bytes.
                        self.profile.write_text(json.dumps(dict(reversed(list(data.items())))))
                        self.assertEqual(self.inspect(*args).stdout, result.stdout)

    def test_invalid_profiles_and_paths_fail_without_writes(self):
        invalid = ['{bad', '[]', '{}',
                   '{"name":"a","name":"b","max_cps":3,"min_duration":0,"min_gap":0}',
                   json.dumps(dict(self.bounds, unknown=1)),
                   json.dumps({k: v for k, v in self.bounds.items() if k != 'min_gap'}),
                   json.dumps(dict(self.bounds, max_cps=True)),
                   json.dumps(dict(self.bounds, min_duration=-1)),
                   json.dumps(dict(self.bounds, min_gap=.0001)),
                   json.dumps(dict(self.bounds, name='')),
                   json.dumps(dict(self.bounds, max_cps=float('nan')))]
        invalid += [json.dumps(dict(self.bounds, cps_mode=mode))
                    for mode in (None, '', 'display', 'GRAPHEME', True, 1, [], {})]
        invalid += ['{"name":"a","max_cps":3,"min_duration":0,"min_gap":0,'
                    '"cps_mode":"grapheme","cps_mode":"codepoint"}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.profile.write_text(raw, encoding='utf-8')
                result = self.inspect()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)
        self.profile.write_bytes(b'\xff')
        self.assertEqual(self.inspect().returncode, 2)
        for path in ('', self.root / 'missing.json', self.root):
            result = self.run_cli('--inspect-profile', path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
        result = self.inspect('--cps-mode', 'display')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_all_reflow_options_rejected_even_when_equal_to_defaults(self):
        cases = [[self.profile], ['--profile', self.profile],
                 ['--compare-profiles', self.profile, self.root / 'existing.json'],
                 ['-o', self.root / 'existing.srt'], ['--output', self.profile],
                 ['--report', self.root / 'existing.json'],
                 ['--report', self.root / 'absent' / 'report.json'],
                 ['--max-cps', '20'], ['--max-chars', '42'],
                 ['--width-mode', 'codepoint'], ['--target-duration', '3'],
                 ['--max-duration', '6'], ['--pause', '.5'],
                 ['--algorithm', 'dp'], ['--group-words', '3'],
                 ['--dry-run'], ['--strict']]
        for args in cases:
            with self.subTest(args=args):
                result = self.inspect(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('--inspect-profile', result.stderr)
                self.assertIn('cannot be combined', result.stderr)

    def test_inspection_never_runs_reflow_srt_or_writer(self):
        # Valid profile bounds may be infeasible for some transcripts; inspection
        # validates a profile, without claiming any transcript is feasible.
        self.profile.write_text(json.dumps(dict(self.bounds, min_duration=7)))
        with contextlib.ExitStack() as stack:
            for name in ('reflow', 'compare_profiles', 'srt_text', 'atomic_write_many'):
                stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                                          side_effect=AssertionError('inspection called ' + name)))
            stdout, stderr = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            self.assertEqual(main(['--inspect-profile', str(self.profile)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())['profile']['min_duration'], 7)
        self.assertEqual(stderr.getvalue(), '')

    def test_repeated_inspection_paths_rejected_in_every_spelling(self):
        other = self.root / 'other.json'
        other.write_text(json.dumps(dict(self.bounds, name='other')))
        invalid = self.root / 'invalid.json'
        invalid.write_text('{bad')
        for first in (self.profile, other, invalid, self.root / 'missing.json'):
            for option in ('--inspect-profile', '--inspect-prof'):
                with self.subTest(first=first, option=option):
                    result = self.run_cli(option, first, f'--inspect-profile={self.profile}')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('--inspect-profile must be supplied exactly once', result.stderr)

    def test_repeated_rate_modes_rejected_even_when_final_mode_matches_profile(self):
        self.profile.write_text(json.dumps(dict(self.bounds, cps_mode='grapheme')))
        for first in ('codepoint', 'grapheme'):
            for option in ('--cps-mode', '--cps-m'):
                with self.subTest(first=first, option=option):
                    # Occurrences on either side of the inspection selector count.
                    result = self.run_cli(option, first, '--inspect-profile', self.profile,
                                          '--cps-mode=grapheme')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('--cps-mode may be supplied at most once with --inspect-profile',
                                  result.stderr)

    def test_repeated_options_fail_before_profile_reading(self):
        for args in (['--inspect-profile', str(self.profile)] * 2,
                     ['--inspect-profile', str(self.profile), '--cps-mode', 'codepoint',
                      '--cps-mode', 'grapheme']):
            with self.subTest(args=args), contextlib.ExitStack() as stack:
                stack.enter_context(patch('mediawrench.reflow.cli.load_json',
                                          side_effect=AssertionError('ambiguous inspection read a file')))
                stdout, stderr = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                # Exercise the installed entry-point convention main(argv=None).
                stack.enter_context(patch.object(sys, 'argv', ['mediawrench reflow', *args]))
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('error:', stderr.getvalue())

    def test_reflow_retains_last_rate_mode_resolution(self):
        for first, last in (('codepoint', 'grapheme'), ('grapheme', 'codepoint')):
            with self.subTest(first=first, last=last):
                result = self.run_cli(ROOT / 'examples/reflow/pause.json', '--dry-run',
                                      '--cps-mode', first, f'--cps-mode={last}')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['config']['cps_mode'], last)

    def test_transcript_still_required_for_reflow_and_help_documents_inspection(self):
        for args in ([], ['--dry-run'], ['--compare-profiles', self.profile, self.root / 'existing.json', '--dry-run']):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('input', result.stderr)
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--inspect-profile', result.stdout)
        self.assertIn('transcript', result.stdout)


if __name__ == '__main__':
    unittest.main()
