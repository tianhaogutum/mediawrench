"""Bind summaries to the exact raw manifest snapshot that supplied their entries."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.speakers import BridgeError, run_manifest
from mediawrench.speakers.cli import main
from mediawrench.speakers.export import render_json
from . import test_manifest_observed as observations

observed = observations.observed


class ManifestObservedSnapshotTests(unittest.TestCase):
    def fixture(self, root):
        entry = observations.ManifestObservedTests().fixture(root)[2]
        # Non-ASCII filename ensures the manifest itself contains multibyte bytes.
        source = root / entry['asr']
        source.rename(root / 'café-你好.json')
        entry['asr'] = 'café-你好.json'
        raw = (json.dumps(dict(schema_version=1, entries=[entry]), ensure_ascii=False,
                          indent=2).replace('\n', '\r\n') + '\r\n').encode('utf-8')
        path = root / 'manifest.json'
        path.write_bytes(raw)
        return path

    def test_raw_manifest_hash_changes_with_same_path_bytes_not_parsed_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            raw = path.read_bytes()
            self.assertNotEqual(len(raw), len(raw.decode('utf-8')))
            self.assertIn(b'\r\n', raw)
            first = run_manifest(path, root / 'out', dry_run=True, observe_input_sha256=True)
            self.assertEqual(first['observed_manifest'], observed(raw))
            self.assertNotEqual(first['observed_manifest'], observed(raw.replace(b'\r\n', b'\n')))
            path.write_bytes(raw + b' \r\n')  # Same parsed entries, different byte binding.
            second = run_manifest(path, root / 'out', dry_run=True, observe_input_sha256=True)
            self.assertEqual(second.pop('observed_manifest'), observed(path.read_bytes()))
            self.assertNotEqual(first.pop('observed_manifest'), observed(path.read_bytes()))
            self.assertEqual(first, second)
            default = run_manifest(path, root / 'out', dry_run=True)
            explicit = run_manifest(path, root / 'out', dry_run=True, observe_input_sha256=False)
            self.assertNotIn('observed_manifest', default)
            self.assertNotIn('observed_inputs', default['entries'][0])
            self.assertEqual(render_json(default), render_json(explicit))
            for entry in second['entries']:
                entry.pop('observed_inputs')
            self.assertEqual(second, default)

    def test_single_raw_manifest_read_supplies_both_observation_and_parsing(self):
        for dry in (False, True):
            with self.subTest(dry=dry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.fixture(root)
                raw = path.read_bytes()
                baseline = run_manifest(path, root / 'baseline')
                read_bytes, open_file = Path.read_bytes, Path.open
                reads, modes = [], []

                def track_open(file, mode='r', *args, **kwargs):
                    if file == path and 'r' in mode:
                        modes.append(mode)
                    return open_file(file, mode, *args, **kwargs)

                def mutate_after_read(file):
                    data = read_bytes(file)
                    reads.append(file)
                    if file == path:
                        file.write_bytes(b'invalid replacement\r\n')
                    return data

                output = root / 'out'
                with patch.object(Path, 'open', track_open), patch.object(Path, 'read_bytes', mutate_after_read):
                    summary = run_manifest(path, output, dry_run=dry, observe_input_sha256=True)
                self.assertEqual(summary['observed_manifest'], observed(raw))
                self.assertEqual(modes, ['rb'])
                self.assertEqual(reads, [path] + [Path(baseline['entries'][0][k])
                                                 for k in ('asr', 'aliases', 'rttm')])
                self.assertEqual(path.read_bytes(), b'invalid replacement\r\n')
                stripped = copy.deepcopy(summary)
                stripped.pop('observed_manifest')
                stripped['entries'][0].pop('observed_inputs')
                stripped['dry_run'] = False
                self.assertEqual(stripped, baseline)
                self.assertEqual(output.exists(), not dry)
                if not dry:
                    self.assertEqual((output / 'entry-0000.json').read_bytes(),
                                     (root / 'baseline' / 'entry-0000.json').read_bytes())

    def test_manifest_errors_never_emit_observation_or_read_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            valid = path.read_bytes()
            value = json.loads(valid)
            structural = [dict(value, schema_version=True), dict(value, entries=[]),
                          dict(value, observed_manifest={})]
            invalid_digest = copy.deepcopy(value)
            invalid_digest['entries'][0]['input_sha256']['asr'] = 'invalid'
            invalid_exports = copy.deepcopy(value)
            invalid_exports['entries'][0]['exports'] = ['markdown', 'markdown']
            missing_identity = copy.deepcopy(value)
            del missing_identity['entries'][0]['channel']
            structural += [invalid_digest, invalid_exports, missing_identity]
            cases = [b'\xff\r\n', b'\xef\xbb\xbf' + valid, b'', b'{',
                     b'{"schema_version":1,"schema_version":1,"entries":[]}',
                     valid.replace(b'"schema_version": 1', b'"schema_version": NaN')]
            cases += [json.dumps(v).encode() for v in structural]
            cases += [valid.replace(b'"exports": [', b'"options":{"max_gap":1e309},"exports": [')]
            for raw in cases + [None, 'directory']:
                for dry in (False, True):
                    with self.subTest(raw=raw, dry=dry):
                        if path.is_dir():
                            path.rmdir()
                        elif path.exists():
                            path.unlink()
                        if raw == 'directory':
                            path.mkdir()
                        elif raw is not None:
                            path.write_bytes(raw)
                        output = root / 'out'
                        with patch('mediawrench.speakers.manifest._read_input') as read:
                            with self.assertRaises((BridgeError, OSError, UnicodeError)):
                                run_manifest(path, output, dry_run=dry, observe_input_sha256=True)
                            stdout, stderr = io.StringIO(), io.StringIO()
                            with redirect_stdout(stdout), redirect_stderr(stderr):
                                code = main(['--manifest', str(path), '--output-dir', str(output),
                                             '--observe-input-sha256', '--summary-format', 'json',
                                             *(['--dry-run'] if dry else [])])
                            read.assert_not_called()
                        self.assertEqual(code, 2)
                        self.assertEqual(stdout.getvalue(), '')
                        self.assertTrue(stderr.getvalue().startswith('error:'))
                        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
