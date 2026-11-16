"""Pair inspection validates configurations without claiming cue feasibility."""
import contextlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.reflow.cli import main
from mediawrench.reflow.io import load_json

ROOT = Path(__file__).resolve().parents[2]


class InspectPairTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = [self.root / 'left.json', self.root / 'right.json']
        self.profiles = [dict(name='读者 é', max_cps=3, min_duration=7, min_gap=.12),
                         dict(name='reader', max_cps=12.5, min_duration=0, min_gap=0)]
        self.save()
        (self.root / 'existing.srt').write_bytes(b'previous SRT')
        (self.root / 'existing.json').write_bytes(b'previous report')

    def save(self):
        for path, profile in zip(self.paths, self.profiles):
            path.write_text(json.dumps(profile), encoding='utf-8')

    def run_cli(self, *args):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, '-m', 'mediawrench.reflow', *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def inspect(self, *extra):
        return self.run_cli('--inspect-pair', *self.paths, *extra)

    def assert_error(self, result, diagnostic='error:'):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn(diagnostic, result.stderr)

    def assert_side_error(self, result, side, path, *extra):
        self.assert_error(result)
        single = self.run_cli('--inspect-profile', path, *extra)
        self.assert_error(single)
        prefix = 'mediawrench reflow: error: '
        self.assertTrue(single.stderr.startswith(prefix))
        self.assertEqual(result.stderr, prefix + f'{side} profile: ' + single.stderr[len(prefix):])

    def expected(self, modes):
        return dict(format_version=1, tool='wordcue-reflow', mode='profile_pair_inspection',
                    profiles={side: dict(profile, cps_mode=mode,
                              rate_unit='graphemes/second' if mode == 'grapheme' else 'codepoints/second')
                              for side, profile, mode in zip(('left', 'right'), self.profiles, modes)})

    def test_invalid_bound_diagnostic_identifies_argument_side(self):
        for index, side in enumerate(('left', 'right')):
            with self.subTest(side=side):
                self.save()
                self.paths[index].write_text(json.dumps(dict(self.profiles[index], max_cps=0)))
                result = self.inspect()
                self.assert_error(result)
                self.assertEqual(result.stderr,
                                 f'mediawrench reflow: error: {side} profile: '
                                 'max_cps must be finite and positive\n')

    def test_pair_without_transcript_exact_schema_and_single_mode_compatibility(self):
        self.profiles[1]['cps_mode'] = 'grapheme'
        self.save()
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), self.expected(('codepoint', 'grapheme')))
        self.assertTrue(result.stdout.endswith('\n'))
        self.assertIn('读者 é', result.stdout)
        # Pair sides are exactly the existing single-profile inspection payloads.
        for side, path in zip(('left', 'right'), self.paths):
            single = self.run_cli('--inspect-prof', path)
            self.assertEqual(single.returncode, 0, single.stderr)
            self.assertEqual(json.loads(single.stdout)['profile'], json.loads(result.stdout)['profiles'][side])

    def test_independent_unit_precedence_and_conflicts_on_either_side(self):
        for left, right, explicit in itertools.product((None, 'codepoint', 'grapheme'), repeat=3):
            with self.subTest(left=left, right=right, explicit=explicit):
                for profile, saved in zip(self.profiles, (left, right)):
                    profile.pop('cps_mode', None)
                    if saved is not None:
                        profile['cps_mode'] = saved
                self.save()
                result = self.inspect(*([] if explicit is None else ['--cps-mode', explicit]))
                if explicit and any(saved and saved != explicit for saved in (left, right)):
                    index = next(i for i, saved in enumerate((left, right))
                                 if saved and saved != explicit)
                    self.assert_side_error(result, ('left', 'right')[index], self.paths[index],
                                           '--cps-mode', explicit)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, '')
                    modes = (left or explicit or 'codepoint', right or explicit or 'codepoint')
                    self.assertEqual(json.loads(result.stdout), self.expected(modes))

    def test_determinism_and_argument_order(self):
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, profile in zip(self.paths, self.profiles):
            path.write_text(json.dumps(dict(reversed(list(profile.items())))))
        self.assertEqual(self.inspect().stdout, result.stdout)
        reversed_pair = self.run_cli('--inspect-pair', *reversed(self.paths))
        self.assertEqual(reversed_pair.returncode, 0, reversed_pair.stderr)
        self.assertEqual(json.loads(reversed_pair.stdout)['profiles'],
                         dict(zip(('right', 'left'), json.loads(result.stdout)['profiles'].values())))

    def test_invalid_profile_fields_and_units_on_either_side(self):
        for index, path in enumerate(self.paths):
            valid = self.profiles[index]
            invalid = ['{bad', '[]', '{}', json.dumps(dict(valid, extra=1)),
                       json.dumps({k: v for k, v in valid.items() if k != 'min_gap'}),
                       json.dumps(dict(valid, name='')), json.dumps(dict(valid, name=None)),
                       json.dumps(dict(valid, max_cps=True)), json.dumps(dict(valid, max_cps=0)),
                       json.dumps(dict(valid, max_cps=float('nan'))),
                       json.dumps(dict(valid, min_duration=-1)), json.dumps(dict(valid, min_gap=.0001)),
                       '{"name":"one","name":"two","max_cps":3,"min_duration":0,"min_gap":0}',
                       '{"name":"one","max_cps":3,"min_duration":0,"min_gap":0,'
                       '"cps_mode":"codepoint","cps_mode":"grapheme"}']
            invalid += [json.dumps(dict(valid, cps_mode=mode))
                        for mode in (None, '', 'display', 'GRAPHEME', True, 1, [], {})]
            for raw in invalid:
                with self.subTest(side=index, raw=raw):
                    path.write_text(raw, encoding='utf-8')
                    self.assert_side_error(self.inspect(), ('left', 'right')[index], path)
            path.write_bytes(b'\xff')
            self.assert_side_error(self.inspect(), ('left', 'right')[index], path)
            self.save()
        self.assert_error(self.inspect('--cps-mode', 'display'))

    def test_distinct_names_not_distinct_bounds(self):
        self.profiles[1] = dict(self.profiles[0])
        self.save()
        result = self.inspect()
        self.assert_error(result, 'distinct names')
        self.assertEqual(result.stderr,
                         'mediawrench reflow: error: inspection profiles must have distinct names\n')
        self.assert_error(self.run_cli('--inspect-pair', self.paths[0], self.paths[0]), 'distinct names')
        self.profiles[1]['name'] = 'different'
        self.save()
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.expected(('codepoint', 'codepoint')))

    def test_bad_paths_and_pair_arity(self):
        for index in (0, 1):
            for path in ('', self.root / 'missing.json', self.root):
                paths = list(self.paths)
                paths[index] = path
                self.assert_side_error(self.run_cli('--inspect-pair', *paths),
                                       ('left', 'right')[index], path)
        for paths in ([], self.paths[:1], [*self.paths, self.paths[0]]):
            self.assert_error(self.run_cli('--inspect-pair', *paths))

    def test_first_failure_order_and_no_processing_on_errors(self):
        invalid_bound = json.dumps(dict(self.profiles[0], max_cps=0))
        conflict = json.dumps(dict(self.profiles[0], cps_mode='grapheme'))
        reason = 'max_cps must be finite and positive'
        cases = [(invalid_bound, '{bad', [], 'left', reason, 1),
                 (conflict, '{bad', ['--cps-mode', 'codepoint'], 'left',
                  'explicit cps_mode conflicts with saved profile cps_mode; '
                  'edit or copy the profile instead', 1),
                 # Identical names cannot mask an invalid right profile.
                 (json.dumps(self.profiles[0]), invalid_bound, [], 'right', reason, 2)]
        for left, right, extra, side, reason, reads in cases:
            with self.subTest(side=side, reason=reason), contextlib.ExitStack() as stack:
                self.paths[0].write_text(left)
                self.paths[1].write_text(right)
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                load = stack.enter_context(patch('mediawrench.reflow.cli.load_json', wraps=load_json))
                for name in ('reflow', 'compare_profiles', 'srt_text', 'atomic_write_many'):
                    stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                                              side_effect=AssertionError('inspection called ' + name)))
                stdout, stderr = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                self.assertEqual(main(['--inspect-pair', *map(str, self.paths), *extra]), 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(stderr.getvalue(), f'mediawrench reflow: error: {side} profile: {reason}\n')
                self.assertEqual([c.args[0] for c in load.call_args_list],
                                 list(map(str, self.paths[:reads])))
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        # The same unreadable argument in both positions is attributed to left.
        missing = self.root / 'missing.json'
        self.assert_side_error(self.run_cli('--inspect-pair', missing, missing), 'left', missing)

    def test_incompatible_options_including_explicit_defaults(self):
        cases = [[self.paths[0]], ['--inspect-profile', self.paths[0]],
                 ['--profile', self.paths[0]], ['--compare-profiles', *self.paths],
                 ['-o', self.root / 'existing.srt'], ['--output', self.paths[0]],
                 ['--report', self.root / 'existing.json'],
                 ['--report', self.root / 'absent' / 'report.json'],
                 ['--max-cps', '20'], ['--max-chars', '42'], ['--width-mode', 'codepoint'],
                 ['--target-duration', '3'], ['--max-duration', '6'], ['--pause', '.5'],
                 ['--algorithm', 'dp'], ['--group-words', '3'], ['--dry-run'], ['--strict']]
        for args in cases:
            with self.subTest(args=args):
                self.assert_error(self.inspect(*args), 'cannot be combined')

    def test_repeated_and_incompatible_options_reject_before_reads(self):
        pair = ['--inspect-pair', *map(str, self.paths)]
        cases = [pair + pair, pair + ['--inspect-pa', *map(str, self.paths)],
                 ['--cps-mode=codepoint', *pair, '--cps-m', 'grapheme'],
                 pair + ['--cps-mode', 'grapheme'] * 2,
                 pair + ['--inspect-profile', str(self.paths[0])],
                 ['--inspect-profile', str(self.paths[0]), *pair],
                 pair + ['--report', str(self.root / 'existing.json')],
                 pair + [str(self.paths[0])]]
        for args in cases:
            with self.subTest(args=args), contextlib.ExitStack() as stack:
                stack.enter_context(patch('mediawrench.reflow.cli.load_json',
                                          side_effect=AssertionError('ambiguous inspection read a file')))
                stdout, stderr = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                stack.enter_context(patch.object(sys, 'argv', ['mediawrench reflow', *args]))
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('error:', stderr.getvalue())

    def test_pair_never_runs_reflow_comparison_srt_or_writer(self):
        # min_duration=7 can exceed reflow's default max_duration; it is a valid
        # configuration bound, not a promise of transcript feasibility.
        with contextlib.ExitStack() as stack:
            for name in ('reflow', 'compare_profiles', 'srt_text', 'atomic_write_many'):
                stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                                          side_effect=AssertionError('inspection called ' + name)))
            stdout, stderr = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            self.assertEqual(main(['--inspect-pair', *map(str, self.paths)]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), self.expected(('codepoint', 'codepoint')))
        self.assertEqual(stderr.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
