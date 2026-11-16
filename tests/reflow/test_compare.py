"""Source-keyed comparison contracts, including real CLI no-write failures."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.reflow import Config, InputError, reflow
from mediawrench.reflow.io import load_json
from mediawrench.reflow.cli import main

ROOT = Path(__file__).resolve().parents[2]


def profile(name, max_cps=20, min_duration=0, min_gap=0):
    return dict(name=name, max_cps=max_cps, min_duration=min_duration, min_gap=min_gap)


def compare(data, left, right, **options):
    from mediawrench.reflow import compare_profiles
    return compare_profiles(data, left, right, **options)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.data = load_json(ROOT / 'examples/reflow/reading-profile.json')
        self.left = load_json(ROOT / 'examples/reflow/profiles/brief.json')
        self.right = load_json(ROOT / 'examples/reflow/profiles/patient.json')

    def test_exact_source_boundary_difference_and_repeat_determinism(self):
        original = copy.deepcopy((self.data, self.left, self.right))
        result = compare(self.data, self.left, self.right, target_duration=.5)
        self.assertEqual(result, compare(self.data, self.left, self.right, target_duration=.5))
        self.assertEqual(original, (self.data, self.left, self.right))
        self.assertEqual((result['format_version'], result['mode']), (1, 'profile_comparison'))
        self.assertEqual(result['boundary_differences'], [{
            'after_unit_id': '/words/1', 'before_unit_id': '/words/2',
            'left': dict(cue_id=1, next_cue_id=2, end_ms=800, next_start_ms=1000, gap_ms=200),
            'right': None}])
        self.assertEqual(result['constraint_differences'], [])
        self.assertEqual([c['text'] for c in result['profiles']['left']['cues']], ['One. Two.', 'Three.'])
        self.assertEqual([c['text'] for c in result['profiles']['right']['cues']], ['One. Two. Three.'])
        for side, p in (('left', self.left), ('right', self.right)):
            expected = reflow(self.data, Config.from_profile(p, target_duration=.5))
            actual = result['profiles'][side]
            self.assertEqual(actual['name'], p['name'])
            for key in ('config', 'feasible', 'objective', 'metrics', 'constraints'):
                self.assertEqual(actual[key], expected[key])
            self.assertTrue(actual['srt_roundtrip']['verified'])
            for cue, full in zip(actual['cues'], expected['cues']):
                for key in cue:
                    self.assertEqual(cue[key], full[key])
        self.assertEqual(result['source_units'], expected['source_units'])
        reverse = compare(self.data, self.right, self.left, target_duration=.5)
        delta, = reverse['boundary_differences']
        self.assertIsNone(delta['left'])
        self.assertEqual(delta['right'], result['boundary_differences'][0]['left'])

    def test_same_bounds_distinct_names_has_no_differences(self):
        result = compare(self.data, self.left, dict(self.left, name='equivalent'), target_duration=.5)
        self.assertEqual(result['boundary_differences'], [])
        self.assertEqual(result['constraint_differences'], [])

    def test_conflicts_are_source_linked_with_no_boundary_changes(self):
        data = load_json(ROOT / 'examples/reflow/profile-conflict.json')
        result = compare(data, self.left, self.right)
        self.assertEqual(result['boundary_differences'], [])
        self.assertTrue(result['profiles']['left']['feasible'])
        self.assertFalse(result['profiles']['right']['feasible'])
        changes = result['constraint_differences']
        self.assertEqual([(v['unit_ids'], v['code']) for v in changes], [
            (['/segments/0/words/0'], 'minimum_duration'),
            (['/segments/0/words/0'], 'minimum_gap'),
            (['/segments/1/words/0'], 'minimum_duration')])
        self.assertEqual([v['right']['shortfall_ms'] for v in changes], [600, 120, 600])
        self.assertTrue(all(v['status'] == 'right_only' and v['left'] is None for v in changes))
        self.assertEqual(changes[1]['next_unit_id'], '/segments/1/words/0')

    def test_changed_shortfall_is_one_change_and_equal_violation_is_omitted(self):
        data = {'segments': [{'text': 'A.', 'start': 0, 'end': .4}]}
        result = compare(data, profile('one', min_duration=1), profile('two', min_duration=2))
        change, = result['constraint_differences']
        self.assertEqual((change['code'], change['unit_ids'], change['status']),
                         ('minimum_duration', ['/segments/0/text'], 'changed'))
        self.assertEqual((change['left']['shortfall_ms'], change['right']['shortfall_ms']), (600, 1600))
        result = compare(data, profile('one', min_duration=1), profile('two', min_duration=1))
        self.assertEqual(result['constraint_differences'], [])
        self.assertFalse(result['profiles']['left']['feasible'])

    def test_renumbering_and_duplicate_upstream_ids_do_not_create_false_differences(self):
        data = {'segments': [
            {'id': 'same', 'words': [dict(w, id='same') for w in self.data['words']]},
            {'id': 'same', 'text': 'x' * 90, 'start': 3, 'end': 4}]}
        result = compare(data, self.left, self.right, target_duration=.5)
        left = result['profiles']['left']['constraints']
        right = result['profiles']['right']['constraints']
        self.assertEqual([v['cue_id'] for v in left if v['code'] == 'line_width'], [3])
        self.assertEqual([v['cue_id'] for v in right if v['code'] == 'line_width'], [2])
        self.assertNotIn('line_width', [v['code'] for v in result['constraint_differences']])
        self.assertEqual(len(result['boundary_differences']), 1)
        self.assertEqual(result['boundary_differences'][0]['after_unit_id'], '/segments/0/words/1')

    def test_grapheme_fallback_repairs_and_shared_rate_algorithm_options(self):
        data = {'segments': [
            {'text': 'e\u0301', 'words': [dict(word='e', start=0, end=.5),
                                       dict(word='\u0301', start=.4, end=1)]},
            {'text': '👩🏽‍💻', 'start': 2, 'end': 3}]}
        for algorithm in ('dp', 'fixed'):
            result = compare(data, profile('one', max_cps=1), profile('two', max_cps=3),
                             algorithm=algorithm, group_words=1, cps_mode='grapheme', width_mode='display')
            self.assertEqual(result['boundary_differences'], [])
            self.assertEqual(result['constraint_differences'], [])
            for side in ('left', 'right'):
                self.assertTrue(result['profiles'][side]['feasible'])
                self.assertEqual([c['speaking_rate']['count'] for c in result['profiles'][side]['cues']], [1, 1])
                self.assertEqual(result['profiles'][side]['cues'][0]['unit_ids'],
                                 ['/segments/0/words/0', '/segments/0/words/1'])
            self.assertEqual({d['code'] for d in result['diagnostics']},
                             {'timing_repair', 'segment_fallback', 'grapheme_boundary_join'})
            self.assertTrue(result['source_units'][-1]['fallback'])

    def test_both_library_profiles_validate_before_processing_with_existing_reasons(self):
        for left, right, reason in ((dict(self.left, max_cps=0), self.right,
                                     'max_cps must be finite and positive'),
                                    (self.left, dict(self.right, min_gap=-1),
                                     'min_gap must be zero (disabled) or between .001 and 3600 seconds'),
                                    (self.left, self.left, 'comparison profiles must have distinct names')):
            with self.subTest(reason=reason), contextlib.ExitStack() as stack:
                for name in ('reflow', 'srt_text'):
                    stack.enter_context(patch('mediawrench.reflow.compare.' + name,
                                              side_effect=AssertionError('invalid profiles reached ' + name)))
                before = copy.deepcopy((self.data, left, right))
                with self.assertRaises(InputError) as error:
                    compare(self.data, left, right)
                self.assertEqual(str(error.exception), reason)
                self.assertEqual((self.data, left, right), before)

    def test_invalid_profiles_options_and_input_raise_input_error(self):
        cases = [(self.left, self.left, {}), (dict(self.left, name=''), self.right, {}),
                 (dict(self.left, extra=2), self.right, {}), (self.left, None, {}),
                 (self.left, self.right, {'max_cps': 40}),
                 (self.left, self.right, {'cps_mode': 'display'})]
        for left, right, options in cases:
            with self.subTest(left=left, right=right, options=options):
                with self.assertRaises(InputError):
                    compare(self.data, left, right, **options)
        with self.assertRaises(InputError):
            compare({'words': []}, self.left, self.right)


class ComparisonCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.input = self.root / 'input.json'
        self.input.write_bytes((ROOT / 'examples/reflow/reading-profile.json').read_bytes())
        self.left = self.root / 'left.json'
        self.right = self.root / 'right.json'
        self.left.write_bytes((ROOT / 'examples/reflow/profiles/brief.json').read_bytes())
        self.right.write_bytes((ROOT / 'examples/reflow/profiles/patient.json').read_bytes())
        self.report = self.root / 'comparison.json'

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'mediawrench.reflow', str(self.input), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def args(self):
        return ['--compare-profiles', self.left, self.right, '--target-duration', '.5']

    def test_repeated_identical_comparison_selectors_rejected(self):
        result = self.run_cli(*self.args(), *self.args(), '--dry-run')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr.endswith(
            'error: --compare-profiles must be supplied exactly once\n'), result.stderr)

    def test_repeated_comparison_pairs_preserve_files_and_directories(self):
        invalid = self.root / 'invalid.json'
        invalid.write_bytes(b'{bad')
        directory = self.root / 'directory'
        directory.mkdir()
        (directory / 'keep').write_bytes(b'keep nested file')
        self.report.write_bytes(b'previous report')
        (self.root / 'previous.srt').write_bytes(b'previous SRT')

        def snapshot():
            return {str(p.relative_to(self.root)): None if p.is_dir() else p.read_bytes()
                    for p in self.root.rglob('*')}

        before = snapshot()
        for first in ((self.left, self.right), (self.right, self.left),
                      (self.root / 'missing.json', self.right), (invalid, self.right),
                      (directory, self.right)):
            for flag in ('--compare-profiles', '--compare-prof', '--comp'):
                for extra in (['--report', self.report],
                              ['--dry-run', '--report', self.root / 'absent' / 'no.json']):
                    with self.subTest(first=first, flag=flag, extra=extra):
                        result = self.run_cli(flag, *first, '--compare-profiles',
                                              self.left, self.right, *extra)
                        self.assert_no_write_error(result)
                        self.assertTrue(result.stderr.endswith(
                            'error: --compare-profiles must be supplied exactly once\n'), result.stderr)
                        self.assertEqual(snapshot(), before)
        # A third occurrence cannot restore validity; invalid later pairs also fail.
        for tail in (['--comp', self.left, self.right] * 2,
                     ['--comp', self.root / 'missing.json', invalid]):
            result = self.run_cli(*self.args(), *tail, '--report', self.report)
            self.assert_no_write_error(result)
            self.assertIn('--compare-profiles must be supplied exactly once', result.stderr)
            self.assertEqual(snapshot(), before)

    def test_repeated_comparison_option_order_and_no_reads_or_processing(self):
        pair = ['--compare-profiles', str(self.left), str(self.right)]
        repeated = pair + ['--comp', str(self.left), str(self.right)]
        repetition = '--compare-profiles must be supplied exactly once'
        cases = [(repeated, repetition),  # Before missing transcript/report.
                 ([str(self.input), *repeated], repetition),
                 ([str(self.input), *repeated, '--output', str(self.report),
                   '--profile', str(self.left), '--max-cps', '12'], repetition),
                 ([str(self.input), *repeated, '--report', str(self.left),
                   '--max-chars', '0'], repetition),  # Before aliases/layout.
                 (repeated + ['--max-chars', 'bad'], 'argument --max-chars: invalid int value'),
                 (repeated + ['--cps-mode', 'display'], 'argument --cps-mode: invalid choice'),
                 (repeated + ['--compare-profiles', str(self.left)], 'expected 2 arguments'),
                 (repeated + ['--unknown'], 'unrecognized arguments: --unknown'),
                 (repeated + ['--inspect-profile', str(self.left)],
                  '--inspect-profile cannot be combined'),
                 (repeated + ['--inspect-pair', str(self.left), str(self.right)],
                  '--inspect-pair cannot be combined'),
                 (repeated + ['--inspect-profile', str(self.left)] * 2,
                  '--inspect-profile must be supplied exactly once')]
        for args, diagnostic in cases:
            with self.subTest(args=args), contextlib.ExitStack() as stack:
                for name in ('load_json', 'validate_paths', 'compare_profiles', 'reflow',
                             'srt_text', 'atomic_write_many', 'Config', 'Config.from_profile'):
                    stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                        side_effect=AssertionError('option error reached ' + name)))
                stdout, stderr = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                stack.enter_context(patch.object(sys, 'argv', ['mediawrench reflow', *args]))
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn(diagnostic, stderr.getvalue())

    def test_single_comparison_abbreviation_and_last_cps_mode_are_preserved(self):
        for first, last in (('codepoint', 'grapheme'), ('grapheme', 'codepoint')):
            with self.subTest(last=last):
                single = self.run_cli(*self.args(), '--cps-mode', last, '--dry-run')
                repeated = self.run_cli('--comp', self.left, self.right, '--cps-mode', first,
                                        '--target-duration', '.5', f'--cps-m={last}', '--dry-run')
                self.assertEqual(single.returncode, 0, single.stderr)
                self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr),
                                 (single.returncode, single.stdout, single.stderr))
                self.assertTrue(all(p['config']['cps_mode'] == last
                                    for p in json.loads(repeated.stdout)['profiles'].values()))

    def test_invalid_bound_diagnostic_identifies_argument_side(self):
        for side, path in (('left', self.left), ('right', self.right)):
            with self.subTest(side=side):
                before = path.read_bytes()
                path.write_text(json.dumps(dict(load_json(path), max_cps=0)))
                self.report.write_bytes(b'previous report')
                snapshot = {p.name: p.read_bytes() for p in self.root.iterdir()}
                result = self.run_cli(*self.args(), '--report', self.report)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, snapshot)
                path.write_bytes(before)
                self.assertEqual(result.stderr,
                                 f'mediawrench reflow: error: {side} profile: '
                                 'max_cps must be finite and positive\n')

    def assert_no_write_error(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)

    def test_profile_read_fields_and_conflicts_retain_reason_on_either_side(self):
        self.report.write_bytes(b'previous report')
        (self.root / 'previous.srt').write_bytes(b'previous SRT')
        for side, path in (('left', self.left), ('right', self.right)):
            valid_bytes = path.read_bytes()
            valid = json.loads(valid_bytes)
            invalid = [b'{bad', b'\xff', b'[]', b'{}', b'{"name":"a","name":"b"}',
                       json.dumps(dict(valid, extra=1)).encode(),
                       json.dumps({k: v for k, v in valid.items() if k != 'min_gap'}).encode()]
            for fields in ({'name': ''}, {'name': None}, {'max_cps': True},
                           {'min_duration': -1}, {'min_gap': .0001}, {'cps_mode': 'display'}):
                invalid.append(json.dumps(dict(valid, **fields)).encode())
            for raw, extra in [(raw, []) for raw in invalid] + [
                    (json.dumps(dict(valid, cps_mode='grapheme')).encode(), ['--cps-mode', 'codepoint'])]:
                with self.subTest(side=side, raw=raw, extra=extra):
                    path.write_bytes(raw)
                    snapshot = {p.name: p.read_bytes() for p in self.root.iterdir()}
                    single = subprocess.run([sys.executable, '-m', 'mediawrench.reflow',
                        '--inspect-profile', str(path), *extra], cwd=ROOT,
                        capture_output=True, text=True, timeout=120)
                    self.assert_no_write_error(single)
                    for dry in ([], ['--dry-run']):
                        result = self.run_cli(*self.args(), '--report', self.report, *extra, *dry)
                        self.assert_no_write_error(result)
                        prefix = 'mediawrench reflow: error: '
                        self.assertEqual(result.stderr, prefix + side + ' profile: '
                                         + single.stderr.removeprefix(prefix))
                        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, snapshot)
            path.write_bytes(valid_bytes)

    def test_missing_and_directory_profiles_identify_argument_side(self):
        for side, path in (('left', self.left), ('right', self.right)):
            valid = path.read_bytes()
            path.unlink()
            try:
                for directory in (False, True):
                    with self.subTest(side=side, directory=directory):
                        if directory:
                            path.mkdir()
                        self.report.write_bytes(b'previous report')
                        snapshot = {p.name: None if p.is_dir() else p.read_bytes()
                                    for p in self.root.iterdir()}
                        result = self.run_cli(*self.args(), '--report', self.report)
                        self.assert_no_write_error(result)
                        self.assertTrue(result.stderr.startswith(f'mediawrench reflow: error: {side} profile: '))
                        self.assertIn(str(path), result.stderr)
                        self.assertEqual({p.name: None if p.is_dir() else p.read_bytes()
                                          for p in self.root.iterdir()}, snapshot)
            finally:
                if path.is_dir():
                    path.rmdir()
                path.write_bytes(valid)

    def bound_error(self, args, expected, reads):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in ('compare_profiles', 'reflow', 'srt_text', 'atomic_write_many'):
                stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                    side_effect=AssertionError('invalid profile reached ' + name)))
            loader = stack.enter_context(patch('mediawrench.reflow.cli.load_json', wraps=load_json))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            result = main(list(map(str, [self.input, *args])))
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), 'mediawrench reflow: error: ' + expected + '\n')
        self.assertEqual([call.args[0] for call in loader.call_args_list], list(map(str, reads)))

    def test_left_fully_validates_before_right_and_transcript_reads(self):
        self.input.write_text('{bad transcript')
        self.left.write_text(json.dumps(profile('one', max_cps=0)))
        self.right.unlink()
        self.bound_error([*self.args(), '--dry-run'],
            'left profile: max_cps must be finite and positive', [self.left])
        self.left.write_text(json.dumps(profile('one')))
        self.right.write_text(json.dumps(profile('two', max_cps=0)))
        self.bound_error([*self.args(), '--dry-run'],
            'right profile: max_cps must be finite and positive', [self.left, self.right])
        # Saved-unit resolution is part of left validation, before right reads.
        self.left.write_text(json.dumps(dict(profile('one'), cps_mode='grapheme')))
        self.bound_error([*self.args(), '--dry-run', '--cps-mode', 'codepoint'],
            'left profile: explicit cps_mode conflicts with saved profile cps_mode; '
            'edit or copy the profile instead', [self.left])
        # Shared layout validation is not a profile failure and precedes reads.
        self.bound_error([*self.args(), '--dry-run', '--max-chars', '0'],
            'max_chars must be an integer between 1 and 500', [])
        self.bound_error([*self.args(), '--dry-run', '--target-duration', '7'],
            'target_duration cannot exceed max_duration', [])

    def test_nonprofile_errors_have_no_side_label_and_preserve_files(self):
        self.report.write_bytes(b'previous report')
        for raw in (b'{bad', b'{}', b'[]'):
            with self.subTest(transcript=raw):
                self.input.write_bytes(raw)
                snapshot = {p.name: p.read_bytes() for p in self.root.iterdir()}
                result = self.run_cli(*self.args(), '--report', self.report)
                self.assert_no_write_error(result)
                self.assertNotIn('left profile:', result.stderr)
                self.assertNotIn('right profile:', result.stderr)
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, snapshot)
        self.input.write_bytes((ROOT / 'examples/reflow/reading-profile.json').read_bytes())
        self.right.write_text(json.dumps(dict(load_json(self.right), name='brief')))
        duplicate = self.run_cli(*self.args(), '--report', self.report)
        self.assert_no_write_error(duplicate)
        self.assertEqual(duplicate.stderr, 'mediawrench reflow: error: comparison profiles must have distinct names\n')
        for extra, reason in ((['--output', self.root / 'no.srt'], 'cannot be combined'),
                              (['--max-chars', '0'], 'max_chars must'),
                              (['--algorithm', 'fixed', '--group-words', '0'], 'distinct names')):
            result = self.run_cli(*self.args(), '--report', self.report, *extra)
            self.assert_no_write_error(result)
            self.assertIn(reason, result.stderr)
            self.assertNotIn('left profile:', result.stderr)
            self.assertNotIn('right profile:', result.stderr)
        self.right.write_text(json.dumps(profile('two')))
        result = self.run_cli(*self.args(), '--report', self.report,
                              '--algorithm', 'fixed', '--group-words', '0')
        self.assert_no_write_error(result)
        self.assertEqual(result.stderr, 'mediawrench reflow: error: group_words must be a positive integer\n')
        self.input.unlink()
        result = self.run_cli(*self.args(), '--report', self.report)
        self.assert_no_write_error(result)
        self.assertNotIn('profile:', result.stderr)
        self.assertIn(str(self.input), result.stderr)
        # Alias rejection precedes invalid profile content and transcript reads.
        self.left.write_text('{bad')
        self.bound_error([*self.args(), '--report', self.left, '--dry-run'],
            'input, profiles, SRT and report must be distinct files (including symlinks/hardlinks)', [])
        self.assertEqual(self.report.read_bytes(), b'previous report')

    def test_comparison_file_dry_run_and_determinism(self):
        first = self.run_cli(*self.args(), '--report', self.report)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, '')
        raw = self.report.read_bytes()
        result = json.loads(raw)
        self.assertEqual(result['boundary_differences'][0]['after_unit_id'], '/words/1')
        self.assertEqual(result['profiles']['left']['name'], 'brief')
        dry = self.run_cli(*self.args(), '--dry-run', '--report', self.root / 'absent' / 'no.json')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(dry.stdout.encode(), raw)
        self.assertFalse((self.root / 'absent').exists())
        self.assertEqual(self.report.read_bytes(), raw)
        again = self.run_cli(*self.args(), '--report', self.report)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.report.read_bytes(), raw)
        self.assertEqual(set(p.name for p in self.root.iterdir()), {'input.json', 'left.json', 'right.json', 'comparison.json'})

    def test_strict_either_infeasible_emits_complete_report(self):
        self.input.write_bytes((ROOT / 'examples/reflow/profile-conflict.json').read_bytes())
        for left, right in ((self.left, self.right), (self.right, self.left)):
            result = self.run_cli('--compare-profiles', left, right, '--strict', '--report', self.report)
            self.assertEqual(result.returncode, 3, result.stderr)
            report = json.loads(self.report.read_text())
            self.assertEqual(len(report['constraint_differences']), 3)
            self.assertEqual(sum(v['feasible'] for v in report['profiles'].values()), 1)
        result = self.run_cli(*self.args(), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['constraint_differences']), 3)

    def test_ambiguous_options_and_missing_arguments_preserve_outputs(self):
        self.report.write_text('prior report')
        srt = self.root / 'prior.srt'
        srt.write_text('prior SRT')
        for extra in (['--output', srt], ['--profile', self.left], ['--max-cps', '12']):
            result = self.run_cli(*self.args(), '--report', self.report, *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(self.report.read_text(), 'prior report')
            self.assertEqual(srt.read_text(), 'prior SRT')
        for args in (self.args(), ['--compare-profiles', self.left, '--dry-run']):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2)

    def test_invalid_input_or_either_profile_preserves_existing_report(self):
        self.report.write_text('prior report')
        for path in (self.input, self.left, self.right):
            before = path.read_bytes()
            for raw in ('{}', '{bad', '{"name":"a","name":"b"}', '[]'):
                path.write_text(raw)
                result = self.run_cli(*self.args(), '--report', self.report)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.report.read_text(), 'prior report')
            path.write_bytes(before)
        self.right.write_text(json.dumps(dict(load_json(self.right), name='brief')))
        result = self.run_cli(*self.args(), '--report', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertIn('distinct', result.stderr)
        self.assertEqual(self.report.read_text(), 'prior report')
        self.right.unlink()
        result = self.run_cli(*self.args(), '--report', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.report.read_text(), 'prior report')

    def test_source_and_profile_output_aliases_rejected_even_dry_run(self):
        for path in (self.input, self.left, self.right):
            before = path.read_bytes()
            for kind in ('direct', 'symlink', 'hardlink'):
                target = self.report
                target.unlink(missing_ok=True)
                if kind == 'direct':
                    target = path
                elif kind == 'symlink':
                    target.symlink_to(path)
                else:
                    os.link(path, target)
                for dry in ([], ['--dry-run']):
                    result = self.run_cli(*self.args(), '--report', target, *dry)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(path.read_bytes(), before)
        self.report.unlink(missing_ok=True)
        result = self.run_cli('--compare-profiles', self.left, self.left, '--dry-run')
        self.assertEqual(result.returncode, 2)

    def test_unsafe_srt_and_unwritable_destination_preserve_report(self):
        self.report.write_text('prior report')
        self.input.write_text(json.dumps({'segments': [
            {'text': '00:00:00,000 --> 00:00:01,000', 'start': 0, 'end': 1}]}))
        for extra in ([], ['--dry-run']):
            result = self.run_cli(*self.args(), '--report', self.report, *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('SRT roundtrip', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(self.report.read_text(), 'prior report')
        self.input.write_bytes((ROOT / 'examples/reflow/reading-profile.json').read_bytes())
        for target in (self.root, self.root / 'missing' / 'report.json'):
            result = self.run_cli(*self.args(), '--report', target)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.report.read_text(), 'prior report')
            self.assertFalse((self.root / 'missing').exists())
            self.assertFalse(list(self.root.glob('.*.tmp')))


if __name__ == '__main__':
    unittest.main()
