"""Exact report coverage contracts over all source segments, including unflagged ones."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import audit
from mediawrench.asraudit.output import csv_flags, review_srt

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
ZERO = {'total_segments': 0, 'limited_segments': 0,
        'limited_segment_indices': [], 'max_searched_pattern_tokens': 0}


def document(text):
    return {'segments': [{'start': 0, 'end': 100, 'text': text}]}


class LoopSearchSummaryTests(unittest.TestCase):
    def test_zero_segments_in_both_formats(self):
        for source, input_format in [({'segments': []}, 'segments'),
                                     ({'transcription': []}, 'whisper.cpp-json-v1.7.6')]:
            for config in (None, {'default': {'max_pattern_tokens': 1}}):
                with self.subTest(input_format=input_format, config=config):
                    report = audit(source, input_format=input_format, loop_config=config)
                    self.assertEqual(report['loop_search_summary'], ZERO)
                    self.assertEqual(report['segments'], [])

    def test_empty_and_too_short_segments_are_counted_but_not_limited(self):
        for text in ('', ' \n\t', 'one two'):
            with self.subTest(text=text):
                report = audit(document(text), loop_config={'default': {'max_pattern_tokens': 1}})
                self.assertEqual(report['loop_search_summary'], {**ZERO, 'total_segments': 1})
                self.assertEqual(report['flags'], [])

    def test_full_and_equal_or_above_feasible_caps_are_not_limited(self):
        for config in (None, {'default': {'max_pattern_tokens': None}},
                       {'default': {'max_pattern_tokens': 3}},
                       {'default': {'max_pattern_tokens': 100}}):
            with self.subTest(config=config):
                report = audit(document('Thank you. Thank you. Thank you.'), loop_config=config)
                self.assertEqual(report['loop_search_summary'], {
                    **ZERO, 'total_segments': 1, 'max_searched_pattern_tokens': 3})
                self.assertEqual(report['flags'][0]['reasons'], ['intra_segment_loop'])

    def test_bounded_unflagged_segment_counts_as_limited(self):
        report = audit(document('Thank you. Thank you. Thank you.'),
                       loop_config={'default': {'max_pattern_tokens': 2}})
        self.assertEqual(report['loop_search_summary'], {
            'total_segments': 1, 'limited_segments': 1,
            'limited_segment_indices': [0], 'max_searched_pattern_tokens': 2})
        self.assertEqual(report['flags'], [])
        self.assertEqual(report['segments'][0]['intra_segment_loops'], [])

    def test_mixed_languages_source_order_and_reduction(self):
        source = {'language': 'en', 'segments': [
            {'start': 70 - i*10, 'end': 80 - i*10, 'text': text, 'id': 90 - i, **metadata}
            for i, (text, metadata) in enumerate([
                ('Thank you. ' * 3, {}),
                ('one two three four five six seven eight nine', {'language': 'de'}),
                ('欢迎收听，' * 3, {'language': 'zh'}),
                ('alpha beta gamma delta epsilon zeta', {'language': 'fr'}),
                ('one two', {'language': 'de'}),
                ('', {'language': 'de'}),
                ('go away ' * 3, {'language': 'xx'})])]}
        config = {'default': {'max_pattern_tokens': 2}, 'languages': {
            'en': {'max_pattern_tokens': None}, 'de': {},
            'zh': {'unit': 'characters', 'min_pattern_units': 4, 'max_pattern_tokens': 5},
            'fr': {'max_pattern_tokens': 1}}}
        before = deepcopy((source, config))
        report = audit(source, loop_config=config)
        self.assertEqual(report['loop_search_summary'], {
            'total_segments': 7, 'limited_segments': 2,
            'limited_segment_indices': [1, 3], 'max_searched_pattern_tokens': 5})
        self.assertEqual([s['loop_search']['searched_max_pattern_tokens'] for s in report['segments']],
                         [3, 2, 5, 1, 0, 0, 2])
        self.assertEqual([f['segment'] for f in report['flags']], [0, 2, 6])
        self.assertEqual((source, config), before)
        indices = [s['segment'] for s in report['segments'] if s['loop_search']['limited_by_config']]
        self.assertEqual(report['loop_search_summary']['limited_segment_indices'], indices)
        self.assertEqual(report['loop_search_summary']['limited_segments'], len(indices))
        self.assertEqual(report['loop_search_summary']['total_segments'], len(report['segments']))
        self.assertEqual(report['loop_search_summary']['max_searched_pattern_tokens'],
                         max(s['loop_search']['searched_max_pattern_tokens'] for s in report['segments']))

    def test_native_invalid_timing_and_sidecar_semantics(self):
        source = json.loads((FIXTURES / 'whisper_cpp_token_csv_v1_7_6.json').read_text())
        source['transcription'][2]['text'] = 'one two three four five six seven eight nine'
        source['transcription'][2]['offsets'] = {'from': 0, 'to': 0}
        source['transcription'][2]['timestamps'] = {'from': '00:00:00,000', 'to': '00:00:00,000'}
        report = audit(source, input_format='whisper.cpp-json-v1.7.6',
                       loop_config={'default': {'max_pattern_tokens': 2}})
        self.assertEqual(report['loop_search_summary'], {
            'total_segments': 5, 'limited_segments': 1,
            'limited_segment_indices': [2], 'max_searched_pattern_tokens': 2})
        self.assertEqual(report['unplaced_segments'], [2])
        self.assertEqual(report['segments'][2]['reasons'], ['invalid_timestamp'])
        # A report-level JSON addition cannot become a flag CSV column or SRT cue.
        old_shape = {key: value for key, value in report.items() if key != 'loop_search_summary'}
        self.assertEqual(csv_flags(report), csv_flags(old_shape))
        self.assertEqual(review_srt(report), review_srt(old_shape))
        self.assertEqual(report['source_document'], source)


if __name__ == '__main__':
    unittest.main()
