"""Merged outputs expose source identifiers without joining actions or losses."""

from copy import deepcopy
import hashlib
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_merged_settings_identifiers import CASES, source_for
from .test_merged_identifier_loss_provenance import (
    LABELS, EXPECTED_SRT, EXPECTED_VTT, mapping_for,
)


class MergedOutputIdentifiersTests(unittest.TestCase):
    def test_exact_output_entries_across_reconciliation_formats_and_policies(self):
        for identifiers in CASES:
            for settings in (False, True):
                _, sidecar = export_subtitles(source_for(identifiers, settings), 'vtt')
                for kept in (False, True):
                    labels = ('0\t0.1\tnew\n6\t9\tC / D\n12\t15\tA / B\n'
                              if kept else LABELS)
                    mapping = mapping_for(sidecar, labels, 3 if kept else 4)
                    original = deepcopy((sidecar, mapping))
                    for new in ('ignore', 'add'):
                        for format in ('srt', 'vtt'):
                            warned = None
                            for policy in ('warn', 'error'):
                                with self.subTest(identifiers=identifiers, settings=settings,
                                                  kept=kept, new=new, format=format, policy=policy):
                                    strict = settings and format == 'srt' and policy == 'error'
                                    options = dict(mapping=mapping, new_policy=new,
                                        missing_policy='keep' if kept else 'error',
                                        output_format=format, settings_loss_policy=policy)
                                    if strict:
                                        with self.assertRaises(BridgeError) as caught:
                                            import_labels(labels, sidecar, **options)
                                        report = caught.exception.report
                                    else:
                                        output, report = import_labels(labels, sidecar, **options)
                                        expected = EXPECTED_SRT if format == 'srt' else EXPECTED_VTT
                                        if kept:
                                            expected = (
                                                '1\n00:00:04,000 --> 00:00:05,000\nplain\n\n'
                                                '2\n00:00:06,000 --> 00:00:09,000\nC\nD\n\n'
                                                '3\n00:00:12,000 --> 00:00:15,000\nA\nB\n\n'
                                                if format == 'srt' else
                                                'WEBVTT\n\n00:00:04.000 --> 00:00:05.000\nplain\n\n'
                                                '00:00:06.000 --> 00:00:09.000\nC\nD\n\n'
                                                '00:00:12.000 --> 00:00:15.000\nA\nB\n\n')
                                        if settings and format == 'vtt':
                                            expected = expected.replace('.000\n', '.000 align:start\n')
                                        if new == 'add':
                                            if format == 'srt':
                                                for i in (3, 2, 1):
                                                    expected = expected.replace(f'{i}\n00:', f'{i + 1}\n00:')
                                                expected = '1\n00:00:00,000 --> 00:00:00,100\nnew\n\n' + expected
                                            else:
                                                expected = expected.replace('WEBVTT\n\n',
                                                    'WEBVTT\n\n00:00:00.000 --> 00:00:00.100\nnew\n\n')
                                        self.assertEqual(output, expected)
                                    if policy == 'warn':
                                        warned = report
                                    else:
                                        self.assertEqual(report, {**warned, 'settings_loss_policy': 'error',
                                            'status': 'error' if strict else 'ok'})
                                    # The arrays belong to outputs, even when there is no loss to join.
                                    metadata = {'settings': 'align:start'} if settings else {}
                                    plain = dict(id='c3', line=None if kept else 3, source_id=None,
                                                 start_ms=4000, end_ms=5000, metadata=metadata)
                                    merged = [dict(id=None, line=line, source_id=None,
                                        start_ms=start, end_ms=end, metadata=metadata,
                                        mapping_operation=op, source_ids=ids,
                                        source_identifiers=values, text=text)
                                        for line, start, end, op, ids, values, text in [
                                            (2, 6000 if kept else 1000, 9000 if kept else 3000,
                                             2, ['c4', 'c5'], identifiers[1], 'C\nD'),
                                            (3 if kept else 4, 12000 if kept else 6000,
                                             15000 if kept else 9000, 1, ['c1', 'c2'], identifiers[0], 'A\nB')]]
                                    entries = ([plain, *merged] if kept else [merged[0], plain, merged[1]])
                                    if new == 'add':
                                        entries.insert(0, dict(id=None, line=1, source_id=None,
                                            start_ms=0, end_ms=100, metadata={}))
                                    self.assertEqual(report['output_cues'],
                                        [{format + '_index': i, **cue} for i, cue in enumerate(entries, 1)])
                                    if not any(v is not None for pair in identifiers for v in pair):
                                        self.assertFalse(any(loss['kind'] == 'merged_cue_identifiers'
                                                             for loss in report['losses']))
                                    self.assertEqual((sidecar, mapping), original)

    def test_srt_original_numbers_are_aligned_for_both_output_formats(self):
        _, sidecar = export_subtitles(b'17\n00:00:01,000 --> 00:00:02,000\nA\n\n'
                                     b'8\n00:00:02,000 --> 00:00:03,000\nB\n', 'srt')
        labels = '4\t6\tA / B\n'
        mapping = {'schema': 'audacity-subtitle-bridge-mapping/1',
                   'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                   'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                   'operations': [{'kind': 'merge', 'sources': ['17', '8'],
                                   'separator': '\n', 'target': 1}]}
        for format in ('srt', 'vtt'):
            _, report = import_labels(labels, sidecar, mapping=mapping, output_format=format)
            self.assertEqual(report['output_cues'], [{format + '_index': 1, 'id': None,
                'line': 1, 'source_id': None, 'start_ms': 4000, 'end_ms': 6000, 'metadata': {},
                'mapping_operation': 1, 'source_ids': ['17', '8'],
                'source_identifiers': ['17', '8'], 'text': 'A\nB'}])
            self.assertEqual(report['losses'], [])
            self.assertEqual(report['warnings'], [])


if __name__ == '__main__':
    unittest.main()
