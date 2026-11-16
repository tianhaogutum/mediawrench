"""Opaque, explicitly balanced restricted language labels in preserved VTT."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import cli, core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


class VttLanguageTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        cues = formatting.VttFormattingTests.assert_mappings(self, source, result)
        original, _ = core.parse_vtt(source, preserve_metadata=True)
        self.assertEqual([r['raw_text'] for r in result.provenance['source_cues']],
                         [c.raw for c in original])
        return cues

    def test_rewrapped_language_voice_contract(self):
        source = document('<lang en><v.em Ada>We\nare ready</v></lang>',
                          '<lang en><v.em Ada>We are\nready now</v></lang>',
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<lang en><v.em Ada>We\nare ready</v></lang>'),
                          (1000, 3000, '<lang en><v.em Ada>now</v></lang>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('lang en', 'v.em Ada'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(19, 21, 1), (22, 25, 2), (26, 31, 2)])
        self.assertTrue(result.vtt.startswith('WEBVTT Formatting\nLanguage: en\n'))
        self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                         ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
        self.assertFalse(result.report['warnings'])
        self.assertIn('language', result.report['loss_policy']['inline_markup_timestamps'])

    def test_whole_line_opaque_labels_and_full_nested_stack(self):
        # These are grammar tests, not BCP47 validity or canonicalization claims.
        for label in ('en', 'EN-us', 'zh-Hant-TW', 'x-123', 'English-madeup-999999999999999'):
            wrap = lambda s: f'<b.a><lang {label}><v.a.B.a Ada Lovelace><lang fr><c.x><i.y><u.z>{s}</u></i></c></lang></v></lang></b>'
            source = document(wrap('Hello there'), wrap('Hello there') + '\nNew line')
            cues = self.assert_mappings(source, convert(source, output_format='vtt'))
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('Hello there')), (1000, 3000, 'New line')])
            self.assertEqual(cues[0].tokens[0].glyph_contexts,
                             (('b.a', f'lang {label}', 'v.a.B.a Ada Lovelace', 'lang fr', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_spans_balance_all_scopes(self):
        wrap = lambda s: f'<c.em><lang en-US><v.a Ada><u.a><b.b>{s}</b></u></v></lang></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])

    def test_within_word_entities_and_whitespace_canonicalization(self):
        old = '<lang en>H</lang>ello <lang fr>th<v.em Ada>er</v>e</lang> &amp;'
        source = document(old, old + ' now')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('lang en',), (), (), (), ()))
        for record, raw in zip(result.provenance['tokens'][:3], ['H</lang>ello', 'th<v.em Ada>er</v>e', '&amp;']):
            self.assertEqual(old[record['source_char_start']:record['source_char_end']], raw)
        source = document('<lang en></lang>Hello<lang fr> \t</lang>there')
        self.assertEqual(self.assert_mappings(source, convert(source, output_format='vtt'))[0].raw, 'Hello there')

    def test_context_conflicts_retain_both_displays(self):
        wrap = lambda label, s: f'<lang {label}>{s}</lang>'
        payloads = [(wrap(a, 'Hello there'), wrap(b, 'Hello there')) for a, b in
                    [('en', 'EN'), ('en-US', 'en-us'), ('x-Ab-Cd', 'x-Cd-Ab')]]
        payloads += [('<lang en>Hello there</lang>', 'Hello there'),
                     ('<lang en>H</lang>ello there', '<lang en>He</lang>llo there'),
                     ('<lang en><lang fr>Hello there</lang></lang>', '<lang fr><lang en>Hello there</lang></lang>'),
                     ('<lang en><lang en>Hello there</lang></lang>', '<lang en>Hello there</lang>'),
                     ('<lang en><v.a Ada>Hello there</v></lang>', '<v.a Ada><lang en>Hello there</lang></v>')]
        payloads += [(f'<lang en><v.{a} {speaker}>Hello there</v></lang>',
                      f'<lang en><v.{b} {other}>Hello there</v></lang>')
                     for a, b, speaker, other in [('a', 'A', 'Ada', 'Ada'), ('a.b', 'b.a', 'Ada', 'Ada'),
                                                   ('a.a', 'a', 'Ada', 'Ada'), ('a', 'a', 'Ada', 'Bob')]]
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

    def test_conservative_evidence_time_settings_and_opt_in_guards(self):
        wrap = lambda s: f'<lang en><v.a Ada>{s}</v></lang>'
        for old, new in [('go now go now', 'go now go now today'), ('We are ready', 'are ready'),
                         ('yes', 'yes please'), ('. !', '. ! next')]:
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
        # Identical nested intervals union only under strict overlap.
        same = document(wrap('We are'), wrap('We are')).replace('00:03.000\n', '00:01.500\n')
        cues = self.assert_mappings(same, convert(same, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000)])

    def test_malformed_and_unsupported_contracts_even_with_flatten(self):
        labels = ['', ' en', 'en ', 'en us', 'en\tUS', 'en\nUS', 'én', 'en_US', 'en.US',
                  '-en', 'en-', 'en--US', '123', '1-en', 'en-&amp;', 'en/US', 'label="en"']
        payloads = [f'<lang {label}>Hello there</lang>' for label in labels]
        payloads += ['<lang>Hello there</lang>', '<lang\ten>Hello there</lang>',
                     '<lang.a. en>Hello there</lang>', '<lang. en>Hello there</lang>',
                     '<lang en>Hello there</lang.a>', '<lang en>Hello there</lang en>',
                     '<lang en>Hello there</lang >', '<LANG en>Hello there</LANG>',
                     '<lang en>Hello there', '<lang en><v.a Ada>Hello there</v>',
                     '<lang en/>Hello there', '<lang en><b>Hello there</lang></b>',
                     '<lang en>Hello there</lang></lang>', '<lang en>Hello there</v>',
                     '<lang en><c.>Hello there</c></lang>', '<lang en><v. Ada>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<lang en>Hello <00:00.500>there</lang>']
        sources = [document(payload) for payload in payloads]
        sources += [document('<lang en>Hello there</lang>').replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        sources += [document('<lang en>Hello there</lang>', settings=' region:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_corruption_blocks_publication(self):
        # The two within-word variants share the same distinct-context list.
        source = document('<lang en>H</lang>ello <v.a Ada>there</v>')
        for old, new in [('<lang en>', '<lang EN>'), ('<v.a Ada>', '<v.b Ada>'),
                         ('<v.a Ada>', '<v.a Bob>'), ('<lang en>H</lang>ello', '<lang en>He</lang>llo')]:
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

    def test_srt_existing_broader_grammar_and_cue_end_policy(self):
        for payload in ('<lang en>Hello there</lang>', '<lang én>Hello there',
                        '<lang.a en_US>Hello there', '<lang en--US>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
