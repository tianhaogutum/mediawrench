"""Locally authored transcript-only integration cases, never ASR truth labels."""
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
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def document(text, **metadata):
    return {'segments': [{'start': 0, 'end': 100, 'text': text}], **metadata}


class LoopTests(unittest.TestCase):
    def test_explicit_ranges_and_suspected_evidence_without_vad(self):
        source = document('Thank you. Thank you. Thank you.')
        before = deepcopy(source)
        report = audit(source)
        item = report['segments'][0]
        self.assertEqual(item['reasons'], ['intra_segment_loop'])
        loop, = item['intra_segment_loops']
        self.assertEqual((loop['start'], loop['end']), (0, 32))
        self.assertEqual([(r['start'], r['end']) for r in loop['occurrences']],
                         [(0, 10), (11, 21), (22, 32)])
        self.assertEqual(loop['pattern_tokens'], ['thank', 'you', '.'])
        self.assertEqual((loop['pattern_units'], loop['repeat_count']), (2, 3))
        self.assertEqual(loop['severity'], 'medium')
        self.assertEqual(loop['assessment'], 'suspected_text_pattern')
        self.assertIsNone(item['outside_speech_fraction'])
        self.assertIsNone(item['speech_corroborated_repeat'])
        self.assertEqual(report['speech_corroborated_repeats'], [])
        self.assertEqual(report['source_document'], before)
        self.assertEqual(source, before)
        self.assertEqual(report['review_ranges'][0]['reasons'], ['intra_segment_loop'])

    def test_normal_repeats_punctuation_and_primitive_minimum(self):
        for text in ('Thank you. Thank you.', 'Thank you. Thank you! Thank you.',
                     'yes yes yes yes yes yes', 'We go home and then we go home.',
                     'one two one three one two', '!!! !!! !!!', '', '  '):
            with self.subTest(text=text):
                self.assertEqual(audit(document(text))['segments'][0]['intra_segment_loops'], [])
        config = {'default': {'min_pattern_units': 1}}
        loop, = audit(document('yes yes yes yes yes yes'), loop_config=config)['segments'][0]['intra_segment_loops']
        self.assertEqual((loop['pattern_units'], loop['repeat_count'], loop['severity']), (1, 6, 'high'))

    def test_original_offsets_survive_normalization_and_multiple_loops(self):
        text = '🎤 Intro: Ｔｈａｎｋ you.\nthank\tyou. THANK you. Stop. Go away! go away! go away!'
        item = audit(document(text))['segments'][0]
        first, second = item['intra_segment_loops']
        self.assertEqual([r['text'] for r in first['occurrences']],
                         ['Ｔｈａｎｋ you.', 'thank\tyou.', 'THANK you.'])
        self.assertEqual(first['start'], text.index('Ｔ'))
        self.assertEqual(second['start'], text.index('Go away'))
        for loop in item['intra_segment_loops']:
            for occurrence in loop['occurrences']:
                self.assertEqual(text[occurrence['start']:occurrence['end']], occurrence['text'])
        # Combining marks stay in their word: normalization never indexes a rewritten string.
        text = 'cafe\u0301 noir. café noir. CAFÉ noir.'
        loop, = audit(document(text))['segments'][0]['intra_segment_loops']
        self.assertEqual(loop['repeat_count'], 3)
        self.assertEqual(loop['occurrences'][0]['text'], 'cafe\u0301 noir.')

    def test_language_selection_thresholds_and_character_ranges(self):
        source = json.loads((FIXTURES / 'intra_segment_loops.json').read_text())
        config = json.loads((FIXTURES / 'loop_config.json').read_text())
        original = deepcopy(config)
        report = audit(source, loop_config=config)
        self.assertEqual(config, original)
        self.assertEqual([len(s['intra_segment_loops']) for s in report['segments']], [1, 1, 0, 0])
        item = report['segments'][1]
        loop, = item['intra_segment_loops']
        self.assertEqual([(o['start'], o['end']) for o in loop['occurrences']], [(0, 5), (5, 10), (10, 15)])
        self.assertEqual(loop['pattern_tokens'], list('欢迎收听，'))
        self.assertEqual(loop['pattern_units'], 4)
        self.assertEqual(item['loop_settings']['language'], 'zh')
        self.assertEqual(item['loop_settings']['language_source'], 'segment')
        self.assertEqual(item['loop_settings']['rule_source'], 'languages.zh')
        self.assertEqual(report['segments'][0]['loop_settings']['language_source'], 'document')
        self.assertEqual(report['loop_config']['languages']['zh']['unit'], 'characters')
        self.assertEqual(report['loop_config']['default']['min_repetitions'], 3)
        self.assertEqual(report['loop_config']['default']['high_severity_repetitions'], 6)
        # No implicit region fallback or language guessing; unknown metadata uses default.
        fallback = audit(document('欢迎收听欢迎收听欢迎收听', language='zh-CN'), loop_config=config)
        self.assertEqual(fallback['segments'][0]['intra_segment_loops'], [])
        self.assertEqual(fallback['segments'][0]['loop_settings']['rule_source'], 'default')
        # Explicit default character rule works without language metadata.
        direct = audit(document('欢迎收听欢迎收听欢迎收听'),
                       loop_config={'default': {'unit': 'characters', 'min_pattern_units': 4}})
        self.assertEqual(direct['segments'][0]['intra_segment_loops'][0]['repeat_count'], 3)
        raised = audit(document('go away go away go away'),
                       loop_config={'default': {'min_repetitions': 4}})
        self.assertEqual(raised['segments'][0]['intra_segment_loops'], [])

    def test_longest_primitive_loop_and_invalid_timing_remain_reviewable(self):
        item = audit(document('go go go away go go go away go go go away'))['segments'][0]
        loop, = item['intra_segment_loops']
        self.assertEqual(loop['pattern_tokens'], ['go', 'go', 'go', 'away'])
        self.assertEqual(loop['repeat_count'], 3)
        source = document('Thank you. Thank you. Thank you.')
        source['segments'][0]['end'] = 0
        report = audit(source)
        self.assertEqual(report['segments'][0]['reasons'], ['invalid_timestamp', 'intra_segment_loop'])
        self.assertEqual(report['unplaced_segments'], [0])
        self.assertEqual(report['review_ranges'], [])
        # Even supplied empty VAD never proves a text loop to be a hallucination.
        report = audit(document('Thank you. Thank you. Thank you.'), [])
        self.assertEqual(report['flags'][0]['intra_segment_loops'][0]['assessment'], 'suspected_text_pattern')
        self.assertEqual(report['speech_corroborated_repeats'], [])

    def test_custom_minimum_size_count_and_severity_boundaries(self):
        text = 'come back. ' * 4
        report = audit(document(text), loop_config={'default': {
            'min_pattern_units': 2, 'min_repetitions': 4, 'high_severity_repetitions': 4}})
        loop, = report['segments'][0]['intra_segment_loops']
        self.assertEqual((loop['repeat_count'], loop['severity']), (4, 'high'))
        report = audit(document(text), loop_config={'default': {'min_pattern_units': 3}})
        self.assertEqual(report['segments'][0]['intra_segment_loops'], [])
        # Equal coverage chooses the primitive period instead of its multiples.
        loop, = audit(document('come back ' * 12))['segments'][0]['intra_segment_loops']
        self.assertEqual((loop['pattern_units'], loop['repeat_count']), (2, 12))
        # A below-minimum prefix must not hide a longer loop starting inside it.
        loop, = audit(document('a a a b a a b a a b'))['segments'][0]['intra_segment_loops']
        self.assertEqual((loop['start'], loop['repeat_count']), (2, 3))

    def test_leading_punctuation_is_part_of_each_occurrence(self):
        for text in ('"come back" "come back" "come back"',
                     '(come back) (come back) (come back)'):
            with self.subTest(text=text):
                loop, = audit(document(text))['segments'][0]['intra_segment_loops']
                self.assertEqual((loop['start'], loop['end']), (0, len(text)))
                self.assertEqual([o['text'] for o in loop['occurrences']], [text[:11]] * 3)
                self.assertEqual((loop['pattern_units'], loop['repeat_count']), (2, 3))

    def test_native_format_uses_identical_original_text_offsets(self):
        text = '欢迎收听，欢迎收听，欢迎收听，'
        source = {'language': 'zh', 'transcription': [{
            'offsets': {'from': 0, 'to': 10000},
            'timestamps': {'from': '00:00:00,000', 'to': '00:00:10,000'}, 'text': text}]}
        config = {'languages': {'zh': {'unit': 'characters', 'min_pattern_units': 4}}}
        native = audit(source, input_format='whisper.cpp-json-v1.7.6', loop_config=config)
        normalized = audit({'language': 'zh', 'segments': [{'start': 0, 'end': 10, 'text': text}]},
                           loop_config=config)
        self.assertEqual(native['segments'], normalized['segments'])
        self.assertEqual(native['source_document'], source)

    def test_invalid_config_has_concrete_paths(self):
        for config, path in [
            ([], 'loop_config'), ({'typo': 1}, 'loop_config.typo'),
            ({'default': None}, 'loop_config.default'),
            ({'default': {'unit': 'guess'}}, 'loop_config.default.unit'),
            ({'default': {'min_pattern_units': 0}}, 'loop_config.default.min_pattern_units'),
            ({'default': {'min_repetitions': True}}, 'loop_config.default.min_repetitions'),
            ({'default': {'min_repetitions': 1}}, 'loop_config.default.min_repetitions'),
            ({'default': {'min_repetitions': 3.5}}, 'loop_config.default.min_repetitions'),
            ({'default': {'min_repetitions': 7}}, 'loop_config.default.high_severity_repetitions'),
            ({'languages': []}, 'loop_config.languages'),
            ({'languages': {'': {}}}, 'loop_config.languages'),
            ({'languages': {'zh': {'typo': 1}}}, 'loop_config.languages.zh.typo'),
        ]:
            with self.subTest(config=config):
                with self.assertRaises(AuditError) as caught:
                    audit(document('fine'), loop_config=config)
                self.assertIn(path, str(caught.exception))


class LoopCLITests(unittest.TestCase):
    def test_real_cli_config_csv_srt_hash_and_failure_without_outputs(self):
        (ROOT / '.tools').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.tools') as directory:
            output = Path(directory)
            source = FIXTURES / 'intra_segment_loops.json'
            config = FIXTURES / 'loop_config.json'
            before = source.read_bytes()
            command = [sys.executable, '-m', 'mediawrench.asraudit', str(source), '--loop-config', str(config)]
            paths = [output / n for n in ('report.json', 'flags.csv', 'review.srt')]
            args = ['--json', str(paths[0]), '--csv', str(paths[1]), '--review-srt', str(paths[2])]
            run = subprocess.run(command + args, cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(paths[0].read_text())
            self.assertEqual([f['segment'] for f in report['flags']], [0, 1])
            self.assertIsNone(report['vad_source'])
            self.assertEqual(report['loop_config_source']['bytes'], len(config.read_bytes()))
            rows = list(csv.DictReader(io.StringIO(paths[1].read_text())))
            self.assertEqual(json.loads(rows[1]['intra_segment_loops']), report['flags'][1]['intra_segment_loops'])
            self.assertIn('intra_segment_loop', paths[2].read_text())
            self.assertEqual(source.read_bytes(), before)
            bad = output / 'bad.json'
            fresh = [output / n for n in ('absent.json', 'absent.csv', 'absent.srt')]
            failures = ['--json', str(fresh[0]), '--csv', str(fresh[1]), '--review-srt', str(fresh[2])]
            for raw, error in [('{"default":{"min_repetitions":1}}', 'loop_config.default.min_repetitions'),
                               ('null', 'loop_config'), ('{"default":{},"default":{}}', 'duplicate JSON key')]:
                bad.write_text(raw)
                failed = subprocess.run(command[:-1] + [str(bad)] + failures,
                                        cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(failed.returncode, 2)
                self.assertIn(error, failed.stderr)
                self.assertFalse(any(p.exists() for p in fresh))
            # The config file is an input, including aliases, and cannot be an output.
            alias = output / 'alias.json'
            alias.hardlink_to(config)
            failed = subprocess.run(command + ['--json', str(alias)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(failed.returncode, 2)
            self.assertIn('aliases an input', failed.stderr)


if __name__ == '__main__':
    unittest.main()
