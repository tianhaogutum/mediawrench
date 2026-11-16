"""Opt-in complete expectations are a whole-manifest preflight requirement."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, run_manifest
from mediawrench.speakers.export import render_json, write_atomic
from .test_manifest_binding import captured
from . import test_manifest_digests as digests


def sha(data):
    return hashlib.new('sha256', data).hexdigest()


class RequiredDigestTests(unittest.TestCase):
    def fixture(self, root, aliases=True):
        entries = digests.ManifestDigestTests().fixture(root)
        if not aliases:
            for entry in entries:
                entry.pop('aliases')
                entry['input_sha256'].pop('aliases')
        return entries

    def save(self, root, entries):
        return digests.ManifestDigestTests().save(root, entries)

    def args(self, path, output, observe=False, dry=False):
        return ['--manifest', str(path), '--output-dir', str(output),
                '--require-input-sha256', '--summary-format', 'json',
                *(['--observe-input-sha256'] if observe else []),
                *(['--dry-run'] if dry else [])]

    def test_later_missing_coverage_rejects_before_earlier_complete_entry_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.fixture(root)
            entries[1]['input_sha256'] = {}
            path = self.save(root, entries)
            # An otherwise runnable earlier entry must not read or render anything.
            with patch('mediawrench.speakers.manifest._read_input') as read:
                with patch('mediawrench.speakers.manifest.write_atomic') as write:
                    with self.assertRaisesRegex(BridgeError, r'entries\[1\].*input_sha256.asr'):
                        run_manifest(path, root / 'out', require_input_sha256=True)
                    read.assert_not_called()
                    write.assert_not_called()
            self.assertFalse((root / 'out').exists())

    def test_omitted_empty_and_each_partial_map_fail_with_context_without_io(self):
        for aliases in (False, True):
            keys = ('asr', 'rttm', 'aliases') if aliases else ('asr', 'rttm')
            for missing in ('omitted', 'empty', *keys):
                for observe in (False, True):
                    for dry in (False, True):
                        with self.subTest(aliases=aliases, missing=missing, observe=observe, dry=dry), tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            entries = self.fixture(root, aliases)
                            entry = entries[1]
                            if missing == 'omitted':
                                entry.pop('input_sha256')
                            elif missing == 'empty':
                                entry['input_sha256'] = {}
                            else:
                                del entry['input_sha256'][missing]
                            path = self.save(root, entries)
                            expected = sha(path.read_bytes())
                            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                            reads, read_bytes = [], Path.read_bytes
                            def track(file):
                                reads.append(file)
                                return read_bytes(file)
                            with patch.object(Path, 'read_bytes', track), patch('mediawrench.speakers.manifest.write_atomic') as write:
                                with self.assertRaises(BridgeError) as error:
                                    run_manifest(path, root / 'out', require_input_sha256=True,
                                                 observe_input_sha256=observe, dry_run=dry,
                                                 expected_manifest_sha256=expected)
                                code, stdout, stderr = captured(self.args(path, root / 'out', observe, dry) + ['--manifest-sha256', expected])
                                write.assert_not_called()
                            self.assertEqual(reads, [path, path])
                            self.assertEqual(code, 2)
                            self.assertEqual(stdout, '')
                            key = missing if missing in keys else 'asr'
                            for detail in ('entries[1]', f"({entry['recording']}/{entry['channel']})",
                                           f'input_sha256.{key}', str(root / entry[key])):
                                self.assertIn(detail, str(error.exception))
                                self.assertIn(detail, stderr)
                            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})
                            self.assertFalse((root / 'out').exists())

    def test_strict_boolean_and_manifest_only_usage_before_reads(self):
        with patch.object(Path, 'read_bytes') as read:
            for value in (None, 0, 1, 0.0, 1.0, '', 'true', [], {}, object()):
                with self.subTest(value=value), self.assertRaisesRegex(BridgeError, 'require_input_sha256: expected a boolean'):
                    run_manifest('missing', 'out', require_input_sha256=value)
            for argv in (['--require-input-sha256'],
                         ['--asr', 'missing', '--rttm', 'missing', '--require-input-sha256'],
                         ['--manifest', 'missing', '--output-dir', 'out', '--require-input-sha256', 'true'],
                         ['--manifest', 'missing', '--require-input-sha256'],
                         ['--manifest', 'missing', '--output-dir', 'out', '--require-input-sha256', '--asr', 'missing']):
                code, stdout, stderr = captured(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, '')
            read.assert_not_called()

    def test_manifest_mismatch_and_existing_preflight_errors_precede_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = self.fixture(root)
            entries[0].pop('input_sha256')
            path = self.save(root, entries)
            expected = sha(path.read_bytes())
            with patch('mediawrench.speakers.manifest._read_input') as read:
                for raw in (path.read_bytes() + b' ', b'\xff', b'{'):
                    path.write_bytes(raw)
                    code, stdout, stderr = captured(self.args(path, root / 'out') + ['--manifest-sha256', expected])
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn('manifest SHA-256 mismatch', stderr)
                bad_cases = [('input_sha256', {'asr': 'A' * 64}, '64 lowercase'),
                             ('input_sha256', {'unknown': 'a' * 64}, 'unknown keys'),
                             ('exports', ['bad'], 'exports'),
                             ('channel', 0, 'channel'),
                             ('options', {'max_gap': float('inf')}, 'nonfinite')]
                for key, value, diagnostic in bad_cases:
                    changed = copy.deepcopy(entries)
                    changed[1][key] = value
                    path = self.save(root, changed)
                    code, stdout, stderr = captured(self.args(path, root / 'out'))
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn(diagnostic, stderr)
                    self.assertNotIn('required expected', stderr)
                # A manifest field cannot substitute for the external option.
                path.write_text(json.dumps(dict(schema_version=1, entries=entries, require_input_sha256=True)))
                code, stdout, stderr = captured(self.args(path, root / 'out'))
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('unknown keys', stderr)
                read.assert_not_called()
            self.assertFalse((root / 'out').exists())

    def test_default_false_accepts_omitted_empty_partial_and_no_aliases(self):
        for aliases in (False, True):
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.fixture(root, aliases)
                entries[0].pop('input_sha256')
                entries[1]['input_sha256'] = {}
                entries[2]['input_sha256'] = {'asr': entries[2]['input_sha256']['asr']}
                path = self.save(root, entries)
                for observe in (False, True):
                    baseline = run_manifest(path, root / f'baseline-{observe}', observe_input_sha256=observe)
                    explicit = run_manifest(path, root / f'false-{observe}', observe_input_sha256=observe,
                                            require_input_sha256=False)
                    self.assertEqual(render_json(baseline), render_json(explicit))
                    self.assertNotIn('input_sha256', explicit['entries'][0])
                    self.assertEqual(explicit['entries'][1]['input_sha256'], {})
                    self.assertNotIn('require_input_sha256', explicit)

    def test_complete_coverage_preserves_bytes_counts_observation_and_dry_run(self):
        for aliases in (False, True):
            for observe in (False, True):
                for order in (['srt', 'markdown'], ['markdown', 'srt']):
                    for case in ('complete', 'mixed', 'failed', 'json-only'):
                        with self.subTest(aliases=aliases, observe=observe, order=order, case=case), tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            entries = self.fixture(root, aliases)
                            for entry in entries:
                                entry['exports'] = order
                            if case != 'mixed':
                                entries = entries[1:]
                            if case == 'failed':
                                for entry in entries:
                                    (root / entry['asr']).write_bytes(b'\xffmismatch')
                            if case == 'mixed':
                                (root / entries[-1]['asr']).write_bytes(b'\xffmismatch')
                            if case == 'json-only':
                                for entry in entries:
                                    entry.pop('exports')
                            path = self.save(root, entries)
                            expected = sha(path.read_bytes())
                            baseline = run_manifest(path, root / 'baseline', observe_input_sha256=observe)
                            kwargs = dict(require_input_sha256=True, observe_input_sha256=observe,
                                          expected_manifest_sha256=expected)
                            library = run_manifest(path, root / 'library', **kwargs)
                            self.assertEqual(library, baseline)
                            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                            code, stdout, stderr = captured(self.args(path, root / 'out', observe, True))
                            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})
                            self.assertFalse((root / 'out').exists())
                            self.assertEqual(json.loads(stdout), run_manifest(path, root / 'dry', dry_run=True, **kwargs))
                            planned = json.loads(stdout)
                            planned['dry_run'] = False
                            self.assertEqual(planned, baseline)
                            real_code, real_stdout, real_stderr = captured(self.args(path, root / 'out', observe))
                            self.assertEqual(code, int(case in ('mixed', 'failed')))
                            self.assertEqual(real_code, code)
                            self.assertEqual(real_stderr, stderr)
                            self.assertEqual(real_stdout, render_json(library))
                            for file in (root / 'out').iterdir():
                                self.assertEqual(file.read_bytes(), (root / 'baseline' / file.name).read_bytes())
                            self.assertEqual(library['counts'], {
                                'complete': dict(total=2, succeeded=2, failed=0, items=5, turns=5),
                                'json-only': dict(total=2, succeeded=2, failed=0, items=5, turns=5),
                                'mixed': dict(total=3, succeeded=1, failed=2, items=5, turns=5),
                                'failed': dict(total=2, succeeded=0, failed=2, items=0, turns=0)}[case])
                            if case in ('mixed', 'failed'):
                                skipped = 1 if case == 'mixed' else 2
                                self.assertEqual(library['export_counts']['srt'], dict(
                                    requested=len(entries), succeeded=int(case == 'mixed'),
                                    failed=int(case == 'mixed'), skipped=skipped))
                                self.assertEqual(library['export_counts']['markdown'], dict(
                                    requested=len(entries), succeeded=2 if case == 'mixed' else 0,
                                    failed=0, skipped=skipped))
                                self.assertIn('SHA-256 mismatch', stderr)
                            if observe:
                                self.assertEqual(library['observed_manifest'], dict(sha256=expected, size_bytes=len(path.read_bytes())))
                            else:
                                self.assertNotIn('observed_manifest', library)
                            if case == 'mixed':
                                self.assertTrue((root / 'out' / 'entry-0000.md').exists())
                                self.assertFalse((root / 'out' / 'entry-0000.srt').exists())
                                audit = json.loads((root / 'out' / 'entry-0001.json').read_bytes())
                                self.assertEqual([r['clipping_status'] for r in audit['corrected_rttm']],
                                                 ['dropped', 'clipped', 'unchanged', 'clipped', 'dropped'])
                            args = self.args(path, root / 'text-dry', observe, True)
                            args[args.index('json')] = 'text'
                            self.assertEqual(captured(args), captured([a for a in args if a != '--require-input-sha256']))

    def test_each_complete_but_mismatched_input_retains_entry_failure_and_skips(self):
        for key in ('asr', 'aliases', 'rttm'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entries = self.fixture(root)[1:]
                for entry in entries:
                    entry['exports'] = ['srt', 'markdown']
                (root / entries[0][key]).write_bytes(b'\xffmismatch')
                path = self.save(root, entries)
                code, stdout, stderr = captured(self.args(path, root / 'out', True))
                self.assertEqual(code, 1)
                summary = json.loads(stdout)
                failed = summary['entries'][0]
                self.assertEqual(failed['error']['stage'], key)
                self.assertIsNone(failed['result'])
                self.assertIn('SHA-256 mismatch', stderr)
                read_order = ('asr', 'aliases', 'rttm')
                self.assertEqual(set(failed['observed_inputs']), set(read_order[:read_order.index(key) + 1]))
                for format_ in ('srt', 'markdown'):
                    self.assertEqual(summary['export_counts'][format_], dict(
                        requested=2, succeeded=1, failed=0, skipped=1))
                self.assertFalse((root / 'out' / 'entry-0000.json').exists())
                self.assertTrue((root / 'out' / 'entry-0001.json').exists())

    def test_complete_coverage_keeps_directory_refusal_and_write_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, self.fixture(root)[1:])
            keep = root / 'keep'
            keep.write_bytes(b'preserve')
            existing = root / 'existing'
            existing.mkdir()
            (existing / 'keep').write_bytes(b'preserve')
            live, dangling = root / 'live', root / 'dangling'
            live.symlink_to(existing)
            dangling.symlink_to(root / 'absent')
            for output in (keep, existing, live, dangling, root / 'missing-parent' / 'out'):
                for dry in (False, True):
                    code, stdout, stderr = captured(self.args(path, output, True, dry))
                    self.assertEqual((code, stdout), (2, ''))
            for failing in ('entry-0000.json', 'entry-0000.srt', 'entry-0000.md', 'summary.json'):
                def fail(destination, text):
                    if destination.name == failing:
                        raise OSError('injected required-digests write failure')
                    write_atomic(destination, text)
                with patch('mediawrench.speakers.manifest.write_atomic', side_effect=fail):
                    code, stdout, stderr = captured(self.args(path, root / 'out', True))
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('injected required-digests write failure', stderr)
                self.assertFalse((root / 'out').exists())
            self.assertEqual(keep.read_bytes(), b'preserve')
            self.assertEqual((existing / 'keep').read_bytes(), b'preserve')
            self.assertTrue(live.is_symlink() and dangling.is_symlink())


if __name__ == '__main__':
    unittest.main()
