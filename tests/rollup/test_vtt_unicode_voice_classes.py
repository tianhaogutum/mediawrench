"""Unicode L/M classes belong only to explicitly balanced preserved VTT voices."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.rollup import CaptionError, convert
from mediawrench.rollup import cli, core
from . import test_vtt_formatting as formatting
from .test_vtt_formatting import document


POSITIVE_CLASSES = ['声', 'ém', 'e\u0301', 'a.B.a', '0_--', '0\u0301', '-\u0903',
                    '_\u20dd', 'ǅ.ʰ.李', 'A\u0301\u0327', 'A\u0327\u0301']
NEGATIVE_CLASSES = ['', 'a.', '.a', 'a..b', '\u0301', '\u0301a', 'a.\u0301b', '١',
                    'a١', 'a²', 'a’b', "a'b", 'a‑b', 'a·b', 'a!', 'a/b', 'a=b',
                    'a&amp;', 'a&#233;', 'a\tb', 'a\nb', 'a\u00a0b']


def wrap(text):
    return f'<lang.em en><v.声 Dr. Jean‑Luc O’Neil-Smith>{text}</v></lang>'


class VttUnicodeVoiceClassTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return formatting.VttFormattingTests.assert_mappings(self, source, result)

    def test_exact_rewrapped_contract(self):
        source = document(wrap('We\nare ready'), wrap('We are\nready now'),
                          settings=' align:start position:20%')
        source = source.replace('source-1\n', 'NOTE before seed\n\nsource-1\n')
        source = source.replace('source-2\n', 'NOTE before growth\n\nsource-2\n') + '\nNOTE end\n'
        for flatten in (False, True):
            result = convert(source, output_format='vtt', word_overlap=True, flatten=flatten)
            cues = self.assert_mappings(source, result)
            self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                             [(0, 3000, wrap('We\nare ready')), (1000, 3000, wrap('now'))])
            self.assertEqual(cues[1].tokens[0].glyph_contexts,
                             (('lang.em en', 'v.声 Dr. Jean‑Luc O’Neil-Smith'),) * 3)
            self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                              for r in result.provenance['tokens'][:3]],
                             [(43, 45, 1), (46, 49, 2), (50, 55, 2)])
            self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                             ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
            self.assertEqual(result.report['summary']['removed_tokens'], 3)
            self.assertFalse(result.report['warnings'])

    def test_class_categories_and_ordered_full_stacks(self):
        for classes in POSITIVE_CLASSES:
            with self.subTest(classes=classes):
                nested = lambda s: f'<b.a><lang.em en><v.{classes} Dr. Jean‑Luc O’Neil-Smith><v.声.声 Éva><c.x><i.y><u.z>{s}</u></i></c></v></v></lang></b>'
                source = document(nested('Hello there'), nested('Hello there') + '\nNew line')
                cues = self.assert_mappings(source, convert(source, output_format='vtt'))
                self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                                 [(0, 3000, nested('Hello there')), (1000, 3000, 'New line')])
                self.assertEqual(cues[0].tokens[0].glyph_contexts,
                                 (('b.a', 'lang.em en', f'v.{classes} Dr. Jean‑Luc O’Neil-Smith',
                                   'v.声.声 Éva', 'c.x', 'i.y', 'u.z'),) * 5)

    def test_sliding_within_word_and_empty_scope_canonicalization(self):
        nested = lambda s: f'<c.em><lang.a.B.a en-US><v.e\u0301.声.e\u0301 E\u0301va‑Li><v.ém O’Neil-Smith><u.a><b.b>{s}</b></u></v></v></lang></c>'
        source = document(nested('We\nare ready'), nested('are\nready now'), nested('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, nested('We')), (0, 3000, nested('are')), (0, 4000, nested('ready')),
                          (1000, 4000, nested('now')), (2000, 4000, nested('today'))])
        old = '<lang.a en><v.声 Jean‑Luc>H</v>ello <v.e\u0301 E\u0301va>th<c.x>er</c>e</v> &amp;</lang>'
        source = document(old, old + ' now')
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts,
                         (('lang.a en', 'v.声 Jean‑Luc'),) + (('lang.a en',),) * 4)
        self.assertEqual(convert(document('<v.声 Ada></v>Hello<v.e\u0301 Ada> \t</v>there'),
                                 output_format='vtt').vtt.splitlines()[-1], 'Hello there')

    def test_opaque_class_label_stack_and_boundary_conflicts(self):
        pairs = [(f'<v.{a} Ada>Hello there</v>', f'<v.{b} Ada>Hello there</v>') for a, b in
                 [('é', 'e\u0301'), ('É', 'é'), ('声.a', 'a.声'), ('声.声', '声'),
                  ('A\u0301\u0327', 'A\u0327\u0301')]]
        pairs += [(f'<v.声 {a}>Hello there</v>', f'<v.声 {b}>Hello there</v>') for a,b in
                  [('Dr.', 'Dr'), ('Jean‑Luc', 'Jean-Luc'), ('Éva', 'E\u0301va'),
                   ('Ada 李', '李 Ada'), ("O'Neil", 'O’Neil')]]
        pairs += [('<lang.a en><v.声 Ada>Hello there</v></lang>', '<v.声 Ada><lang.a en>Hello there</lang></v>'),
                  ('<v.声 Ada><v.ém Bob>Hello there</v></v>', '<v.ém Bob><v.声 Ada>Hello there</v></v>'),
                  ('<v.声 Ada>H</v>ello there', '<v.声 Ada>He</v>llo there')]
        for old, new in pairs:
            for word in (False, True):
                source = document(old, new + '\nNew line')
                result = convert(source, output_format='vtt', word_overlap=word)
                cues = self.assert_mappings(source, result)
                self.assertEqual(result.report['summary']['removed_tokens'], 0)
                self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])

    def test_unicode_classes_do_not_relax_repair_guards(self):
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

    def test_bad_classes_nonvoice_ascii_speaker_and_structure_boundaries(self):
        payloads = [f'<v.{classes} Ada>Hello there</v>' for classes in NEGATIVE_CLASSES]
        payloads += [f'<{tag}.声{label}>Hello there</{tag}>' for tag, label in
                     [('b', ''), ('i', ''), ('u', ''), ('c', ''), ('lang', ' en')]]
        payloads += ['<v.声 Ada>Hello there', '<v.声 Ada>Hello there</v.声>',
                     '<v.声 Ada>Hello there</v Ada>', '<v.声 Ada><b>Hello there</v></b>',
                     '<lang.声 en><v.声 Ada>Hello there</v></lang>', '<lang én>Hello there</lang>',
                     '<v.声>Hello there</v>', '<v.声 >Hello there</v>', '<v.声  Ada>Hello there</v>',
                     '<v.声 Ada >Hello there</v>', '<v.声 Ada\tLi>Hello there</v>',
                     '<v.声 Ada\u00a0Li>Hello there</v>', '<v.声 A&amp;B>Hello there</v>',
                     '<v.声 speaker="Ada">Hello there</v>', '<v.声 A‑‑B>Hello there</v>',
                     '<v.声 A·B>Hello there</v>', '<ruby>Hello there</ruby>', '<x>Hello there</x>',
                     '<v.声 Ada>Hello <00:00.500>there</v>']
        sources = [document(p) for p in payloads]
        sources += [document(wrap('Hello there')).replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_class_codepoint_and_glyph_corruption_blocks_both_files(self):
        source = document('<lang.a en><v.声 Ada>H</v>ello <v.e\u0301 Éva>there</v></lang>')
        for old, new in [('<v.声 Ada>', '<v.字 Ada>'), ('<v.e\u0301 Éva>', '<v.é Éva>'),
                         ('<v.声 Ada>', '<v.声 Bob>'),
                         ('<v.声 Ada>H</v>ello', '<v.声 Ada>He</v>llo')]:
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

    def test_srt_class_grammar_and_broader_cue_end_policy_unchanged(self):
        for payload in ('<v.声 Ada>Hello there</v>', '<v.e\u0301 Ada>Hello there', '<v.ém Ada>Hello there</v>'):
            for flatten in (False, True):
                with self.assertRaises(CaptionError):
                    convert('WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n', flatten=flatten)
        for payload in ('<v.a Ada>Hello there</v>', '<v.a A‑‑B>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            self.assertIn('Hello there', convert(source, flatten=True).srt)
