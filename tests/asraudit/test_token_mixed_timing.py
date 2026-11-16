"""Mixed raw timing in populated flagged views."""

from copy import deepcopy
import itertools
import unittest

from mediawrench.asraudit import audit
from .test_token_untimed_only import FORMAT, mixed_native as prior_native


def mixed_native():
    document = prior_native()
    records = document['transcription']
    timed = deepcopy(records[5]['tokens'][0])  # timed special, zero interval
    records[1]['tokens'].append(deepcopy(timed))  # unflagged mixed view
    records[7]['tokens'].append(timed)  # second flagged mixed view
    return document


class MixedTimingSummaryTests(unittest.TestCase):
    def assert_indices(self, document, expected, fmt=FORMAT):
        before = deepcopy(document)
        report = audit(document, input_format=fmt)
        self.assertEqual(report['token_evidence_summary']['mixed_timing_segment_indices'], expected)
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)
        for flag in report['flags']:
            if flag['token_evidence'] is not None:
                self.assertEqual([entry['token'] for entry in flag['token_evidence']['tokens']],
                                 before['transcription'][flag['segment']]['tokens'])
        return report

    def test_source_order_with_unflagged_gap_and_all_other_categories(self):
        report = self.assert_indices(mixed_native(), [2, 7])
        self.assertEqual([flag['segment'] for flag in report['flags']], [0, 2, 3, 4, 5, 7, 8, 10])
        self.assertEqual(report['token_evidence_summary'], dict(
            flagged_segments=8, unavailable_segments=1, empty_segments=1,
            segments_with_tokens=6, token_rows=10, timed_token_rows=4, untimed_token_rows=6,
            unavailable_segment_indices=[0], empty_segment_indices=[4],
            untimed_only_segment_indices=[3, 8], mixed_timing_segment_indices=[2, 7], timed_only_segment_indices=[5, 10],
            timed_token_source_paths=['transcription[2].tokens[0]', 'transcription[5].tokens[0]', 'transcription[7].tokens[1]', 'transcription[10].tokens[0]'],
            untimed_token_source_paths=['transcription[2].tokens[1]', 'transcription[2].tokens[2]', 'transcription[3].tokens[0]', 'transcription[3].tokens[1]', 'transcription[7].tokens[0]', 'transcription[8].tokens[0]'],
            segment_token_counts=[
                {'segment': 2, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2},
                {'segment': 3, 'token_rows': 2, 'timed_token_rows': 0, 'untimed_token_rows': 2},
                {'segment': 5, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0},
                {'segment': 7, 'token_rows': 2, 'timed_token_rows': 1, 'untimed_token_rows': 1},
                {'segment': 8, 'token_rows': 1, 'timed_token_rows': 0, 'untimed_token_rows': 1},
                {'segment': 10, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0}]))
        self.assertTrue(all(flag['reasons'] == ['high_no_speech'] for flag in report['flags']))
        self.assertEqual(report['segments'][1]['reasons'], [])

    def test_every_timing_mask_for_ordinary_special_and_mixed_identity(self):
        # The same timing masks apply to ordinary-only, special-only and mixed rows.
        base = prior_native()['transcription'][3]
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
                    report = self.assert_indices({'transcription': [record]},
                                                 [0] if 0 < sum(mask) < 3 else [])
                    summary = report['token_evidence_summary']
                    self.assertEqual(summary['timed_token_rows'], sum(mask))
                    self.assertEqual(summary['untimed_token_rows'], 3 - sum(mask))
                    self.assertEqual(summary['untimed_only_segment_indices'], [] if any(mask) else [0])

    def test_zero_reversed_outside_and_large_pairs_on_valid_and_invalid_segments(self):
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
                    report = self.assert_indices(document, [2, 3, 7])
                    if invalid:
                        self.assertIn(3, report['unplaced_segments'])
                        self.assertFalse(any(3 in cue['segments'] for cue in report['review_ranges']))

    def test_missing_empty_default_opaque_no_flags_and_empty_documents(self):
        native = mixed_native()
        for index in (0, 4, 5, 8, 10):
            self.assert_indices({'transcription': [native['transcription'][index]]}, [])
        default = {'segments': [dict(start=i*10, end=i*10+10, text=f'item {i}',
                                    no_speech_prob=.9) for i in range(5)]}
        for record, value in zip(default['segments'],
                                 (native['transcription'][2]['tokens'], [], 'opaque', None)):
            record['tokens'] = value
        report = self.assert_indices(default, [], 'segments')
        self.assertEqual(report['token_evidence_summary']['unavailable_segment_indices'], list(range(5)))
        for document, key, fmt in ((native, 'transcription', FORMAT), (default, 'segments', 'segments')):
            for record in document[key]:
                record.pop('no_speech_prob', None)
            self.assertEqual(self.assert_indices(document, [], fmt)['flags'], [])
            self.assert_indices({key: []}, [], fmt)


if __name__ == '__main__':
    unittest.main()
