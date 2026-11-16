"""Class-suffixed language scopes retain opaque exact glyph stacks in VTT."""
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


class VttClassedLanguageTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return language.VttLanguageTests.assert_mappings(self, source, result)

    def test_rewrapped_classed_language_contract(self):
        source = document('<lang.em en><v.em Ada>We\nare ready</v></lang>',
                          '<lang.em en><v.em Ada>We are\nready now</v></lang>',
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 3000, '<lang.em en><v.em Ada>We\nare ready</v></lang>'),
                          (1000, 3000, '<lang.em en><v.em Ada>now</v></lang>')])
        self.assertEqual(cues[1].tokens[0].glyph_contexts, (('lang.em en', 'v.em Ada'),) * 3)
        self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                          for r in result.provenance['tokens'][:3]],
                         [(22, 24, 1), (25, 28, 2), (29, 34, 2)])
        self.assertTrue(result.vtt.startswith('WEBVTT Formatting\nLanguage: en\n'))
        self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                         ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
        self.assertFalse(result.report['warnings'])

    def test_whole_line_nested_opaque_classes_labels_and_stack(self):
        for tag in ('lang.a en', 'lang.a.B.a EN-us', 'lang.A_1.-x.2 zh-Hant-TW',
                    'lang.0 x-123', 'lang.a English-madeup-999999999999999'):
            wrap = lambda s: f'<b.a><{tag}><v.a.B.a Ada Lovelace><lang.inner fr><c.x><i.y><u.z>{s}</u></i></c></lang></v></lang></b>'
            source = document(wrap('Hello there'), wrap('Hello there') + '\nNew line')
            cues = self.assert_mappings(source, convert(source, output_format='vtt'))
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('Hello there')), (1000, 3000, 'New line')])
            self.assertEqual(cues[0].tokens[0].glyph_contexts,
                             (('b.a', tag, 'v.a.B.a Ada Lovelace', 'lang.inner fr', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_spans_balance_full_stack(self):
        wrap = lambda s: f'<c.em><lang.a.B.a en-US><v.a Ada><lang.inner fr><u.a><b.b>{s}</b></u></lang></v></lang></c>'
        source = document(wrap('We\nare ready'), wrap('are\nready now'), wrap('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, wrap('We')), (0, 3000, wrap('are')), (0, 4000, wrap('ready')),
                          (1000, 4000, wrap('now')), (2000, 4000, wrap('today'))])
        self.assertNotIn('</lang.', '\n'.join(c.raw for c in cues))

    def test_within_word_entities_and_empty_whitespace_canonicalization(self):
        old = '<lang.a en>H</lang>ello <lang.b fr>th<v.em Ada>er</v>e</lang> &amp;'
        source = document(old, old + ' now')
        result = convert(source, output_format='vtt', word_overlap=True)
        cues = self.assert_mappings(source, result)
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('lang.a en',), (), (), (), ()))
        for record, raw in zip(result.provenance['tokens'][:3], ['H</lang>ello', 'th<v.em Ada>er</v>e', '&amp;']):
            self.assertEqual(old[record['source_char_start']:record['source_char_end']], raw)
        source = document('<lang.a en></lang>Hello<lang.b fr> \t</lang>there')
        self.assertEqual(self.assert_mappings(source, convert(source, output_format='vtt'))[0].raw, 'Hello there')

    def test_context_conflicts_retain_both_displays(self):
        pairs = [('lang.a en', 'lang.A en'), ('lang.a.b en', 'lang.b.a en'),
                 ('lang.a.a en', 'lang.a en'), ('lang.a en', 'lang en'),
                 ('lang.a en', 'lang.a EN'), ('lang.a en-US', 'lang.a en-us'),
                 ('lang.a x-Ab-Cd', 'lang.a x-Cd-Ab')]
        payloads = [(f'<{a}>Hello there</lang>', f'<{b}>Hello there</lang>') for a, b in pairs]
        payloads += [('<lang.a en>H</lang>ello there', '<lang.a en>He</lang>llo there'),
                     ('<lang.a en><lang.b fr>Hello there</lang></lang>', '<lang.b fr><lang.a en>Hello there</lang></lang>'),
                     ('<lang.a en><lang.a en>Hello there</lang></lang>', '<lang.a en>Hello there</lang>'),
                     ('<lang.a en><v.a Ada>Hello there</v></lang>', '<v.a Ada><lang.a en>Hello there</lang></v>')]
        payloads += [(f'<lang.a en><v.{a} {speaker}>Hello there</v></lang>',
                      f'<lang.a en><v.{b} {other}>Hello there</v></lang>')
                     for a, b, speaker, other in [('a', 'A', 'Ada', 'Ada'), ('a.b', 'b.a', 'Ada', 'Ada'),
                                                 ('a.a', 'a', 'Ada', 'Ada'), ('a', 'a', 'Ada', 'Bob')]]
        for old, new in payloads:
            for word in (False, True):
                with self.subTest(old=old, new=new, word=word):
                    result = convert(document(old, new + '\nNew line'), output_format='vtt', word_overlap=word)
                    cues = self.assert_mappings(document(old, new + '\nNew line'), result)
                    self.assertEqual(result.report['summary']['removed_tokens'], 0)
                    self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])
                    if word:
                        self.assertIn('contexts', result.report['ambiguities'][0]['reason'])

    def test_conservative_evidence_time_settings_and_opt_in_guards(self):
        wrap = lambda s: f'<lang.a en><v.a Ada>{s}</v></lang>'
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
        same = document(wrap('We are'), wrap('We are')).replace('00:03.000\n', '00:01.500\n')
        cues = self.assert_mappings(same, convert(same, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000)])

    def test_malformed_and_unsupported_contracts_even_with_flatten(self):
        labels = ['', ' en', 'en ', 'en us', 'en\tUS', 'en\nUS', 'én', 'en_US', 'en.US',
                  '-en', 'en-', 'en--US', '123', '1-en', 'en-&amp;', 'en/US', 'label="en"']
        payloads = [f'<lang.a {label}>Hello there</lang>' for label in labels]
        payloads += [f'<{tag}>Hello there</lang>' for tag in
                     ['lang.', 'lang.a', 'lang.a ', 'lang. en', 'lang..a en', 'lang.a. en',
                      'lang.é en', 'lang.a! en', 'lang.a/b en', 'lang.a\ten', 'LANG.a en', 'lang.a en/']]
        payloads += ['<lang.a en>Hello there</lang.a>', '<lang.a en>Hello there</lang en>',
                     '<lang.a en>Hello there</lang >', '<lang.a en>Hello there',
                     '<lang.a en><v.a Ada>Hello there</v>', '<lang.a en><b>Hello there</lang></b>',
                     '<lang.a en>Hello there</lang></lang>', '<lang.a en>Hello there</v>',
                     '<lang.a en><c.>Hello there</c></lang>', '<lang.a en><v. Ada>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<lang.a en>Hello <00:00.500>there</lang>']
        sources = [document(payload) for payload in payloads]
        sources += [document('<lang.a en>Hello there</lang>').replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        sources += [document('<lang.a en>Hello there</lang>', settings=' region:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_context_corruption_blocks_publication(self):
        source = document('<lang.a.b.a en>H</lang>ello <v.a Ada>there</v>')
        for old, new in [('<lang.a.b.a en>', '<lang.a.b en>'), ('<lang.a.b.a en>', '<lang.a.a.b en>'),
                         ('<lang.a.b.a en>', '<lang.A.b.a en>'), ('<lang.a.b.a en>', '<lang.a.b.a EN>'),
                         ('<v.a Ada>', '<v.b Ada>'), ('<v.a Ada>', '<v.a Bob>'),
                         ('<lang.a.b.a en>H</lang>ello', '<lang.a.b.a en>He</lang>llo')]:
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

    def test_srt_existing_broader_classed_language_policy(self):
        for payload in ('<lang.a en>Hello there</lang>', '<lang.a én>Hello there',
                        '<lang.a en_US>Hello there', '<lang.a en--US>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
