"""Opt-in option values are checked without claiming input or merge success."""

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, merge, parse_rttm, run_manifest, validate_manifest
from mediawrench.speakers.export import render_json
from .test_manifest_validation import captured, sha


NUMERIC_KEYS = ('min_coverage', 'tie_epsilon', 'max_gap', 'rttm_offset_seconds',
                'rttm_drift_ppm', 'recording_duration')


def invalid_options():
    for key in NUMERIC_KEYS:
        for value in (True, False, '1', [], {}, 10**400):
            yield {key: value}, key
        if key != 'recording_duration':
            yield {key: None}, key
    for key in ('min_coverage', 'tie_epsilon'):
        for value in (-0.01, 1.01):
            yield {key: value}, key
    yield {'max_gap': -1}, 'max_gap'
    for value in (-1000000, -1000001):
        yield {'rttm_drift_ppm': value}, 'rttm_drift_ppm'
    for value in (0, -1):
        yield {'recording_duration': value}, 'recording_duration'
    for value in (None, True, 1, [], {}, '', 'CLIP', 'ignore'):
        yield {'rttm_out_of_range_policy': value}, 'rttm_out_of_range_policy'
    for options in ({'rttm_out_of_range_policy': 'clip'},
                    {'rttm_out_of_range_policy': 'clip', 'recording_duration': None}):
        yield options, 'recording_duration'


class ManifestOptionValidationTests(unittest.TestCase):
    def save(self, root, options=None):
        path = root / 'manifest.json'
        entries = [dict(recording='café-你好', channel='001', asr='absent/asr',
                        rttm='absent/rttm', aliases='absent/aliases', exports=['srt', 'markdown'],
                        options={} if options is None else options)]
        self.write(path, dict(schema_version=1, entries=entries))
        return path

    def write(self, path, value):
        raw = (json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\r\n') + '\r\n').encode()
        path.write_bytes(raw)
        return raw

    def args(self, path):
        return ['--manifest', str(path), '--validate-manifest', '--validate-options', '--summary-format', 'json']

    @contextmanager
    def manifest_only(self, path):
        original = Path.open
        opens = []

        def guard(file, mode='r', *args, **kwargs):
            self.assertEqual((file, mode), (path, 'rb'))
            opens.append((file, mode))
            return original(file, mode, *args, **kwargs)

        with patch.object(Path, 'open', guard), \
                patch.object(Path, 'stat', side_effect=AssertionError('entry lookup')), \
                patch('mediawrench.speakers.manifest._read_input') as read, \
                patch('mediawrench.speakers.manifest.merge') as process, \
                patch('mediawrench.speakers.manifest.write_atomic') as write, \
                patch.object(Path, 'mkdir') as mkdir:
            yield opens
            for operation in (read, process, write, mkdir):
                operation.assert_not_called()

    def test_invalid_known_option_missing_inputs_rejected_without_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, {'min_coverage': True})
            raw = path.read_bytes()
            with self.manifest_only(path) as opens:
                self.assertEqual(validate_manifest(path)['entries'][0]['options'], {'min_coverage': True})
                with self.assertRaisesRegex(BridgeError, r'entries\[0\].*café-你好/1.*options.*min_coverage'):
                    validate_manifest(path, validate_options=True)
                code, stdout, stderr = captured(self.args(path))
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('entries[0] (café-你好/1) options:', stderr)
                self.assertIn('min_coverage', stderr)
                self.assertEqual(len(opens), 3)
            self.assertEqual(list(root.iterdir()), [path])
            self.assertEqual(path.read_bytes(), raw)

    def test_all_input_independent_rules_match_merge_diagnostics(self):
        asr = {'words': [{'word': 'yes', 'start': 0, 'end': 1}]}
        diar = parse_rttm('SPEAKER r 1 0 1 <NA> <NA> A <NA> <NA>')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for options, key in invalid_options():
                with self.subTest(options=options):
                    with self.assertRaises(BridgeError) as core_error:
                        merge(asr, diar, **options)
                    path = self.save(root, options)
                    with self.manifest_only(path):
                        default = validate_manifest(path)
                        self.assertEqual(default, validate_manifest(path, validate_options=False))
                        self.assertNotIn('validation_scope', default)
                        with self.assertRaises(BridgeError) as error:
                            validate_manifest(path, validate_options=True)
                        self.assertEqual(str(error.exception),
                            'entries[0] (café-你好/1) options: ' + str(core_error.exception))
                        code, stdout, stderr = captured(self.args(path))
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertIn(key, stderr)
                        self.assertNotIn('Traceback', stderr)

    def test_success_scope_preserves_raw_options_and_default_report(self):
        good = [{}, {'min_coverage': 0, 'tie_epsilon': 1, 'max_gap': 0},
                {'min_coverage': 1, 'tie_epsilon': 0, 'recording_duration': None},
                {'rttm_offset_seconds': -1e308, 'rttm_drift_ppm': -999999.9999999999},
                {'rttm_offset_seconds': 1e20, 'rttm_drift_ppm': 1e308},
                {'rttm_out_of_range_policy': 'clip', 'recording_duration': 1e-300}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for options in good:
                path = self.save(root, options)
                raw = path.read_bytes()
                with self.subTest(options=options), self.manifest_only(path) as opens:
                    baseline = validate_manifest(path)
                    report = validate_manifest(path, validate_options=True, expected_manifest_sha256=sha(raw))
                    expected = dict(baseline, expected_manifest_sha256=sha(raw),
                                    validation_scope='structure_and_option_values')
                    self.assertEqual(report, expected)
                    self.assertEqual(report['entries'][0]['options'], options)
                    self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))
                    code, stdout, stderr = captured(self.args(path) + ['--manifest-sha256', sha(raw)])
                    self.assertEqual((code, stderr), (0, ''))
                    self.assertEqual(stdout, render_json(expected))
                    code, stdout, stderr = captured(self.args(path) + ['--summary-format', 'text'])
                    self.assertEqual((code, stdout, stderr), (0,
                        'Manifest validated: 1 entries, 0/3 input expectations; merge option values checked; '
                        'input files not read; no output written\n', ''))
                    self.assertEqual(len(opens), 4)
            self.assertEqual(list(root.iterdir()), [path])
            # Config success does not imply corrected rows, content or merge success.
            with self.assertRaisesRegex(BridgeError, 'collapsed'):
                merge({'words': []}, parse_rttm('SPEAKER r 1 0 1 <NA> <NA> A <NA> <NA>'),
                      rttm_offset_seconds=1e20)
            processed = run_manifest(path, root / 'unused', dry_run=True)
            self.assertEqual(processed['entries'][0]['error']['stage'], 'asr')
            self.assertFalse((root / 'unused').exists())

    def test_precedence_schema_coverage_then_semantics_across_all_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, {'min_coverage': True})
            value = json.loads(path.read_bytes())
            second = dict(recording='other', channel=2, asr='absent/asr2', rttm='absent/rttm2')
            value['entries'].append(second)
            malformed = copy.deepcopy(value)
            malformed['entries'][1]['options'] = {'unknown': 1}
            cases = [(malformed, [], 'unknown keys'),
                     (value, ['--require-input-sha256'], 'input_sha256.asr'),
                     (value, [], 'entries[0] (café-你好/1) options:')]
            complete = copy.deepcopy(value)
            for entry in complete['entries']:
                entry['input_sha256'] = {k: 'a' * 64 for k in ('asr', 'rttm', 'aliases') if k in entry}
            complete['entries'][0]['options'] = {}
            complete['entries'][1]['options'] = {'rttm_out_of_range_policy': 'clip'}
            cases.append((complete, ['--require-input-sha256'], 'entries[1] (other/2) options:'))
            for data, flags, diagnostic in cases:
                raw = self.write(path, data)
                with self.manifest_only(path):
                    code, stdout, stderr = captured(self.args(path) + flags + ['--manifest-sha256', sha(raw)])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn(diagnostic, stderr)
                    code, stdout, stderr = captured(self.args(path) + flags + ['--manifest-sha256', '0' * 64])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn('manifest SHA-256 mismatch', stderr)
            # Nonfinite JSON is structural and still wins over required coverage.
            for key in NUMERIC_KEYS:
                for value in (float('nan'), float('inf'), -float('inf')):
                    path = self.save(root, {key: value})
                    with self.manifest_only(path):
                        code, stdout, stderr = captured(self.args(path) + ['--require-input-sha256'])
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertIn('nonfinite', stderr)
                        self.assertNotIn('required expected', stderr)

    def test_strict_boolean_and_cli_mode_conflicts_before_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root)
            cases = [['--validate-options'], ['--manifest', str(path), '--validate-options', '--output-dir', str(root / 'out')],
                     self.args(path) + ['false'], self.args(path) + ['--validate-options=true']]
            for flags in (['--output-dir', str(root / 'out')], ['--dry-run'], ['--observe-input-sha256'],
                          ['--asr', 'absent'], ['--min-coverage', '1']):
                cases.append(self.args(path) + flags)
            with patch.object(Path, 'open') as read, patch.object(Path, 'mkdir') as mkdir:
                for value in (None, 0, 1, 'true', [], {}):
                    with self.assertRaisesRegex(BridgeError, 'validate_options: expected a boolean'):
                        validate_manifest(path, validate_options=value)
                for args in cases:
                    with self.subTest(args=args):
                        code, stdout, stderr = captured(args)
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertTrue(stderr)
                read.assert_not_called()
                mkdir.assert_not_called()

    def test_single_snapshot_option_checks_use_parsed_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, {'max_gap': 0})
            raw = path.read_bytes()
            original = Path.read_bytes
            reads = []

            def mutate(file):
                self.assertEqual(file, path)
                reads.append(file)
                data = original(file)
                file.write_bytes(b'invalid replacement')
                return data

            with patch.object(Path, 'read_bytes', mutate):
                report = validate_manifest(path, validate_options=True, expected_manifest_sha256=sha(raw))
            self.assertEqual(reads, [path])
            self.assertEqual(report['entries'][0]['options'], {'max_gap': 0})
            self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))


if __name__ == '__main__':
    unittest.main()
