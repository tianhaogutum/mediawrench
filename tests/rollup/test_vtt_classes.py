"""Bounded c/class preservation contracts; class strings are opaque, not CSS."""
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


class VttClassTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return formatting.VttFormattingTests.assert_mappings(self, source, result)

    def test_whole_line_bare_and_class_list_contract(self):
        for tag in ('c', 'c.em', 'c.em.strong', 'c.A_1.-x.2', 'c.em.em'):
            with self.subTest(tag=tag):
                source = document(f'<{tag}>Hello there</c>', f'<{tag}>Hello there</c>\nNew line',
                                  settings=' align:start position:20%')
                result = convert(source, output_format='vtt')
                cues = self.assert_mappings(source, result)
                self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                                 [(0, 3000, f'<{tag}>Hello there</c>'), (1000, 3000, 'New line')])
                self.assertEqual(cues[0].tokens[0].glyph_contexts, ((tag,),) * 5)
                self.assertFalse(result.report['warnings'])
                self.assertEqual(result.report['summary']['removed_tokens'], 2)

    def test_rewrapped_nested_class_stack_reopens(self):
        source = document('<c.em><b>We\nare ready</b></c>',
                          '<c.em><b>We are\nready <c.new><u>now</u></c></b></c>',
                          settings=' align:start')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<c.em><b>We\nare ready</b></c>'),
                          (1000, 3000, '<c.em><b><c.new><u>now</u></c></b></c>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('c.em', 'b', 'c.new', 'u'),) * 3)
        self.assertEqual(result.provenance['output_cues'][1]['source_span'],
                         {'cue': 2, 'token_start': 4, 'token_end': 5})
        records = result.provenance['tokens']
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line']) for r in records[:3]],
                         [(9, 11, 1), (12, 15, 2), (16, 21, 2)])
        self.assertEqual(records[-1]['source_char_start'], 32)

    def test_sliding_class_spans_close_with_base_tag(self):
        wrap = lambda s: f'<c.outer><i><c.inner>{s}</c></i></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])
        self.assertNotIn('</c.', result.vtt)

    def test_within_word_classes_and_entity_offsets(self):
        source = document('<c.em>H</c>ello <c.outer>th<c.inner>er</c>e</c> &amp;',
                          '<c.em>H</c>ello <c.outer>th<c.inner>er</c>e</c> &amp; now')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual(cues[0].raw, '<c.em>H</c>ello <c.outer>th<c.inner>er</c>e</c> &amp;')
        raw = result.provenance['source_cues'][0]['raw_text']
        for record, expected in zip(result.provenance['tokens'][:3],
                                    ['H</c>ello', 'th<c.inner>er</c>e', '&amp;']):
            self.assertEqual(raw[record['source_char_start']:record['source_char_end']], expected)

    def test_class_context_conflicts_keep_both_displays(self):
        pairs = [('<c.em>Hello there</c>', '<c.other>Hello there</c>'),
                 ('<c.em>Hello there</c>', '<c.Em>Hello there</c>'),
                 ('<c.a.b>Hello there</c>', '<c.b.a>Hello there</c>'),
                 ('<c>Hello there</c>', 'Hello there'),
                 ('<c.em>H</c>ello there', '<c.em>He</c>llo there'),
                 ('<c.a><b>Hello there</b></c>', '<b><c.a>Hello there</c></b>')]
        for old, new in pairs:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    source = document(old, new + '\nNew line')
                    result = convert(source, output_format='vtt', word_overlap=word)
                    cues = self.assert_mappings(source, result)
                    self.assertEqual(result.report['summary']['removed_tokens'], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0,2000), (1000,3000)])
                    if word:
                        self.assertIn('contexts', result.report['ambiguities'][0]['reason'])

    def test_classes_do_not_bypass_overlap_evidence(self):
        for old, new, reason in [('go\nnow go now', 'go now\ngo now today', 'multiple'),
                                 ('We\nare ready', 'are\nready', 'suffix-only'),
                                 ('We\nare ready', 'ready\nnow', 'short/repetitive'),
                                 ('.\n!', '. !\nnext', 'word evidence')]:
            with self.subTest(old=old, new=new):
                source = document(f'<c.em>{old}</c>', f'<c.em>{new}</c>')
                result = convert(source, output_format='vtt', word_overlap=True)
                self.assert_mappings(source, result)
                self.assertEqual(result.report['summary']['removed_tokens'], 0)
                self.assertIn(reason, result.report['ambiguities'][0]['reason'])
        for replacement in ('00:02.000 --> 00:03.000', '00:02.001 --> 00:03.000',
                            '00:01.000 --> 00:01.500'):
            source = document('<c.em>We\nare ready</c>', '<c.em>We are\nready now</c>')
            result = convert(source.replace('00:01.000 --> 00:03.000', replacement),
                             output_format='vtt', word_overlap=True, max_gap_ms=10000)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
        source = document('<c.em>We\nare ready</c>', '<c.em>We are\nready now</c>', settings=' align:start')
        result = convert(source.replace('00:03.000 align:start', '00:03.000 align:end'),
                         output_format='vtt', word_overlap=True)
        self.assertEqual(result.report['summary']['removed_tokens'], 0)
        self.assertIn('settings', result.report['ambiguities'][0]['reason'])

    def test_class_grammar_and_other_unsupported_structures_reject(self):
        payloads = ['<c.>Hello there</c>', '<c..a>Hello there</c>', '<c.a.>Hello there</c>',
                    '<c.a b>Hello there</c>', '<c.é>Hello there</c>', '<c a>Hello there</c>',
                    '<c.a>Hello there</c.a>', '<c.a><b>Hello there</c></b>', '<c.a>Hello there',
                    '<c.a/>Hello there', '<c.a>Hello there</c >', '<b.a>Hello there</b.a>',
                    '<v Ada>Hello there', '<lang en>Hello there', '<ruby>Hello there</ruby>',
                    '<c.a>Hello <00:00.500>there</c>']
        for payload in payloads:
            for flatten in (False, True):
                with self.subTest(payload=payload, flatten=flatten), self.assertRaises(CaptionError):
                    convert(document(payload), output_format='vtt', flatten=flatten)

    def test_class_readback_corruption_is_rejected(self):
        real_parser = core._parse_vtt_document
        calls = 0

        def corrupt(text, **options):
            nonlocal calls
            calls += 1
            if calls == 2:
                text = text.replace('<c.em>', '<c.other>')
            return real_parser(text, **options)

        with patch.object(core, '_parse_vtt_document', side_effect=corrupt):
            with self.assertRaisesRegex(CaptionError, 'roundtrip'):
                convert(document('<c.em>Hello there</c>'), output_format='vtt')
        self.assertEqual(calls, 2)

    def test_srt_class_policy_still_requires_flatten(self):
        source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n<c.em>Hello there</c>\n'
        with self.assertRaisesRegex(CaptionError, 'flatten'):
            convert(source)
        result = convert(source, flatten=True)
        self.assertIn('Hello there', result.srt)
        self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
