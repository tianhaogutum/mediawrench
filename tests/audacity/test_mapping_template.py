"""Caller-specified operations become validated, hash-bound review JSON.

The command never infers IDs, lines, separators or split payloads. It requires
complete default reconciliation, prints the exact mapping JSON in dry-run and
write modes, and creates only a new mapping file after all validation succeeds.
"""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import export_subtitles, import_labels
from mediawrench.audacity.formats import parse_srt, parse_vtt

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (b'7\n00:00:01,000 --> 00:00:03,000\nHello\nworld\n\n'
          b'9\n00:00:04,000 --> 00:00:05,000\nAgain\n\n'
          b'11\n00:00:06,000 --> 00:00:07,000\nLater\n\n')
EDITED = '1\t2\tHello\n2\t3\tworld\n4\t7\tAgain / Later\n'
OPERATIONS = [
    {'kind': 'split', 'source': '7', 'separator': '\n',
     'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
    {'kind': 'merge', 'sources': ['9', '11'], 'separator': '\n', 'target': 3}]


class MappingTemplateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.labels, self.sidecar, self.plan, self.output = [self.work / name for name in
            ['edited.txt', 'sidecar.json', 'operations.json', 'mapping.json']]
        self.original_labels, self.sidecar_data = export_subtitles(SOURCE, 'srt')
        self.labels.write_text(EDITED, encoding='utf-8')
        self.sidecar.write_text(json.dumps(self.sidecar_data), encoding='utf-8')
        self.write_plan(OPERATIONS)

    def write_plan(self, operations):
        self.plan.write_text(json.dumps({'operations': operations}), encoding='utf-8')

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.audacity', 'mapping-template',
                               str(self.labels), '--sidecar', str(self.sidecar),
                               '--operations', str(self.plan), '--output', str(self.output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def assert_rejected(self, message=None, *extra):
        process = self.run_cli(*extra)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(process.stdout, '')
        error = json.loads(process.stderr)
        self.assertEqual(error['status'], 'error')
        if message:
            self.assertIn(message, error['error'])
        self.assertFalse(self.output.exists())
        self.assertEqual(sorted(p.name for p in self.work.iterdir()),
                         ['edited.txt', 'operations.json', 'sidecar.json'])
        return error

    def test_generated_hash_bound_json_imports_exact_split_merge(self):
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        mapping = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(process.stdout, self.output.read_text(encoding='utf-8'))
        self.assertEqual(mapping, {'schema': 'audacity-subtitle-bridge-mapping/1',
                                  'sidecar_integrity_sha256': self.sidecar_data['integrity_sha256'],
                                  'labels_sha256': hashlib.sha256(EDITED.encode()).hexdigest(),
                                  'operations': OPERATIONS})
        output, report = import_labels(EDITED, self.sidecar_data, mapping=mapping)
        self.assertEqual([(c.start_ms, c.end_ms, c.text) for c in parse_srt(output)[0]],
                         [(1000, 2000, 'Hello'), (2000, 3000, 'world'), (4000, 7000, 'Again\nLater')])
        self.assertEqual([c['source_ids'] for c in report['output_cues']], [['7'], ['7'], ['9', '11']])
        from mediawrench.audacity import BridgeError
        with self.assertRaisesRegex(BridgeError, 'labels hash mismatch'):
            import_labels(EDITED.replace('1\t2', '1.1\t2'), self.sidecar_data, mapping=mapping)

    def test_dry_run_normalizes_bom_crlf_and_writes_nothing(self):
        self.labels.write_bytes(b'\xef\xbb\xbf' + EDITED.replace('\n', '\r\n').encode())
        dry = self.run_cli('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(self.output.exists())
        mapping = json.loads(dry.stdout)
        self.assertEqual(mapping['labels_sha256'], hashlib.sha256(EDITED.encode()).hexdigest())
        written = self.run_cli()
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout, dry.stdout)
        self.assertEqual(self.output.read_text(encoding='utf-8'), dry.stdout)

    def test_no_projection_decoding_exact_caller_text_survives(self):
        source = SOURCE.replace(b'Hello\nworld', 'café / literal\t\\N\n中文🎵'.encode())
        _, sidecar = export_subtitles(source, 'srt')
        self.sidecar.write_text(json.dumps(sidecar), encoding='utf-8')
        labels = '1\t2\tcafé / literal\\t\\\\N\n2\t3\t中文🎵\n4\t7\tAgain / Later\n'
        self.labels.write_text(labels, encoding='utf-8')
        operations = copy.deepcopy(OPERATIONS)
        operations[0]['targets'] = [{'line': 1, 'text': 'café / literal\t\\N'}, {'line': 2, 'text': '中文🎵'}]
        self.write_plan(operations)
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        mapping = json.loads(process.stdout)
        self.assertEqual(mapping['operations'], operations)
        output, _ = import_labels(labels, sidecar, mapping=mapping)
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['café / literal\t\\N', '中文🎵', 'Again\nLater'])

    def test_no_inference_when_plan_or_exact_split_text_is_missing(self):
        for plan in [{}, {'operations': []}, {'operations': OPERATIONS, 'guess': True}]:
            with self.subTest(plan=plan):
                self.plan.write_text(json.dumps(plan), encoding='utf-8')
                self.assert_rejected()
        operations = copy.deepcopy(OPERATIONS)
        del operations[0]['targets'][0]['text']
        self.write_plan(operations)
        self.assert_rejected('line/text')

    def test_invalid_json_never_writes_mapping(self):
        for raw in ['null', '[]', '{', '{"operations":[],"operations":[]}', '{"operations":NaN}']:
            with self.subTest(raw=raw):
                self.plan.write_text(raw, encoding='utf-8')
                self.assert_rejected()

    def test_invalid_sources_targets_and_compositions_never_write(self):
        variants = []
        for field, value in [('source', 'unknown'), ('source', 7), ('separator', 'DROP'),
                             ('targets', [{'line': True, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]),
                             ('targets', [{'line': 1, 'text': 'Hello'}, {'line': 99, 'text': 'world'}]),
                             ('targets', [{'line': 2, 'text': 'Hello'}, {'line': 1, 'text': 'world'}]),
                             ('targets', [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'worl'}])]:
            operations = copy.deepcopy(OPERATIONS)
            operations[0][field] = value
            variants.append(operations)
        for fields in [{'sources': ['11', '9']}, {'sources': ['7', '11']}, {'target': 1}]:
            operations = copy.deepcopy(OPERATIONS)
            operations[1].update(fields)
            variants.append(operations)
        variants.append(OPERATIONS + OPERATIONS[:1])
        for operations in variants:
            with self.subTest(operations=operations):
                self.write_plan(operations)
                self.assert_rejected()

    def test_corrupt_sidecar_bad_labels_and_projection_are_rejected(self):
        corrupt = copy.deepcopy(self.sidecar_data)
        corrupt['cues'][0]['text'] = 'changed'
        self.sidecar.write_text(json.dumps(corrupt), encoding='utf-8')
        self.assert_rejected('integrity')
        self.sidecar.write_text(json.dumps(self.sidecar_data), encoding='utf-8')
        for labels in ['1\t2\tHello\n\n', EDITED.replace('Hello', '[cue:7] Hello'),
                       EDITED.replace('Hello', 'changed'), EDITED.replace('1\t2', 'NaN\t2')]:
            with self.subTest(labels=labels):
                self.labels.write_text(labels, encoding='utf-8')
                self.assert_rejected()
        self.labels.write_bytes(b'\xff')
        self.assert_rejected('UTF-8')

    def test_unmapped_unchanged_rows_work_but_unresolved_edits_fail(self):
        labels = '1\t2\tHello\n2\t3\tworld\n' + '\n'.join(self.original_labels.splitlines()[1:]) + '\n'
        self.labels.write_text(labels, encoding='utf-8')
        self.write_plan(OPERATIONS[:1])
        ok = self.run_cli('--dry-run')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)['operations'], OPERATIONS[:1])
        for changed in [labels.replace('[cue:9] Again', '[cue:9] edited'),
                        labels.replace('[cue:9]', '[cue:unknown]'),
                        labels + '8\t9\t[cue:7] Hello / world\n',
                        labels + '8\t9\tnew\n', '\n'.join(labels.splitlines()[:-1]) + '\n']:
            with self.subTest(labels=changed):
                self.labels.write_text(changed, encoding='utf-8')
                self.assert_rejected()

    def test_vtt_bridge_ids_settings_inheritance_and_conflicts(self):
        source = ('WEBVTT\n\nspeaker\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
                  '00:04.000 --> 00:05.000 align:end\nAgain\n\n'
                  '00:06.000 --> 00:07.000 align:end\nLater\n')
        _, sidecar = export_subtitles(source.encode(), 'vtt')
        self.sidecar.write_text(json.dumps(sidecar), encoding='utf-8')
        operations = copy.deepcopy(OPERATIONS)
        operations[0]['source'] = 'c1'
        operations[1]['sources'] = ['c2', 'c3']
        self.write_plan(operations)
        ok = self.run_cli('--dry-run')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        output, _ = import_labels(EDITED, sidecar, mapping=json.loads(ok.stdout), output_format='vtt')
        cues = parse_vtt(output)[0]
        self.assertEqual([c.metadata['settings'] for c in cues], ['align:start', 'align:start', 'align:end'])
        self.assertEqual([c.source_id for c in cues], ['speaker', 'speaker', None])
        _, conflicting = export_subtitles(source.replace('align:end\nLater', 'align:start\nLater').encode(), 'vtt')
        self.sidecar.write_text(json.dumps(conflicting), encoding='utf-8')
        self.assert_rejected('incompatible source metadata')

    def test_point_duration_is_explicit_and_shared_with_import(self):
        labels = EDITED.replace('1\t2', '1\t1')
        self.labels.write_text(labels, encoding='utf-8')
        self.assert_rejected('point label')
        ok = self.run_cli('--point-duration', '0.5')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        output, _ = import_labels(labels, self.sidecar_data, mapping=json.loads(ok.stdout), point_duration='0.5')
        self.assertEqual(parse_srt(output)[0][0].end_ms, 1500)

    def test_output_no_clobber_and_dry_run_path_validation(self):
        for extra in [(), ('--dry-run',)]:
            self.output.write_text('keep me', encoding='utf-8')
            result = self.run_cli(*extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('overwrite', json.loads(result.stderr)['error'])
            self.assertEqual(self.output.read_text(), 'keep me')
            self.output.unlink()
        self.output.symlink_to(self.work / 'missing')
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overwrite', json.loads(result.stderr)['error'])
        self.assertTrue(self.output.is_symlink())
        self.output.unlink()
        self.output = self.work / 'missing-dir' / 'mapping.json'
        self.assert_rejected('directory does not exist', '--dry-run')


if __name__ == '__main__':
    unittest.main()
