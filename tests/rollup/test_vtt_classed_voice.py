"""Balanced class-suffixed voice scopes keep exact speaker/class contexts."""
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


class VttClassedVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return formatting.VttFormattingTests.assert_mappings(self, source, result)

    def test_rewrapped_classed_voice(self):
        source = document('<v.em Ada><b.strong>We\nare ready</b></v>',
                          '<v.em Ada><b.strong>We are\nready now</b></v>',
                          settings=' align:start position:20%')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<v.em Ada><b.strong>We\nare ready</b></v>'),
                          (1000, 3000, '<v.em Ada><b.strong>now</b></v>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('v.em Ada', 'b.strong'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(20, 22, 1), (23, 26, 2), (27, 32, 2)])
        self.assertFalse(result.report['warnings'])

    def test_whole_line_nested_ordered_classes_and_speakers(self):
        for tag in ('v.a Ada', 'v.a.B.a Ada Lovelace', 'v.A_1.-x.2 A_1 -x 2'):
            wrap = lambda s: f'<b.a><{tag}><v.inner Bob><c.x><i.y><u.z>{s}</u></i></c></v></v></b>'
            source = document(wrap('Hello there'), wrap('Hello there') + '\nNew line')
            cues = self.assert_mappings(source, convert(source, output_format='vtt'))
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('Hello there')), (1000, 3000, 'New line')])
            self.assertEqual(cues[0].tokens[0].glyph_contexts,
                             (('b.a', tag, 'v.inner Bob', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_splits_balance_full_voice_stack(self):
        wrap = lambda s: f'<c.em><v.a.b.a Ada Lovelace><u.a><b.b>{s}</b></u></v></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])
        self.assertNotIn('</v.', result.vtt)

    def test_within_word_scopes_entities_and_empty_whitespace_scopes(self):
        old = '<v.a Ada>H</v>ello <v.b Bob>th<c.a>er</c>e</v> &amp;'
        source = document(old, old + ' now')
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('v.a Ada',), (), (), (), ()))
        for token, raw in zip(core.parse_vtt(source, preserve_metadata=True)[0][0].tokens,
                              ['H</v>ello', 'th<c.a>er</c>e', '&amp;']):
            self.assertEqual(old[token.char_start:token.char_end], raw)
        source = document('<v.a Ada></v>Hello<v.b Bob> \t</v>there')
        self.assertEqual(self.assert_mappings(source, convert(source, output_format='vtt'))[0].raw,
                         'Hello there')

    def test_class_speaker_and_scope_differences_retain_both(self):
        pairs = [('v.a Ada', 'v.A Ada'), ('v.a.b Ada', 'v.b.a Ada'),
                 ('v.a.a Ada', 'v.a Ada'), ('v.a Ada', 'v.a Bob'), ('v.a Ada', 'v Ada')]
        payloads = [(f'<{a}>Hello there</v>', f'<{b}>Hello there</v>') for a, b in pairs]
        payloads += [('<v.a Ada>H</v>ello there', '<v.a Ada>He</v>llo there'),
                     ('<v.a Ada><b>Hello there</b></v>', '<b><v.a Ada>Hello there</v></b>')]
        for old, new in payloads:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    source = document(old, new + '\nNew line')
                    result = convert(source, output_format='vtt', word_overlap=word)
                    cues = self.assert_mappings(source, result)
                    self.assertEqual(result.report['summary']['removed_tokens'], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])
                    if word:
                        self.assertIn('contexts', result.report['ambiguities'][0]['reason'])

    def test_evidence_timing_settings_and_opt_in_guards(self):
        for old, new in [('go now go now', 'go now go now today'), ('We are ready', 'are ready'),
                         ('yes', 'yes please'), ('. !', '. ! next')]:
            result = convert(document(f'<v.a Ada>{old}</v>', f'<v.a Ada>{new}</v>'),
                             output_format='vtt', word_overlap=True)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertTrue(result.report['ambiguities'])
        source = document('<v.a Ada>We\nare ready</v>', '<v.a Ada>We are\nready now</v>')
        for changed, reason in [(source.replace('00:01.000 -->', '00:02.000 -->'), 'strict temporal'),
                                (source.replace('00:01.000 -->', '00:02.001 -->'), 'strict temporal'),
                                (source.replace('00:03.000\n', '00:03.000 align:end\n'), 'settings'),
                                (source.replace('00:03.000\n', '00:01.500\n'), 'nested')]:
            result = convert(changed, output_format='vtt', word_overlap=True, max_gap_ms=10000)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertIn(reason, result.report['ambiguities'][0]['reason'])
        self.assertEqual(convert(source, output_format='vtt').report['summary']['removed_tokens'], 0)

    def test_malformed_voice_and_unsupported_markup_reject_even_with_flatten(self):
        payloads = ['<v. Ada>Hello there</v>', '<v..a Ada>Hello there</v>', '<v.a. Ada>Hello there</v>',
                    '<v.a>Hello there</v>', '<v.a >Hello there</v>', '<v.a  Ada>Hello there</v>',
                    '<v.a Ada >Hello there</v>', '<v.a Ada  Bob>Hello there</v>', '<v.a\tAda>Hello there</v>',
                    '<v.a Ada\nBob>Hello there</v>', '<v.a \u0301Adá>Hello there</v>', '<v.a A.B>Hello there</v>',
                    '<v.a A&amp;B>Hello there</v>', '<v.a speaker="Ada">Hello there</v>',
                    '<v.a Ada>Hello there</v.a>', '<v.a Ada>Hello there</v >',
                    '<v.a Ada>Hello there', '<v.a Ada><b>Hello there</b>', '<v.a Ada/>Hello there',
                    '<v.a Ada><b>Hello there</v></b>', '<v.a Ada>Hello there</v></v>',
                    '<lang en_US>Hello there</lang>', '<ruby>Hello there</ruby>', '<x>Hello there</x>',
                    '<v.a Ada>Hello <00:00.500>there</v>']
        for payload in payloads:
            for flatten in (False, True):
                with self.subTest(payload=payload, flatten=flatten), self.assertRaises(CaptionError):
                    convert(document(payload), output_format='vtt', flatten=flatten)

    def test_readback_rejects_changed_class_speaker_or_within_word_scope(self):
        # Both boundary variants have identical distinct-context lists: glyph stacks must be checked.
        for old, new in [('<v.a Ada>', '<v.b Ada>'), ('<v.a Ada>', '<v.a Bob>'),
                         ('<v.a Ada>H</v>ello', '<v.a Ada>He</v>llo')]:
            real_parser = core._parse_vtt_document
            calls = 0

            def corrupt(text, **options):
                nonlocal calls
                calls += 1
                return real_parser(text.replace(old, new) if calls == 2 else text, **options)

            with patch.object(core, '_parse_vtt_document', side_effect=corrupt):
                with self.assertRaisesRegex(CaptionError, 'roundtrip'):
                    convert(document('<v.a Ada>H</v>ello there'), output_format='vtt')
            self.assertEqual(calls, 2)
