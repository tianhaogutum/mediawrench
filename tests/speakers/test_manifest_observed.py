"""Observed bytes describe completed reads, including failed prerequisites."""

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
from . import test_manifest_digests as digests

ROOT = Path(__file__).resolve().parents[2]
ORDER = ('asr', 'aliases', 'rttm')


def observed(data):
    # Independent oracle over fixture bytes, never implementation evidence/stat/text.
    return dict(sha256=hashlib.new('sha256', data).hexdigest(),
                size_bytes=len(data))


class ManifestObservedTests(unittest.TestCase):
    def fixture(self, root):
        return digests.ManifestDigestTests().fixture(root)

    def save(self, root, entries):
        return digests.ManifestDigestTests().save(root, entries)

    def cli(self, path, output, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.speakers', '--manifest',
                               str(path), '--output-dir', str(output), '--summary-format', 'json',
                               '--observe-input-sha256', *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def check_run(self, root, entries, code):
        path = self.save(root, entries)
        output = root / 'out'
        before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
        dry = self.cli(path, output, '--dry-run')
        self.assertEqual(dry.returncode, code, dry.stderr)
        self.assertEqual({p: p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
        self.assertFalse(output.exists())
        planned = json.loads(dry.stdout)
        self.assertTrue(planned['dry_run'])
        self.assertEqual(planned, run_manifest(path, output, dry_run=True, observe_input_sha256=True))
        real = self.cli(path, output)
        self.assertEqual(real.returncode, code, real.stderr)
        summary = json.loads(real.stdout)
        planned['dry_run'] = False
        self.assertEqual(summary, planned)
        self.assertEqual(real.stderr, dry.stderr)
        self.assertEqual(real.stdout, (output / 'summary.json').read_text())
        self.assertEqual(summary, run_manifest(path, root / 'library', observe_input_sha256=True))
        # Observation must not change any decision, diagnostic, count or output byte.
        baseline = run_manifest(path, root / 'baseline')
        self.assertEqual(summary['observed_manifest'], observed(path.read_bytes()))
        stripped = copy.deepcopy(summary)
        stripped.pop('observed_manifest')
        for entry in stripped['entries']:
            entry.pop('observed_inputs')
        self.assertEqual(stripped, baseline)
        for file in output.iterdir():
            if file.name != 'summary.json':
                self.assertEqual(file.read_bytes(), (root / 'baseline' / file.name).read_bytes())
        return summary, real

    def test_same_path_mutation_records_mismatch_snapshot_and_skips_later_inputs(self):
        for key in ORDER:
            for order in (['srt', 'markdown'], ['markdown', 'srt']):
                with self.subTest(key=key, order=order), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    entries = self.fixture(root)[1:]
                    entries[0]['exports'] = order
                    file = root / entries[0][key]
                    original = file.read_bytes()
                    file.write_bytes(original + b'\r\n')  # Same path, still valid content.
                    expected = {k: observed((root / entries[0][k]).read_bytes())
                                for k in ORDER[:ORDER.index(key) + 1]}
                    # These paths should never be attempted after the mismatch.
                    for later in ORDER[ORDER.index(key) + 1:]:
                        entries[0][later] = 'missing-later'
                    summary, proc = self.check_run(root, entries, 1)
                    entry = summary['entries'][0]
                    self.assertEqual(entry['observed_inputs'], expected)
                    self.assertNotEqual(expected[key]['sha256'], entry['input_sha256'][key])
                    self.assertEqual(entry['error']['stage'], key)
                    self.assertIn('SHA-256 mismatch', proc.stderr)
                    self.assertEqual(summary['counts'], dict(total=2, succeeded=1, failed=1, items=1, turns=1))
                    self.assertEqual(summary['export_counts'], {
                        'srt': dict(requested=1, succeeded=0, failed=0, skipped=1),
                        'markdown': dict(requested=2, succeeded=1, failed=0, skipped=1)})

    def test_success_overflow_and_clipping_preserve_all_bytes_in_both_orders(self):
        for order in (['srt', 'markdown'], ['markdown', 'srt']):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.fixture(root)
                entries[0]['exports'] = entries[1]['exports'] = order
                entries[0].pop('input_sha256')
                entries[1]['input_sha256'] = {}  # Also partial and fully specified expectations.
                entries[2]['input_sha256'].pop('rttm')
                expected = [{k: observed((root / e[k]).read_bytes()) for k in ORDER} for e in entries]
                summary, proc = self.check_run(root, entries, 1)
                self.assertEqual([e['observed_inputs'] for e in summary['entries']], expected)
                self.assertEqual(summary['counts'], dict(total=3, succeeded=2, failed=1, items=6, turns=6))
                self.assertEqual(summary['export_counts'], {
                    'srt': dict(requested=2, succeeded=1, failed=1, skipped=0),
                    'markdown': dict(requested=3, succeeded=3, failed=0, skipped=0)})
                self.assertIn('99:59:59.999', proc.stderr)
                self.assertEqual(proc.stderr.count('warning:'), 4)
                self.assertTrue((root / 'out' / 'entry-0000.json').exists())
                self.assertTrue((root / 'out' / 'entry-0000.md').exists())
                self.assertFalse((root / 'out' / 'entry-0000.srt').exists())
                clipped = json.loads((root / 'out' / 'entry-0001.json').read_text())
                self.assertEqual(clipped['config']['rttm_time_correction']['out_of_range_policy'], 'clip')
                self.assertEqual([r['clipping_status'] for r in clipped['corrected_rttm']],
                                 ['dropped', 'clipped', 'unchanged', 'clipped', 'dropped'])

    def test_malformed_read_and_missing_or_unread_input_are_distinct(self):
        for key in ORDER:
            for failure in ('decode', 'parse', 'missing', 'directory'):
                with self.subTest(key=key, failure=failure), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    entry = self.fixture(root)[2]
                    entry['exports'] = ['markdown', 'srt']
                    file = root / entry[key]
                    if failure in ('decode', 'parse'):
                        data = b'\xff\r\n' if failure == 'decode' else b''
                        file.write_bytes(data)
                        # Matching hashes do not turn a malformed read into verified content.
                        entry['input_sha256'][key] = observed(data)['sha256']
                        read_keys = ORDER[:ORDER.index(key) + 1]
                    else:
                        file.unlink()
                        if failure == 'directory':
                            file.mkdir()
                        read_keys = ORDER[:ORDER.index(key)]
                    expected = {k: observed((root / entry[k]).read_bytes()) for k in read_keys}
                    summary, proc = self.check_run(root, [entry], 1)
                    self.assertEqual(summary['entries'][0]['observed_inputs'], expected)
                    self.assertEqual(summary['entries'][0]['error']['stage'], key)
                    self.assertNotIn('SHA-256 mismatch', proc.stderr)
                    self.assertEqual(summary['status'], 'failed')
                    self.assertEqual(summary['counts'], dict(total=1, succeeded=0, failed=1, items=0, turns=0))
                    self.assertEqual(summary['export_counts'], {
                        f: dict(requested=1, succeeded=0, failed=0, skipped=1) for f in ('srt', 'markdown')})
                    self.assertEqual([p.name for p in (root / 'out').iterdir()], ['summary.json'])

    def test_single_snapshot_is_observed_checked_and_parsed_without_reread(self):
        for expectations in (False, True):
            with self.subTest(expectations=expectations), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry = self.fixture(root)[2]
                # Include multibyte UTF-8 and CRLF; byte length differs from character count.
                asr = root / entry['asr']
                value = json.loads(asr.read_text())
                value['words'][0]['word'] = ' café 你好'
                asr.write_bytes((json.dumps(value, ensure_ascii=False) + '\r\n').encode('utf-8'))
                entry['input_sha256']['asr'] = observed(asr.read_bytes())['sha256']
                if not expectations:
                    entry.pop('input_sha256')
                path = self.save(root, [entry])
                snapshots = {root / entry[k]: (root / entry[k]).read_bytes() for k in ORDER}
                baseline = root / 'baseline'
                run_manifest(path, baseline)
                reads = []
                read_bytes = Path.read_bytes

                def mutate_after_read(file):
                    data = read_bytes(file)
                    reads.append(file)
                    file.write_bytes(b'invalid replacement')
                    return data

                output = root / 'out'
                with patch.object(Path, 'read_bytes', mutate_after_read):
                    summary = run_manifest(path, output, observe_input_sha256=True)
                self.assertEqual(summary['status'], 'complete')
                self.assertEqual(reads, [path] + [root / entry[k] for k in ORDER])
                self.assertEqual(summary['entries'][0]['observed_inputs'],
                                 {k: observed(snapshots[root / entry[k]]) for k in ORDER})
                self.assertEqual((output / 'entry-0000.json').read_bytes(),
                                 (baseline / 'entry-0000.json').read_bytes())
                self.assertIn('café 你好', (output / 'entry-0000.json').read_text())

    def test_omission_and_absent_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self.fixture(root)[2]
            entry.pop('aliases')
            entry.pop('input_sha256')
            path = self.save(root, [entry])
            default = run_manifest(path, root / 'out', dry_run=True)
            explicit = run_manifest(path, root / 'out', dry_run=True, observe_input_sha256=False)
            self.assertEqual(render_json(default), render_json(explicit))
            self.assertNotIn('observed_manifest', default)
            self.assertNotIn('observed_inputs', default['entries'][0])
            summary, _ = self.check_run(root, [entry], 0)
            self.assertEqual(summary['entries'][0]['observed_inputs'],
                             {k: observed((root / entry[k]).read_bytes()) for k in ('asr', 'rttm')})

    def test_usage_preflight_directory_and_write_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.fixture(root)[2:])
            keep = root / 'keep'
            keep.write_bytes(b'preserve')
            existing = root / 'existing'
            existing.mkdir()
            (existing / 'keep').write_bytes(b'preserve')
            live, dangling = root / 'live', root / 'dangling'
            live.symlink_to(existing)
            dangling.symlink_to(root / 'absent')
            for target in (keep, existing, live, dangling, root / 'missing-parent' / 'out'):
                for extra in ([], ['--dry-run']):
                    proc = self.cli(path, target, *extra)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, '')
            self.assertEqual(keep.read_bytes(), b'preserve')
            self.assertEqual((existing / 'keep').read_bytes(), b'preserve')
            self.assertTrue(live.is_symlink() and dangling.is_symlink())
            for extra in ([], ['--asr', 'missing', '--rttm', 'missing', '--output', str(keep)]):
                proc = subprocess.run([sys.executable, '-m', 'mediawrench.speakers',
                                       '--observe-input-sha256', *extra], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertIn('--observe-input-sha256 requires --manifest', proc.stderr)
                self.assertEqual(keep.read_bytes(), b'preserve')
            for extra in (['false'], ['--asr', 'missing'], ['--summary-format', 'yaml']):
                proc = self.cli(path, root / 'out', *extra)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertFalse((root / 'out').exists())
            for invalid in (None, 0, 1, 'true', [], {}):
                with patch('mediawrench.speakers.manifest._entries') as preflight:
                    with self.assertRaisesRegex(BridgeError, 'observe_input_sha256.*boolean'):
                        run_manifest(path, root / 'out', observe_input_sha256=invalid)
                    preflight.assert_not_called()
            entries = self.fixture(root)[1:]
            entries[1]['input_sha256']['asr'] = 'invalid'
            path = self.save(root, entries)
            with patch('mediawrench.speakers.manifest._read_input') as read:
                with self.assertRaisesRegex(BridgeError, 'input_sha256'):
                    run_manifest(path, root / 'out', observe_input_sha256=True)
                read.assert_not_called()
            proc = self.cli(path, root / 'out')
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
            path = self.save(root, self.fixture(root)[2:])
            for failing in ('entry-0000.md', 'summary.json'):
                output = root / 'out'

                def fail(destination, text):
                    if destination.name == failing:
                        raise OSError('injected write failure')
                    write_atomic(destination, text)

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch('mediawrench.speakers.manifest.write_atomic', side_effect=fail):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(['--manifest', str(path), '--output-dir', str(output),
                                     '--observe-input-sha256', '--summary-format', 'json'])
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('injected write failure', stderr.getvalue())
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
