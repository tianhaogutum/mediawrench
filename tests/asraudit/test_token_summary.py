"""Flag-only token availability locations."""

from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import audit
from mediawrench.asraudit.output import csv_tokens


FORMAT = 'whisper.cpp-json-v1.7.6'
FIXTURE = Path(__file__).parent / 'fixtures/whisper_cpp_token_csv_v1_7_6.json'
ZERO = dict(flagged_segments=0, unavailable_segments=0, empty_segments=0,
            segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
            unavailable_segment_indices=[], empty_segment_indices=[], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
            untimed_token_source_paths=[], segment_token_counts=[])


def mixed_native():
    source = json.loads(FIXTURE.read_bytes())['transcription']
    # Repeated categories and gaps distinguish segment indices from flag positions.
    records = [deepcopy(source[i]) for i in (3, 2, 4, 0, 3, 4, 3, 4, 2)]
    for i, record in enumerate(records):
        start = (len(records) - i) * 10000  # reverse time and upstream ID order
        record.update(id=100 - i, text=f'unique segment {i}')
        record['offsets'] = {'from': start, 'to': start + 10000}
        record['timestamps'] = {}
        for endpoint, millis in record['offsets'].items():
            seconds = millis // 1000
            record['timestamps'][endpoint] = f'00:{seconds // 60:02}:{seconds % 60:02},000'
        if i in (0, 2, 3, 6, 7):
            record['no_speech_prob'] = .9
        else:
            record.pop('no_speech_prob', None)
    return {'transcription': records}


def default_metadata():
    records = [{'start': 50 - i * 10, 'end': 60 - i * 10, 'text': f'item {i}',
                'id': 100 - i, 'no_speech_prob': .9} for i in range(5)]
    for record, tokens in zip(records, ([], [{'opaque': 'not native'}], 'opaque', None)):
        record['tokens'] = tokens
    records[1].pop('no_speech_prob')  # unflagged opaque metadata stays excluded
    return {'segments': records}


class TokenSummaryTests(unittest.TestCase):
    def assert_locations(self, report, unavailable, empty):
        summary = report['token_evidence_summary']
        self.assertEqual(summary['unavailable_segment_indices'], unavailable)
        self.assertEqual(summary['empty_segment_indices'], empty)
        self.assertEqual(summary['unavailable_segments'], len(unavailable))
        self.assertEqual(summary['empty_segments'], len(empty))
        flags = report['flags']
        self.assertEqual(unavailable, [f['segment'] for f in flags if f['token_evidence'] is None])
        self.assertEqual(empty, [f['segment'] for f in flags
                                if f['token_evidence'] is not None and f['token_evidence']['tokens'] == []])

    def test_native_source_order_skips_unflagged_missing_empty_and_populated(self):
        document = mixed_native()
        before = deepcopy(document)
        report = audit(document, input_format=FORMAT)
        self.assertEqual([f['segment'] for f in report['flags']], [0, 2, 3, 6, 7])
        self.assert_locations(report, [0, 6], [2, 7])
        self.assertEqual(report['token_evidence_summary'], dict(
            flagged_segments=5, unavailable_segments=2, empty_segments=2,
            segments_with_tokens=1, token_rows=3, timed_token_rows=1, untimed_token_rows=2,
            unavailable_segment_indices=[0, 6], empty_segment_indices=[2, 7], untimed_only_segment_indices=[], mixed_timing_segment_indices=[3], timed_only_segment_indices=[],
            timed_token_source_paths=['transcription[3].tokens[0]'],
            untimed_token_source_paths=['transcription[3].tokens[1]', 'transcription[3].tokens[2]'],
            segment_token_counts=[
                {'segment': 3, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2}]))
        self.assertEqual(report['flags'][2]['token_evidence']['source_path'], 'transcription[3].tokens')
        rows = list(csv.DictReader(io.StringIO(csv_tokens(report), newline='')))
        self.assertEqual([int(row['segment']) for row in rows], [3, 3, 3])
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)

    def test_default_metadata_is_unavailable_even_when_explicitly_empty(self):
        document = default_metadata()
        before = deepcopy(document)
        report = audit(document)
        self.assert_locations(report, [0, 2, 3, 4], [])
        self.assertEqual(report['token_evidence_summary'], dict(
            flagged_segments=4, unavailable_segments=4, empty_segments=0,
            segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
            unavailable_segment_indices=[0, 2, 3, 4], empty_segment_indices=[], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
            untimed_token_source_paths=[], segment_token_counts=[]))
        self.assertEqual(document, before)
        self.assertEqual(report['source_document'], before)

    def test_no_flags_and_no_segments_in_both_formats(self):
        native = mixed_native()
        for record in native['transcription']:
            record.pop('no_speech_prob', None)
        default = default_metadata()
        for record in default['segments']:
            record.pop('no_speech_prob', None)
        for document, fmt in ((native, FORMAT), ({'transcription': []}, FORMAT),
                              (default, 'segments'), ({'segments': []}, 'segments')):
            with self.subTest(document=document, fmt=fmt):
                report = audit(document, input_format=fmt)
                self.assertEqual(report['flags'], [])
                self.assert_locations(report, [], [])
                self.assertEqual(report['token_evidence_summary'], ZERO)

    def test_existing_fixture_exact_counts_and_zero_row_locations(self):
        report = audit(json.loads(FIXTURE.read_bytes()), input_format=FORMAT)
        self.assert_locations(report, [3], [4])
        self.assertEqual(report['token_evidence_summary'], dict(
            flagged_segments=4, unavailable_segments=1, empty_segments=1,
            segments_with_tokens=2, token_rows=4, timed_token_rows=2, untimed_token_rows=2,
            unavailable_segment_indices=[3], empty_segment_indices=[4], untimed_only_segment_indices=[], mixed_timing_segment_indices=[0], timed_only_segment_indices=[1],
            timed_token_source_paths=['transcription[0].tokens[0]', 'transcription[1].tokens[0]'],
            untimed_token_source_paths=['transcription[0].tokens[1]', 'transcription[0].tokens[2]'],
            segment_token_counts=[
                {'segment': 0, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2},
                {'segment': 1, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0}]))


if __name__ == '__main__':
    unittest.main()
