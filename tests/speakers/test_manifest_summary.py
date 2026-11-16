"""Machine-readable manifest outcomes keep diagnostics off JSON stdout."""

from contextlib import redirect_stderr, redirect_stdout
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
from mediawrench.speakers.export import write_atomic

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / 'examples' / 'speakers' / 'batch'


def strict_json(text):
    def invalid(value):
        raise ValueError(f'nonfinite JSON: {value}')
    return json.loads(text, parse_constant=invalid)


class ManifestSummaryTests(unittest.TestCase):
    def save(self, root, entries):
        path = root / 'manifest.json'
        path.write_text(json.dumps(dict(schema_version=1, entries=entries)), encoding='utf-8')
        return path

    def entries(self):
        entries = json.loads((BATCH / 'manifest-presentations.json').read_text())['entries']
        for entry in entries:
            for key in ('asr', 'rttm'):
                entry[key] = str((BATCH / entry[key]).resolve())
        return entries

    def cli(self, path, output, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.speakers', '--manifest',
                               str(path), '--output-dir', str(output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_partial_json_stdout_preserves_all_outcomes_in_either_export_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for order in (['srt', 'markdown'], ['markdown', 'srt']):
                with self.subTest(order=order):
                    entries = self.entries()
                    entries[0]['exports'] = order
                    path = self.save(root, entries)
                    output = root / order[0]
                    proc = self.cli(path, output, '--summary-format', 'json')
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    summary = strict_json(proc.stdout)
                    self.assertTrue(proc.stdout.endswith('\n'))
                    self.assertEqual(summary, strict_json((output / 'summary.json').read_text()))
                    library = root / f'library-{order[0]}'
                    self.assertEqual(summary, run_manifest(path, library))
                    self.assertEqual(summary['status'], 'partial')
                    self.assertEqual(summary['counts'], dict(total=3, succeeded=2, failed=1, items=6, turns=6))
                    first = summary['entries'][0]
                    self.assertEqual(first['exports']['markdown'], dict(status='ok', file='entry-0000.md'))
                    self.assertEqual(first['exports']['srt'], dict(status='error', file=None))
                    self.assertEqual(first['error'], first['export_errors']['srt'])
                    self.assertIn('99:59:59.999', first['error']['message'])
                    self.assertEqual(proc.stderr.count('entries[0] (after-limit/1) export:srt:'), 1)
                    self.assertEqual(proc.stderr.count('warning: entries[1] (clipping/1):'), 4)
                    for index in range(3):
                        self.assertEqual((output / f'entry-{index:04d}.json').read_bytes(),
                                         (library / f'entry-{index:04d}.json').read_bytes())

    def test_complete_json_summary_and_default_text_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.entries()[1:]
            entries[1]['exports'] = ['srt']
            path = self.save(root, entries)
            output = root / 'json'
            proc = self.cli(path, output, '--summary-format', 'json')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = strict_json(proc.stdout)
            self.assertEqual(summary['status'], 'complete')
            self.assertFalse(summary['partial_results'])
            self.assertEqual(summary['counts'], dict(total=2, succeeded=2, failed=0, items=5, turns=5))
            self.assertEqual(summary, strict_json((output / 'summary.json').read_text()))
            for request in (None, [], ['srt']):
                entry = entries[1].copy()
                entry.pop('exports')
                if request is not None:
                    entry['exports'] = request
                path = self.save(root, [entry])
                target = root / f'text-{request}'
                default = self.cli(path, target, '--dry-run')
                explicit = self.cli(path, target, '--dry-run', '--summary-format', 'text')
                machine = self.cli(path, target, '--dry-run', '--summary-format', 'json')
                self.assertEqual((default.returncode, explicit.returncode, machine.returncode), (0, 0, 0))
                self.assertEqual(default.stdout, 'complete: 1 succeeded, 0 failed; no output written\n')
                self.assertEqual((explicit.stdout, explicit.stderr), (default.stdout, default.stderr))
                planned = strict_json(machine.stdout)['entries'][0]
                self.assertEqual('exports' in planned, bool(request))
                self.assertFalse(target.exists())

    def test_dry_run_parity_for_complete_partial_and_failed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [('complete', self.entries()[1:], 0), ('partial', self.entries(), 1)]
            failed = self.entries()
            for entry in failed:
                entry['asr'] = str(root / 'missing.json')
            cases.append(('failed', failed, 1))
            for status, entries, code in cases:
                with self.subTest(status=status):
                    path = self.save(root, entries)
                    output = root / status
                    before = set(root.iterdir())
                    dry = self.cli(path, output, '--summary-format', 'json', '--dry-run')
                    self.assertEqual(dry.returncode, code, dry.stderr)
                    self.assertEqual(set(root.iterdir()), before)
                    planned = strict_json(dry.stdout)
                    self.assertTrue(planned['dry_run'])
                    self.assertEqual(planned, run_manifest(path, output, dry_run=True))
                    real = self.cli(path, output, '--summary-format', 'json')
                    self.assertEqual(real.returncode, code, real.stderr)
                    self.assertEqual(real.stderr, dry.stderr)
                    planned['dry_run'] = False
                    self.assertEqual(strict_json(real.stdout), planned)
                    self.assertEqual(strict_json((output / 'summary.json').read_text()), planned)
                    self.assertEqual(planned['status'], status)
                    if status == 'failed':
                        self.assertEqual(planned['counts'], dict(total=3, succeeded=0, failed=3, items=0, turns=0))
                        self.assertFalse(planned['partial_results'])
                        self.assertEqual(list(output.iterdir()), [output / 'summary.json'])
                        for entry in planned['entries']:
                            self.assertIsNone(entry['result'])
                            self.assertEqual(entry['error']['stage'], 'asr')
                            self.assertNotIn('export_errors', entry)
                            self.assertTrue(all(e == dict(status='skipped', file=None) for e in entry['exports'].values()))

    def test_preflight_and_directory_errors_emit_no_json_and_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.entries())
            file = root / 'keep'
            file.write_text('preserve')
            existing = root / 'existing'
            existing.mkdir()
            (existing / 'keep').write_text('preserve')
            live, dangling = root / 'live', root / 'dangling'
            live.symlink_to(existing)
            dangling.symlink_to(root / 'absent')
            before = path.read_bytes()
            for target in (file, existing, live, dangling, root / 'missing-parent' / 'out'):
                for extra in ([], ['--dry-run']):
                    proc = self.cli(path, target, '--summary-format', 'json', *extra)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, '')
                    self.assertIn('error:', proc.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(file.read_text(), 'preserve')
            self.assertEqual((existing / 'keep').read_text(), 'preserve')
            self.assertTrue(live.is_symlink() and dangling.is_symlink())
            for invalid in (None, 'markdown', ['markdown', 'markdown'], ['srt', 'json']):
                entries = self.entries()
                entries[1]['exports'] = invalid
                path = self.save(root, entries)
                for extra in ([], ['--dry-run']):
                    proc = self.cli(path, root / 'out', '--summary-format', 'json', *extra)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, '')
                    self.assertIn('entries[1].exports', proc.stderr)
                    self.assertFalse((root / 'out').exists())

    def test_flag_is_manifest_only_and_choices_validate_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.entries())
            invalid = self.cli(path, root / 'out', '--summary-format', 'yaml')
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, '')
            self.assertIn('invalid choice', invalid.stderr)
            self.assertFalse((root / 'out').exists())
            output = root / 'single.json'
            output.write_text('preserve')
            for format_ in ('text', 'json'):
                proc = subprocess.run([sys.executable, '-m', 'mediawrench.speakers',
                                       '--asr', str(BATCH / 'room-a.json'), '--rttm', str(BATCH / 'rooms.rttm'),
                                       '--recording', 'room-a', '--channel', '1', '--output', str(output),
                                       '--summary-format', format_], cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertIn('--summary-format requires --manifest', proc.stderr)
                self.assertEqual(output.read_text(), 'preserve')

    def test_dual_export_errors_are_in_json_and_each_stderr_diagnostic_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.entries()[:1])
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('mediawrench.speakers.manifest.render_markdown', side_effect=BridgeError('injected Markdown failure')):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['--manifest', str(path), '--output-dir', str(root / 'out'), '--summary-format', 'json'])
            self.assertEqual(code, 1)
            summary = strict_json(stdout.getvalue())
            entry = summary['entries'][0]
            self.assertEqual(set(entry['export_errors']), {'srt', 'markdown'})
            self.assertEqual(summary['export_counts'], {
                format_: dict(requested=1, succeeded=0, failed=1, skipped=0)
                for format_ in ('srt', 'markdown')})
            self.assertEqual(summary['counts'], dict(total=1, succeeded=0, failed=1, items=1, turns=1))
            for format_ in ('srt', 'markdown'):
                self.assertEqual(stderr.getvalue().count(f'entries[0] (after-limit/1) export:{format_}:'), 1)
            self.assertTrue((root / 'out' / entry['result']).exists())

    def test_write_errors_clean_output_without_emitting_a_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.entries()[1:])
            output = root / 'out'
            for failing in ('entry-0000.md', 'summary.json'):
                def fail(destination, text):
                    if destination.name == failing:
                        self.assertTrue((output / 'entry-0000.json').exists())
                        raise OSError(f'injected write failure: {failing}')
                    write_atomic(destination, text)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch('mediawrench.speakers.manifest.write_atomic', side_effect=fail):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(['--manifest', str(path), '--output-dir', str(output), '--summary-format', 'json'])
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn(failing, stderr.getvalue())
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
