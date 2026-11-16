"""Unicode speaker words are exact opaque codepoint sequences in preserved VTT."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import cli, core
from . import test_vtt_language as language
from .test_vtt_formatting import document


class VttUnicodeVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return language.VttLanguageTests.assert_mappings(self, source, result)

    def test_exact_rewrapped_unicode_speaker_contract(self):
        source = document('<lang.em en><v.em Adá>We\nare ready</v></lang>',
                          '<lang.em en><v.em Adá>We are\nready now</v></lang>',
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<lang.em en><v.em Adá>We\nare ready</v></lang>'),
                          (1000, 3000, '<lang.em en><v.em Adá>now</v></lang>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('lang.em en', 'v.em Adá'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(22, 24, 1), (25, 28, 2), (29, 34, 2)])
        self.assertTrue(result.vtt.startswith('WEBVTT Formatting\nLanguage: en\n'))
        self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                         ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
        self.assertFalse(result.report['warnings'])

    def test_word_categories_bare_classed_and_full_stack(self):
        # Lu/Ll/Lt/Lm/Lo; following Mn/Mc/Me; only ASCII digits are allowed.
        for label in ('Adá', 'Ada\u0301', '李 明', 'Αλφα', 'Борис', 'ǅ ʰ 中',
                      'A\u0301 A\u0903 A\u20dd', '0_- _\u0301 -x 9李', 'Adá Lovelace'):
            for voice in ('v', 'v.a.B.a'):
                with self.subTest(label=label, voice=voice):
                    wrap = lambda s: f'<b.a><lang.em en><{voice} {label}><v.inner Éva><c.x><i.y><u.z>{s}</u></i></c></v></v></lang></b>'
                    source = document(wrap('Hello there'), wrap('Hello there') + '\nNew line')
                    cues = self.assert_mappings(source, convert(source, output_format='vtt'))
                    self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                                     [(0, 3000, wrap('Hello there')), (1000, 3000, 'New line')])
                    self.assertEqual(cues[0].tokens[0].glyph_contexts,
                                     (('b.a', 'lang.em en', f'{voice} {label}', 'v.inner Éva', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_spans_balance_exact_decomposed_stack(self):
        wrap = lambda s: f'<c.em><lang.a.B.a en-US><v.a Ada\u0301><lang.inner fr><u.a><b.b>{s}</b></u></lang></v></lang></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])

    def test_within_word_entities_and_whitespace_canonicalization(self):
        old = '<lang.a en><v Adá>H</v>ello <v.b Ada\u0301>th<c.x>er</c>e</v> &amp;</lang>'
        source = document(old, old + ' now')
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts,
                         (('lang.a en', 'v Adá'),) + (('lang.a en',),) * 4)
        source = document('<v Adá></v>Hello<v.a Ada\u0301> \t</v>there')
        self.assertEqual(self.assert_mappings(source, convert(source, output_format='vtt'))[0].raw, 'Hello there')

    def test_exact_codepoints_classes_stack_order_and_boundaries(self):
        pairs = [(f'<v.a {a}>Hello there</v>', f'<v.a {b}>Hello there</v>')
                 for a, b in [('Adá', 'Ada\u0301'), ('Adá', 'adá'), ('Adá 李', '李 Adá'),
                              ('A\u0301\u0327', 'A\u0327\u0301'), ('K', 'K')]]
        pairs += [(f'<v.{a} Adá>Hello there</v>', f'<v.{b} Adá>Hello there</v>')
                  for a, b in [('a', 'A'), ('a.b', 'b.a'), ('a.a', 'a')]]
        pairs += [('<v Adá>Hello there</v>', '<v.a Adá>Hello there</v>'),
                  ('<lang.a en><v Adá>Hello there</v></lang>', '<v Adá><lang.a en>Hello there</lang></v>'),
                  ('<v Adá><v 李>Hello there</v></v>', '<v 李><v Adá>Hello there</v></v>'),
                  ('<v Adá>H</v>ello there', '<v Adá>He</v>llo there')]
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

    def test_unicode_context_keeps_time_evidence_and_settings_guards(self):
        wrap = lambda s: f'<lang.a en><v.a Adá>{s}</v></lang>'
        for old, new in [('go now go now', 'go now go now today'), ('We are ready', 'are ready'),
                         ('yes', 'yes please'), ('. !', '. ! next'), ('go go', 'go go now')]:
            result = convert(document(wrap(old), wrap(new)), output_format='vtt', word_overlap=True)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertTrue(result.report['ambiguities'])
        source = document(wrap('We\nare ready'), wrap('We are\nready now'))
        for changed, reason in [(source.replace('00:01.000 -->', '00:02.000 -->'), 'strict temporal'),
                                (source.replace('00:01.000 -->', '00:02.001 -->'), 'strict temporal'),
                                (source.replace('00:03.000\n', '00:03.000 align:end\n'), 'settings'),
                                (source.replace('00:03.000\n', '00:01.500\n'), 'nested')]:
            result = convert(changed, output_format='vtt', word_overlap=True, max_gap_ms=10000)
            self.assertEqual(result.report['summary']['removed_tokens'], 0)
            self.assertIn(reason, result.report['ambiguities'][0]['reason'])
        self.assertEqual(convert(source, output_format='vtt').report['summary']['removed_tokens'], 0)

    def test_malformed_labels_classes_and_unsupported_contexts(self):
        labels = ['', ' Adá', 'Adá ', 'Adá  李', 'Adá\t李', 'Adá\n李', '\u0301Ada',
                  'Adá \u0301Li', 'Adá\u00a0李', 'Adá\u2003李', 'A.B', 'A/B', 'O‘Neil',
                  'A&amp;B', 'speaker="Adá"', '١', 'A١', '²', 'Ⅳ', 'A🙂', 'A\u200dB',
                  'A\u200eB', 'A\ufeffB', 'A\x00B']
        payloads = [f'<{voice} {label}>Hello there</v>' for voice in ('v', 'v.a') for label in labels]
        payloads += [f'<{tag}>Hello there</v>' for tag in
                     ['v.', 'v.a', 'v. Adá', 'v..a Adá', 'v.a. Adá', 'v.́é Adá', 'v.a! Adá', 'V Adá']]
        payloads += ['<v.a Adá>Hello there</v.a>', '<v Adá>Hello there</v Adá>',
                     '<v Adá>Hello there</v >', '<v Adá>Hello there',
                     '<lang.a en><v Adá>Hello there', '<v Adá><b>Hello there</v></b>',
                     '<v Adá>Hello there</v></v>', '<v Adá>Hello there</lang>',
                     '<lang.a én><v Adá>Hello there</v></lang>',
                     '<lang.é en><v Adá>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<v Adá>Hello <00:00.500>there</v>']
        sources = [document(p) for p in payloads]
        sources += [document('<v Adá>Hello there</v>').replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_unicode_and_glyph_corruption_blocks_publication(self):
        source = document('<lang.a en><v.a Adá>H</v>ello <v Ada\u0301>there</v></lang>')
        for old, new in [('<v.a Adá>', '<v.a Ada\u0301>'), ('<v Ada\u0301>', '<v Adá>'),
                         ('<v.a Adá>', '<v.a adá>'), ('<v.a Adá>', '<v.b Adá>'),
                         ('<v.a Adá>H</v>ello', '<v.a Adá>He</v>llo')]:
            real_parser = core._parse_vtt_document
            calls = 0

            def corrupt(text, **options):
                nonlocal calls
                calls += 1
                return real_parser(text.replace(old, new) if calls == 2 else text, **options)

            with tempfile.TemporaryDirectory() as folder:
                src, out = Path(folder) / 'input.vtt', Path(folder) / 'output.vtt'
                src.write_text(source)
                with patch.object(core, '_parse_vtt_document', side_effect=corrupt), contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main([str(src), '--output-format', 'vtt', '-o', str(out)]), 2)
                self.assertIn('roundtrip', err.getvalue())
                self.assertFalse(out.exists())
                self.assertFalse(Path(str(out) + '.json').exists())
                self.assertEqual(calls, 2)

    def test_srt_broader_lossy_grammar_and_cue_end_unchanged(self):
        for payload in ('<v Adá>Hello there', '<v.a Ada\u0301>Hello there',
                        '<v O’Neil>Hello there', '<v \u0301Ada>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            self.assertIn('Hello there', convert(source, flatten=True).srt)
