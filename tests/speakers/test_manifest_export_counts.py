"""Aggregate export outcomes must match the saved and machine-readable plan."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.speakers import run_manifest

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / 'examples' / 'speakers' / 'batch'


class ManifestExportCountTests(unittest.TestCase):
    def entries(self):
        entries = json.loads((BATCH / 'manifest-presentations.json').read_text())['entries']
        for entry in entries:
            for key in ('asr', 'rttm'):
                entry[key] = str(BATCH / entry[key])
        return entries

    def check_run(self, root, entries, expected, counts, status, code):
        path = root / 'manifest.json'
        path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
        output = root / 'out'
        command = [sys.executable, '-m', 'mediawrench.speakers', '--manifest',
                   str(path), '--output-dir', str(output), '--summary-format', 'json']
        before = sorted(root.iterdir())
        dry = subprocess.run([*command, '--dry-run'], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(dry.returncode, code, dry.stderr)
        self.assertEqual(sorted(root.iterdir()), before)
        planned = json.loads(dry.stdout)
        self.assertTrue(planned['dry_run'])
        self.assertEqual(planned, run_manifest(path, output, dry_run=True))
        real = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(real.returncode, code, real.stderr)
        summary = json.loads(real.stdout)
        planned['dry_run'] = False
        self.assertEqual(summary, planned)
        self.assertEqual(real.stderr, dry.stderr)
        self.assertEqual(summary, json.loads((output / 'summary.json').read_text()))
        library = root / 'library'
        self.assertEqual(summary, run_manifest(path, library))
        self.assertEqual(summary['status'], status)
        self.assertEqual(summary['counts'], counts)
        if expected is None:
            self.assertNotIn('export_counts', summary)
        else:
            self.assertEqual(summary['export_counts'], expected)
            for totals in expected.values():
                self.assertEqual(totals['requested'], sum(totals[k] for k in ('succeeded', 'failed', 'skipped')))
        for entry in summary['entries']:
            if entry['result']:
                self.assertEqual((output / entry['result']).read_bytes(), (library / entry['result']).read_bytes())
        return summary, real

    def test_mixed_counts_include_real_overflow_and_prerequisite_skips_in_both_orders(self):
        for order in (['srt', 'markdown'], ['markdown', 'srt']):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.entries()
                entries[0]['exports'] = entries[1]['exports'] = order
                for format_ in ('srt', 'markdown'):
                    failed = entries[2].copy()
                    failed.update(recording=f'missing-{format_}', asr=str(root / 'missing.json'), exports=[format_])
                    entries.append(failed)
                summary, proc = self.check_run(root, entries, {
                    'srt': dict(requested=3, succeeded=1, failed=1, skipped=1),
                    'markdown': dict(requested=4, succeeded=3, failed=0, skipped=1),
                }, dict(total=5, succeeded=2, failed=3, items=6, turns=6), 'partial', 1)
                self.assertTrue(summary['partial_results'])
                self.assertEqual(summary['entries'][0]['export_errors']['srt'], summary['entries'][0]['error'])
                self.assertIn('99:59:59.999', proc.stderr)
                self.assertEqual(proc.stderr.count('error:'), 3)
                self.assertEqual(proc.stderr.count('warning:'), 4)
                for index in range(3):
                    self.assertTrue((root / 'out' / f'entry-{index:04d}.md').exists())
                # Aggregate bookkeeping must not change mandatory JSON evidence.
                for entry in entries:
                    entry.pop('exports')
                path = root / 'manifest.json'
                path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
                baseline = root / 'json-only'
                run_manifest(path, baseline)
                for index in range(3):
                    self.assertEqual((root / 'out' / f'entry-{index:04d}.json').read_bytes(),
                                     (baseline / f'entry-{index:04d}.json').read_bytes())

    def test_complete_and_all_failed_totals_with_unrequested_formats_absent(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.entries()[1:]
                for entry in entries:
                    entry['exports'] = ['markdown']
                    if failed:
                        entry['asr'] = str(root / 'missing.json')
                summary, proc = self.check_run(root, entries, {
                    'markdown': dict(requested=2, succeeded=0 if failed else 2, failed=0, skipped=2 if failed else 0),
                }, dict(total=2, succeeded=0 if failed else 2, failed=2 if failed else 0,
                        items=0 if failed else 5, turns=0 if failed else 5),
                    'failed' if failed else 'complete', int(failed))
                self.assertFalse(summary['partial_results'])
                self.assertEqual(proc.stderr.count('error:'), 2 if failed else 0)
                if failed:
                    self.assertEqual(sorted(p.name for p in (root / 'out').iterdir()), ['summary.json'])
                    self.assertTrue(all('export_errors' not in e and e['result'] is None for e in summary['entries']))

    def test_omitted_and_empty_requests_preserve_exact_json_only_summary_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.entries()[1:]
            entries[0].pop('exports')
            entries[1]['exports'] = []
            summary, _ = self.check_run(root, entries, None,
                                       dict(total=2, succeeded=2, failed=0, items=5, turns=5), 'complete', 0)
            self.assertEqual(set(summary), {'schema_version', 'manifest', 'dry_run', 'status',
                                           'partial_results', 'counts', 'entries'})
            for entry in summary['entries']:
                self.assertEqual(set(entry), {'index', 'recording', 'channel', 'asr', 'rttm', 'options',
                                              'result', 'status', 'items', 'turns', 'warnings'})


if __name__ == '__main__':
    unittest.main()
