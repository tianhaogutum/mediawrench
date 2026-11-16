"""Manifest-only validation reports structure/expectations, never input success."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mediawrench.speakers as bridge
import mediawrench.speakers.manifest as manifest
from mediawrench.speakers.cli import main
from mediawrench.speakers.export import render_json


def sha(data):
    return hashlib.sha256(data).hexdigest()


def captured(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


class ManifestValidationTests(unittest.TestCase):
    def fixture(self, root):
        entries = [dict(recording='café-你好', channel='001', asr='absent/asr.json',
                        rttm='absent/input.rttm', aliases='absent/aliases.json',
                        input_sha256=dict(asr='a' * 64), exports=['markdown', 'srt'],
                        options=dict(recording_duration=2)),
                   dict(recording='other', channel=2, asr='other/asr', rttm='other/rttm')]
        path = root / 'manifest.json'
        self.save(path, dict(schema_version=1, entries=entries))
        return path

    def save(self, path, value):
        raw = (json.dumps(value, ensure_ascii=False, indent=2).replace('\n', '\r\n') + '\r\n').encode()
        path.write_bytes(raw)
        return raw

    def args(self, path):
        return ['--validate-manifest', '--manifest', str(path), '--summary-format', 'json']

    def test_missing_inputs_validate_without_output_or_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            keep = root / 'keep'
            keep.write_bytes(b'preserve')
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            opens = []
            original = Path.open

            def guard(file, mode='r', *args, **kwargs):
                opens.append((file, mode))
                self.assertEqual((file, mode), (path, 'rb'))
                return original(file, mode, *args, **kwargs)

            with patch.object(Path, 'open', guard), \
                    patch('mediawrench.speakers.manifest._read_input') as read, \
                    patch('mediawrench.speakers.manifest.merge') as merge, \
                    patch('mediawrench.speakers.manifest.write_atomic') as write, \
                    patch.object(Path, 'mkdir') as mkdir:
                report = bridge.validate_manifest(path)
                code, stdout, stderr = captured(self.args(path))
                self.assertEqual((code, stderr), (0, ''))
                self.assertEqual(stdout, render_json(report))
                self.assertEqual(opens, [(path, 'rb'), (path, 'rb')])
                for operation in (read, merge, write, mkdir):
                    operation.assert_not_called()
            self.assertEqual({p: p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
            self.assertEqual(report, dict(
                schema_version=1, report_type='manifest_validation', status='validated',
                manifest=str(path), manifest_snapshot=dict(sha256=sha(before[path]), size_bytes=len(before[path])),
                expected_manifest_sha256=None, require_input_sha256=False,
                input_contents_checked=False, input_digests_checked=False, outputs_written=False,
                counts=dict(entries=2, referenced_inputs=5, expected_inputs=1), entries=[
                    dict(index=0, recording='café-你好', channel='1', asr=str(root / 'absent/asr.json'),
                         rttm=str(root / 'absent/input.rttm'), aliases=str(root / 'absent/aliases.json'),
                         input_sha256=dict(asr='a' * 64), exports=['markdown', 'srt'],
                         options=dict(recording_duration=2)),
                    dict(index=1, recording='other', channel='2', asr=str(root / 'other/asr'),
                         rttm=str(root / 'other/rttm'), options={})]))
            for suffix in ([], ['--summary-format', 'text']):
                code, stdout, stderr = captured(['--validate-manifest', '--manifest', str(path)] + suffix)
                self.assertEqual((code, stdout, stderr), (0,
                    'Manifest validated: 2 entries, 1/5 input expectations; input files not read; no output written\n', ''))

    def test_one_raw_snapshot_supplies_hash_check_parse_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            value = json.loads(path.read_bytes())
            for entry in value['entries']:
                entry['input_sha256'] = {key: 'b' * 64 for key in ('asr', 'rttm', 'aliases') if key in entry}
            raw = self.save(path, value)
            self.assertIn(b'\r\n', raw)
            self.assertGreater(len(raw), len(raw.decode()))
            self.assertNotEqual(sha(raw), sha(raw.replace(b'\r\n', b'\n')))
            read_bytes = Path.read_bytes
            reads = []

            def mutate(file):
                self.assertEqual(file, path)
                reads.append(file)
                data = read_bytes(file)
                file.write_bytes(b'\xffreplacement')
                return data

            with patch.object(Path, 'read_bytes', mutate), patch.object(manifest, '_entries', wraps=manifest._entries) as parse:
                report = bridge.validate_manifest(path, expected_manifest_sha256=sha(raw), require_input_sha256=True)
            self.assertEqual(reads, [path])
            parse.assert_called_once_with(path, raw)
            self.assertEqual(path.read_bytes(), b'\xffreplacement')
            self.assertEqual(report['manifest_snapshot'], dict(sha256=sha(raw), size_bytes=len(raw)))
            self.assertEqual(report['expected_manifest_sha256'], sha(raw))
            self.assertTrue(report['require_input_sha256'])
            self.assertEqual(report['counts'], dict(entries=2, referenced_inputs=5, expected_inputs=5))
            self.assertFalse(report['input_digests_checked'])

    def test_coverage_optional_or_complete_and_contextual_required_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            baseline = json.loads(path.read_bytes())
            for entry in baseline['entries']:
                entry['input_sha256'] = {key: 'b' * 64 for key in ('asr', 'rttm', 'aliases') if key in entry}
            for index, key in ((0, 'asr'), (0, 'rttm'), (0, 'aliases'), (1, 'asr'), (1, 'rttm')):
                for missing in ('key', 'empty', 'omitted'):
                    value = copy.deepcopy(baseline)
                    entry = value['entries'][index]
                    if missing == 'key':
                        del entry['input_sha256'][key]
                    elif missing == 'empty':
                        entry['input_sha256'] = {}
                    else:
                        del entry['input_sha256']
                    raw = self.save(path, value)
                    first = key if missing == 'key' else 'asr'
                    with self.subTest(index=index, key=key, missing=missing), \
                            patch.object(manifest, '_read_input') as read:
                        report = bridge.validate_manifest(path)
                        self.assertLess(report['counts']['expected_inputs'], 5)
                        with self.assertRaisesRegex(bridge.BridgeError, f'input_sha256.{first}'):
                            bridge.validate_manifest(path, require_input_sha256=True)
                        code, stdout, stderr = captured(self.args(path) + [
                            '--require-input-sha256', '--manifest-sha256', sha(raw)])
                        self.assertEqual((code, stdout), (2, ''))
                        for context in (f'entries[{index}]', entry['recording'],
                                        f'input_sha256.{first}', str(root / entry[first])):
                            self.assertIn(context, stderr)
                        read.assert_not_called()
            self.save(path, baseline)
            report = bridge.validate_manifest(path, require_input_sha256=True)
            self.assertEqual(report['counts']['expected_inputs'], 5)
            self.assertNotIn('aliases', report['entries'][1])

    def test_malformed_snapshot_and_mismatch_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            good = path.read_bytes()
            value = json.loads(good)
            malformed = [b'\xff', b'', b'{', b'\xef\xbb\xbf' + good,
                         b'{"schema_version":1,"schema_version":1,"entries":[]}',
                         b'{"schema_version":1,"entries":[],"extra":NaN}']
            structures = [None, [], {}, dict(value, schema_version=True),
                          dict(value, entries=[]), dict(value, extra=1)]
            for update in (dict(channel=True), dict(channel='１'), dict(asr=''), dict(rttm='\x00'),
                           dict(exports=['srt', 'srt']), dict(exports=['json']),
                           dict(options=dict(max_gap=float('inf'))), dict(options=dict(unknown=1)),
                           dict(input_sha256=dict(asr='A' * 64)), dict(input_sha256=dict(asr=False)),
                           dict(input_sha256=dict(extra='a' * 64))):
                case = copy.deepcopy(value)
                case['entries'][0].update(update)
                structures.append(case)
            case = copy.deepcopy(value)
            case['entries'][1] = copy.deepcopy(case['entries'][0])
            structures.append(case)
            case = copy.deepcopy(value)
            del case['entries'][0]['aliases']
            case['entries'][0]['input_sha256']['aliases'] = 'a' * 64
            structures.append(case)
            malformed += [json.dumps(v).encode() for v in structures]
            for raw in malformed:
                path.write_bytes(raw)
                with self.subTest(raw=raw), patch.object(manifest, '_entries', wraps=manifest._entries) as parse, \
                        patch.object(manifest, '_read_input') as read:
                    code, stdout, stderr = captured(self.args(path) + [
                        '--manifest-sha256', sha(good), '--require-input-sha256'])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn('manifest SHA-256 mismatch', stderr)
                    for detail in (str(path), sha(good), sha(raw)):
                        self.assertIn(detail, stderr)
                    parse.assert_not_called()
                    for expectation in ([], ['--manifest-sha256', sha(raw)]):
                        code, stdout, stderr = captured(self.args(path) + expectation + ['--require-input-sha256'])
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertNotIn('SHA-256 mismatch', stderr)
                        self.assertNotIn('required expected SHA-256', stderr)
                    read.assert_not_called()
            self.assertEqual(list(root.iterdir()), [path])

    def test_modes_and_policy_syntax_rejected_before_any_reads_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            keep = root / 'existing'
            keep.mkdir()
            (keep / 'sentinel').write_bytes(b'preserve')
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            conflicts = [['--dry-run'], ['--observe-input-sha256'], ['--observe-input-sha256', 'false'],
                         ['--summary-format', 'yaml'], ['--validate-manifest=true'],
                         ['--asr', 'missing'], ['--rttm', 'missing'], ['--recording', 'r'],
                         ['--channel', '1'], ['--aliases', 'missing'], ['--format', 'json'],
                         ['--output', str(root / 'new')], ['--output', str(keep / 'sentinel')]]
            for key in manifest.OPTIONS:
                conflicts.append(['--' + key.replace('_', '-'), 'error' if key == 'rttm_out_of_range_policy' else '1'])
            for output in (keep, root / 'new', root / 'missing-parent' / 'out'):
                conflicts.append(['--output-dir', str(output)])
            cases = [self.args(path) + flags for flags in conflicts]
            cases += [['--validate-manifest'], ['--validate-manifest', '--asr', 'missing', '--rttm', 'missing'],
                      ['--manifest', str(path)], self.args(path) + ['--manifest-sha256']]
            invalid_hashes = ['', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64,
                              '０' * 64, 'a' * 63 + '\n', ' ' + 'a' * 64]
            with patch.object(Path, 'read_bytes') as read, patch.object(Path, 'read_text') as text, \
                    patch.object(Path, 'mkdir') as mkdir, patch.object(manifest, 'write_atomic') as write:
                for flags in cases + [self.args(path) + ['--manifest-sha256', v] for v in invalid_hashes]:
                    with self.subTest(flags=flags):
                        code, stdout, stderr = captured(flags)
                        self.assertEqual((code, stdout), (2, ''))
                        self.assertTrue(stderr)
                for value in invalid_hashes + [False, True, 1, b'a' * 64, [], {}]:
                    with self.assertRaisesRegex(bridge.BridgeError, 'expected_manifest_sha256'):
                        bridge.validate_manifest(path, expected_manifest_sha256=value)
                for value in (None, 0, 1, 'true', [], {}):
                    with self.assertRaisesRegex(bridge.BridgeError, 'require_input_sha256.*boolean'):
                        bridge.validate_manifest(path, require_input_sha256=value)
                for operation in (read, text, mkdir, write):
                    operation.assert_not_called()
            self.assertEqual({p: p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
            self.assertFalse((root / 'new').exists())
            self.assertFalse((root / 'missing-parent').exists())

    def test_manifest_io_failure_and_structure_only_option_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root / 'missing', root):
                code, stdout, stderr = captured(self.args(path))
                self.assertEqual((code, stdout), (2, ''))
                self.assertTrue(stderr)
            path = self.fixture(root)
            value = json.loads(path.read_bytes())
            value['entries'][0]['options'] = dict(min_coverage='not a merge value')
            self.save(path, value)
            self.assertEqual(bridge.validate_manifest(path)['entries'][0]['options'],
                             dict(min_coverage='not a merge value'))
            # Validation success must never be confused with processing success.
            processed = bridge.run_manifest(path, root / 'unused', dry_run=True)
            self.assertEqual(processed['status'], 'failed')
            self.assertEqual(processed['counts']['failed'], 2)
            self.assertFalse((root / 'unused').exists())


if __name__ == '__main__':
    unittest.main()
