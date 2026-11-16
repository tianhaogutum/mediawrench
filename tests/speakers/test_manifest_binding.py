"""Expected manifest hashes bind one raw snapshot before any decoding/preflight."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, run_manifest
from mediawrench.speakers.cli import main
from mediawrench.speakers.export import render_json, write_atomic
from . import test_manifest_observed_snapshot as snapshots
from . import test_manifest_observed as observations

ROOT = Path(__file__).resolve().parents[2]


def sha(data):
    return hashlib.new('sha256', data).hexdigest()


def captured(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


class ManifestBindingTests(unittest.TestCase):
    def fixture(self, root):
        return snapshots.ManifestObservedSnapshotTests().fixture(root)

    def args(self, path, output, expected, observe=False, dry=False):
        return ['--manifest', str(path), '--output-dir', str(output),
                '--manifest-sha256', expected, '--summary-format', 'json',
                *(['--observe-input-sha256'] if observe else []),
                *(['--dry-run'] if dry else [])]

    def test_same_path_whitespace_mutation_rejected_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            raw = path.read_bytes()
            expected = sha(raw)
            path.write_bytes(raw + b' \r\n')
            self.assertEqual(json.loads(raw), json.loads(path.read_bytes()))
            actual = sha(path.read_bytes())
            self.assertNotEqual(expected, actual)
            before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
            for observe in (False, True):
                for dry in (False, True):
                    with self.subTest(observe=observe, dry=dry):
                        with patch('mediawrench.speakers.manifest._entries') as preflight:
                            with patch('mediawrench.speakers.manifest._read_input') as read:
                                with self.assertRaisesRegex(BridgeError, 'manifest.*SHA-256 mismatch') as error:
                                    run_manifest(path, root / 'out', dry_run=dry,
                                                 observe_input_sha256=observe,
                                                 expected_manifest_sha256=expected)
                                code, stdout, stderr = captured(self.args(path, root / 'out', expected, observe, dry))
                                preflight.assert_not_called()
                                read.assert_not_called()
                        self.assertEqual(code, 2)
                        self.assertEqual(stdout, '')
                        for detail in (str(path), f'expected {expected}', f'actual {actual}'):
                            self.assertIn(detail, str(error.exception))
                            self.assertIn(detail, stderr)
                        self.assertEqual({p: p.read_bytes() for p in root.iterdir() if p.is_file()}, before)
                        self.assertFalse((root / 'out').exists())

    def test_matching_single_raw_read_checks_observes_and_parses_same_buffer(self):
        for observe in (False, True):
            for dry in (False, True):
                with self.subTest(observe=observe, dry=dry), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = self.fixture(root)
                    raw = path.read_bytes()
                    self.assertIn(b'\r\n', raw)
                    self.assertGreater(len(raw), len(raw.decode('utf-8')))
                    expected = sha(raw)
                    self.assertNotEqual(expected, sha(raw.replace(b'\r\n', b'\n')))
                    baseline = run_manifest(path, root / 'baseline', observe_input_sha256=observe)
                    reads, modes = [], []
                    read_bytes, open_file = Path.read_bytes, Path.open

                    def track_open(file, mode='r', *args, **kwargs):
                        if file == path and 'r' in mode:
                            modes.append(mode)
                        return open_file(file, mode, *args, **kwargs)

                    def mutate(file):
                        data = read_bytes(file)
                        reads.append(file)
                        if file == path:
                            file.write_bytes(b'\xffinvalid replacement')
                        return data

                    with patch.object(Path, 'open', track_open), patch.object(Path, 'read_bytes', mutate):
                        summary = run_manifest(path, root / 'out', dry_run=dry,
                                               observe_input_sha256=observe,
                                               expected_manifest_sha256=expected)
                    self.assertEqual(modes, ['rb'])
                    self.assertEqual(reads, [path] + [Path(baseline['entries'][0][k])
                                                     for k in ('asr', 'aliases', 'rttm')])
                    self.assertEqual(path.read_bytes(), b'\xffinvalid replacement')
                    if observe:
                        self.assertEqual(summary['observed_manifest'], dict(sha256=expected, size_bytes=len(raw)))
                    else:
                        self.assertNotIn('observed_manifest', summary)
                        self.assertNotIn('observed_inputs', summary['entries'][0])
                    summary['dry_run'] = False
                    self.assertEqual(summary, baseline)
                    self.assertNotIn('expected_manifest_sha256', summary)
                    self.assertEqual((root / 'out').exists(), not dry)
                    if not dry:
                        for file in (root / 'out').iterdir():
                            self.assertEqual(file.read_bytes(), (root / 'baseline' / file.name).read_bytes())

    def test_mismatch_precedes_malformed_bytes_structure_and_output_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            valid = path.read_bytes()
            value = json.loads(valid)
            malformed = [b'\xff\r\n', b'{', b'', b'\xef\xbb\xbf' + valid,
                         b'{"schema_version":1,"schema_version":1,"entries":[]}',
                         json.dumps(dict(value, schema_version=True)).encode(),
                         json.dumps(dict(value, expected_manifest_sha256='a' * 64)).encode()]
            for raw in malformed:
                path.write_bytes(raw)
                for observe in (False, True):
                    for dry in (False, True):
                        with self.subTest(raw=raw, observe=observe, dry=dry):
                            output = root / 'missing-parent' / 'out'
                            reads = []
                            read_bytes = Path.read_bytes

                            def track(file):
                                reads.append(file)
                                return read_bytes(file)

                            with patch.object(Path, 'read_bytes', track):
                                code, stdout, stderr = captured(self.args(path, output, sha(valid), observe, dry))
                            self.assertEqual(reads, [path])
                            self.assertEqual(code, 2)
                            self.assertEqual(stdout, '')
                            for detail in ('manifest', 'SHA-256 mismatch', str(path), sha(valid), sha(raw)):
                                self.assertIn(detail, stderr)
                            self.assertFalse(output.parent.exists())
                # Matching malformed bytes must reach ordinary decode/preflight rejection.
                with patch('mediawrench.speakers.manifest._read_input') as read:
                    code, stdout, stderr = captured(self.args(path, root / 'out', sha(raw), True))
                    read.assert_not_called()
                self.assertEqual(code, 2)
                self.assertEqual(stdout, '')
                self.assertNotIn('SHA-256 mismatch', stderr)
                self.assertFalse((root / 'out').exists())

    def test_invalid_syntax_types_and_mode_fail_before_file_reads(self):
        invalid_strings = ['', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64,
                           'a' * 63 + '\n', ' ' + 'a' * 64, 'a' * 64 + ' ',
                           '０' * 64, 'é' * 64, '\x00' * 64]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'missing-manifest'
            with patch.object(Path, 'read_bytes') as read, patch.object(Path, 'read_text') as text_read:
                for value in invalid_strings + [False, True, 0, 1, 1.5, b'a' * 64, [], {}, object()]:
                    with self.subTest(value=value):
                        with self.assertRaisesRegex(BridgeError, 'expected_manifest_sha256.*64 lowercase'):
                            run_manifest(path, root / 'out', expected_manifest_sha256=value)
                for value in invalid_strings[:-1]:  # argv cannot contain NUL at OS boundary.
                    code, stdout, stderr = captured(self.args(path, root / 'out', value))
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, '')
                    self.assertIn('--manifest-sha256', stderr)
                    self.assertIn('64 lowercase', stderr)
                for argv in ([ '--manifest-sha256', 'a' * 64],
                             ['--manifest-sha256', 'a' * 64, '--asr', 'missing', '--rttm', 'missing'],
                             ['--manifest', str(path), '--output-dir', str(root / 'out'), '--manifest-sha256'],
                             self.args(path, root / 'out', 'a' * 64) + ['--asr', 'missing'],
                             ['--manifest', str(path), '--manifest-sha256', 'a' * 64]):
                    code, stdout, stderr = captured(argv)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, '')
                read.assert_not_called()
                text_read.assert_not_called()
            self.assertFalse((root / 'out').exists())
            # Valid expectation cannot replace an actual successful byte read.
            for is_directory in (False, True):
                if is_directory:
                    path.mkdir()
                code, stdout, stderr = captured(self.args(path, root / 'out', 'a' * 64))
                self.assertEqual(code, 2)
                self.assertEqual(stdout, '')
                self.assertNotIn('SHA-256 mismatch', stderr)

    def test_matching_preserves_summaries_results_outcomes_and_dry_run(self):
        for order in (['srt', 'markdown'], ['markdown', 'srt']):
            for case in ('complete', 'mixed', 'failed', 'json-only'):
                for observe in (False, True):
                    with self.subTest(order=order, case=case, observe=observe), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        fixture = observations.ManifestObservedTests()
                        entries = fixture.fixture(root)
                        for entry in entries:
                            entry['exports'] = order
                        if case in ('complete', 'json-only'):
                            entries = entries[1:]
                        elif case == 'failed':
                            entries = entries[1:]
                            for entry in entries:
                                (root / entry['asr']).write_bytes(b'\xffmismatch')
                        else:
                            # Mix overflow, clipping and a byte mismatch at the same input path.
                            (root / entries[-1]['asr']).write_bytes(b'\xffmismatch')
                        if case == 'json-only':
                            for entry in entries:
                                entry.pop('exports')
                        path = fixture.save(root, entries)
                        expected = sha(path.read_bytes())
                        baseline = run_manifest(path, root / 'baseline', observe_input_sha256=observe)
                        none = run_manifest(path, root / 'none', observe_input_sha256=observe,
                                            expected_manifest_sha256=None)
                        self.assertEqual(render_json(none), render_json(baseline))
                        output = root / 'out'
                        before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                        def cli(dry):
                            return subprocess.run([sys.executable, '-m', 'mediawrench.speakers',
                                                   *self.args(path, output, expected, observe, dry)],
                                                  cwd=ROOT, capture_output=True, text=True, timeout=120)
                        dry = cli(True)
                        self.assertEqual({p: p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
                        self.assertFalse(output.exists())
                        real = cli(False)
                        code = int(case in ('mixed', 'failed'))
                        self.assertEqual(real.returncode, code, real.stderr)
                        self.assertEqual(dry.returncode, code, dry.stderr)
                        self.assertEqual(real.stderr, dry.stderr)
                        summary = json.loads(real.stdout)
                        self.assertEqual(real.stdout, (output / 'summary.json').read_text())
                        self.assertEqual(summary, baseline)
                        self.assertEqual(summary, run_manifest(path, root / 'library',
                                         observe_input_sha256=observe, expected_manifest_sha256=expected))
                        planned = json.loads(dry.stdout)
                        self.assertEqual(planned, run_manifest(path, root / 'unused', dry_run=True,
                                         observe_input_sha256=observe, expected_manifest_sha256=expected))
                        self.assertTrue(planned['dry_run'])
                        planned['dry_run'] = False
                        self.assertEqual(planned, summary)
                        for file in output.iterdir():
                            self.assertEqual(file.read_bytes(), (root / 'baseline' / file.name).read_bytes())
                        # Expected digest alone does not alter default text stdout.
                        args = self.args(path, root / 'text-dry', expected, observe, True)
                        args[args.index('json')] = 'text'
                        self.assertEqual(captured(args), captured(args[:args.index('--manifest-sha256')] + args[args.index('--manifest-sha256') + 2:]))

    def test_matching_retains_directory_refusal_and_write_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            expected = sha(path.read_bytes())
            keep, existing = root / 'keep', root / 'existing'
            keep.write_bytes(b'preserve')
            existing.mkdir()
            (existing / 'keep').write_bytes(b'preserve')
            live, dangling = root / 'live', root / 'dangling'
            live.symlink_to(existing)
            dangling.symlink_to(root / 'absent')
            for target in (keep, existing, live, dangling, root / 'missing-parent' / 'out'):
                for dry in (False, True):
                    code, stdout, stderr = captured(self.args(path, target, expected, True, dry))
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, '')
            self.assertEqual(keep.read_bytes(), b'preserve')
            self.assertEqual((existing / 'keep').read_bytes(), b'preserve')
            self.assertTrue(live.is_symlink() and dangling.is_symlink())
            for failing in ('entry-0000.json', 'entry-0000.md', 'summary.json'):
                def fail(destination, text):
                    if destination.name == failing:
                        raise OSError('injected binding write failure')
                    write_atomic(destination, text)
                with patch('mediawrench.speakers.manifest.write_atomic', side_effect=fail):
                    code, stdout, stderr = captured(self.args(path, root / 'out', expected, True))
                self.assertEqual(code, 2)
                self.assertEqual(stdout, '')
                self.assertIn('injected binding write failure', stderr)
                self.assertFalse((root / 'out').exists())


if __name__ == '__main__':
    unittest.main()
