import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.reflow.cli import main

ROOT = Path(__file__).resolve().parents[2]


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".tools" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = self.root / "input.json"
        self.output = self.root / "output.srt"
        self.report = self.root / "report.json"
        self.input.write_text((ROOT / "examples/reflow/pause.json").read_text(), encoding="utf-8")

    def run_cli(self, *args):
        # Real process, local argv, no shell; subprocess testing is serial and bounded.
        return subprocess.run([sys.executable, "-m", "mediawrench.reflow", str(self.input), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env=dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1"))

    def output_args(self):
        return ["-o", str(self.output), "--report", str(self.report)]

    def test_earlier_output_input_alias_cannot_be_replaced_by_valid_equals(self):
        before = self.input.read_bytes()
        result = self.run_cli('-o', str(self.input), f'--out={self.output}',
                              '--report', str(self.report))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr.endswith(
            'error: --output must be supplied exactly once\n'), result.stderr)
        self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_earlier_report_input_alias_cannot_be_replaced_by_valid_equals(self):
        before = self.input.read_bytes()
        result = self.run_cli('--report', str(self.input), '-o', str(self.output),
                              f'--rep={self.report}')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr.endswith(
            'error: --report must be supplied exactly once\n'), result.stderr)
        self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def assert_option_error_before_work(self, args, diagnostic, exit_code=2):
        with contextlib.ExitStack() as stack:
            for name in ('validate_paths', 'load_json', 'Config.from_profile', 'Config',
                         'reflow', 'compare_profiles', 'srt_text', 'json_text', 'atomic_write_many'):
                stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                    side_effect=AssertionError('option error reached ' + name)))
            stdout, stderr = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            with self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, exit_code)
            self.assertIn(diagnostic, stderr.getvalue() if exit_code else stdout.getvalue())
            self.assertEqual(stdout.getvalue() if exit_code else stderr.getvalue(), '')

    def test_repeated_reports_preserve_paths_without_reads_or_processing(self):
        profile = ROOT / 'examples/reflow/profiles/brief.json'
        pair = ['--comp', str(profile), str(ROOT / 'examples/reflow/profiles/patient.json')]
        directory = self.root / 'directory'
        directory.mkdir()
        (directory / 'keep').write_bytes(b'nested contents')
        invalid = self.root / 'invalid.json'
        invalid.write_bytes(b'{bad')
        self.output.write_bytes(b'previous SRT')
        self.report.write_bytes(b'previous report')
        symlink = self.root / 'symlink.json'
        symlink.symlink_to(self.input)
        hardlink = self.root / 'hardlink.json'
        os.link(self.input, hardlink)

        def snapshot():
            return {str(p.relative_to(self.root)):
                    ('symlink', os.readlink(p)) if p.is_symlink() else
                    ('directory',) if p.is_dir() else ('file', p.stat().st_ino, p.read_bytes())
                    for p in self.root.rglob('*')}

        before = snapshot()
        earlier = (self.report, self.output, self.input, profile, symlink, hardlink,
                   directory, invalid, self.root / 'absent' / 'report.json', '', '\0')
        for mode in (['-o', str(self.output)], pair):
            for first in earlier:
                for flag in ('--report', '--repor', '--repo', '--rep', '--re', '--r'):
                    for equals in (False, True):
                        for dry in ([], ['--dry-run', '--strict']):
                            with self.subTest(mode=mode, first=first, flag=flag, equals=equals, dry=dry):
                                selection = [f'{flag}={first}'] if equals else [flag, str(first)]
                                self.assert_option_error_before_work(
                                    [str(self.input), *selection, *mode, f'--rep={self.report}', *dry],
                                    '--report must be supplied exactly once')
                                self.assertEqual(snapshot(), before)
        for mode in (['-o', str(self.root / 'absent' / 'out.srt')], pair):
            self.assert_option_error_before_work(
                [str(self.input), '--report', str(self.report), *mode,
                 '--report', str(invalid), f'--r={self.root / "absent" / "final.json"}'],
                '--report must be supplied exactly once')
            self.assertEqual(snapshot(), before)

    def test_repeated_outputs_preserve_paths_without_reads_or_processing(self):
        profile = self.root / 'profile.json'
        profile.write_bytes((ROOT / 'examples/reflow/profiles/brief.json').read_bytes())
        directory = self.root / 'directory'
        directory.mkdir()
        (directory / 'keep').write_bytes(b'nested contents')
        invalid = self.root / 'invalid.json'
        invalid.write_bytes(b'{bad')
        self.output.write_bytes(b'previous SRT')
        self.report.write_bytes(b'previous report')
        symlink = self.root / 'symlink.json'
        symlink.symlink_to(self.input)
        hardlink = self.root / 'hardlink.json'
        os.link(self.input, hardlink)

        def snapshot():
            return {str(p.relative_to(self.root)):
                    ('symlink', os.readlink(p)) if p.is_symlink() else
                    ('directory', p.stat().st_ino) if p.is_dir() else
                    ('file', p.stat().st_ino, p.read_bytes())
                    for p in self.root.rglob('*')}

        before = snapshot()
        earlier = (self.output, self.report, self.input, profile, symlink, hardlink,
                   directory, invalid, self.root / 'absent' / 'out.srt', '', '\0')
        for first in earlier:
            for flag in ('-o', '--output', '--outpu', '--outp', '--out', '--ou', '--o'):
                for equals in (False, True):
                    for dry in ([], ['--dry-run', '--strict']):
                        with self.subTest(first=first, flag=flag, equals=equals, dry=dry):
                            selection = [f'{flag}={first}'] if equals else [flag, str(first)]
                            self.assert_option_error_before_work(
                                [str(self.input), *selection, f'--out={self.output}',
                                 '--report', str(self.report), '--profile', str(profile), *dry],
                                '--output must be supplied exactly once')
                            self.assertEqual(snapshot(), before)
        for args in ([f'-o{self.input}', f'--out={self.output}'],
                     ['-o', str(self.output)] * 3,
                     ['--output', str(self.input), f'-o={invalid}',
                      f'--o={self.root / "absent" / "out.srt"}'],
                     ['-o', str(self.output)] * 4):
            self.assert_option_error_before_work(
                [str(self.input), *args, '--report', str(self.report)],
                '--output must be supplied exactly once')
            self.assertEqual(snapshot(), before)

    def test_output_repetition_error_order(self):
        path = str(ROOT / 'examples/reflow/profiles/brief.json')
        repeated = ['-o', str(self.input), f'--out={self.output}']
        pair = ['--comp', path, path]
        profile = ['--profile', path, f'--prof={path}']
        report = ['--report', path, f'--rep={path}']
        repetition = '--output must be supplied exactly once'
        cases = [(repeated, repetition, 2),
                 ([str(self.input), *repeated], repetition, 2),
                 ([str(self.input), *pair, *repeated], repetition, 2),
                 ([str(self.input), '--profile', path, '--max-cps', '8', *repeated], repetition, 2),
                 ([str(self.input), '--max-chars', '0', *repeated], repetition, 2),
                 ([str(self.root / 'missing.json'), '--profile', str(self.root / 'bad.json'),
                   *repeated, '--dry-run'], repetition, 2),
                 (pair + repeated + pair + profile + report, '--compare-profiles must be supplied exactly once', 2),
                 (repeated + pair + pair, '--compare-profiles must be supplied exactly once', 2),
                 (profile + repeated + report, '--profile must be supplied exactly once', 2),
                 (repeated + profile, '--profile must be supplied exactly once', 2),
                 (report + repeated, '--report must be supplied exactly once', 2),
                 (repeated + report, '--report must be supplied exactly once', 2),
                 (repeated + ['--max-chars', 'bad'], 'argument --max-chars: invalid int value', 2),
                 (repeated + ['--cps-mode', 'display'], 'argument --cps-mode: invalid choice', 2),
                 (repeated + ['--output'], 'argument -o/--output: expected one argument', 2),
                 (repeated + ['--comp', path], 'expected 2 arguments', 2),
                 (repeated + ['--unknown'], 'unrecognized arguments: --unknown', 2),
                 (repeated + ['--max=1'], 'ambiguous option: --max=1', 2),
                 (repeated + ['--inspect-profile', path], '--inspect-profile cannot be combined', 2),
                 (repeated + ['--inspect-pair', path, path], '--inspect-pair cannot be combined', 2),
                 (repeated + ['--inspect-profile', path] * 2, '--inspect-profile must be supplied exactly once', 2),
                 (repeated + ['--inspect-pair', path, path] * 2, '--inspect-pair must be supplied exactly once', 2),
                 (repeated + ['--inspect-pair', path, path, '--inspect-profile', path],
                  '--inspect-pair cannot be combined with --inspect-profile', 2),
                 (repeated + ['--inspect-pair', path, path, '--cps-mode', 'codepoint', '--cps-mode', 'grapheme'],
                  '--cps-mode may be supplied at most once with --inspect-pair', 2),
                 (repeated + ['--help'], 'SRT destination', 0)]
        for args, diagnostic, code in cases:
            with self.subTest(args=args):
                self.assert_option_error_before_work(args, diagnostic, code)
        # One output in comparison retains its incompatible-option error.
        self.assert_option_error_before_work(
            [str(self.input), *pair, '-o', str(self.output), '--dry-run'],
            '--compare-profiles cannot be combined with --output, --profile or --max-cps')

    def test_report_repetition_error_order(self):
        path = str(ROOT / 'examples/reflow/profiles/brief.json')
        repeated = ['--report', str(self.input), f'--rep={self.report}']
        pair = ['--comp', path, path]
        profile = ['--profile', path, f'--prof={path}']
        repetition = '--report must be supplied exactly once'
        cases = [(repeated, repetition, 2),
                 ([str(self.input), *repeated], repetition, 2),
                 ([str(self.input), *pair, *repeated, '-o', str(self.output)], repetition, 2),
                 ([str(self.input), '--profile', path, '--max-cps', '8', *repeated], repetition, 2),
                 ([str(self.input), '--max-chars', '0', *repeated], repetition, 2),
                 (pair + repeated + pair, '--compare-profiles must be supplied exactly once', 2),
                 (repeated + pair + pair + profile, '--compare-profiles must be supplied exactly once', 2),
                 (profile + repeated, '--profile must be supplied exactly once', 2),
                 (repeated + profile, '--profile must be supplied exactly once', 2),
                 (repeated + ['--max-chars', 'bad'], 'argument --max-chars: invalid int value', 2),
                 (repeated + ['--cps-mode', 'display'], 'argument --cps-mode: invalid choice', 2),
                 (repeated + ['--report'], 'argument --report: expected one argument', 2),
                 (repeated + ['--comp', path], 'expected 2 arguments', 2),
                 (repeated + ['--unknown'], 'unrecognized arguments: --unknown', 2),
                 (repeated + ['--max=1'], 'ambiguous option: --max=1', 2),
                 (repeated + ['--inspect-profile', path], '--inspect-profile cannot be combined', 2),
                 (repeated + ['--inspect-pair', path, path], '--inspect-pair cannot be combined', 2),
                 (repeated + ['--inspect-profile', path] * 2, '--inspect-profile must be supplied exactly once', 2),
                 (repeated + ['--inspect-pair', path, path] * 2, '--inspect-pair must be supplied exactly once', 2),
                 (repeated + ['--inspect-pair', path, path, '--inspect-profile', path],
                  '--inspect-pair cannot be combined with --inspect-profile', 2),
                 (repeated + ['--inspect-pair', path, path, '--cps-mode', 'codepoint', '--cps-mode', 'grapheme'],
                  '--cps-mode may be supplied at most once with --inspect-pair', 2),
                 (repeated + ['--help'], 'JSON diagnostic/comparison destination', 0)]
        for args, diagnostic, code in cases:
            with self.subTest(args=args):
                self.assert_option_error_before_work(args, diagnostic, code)

    def test_single_report_spellings_keep_bytes_and_last_cps_mode(self):
        path = str(ROOT / 'examples/reflow/profiles/brief.json')
        for mode in ([], ['--comp', path, str(ROOT / 'examples/reflow/profiles/patient.json')]):
            for last in ('codepoint', 'grapheme'):
                expected = self.run_cli(*mode, '--report', str(self.report),
                                        '--cps-mode', last, '--dry-run')
                self.assertEqual(expected.returncode, 0, expected.stderr)
                for flag in ('--report', '--repor', '--repo', '--rep', '--re', '--r'):
                    result = self.run_cli(*mode, f'{flag}={self.report}', '--cps-mode', 'codepoint',
                                          '--cps-m', 'grapheme', '--cps-mode', last, '--dry-run')
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (expected.returncode, expected.stdout, expected.stderr))
        self.assertFalse(self.report.exists())

    def test_earlier_missing_profile_cannot_be_replaced_by_valid_abbreviation(self):
        result = self.run_cli('--profile', str(self.root / 'missing.json'),
                              '--prof', str(ROOT / 'examples/reflow/profiles/brief.json'), '--dry-run')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr.endswith(
            'error: --profile must be supplied exactly once\n'), result.stderr)

    def test_repeated_profiles_preserve_all_files_and_directories(self):
        valid = ROOT / 'examples/reflow/profiles/brief.json'
        invalid = self.root / 'invalid.json'
        invalid.write_bytes(b'{bad')
        directory = self.root / 'directory'
        directory.mkdir()
        (directory / 'keep').write_bytes(b'nested contents')
        self.output.write_bytes(b'previous SRT')
        self.report.write_bytes(b'previous report')

        def snapshot():
            return {str(p.relative_to(self.root)): None if p.is_dir() else p.read_bytes()
                    for p in self.root.rglob('*')}

        before = snapshot()
        for first in (valid, ROOT / 'examples/reflow/profiles/patient.json', invalid,
                      directory, self.root / 'missing.json', ''):
            for flag in ('--profile', '--profil', '--profi', '--prof', '--pro', '--pr'):
                for equals in (False, True):
                    for dry in ([], ['--dry-run']):
                        with self.subTest(first=first, flag=flag, equals=equals, dry=dry):
                            selection = [f'{flag}={first}'] if equals else [flag, str(first)]
                            result = self.run_cli(*selection, f'--prof={valid}',
                                                  *self.output_args(), *dry)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, '')
                            self.assertTrue(result.stderr.endswith(
                                'error: --profile must be supplied exactly once\n'), result.stderr)
                            self.assertEqual(snapshot(), before)
        for extra in (['--profile', str(valid), '--prof', str(invalid)],
                      ['--profile', str(valid)] * 3):
            result = self.run_cli(*extra, '-o', str(self.root / 'absent' / 'out.srt'),
                                  '--report', str(self.report))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertIn('--profile must be supplied exactly once', result.stderr)
            self.assertEqual(snapshot(), before)

    def test_profile_repetition_option_order_before_reads_and_processing(self):
        path = str(ROOT / 'examples/reflow/profiles/brief.json')
        repeated = ['--profile', path, f'--prof={path}']
        pair = ['--comp', path, path]
        repetition = '--profile must be supplied exactly once'
        cases = [(repeated, repetition, 2),  # Before missing transcript/destinations.
                 ([str(self.input), *repeated], repetition, 2),
                 ([str(self.input), *repeated, '--max-cps', '8'], repetition, 2),
                 ([str(self.input), *repeated, '--report', path, '--max-chars', '0'], repetition, 2),
                 ([str(self.input), *pair, *repeated], repetition, 2),
                 (pair + repeated + pair, '--compare-profiles must be supplied exactly once', 2),
                 (repeated + pair + pair, '--compare-profiles must be supplied exactly once', 2),
                 (repeated + ['--max-chars', 'bad'], 'argument --max-chars: invalid int value', 2),
                 (repeated + ['--cps-mode', 'display'], 'argument --cps-mode: invalid choice', 2),
                 (repeated + ['--profile'], 'argument --profile: expected one argument', 2),
                 (repeated + ['--unknown'], 'unrecognized arguments: --unknown', 2),
                 (repeated + ['--max=1'], 'ambiguous option: --max=1', 2),
                 (repeated + ['--inspect-profile', path], '--inspect-profile cannot be combined', 2),
                 (repeated + ['--inspect-pair', path, path], '--inspect-pair cannot be combined', 2),
                 (repeated + ['--inspect-profile', path] * 2, '--inspect-profile must be supplied exactly once', 2),
                 (repeated + ['--inspect-pair', path, path, '--inspect-profile', path],
                  '--inspect-pair cannot be combined with --inspect-profile', 2),
                 (repeated + ['--inspect-profile', path, '--cps-mode', 'codepoint', '--cps-mode', 'grapheme'],
                  '--cps-mode may be supplied at most once with --inspect-profile', 2),
                 (repeated + ['--help'], 'supply exactly once', 0)]
        for args, diagnostic, exit_code in cases:
            with self.subTest(args=args), contextlib.ExitStack() as stack:
                for name in ('load_json', 'validate_paths', 'reflow', 'compare_profiles',
                             'srt_text', 'json_text', 'atomic_write_many', 'Config.from_profile', 'Config'):
                    stack.enter_context(patch('mediawrench.reflow.cli.' + name,
                        side_effect=AssertionError('option error reached ' + name)))
                stdout, stderr = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(stdout))
                stack.enter_context(contextlib.redirect_stderr(stderr))
                stack.enter_context(patch.object(sys, 'argv', ['mediawrench reflow', *args]))
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, exit_code)
                self.assertIn(diagnostic, stderr.getvalue() if exit_code else stdout.getvalue())
                self.assertEqual(stdout.getvalue() if exit_code else stderr.getvalue(), '')

    def test_single_profile_spellings_and_last_cps_mode_keep_results(self):
        path = str(ROOT / 'examples/reflow/profiles/brief.json')
        for first, last in (('codepoint', 'grapheme'), ('grapheme', 'codepoint')):
            expected = self.run_cli('--profile', path, '--cps-mode', last, '--dry-run')
            self.assertEqual(expected.returncode, 0, expected.stderr)
            for flag in ('--profile', '--profil', '--profi', '--prof', '--pro', '--pr'):
                result = self.run_cli(f'{flag}={path}', '--cps-mode', first,
                                      f'--cps-m={last}', '--dry-run')
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (expected.returncode, expected.stdout, expected.stderr))
                config = json.loads(result.stdout)['config']
                self.assertEqual((config['profile_name'], config['cps_mode']), ('brief', last))

    def test_real_cli_json_to_srt_and_report(self):
        result = self.run_cli(*self.output_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("00:00:02,000 --> 00:00:03,000", self.output.read_text())
        report = json.loads(self.report.read_text())
        self.assertTrue(report["srt_roundtrip"]["verified"])
        self.assertEqual(report["metrics"]["mapped_words"], 4)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_dry_run_no_output_or_parent_creation(self):
        missing = self.root / "absent" / "cue.srt"
        result = self.run_cli("--dry-run", "-o", str(missing), "--report", str(self.report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["metrics"]["source_words"], 4)
        self.assertFalse(missing.parent.exists())
        self.assertFalse(self.report.exists())

    def test_invalid_input_leaves_existing_outputs_unchanged(self):
        self.input.write_text('{"segments": [{"words": [{"text": "unknown"}]}]}')
        self.output.write_text("previous SRT")
        self.report.write_text("previous report")
        result = self.run_cli(*self.output_args())
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.output.read_text(), "previous SRT")
        self.assertEqual(self.report.read_text(), "previous report")

    def test_nonfinite_and_duplicate_json_rejected(self):
        for raw in ('{"words":[{"word":"A","start":NaN,"end":1}]}',
                    '{"words":[{"word":"A","start":1e999,"end":1}]}',
                    '{"segments":[],"segments":[]}', '{bad'):
            self.input.write_text(raw)
            result = self.run_cli(*self.output_args())
            self.assertEqual(result.returncode, 2, raw)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.report.exists())

    def test_invalid_utf8_rejected(self):
        self.input.write_bytes(b'\xff')
        result = self.run_cli(*self.output_args())
        self.assertEqual(result.returncode, 2)
        self.assertIn("UTF-8", result.stderr)

    def test_infeasible_strict_emits_untruncated_outputs_exit_three(self):
        word = "unavoidablylongword"
        self.input.write_text(json.dumps({"words": [{"word": word, "start": 0, "end": 1}]}))
        result = self.run_cli(*self.output_args(), "--max-chars", "3", "--strict")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(word, self.output.read_text())
        report = json.loads(self.report.read_text())
        self.assertFalse(report["feasible"])
        self.assertEqual(report["constraints"][0]["code"], "line_width")
        result = self.run_cli(*self.output_args(), "--max-chars", "3")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_display_width_modes_and_strict_cluster_overflow(self):
        self.input.write_text(json.dumps({"segments": [{"text": "你好世界", "start": 0, "end": 3}]}))
        for mode, expected in (("codepoint", ["你好世界"]), ("display", ["你好", "世界"])):
            result = self.run_cli(*self.output_args(), "--max-chars", "4", "--width-mode", mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(self.report.read_text())
            self.assertEqual(report["cues"][0]["lines"], expected)
            self.assertEqual(report["config"]["width_mode"], mode)
            self.assertTrue(report["srt_roundtrip"]["verified"])
        self.input.write_text(json.dumps({"words": [{"word": "👩🏽‍💻", "start": 0, "end": 3}]}))
        result = self.run_cli(*self.output_args(), "--max-chars", "1", "--width-mode", "display", "--strict")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("👩🏽‍💻", self.output.read_text())
        report = json.loads(self.report.read_text())
        self.assertFalse(report["feasible"])
        self.assertEqual(report["cues"][0]["line_widths"], [2])
        before = (self.output.read_bytes(), self.report.read_bytes())
        result = self.run_cli(*self.output_args(), "--width-mode", "pixels")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, (self.output.read_bytes(), self.report.read_bytes()))

    def test_input_output_collision_rejected(self):
        before = self.input.read_bytes()
        result = self.run_cli("-o", str(self.input), "--report", str(self.report))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(self.report.exists())

    def test_cps_modes_real_cli_strict_and_dry_run(self):
        self.input.write_text(json.dumps({"segments": [{"text": "e\u0301 👩🏽‍💻", "start": 0, "end": 1}]}))
        for mode, count, exit_code in (("codepoint", 7, 3), ("grapheme", 3, 0)):
            args = ["--cps-mode", mode, "--max-cps", "3", "--strict", "--width-mode", "display"]
            result = self.run_cli(*self.output_args(), *args)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            report = json.loads(self.report.read_text())
            self.assertEqual(report["config"]["cps_mode"], mode)
            rate = report["cues"][0]["speaking_rate"]
            self.assertEqual((rate["count"], rate["cps"]), (count, count))
            self.assertEqual(rate["rate_unit"], "codepoints/second" if mode == "codepoint" else "graphemes/second")
            self.assertTrue(report["srt_roundtrip"]["verified"])
            self.assertIn("e\u0301 👩🏽‍💻", self.output.read_text())
            before = (self.output.read_bytes(), self.report.read_bytes())
            dry = self.run_cli(*self.output_args(), *args, "--dry-run")
            self.assertEqual(dry.returncode, exit_code, dry.stderr)
            self.assertEqual(json.loads(dry.stdout), report)
            self.assertEqual(before, (self.output.read_bytes(), self.report.read_bytes()))
        result = self.run_cli(*self.output_args(), "--cps-mode", "grapheme", "--max-cps", "2", "--strict")
        self.assertEqual(result.returncode, 3, result.stderr)
        violation, = json.loads(self.report.read_text())["constraints"]
        self.assertEqual((violation["cps"], violation["rate_unit"]), (3, "graphemes/second"))
        self.assertIn("e\u0301 👩🏽‍💻", self.output.read_text())
        # A named profile controls the numeric bound; mode remains independent.
        profile = self.root / "profile.json"
        profile.write_text(json.dumps(dict(name="custom", max_cps=3, min_duration=0, min_gap=0)))
        result = self.run_cli("--profile", str(profile), "--cps-mode", "grapheme", "--strict", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["config"]["profile_name"], "custom")

    def test_invalid_cps_modes_leave_outputs_untouched(self):
        self.output.write_text("previous SRT")
        self.report.write_text("previous report")
        before_input = self.input.read_bytes()
        for mode in ("display", "", "GRAPHEME"):
            result = self.run_cli(*self.output_args(), "--cps-mode", mode)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--cps-mode", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual((self.output.read_text(), self.report.read_text()), ("previous SRT", "previous report"))
            self.assertEqual(self.input.read_bytes(), before_input)

    def test_named_profiles_real_cli_and_strict_paragraph_conflict(self):
        self.input.write_text((ROOT / "examples/reflow/reading-profile.json").read_text())
        for name, texts in (("brief", ["One. Two.", "Three."]), ("patient", ["One. Two. Three."])):
            args = ["--profile", str(ROOT / f"examples/reflow/profiles/{name}.json"), "--target-duration", ".5"]
            result = self.run_cli(*self.output_args(), *args, "--strict")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(self.report.read_text())
            self.assertEqual([c["text"] for c in report["cues"]], texts)
            self.assertEqual(report["config"]["profile_name"], name)
            self.assertTrue(report["srt_roundtrip"]["verified"])
            before = (self.output.read_bytes(), self.report.read_bytes())
            self.assertEqual(self.run_cli(*self.output_args(), *args).returncode, 0)
            self.assertEqual(before, (self.output.read_bytes(), self.report.read_bytes()))
        self.input.write_text((ROOT / "examples/reflow/profile-conflict.json").read_text())
        result = self.run_cli(*self.output_args(), "--profile", str(ROOT / "examples/reflow/profiles/patient.json"), "--strict")
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertFalse(report["feasible"])
        self.assertEqual([c["text"] for c in report["cues"]], ["One.", "Two."])
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in report["cues"]], [(0, 400), (400, 800)])
        self.assertEqual({v["code"] for v in report["constraints"]}, {"minimum_duration", "minimum_gap"})
        self.assertIn("00:00:00,400 --> 00:00:00,800", self.output.read_text())

    def test_invalid_profiles_and_overrides_leave_outputs_untouched(self):
        profile = self.root / "profile.json"
        self.output.write_text("previous SRT")
        self.report.write_text("previous report")
        valid = dict(name="custom", max_cps=15, min_duration=1, min_gap=.1)
        for raw in ("{bad", '{"name":"x","name":"y"}', json.dumps(dict(valid, min_gap=-1)),
                    json.dumps(dict(valid, min_duraton=2)), json.dumps(dict(valid, max_cps=True)), "{}"):
            profile.write_text(raw)
            result = self.run_cli(*self.output_args(), "--profile", str(profile))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual((self.output.read_text(), self.report.read_text()), ("previous SRT", "previous report"))
        profile.write_text(json.dumps(valid))
        result = self.run_cli(*self.output_args(), "--profile", str(profile), "--max-cps", "30")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("override", result.stderr)
        for path in ("", str(self.root / "missing-profile.json")):
            result = self.run_cli(*self.output_args(), "--profile", path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual((self.output.read_text(), self.report.read_text()), ("previous SRT", "previous report"))
        before = profile.read_bytes()
        result = self.run_cli("-o", str(profile), "--report", str(self.report), "--profile", str(profile))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(profile.read_bytes(), before)
        self.assertEqual(self.report.read_text(), "previous report")

    def test_symlink_and_hardlink_collision_rejected(self):
        for kind in ("symlink", "hardlink"):
            if kind == "symlink":
                self.output.symlink_to(self.input)
            else:
                os.link(self.input, self.output)
            result = self.run_cli(*self.output_args())
            self.assertEqual(result.returncode, 2, result.stderr)
            self.output.unlink()

    def test_outputs_collision_rejected(self):
        result = self.run_cli("-o", str(self.output), "--report", str(self.output))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_staging_failure_leaves_existing_output_unchanged(self):
        self.output.write_text("previous")
        result = self.run_cli("-o", str(self.output), "--report", str(self.root / "missing" / "report.json"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(), "previous")
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_directory_output_does_not_modify_other_destination(self):
        self.output.write_text("previous")
        result = self.run_cli("-o", str(self.output), "--report", str(self.root))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(), "previous")

    def test_missing_required_output_and_bad_options(self):
        for args in ([], ["--dry-run", "--max-chars", "0"], ["--dry-run", "--pause", "nan"],
                     ["--dry-run", "--algorithm", "unknown"],
                     ["--dry-run", "--algorithm", "fixed", "--group-words", "0"]):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertNotIn("Traceback", result.stderr)

    def test_deterministic_output_bytes(self):
        self.assertEqual(self.run_cli(*self.output_args()).returncode, 0)
        before = (self.output.read_bytes(), self.report.read_bytes())
        self.assertEqual(self.run_cli(*self.output_args()).returncode, 0)
        self.assertEqual(before, (self.output.read_bytes(), self.report.read_bytes()))

    def test_srt_unsafe_text_produces_no_files(self):
        self.input.write_text(json.dumps({"segments": [{"text": "00:00:00,000 --> 00:00:01,000", "start": 0, "end": 1}]}))
        result = self.run_cli(*self.output_args())
        self.assertEqual(result.returncode, 2)
        self.assertIn("SRT roundtrip", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_bom_json_accepted(self):
        self.input.write_bytes(b'\xef\xbb\xbf' + self.input.read_bytes())
        self.assertEqual(self.run_cli(*self.output_args()).returncode, 0)

    def test_top_level_transcript_mirror(self):
        fixture = json.loads(self.input.read_text())
        fixture["text"] = "  Hello there.  Next topic.  "
        self.input.write_text(json.dumps(fixture))
        self.assertEqual(self.run_cli(*self.output_args()).returncode, 0)
        self.assertEqual(json.loads(self.report.read_text())["source_text"], fixture["text"])
        fixture["text"] += " Missing words."
        self.input.write_text(json.dumps(fixture))
        self.assertEqual(self.run_cli(*self.output_args()).returncode, 2)


if __name__ == "__main__":
    unittest.main()
