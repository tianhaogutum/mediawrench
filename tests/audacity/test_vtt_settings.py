"""Synthetic integration cases for the pinned 2019 WebVTT settings syntax."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.formats import parse_vtt
from .test_bridge import resign

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'examples/audacity/settings.vtt').read_bytes()
FIRST = 'line:20% position:35%,line-left size:60% align:start'
SECOND = 'vertical:rl line:-1,end position:80%,line-right size:40% align:end'


class VTTSettingsTests(unittest.TestCase):
    def test_timing_edit_restores_identifier_settings_and_multiline_text(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        output, report = import_labels(labels.replace('1.000\t2.500', '1.250\t2.750'),
                                       sidecar, output_format='vtt', source=SOURCE)
        self.assertIn('speaker-a\n00:00:01.250 --> 00:00:02.750 ' + FIRST + '\nHello\nworld', output)
        cues, meta = parse_vtt(output)
        self.assertEqual([c.source_id for c in cues], ['speaker-a', 'speaker-b'])
        self.assertEqual([c.metadata['settings'] for c in cues], [FIRST, SECOND])
        self.assertEqual([c.text for c in cues], ['Hello\nworld'] * 2)
        self.assertEqual(meta['header'], 'WEBVTT settings roundtrip')
        self.assertEqual(meta['notes'][0]['text'], sidecar['source']['metadata']['notes'][0]['text'])
        self.assertEqual(report['losses'], [])
        self.assertEqual(report['warnings'], [])
        self.assertEqual(report['output_format'], 'vtt')
        self.assertEqual(report['output_cues'][0]['metadata']['settings'], FIRST)

    def test_crossing_identical_text_keeps_settings_on_bridge_identity(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        rows = labels.splitlines()
        edited = rows[1] + '\n' + rows[0].replace('1.000\t2.500', '5.000\t6.000') + '\n'
        output, report = import_labels(edited, sidecar, output_format='vtt')
        cues, _ = parse_vtt(output)
        self.assertEqual([(c.source_id, c.start_ms, c.metadata['settings']) for c in cues],
                         [('speaker-b', 3000, SECOND), ('speaker-a', 5000, FIRST)])
        self.assertEqual([c['id'] for c in report['output_cues']], ['c2', 'c1'])

    def test_srt_reports_settings_loss_for_each_returned_cue(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        output, report = import_labels(labels, sidecar)
        self.assertIn('00:00:01,000 --> 00:00:02,500\nHello\nworld', output)
        losses = [loss for loss in report['losses'] if loss['kind'] == 'webvtt_cue_settings']
        self.assertEqual([(x['id'], x['source_id'], x['settings']) for x in losses],
                         [('c1', 'speaker-a', FIRST), ('c2', 'speaker-b', SECOND)])
        self.assertTrue(any('settings' in w and 'SRT' in w for w in report['warnings']))

    def test_settings_supported_values_and_lexical_preservation(self):
        values = ['vertical:lr', 'vertical:rl', 'line:0', 'line:-1,start', 'line:001,center',
                  'line:0%,end', 'line:100.0%,center', 'position:0%',
                  'position:100%,line-left', 'position:50.25%,center',
                  'position:00.50%,line-right', 'size:0%', 'size:100.000%',
                  'align:start', 'align:center', 'align:end', 'align:left', 'align:right',
                  'align:right\t size:045.50%']
        for value in values:
            with self.subTest(value=value):
                source = f'WEBVTT\n\nid\n00:01.000 --> 00:02.000 {value}\ntext\n'
                labels, sidecar = export_subtitles(source.encode(), 'vtt')
                output, _ = import_labels(labels, sidecar, output_format='vtt')
                self.assertIn('00:00:01.000 --> 00:00:02.000 ' + value + '\n', output)

    def test_invalid_or_unsupported_settings_never_silently_ignored(self):
        values = ['align:middle', 'align:START', 'vertical:horizontal', 'region:r',
                  'unknown:value', 'align:start align:end', 'line:+1', 'line:1.5',
                  'line:-1%', 'line:101%', 'line:1,left', 'line:auto',
                  'position:50', 'position:50%,start', 'position:101%', 'size:-1%',
                  'size:100.001%', 'size:NaN%', 'size:.5%', 'size:1.%',
                  'align:', 'size:10%\falign:start']
        for value in values:
            with self.subTest(value=value), self.assertRaises(BridgeError):
                export_subtitles(f'WEBVTT\n\n00:01.000 --> 00:02.000 {value}\ntext'.encode(), 'vtt')
        with self.assertRaises(BridgeError):
            export_subtitles(b'WEBVTT\n\n00:01.000 --> 00:02.000align:start\ntext', 'vtt')

    def test_keep_drop_and_new_cues_do_not_reassign_metadata(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        edited = labels.splitlines()[1] + '\n5\t6\tnew cue\n'
        output, _ = import_labels(edited, sidecar, output_format='vtt', missing_policy='keep', new_policy='add')
        cues, _ = parse_vtt(output)
        self.assertEqual([c.metadata.get('settings', '') for c in cues], [FIRST, SECOND, ''])
        self.assertEqual([c.source_id for c in cues], ['speaker-a', 'speaker-b', None])
        output, report = import_labels('', sidecar, output_format='vtt', missing_policy='drop')
        self.assertTrue(output.startswith('WEBVTT settings roundtrip\n\nNOTE'))
        self.assertEqual(report['output_count'], 0)
        _, report = import_labels(labels.splitlines()[1], sidecar, missing_policy='drop')
        self.assertEqual([x['id'] for x in report['losses'] if x['kind'] == 'webvtt_cue_settings'], ['c2'])

    def test_unknown_and_duplicate_bridge_ids_still_fail(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        for edited in [labels.replace('[cue:c1]', '[cue:unknown]'),
                       labels + '5\t6\t[cue:c1] Hello / world\n']:
            with self.subTest(edited=edited), self.assertRaises(BridgeError):
                import_labels(edited, sidecar, output_format='vtt', new_policy='add',
                              missing_policy='drop', text_policy='edited')

    def test_unsupported_source_and_edited_vtt_content_still_fail(self):
        for payload in ['STYLE\n::cue {color:red}', 'REGION\nid:r',
                        '00:01.000 --> 00:02.000 align:start\n<b>text</b>',
                        '00:01.000 --> 00:02.000 size:50%\na &amp; b']:
            with self.subTest(payload=payload), self.assertRaises(BridgeError):
                export_subtitles(('WEBVTT\n\n' + payload).encode(), 'vtt')
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        for text in ['<b>text</b>', 'a &amp; b', 'a --> b']:
            with self.subTest(text=text), self.assertRaises(BridgeError):
                import_labels(labels.replace('Hello / world', text), sidecar,
                              output_format='vtt', text_policy='edited')
            with self.subTest(new=text), self.assertRaises(BridgeError):
                import_labels(labels + '5\t6\t' + text, sidecar, output_format='vtt', new_policy='add')

    def test_resigned_sidecar_cannot_inject_unsupported_metadata(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        for key, value in [('settings', 'region:r'), ('settings', []), ('style', 'color:red')]:
            bad = copy.deepcopy(sidecar)
            bad['cues'][0]['metadata'] = {key: value}
            with self.subTest(key=key), self.assertRaises(BridgeError):
                import_labels(labels, resign(bad), output_format='vtt')
        for key, value in [('header', 'WEBVTT\nSTYLE'), ('notes', [{'before_cue': 0, 'text': 'STYLE\nred'}])]:
            bad = copy.deepcopy(sidecar)
            bad['source']['metadata'][key] = value
            with self.subTest(key=key), self.assertRaises(BridgeError):
                import_labels(labels, resign(bad), output_format='vtt')
        bad = copy.deepcopy(sidecar)
        bad['cues'][0]['source_id'] = 'id\nSTYLE'
        with self.assertRaises(BridgeError):
            import_labels(labels, resign(bad), output_format='vtt')

    def test_old_sidecar_without_settings_and_plain_labels_vtt(self):
        labels, sidecar = export_subtitles(b'WEBVTT\n\nid\n00:01.000 --> 00:02.000\ntext', 'vtt')
        self.assertEqual(sidecar['cues'][0]['metadata'], {})
        self.assertEqual(sidecar['schema'], 'audacity-subtitle-bridge/2')
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        resign(sidecar)
        output, _ = import_labels(labels, sidecar, output_format='vtt')
        self.assertIn('id\n00:00:01.000 --> 00:00:02.000\ntext', output)
        output, report = import_labels('1\t2\tplain', allow_lossy=True, output_format='vtt')
        self.assertEqual(parse_vtt(output)[0][0].text, 'plain')
        self.assertIn('LOSSY', report['warnings'][0])
        with self.assertRaises(BridgeError):
            import_labels(labels, sidecar, output_format='ass')

    def test_settings_require_new_schema_to_fail_safely_in_old_clients(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        self.assertEqual(sidecar['schema'], 'audacity-subtitle-bridge/2')
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        with self.assertRaisesRegex(BridgeError, 'Legacy sidecar schema'):
            import_labels(labels, resign(sidecar))

    def test_notes_between_cues_and_after_cues_retained_in_preamble_and_audited(self):
        source = SOURCE.replace(b'speaker-b\n', b'NOTE between cues\n\nspeaker-b\n') + b'\nNOTE after cues\n'
        labels, sidecar = export_subtitles(source, 'vtt')
        output, report = import_labels(labels, sidecar, output_format='vtt')
        self.assertLess(output.index('NOTE between'), output.index('NOTE after'))
        self.assertLess(output.index('NOTE after'), output.index('speaker-a'))
        self.assertIn('webvtt_notes_to_preamble', [a['kind'] for a in report['actions']])
        self.assertEqual(len(parse_vtt(output)[1]['notes']), 3)

    def test_srt_plain_text_can_output_vtt_but_markup_fails(self):
        source = b'7\n00:00:01,000 --> 00:00:02,000\nplain\n'
        labels, sidecar = export_subtitles(source, 'srt')
        output, _ = import_labels(labels, sidecar, output_format='vtt')
        self.assertEqual(parse_vtt(output)[0][0].source_id, '7')
        labels, sidecar = export_subtitles(source.replace(b'plain', b'<i>markup</i>'), 'srt')
        with self.assertRaisesRegex(BridgeError, 'markup'):
            import_labels(labels, sidecar, output_format='vtt')

    def test_cli_vtt_output_and_srt_loss_report_and_no_output_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, labels, sidecar = root/'in.vtt', root/'labels.txt', root/'sidecar.json'
            source.write_bytes(SOURCE)
            def run(*args):
                return subprocess.run([sys.executable, '-m', 'mediawrench.audacity', *map(str, args)],
                                      cwd=ROOT, capture_output=True, text=True, timeout=120)
            result = run('export', source, '--labels', labels, '--sidecar', sidecar)
            self.assertEqual(result.returncode, 0, result.stderr)
            labels.write_text(labels.read_text().replace('1.000\t2.500', '1.250\t2.750'))
            output = root/'out.vtt'
            args = ('import', labels, '--sidecar', sidecar, '--source', source, '--output', output, '--format', 'vtt')
            result = run(*args, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())
            result = run(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('00:00:01.250 --> 00:00:02.750 ' + FIRST, output.read_text())
            self.assertEqual(json.loads(result.stdout)['losses'], [])
            result = run('import', labels, '--sidecar', sidecar, '--output', root/'out.srt')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('settings', result.stderr)
            self.assertEqual(len([x for x in json.loads(result.stdout)['losses'] if x['kind'] == 'webvtt_cue_settings']), 2)
            labels.write_text(labels.read_text().replace('[cue:c1]', '[cue:unknown]'))
            result = run('import', labels, '--sidecar', sidecar, '--output', root/'bad.vtt', '--format', 'vtt')
            self.assertEqual(result.returncode, 2)
            self.assertIn('unknown cue ID', json.loads(result.stderr)['error'])
            self.assertFalse((root/'bad.vtt').exists())
            self.assertFalse((root/'bad.vtt.report.json').exists())


if __name__ == '__main__':
    unittest.main()
