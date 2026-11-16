"""U+2011 joins letter/mark speaker runs without normalizing opaque VTT contexts."""
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
    return f'<lang.em en><v.em Dr. Jean‑Luc O’Neil-Smith>{text}</v></lang>'


POSITIVE_LABELS = [
    'Jean‑Luc', 'O’Neil‑Smith', "O'Neil‑Smith", "A‑B-C’D'E", 'A-B‑C',
    'A\u0301‑B\u0903‑C\u20dd', '李‑明', 'ǅ‑ʰ‑Борис', 'Éva‑Li', 'E\u0301va‑Li',
    'A\u0301\u0327‑B', 'A\u0327\u0301‑B', 'Dr. J. Jean‑Luc O’Neil-Smith A_1 -- -x 2- _',
]
NEGATIVE_LABELS = [
    '‑A', 'A‑', 'A‑‑B', 'A‑-B', 'A-‑B', "A‑'B", 'A’‑B', 'A‑’B', "A'‑B",
    'A‑\u0301B', 'A‑\u0903B', 'A‑\u20ddB', '\u0301A‑B', '\u0301‑B', 'A‑\u0301',
    '1A‑B', 'A1‑B', 'A‑B2', 'A_‑B', 'A‑_B', 'A‑B_', 'A‑١', 'A‑B²',
    'A.‑B', 'A‑B.', 'A‑B.C', 'A‑B‑.', 'Jean‐Luc', 'Jean–Luc', 'Jean—Luc',
    'A‑B‐C', 'A‑B–C', 'A‑B—C', 'A‑B−C', 'A‑B‘C', 'A‑B/C', 'A‑B🙂', 'A‑\u200dB',
    'A‑B&amp;C', 'A&#8209;B', 'speaker="Jean‑Luc"', '', ' Jean‑Luc', 'Jean‑Luc ',
    'Jean‑Luc  李', 'Jean‑Luc\t李', 'Jean‑Luc\n李', 'Jean‑Luc\u00a0李',
]


class VttNonbreakingVoiceTests(unittest.TestCase):
    def assert_mappings(self, source, result):
        return language.VttLanguageTests.assert_mappings(self, source, result)

    def test_exact_rewrapped_nonbreaking_contract(self):
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
                             (('lang.em en', 'v.em Dr. Jean‑Luc O’Neil-Smith'),) * 3)
            self.assertEqual([(r['source_char_start'], r['source_char_end'], r['source_line'])
                              for r in result.provenance['tokens'][:3]],
                             [(44, 46, 1), (47, 50, 2), (51, 56, 2)])
            self.assertEqual([n['text'] for n in core._parse_vtt_document(result.vtt, preserve_metadata=True).notes],
                             ['NOTE before seed', 'NOTE before growth', 'NOTE end'])
            self.assertEqual(result.report['summary']['removed_tokens'], 3)
            self.assertFalse(result.report['warnings'])

    def test_letter_mark_runs_mixed_separators_and_full_stack(self):
        for label in POSITIVE_LABELS:
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
        nested = lambda s: f'<c.em><lang.a.B.a en-US><v.a E\u0301va‑Li O’Neil‑Smith><v Éva‑Li O\'Neil‑Smith><u.a><b.b>{s}</b></u></v></v></lang></c>'
        source = document(nested('We\nare ready'), nested('are\nready now'), nested('ready now today'))
        cues = self.assert_mappings(source, convert(source, output_format='vtt', word_overlap=True))
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in cues],
                         [(0, 2000, nested('We')), (0, 3000, nested('are')), (0, 4000, nested('ready')),
                          (1000, 4000, nested('now')), (2000, 4000, nested('today'))])
        old = '<lang.a en><v Jean‑Luc>H</v>ello <v.b E\u0301va‑Li>th<c.x>er</c>e</v> &amp;</lang>'
        cues = self.assert_mappings(document(old, old + ' now'),
                                    convert(document(old, old + ' now'), output_format='vtt', word_overlap=True))
        self.assertEqual(cues[0].raw, old)
        self.assertEqual(cues[0].tokens[0].glyph_contexts, (('lang.a en', 'v Jean‑Luc'),) + (('lang.a en',),) * 4)
        self.assertEqual(convert(document('<v Jean‑Luc></v>Hello<v.a O’Neil‑Smith> \t</v>there'),
                                 output_format='vtt').vtt.splitlines()[-1], 'Hello there')

    def test_opaque_codepoints_classes_stacks_and_boundaries(self):
        pairs = [(f'<v.a {a}>Hello there</v>', f'<v.a {b}>Hello there</v>') for a, b in
                 [('Jean‑Luc', 'Jean-Luc'), ('Jean‑Luc', 'JeanLuc'), ('Jean‑Luc', 'jean‑Luc'),
                  ('Éva‑Li', 'E\u0301va‑Li'), ('A\u0301\u0327‑B', 'A\u0327\u0301‑B'),
                  ('Jean‑Luc 李', '李 Jean‑Luc'), ("O'Neil‑Smith", 'O’Neil‑Smith')]]
        pairs += [(f'<v.{a} Jean‑Luc>Hello there</v>', f'<v.{b} Jean‑Luc>Hello there</v>')
                  for a, b in [('a', 'A'), ('a.b', 'b.a'), ('a.a', 'a')]]
        pairs += [('<lang.a en><v Jean‑Luc>Hello there</v></lang>', '<v Jean‑Luc><lang.a en>Hello there</lang></v>'),
                  ('<v Jean‑Luc><v O’Neil‑Smith>Hello there</v></v>', '<v O’Neil‑Smith><v Jean‑Luc>Hello there</v></v>'),
                  ('<v Jean‑Luc>H</v>ello there', '<v Jean‑Luc>He</v>llo there')]
        for old, new in pairs:
            for word in (False, True):
                source = document(old, new + '\nNew line')
                result = convert(source, output_format='vtt', word_overlap=word)
                cues = self.assert_mappings(source, result)
                self.assertEqual(result.report['summary']['removed_tokens'], 0)
                self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(0, 2000), (1000, 3000)])

    def test_nonbreaking_context_does_not_relax_repair_guards(self):
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

    def test_bad_nonbreaking_words_and_unsupported_structures(self):
        payloads = [f'<{voice} {label}>Hello there</v>' for voice in ('v', 'v.a') for label in NEGATIVE_LABELS]
        payloads += ['<v.́é Jean‑Luc>Hello there</v>', '<v..a Jean‑Luc>Hello there</v>',
                     '<v.a Jean‑Luc>Hello there</v.a>', '<v Jean‑Luc>Hello there',
                     '<lang.a en><v Jean‑Luc>Hello there</v>', '<v Jean‑Luc><b>Hello there</v></b>',
                     '<lang.a én><v Jean‑Luc>Hello there</v></lang>',
                     '<lang.é en><v Jean‑Luc>Hello there</v></lang>',
                     '<ruby>Hello there</ruby>', '<x>Hello there</x>', '<v Jean‑Luc>Hello <00:00.500>there</v>']
        sources = [document(p) for p in payloads]
        sources += [document(wrap('Hello there')).replace('source-1', block + '\n\nsource-1')
                    for block in ('STYLE\n::cue { color: red }', 'REGION\nid:main')]
        for source in sources:
            for flatten in (False, True):
                with self.subTest(source=source, flatten=flatten), self.assertRaises(CaptionError):
                    convert(source, output_format='vtt', flatten=flatten)

    def test_readback_codepoint_context_glyph_corruption_blocks_both_files(self):
        source = document('<lang.a en><v.a Jean‑Luc>H</v>ello <v E\u0301va‑Li>there</v></lang>')
        for old, new in [('<v.a Jean‑Luc>', '<v.a Jean-Luc>'), ('<v.a Jean‑Luc>', '<v.a JeanLuc>'),
                         ('<v E\u0301va‑Li>', '<v Éva‑Li>'), ('<v.a Jean‑Luc>', '<v.b Jean‑Luc>'),
                         ('<v.a Jean‑Luc>H</v>ello', '<v.a Jean‑Luc>He</v>llo')]:
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
        for payload in ('<v Jean‑Luc>Hello there</v>', '<v Jean‑Luc>Hello there', '<v A‑‑B>Hello there'):
            source = 'WEBVTT\n\n00:00.000 --> 00:02.000\n' + payload + '\n'
            with self.assertRaisesRegex(CaptionError, 'flatten'):
                convert(source)
            result = convert(source, flatten=True)
            self.assertIn('Hello there', result.srt)
            self.assertEqual(result.report['warnings'][0]['kind'], 'lossy_flattening')
