"""Class-suffixed b/i/u contracts: opaque exact contexts, no CSS support."""
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


class VttClassedFormattingTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return formatting.VttFormattingTests.assert_mappings(self, source, result)

    def test_whole_line_classes_on_each_formatting_tag(self):
        for base in ('b', 'i', 'u'):
            for classes in ('.em', '.A_1.-x.2', '.em.em'):
                tag = base + classes
                with self.subTest(tag=tag):
                    source = document(f'<{tag}>Hello there</{base}>',
                                      f'<{tag}>Hello there</{base}>\nNew line', settings=' align:start')
                    result = convert(source, output_format='vtt')
                    cues = self.assert_mappings(source, result)
                    self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                                     [(0, 3000, f'<{tag}>Hello there</{base}>'), (1000, 3000, 'New line')])
                    self.assertEqual(cues[0].tokens[0].glyph_contexts, ((tag,),) * 5)
                    self.assertFalse(result.report['warnings'])

    def test_rewrapped_nested_classed_tags_reopen_full_stack(self):
        source = document('<b.em><c.outer><i.soft>We\nare ready</i></c></b>',
                          '<b.em><c.outer><i.soft>We are\nready <u.new>now</u></i></c></b>',
                          settings=' align:start position:20%')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<b.em><c.outer><i.soft>We\nare ready</i></c></b>'),
                          (1000, 3000, '<b.em><c.outer><i.soft><u.new>now</u></i></c></b>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts,
                         (('b.em', 'c.outer', 'i.soft', 'u.new'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(23, 25, 1), (26, 29, 2), (30, 35, 2)])

    def test_sliding_fragments_close_base_tags(self):
        wrap = lambda s: f'<u.em><b.strong><c><i.soft>{s}</i></c></b></u>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])
        for base in ('b', 'i', 'u', 'c'):
            self.assertNotIn(f'</{base}.', result.vtt)

    def test_within_word_boundaries_and_entity_offsets(self):
        old = '<b.em>H</b>ello <i.a>th<u.b>er</u>e</i> &amp;'
        source = document(old, old + ' now')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('b.em',), (), (), (), ()))
        raw = result.provenance['source_cues'][0]['raw_text']
        for record, expected in zip(result.provenance['tokens'][:3],
                                    ['H</b>ello', 'th<u.b>er</u>e', '&amp;']):
            self.assertEqual(raw[record['source_char_start']:record['source_char_end']], expected)

    def test_class_or_boundary_conflicts_keep_both_displays(self):
        pairs = [('<b.a>Hello there</b>', '<b.b>Hello there</b>'),
                 ('<i.a>Hello there</i>', '<i.A>Hello there</i>'),
                 ('<u.a.b>Hello there</u>', '<u.b.a>Hello there</u>'),
                 ('<b.a.a>Hello there</b>', '<b.a>Hello there</b>'),
                 ('<b.a>Hello there</b>', '<i.a>Hello there</i>'),
                 ('<b.a>H</b>ello there', '<b.a>He</b>llo there'),
                 ('<b.a><c>Hello there</c></b>', '<c><b.a>Hello there</b></c>')]
        for old, new in pairs:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    result = convert(document(old, new + '\nNew line'), output_format='vtt', word_overlap=word)
                    cues = self.assert_mappings(document(old, new + '\nNew line'), result)
                    self.assertEqual(result.report['summary']['removed_tokens'], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])
                    if word:
                        self.assertIn('contexts', result.report['ambiguities'][0]['reason'])

    def test_annotations_keep_existing_ambiguity_guards(self):
        for old, new in [('go now go now', 'go now go now today'), ('We are ready', 'are ready'),
                         ('yes', 'yes please'), ('. !', '. ! next')]:
            source = document(f'<b.a>{old}</b>', f'<b.a>{new}</b>')
            result = convert(source, output_format='vtt', word_overlap=True)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertTrue(result.report['ambiguities'])
        source = document('<i.a>We\nare ready</i>', '<i.a>We are\nready now</i>')
        for changed, reason in [(source.replace('00:01.000 -->', '00:02.000 -->'), 'strict temporal'),
                                (source.replace('00:01.000 -->', '00:02.001 -->'), 'strict temporal'),
                                (source.replace('00:03.000\n', '00:03.000 align:end\n'), 'settings')]:
            result = convert(changed, output_format='vtt', word_overlap=True, max_gap_ms=10000)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertIn(reason, result.report['ambiguities'][0]['reason'])
        self.assertEqual(convert(source, output_format='vtt').report['summary']['removed_tokens'], 0)

    def test_malformed_classed_formatting_still_rejects(self):
        for base in ('b', 'i', 'u'):
            for payload in (f'<{base}.>Hello there</{base}>', f'<{base}..a>Hello there</{base}>',
                            f'<{base}.a.>Hello there</{base}>', f'<{base}.é>Hello there</{base}>',
                            f'<{base}.a b>Hello there</{base}>', f'<{base} a>Hello there</{base}>',
                            f'<{base}.a>Hello there</{base}.a>', f'<{base}.a/>Hello there',
                            f'<{base}.a>Hello there', f'<{base}.a><c>Hello there</{base}></c>',
                            f'<{base}.a>Hello <00:00.500>there</{base}>'):
                for flatten in (False, True):
                    with self.subTest(payload=payload, flatten=flatten), self.assertRaises(CaptionError):
                        convert(document(payload), output_format='vtt', flatten=flatten)

    def test_readback_class_boundary_corruption_is_rejected(self):
        real_parser = core._parse_vtt_document
        calls = 0

        def corrupt(text, **options):
            nonlocal calls
            calls += 1
            if calls == 2:
                text = text.replace('<b.em>H</b>ello', '<b.em>He</b>llo')
            return real_parser(text, **options)

        with patch.object(core, '_parse_vtt_document', side_effect=corrupt):
            with self.assertRaisesRegex(CaptionError, 'roundtrip'):
                convert(document('<b.em>H</b>ello there'), output_format='vtt')
        self.assertEqual(calls, 2)

    def test_srt_classed_formatting_policy_still_requires_flatten(self):
        for base in ('b', 'i', 'u'):
            source = f'WEBVTT\n\n00:00.000 --> 00:02.000\n<{base}.em>Hello there</{base}>\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
