"""Explicit diagnostics collect option errors, never input or processing outcomes."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mediawrench.speakers as bridge
from mediawrench.speakers.export import render_json
from . import test_manifest_option_validation as option_validation
from .test_manifest_validation import captured, sha


class ManifestOptionDiagnosticsTests(unittest.TestCase):
    manifest_only = option_validation.ManifestOptionValidationTests.manifest_only
    write = option_validation.ManifestOptionValidationTests.write

    def save(self, root, options):
        path = root / 'manifest.json'
        value = dict(schema_version=1, entries=[
            dict(recording=f'café-你好-{index}', channel='001', asr=f'absent/{index}/asr',
                 rttm=f'absent/{index}/rttm', aliases=f'absent/{index}/aliases',
                 exports=['markdown', 'srt'], options=option)
            for index, option in enumerate(options)])
        return path, value, self.write(path, value)

    def args(self, path):
        return ['--manifest', str(path), '--validate-manifest', '--diagnose-options', '--summary-format', 'json']

    def text(self, report):
        return (f"Manifest option diagnostics: {report['counts']['entries']} entries, "
                f"{report['counts']['invalid_option_entries']} invalid option entries; "
                'input files not read; no output written\n' +
                ''.join(error['message'] + '\n' for error in report['option_errors']))

    def test_multiple_invalid_entries_separated_by_valid_with_no_entry_io(self):
        options = [dict(min_coverage=True, max_gap=-1), dict(max_gap=0, rttm_offset_seconds=1e20),
                   dict(rttm_out_of_range_policy='clip')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, value, raw = self.save(root, options)
            with self.manifest_only(path) as opens:
                baseline = bridge.validate_manifest(path)
                with self.assertRaises(bridge.BridgeError) as first:
                    bridge.validate_manifest(path, validate_options=True)
                report = bridge.diagnose_manifest_options(path)
                self.assertEqual(report['report_type'], 'manifest_option_diagnostics')
                self.assertEqual(report['validation_scope'], 'structure_and_option_values')
                self.assertEqual(report['status'], 'options_invalid')
                self.assertEqual(report['counts'], dict(entries=3, referenced_inputs=9,
                                                       expected_inputs=0, invalid_option_entries=2))
                self.assertEqual(report['entries'], baseline['entries'])
                self.assertEqual([entry['options'] for entry in report['entries']], options)
                self.assertEqual(report['option_errors'], [
                    dict(index=0, recording='café-你好-0', channel='1', stage='options', message=str(first.exception)),
                    dict(index=2, recording='café-你好-2', channel='1', stage='options',
                         message="entries[2] (café-你好-2/1) options: recording_duration is required for rttm_out_of_range_policy='clip'")])
                for key in ('input_contents_checked', 'input_digests_checked', 'outputs_written'):
                    self.assertIs(report[key], False)
                expected = dict(baseline, report_type='manifest_option_diagnostics', status='options_invalid',
                                validation_scope='structure_and_option_values', counts=report['counts'],
                                option_errors=report['option_errors'])
                self.assertEqual(report, expected)
                code, stdout, stderr = captured(self.args(path))
                self.assertEqual((code, stdout, stderr), (1, render_json(expected), ''))
                code, stdout, stderr = captured(self.args(path) + ['--summary-format', 'text'])
                self.assertEqual((code, stdout, stderr), (1, self.text(report), ''))
                self.assertEqual(len(opens), 5)
            self.assertEqual(list(root.iterdir()), [path])
            self.assertEqual(path.read_bytes(), raw)

    def test_every_shared_rule_and_first_error_order_matches_merge(self):
        options = [option for option, _ in option_validation.invalid_options()]
        options += [dict(min_coverage=2, max_gap=-1),
                    dict(rttm_out_of_range_policy='invalid', rttm_offset_seconds='bad'),
                    dict(rttm_drift_ppm=-1000000, recording_duration=0)]
        asr = {'words': [{'word': 'yes', 'start': 0, 'end': 1}]}
        diar = bridge.parse_rttm('SPEAKER r 1 0 1 <NA> <NA> A <NA> <NA>')
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = self.save(Path(directory), options)
            with self.manifest_only(path):
                report = bridge.diagnose_manifest_options(path)
            self.assertEqual(report['counts']['invalid_option_entries'], len(options))
            for index, (option, error) in enumerate(zip(options, report['option_errors'], strict=True)):
                with self.subTest(options=option), self.assertRaises(bridge.BridgeError) as expected:
                    bridge.merge(asr, diar, **option)
                self.assertEqual(error, dict(index=index, recording=f'café-你好-{index}', channel='1',
                    stage='options', message=f'entries[{index}] (café-你好-{index}/1) options: {expected.exception}'))

    def test_valid_configuration_report_preserves_values_and_does_not_imply_processing(self):
        options = [{}, dict(recording_duration=None), dict(min_coverage=0, tie_epsilon=1, max_gap=0),
                   dict(rttm_offset_seconds=1e20),
                   dict(rttm_out_of_range_policy='clip', recording_duration=1e-300)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, value, _ = self.save(root, options)
            for entry in value['entries']:
                entry['input_sha256'] = {k: 'a' * 64 for k in ('asr', 'rttm', 'aliases')}
            raw = self.write(path, value)
            with self.manifest_only(path):
                report = bridge.diagnose_manifest_options(path, expected_manifest_sha256=sha(raw), require_input_sha256=True)
                baseline = bridge.validate_manifest(path, expected_manifest_sha256=sha(raw), require_input_sha256=True)
                self.assertEqual(report, dict(baseline, report_type='manifest_option_diagnostics',
                    status='options_valid', validation_scope='structure_and_option_values', option_errors=[],
                    counts=dict(entries=5, referenced_inputs=15, expected_inputs=15, invalid_option_entries=0)))
                self.assertEqual([entry['options'] for entry in report['entries']], options)
                code, stdout, stderr = captured(self.args(path) + ['--require-input-sha256', '--manifest-sha256', sha(raw)])
                self.assertEqual((code, stdout, stderr), (0, render_json(report), ''))
                code, stdout, stderr = captured(self.args(path) + ['--summary-format', 'text'])
                self.assertEqual((code, stdout, stderr), (0, self.text(report), ''))
            result = bridge.run_manifest(path, root / 'unused', dry_run=True)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['counts']['failed'], 5)
            self.assertFalse((root / 'unused').exists())

    def test_preflight_failures_win_before_collection_including_later_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, value, _ = self.save(root, [dict(min_coverage=True), {}, dict(max_gap=-1)])
            cases = []
            for update, message in ((dict(options={'unknown': 1}), 'unknown keys'),
                                    (dict(options={'max_gap': 1e309}), 'nonfinite'),
                                    (dict(input_sha256={'asr': 'A' * 64}), 'lowercase hex'),
                                    (dict(channel=True), 'positive integer'),
                                    (dict(exports=['json']), 'unique formats')):
                invalid = copy.deepcopy(value)
                invalid['entries'][2].update(update)
                cases.append((json.dumps(invalid).encode(), message))
            cases += [(b'\xff', 'decode'), (b'{', 'JSON'),
                      (b'{"schema_version":1,"schema_version":1}', 'duplicate')]
            for raw, message in cases:
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.manifest_only(path), \
                        patch('mediawrench.speakers.manifest._validate_merge_options') as rules:
                    for expectation, diagnostic in ((sha(raw), message), ('0' * 64, 'manifest SHA-256 mismatch')):
                        code, stdout, stderr = captured(self.args(path) + ['--manifest-sha256', expectation, '--require-input-sha256'])
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertIn(diagnostic, stderr)
                    rules.assert_not_called()
            self.write(path, value)
            with self.manifest_only(path), patch('mediawrench.speakers.manifest._validate_merge_options') as rules:
                code, stdout, stderr = captured(self.args(path) + ['--require-input-sha256'])
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('entries[0] (café-你好-0/1) input_sha256.asr:', stderr)
                with self.assertRaisesRegex(bridge.BridgeError, 'required expected SHA-256'):
                    bridge.diagnose_manifest_options(path, require_input_sha256=True)
                rules.assert_not_called()
            # Complete required coverage permits collection despite nonexistent inputs.
            for entry in value['entries']:
                entry['input_sha256'] = {k: 'b' * 64 for k in ('asr', 'rttm', 'aliases')}
            raw = self.write(path, value)
            with self.manifest_only(path):
                report = bridge.diagnose_manifest_options(path, require_input_sha256=True, expected_manifest_sha256=sha(raw))
                self.assertEqual([e['index'] for e in report['option_errors']], [0, 2])

    def test_modes_and_policy_syntax_fail_before_manifest_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = self.save(root, [{}])
            cases = [['--diagnose-options'], ['--manifest', str(path), '--diagnose-options'],
                     self.args(path) + ['--validate-options'], self.args(path) + ['true'],
                     self.args(path) + ['--diagnose-options=true']]
            for flags in (['--output-dir', str(root / 'out')], ['--dry-run'], ['--observe-input-sha256'],
                          ['--asr', 'missing'], ['--max-gap', '1'], ['--output', str(path)],
                          ['--manifest-sha256', 'A' * 64], ['--summary-format', 'yaml']):
                cases.append(self.args(path) + flags)
            with patch.object(Path, 'open') as read, patch.object(Path, 'mkdir') as mkdir:
                for args in cases:
                    code, stdout, stderr = captured(args)
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertTrue(stderr)
                for value in (None, 0, 1, 'true', [], {}):
                    with self.assertRaisesRegex(bridge.BridgeError, 'require_input_sha256: expected a boolean'):
                        bridge.diagnose_manifest_options(path, require_input_sha256=value)
                for value in (True, 1, '', 'A' * 64, 'a' * 63, '０' * 64, b'a' * 64, [], {}):
                    with self.assertRaisesRegex(bridge.BridgeError, 'expected_manifest_sha256:'):
                        bridge.diagnose_manifest_options(path, expected_manifest_sha256=value)
                read.assert_not_called()
                mkdir.assert_not_called()
            for unavailable in (root / 'missing', root):
                code, stdout, stderr = captured(self.args(unavailable))
                self.assertEqual((code, stdout), (2, ''))
                self.assertTrue(stderr)

    def test_one_snapshot_even_when_manifest_replaced_after_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, raw = self.save(Path(directory), [dict(max_gap=-1), {}, dict(min_coverage=False)])
            original, reads = Path.read_bytes, []

            def mutate(file):
                self.assertEqual(file, path)
                reads.append(file)
                data = original(file)
                file.write_bytes(b'\xffreplacement')
                return data

            with patch.object(Path, 'read_bytes', mutate):
                report = bridge.diagnose_manifest_options(path, expected_manifest_sha256=sha(raw))
            self.assertEqual(reads, [path])
            self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))
            self.assertEqual([e['index'] for e in report['option_errors']], [0, 2])


if __name__ == '__main__':
    unittest.main()
