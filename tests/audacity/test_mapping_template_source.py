"""Optional template pairing checks use original bytes, never normalized text."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import (
    BridgeError, create_mapping_template, export_subtitles, import_labels,
)

ROOT = Path(__file__).resolve().parents[2]
CHOICES = dict(missing_policy='keep', new_policy='add')


def fixture(fmt):
    if fmt == 'srt':
        source = (b'7\n00:00:01,000 --> 00:00:03,000\nHello\nworld\n\n'
                  b'9\n00:00:04,000 --> 00:00:05,000\nAgain\n\n'
                  b'11\n00:00:06,000 --> 00:00:07,000\nLater\n\n'
                  b'13\n00:00:08,000 --> 00:00:09,000\nMissing\n')
        ids = ['7', '9', '11']
    else:
        source = (b'WEBVTT\n\nspeaker\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
                  b'a\n00:04.000 --> 00:05.000 align:end\nAgain\n\n'
                  b'b\n00:06.000 --> 00:07.000 align:end\nLater\n\n'
                  b'm\n00:08.000 --> 00:09.000\nMissing\n')
        ids = ['c1', 'c2', 'c3']
    labels = '1\t2\tHello\n2\t3\tworld\n4\t7\tAgain / Later\n10\t11\tNew\n'
    operations = [
        {'kind': 'split', 'source': ids[0], 'separator': '\n',
         'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
        {'kind': 'merge', 'sources': ids[1:], 'separator': '\n', 'target': 3}]
    return source, labels, operations


def variants(source):
    return {'content': source.replace(b'Hello', b'Hallo'),
            'bom': b'\xef\xbb\xbf' + source,
            'crlf': source.replace(b'\n', b'\r\n'), 'empty': b''}


class MappingTemplateSourceTests(unittest.TestCase):
    def test_library_exact_source_and_optional_behavior_preserve_mapping(self):
        for fmt in ['srt', 'vtt']:
            original, labels, operations = fixture(fmt)
            for source in [original, variants(original)['bom'], variants(original)['crlf']]:
                with self.subTest(fmt=fmt, source_hash=hashlib.sha256(source).hexdigest()):
                    _, sidecar = export_subtitles(source, fmt)
                    before = copy.deepcopy((sidecar, operations))
                    unchecked = create_mapping_template(labels, sidecar, operations, **CHOICES)
                    checked = create_mapping_template(labels, sidecar, operations, source=source, **CHOICES)
                    self.assertEqual(checked, unchecked)
                    self.assertEqual(checked, {
                        'schema': 'audacity-subtitle-bridge-mapping/1',
                        'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                        'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                        'operations': operations})
                    self.assertEqual((sidecar, operations), before)
                    _, audit = import_labels(labels, sidecar, mapping=checked, source=source,
                                             output_format=fmt, **CHOICES)
                    self.assertTrue(audit['source_verified'])
                    self.assertEqual(audit['output_count'], 5)
                    self.assertEqual([c.get('source_ids') for c in audit['output_cues']],
                                     [[operations[0]['source']], [operations[0]['source']],
                                      operations[1]['sources'], None, None])

    def test_library_mismatch_uses_import_byte_contract(self):
        for fmt in ['srt', 'vtt']:
            source, labels, operations = fixture(fmt)
            _, sidecar = export_subtitles(source, fmt)
            mapping = create_mapping_template(labels, sidecar, operations, **CHOICES)
            for change, changed in variants(source).items():
                with self.subTest(fmt=fmt, change=change):
                    if change in ['bom', 'crlf']:
                        _, normalized = export_subtitles(changed, fmt)
                        self.assertEqual(normalized['cues'], sidecar['cues'])
                    with self.assertRaisesRegex(BridgeError, 'Source hash mismatch') as imported:
                        import_labels(labels, sidecar, mapping=mapping, source=changed, **CHOICES)
                    with self.assertRaisesRegex(BridgeError, 'Source hash mismatch') as prepared:
                        create_mapping_template(labels, sidecar, operations, source=changed, **CHOICES)
                    self.assertEqual(str(prepared.exception), str(imported.exception))
                    self.assertEqual(prepared.exception.report['status'], 'error')
                    self.assertNotIn('mapping', prepared.exception.report)

    def test_verified_source_does_not_bypass_operation_or_sidecar_validation(self):
        source, labels, operations = fixture('srt')
        _, sidecar = export_subtitles(source, 'srt')
        invalid = copy.deepcopy(operations)
        invalid[0]['targets'][0]['text'] = 'wrong'
        with self.assertRaisesRegex(BridgeError, 'reconstruct'):
            create_mapping_template(labels, sidecar, invalid, source=source, **CHOICES)
        sidecar['cues'][0]['text'] = 'tampered'
        with self.assertRaisesRegex(BridgeError, 'integrity'):
            create_mapping_template(labels, sidecar, operations, source=source, **CHOICES)

    def test_cli_valid_split_merge_accepts_source_and_refuses_mismatches_without_files(self):
        for fmt in ['srt', 'vtt']:
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                source, labels, operations = fixture(fmt)
                _, sidecar = export_subtitles(source, fmt)
                (work / 'source').write_bytes(source)  # no suffix guessing
                # Label normalization is separate from original-source byte verification.
                (work / 'labels').write_bytes(b'\xef\xbb\xbf' + labels.replace('\n', '\r\n').encode())
                (work / 'sidecar').write_text(json.dumps(sidecar), encoding='utf-8')
                (work / 'plan').write_text(json.dumps({'operations': operations}), encoding='utf-8')
                output = work / 'mapping.json'
                def run(*extra):
                    return subprocess.run([sys.executable, '-m', 'mediawrench.audacity',
                        'mapping-template', str(work / 'labels'), '--sidecar', str(work / 'sidecar'),
                        '--operations', str(work / 'plan'), '--output', str(output),
                        '--missing-policy', 'keep', '--new-policy', 'add', *extra],
                        cwd=ROOT, capture_output=True, text=True, timeout=120)
                initial = sorted(work.iterdir())
                unchecked = run()
                self.assertEqual(unchecked.returncode, 0, unchecked.stderr)
                self.assertEqual(json.loads(unchecked.stdout),
                                 create_mapping_template(labels, sidecar, operations, **CHOICES))
                output.unlink()
                flags = ('--source', str(work / 'source'))
                for extra in [('--dry-run',), ()]:
                    checked = run(*flags, *extra)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    self.assertEqual(checked.stdout, unchecked.stdout)
                    if not extra:
                        self.assertEqual(output.read_text(encoding='utf-8'), checked.stdout)
                        output.unlink()
                    self.assertEqual(sorted(work.iterdir()), initial)
                for change, changed in variants(source).items():
                    (work / 'source').write_bytes(changed)
                    for extra in [(), ('--dry-run',)]:
                        with self.subTest(change=change, extra=extra):
                            refused = run(*flags, *extra)
                            self.assertEqual(refused.returncode, 2, refused.stderr)
                            self.assertEqual(refused.stdout, '')
                            error = json.loads(refused.stderr)
                            self.assertIn('Source hash mismatch', error['error'])
                            self.assertEqual(error['report']['status'], 'error')
                            self.assertEqual(sorted(work.iterdir()), initial)
                (work / 'source').write_bytes(source)
                output.write_bytes(b'existing mapping')
                for extra in [(), ('--dry-run',)]:
                    refused = run(*flags, *extra)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertIn('overwrite', json.loads(refused.stderr)['error'])
                    self.assertEqual(output.read_bytes(), b'existing mapping')
                self.assertEqual(sorted(work.iterdir()), sorted(initial + [output]))


if __name__ == '__main__':
    unittest.main()
