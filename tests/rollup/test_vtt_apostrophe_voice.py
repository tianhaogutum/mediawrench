"""Single internal ASCII/curly apostrophes join letter/mark speaker runs only."""
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
    return f'<lang.em en><v.em O’Neil>{text}</v></lang>'


class VttApostropheVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return language.VttLanguageTests.assert_mappings(self, source, result)

    def test_exact_rewrapped_apostrophe_contract(self):
        source = document(wrap('We\nare ready'), wrap('We are\nready now'),
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, wrap('We\nare ready')), (1000, 3000, wrap('now'))])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('lang.em en', 'v.em O’Neil'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(25, 27, 1), (28, 31, 2), (32, 37, 2)])
        self.assertTrue(result.vtt.startswith('WEBVTT Formatting\nLanguage: en\n'))
        self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                         ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
        self.assertEqual(result.report['summary']['removed_tokens'], 3)
        self.assertFalse(result.report['warnings'])

    def test_permitted_runs_flanks_and_whole_line_full_stack(self):
        # Left flank L/M, right flank L; each entire apostrophe word is letters/marks.
        # The old digit/underscore/hyphen grammar is still allowed in other words.
        labels = ["O'Neil", 'O’Neil', "D’A'B", "A\u0301'B", 'A\u0903’B\u20dd',
                  'ǅ’ʰ', "李'明", 'Αλφα’Борис', "O'Neil A_1 -x 2", 'A’B C’D']
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

    def test_sliding_and_within_word_stacks(self):
        nested = lambda s: f'<c.em><lang.a.B.a en-US><v.a O’Ne\u0301il><v O\'Neil><u.a><b.b>{s}</b></u></v></v></lang></c>'
        source = document(nested('We\nare ready'), nested('are\nready now'), nested('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, nested('We')), (0, 3000, nested('are')), (0, 4000, nested('ready')),
                          (1000, 4000, nested('now')), (2000, 4000, nested('today'))])
        old = '<lang.a en><v O’Neil>H</v>ello <v.b O\'Neil>th<c.x>er</c>e</v> &amp;</lang>'
        source = document(old, old + ' now')
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('lang.a en', 'v O’Neil'),) + (('lang.a en',),) * 4)
        source = document('<v O’Neil></v>Hello<v.a O\'Neil> \t</v>there')
        self.assertEqual(self.assert_mappings(source, convert(source, output_format='vtt'))[0].raw, 'Hello there')

    def test_opaque_apostrophes_codepoints_classes_and_boundaries(self):
        pairs = [(f'<v.a {a}>Hello there</v>', f'<v.a {b}>Hello there</v>')
                 for a, b in [("O'Neil", 'O’Neil'), ('O’Néil', 'O’Ne\u0301il'), ('O’Neil', 'o’Neil'),
                              ('O’Neil 李', '李 O’Neil'), ('A\u0301\u0327’B', 'A\u0327\u0301’B')]]
        pairs += [(f'<v.{a} O’Neil>Hello there</v>', f'<v.{b} O’Neil>Hello there</v>')
                  for a, b in [('a', 'A'), ('a.b', 'b.a'), ('a.a', 'a')]]
        pairs += [('<lang.a en><v O’Neil>Hello there</v></lang>', '<v O’Neil><lang.a en>Hello there</lang></v>'),
                  ('<v O’Neil><v O\'Neil>Hello there</v></v>', '<v O\'Neil><v O’Neil>Hello there</v></v>'),
                  ('<v O’Neil>H</v>ello there', '<v O’Neil>He</v>llo there')]
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

    def test_apostrophe_context_keeps_evidence_time_settings_guards(self):
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

    def test_bad_apostrophe_flanks_and_unsupported_structures(self):
        labels = ['', "'", '’', "'Ada", '’Ada', "Ada'", 'Ada’', "A''B", 'A’’B', "A’'B",
                  "A'\u0301B", 'A’\u0903B', "1'A", "A'1", "A1'B", 'A’B1', "A_'B", 'A-’B', "A'B_", 'A’B-',
                  "A' B", 'A ’B', ' O’Neil', 'O’Neil ', 'O’Neil  李', 'O’Neil\t李', 'O’Neil\n李',
                  '\u0301O’Neil', 'O’Neil\u00a0李', 'A‘B', 'A.B', 'A/B', "A'B&amp;C", 'speaker="O’Neil"',
                  'A’١', 'A’🙂', 'A’\u200dB']
        payloads = [f'<{voice} {label}>Hello there</v>' for voice in ('v', 'v.a') for label in labels]
        payloads += ['<v.́é O’Neil>Hello there</v>', '<v..a O’Neil>Hello there</v>',
                     '<v.a O’Neil>Hello there</v.a>', '<v O’Neil>Hello there</v >',
                     '<v O’Neil>Hello there', '<lang.a en><v O’Neil>Hello there',
                     '<v O’Neil><b>Hello there</v></b>', '<v O’Neil>Hello there</v></v>',
                     '<lang.a én><v O’Neil>Hello there</v></lang>',
                     '<lang.é en><v O’Neil>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<v O’Neil>Hello <00:00.500>there</v>']
        sources = [document(p) for p in payloads]
        sources += [document(wrap('Hello there')).replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_apostrophe_unicode_glyph_corruption_blocks_publication(self):
        source = document('<lang.a en><v.a O’Neil>H</v>ello <v O’Ne\u0301il>there</v></lang>')
        for old, new in [('<v.a O’Neil>', "<v.a O'Neil>"), ('<v O’Ne\u0301il>', '<v O’Néil>'),
                         ('<v.a O’Neil>', '<v.a o’Neil>'), ('<v.a O’Neil>', '<v.b O’Neil>'),
                         ('<v.a O’Neil>H</v>ello', '<v.a O’Neil>He</v>llo')]:
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

    def test_srt_lossy_grammar_and_cue_end_stay_broader(self):
        for payload in ("<v.a O'Neil>Hello there", '<v O’Neil>Hello there', "<v A'1>Hello there",
                        "<v A''B>Hello there", '<v \u0301O’Neil>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            self.assertIn('Hello there', convert(source, flatten=True).srt)
