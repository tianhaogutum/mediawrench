"""Contract: reject only retained WebVTT settings lost to SRT, before file writes.

Default/explicit warn return the same SRT and loss details. Error raises BridgeError
with the complete audit; CLI exits 2 with JSON stderr and creates no files. Kept
and mapped cues count; dropped cues do not. VTT restoration, settings-free SRT,
and other metadata losses retain their existing behavior under either policy.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.formats import parse_srt, parse_vtt


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (b'WEBVTT\n\nspeaker\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
          b'00:04.000 --> 00:05.000\nplain\n')


class SettingsLossPolicyTests(unittest.TestCase):
    def setUp(self):
        self.labels, self.sidecar = export_subtitles(SOURCE, 'vtt')

    def test_warning_default_and_explicit_warn_preserve_existing_output(self):
        default, default_report = import_labels(self.labels, self.sidecar)
        output, report = import_labels(self.labels, self.sidecar, settings_loss_policy='warn')
        self.assertEqual(output, default)
        self.assertEqual(report, default_report)
        self.assertEqual(report['settings_loss_policy'], 'warn')
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['Hello\nworld', 'plain'])
        self.assertTrue(any('settings' in warning for warning in report['warnings']))
        self.assertEqual([x for x in report['losses'] if x['kind'] == 'webvtt_cue_settings'],
                         [{'kind': 'webvtt_cue_settings', 'id': 'c1', 'source_id': 'speaker',
                           'settings': 'align:start', 'srt_index': 1, 'line': 1,
                           'reason': 'SRT cannot represent WebVTT cue settings'}])

    def test_error_reports_every_retained_loss_and_suggests_vtt(self):
        labels, sidecar = export_subtitles((ROOT / 'examples/audacity/settings.vtt').read_bytes(), 'vtt')
        _, warned = import_labels(labels, sidecar)
        with self.assertRaisesRegex(BridgeError, 'WebVTT cue settings') as caught:
            import_labels(labels, sidecar, settings_loss_policy='error')
        self.assertIn('--format vtt', str(caught.exception))
        report = caught.exception.report
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['settings_loss_policy'], 'error')
        self.assertEqual(report['output_format'], 'srt')
        self.assertEqual(report['losses'], warned['losses'])
        self.assertEqual(report['output_cues'], warned['output_cues'])
        self.assertEqual(report['output_count'], 2)

    def test_vtt_strict_restores_settings_after_timing_edit(self):
        edited = self.labels.replace('1.000\t3.000', '1.250\t3.250')
        output, report = import_labels(edited, self.sidecar, settings_loss_policy='error',
                                       output_format='vtt')
        cue = parse_vtt(output)[0][0]
        self.assertEqual((cue.source_id, cue.start_ms, cue.end_ms, cue.text, cue.metadata),
                         ('speaker', 1250, 3250, 'Hello\nworld', {'settings': 'align:start'}))
        self.assertEqual(report['losses'], [])

    def test_dropped_settings_do_not_block_plain_new_or_empty_output(self):
        for edited, expected in [(self.labels.splitlines()[1] + '\n6\t7\tnew\n', ['plain', 'new']),
                                 ('', [])]:
            with self.subTest(edited=edited):
                output, report = import_labels(edited, self.sidecar, missing_policy='drop',
                                               new_policy='add', settings_loss_policy='error')
                self.assertEqual(report['output_count'], len(expected))
                self.assertFalse(any(x['kind'] == 'webvtt_cue_settings' for x in report['losses']))
                self.assertEqual([c.text for c in parse_srt(output)[0]] if output else [], expected)

    def test_kept_missing_cue_still_blocks_and_allow_lossy_cannot_bypass(self):
        with self.assertRaises(BridgeError) as caught:
            import_labels(self.labels.splitlines()[1], self.sidecar, missing_policy='keep',
                          allow_lossy=True, settings_loss_policy='error')
        report = caught.exception.report
        self.assertEqual(report['status'], 'error')
        self.assertIsNone(report['output_cues'][0]['line'])
        self.assertEqual([x['id'] for x in report['losses'] if x['kind'] == 'webvtt_cue_settings'], ['c1'])

    def test_strict_is_settings_only_and_supports_legacy_and_plain_sources(self):
        source = b'WEBVTT description\n\nNOTE retained in sidecar\n\nid\n00:01.000 --> 00:02.000\ntext\n'
        labels, sidecar = export_subtitles(source, 'vtt')
        # Original /1 sidecars have no settings and remain readable under strict mode.
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        from .test_bridge import resign
        resign(sidecar)
        output, report = import_labels(labels, sidecar, settings_loss_policy='error')
        self.assertEqual(parse_srt(output)[0][0].text, 'text')
        self.assertEqual({x['kind'] for x in report['losses']},
                         {'webvtt_document_metadata', 'webvtt_cue_identifier'})
        labels, sidecar = export_subtitles(b'7\n00:00:01,000 --> 00:00:02,000\ntext\n', 'srt')
        output, report = import_labels(labels, sidecar, settings_loss_policy='error')
        self.assertEqual(parse_srt(output)[0][0].text, 'text')
        self.assertEqual(report['losses'], [])
        output, report = import_labels('1\t2\tplain', allow_lossy=True, settings_loss_policy='error')
        self.assertEqual(parse_srt(output)[0][0].text, 'plain')
        self.assertIn('LOSSY', report['warnings'][0])

    def test_split_and_merge_settings_are_checked_on_composed_outputs(self):
        source = (b'WEBVTT\n\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
                  b'00:04.000 --> 00:05.000 align:start\nAgain\n\n'
                  b'00:06.000 --> 00:07.000 align:start\nLater\n')
        _, sidecar = export_subtitles(source, 'vtt')
        labels = '1\t2\tHello\n2\t3\tworld\n4\t7\tAgain / Later\n'
        mapping = {'schema': 'audacity-subtitle-bridge-mapping/1',
                   'sidecar_integrity_sha256': sidecar['integrity_sha256'],
                   'labels_sha256': hashlib.sha256(labels.encode()).hexdigest(),
                   'operations': [
                       {'kind': 'split', 'source': 'c1', 'separator': '\n',
                        'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
                       {'kind': 'merge', 'sources': ['c2', 'c3'], 'separator': '\n', 'target': 3}]}
        with self.assertRaises(BridgeError) as caught:
            import_labels(labels, sidecar, mapping=mapping, settings_loss_policy='error')
        report = caught.exception.report
        losses = [x for x in report['losses'] if x['kind'] == 'webvtt_cue_settings']
        self.assertEqual(len(losses), 3)
        self.assertEqual([x['source_ids'] for x in report['output_cues']], [['c1'], ['c1'], ['c2', 'c3']])
        self.assertEqual([x['settings'] for x in losses], ['align:start'] * 3)
        output, _ = import_labels(labels, sidecar, mapping=mapping, settings_loss_policy='error',
                                  output_format='vtt')
        self.assertEqual([c.metadata['settings'] for c in parse_vtt(output)[0]], ['align:start'] * 3)

    def test_invalid_policy_fails_even_for_lossless_inputs(self):
        for policy in ['ignore', 'ERROR', '', None, True, []]:
            with self.subTest(policy=policy), self.assertRaisesRegex(BridgeError, 'settings.loss policy'):
                import_labels('1\t2\tplain', allow_lossy=True, settings_loss_policy=policy)

    def test_strict_does_not_bypass_identity_text_or_format_validation(self):
        for edited, message in [(self.labels.replace('[cue:c1]', '[cue:unknown]'), 'unknown cue ID'),
                                (self.labels.replace('Hello / world', 'changed'), 'Unresolved edits')]:
            with self.subTest(edited=edited), self.assertRaisesRegex(BridgeError, message):
                import_labels(edited, self.sidecar, settings_loss_policy='error')
        with self.assertRaisesRegex(BridgeError, 'Output format'):
            import_labels(self.labels, self.sidecar, output_format='ass', settings_loss_policy='error')

    def test_cli_strict_rejects_without_outputs_including_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels, sidecar = root / 'labels.txt', root / 'sidecar.json'
            labels.write_text(self.labels, encoding='utf-8')
            sidecar.write_text(json.dumps(self.sidecar), encoding='utf-8')
            def run(output, *extra):
                return subprocess.run([sys.executable, '-m', 'mediawrench.audacity', 'import',
                                       str(labels), '--sidecar', str(sidecar), '--output', str(output),
                                       *extra], cwd=ROOT, capture_output=True, text=True, timeout=120)
            # Filename suffix does not change the default SRT format.
            for dry, custom in [(False, False), (True, True)]:
                with self.subTest(dry_run=dry):
                    output = root / 'blocked.vtt'
                    report_path = root / 'custom.json' if custom else Path(str(output) + '.report.json')
                    extra = ['--report', str(report_path)] if custom else []
                    if dry:
                        extra.append('--dry-run')
                    result = run(output, '--settings-loss-policy', 'error', *extra)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    error = json.loads(result.stderr)
                    self.assertIn('WebVTT cue settings', error['error'])
                    self.assertEqual(error['report']['status'], 'error')
                    self.assertEqual(error['report']['losses'][-1]['settings'], 'align:start')
                    self.assertFalse(output.exists())
                    self.assertFalse(report_path.exists())
                    self.assertFalse(list(root.glob('.asb-*')))
            result = run(root / 'restored.vtt', '--settings-loss-policy', 'error', '--format', 'vtt')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('align:start', (root / 'restored.vtt').read_text())
            self.assertEqual(json.loads(result.stdout)['settings_loss_policy'], 'error')
            result = run(root / 'warn.srt', '--settings-loss-policy', 'warn')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('settings', result.stderr)
            result = run(root / 'invalid.srt', '--settings-loss-policy', 'ignore')
            self.assertEqual(result.returncode, 2)
            self.assertIn('invalid choice', result.stderr)
            self.assertFalse((root / 'invalid.srt').exists())


if __name__ == '__main__':
    unittest.main()
