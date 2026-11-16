"""Single Unicode-letter initials retain one terminal ASCII period in balanced VTT voices."""
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


def wrap(text):
    return f'<lang.em en><v.em J. O’Neil-Smith>{text}</v></lang>'


class VttInitialVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return language.VttLanguageTests.assert_mappings(self, source, result)

    def test_exact_rewrapped_initial_contract(self):
        source = document(wrap('We\nare ready'), wrap('We are\nready now'),
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        for flatten in (False, True):
            result = convert(source, output_format='vtt', word_overlap=True, flatten=flatten)
            cues = self.assert_mappings(source, result)
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('We\nare ready')), (1000, 3000, wrap('now'))])
            self.assertEqual(cues[1].tokens[0].glyph_contexts, (('lang.em en', 'v.em J. O’Neil-Smith'),) * 3)
            self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                              for r in result.provenance['tokens'][:3]],
                             [(34, 36, 1), (37, 40, 2), (41, 46, 2)])
            self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                             ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
            self.assertEqual(result.report['summary']['removed_tokens'], 3)
            self.assertFalse(result.report['warnings'])

    def test_initial_words_full_stack_and_unchanged_nonapostrophe_words(self):
        labels = ['J.', 'É.', 'E\u0301.', 'j.', '李.', 'ʰ.', 'ǅ.', 'Б.', 'A\u0301\u0327.',
                  'A\u0903\u20dd.', "J. O’Neil-Smith", "É. O'Neil-Smith", 'E\u0301. 李 明',
                  'A. B. A_1 -- -x 2- _', 'J. A-B’C']
        for label in labels:
            for voice in ('v', 'v.a.B.a'):
                with self.subTest(label=label, voice=voice):
                    nested = lambda s: f'<b.a><lang.em en><{voice} {label}><v.inner Éva><c.x><i.y><u.z>{s}</u></i></c></v></v></lang></b>'
                    source = document(nested('Hello there'), nested('Hello there') + '\nNew line')
                    cues = self.assert_mappings(source, convert(source, output_format='vtt'))
                    self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                                     [(0, 3000, nested('Hello there')), (1000, 3000, 'New line')])
                    self.assertEqual(cues[0].tokens[0].glyph_contexts,
                                     (('b.a', 'lang.em en', f'{voice} {label}', 'v.inner Éva', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_within_word_and_empty_scope_canonicalization(self):
        nested = lambda s: f'<c.em><lang.a.B.a en-US><v.a E\u0301. O’Ne\u0301il-Smith><v É. O\'Neil-Smith><u.a><b.b>{s}</b></u></v></v></lang></c>'
        source = document(nested('We\nare ready'), nested('are\nready now'), nested('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, nested('We')), (0, 3000, nested('are')), (0, 4000, nested('ready')),
                          (1000, 4000, nested('now')), (2000, 4000, nested('today'))])
        old = '<lang.a en><v J. O’Neil-Smith>H</v>ello <v.b É. O\'Neil-Smith>th<c.x>er</c>e</v> &amp;</lang>'
        cues = self.assert_mappings(document(old, old + ' now'),
                                    convert(document(old, old + ' now'), output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('lang.a en', 'v J. O’Neil-Smith'),) + (('lang.a en',),) * 4)
        self.assertEqual(convert(document('<v J. O’Neil-Smith></v>Hello<v.a É. O\'Neil-Smith> \t</v>there'),
                                 output_format='vtt').vtt.splitlines()[-1], 'Hello there')

    def test_opaque_punctuation_unicode_classes_stacks_and_boundaries(self):
        pairs = [(f'<v.a {a}>Hello there</v>', f'<v.a {b}>Hello there</v>') for a, b in
                 [('J.', 'J'), ('J.', 'j.'), ('É.', 'E\u0301.'), ('A\u0301\u0327.', 'A\u0327\u0301.'),
                  ('J. O’Neil-Smith', 'O’Neil-Smith J.'), ("J. O'Neil-Smith", 'J. O’Neil-Smith')]]
        pairs += [(f'<v.{a} J. O’Neil-Smith>Hello there</v>', f'<v.{b} J. O’Neil-Smith>Hello there</v>')
                  for a, b in [('a', 'A'), ('a.b', 'b.a'), ('a.a', 'a')]]
        pairs += [('<lang.a en><v J. O’Neil-Smith>Hello there</v></lang>', '<v J. O’Neil-Smith><lang.a en>Hello there</lang></v>'),
                  ('<v J. O’Neil-Smith>H</v>ello there', '<v J. O’Neil-Smith>He</v>llo there')]
        for old, new in pairs:
            for word in (False, True):
                result = convert(document(old, new + '\nNew line'), output_format='vtt', word_overlap=word)
                cues = self.assert_mappings(document(old, new + '\nNew line'), result)
                self.assertEqual(result.report['summary']['removed_tokens'], 0)
                self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])

    def test_initial_context_does_not_relax_repair_guards(self):
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

    def test_bad_initials_and_unsupported_structures(self):
        labels = ['.J', 'J..', '.J.', 'J.J', 'J.J.', 'J.\u0301', '.', '..',
                  '1.', '١.', '_.', '-.', 'J1.', 'J_.', 'J-.', '-J.', "J'.", 'J’.', "J’A.",
                  'J-A.', "J.’A", '\u0301.', '\u0301J.', 'J。', 'J．', 'J.🙂', 'J\u200d.',
                  'J&amp;.', 'speaker="J."', '', ' J.', 'J. ', 'J.  李', 'J.\t李', 'J.\n李', 'J.\u00a0李']
        payloads = [f'<{voice} {label}>Hello there</v>' for voice in ('v', 'v.a') for label in labels]
        payloads += ['<v.́é J. O’Neil-Smith>Hello there</v>', '<v..a J. O’Neil-Smith>Hello there</v>',
                     '<v.a J. O’Neil-Smith>Hello there</v.a>', '<v J. O’Neil-Smith>Hello there',
                     '<lang.a en><v J. O’Neil-Smith>Hello there</v>', '<v J. O’Neil-Smith><b>Hello there</v></b>',
                     '<lang.a én><v J. O’Neil-Smith>Hello there</v></lang>',
                     '<lang.é en><v J. O’Neil-Smith>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<v J. O’Neil-Smith>Hello <00:00.500>there</v>']
        sources = [document(p) for p in payloads]
        sources += [document(wrap('Hello there')).replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_punctuation_unicode_glyph_corruption_blocks_publication(self):
        source = document('<lang.a en><v.a J. O’Neil-Smith>H</v>ello <v E\u0301. O’Ne\u0301il-Smith>there</v></lang>')
        for old, new in [('<v.a J. O’Neil-Smith>', '<v.a J O’Neil-Smith>'),
                         ('<v.a J. O’Neil-Smith>', '<v.a j. O’Neil-Smith>'),
                         ('<v E\u0301. O’Ne\u0301il-Smith>', '<v É. O’Ne\u0301il-Smith>'),
                         ('<v.a J. O’Neil-Smith>H</v>ello', '<v.a J. O’Neil-Smith>He</v>llo')]:
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

    def test_srt_broader_labels_and_cue_end_policy_unchanged(self):
        for payload in ('<v J.>Hello there</v>', '<v Dr.>Hello there', '<v J..>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
