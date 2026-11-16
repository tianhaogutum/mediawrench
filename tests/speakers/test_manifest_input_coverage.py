"""Coverage enumerates supplied expectations, never input or processing validity."""

import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mediawrench.speakers as bridge
from mediawrench.speakers.export import render_json
from . import test_manifest_option_validation as option_validation
from .test_manifest_validation import captured, sha


class ManifestInputCoverageTests(unittest.TestCase):
    manifest_only = option_validation.ManifestOptionValidationTests.manifest_only
    write = option_validation.ManifestOptionValidationTests.write

    def fixture(self, root):
        value = dict(schema_version=1, entries=[
            dict(recording=f'café-你好-{i}', channel='001', asr='absent/shared',
                 rttm=f'absent/{i}/rttm', options=dict(min_coverage=True),
                 exports=['markdown', 'srt']) for i in range(5)])
        for i in (0, 3, 4):
            value['entries'][i]['aliases'] = 'absent/shared'
        for i in (1, 4):
            value['entries'][i]['input_sha256'] = {k: 'a' * 64 for k in ('aliases', 'rttm', 'asr')
                                                  if k in value['entries'][i]}
        value['entries'][2]['input_sha256'] = {}
        value['entries'][3]['input_sha256'] = dict(asr='b' * 64)
        path = root / 'manifest.json'
        return path, value, self.write(path, value)

    def args(self, path):
        return ['--manifest', str(path), '--validate-manifest', '--diagnose-input-coverage',
                '--summary-format', 'json']

    def test_all_missing_slots_across_entries_without_entry_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, value, raw = self.fixture(root)
            with self.manifest_only(path) as opens, \
                    patch('mediawrench.speakers.manifest._validate_merge_options') as rules:
                baseline = bridge.validate_manifest(path)
                report = bridge.diagnose_manifest_input_coverage(path)
                slots = [(0, 'asr'), (0, 'rttm'), (0, 'aliases'), (2, 'asr'), (2, 'rttm'),
                         (3, 'rttm'), (3, 'aliases')]
                missing = [dict(index=i, recording=f'café-你好-{i}', channel='1', input=k,
                                path=str(root / value['entries'][i][k])) for i, k in slots]
                expected = dict(baseline, report_type='manifest_input_coverage',
                    validation_scope='expected_input_coverage', status='coverage_incomplete',
                    option_values_checked=False, missing_input_sha256=missing,
                    counts=dict(entries=5, referenced_inputs=13, expected_inputs=6, missing_inputs=7))
                expected.pop('require_input_sha256')
                self.assertEqual(report, expected)
                self.assertEqual([e['options'] for e in report['entries']], [e['options'] for e in value['entries']])
                self.assertEqual(captured(self.args(path)), (1, render_json(expected), ''))
                text = ('Manifest input coverage: 5 entries, 6/13 input expectations, 7 missing; '
                        'coverage only; input files not read; no output written\n' + ''.join(
                        f"entries[{e['index']}] ({e['recording']}/{e['channel']}) input_sha256.{e['input']}: "
                        f"missing expected SHA-256 for {e['path']}\n" for e in missing))
                self.assertEqual(captured(self.args(path) + ['--summary-format', 'text']), (1, text, ''))
                rules.assert_not_called()
                self.assertEqual(len(opens), 4)
            self.assertEqual(list(root.iterdir()), [path])
            self.assertEqual(path.read_bytes(), raw)

    def test_every_subset_and_optional_aliases_has_exact_slot_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            for aliases in (False, True):
                keys = ['asr', 'rttm'] + (['aliases'] if aliases else [])
                entry = dict(recording='r', channel=1, **{k: '/absent/shared' for k in keys})
                for length in range(len(keys) + 1):
                    for supplied in itertools.combinations(keys, length):
                        value = dict(entry, input_sha256={k: 'a' * 64 for k in reversed(supplied)})
                        self.write(path, dict(schema_version=1, entries=[value]))
                        with self.manifest_only(path):
                            report = bridge.diagnose_manifest_input_coverage(path)
                        self.assertEqual([e['input'] for e in report['missing_input_sha256']],
                                         [k for k in keys if k not in supplied])
                        self.assertEqual(report['entries'][0]['input_sha256'], value['input_sha256'])
                        self.assertEqual(report['counts']['missing_inputs'], len(keys) - length)
                        self.assertEqual(report['status'], 'coverage_complete' if length == len(keys)
                                         else 'coverage_incomplete')

    def test_complete_coverage_does_not_check_options_files_or_digest_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, value, _ = self.fixture(root)
            for entry in value['entries']:
                entry['input_sha256'] = {k: '0' * 64 for k in ('asr', 'rttm', 'aliases') if k in entry}
            raw = self.write(path, value)
            with self.manifest_only(path), patch('mediawrench.speakers.manifest._validate_merge_options') as rules:
                report = bridge.diagnose_manifest_input_coverage(path, expected_manifest_sha256=sha(raw))
                self.assertEqual((report['status'], report['missing_input_sha256']), ('coverage_complete', []))
                self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))
                for key in ('input_contents_checked', 'input_digests_checked', 'option_values_checked', 'outputs_written'):
                    self.assertIs(report[key], False)
                self.assertEqual(captured(self.args(path) + ['--manifest-sha256', sha(raw)]),
                                 (0, render_json(report), ''))
                rules.assert_not_called()
            # Coverage is complete despite absent files and invalid known options.
            diagnostic = bridge.diagnose_manifest_options(path, require_input_sha256=True)
            self.assertEqual(diagnostic['counts']['invalid_option_entries'], 5)
            result = bridge.run_manifest(path, root / 'unused', dry_run=True, require_input_sha256=True)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse((root / 'unused').exists())

    def test_full_schema_hash_and_digest_syntax_precede_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, _ = self.fixture(Path(directory))
            for update, message in ((dict(input_sha256={'rttm': 'A' * 64}), 'lowercase hex'),
                    (dict(input_sha256={'alien': 'a' * 64}), 'unknown keys'),
                    (dict(options={'unknown': 1}), 'unknown keys'),
                    (dict(options={'max_gap': 1e309}), 'nonfinite'),
                    (dict(channel=True), 'positive integer'),
                    (dict(exports=['json']), 'unique formats'),
                    (dict(asr=''), 'nonempty file path')):
                invalid = copy.deepcopy(value)
                invalid['entries'][4].update(update)
                raw = self.write(path, invalid)
                with self.subTest(update=update), self.manifest_only(path):
                    self.assertEqual(captured(self.args(path) + ['--manifest-sha256', '0' * 64])[0:2], (2, ''))
                    code, stdout, stderr = captured(self.args(path) + ['--manifest-sha256', sha(raw)])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn(message, stderr)
            for raw, message in ((b'\xff', 'decode'), (b'{', 'JSON'),
                    (b'{"schema_version":1,"schema_version":1}', 'duplicate'),
                    (b'{"schema_version":1,"entries":[]}', 'nonempty array'),
                    (b'{"schema_version":1,"entries":[{"recording":"r","channel":1,"asr":"a","rttm":"r","input_sha256":{"aliases":"' + b'a' * 64 + b'"}}]}', 'requires a aliases path')):
                path.write_bytes(raw)
                with self.manifest_only(path):
                    code, stdout, stderr = captured(self.args(path) + ['--manifest-sha256', '0' * 64])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn('manifest SHA-256 mismatch', stderr)
                    code, stdout, stderr = captured(self.args(path) + ['--manifest-sha256', sha(raw)])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn(message, stderr)

    def test_conflicts_and_hash_types_before_reads_and_old_required_policy_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = self.fixture(Path(directory))
            cases = [['--diagnose-input-coverage'], ['--manifest', str(path), '--diagnose-input-coverage']]
            for flags in (['--require-input-sha256'], ['--validate-options'], ['--diagnose-options'],
                    ['--dry-run'], ['--observe-input-sha256'], ['--output-dir', 'out'], ['--asr', 'a'],
                    ['--output', 'out'], ['--max-gap', '1'], ['--summary-format', 'yaml'],
                    ['--manifest-sha256', 'A' * 64], ['--diagnose-input-coverage=true']):
                cases.append(self.args(path) + flags)
            with patch.object(Path, 'open') as read, patch.object(Path, 'mkdir') as mkdir:
                for args in cases:
                    with self.subTest(args=args):
                        code, stdout, stderr = captured(args)
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertTrue(stderr)
                for value in (True, 1, '', 'A' * 64, 'a' * 63, '０' * 64, b'a' * 64, [], {}):
                    with self.assertRaisesRegex(bridge.BridgeError, 'expected_manifest_sha256:'):
                        bridge.diagnose_manifest_input_coverage(path, expected_manifest_sha256=value)
                with self.assertRaises(TypeError):
                    bridge.diagnose_manifest_input_coverage(path, require_input_sha256=True)
                read.assert_not_called()
                mkdir.assert_not_called()
            with self.manifest_only(path):
                for function in (bridge.validate_manifest, bridge.diagnose_manifest_options):
                    with self.assertRaisesRegex(bridge.BridgeError,
                            r'entries\[0\] \(café-你好-0/1\) input_sha256.asr: required expected SHA-256'):
                        function(path, require_input_sha256=True)
            for unavailable in (Path(directory) / 'missing', Path(directory)):
                code, stdout, stderr = captured(self.args(unavailable))
                self.assertEqual((code, stdout), (2, ''))
                self.assertTrue(stderr)

    def test_one_manifest_snapshot_even_after_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, raw = self.fixture(Path(directory))
            original, reads = Path.read_bytes, []
            def mutate(file):
                self.assertEqual(file, path)
                reads.append(file)
                data = original(file)
                file.write_bytes(b'\xffreplacement')
                return data
            with patch.object(Path, 'read_bytes', mutate):
                report = bridge.diagnose_manifest_input_coverage(path, expected_manifest_sha256=sha(raw))
            self.assertEqual(reads, [path])
            self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))
            self.assertEqual(report['counts']['missing_inputs'], 7)


if __name__ == '__main__':
    unittest.main()
