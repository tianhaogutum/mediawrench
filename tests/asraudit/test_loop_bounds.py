"""Synthetic contracts for explicitly bounded period search; no timing benchmarks."""
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.asraudit import AuditError, audit

ROOT = Path(__file__).resolve().parents[2]


def document(text, **metadata):
    return {'segments': [{'start': 0, 'end': 100, 'text': text}], **metadata}


def bounded(text, maximum, **rule):
    return audit(document(text), loop_config={'default': {
        'max_pattern_tokens': maximum, **rule}})


class LoopBoundTests(unittest.TestCase):
    def test_inclusive_cap_counts_punctuation_and_discloses_omitted_periods(self):
        text = 'Thank you. Thank you. Thank you.'
        limited = bounded(text, 2)
        self.assertEqual(limited['segments'][0]['intra_segment_loops'], [])
        self.assertEqual(limited['flags'], [])
        self.assertEqual(limited['segments'][0]['loop_search'], {
            'token_count': 9, 'max_possible_pattern_tokens': 3,
            'searched_max_pattern_tokens': 2, 'limited_by_config': True})
        inclusive = bounded(text, 3)
        self.assertEqual(inclusive['flags'][0]['loop_settings']['max_pattern_tokens'], 3)
        self.assertEqual(inclusive['flags'][0]['intra_segment_loops'][0]['pattern_tokens'],
                         ['thank', 'you', '.'])
        self.assertFalse(inclusive['segments'][0]['loop_search']['limited_by_config'])
        self.assertEqual(inclusive['source_document'], document(text))

    def test_default_and_explicit_null_preserve_full_search(self):
        text = 'go go go away ' * 3
        implicit = audit(document(text))
        explicit = bounded(text, None)
        self.assertEqual(implicit, explicit)
        self.assertIsNone(implicit['loop_config']['default']['max_pattern_tokens'])
        item = implicit['segments'][0]
        self.assertEqual(item['loop_search'], {
            'token_count': 12, 'max_possible_pattern_tokens': 4,
            'searched_max_pattern_tokens': 4, 'limited_by_config': False})
        self.assertEqual(item['intra_segment_loops'][0]['pattern_tokens'], ['go', 'go', 'go', 'away'])

    def test_full_tail_is_scanned_with_original_offsets_and_all_copies(self):
        prefix = '🎤 ' + ' '.join(f'unique{i}' for i in range(80)) + '\n'
        text = prefix + 'ＧＯ away\tgo AWAY\ngo away go away go away go away'
        item = bounded(text, 2)['segments'][0]
        loop, = item['intra_segment_loops']
        self.assertEqual((loop['start'], loop['end']), (len(prefix), len(text)))
        self.assertEqual((loop['repeat_count'], loop['severity']), (6, 'high'))
        self.assertEqual(item['loop_search']['token_count'], 93)
        self.assertEqual(item['loop_search']['searched_max_pattern_tokens'], 2)
        self.assertTrue(item['loop_search']['limited_by_config'])
        for occurrence in loop['occurrences']:
            self.assertEqual(text[occurrence['start']:occurrence['end']], occurrence['text'])

    def test_character_mode_and_minimum_are_distinct_from_token_cap(self):
        text = '欢迎收听，' * 3
        self.assertEqual(bounded(text, 4, unit='characters', min_pattern_units=4)
                         ['segments'][0]['intra_segment_loops'], [])
        loop, = bounded(text, 5, unit='characters', min_pattern_units=4)['flags'][0]['intra_segment_loops']
        self.assertEqual((loop['pattern_units'], len(loop['pattern_tokens'])), (4, 5))
        self.assertEqual([o['text'] for o in loop['occurrences']], ['欢迎收听，'] * 3)
        # A legal cap below the lexical minimum cannot manufacture a larger period.
        self.assertEqual(bounded('yes ' * 12, 1)['segments'][0]['intra_segment_loops'], [])
        loop, = bounded('yes ' * 12, 1, min_pattern_units=1)['flags'][0]['intra_segment_loops']
        self.assertEqual((loop['pattern_units'], loop['repeat_count']), (1, 12))

    def test_selection_is_among_searched_periods(self):
        text = 'go go go away ' * 3
        self.assertEqual(bounded(text, 2)['segments'][0]['intra_segment_loops'], [])
        item = bounded(text, 2, min_pattern_units=1)['segments'][0]
        self.assertEqual([loop['pattern_tokens'] for loop in item['intra_segment_loops']], [['go']] * 3)
        self.assertEqual([loop['repeat_count'] for loop in item['intra_segment_loops']], [3] * 3)

    def test_language_inheritance_reset_and_no_mutation(self):
        source = document('Thank you. ' * 3, language='en')
        source['segments'] += [dict(source['segments'][0], language='de'),
                               dict(source['segments'][0], language='en-US')]
        config = {'default': {'max_pattern_tokens': 2},
                  'languages': {'en': {'max_pattern_tokens': None}, 'de': {}}}
        before_source, before_config = deepcopy(source), deepcopy(config)
        report = audit(source, loop_config=config)
        self.assertEqual([len(s['intra_segment_loops']) for s in report['segments']], [1, 0, 0])
        self.assertEqual([s['loop_settings']['max_pattern_tokens'] for s in report['segments']], [None, 2, 2])
        self.assertEqual([s['loop_search']['limited_by_config'] for s in report['segments']], [False, True, True])
        self.assertEqual(report['loop_config']['languages']['de']['max_pattern_tokens'], 2)
        self.assertEqual((source, config), (before_source, before_config))

    def test_empty_short_large_cap_and_repeat_count_change_effective_bound(self):
        for text, count, possible in [('', 0, 0), (' \n ', 0, 0), ('one two', 2, 0),
                                       ('one two three four five', 5, 1)]:
            with self.subTest(text=text):
                search = bounded(text, 100)['segments'][0]['loop_search']
                self.assertEqual(search, {'token_count': count, 'max_possible_pattern_tokens': possible,
                                         'searched_max_pattern_tokens': possible, 'limited_by_config': False})
        report = bounded('come back ' * 4, 2, min_repetitions=4)
        self.assertEqual(report['segments'][0]['loop_search']['max_possible_pattern_tokens'], 2)
        self.assertEqual(report['flags'][0]['intra_segment_loops'][0]['repeat_count'], 4)

    def test_invalid_bounds_have_exact_default_and_language_paths(self):
        for value in (True, False, 0, -1, 2.0, '2', [], {}):
            for scope in ('default', 'languages.en'):
                with self.subTest(value=value, scope=scope):
                    rule = {'max_pattern_tokens': value}
                    config = {'default': rule} if scope == 'default' else {'languages': {'en': rule}}
                    with self.assertRaises(AuditError) as caught:
                        audit(document('fine'), loop_config=config)
                    self.assertEqual(str(caught.exception),
                                     f'loop_config.{scope}.max_pattern_tokens: expected null or integer >= 1')


class LoopBoundCLITests(unittest.TestCase):
    def test_cli_json_csv_search_provenance_dry_run_and_invalid_no_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            source, config = output / 'input.json', output / 'config.json'
            text = 'unique prefix. ' + 'come back ' * 6
            source.write_text(json.dumps(document(text)))
            config.write_text('{"default":{"max_pattern_tokens":2}}')
            original_source, original_config = source.read_bytes(), config.read_bytes()
            paths = [output / name for name in ('report.json', 'flags.csv', 'review.srt')]
            command = [sys.executable, '-m', 'mediawrench.asraudit', str(source), '--loop-config', str(config)]
            options = ['--json', str(paths[0]), '--csv', str(paths[1]), '--review-srt', str(paths[2])]
            run = subprocess.run(command + options, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(paths[0].read_text())
            flag, = report['flags']
            self.assertTrue(flag['loop_search']['limited_by_config'])
            row, = csv.DictReader(io.StringIO(paths[1].read_text()))
            for key in ('loop_search', 'loop_settings', 'intra_segment_loops'):
                self.assertEqual(json.loads(row[key]), flag[key])
            self.assertIn('intra_segment_loop', paths[2].read_text())
            self.assertEqual((source.read_bytes(), config.read_bytes()), (original_source, original_config))
            fresh = [output / name for name in ('absent.json', 'absent.csv', 'absent.srt')]
            options = ['--json', str(fresh[0]), '--csv', str(fresh[1]), '--review-srt', str(fresh[2])]
            dry = subprocess.run(command + options + ['--dry-run'], cwd=ROOT,
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(json.loads(dry.stdout)['segments'], report['segments'])
            self.assertFalse(any(p.exists() for p in fresh))
            config.write_text('{"languages":{"en":{"max_pattern_tokens":0}}}')
            failed = subprocess.run(command + options, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(failed.returncode, 2)
            self.assertIn('loop_config.languages.en.max_pattern_tokens: expected null or integer >= 1', failed.stderr)
            self.assertFalse(any(p.exists() for p in fresh))


if __name__ == '__main__':
    unittest.main()
