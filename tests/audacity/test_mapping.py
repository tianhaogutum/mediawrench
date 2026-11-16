"""Explicit split/merge integration: no inferred identities or text edits."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.formats import parse_srt, parse_vtt

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (b'7\n00:00:01,000 --> 00:00:03,000\nHello\nworld\n\n'
          b'9\n00:00:04,000 --> 00:00:05,000\nAgain\n\n'
          b'11\n00:00:06,000 --> 00:00:07,000\nLater\n\n')
EDITED = '1\t2\tHello\n2\t3\tworld\n4\t7\tAgain / Later\n'


def mapping_for(sidecar, labels=EDITED):
    return {'schema': 'audacity-subtitle-bridge-mapping/1',
            'sidecar_integrity_sha256': sidecar['integrity_sha256'],
            'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
            'operations': [
                {'kind': 'split', 'source': '7', 'separator': '\n',
                 'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
                {'kind': 'merge', 'sources': ['9', '11'], 'separator': '\n', 'target': 3}]}


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.labels, self.sidecar = export_subtitles(SOURCE, 'srt')
        self.mapping = mapping_for(self.sidecar)

    def test_split_and_adjacent_merge_preserve_all_sources_and_rules(self):
        output, report = import_labels(EDITED, self.sidecar, mapping=self.mapping, source=SOURCE)
        cues, _ = parse_srt(output)
        self.assertEqual([(c.start_ms, c.end_ms, c.text) for c in cues],
                         [(1000, 2000, 'Hello'), (2000, 3000, 'world'), (4000, 7000, 'Again\nLater')])
        self.assertEqual(report['mapping'], self.mapping)
        actions = [a for a in report['actions'] if a['kind'] in ('split', 'merge')]
        self.assertEqual(actions[0]['sources'], self.sidecar['cues'][:1])
        self.assertEqual(actions[1]['sources'], self.sidecar['cues'][1:])
        self.assertEqual(actions[0]['separator'], '\n')
        self.assertEqual(actions[1]['targets'], [{'line': 3, 'text': 'Again\nLater'}])
        self.assertEqual([x['source_ids'] for x in report['output_cues']], [['7'], ['7'], ['9', '11']])
        self.assertEqual([x['mapping_operation'] for x in report['output_cues']], [1, 1, 2])
        self.assertEqual([x['text'] for x in report['output_cues']], ['Hello', 'world', 'Again\nLater'])

    def test_no_mapping_keeps_strict_identity_behavior(self):
        with self.assertRaises(BridgeError) as caught:
            import_labels(EDITED, self.sidecar)
        self.assertEqual({c['kind'] for c in caught.exception.report['conflicts']},
                         {'new_label', 'missing_cue'})
        duplicate = self.labels + '8\t9\t[cue:7] Hello / world\n'
        with self.assertRaisesRegex(BridgeError, 'Duplicate label cue ID'):
            import_labels(duplicate, self.sidecar)
        _, report = import_labels(self.labels, self.sidecar)
        self.assertNotIn('mapping', report)

    def test_overlap_consumption_rejected_even_with_permissive_policies(self):
        self.mapping['operations'][1]['sources'] = ['7', '9']
        self.assert_invalid('consumed')
        # A mapped original may not also return through the usual ID path.
        labels = EDITED + '8\t9\t[cue:7] Hello / world\n'
        self.mapping = mapping_for(self.sidecar, labels)
        with self.assertRaisesRegex(BridgeError, 'Duplicate label cue ID'):
            import_labels(labels, self.sidecar, mapping=self.mapping)

    def assert_invalid(self, message=None, labels=EDITED):
        with self.assertRaises(BridgeError) as caught:
            import_labels(labels, self.sidecar, mapping=self.mapping,
                          text_policy='edited', missing_policy='drop', new_policy='add')
        self.assertEqual(caught.exception.report['status'], 'error')
        if message:
            self.assertIn(message, str(caught.exception))

    def test_text_loss_and_unapproved_projection_rejected(self):
        for target_text, separator in [('worl', '\n'), ('world', ''), ('changed', '\n')]:
            self.mapping = mapping_for(self.sidecar)
            self.mapping['operations'][0]['targets'][1]['text'] = target_text
            self.mapping['operations'][0]['separator'] = separator
            with self.subTest(text=target_text, separator=separator):
                self.assert_invalid('reconstruct')
        self.mapping = mapping_for(self.sidecar, EDITED.replace('Hello', 'edited'))
        self.assert_invalid('payload', EDITED.replace('Hello', 'edited'))

    def test_invalid_schema_shapes_sources_and_targets(self):
        variants = [None, [], {}, {**self.mapping, 'extra': True},
                    {**self.mapping, 'schema': 'guess'}, {**self.mapping, 'operations': []}]
        for field, value in [('source', 'unknown'), ('source', []), ('kind', 'guess'),
                             ('separator', 'DROP'), ('separator', None), ('targets', []),
                             ('targets', [{'line': True, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]),
                             ('targets', [{'line': 1, 'text': 'Hello'}, {'line': 99, 'text': 'world'}]),
                             ('targets', [{'line': 2, 'text': 'Hello'}, {'line': 1, 'text': 'world'}]),
                             ('targets', [{'line': 1, 'text': ''}, {'line': 2, 'text': 'world'}])]:
            bad = mapping_for(self.sidecar)
            bad['operations'][0][field] = value
            variants.append(bad)
        for sources in [['9'], ['9', '9'], ['11', '9'], ['7', '11'], [True, '11']]:
            bad = mapping_for(self.sidecar)
            bad['operations'][1]['sources'] = sources
            variants.append(bad)
        bad = mapping_for(self.sidecar)
        bad['operations'][1]['target'] = 1
        variants.append(bad)
        for mapping in variants:
            if mapping is None:
                continue  # None is the intentional no-mapping default.
            self.mapping = mapping
            with self.subTest(mapping=mapping):
                self.assert_invalid()

    def test_mapping_bound_to_sidecar_and_exact_decoded_labels(self):
        for field in ['sidecar_integrity_sha256', 'labels_sha256']:
            self.mapping = mapping_for(self.sidecar)
            self.mapping[field] = '0' * 64
            with self.subTest(field=field):
                self.assert_invalid('hash mismatch')
        with self.assertRaisesRegex(BridgeError, 'sidecar'):
            import_labels(EDITED, mapping=self.mapping, allow_lossy=True)

    def test_merge_only_requires_adjacent_order_and_no_repeated_sources(self):
        for ids in [['7', '11'], ['11', '9'], ['9', '9']]:
            self.mapping['operations'] = [{'kind': 'merge', 'sources': ids, 'separator': '\n', 'target': 3}]
            with self.subTest(ids=ids):
                self.assert_invalid()

    def test_mapping_cannot_override_reserved_prefix_or_missing_policy(self):
        labels = EDITED.replace('Hello', '[cue:7] Hello')
        mapping = mapping_for(self.sidecar, labels)
        with self.assertRaisesRegex(BridgeError, 'payload'):
            import_labels(labels, self.sidecar, mapping=mapping)
        labels = '1\t2\tHello\n2\t3\tworld\n'
        mapping = mapping_for(self.sidecar, labels)
        mapping['operations'] = mapping['operations'][:1]
        with self.assertRaises(BridgeError) as caught:
            import_labels(labels, self.sidecar, mapping=mapping)
        self.assertEqual([c['id'] for c in caught.exception.report['conflicts']], ['9', '11'])

    def test_merge_with_empty_separator_preserves_exact_source_payloads(self):
        labels = '1\t3\t[cue:7] Hello / world\n4\t7\tAgainLater\n'
        mapping = mapping_for(self.sidecar, labels)
        mapping['operations'] = [{'kind': 'merge', 'sources': ['9', '11'], 'separator': '', 'target': 2}]
        output, report = import_labels(labels, self.sidecar, mapping=mapping)
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['Hello\nworld', 'AgainLater'])
        self.assertEqual(report['output_cues'][0]['id'], '7')

    def test_partial_mapping_uses_existing_missing_new_and_identity_policies(self):
        labels = '1\t2\tHello\n2\t3\tworld\n' + self.labels.splitlines()[1] + '\n8\t9\tnew\n'
        self.mapping = mapping_for(self.sidecar, labels)
        self.mapping['operations'] = self.mapping['operations'][:1]
        output, report = import_labels(labels, self.sidecar, mapping=self.mapping,
                                       missing_policy='keep', new_policy='add')
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['Hello', 'world', 'Again', 'Later', 'new'])
        self.assertEqual(report['output_cues'][2]['id'], '9')
        self.assertIsNone(report['output_cues'][3]['line'])

    def test_split_unicode_tabs_backslashes_preserve_exact_text(self):
        source = SOURCE.replace(b'Hello\nworld', 'café\t\\N 中文🎵'.encode())
        _, sidecar = export_subtitles(source, 'srt')
        labels = '1\t2\tcafé\\t\\\\N\n2\t3\t中文🎵\n4\t7\tAgain / Later\n'
        mapping = mapping_for(sidecar, labels)
        mapping['operations'][0].update(separator=' ', targets=[{'line': 1, 'text': 'café\t\\N'},
                                                               {'line': 2, 'text': '中文🎵'}])
        output, _ = import_labels(labels, sidecar, mapping=mapping)
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['café\t\\N', '中文🎵', 'Again\nLater'])

    def test_vtt_metadata_split_inherits_merge_requires_equal_settings(self):
        source = ('WEBVTT\n\na\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
                  'b\n00:04.000 --> 00:05.000 align:start\nAgain\n\n'
                  'c\n00:06.000 --> 00:07.000 align:start\nLater\n')
        _, sidecar = export_subtitles(source.encode(), 'vtt')
        mapping = mapping_for(sidecar)
        mapping['operations'][0]['source'] = 'c1'
        mapping['operations'][1]['sources'] = ['c2', 'c3']
        output, report = import_labels(EDITED, sidecar, mapping=mapping, output_format='vtt')
        cues, _ = parse_vtt(output)
        self.assertEqual([c.source_id for c in cues], ['a', 'a', None])
        self.assertEqual([c.metadata for c in cues], [{'settings': 'align:start'}] * 3)
        self.assertTrue(any(x['kind'] == 'merged_cue_identifiers' for x in report['losses']))
        _, different = export_subtitles(source.replace('align:start\nLater', 'align:end\nLater').encode(), 'vtt')
        mapping['sidecar_integrity_sha256'] = different['integrity_sha256']
        with self.assertRaisesRegex(BridgeError, 'metadata'):
            import_labels(EDITED, different, mapping=mapping)

    def test_cli_mapping_success_dry_run_and_invalid_files_no_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels, sidecar, mapping, output = [root / x for x in ['labels.txt', 'sidecar.json', 'mapping.json', 'out.srt']]
            labels.write_text(EDITED, encoding='utf-8')
            sidecar.write_text(json.dumps(self.sidecar), encoding='utf-8')
            mapping.write_text(json.dumps(self.mapping), encoding='utf-8')
            args = [sys.executable, '-m', 'mediawrench.audacity', 'import', str(labels),
                    '--sidecar', str(sidecar), '--mapping', str(mapping), '--output', str(output)]
            def run(*extra):
                return subprocess.run(args + list(extra), cwd=ROOT, capture_output=True, text=True, timeout=120)
            dry = run('--dry-run')
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse(output.exists())
            ok = run()
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(parse_srt(output.read_text())[0][-1].text, 'Again\nLater')
            output.unlink()
            Path(str(output) + '.report.json').unlink()
            bad = copy.deepcopy(self.mapping)
            bad['operations'][0]['targets'][1]['text'] = 'lost'
            for raw in ['null', '[]', '{', '{"schema":"a","schema":"b"}', '{"x":NaN}', json.dumps(bad)]:
                mapping.write_text(raw, encoding='utf-8')
                result = run()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stderr)['status'], 'error')
                self.assertFalse(output.exists())
                self.assertFalse(Path(str(output) + '.report.json').exists())


if __name__ == '__main__':
    unittest.main()
