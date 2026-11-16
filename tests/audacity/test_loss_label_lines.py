"""Per-cue losses locate edited label lines, separately from final output indices."""

import hashlib
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_identifier_loss_provenance import SOURCE as MIXED_SOURCE
from .test_merged_identifier_loss_provenance import (
    SOURCE, LABELS, EXPECTED_SRT, EXPECTED_VTT, mapping_for,
)


class LossLabelLinesTests(unittest.TestCase):
    def assert_lines(self, report, expected):
        index_key = report['output_format'] + '_index'
        losses = [x for x in report['losses'] if x['kind'] != 'webvtt_document_metadata']
        self.assertEqual([(x['kind'], x[index_key], x['line']) for x in losses], expected)
        for loss in losses:
            self.assertEqual(loss['line'], report['output_cues'][loss[index_key] - 1]['line'])
        for loss in report['losses']:
            if loss['kind'] == 'webvtt_document_metadata':
                self.assertNotIn('line', loss)

    def test_reordered_repeated_null_merges_and_identifier_free_negative(self):
        identifier = '  voix / 同じ  '
        for identifiers in (True, False):
            source = SOURCE.replace(b'alpha\n', (identifier + '\n').encode())
            source = source.replace(b'beta\n', (identifier + '\n').encode())
            if not identifiers:
                source = source.replace((identifier + '\n').encode(), b'').replace(b'delta\n', b'')
            source = source.replace(b'.000\n', b'.000 align:start\n')
            _, sidecar = export_subtitles(source, 'vtt')
            for format, expected_output in [('srt', EXPECTED_SRT), ('vtt', EXPECTED_VTT)]:
                warned = None
                for policy in ('warn', 'error'):
                    with self.subTest(identifiers=identifiers, format=format, policy=policy):
                        options = dict(mapping=mapping_for(sidecar), new_policy='ignore',
                                       output_format=format, settings_loss_policy=policy)
                        if format == 'srt' and policy == 'error':
                            with self.assertRaises(BridgeError) as caught:
                                import_labels(LABELS, sidecar, **options)
                            report = caught.exception.report
                        else:
                            output, report = import_labels(LABELS, sidecar, **options)
                            expected = expected_output
                            if format == 'vtt':
                                expected = expected.replace('.000\n', '.000 align:start\n')
                            self.assertEqual(output, expected)
                        if policy == 'warn':
                            warned = report
                        else:
                            self.assertEqual(report['losses'], warned['losses'])
                            self.assertEqual(report['output_cues'], warned['output_cues'])
                            self.assertEqual(report['warnings'], warned['warnings'])
                        expected = ([('merged_cue_identifiers', 3, 4),
                                     ('merged_cue_identifiers', 1, 2)] if identifiers else [])
                        if format == 'srt':
                            expected += [('webvtt_cue_settings', 1, 2),
                                         ('webvtt_cue_settings', 2, 3),
                                         ('webvtt_cue_settings', 3, 4)]
                        self.assert_lines(report, expected)
                        merged = [x for x in report['losses'] if x['kind'] == 'merged_cue_identifiers']
                        self.assertEqual([x['source_identifiers'] for x in merged],
                                         [[identifier, identifier], [None, 'delta']] if identifiers else [])

    def test_split_ordinary_kept_and_new_rows_under_both_missing_policies(self):
        source = MIXED_SOURCE + b'\nkept\n00:10.000 --> 00:11.000\nKeep\n'
        source = source.replace(b'WEBVTT\n', b'WEBVTT audit\n')
        source = source.replace(b'.000\n', b'.000 align:start\n')
        _, sidecar = export_subtitles(source, 'vtt')
        labels = ('0\t0.1\tnew\n0.2\t0.3\t[cue:c3] Tail\n12\t13\tAgain / Later\n'
                  '14\t15\tHello\n16\t17\t[cue:c2] plain\n18\t19\tworld\n')
        mapping = {'schema': 'audacity-subtitle-bridge-mapping/1',
                   'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                   'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                   'operations': [
                       {'kind': 'split', 'source': 'c1', 'separator': '\n',
                        'targets': [{'line': 4, 'text': 'Hello'}, {'line': 6, 'text': 'world'}]},
                       {'kind': 'merge', 'sources': ['c4', 'c5'], 'separator': '\n', 'target': 3}]}
        for missing in ('keep', 'drop'):
            for new in ('ignore', 'add'):
                for format in ('srt', 'vtt'):
                    warned = None
                    for policy in ('warn', 'error'):
                        with self.subTest(missing=missing, new=new, format=format, policy=policy):
                            options = dict(mapping=mapping, missing_policy=missing, new_policy=new,
                                           output_format=format, settings_loss_policy=policy)
                            if format == 'srt' and policy == 'error':
                                with self.assertRaises(BridgeError) as caught:
                                    import_labels(labels, sidecar, **options)
                                report = caught.exception.report
                            else:
                                _, report = import_labels(labels, sidecar, **options)
                            if policy == 'warn':
                                warned = report
                            else:
                                self.assertEqual(report['losses'], warned['losses'])
                                self.assertEqual(report['output_cues'], warned['output_cues'])
                                self.assertEqual(report['warnings'], warned['warnings'])
                            rows = [(2, True)] + ([(None, True)] if missing == 'keep' else [])
                            rows += [(3, False), (4, True), (5, False), (6, True)]
                            offset = int(new == 'add')
                            merge_index = 2 + int(missing == 'keep') + offset
                            expected = [('merged_cue_identifiers', merge_index, 3)]
                            if format == 'srt':
                                for index, (line, has_identifier) in enumerate(rows, 1 + offset):
                                    if has_identifier:
                                        expected.append(('webvtt_cue_identifier', index, line))
                                    expected.append(('webvtt_cue_settings', index, line))
                            self.assert_lines(report, expected)
                            self.assertEqual([x['line'] for x in report['output_cues']],
                                             ([1] if new == 'add' else []) + [r[0] for r in rows])
                            self.assertIn({'kind': 'missing_cue', 'id': 'c6', 'policy': missing}, report['actions'])


if __name__ == '__main__':
    unittest.main()
