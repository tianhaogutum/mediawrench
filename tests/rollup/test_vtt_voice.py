"""Restricted, explicitly balanced voice scopes in preserved VTT fragments."""
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


class VttVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return formatting.VttFormattingTests.assert_mappings(self, source, result)

    def test_rewrapped_voice_reopens_with_classed_formatting(self):
        source = document('<v Ada><b.em>We\nare ready</b></v>',
                          '<v Ada><b.em>We are\nready now</b></v>',
                          settings=' align:start position:20%')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<v Ada><b.em>We\nare ready</b></v>'),
                          (1000, 3000, '<v Ada><b.em>now</b></v>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('v Ada', 'b.em'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(13, 15, 1), (16, 19, 2), (20, 25, 2)])
        self.assertFalse(result.report['warnings'])

    def test_whole_line_voice_name_grammar_and_nested_scopes(self):
        for name in ('Ada', 'Ada Lovelace', 'A_1 -x 2'):
            wrap = lambda s: f'<v {name}><c.a.b><i.soft>{s}</i></c></v>'
            source = document(wrap('Hello there'), wrap('Hello there') + '\nNew line')
            result = convert(source, output_format='vtt')
            cues = self.assert_mappings(source, result)
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('Hello there')), (1000, 3000, 'New line')])
            self.assertEqual(cues[0].tokens[0].glyph_contexts,
                             ((f'v {name}', 'c.a.b', 'i.soft'),) * 5)
        payload = '<b.a><v Ada><v Bob><u.x>Hello there</u></v></v></b>'
        result = convert(document(payload, payload + '\nNew line'), output_format='vtt')
        cues = self.assert_mappings(document(payload, payload + '\nNew line'), result)
        self.assertEqual(cues[0].raw, payload)
        self.assertEqual(cues[0].end_ms, 3000)

    def test_sliding_voice_spans_close_with_base_name(self):
        wrap = lambda s: f'<c.em><v Ada Lovelace><u.a><b.b>{s}</b></u></v></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])
        self.assertNotIn('</v ', result.vtt)

    def test_within_word_voice_changes_and_original_entity_offsets(self):
        old = '<v Ada>H</v>ello <v Bob>th<c.a>er</c>e</v> &amp;'
        source = document(old, old + ' now')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('v Ada',), (), (), (), ()))
        for record, raw in zip(result.provenance['tokens'][:3], ['H</v>ello', 'th<c.a>er</c>e', '&amp;']):
            self.assertEqual(old[record['source_char_start']:record['source_char_end']], raw)

    def test_speaker_case_order_scope_and_format_conflicts_keep_displays(self):
        pairs = [('<v Ada>Hello there</v>', '<v Bob>Hello there</v>'),
                 ('<v Ada>Hello there</v>', '<v ada>Hello there</v>'),
                 ('<v Ada Bob>Hello there</v>', '<v Bob Ada>Hello there</v>'),
                 ('<v Ada>Hello there</v>', 'Hello there'),
                 ('<v Ada>H</v>ello there', '<v Ada>He</v>llo there'),
                 ('<v Ada><b.a>Hello there</b></v>', '<b.a><v Ada>Hello there</v></b>')]
        for old, new in pairs:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    source = document(old, new + '\nNew line')
                    result = convert(source, output_format='vtt', word_overlap=word)
                    cues = self.assert_mappings(source, result)
                    self.assertEqual(result.report['summary']['removed_tokens'], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])
                    if word:
                        self.assertIn('contexts', result.report['ambiguities'][0]['reason'])

    def test_voice_keeps_evidence_timing_settings_and_word_opt_in_guards(self):
        for old, new in [('go now go now', 'go now go now today'), ('We are ready', 'are ready'),
                         ('yes', 'yes please'), ('. !', '. ! next')]:
            source = document(f'<v Ada>{old}</v>', f'<v Ada>{new}</v>')
            result = convert(source, output_format='vtt', word_overlap=True)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertTrue(result.report['ambiguities'])
        source = document('<v Ada>We\nare ready</v>', '<v Ada>We are\nready now</v>')
        for changed, reason in [(source.replace('00:01.000 -->', '00:02.000 -->'), 'strict temporal'),
                                (source.replace('00:01.000 -->', '00:02.001 -->'), 'strict temporal'),
                                (source.replace('00:03.000\n', '00:03.000 align:end\n'), 'settings'),
                                (source.replace('00:03.000\n', '00:01.500\n'), 'nested')]:
            result = convert(changed, output_format='vtt', word_overlap=True, max_gap_ms=10000)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertIn(reason, result.report['ambiguities'][0]['reason'])
        self.assertEqual(convert(source, output_format='vtt').report['summary']['removed_tokens'], 0)

    def test_unsupported_and_unbalanced_voice_syntax_rejects_even_with_flatten(self):
        payloads = ['<v>Hello there</v>', '<v >Hello there</v>', '<v  Ada>Hello there</v>',
                    '<v Ada >Hello there</v>', '<v Ada  Bob>Hello there</v>', '<v\tAda>Hello there</v>',
                    '<v Ada\nBob>Hello there</v>', '<v \u0301Adá>Hello there</v>', '<v A.B>Hello there</v>',
                    '<v A&amp;B>Hello there</v>', '<v speaker="Ada">Hello there</v>',
                    '<v. Ada>Hello there</v>', '<v Ada>Hello there</v.a>', '<v Ada>Hello there</v >',
                    '<v Ada>Hello there', '<v Ada><b>Hello there</b>', '<v Ada/>Hello there',
                    '<v Ada><b>Hello there</v></b>', '<v Ada>Hello there</v></v>',
                    '<lang en_US>Hello there</lang>', '<ruby>Hello there</ruby>', '<x>Hello there</x>',
                    '<v Ada>Hello <00:00.500>there</v>']
        for payload in payloads:
            for flatten in (False, True):
                with self.subTest(payload=payload, flatten=flatten), self.assertRaises(CaptionError):
                    convert(document(payload), output_format='vtt', flatten=flatten)

    def test_readback_rejects_changed_speaker_or_within_word_scope(self):
        for old, new in [('<v Ada>', '<v Bob>'), ('<v Ada>H</v>ello', '<v Ada>He</v>llo')]:
            real_parser = core._parse_vtt_document
            calls = 0

            def corrupt(text, **options):
                nonlocal calls
                calls += 1
                if calls == 2:
                    text = text.replace(old, new)
                return real_parser(text, **options)

            with patch.object(core, '_parse_vtt_document', side_effect=corrupt):
                with self.assertRaisesRegex(CaptionError, 'roundtrip'):
                    convert(document('<v Ada>H</v>ello there'), output_format='vtt')
            self.assertEqual(calls, 2)

    def test_empty_and_whitespace_only_scopes_canonicalize(self):
        result = convert(document('<v Ada></v>Hello<v Bob> \t</v>there'), output_format='vtt')
        self.assertEqual(self.assert_mappings(document('<v Ada></v>Hello<v Bob> \t</v>there'), result)[0].raw,
                         'Hello there')

    def test_srt_keeps_broader_voice_grammar_and_implicit_end_policy(self):
        for payload in ('<v Ada>Hello there</v>', '<v Adá>Hello there', '<v.a Ada>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
