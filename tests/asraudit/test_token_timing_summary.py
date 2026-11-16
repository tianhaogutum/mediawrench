"""Raw timing-presence contracts."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import audit

FORMAT = 'whisper.cpp-json-v1.7.6'
FIXTURE = Path(__file__).parent / 'fixtures/whisper_cpp_token_csv_v1_7_6.json'
MIXED = dict(flagged_segments=4, unavailable_segments=1, empty_segments=1,
             segments_with_tokens=2, token_rows=4, timed_token_rows=2, untimed_token_rows=2,
             unavailable_segment_indices=[3], empty_segment_indices=[4], untimed_only_segment_indices=[], mixed_timing_segment_indices=[0], timed_only_segment_indices=[1],
            timed_token_source_paths=['transcription[0].tokens[0]', 'transcription[1].tokens[0]'],
            untimed_token_source_paths=['transcription[0].tokens[1]', 'transcription[0].tokens[2]'],
            segment_token_counts=[
                {'segment': 0, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2},
                {'segment': 1, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0}])
ZERO = dict(flagged_segments=0, unavailable_segments=0, empty_segments=0,
            segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
            unavailable_segment_indices=[], empty_segment_indices=[], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
            untimed_token_source_paths=[], segment_token_counts=[])


class TokenTimingSummaryTests(unittest.TestCase):
    def setUp(self):
        self.native = json.loads(FIXTURE.read_bytes())

    def assert_summary(self, document, expected, fmt=FORMAT):
        before = deepcopy(document)
        report = audit(document, input_format=fmt)
        summary = report['token_evidence_summary']
        self.assertEqual(summary, expected)
        self.assertEqual(summary['timed_token_rows'] + summary['untimed_token_rows'], summary['token_rows'])
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)
        return report

    def test_mixed_timed_untimed_special_and_unflagged_rows(self):
        # Add an unflagged untimed token beside the unflagged timed token.
        self.native['transcription'][2]['tokens'].append(
            deepcopy(self.native['transcription'][0]['tokens'][1]))
        report = self.assert_summary(self.native, MIXED)
        self.assertEqual([f['segment'] for f in report['flags']], [0, 1, 3, 4])
        self.assertEqual(report['segments'][2]['reasons'], [])
        self.assertEqual(report['flags'][0]['reasons'], ['adjacent_repeat'])
        for flag in report['flags']:
            self.assertIsNone(flag['avg_logprob'])
            view = flag['token_evidence']
            if view is not None:
                self.assertEqual([entry['token'] for entry in view['tokens']],
                                 self.native['transcription'][flag['segment']]['tokens'])
        self.assertTrue(all(f['no_speech_prob'] is None for f in report['flags'][:2]))

    def test_zero_reversed_out_of_segment_and_large_pairs_still_count_as_timed(self):
        pairs = [
            ({'from': 0, 'to': 0}, {'from': '00:00:00,000', 'to': '00:00:00,000'}),
            ({'from': 12000, 'to': 11000}, {'from': '00:00:12,000', 'to': '00:00:11,000'}),
            ({'from': 9007199254740991, 'to': 11000},
             {'from': '2501999792:59:00,991', 'to': '00:00:11,000'}),
        ]
        for offsets, timestamps in pairs:
            for invalid_segment in (False, True):
                with self.subTest(offsets=offsets, invalid_segment=invalid_segment):
                    document = deepcopy(self.native)
                    record = document['transcription'][0]
                    token = record['tokens'][0]
                    token.update(offsets=offsets, timestamps=timestamps)
                    if invalid_segment:
                        record['offsets']['to'] = 0
                        record['timestamps']['to'] = '00:00:00,000'
                    expected = dict(MIXED, flagged_segments=3, segments_with_tokens=1,
                                    token_rows=3, timed_token_rows=1, timed_only_segment_indices=[],
                                    timed_token_source_paths=['transcription[0].tokens[0]'],
                                    untimed_token_source_paths=['transcription[0].tokens[1]', 'transcription[0].tokens[2]'],
                                    segment_token_counts=[
                {'segment': 0, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2}]) if invalid_segment else MIXED
                    report = self.assert_summary(document, expected)
                    self.assertEqual(report['flags'][0]['token_evidence']['tokens'][0]['token'], token)
                    if invalid_segment:
                        self.assertEqual(report['flags'][0]['reasons'], ['invalid_timestamp'])
                        self.assertIn(0, report['unplaced_segments'])
                        self.assertFalse(any(0 in r['segments'] for r in report['review_ranges']))

    def test_special_identity_probability_and_dtw_do_not_imply_timing(self):
        for timed_indices in ((), (1,), (0, 1, 2, 3)):
            with self.subTest(timed_indices=timed_indices):
                document = deepcopy(self.native)
                tokens = document['transcription'][0]['tokens'] + document['transcription'][1]['tokens']
                for i, token in enumerate(tokens):
                    token['p'] = 0 if i % 2 else 1
                    token['t_dtw'] = 1e200 if i % 2 else -1
                    token.pop('offsets', None)
                    token.pop('timestamps', None)
                    if i in timed_indices:
                        token['offsets'] = {'from': 0, 'to': 0}
                        token['timestamps'] = dict.fromkeys(('from', 'to'), '00:00:00,000')
                # Index 1 is [_EOT_]; special-token text/ID never decide timing.
                expected = dict(MIXED, timed_token_rows=len(timed_indices),
                                untimed_token_rows=4 - len(timed_indices),
                                untimed_only_segment_indices=([0, 1] if not timed_indices else
                                                              [1] if timed_indices == (1,) else []),
                                mixed_timing_segment_indices=[0] if timed_indices == (1,) else [],
                                timed_only_segment_indices=[0, 1] if len(timed_indices) == 4 else [],
                                timed_token_source_paths=[f'transcription[{s}].tokens[{j}]' for s, j, flat in ((0, 0, 0), (0, 1, 1), (0, 2, 2), (1, 0, 3)) if flat in timed_indices],
                                untimed_token_source_paths=[f'transcription[{s}].tokens[{j}]' for s, j, flat in ((0, 0, 0), (0, 1, 1), (0, 2, 2), (1, 0, 3)) if flat not in timed_indices],
                                segment_token_counts=[dict(segment=i, token_rows=n,
                                    timed_token_rows=t, untimed_token_rows=n-t) for i, n, t in
                                    ((0, 3, sum(j in timed_indices for j in range(3))),
                                     (1, 1, int(3 in timed_indices)))])
                report = self.assert_summary(document, expected)
                self.assertEqual(report['flags'][0]['reasons'], ['adjacent_repeat'])
                self.assertIsNone(report['flags'][0]['no_speech_prob'])

    def test_unavailable_empty_default_metadata_and_no_flags_have_zero_rows(self):
        for index, unavailable, empty in ((3, 1, 0), (4, 0, 1)):
            self.assert_summary({'transcription': [self.native['transcription'][index]]}, dict(
                ZERO, flagged_segments=1, unavailable_segments=unavailable, empty_segments=empty,
                unavailable_segment_indices=[0] if unavailable else [], empty_segment_indices=[0] if empty else [], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
                untimed_token_source_paths=[], segment_token_counts=[]))
        default = {'segments': [dict(start=i*10, end=i*10+10, text=f'item {i}', no_speech_prob=.9)
                                for i in range(5)]}
        for record, value in zip(default['segments'],
                                 (self.native['transcription'][0]['tokens'], [], 'opaque', None)):
            record['tokens'] = deepcopy(value)
        self.assert_summary(default, dict(ZERO, flagged_segments=5, unavailable_segments=5,
                                         unavailable_segment_indices=[0, 1, 2, 3, 4]), 'segments')
        for record in default['segments']:
            record.pop('no_speech_prob')
        for document, fmt in ((default, 'segments'), ({'segments': []}, 'segments'),
                              ({'transcription': []}, FORMAT),
                              ({'transcription': [self.native['transcription'][2]]}, FORMAT)):
            with self.subTest(document=document, fmt=fmt):
                self.assert_summary(document, ZERO, fmt)


if __name__ == '__main__':
    unittest.main()
