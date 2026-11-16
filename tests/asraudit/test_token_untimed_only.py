"""Populated flag views with no supplied paired timing."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import audit

FORMAT = 'whisper.cpp-json-v1.7.6'
FIXTURE = Path(__file__).parent / 'fixtures/whisper_cpp_token_csv_v1_7_6.json'


def mixed_native():
    source = json.loads(FIXTURE.read_bytes())['transcription']
    ordinary = source[0]['tokens'][2]
    special = source[0]['tokens'][1]
    timed = source[1]['tokens'][0]
    timed_special = dict(deepcopy(special), offsets={'from': 0, 'to': 0},
                         timestamps=dict.fromkeys(('from', 'to'), '00:00:00,000'))
    # Missing, unflagged untimed, mixed, all-untimed, empty, timed special,
    # unflagged missing, ordinary-only, special-only, unflagged empty, all-timed.
    categories = [None, [ordinary], source[0]['tokens'], [ordinary, special], [],
                  [timed_special], None, [ordinary], [special], [], [timed]]
    records = []
    for i, tokens in enumerate(categories):
        start = (len(categories) - i) * 10000  # reverse time and upstream IDs
        record = dict(text=f'item {i}', id=100 - i,
                      offsets={'from': start, 'to': start + 10000}, timestamps={})
        for endpoint, millis in record['offsets'].items():
            seconds = millis // 1000
            record['timestamps'][endpoint] = f'00:{seconds // 60:02}:{seconds % 60:02},000'
        if i not in (1, 6, 9):
            record['no_speech_prob'] = .9
        if tokens is not None:
            record['tokens'] = deepcopy(tokens)
        records.append(record)
    return {'transcription': records}


class UntimedOnlySummaryTests(unittest.TestCase):
    def assert_indices(self, document, expected, fmt=FORMAT):
        before = deepcopy(document)
        report = audit(document, input_format=fmt)
        self.assertEqual(report['token_evidence_summary']['untimed_only_segment_indices'], expected)
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)
        return report

    def test_source_order_populated_only_skips_mixed_timed_and_unflagged(self):
        document = mixed_native()
        report = self.assert_indices(document, [3, 7, 8])
        self.assertEqual([flag['segment'] for flag in report['flags']], [0, 2, 3, 4, 5, 7, 8, 10])
        self.assertEqual(report['token_evidence_summary'], dict(
            flagged_segments=8, unavailable_segments=1, empty_segments=1,
            segments_with_tokens=6, token_rows=9, timed_token_rows=3, untimed_token_rows=6,
            unavailable_segment_indices=[0], empty_segment_indices=[4],
            untimed_only_segment_indices=[3, 7, 8], mixed_timing_segment_indices=[2], timed_only_segment_indices=[5, 10],
            timed_token_source_paths=['transcription[2].tokens[0]', 'transcription[5].tokens[0]', 'transcription[10].tokens[0]'],
            untimed_token_source_paths=['transcription[2].tokens[1]', 'transcription[2].tokens[2]', 'transcription[3].tokens[0]', 'transcription[3].tokens[1]', 'transcription[7].tokens[0]', 'transcription[8].tokens[0]'],
            segment_token_counts=[
                {'segment': 2, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2},
                {'segment': 3, 'token_rows': 2, 'timed_token_rows': 0, 'untimed_token_rows': 2},
                {'segment': 5, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0},
                {'segment': 7, 'token_rows': 1, 'timed_token_rows': 0, 'untimed_token_rows': 1},
                {'segment': 8, 'token_rows': 1, 'timed_token_rows': 0, 'untimed_token_rows': 1},
                {'segment': 10, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0}]))
        self.assertTrue(all(flag['reasons'] == ['high_no_speech'] for flag in report['flags']))
        for flag in report['flags']:
            view = flag['token_evidence']
            if view is not None:
                self.assertEqual([entry['token'] for entry in view['tokens']],
                                 document['transcription'][flag['segment']]['tokens'])

    def test_all_untimed_all_timed_and_special_probability_dtw_independence(self):
        for timed in (False, True):
            with self.subTest(timed=timed):
                document = mixed_native()
                for record in document['transcription']:
                    for i, token in enumerate(record.get('tokens', [])):
                        token.update(p=i % 2, t_dtw=1e200 if i % 2 else -1)
                        token.pop('offsets', None)
                        token.pop('timestamps', None)
                        if timed:
                            token.update(offsets={'from': 0, 'to': 0}, timestamps=
                                         dict.fromkeys(('from', 'to'), '00:00:00,000'))
                report = self.assert_indices(document, [] if timed else [2, 3, 5, 7, 8, 10])
                summary = report['token_evidence_summary']
                self.assertEqual(summary['timed_token_rows'], 9 if timed else 0)
                self.assertEqual(summary['untimed_token_rows'], 0 if timed else 9)

    def test_zero_reversed_out_of_segment_large_pairs_exclude_even_invalid_segments(self):
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
                    report = self.assert_indices(document, [7, 8])
                    if invalid:
                        self.assertIn(3, report['unplaced_segments'])
                        self.assertFalse(any(3 in cue['segments'] for cue in report['review_ranges']))

    def test_missing_empty_default_opaque_no_flags_and_empty_documents(self):
        native = mixed_native()
        for index in (0, 4):
            self.assert_indices({'transcription': [native['transcription'][index]]}, [])
        default = {'segments': [dict(start=i*10, end=i*10+10, text=f'item {i}',
                                    no_speech_prob=.9) for i in range(5)]}
        for record, value in zip(default['segments'], ([], [{'opaque': True}], 'opaque', None)):
            record['tokens'] = value
        self.assert_indices(default, [], 'segments')
        for document, key, fmt in ((native, 'transcription', FORMAT), (default, 'segments', 'segments')):
            for record in document[key]:
                record.pop('no_speech_prob', None)
            self.assertEqual(self.assert_indices(document, [], fmt)['flags'], [])
            self.assert_indices({key: []}, [], fmt)


if __name__ == '__main__':
    unittest.main()
