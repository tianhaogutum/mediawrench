"""Exact timed token locators over flags."""

from copy import deepcopy
import itertools
import json
import unittest

from mediawrench.asraudit import AuditError, audit
from .test_token_timed_only import FORMAT, mixed_native
from .test_token_untimed_only import FIXTURE


def paths(*positions):
    return [f'transcription[{segment}].tokens[{token}]' for segment, token in positions]


MIXED_PATHS = paths((2, 0), (5, 0), (7, 1), (10, 0))


class TimedTokenSourcePathsTests(unittest.TestCase):
    def assert_paths(self, document, expected, fmt=FORMAT):
        before = deepcopy(document)
        report = audit(document, input_format=fmt)
        summary = report['token_evidence_summary']
        actual = summary['timed_token_source_paths']
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), summary['timed_token_rows'])
        self.assertEqual(len(actual), len(set(actual)))
        untimed = summary['untimed_token_source_paths']
        self.assertTrue(set(actual).isdisjoint(untimed))
        for counts in summary['segment_token_counts']:
            prefix = f"transcription[{counts['segment']}].tokens["
            self.assertEqual(sum(path.startswith(prefix) for path in actual), counts['timed_token_rows'])
        entries = [entry for flag in report['flags'] if flag['token_evidence'] is not None
                   for entry in flag['token_evidence']['tokens']]
        self.assertEqual(actual, [entry['source_path'] for entry in entries
                                  if 'offsets' in entry['token'] and 'timestamps' in entry['token']])
        self.assertEqual(set(actual) | set(untimed), {entry['source_path'] for entry in entries})
        self.assertEqual(len(actual) + len(untimed), summary['token_rows'])
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)
        if fmt == FORMAT:
            for flag in report['flags']:
                if flag['token_evidence'] is not None:
                    self.assertEqual([entry['token'] for entry in flag['token_evidence']['tokens']],
                                     before['transcription'][flag['segment']]['tokens'])
        return report

    def test_exact_source_order_with_gaps_reversed_times_ids_and_readme(self):
        document = mixed_native()
        report = self.assert_paths(document, MIXED_PATHS)
        self.assertEqual([flag['segment'] for flag in report['flags']], [0, 2, 3, 4, 5, 7, 8, 10])
        self.assertTrue(all(flag['reasons'] == ['high_no_speech'] for flag in report['flags']))
        self.assertTrue(all(flag['avg_logprob'] is None for flag in report['flags']))
        for field in ('offsets', 'timestamps'):
            del document['transcription'][10]['tokens'][0][field]
        self.assert_paths(document, MIXED_PATHS[:-1])
        record = document['transcription'][3]
        record['tokens'] = [deepcopy(document['transcription'][5]['tokens'][0]) for _ in range(12)]
        self.assert_paths({'transcription': [record]}, paths(*((0, i) for i in range(12))))
        # Segment indices past 9 must also retain numeric source order.
        records = [deepcopy(record) for _ in range(12)]
        for i, item in enumerate(records):
            item['text'] = f'unique {i}'
            item['tokens'] = item['tokens'][:1]
        self.assert_paths({'transcription': records}, paths(*((i, 0) for i in range(12))))
        report = self.assert_paths(json.loads(FIXTURE.read_bytes()), paths((0, 0), (1, 0)))
        self.assertEqual(report['flags'][0]['reasons'], ['adjacent_repeat'])
        self.assertIsNone(report['flags'][0]['no_speech_prob'])

    def test_every_timing_mask_for_ordinary_special_and_mixed_rows(self):
        base = mixed_native()['transcription'][3]
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
                    self.assert_paths({'transcription': [record]},
                                      paths(*((0, i) for i, timed in enumerate(mask) if timed)))

    def test_zero_reversed_outside_large_pairs_included_on_valid_and_invalid_segments(self):
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
                    report = self.assert_paths(document, paths((2, 0), (3, 1), (5, 0), (7, 1), (10, 0)))
                    if invalid:
                        self.assertIn(3, report['unplaced_segments'])
                        self.assertFalse(any(3 in cue['segments'] for cue in report['review_ranges']))

    def test_missing_empty_default_opaque_unflagged_and_empty_documents(self):
        native = mixed_native()
        for index in (0, 3, 4, 8):
            self.assert_paths({'transcription': [native['transcription'][index]]}, [])
        # A populated untimed unflagged view is also excluded.
        native['transcription'][1]['tokens'] = deepcopy(native['transcription'][3]['tokens'])
        self.assert_paths(native, MIXED_PATHS)
        default = {'segments': [dict(start=i*10, end=i*10+10, text=f'item {i}',
                                    no_speech_prob=.9) for i in range(5)]}
        for record, value in zip(default['segments'],
                                 (native['transcription'][3]['tokens'], [], 'opaque', None)):
            record['tokens'] = deepcopy(value)
        report = self.assert_paths(default, [], 'segments')
        self.assertEqual(report['token_evidence_summary']['unavailable_segment_indices'], list(range(5)))
        for document, key, fmt in ((native, 'transcription', FORMAT), (default, 'segments', 'segments')):
            for record in document[key]:
                record.pop('no_speech_prob', None)
            self.assertEqual(self.assert_paths(document, [], fmt)['flags'], [])
            self.assert_paths({key: []}, [], fmt)

    def test_one_sided_or_null_timing_is_still_rejected_even_unflagged(self):
        for field in ('offsets', 'timestamps'):
            for value in (None, {}):
                with self.subTest(field=field, value=value):
                    document = mixed_native()
                    token = document['transcription'][1]['tokens'][0]
                    token.pop('offsets', None)
                    token.pop('timestamps', None)
                    token[field] = value
                    with self.assertRaisesRegex(AuditError, r'transcription\[1\]\.tokens\[0\]\.'):
                        audit(document, input_format=FORMAT)


if __name__ == '__main__':
    unittest.main()
