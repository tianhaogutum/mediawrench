"""Saved rate units, legacy resolution, and no-write conflicts in both CLI paths."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.reflow import Config, InputError, compare_profiles, reflow
from mediawrench.reflow.io import load_json

ROOT = Path(__file__).resolve().parents[2]


def profile(**fields):
    return dict(dict(name='saved', max_cps=3, min_duration=0, min_gap=0), **fields)


class ProfileModeTests(unittest.TestCase):
    def setUp(self):
        self.data = load_json(ROOT / 'examples/reflow/grapheme-rate.json')

    def test_saved_units_reach_config_cues_and_violations_in_either_width_mode(self):
        for mode, count, units in (('codepoint', 7, 'codepoints'), ('grapheme', 3, 'graphemes')):
            saved = profile(cps_mode=mode, max_cps=2)
            before = copy.deepcopy(saved)
            for width in ('codepoint', 'display'):
                with self.subTest(mode=mode, width=width):
                    config = Config.from_profile(saved, width_mode=width)
                    # Fix the two-word group to inspect both infeasible rate units.
                    report = reflow(self.data, config, algorithm='fixed', group_words=2)
                    self.assertEqual(report['config']['cps_mode'], mode)
                    self.assertEqual(report['config']['max_cps'], 2)
                    cue, = report['cues']
                    self.assertEqual((cue['start_ms'], cue['end_ms']), (0, 1000))
                    self.assertEqual(cue['speaking_rate'], dict(cps_mode=mode, count=count,
                        count_unit=units, cps=count, rate_unit=units + '/second'))
                    violation, = report['constraints']
                    self.assertEqual((violation['code'], violation['cps_mode'], violation['cps'],
                        violation['max_cps'], violation['rate_unit']),
                        ('speaking_rate', mode, count, 2, units + '/second'))
                    self.assertEqual(violation['unit_ids'], ['/segments/0/words/0', '/segments/0/words/1'])
                    self.assertEqual(config, Config.from_profile(saved, cps_mode=mode, width_mode=width))
            self.assertEqual(saved, before)

    def test_legacy_defaults_scores_partitions_and_explicit_modes_remain_compatible(self):
        for name, objective, texts in (('brief', [0, 0, 1400], ['One. Two.', 'Three.']),
                                       ('patient', [0, 0, 3640], ['One. Two. Three.'])):
            saved = load_json(ROOT / f'examples/reflow/profiles/{name}.json')
            self.assertEqual(len(saved), 4)
            default = Config.from_profile(saved, target_duration=.5)
            explicit = Config.from_profile(saved, cps_mode='codepoint', target_duration=.5)
            report = reflow(load_json(ROOT / 'examples/reflow/reading-profile.json'), default)
            self.assertEqual(report, reflow(load_json(ROOT / 'examples/reflow/reading-profile.json'), explicit))
            self.assertEqual(report['objective'], objective)
            self.assertEqual([c['text'] for c in report['cues']], texts)
            self.assertEqual(default.cps_mode, 'codepoint')
        for mode in ('codepoint', 'grapheme'):
            self.assertEqual(Config.from_profile(profile(), cps_mode=mode),
                             Config.from_profile(profile(cps_mode=mode)))

    def test_saved_modes_reject_conflicts_and_invalid_values(self):
        for saved, explicit in (('grapheme', 'codepoint'), ('codepoint', 'grapheme')):
            with self.assertRaisesRegex(InputError, 'conflict'):
                Config.from_profile(profile(cps_mode=saved), cps_mode=explicit)
        for invalid in ('', 'display', 'GRAPHEME', None, True, 1, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(InputError, 'cps_mode'):
                    Config.from_profile(profile(cps_mode=invalid))
                for saved in (profile(), profile(cps_mode='grapheme')):
                    with self.assertRaises(InputError):
                        Config.from_profile(saved, cps_mode=invalid)
        for invalid in (dict(profile(cps_mode='grapheme'), extra=True),
                        {k: v for k, v in profile(cps_mode='grapheme').items() if k != 'min_gap'}):
            with self.assertRaises(InputError):
                Config.from_profile(invalid)

    def test_comparison_keeps_independent_saved_units_and_source_keyed_tradeoff(self):
        for limit in (2, 3):
            left = profile(name='points', cps_mode='codepoint', max_cps=limit)
            right = profile(name='clusters', cps_mode='grapheme', max_cps=limit)
            result = compare_profiles(self.data, left, right, algorithm='fixed', group_words=2)
            self.assertEqual(result['boundary_differences'], [])
            change, = result['constraint_differences']
            self.assertEqual(change['code'], 'speaking_rate')
            self.assertEqual(change['unit_ids'], ['/segments/0/words/0', '/segments/0/words/1'])
            self.assertEqual(change['status'], 'changed' if limit == 2 else 'left_only')
            self.assertEqual(change['left']['rate_unit'], 'codepoints/second')
            if limit == 2:
                self.assertEqual(change['right']['rate_unit'], 'graphemes/second')
            else:
                self.assertIsNone(change['right'])
            for side, mode, count in (('left', 'codepoint', 7), ('right', 'grapheme', 3)):
                summary = result['profiles'][side]
                self.assertEqual(summary['config']['cps_mode'], mode)
                self.assertEqual(summary['cues'][0]['speaking_rate']['cps'], count)
                self.assertEqual(summary['feasible'], side == 'right' and limit == 3)
            for mode in ('codepoint', 'grapheme'):
                with self.assertRaisesRegex(InputError, 'conflict'):
                    compare_profiles(self.data, left, right, cps_mode=mode)


class ProfileModeCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.input = self.root / 'input.json'
        self.input.write_bytes((ROOT / 'examples/reflow/grapheme-rate.json').read_bytes())
        self.left = self.root / 'left.json'
        self.right = self.root / 'right.json'
        self.output = self.root / 'output.srt'
        self.report = self.root / 'report.json'

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'mediawrench.reflow', str(self.input), *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def write_profile(self, path, **fields):
        path.write_text(json.dumps(profile(**fields)), encoding='utf-8')

    def single_args(self):
        return ['--profile', self.left, '-o', self.output, '--report', self.report]

    def compare_args(self):
        return ['--compare-profiles', self.left, self.right, '--report', self.report]

    def assert_no_write(self, args, diagnostic=None):
        self.output.write_bytes(b'previous SRT')
        self.report.write_bytes(b'previous report')
        before = {p: p.read_bytes() for p in self.root.iterdir()}
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
        if diagnostic:
            self.assertIn(diagnostic, result.stderr)
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_saved_profile_units_real_cli_strict_equal_override_and_dry_run(self):
        for mode, count, exit_code in (('codepoint', 7, 3), ('grapheme', 3, 0)):
            self.write_profile(self.left, cps_mode=mode)
            for width in ('codepoint', 'display'):
                result = self.run_cli(*self.single_args(), '--strict', '--width-mode', width)
                self.assertEqual(result.returncode, exit_code, result.stderr)
                report = json.loads(self.report.read_text())
                self.assertEqual(report['config']['cps_mode'], mode)
                self.assertEqual(report['cues'][0]['speaking_rate']['cps'], count)
                self.assertTrue(report['srt_roundtrip']['verified'])
                self.assertIn('e\u0301 👩🏽‍💻', self.output.read_text())
                before = (self.output.read_bytes(), self.report.read_bytes())
                dry = self.run_cli(*self.single_args(), '--strict', '--width-mode', width,
                                   '--cps-mode', mode, '--dry-run')
                self.assertEqual(dry.returncode, exit_code, dry.stderr)
                self.assertEqual(json.loads(dry.stdout), report)
                self.assertEqual(before, (self.output.read_bytes(), self.report.read_bytes()))

    def test_legacy_profile_cli_default_and_explicit_mode(self):
        self.write_profile(self.left)
        for extra, mode, count in (([], 'codepoint', 7), (['--cps-mode', 'grapheme'], 'grapheme', 3)):
            result = self.run_cli(*self.single_args(), *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(self.report.read_text())
            self.assertEqual(report['config']['cps_mode'], mode)
            self.assertEqual(report['cues'][0]['speaking_rate']['cps'], count)

    def test_invalid_saved_modes_fields_and_conflicts_preserve_every_file(self):
        for mode in ('display', '', 'GRAPHEME', None, True, 1, [], {}):
            self.write_profile(self.left, cps_mode=mode)
            with self.subTest(mode=mode):
                self.assert_no_write(self.single_args(), 'cps_mode')
        for raw in (json.dumps(profile(cps_mode='grapheme', extra=1)),
                    '{"name":"saved","max_cps":3,"min_duration":0,"cps_mode":"grapheme"}',
                    '{"name":"saved","max_cps":3,"min_duration":0,"min_gap":0,'
                    '"cps_mode":"grapheme","cps_mode":"codepoint"}'):
            self.left.write_text(raw)
            self.assert_no_write(self.single_args())
        for mode, explicit in (('grapheme', 'codepoint'), ('codepoint', 'grapheme')):
            self.write_profile(self.left, cps_mode=mode)
            for dry in ([], ['--dry-run']):
                self.assert_no_write([*self.single_args(), '--cps-mode', explicit, *dry], 'conflict')

    def test_comparison_saved_and_legacy_units_equal_explicit_and_conflicts(self):
        self.write_profile(self.left, name='legacy')
        self.write_profile(self.right, name='clusters', cps_mode='grapheme')
        result = self.run_cli(*self.compare_args(), '--strict')
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual([p['config']['cps_mode'] for p in report['profiles'].values()], ['codepoint', 'grapheme'])
        self.assertEqual(report['constraint_differences'][0]['status'], 'left_only')
        result = self.run_cli(*self.compare_args(), '--strict', '--cps-mode', 'grapheme')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['constraint_differences'], [])
        self.assertTrue(all(p['config']['cps_mode'] == 'grapheme' for p in report['profiles'].values()))
        self.write_profile(self.left, name='points', cps_mode='codepoint')
        for mode in ('codepoint', 'grapheme'):
            for dry in ([], ['--dry-run']):
                self.assert_no_write([*self.compare_args(), '--cps-mode', mode, *dry], 'conflict')
        # Invalid units on either comparison side also preserve the report.
        for path, other in ((self.left, self.right), (self.right, self.left)):
            self.write_profile(other, name='valid')
            self.write_profile(path, name='invalid', cps_mode=None)
            self.assert_no_write(self.compare_args(), 'cps_mode')


if __name__ == '__main__':
    unittest.main()
