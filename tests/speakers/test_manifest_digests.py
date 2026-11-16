"""Optional input digests bind bytes before parsing and retain export skip counts."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, run_manifest
from . import test_manifest_export_counts as export_counts

ROOT = Path(__file__).resolve().parents[2]


class ManifestDigestTests(unittest.TestCase):
    def fixture(self, root):
        entries = export_counts.ManifestExportCountTests().entries()
        for index, entry in enumerate(entries):
            # Keep the fixture bindings relative to the manifest, including aliases.
            entry['aliases'] = str(root / f'aliases-{index}.json')
            Path(entry['aliases']).write_bytes(b'{}\r\n')
            for key in ('asr', 'aliases', 'rttm'):
                source = Path(entry[key])
                local = root / f'{key}-{index}{source.suffix}'
                local.write_bytes(source.read_bytes())
                entry[key] = local.name
            entry['input_sha256'] = {
                key: hashlib.sha256((root / entry[key]).read_bytes()).hexdigest()
                for key in ('asr', 'aliases', 'rttm')}
        return entries

    def save(self, root, entries):
        path = root / 'manifest.json'
        path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
        return path

    def cli(self, path, output, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.speakers', '--manifest',
                               str(path), '--output-dir', str(output), '--summary-format', 'json', *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def check_run(self, root, entries, code):
        path = self.save(root, entries)
        output = root / 'out'
        before = sorted(root.rglob('*'))
        dry = self.cli(path, output, '--dry-run')
        self.assertEqual(dry.returncode, code, dry.stderr)
        self.assertEqual(sorted(root.rglob('*')), before)
        planned = json.loads(dry.stdout)
        self.assertTrue(planned['dry_run'])
        self.assertEqual(planned, run_manifest(path, output, dry_run=True))
        real = self.cli(path, output)
        self.assertEqual(real.returncode, code, real.stderr)
        summary = json.loads(real.stdout)
        planned['dry_run'] = False
        self.assertEqual(summary, planned)
        self.assertEqual(dry.stderr, real.stderr)
        self.assertEqual(summary, json.loads((output / 'summary.json').read_text()))
        self.assertEqual(summary, run_manifest(path, root / 'library'))
        return summary, real

    def test_matching_digests_preserve_overflow_clipping_and_all_result_bytes(self):
        for order in (['srt', 'markdown'], ['markdown', 'srt']):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.fixture(root)
                entries[0]['exports'] = entries[1]['exports'] = order
                summary, proc = self.check_run(root, entries, 1)
                self.assertEqual(summary['counts'], dict(total=3, succeeded=2, failed=1, items=6, turns=6))
                self.assertEqual(summary['export_counts'], {
                    'srt': dict(requested=2, succeeded=1, failed=1, skipped=0),
                    'markdown': dict(requested=3, succeeded=3, failed=0, skipped=0)})
                self.assertIn('99:59:59.999', proc.stderr)
                self.assertEqual(proc.stderr.count('warning:'), 4)
                baseline_entries = copy.deepcopy(entries)
                for original, entry in zip(entries, summary['entries']):
                    self.assertEqual(entry['input_sha256'], original['input_sha256'])
                for entry in baseline_entries:
                    entry.pop('input_sha256')
                path = self.save(root, baseline_entries)
                baseline = run_manifest(path, root / 'baseline')
                for entry in baseline['entries']:
                    self.assertNotIn('input_sha256', entry)
                expected = copy.deepcopy(summary)
                for entry in expected['entries']:
                    entry.pop('input_sha256')
                self.assertEqual(baseline, expected)
                for file in (root / 'out').iterdir():
                    if file.name != 'summary.json':
                        self.assertEqual(file.read_bytes(), (root / 'baseline' / file.name).read_bytes())

    def test_same_path_byte_mutation_skips_only_that_entry_in_both_orders(self):
        for key in ('asr', 'aliases', 'rttm'):
            for order in (['srt', 'markdown'], ['markdown', 'srt']):
                with self.subTest(key=key, order=order), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    entries = self.fixture(root)[1:]
                    entries[0]['exports'] = order
                    source = root / entries[0][key]
                    # Valid content after mutation: digest must check bytes, not parsed values.
                    source.write_bytes(source.read_bytes() + b'\n')
                    summary, proc = self.check_run(root, entries, 1)
                    self.assertEqual(summary['status'], 'partial')
                    self.assertTrue(summary['partial_results'])
                    self.assertEqual(summary['counts'], dict(total=2, succeeded=1, failed=1, items=1, turns=1))
                    self.assertEqual(summary['export_counts'], {
                        'srt': dict(requested=1, succeeded=0, failed=0, skipped=1),
                        'markdown': dict(requested=2, succeeded=1, failed=0, skipped=1)})
                    entry = summary['entries'][0]
                    self.assertIsNone(entry['result'])
                    self.assertEqual(entry['status'], 'error')
                    self.assertEqual(entry['error']['stage'], key)
                    self.assertNotIn('export_errors', entry)
                    self.assertTrue(all(value == dict(status='skipped', file=None) for value in entry['exports'].values()))
                    self.assertIn(f'entries[0] (clipping/1) {key}:', proc.stderr)
                    self.assertIn(str(source), proc.stderr)
                    self.assertIn(entries[0]['input_sha256'][key], proc.stderr)
                    self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), proc.stderr)
                    self.assertEqual(proc.stderr.count('error:'), 1)
                    self.assertEqual(sorted(p.name for p in (root / 'out').iterdir()),
                                     ['entry-0001.json', 'entry-0001.md', 'summary.json'])

    def test_mismatch_precedes_decode_and_all_failed_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.fixture(root)[1:]
            for entry in entries:
                entry['exports'] = ['srt', 'markdown']
                (root / entry['asr']).write_bytes(b'\xffnot-json')
            summary, proc = self.check_run(root, entries, 1)
            self.assertEqual(summary['status'], 'failed')
            self.assertFalse(summary['partial_results'])
            self.assertEqual(summary['counts'], dict(total=2, succeeded=0, failed=2, items=0, turns=0))
            self.assertEqual(summary['export_counts'], {
                key: dict(requested=2, succeeded=0, failed=0, skipped=2) for key in ('srt', 'markdown')})
            self.assertEqual(proc.stderr.count('SHA-256 mismatch'), 2)
            self.assertEqual(sorted(p.name for p in (root / 'out').iterdir()), ['summary.json'])
            # Matching invalid UTF-8 still follows the ordinary decoding-error path.
            for entry in entries:
                entry['input_sha256']['asr'] = hashlib.sha256(b'\xffnot-json').hexdigest()
            path = self.save(root, entries)
            decoded = run_manifest(path, root / 'decode', dry_run=True)
            self.assertTrue(all(e['error']['stage'] == 'asr' and 'SHA-256 mismatch' not in e['error']['message']
                                for e in decoded['entries']))

    def test_preflight_rejects_invalid_expectations_before_any_entry_reads(self):
        invalid = [None, [], 'hash', {'json': 'a' * 64}, {'aliases': 'a' * 64}]
        invalid += [{'asr': value} for value in (None, True, 1, [], {}, '', 'a' * 63, 'a' * 65,
                                                'A' * 64, 'g' * 64, 'é' * 64, ' ' + 'a' * 63)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.fixture(root)[1:]
            entries[1].pop('aliases')
            for value in invalid:
                with self.subTest(value=value):
                    entries[1]['input_sha256'] = value
                    path = self.save(root, entries)
                    for dry in (False, True):
                        with patch('mediawrench.speakers.manifest._read_input') as read_input:
                            with self.assertRaisesRegex(BridgeError, r'entries\[1\].input_sha256'):
                                run_manifest(path, root / 'out', dry_run=dry)
                            read_input.assert_not_called()
                        proc = self.cli(path, root / 'out', *(['--dry-run'] if dry else []))
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertEqual(proc.stdout, '')
                        self.assertFalse((root / 'out').exists())

    def test_checks_and_parses_the_same_single_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.fixture(root)[2]
            path = self.save(root, [entry])
            reads = []
            read_bytes = Path.read_bytes

            def mutate_after_read(file):
                data = read_bytes(file)
                reads.append(file)
                file.write_bytes(b'invalid replacement')
                return data

            with patch.object(Path, 'read_bytes', mutate_after_read):
                summary = run_manifest(path, root / 'out', dry_run=True)
            self.assertEqual(summary['status'], 'complete')
            self.assertEqual(reads, [path] + [root / entry[key] for key in ('asr', 'aliases', 'rttm')])

    def test_partial_and_empty_expectations_and_read_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.fixture(root)[1:]
            entries[0]['input_sha256'] = {}
            entries[1]['input_sha256'] = {'asr': entries[1]['input_sha256']['asr']}
            summary, _ = self.check_run(root, entries, 0)
            self.assertEqual(summary['status'], 'complete')
            self.assertEqual(summary['entries'][0]['input_sha256'], {})
            for key in ('asr', 'aliases', 'rttm'):
                with self.subTest(key=key):
                    entry = copy.deepcopy(entries[1])
                    entry['input_sha256'] = {key: 'a' * 64}
                    entry[key] = 'absent'
                    path = self.save(root, [entry])
                    failed = run_manifest(path, root / 'absent-out', dry_run=True)
                    self.assertEqual(failed['entries'][0]['error']['stage'], key)
                    self.assertNotIn('SHA-256 mismatch', failed['entries'][0]['error']['message'])


if __name__ == '__main__':
    unittest.main()
