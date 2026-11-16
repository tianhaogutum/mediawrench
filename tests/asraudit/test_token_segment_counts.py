"""Per-populated-flag row counts."""

from copy import deepcopy
import itertools
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import audit
from .test_token_timed_only import FORMAT, mixed_native


def counts(segment, rows, timed):
    return dict(segment=segment, token_rows=rows, timed_token_rows=timed,
                untimed_token_rows=rows - timed)


MIXED_COUNTS = [counts(2, 3, 1), counts(3, 2, 0), counts(5, 1, 1),
                counts(7, 2, 1), counts(8, 1, 0), counts(10, 1, 1)]


class SegmentTokenCountsTests(unittest.TestCase):
    def assert_counts(self, document, expected, fmt=FORMAT):
        before = deepcopy(document)
        report = audit(document, input_format=fmt)
        summary = report['token_evidence_summary']
        actual = summary['segment_token_counts']
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), summary['segments_with_tokens'])
        self.assertEqual([item['segment'] for item in actual], [
            flag['segment'] for flag in report['flags']
            if flag['token_evidence'] is not None and flag['token_evidence']['tokens']])
        for key in ('token_rows', 'timed_token_rows', 'untimed_token_rows'):
            self.assertEqual(sum(item[key] for item in actual), summary[key])
        for item in actual:
            self.assertEqual(item['timed_token_rows'] + item['untimed_token_rows'], item['token_rows'])
        for key, predicate in (
                ('timed_only_segment_indices', lambda c: c['untimed_token_rows'] == 0),
                ('untimed_only_segment_indices', lambda c: c['timed_token_rows'] == 0),
                ('mixed_timing_segment_indices', lambda c: 0 < c['timed_token_rows'] < c['token_rows'])):
            self.assertEqual([item['segment'] for item in actual if predicate(item)], summary[key])
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)
        if fmt == FORMAT:
            for flag in report['flags']:
                if flag['token_evidence'] is not None:
                    self.assertEqual([entry['token'] for entry in flag['token_evidence']['tokens']],
                                     before['transcription'][flag['segment']]['tokens'])
        return report

    def test_source_order_and_exact_readme_fixture_counts(self):
        document = mixed_native()  # reverse timestamp/ID order with unflagged gaps
        report = self.assert_counts(document, MIXED_COUNTS)
        self.assertEqual([flag['segment'] for flag in report['flags']], [0, 2, 3, 4, 5, 7, 8, 10])
        self.assertTrue(all(flag['reasons'] == ['high_no_speech'] for flag in report['flags']))
        self.assertTrue(all(flag['avg_logprob'] is None for flag in report['flags']))
        self.assertEqual(report['segments'][1]['reasons'], [])
        fixture = Path(__file__).parent / 'fixtures/whisper_cpp_token_csv_v1_7_6.json'
        report = self.assert_counts(json.loads(fixture.read_bytes()), [counts(0, 3, 1), counts(1, 1, 1)])
        self.assertEqual(report['flags'][0]['reasons'], ['adjacent_repeat'])
        self.assertIsNone(report['flags'][0]['no_speech_prob'])

    def test_every_timing_mask_for_ordinary_special_and_mixed_rows(self):
        base = mixed_native()['transcription'][3]  # two untimed ordinary/special tokens
        for identities in ((0, 0, 0), (1, 1, 1), (0, 1, 0)):
            for mask in itertools.product((False, True), repeat=3):
                with self.subTest(identities=identities, mask=mask):
                    record = deepcopy(base)
                    record['tokens'] = [deepcopy(base['tokens'][i]) for i in identities]
                    for i, (token, timed) in enumerate(zip(record['tokens'], mask)):
                        token.update(p=i % 2, t_dtw=1e200 if i % 2 else -1)
                        if timed:
                            token.update(offsets={'from': 0, 'to': 0}, timestamps=
                                         dict.fromkeys(('from', 'to'), '00:00:00,000'))
                    self.assert_counts({'transcription': [record]}, [counts(0, 3, sum(mask))])

    def test_zero_reversed_outside_large_pairs_on_valid_and_invalid_segments(self):
        pairs = [
            ({'from': 0, 'to': 0}, dict.fromkeys(('from', 'to'), '00:00:00,000')),
            ({'from': 12000, 'to': 11000}, {'from': '00:00:12,000', 'to': '00:00:11,000'}),
            ({'from': 9007199254740991, 'to': 11000},
             {'from': '2501999792:59:00,991', 'to': '00:00:11,000'}),
        ]
        for offsets, timestamps in pairs:
            for invalid in (False, True):
                with self.subTest(offsets=offsets, invalid=invalid):
                    document = mixed_native()
                    record = document['transcription'][3]
                    record['tokens'][1].update(offsets=offsets, timestamps=timestamps)
                    if invalid:
                        record['offsets']['to'] = record['offsets']['from']
                        record['timestamps']['to'] = record['timestamps']['from']
                    expected = deepcopy(MIXED_COUNTS)
                    expected[1] = counts(3, 2, 1)
                    report = self.assert_counts(document, expected)
                    if invalid:
                        self.assertIn(3, report['unplaced_segments'])
                        self.assertFalse(any(3 in cue['segments'] for cue in report['review_ranges']))

    def test_missing_empty_default_opaque_unflagged_and_empty_documents(self):
        native = mixed_native()
        for index in (0, 4):
            self.assert_counts({'transcription': [native['transcription'][index]]}, [])
        default = {'segments': [dict(start=i*10, end=i*10+10, text=f'item {i}',
                                    no_speech_prob=.9) for i in range(5)]}
        for record, value in zip(default['segments'],
                                 (native['transcription'][2]['tokens'], [], 'opaque', None)):
            record['tokens'] = deepcopy(value)
        report = self.assert_counts(default, [], 'segments')
        self.assertEqual(report['token_evidence_summary']['unavailable_segment_indices'], list(range(5)))
        for document, key, fmt in ((native, 'transcription', FORMAT), (default, 'segments', 'segments')):
            for record in document[key]:
                record.pop('no_speech_prob', None)
            self.assertEqual(self.assert_counts(document, [], fmt)['flags'], [])
            self.assert_counts({key: []}, [], fmt)


if __name__ == '__main__':
    unittest.main()
