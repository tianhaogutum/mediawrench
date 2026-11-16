"""Each settings loss names the final SRT row, including composed outputs."""

import hashlib
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.formats import parse_vtt


SOURCE = (b'WEBVTT\n\nspeaker\n00:01.000 --> 00:03.000 align:start\nHello\nworld\n\n'
          b'00:03.000 --> 00:04.000\nplain\n\n'
          b'again\n00:04.000 --> 00:05.000 align:end\nAgain\n\n'
          b'later\n00:06.000 --> 00:07.000 align:end\nLater\n')
LABELS = '1\t2\tHello\n2\t3\tworld\n3\t4\t[cue:c2] plain\n4\t7\tAgain / Later\n'
EXPECTED_SRT = ('1\n00:00:01,000 --> 00:00:02,000\nHello\n\n'
                '2\n00:00:02,000 --> 00:00:03,000\nworld\n\n'
                '3\n00:00:03,000 --> 00:00:04,000\nplain\n\n'
                '4\n00:00:04,000 --> 00:00:07,000\nAgain\nLater\n\n')
PROVENANCE = [{'srt_index': 1, 'line': 1, 'mapping_operation': 1, 'source_ids': ['c1']},
              {'srt_index': 2, 'line': 2, 'mapping_operation': 1, 'source_ids': ['c1']},
              {'srt_index': 4, 'line': 4, 'mapping_operation': 2, 'source_ids': ['c3', 'c4']}]


def mapping_for(sidecar):
    return {'schema': 'audacity-subtitle-bridge-mapping/1',
            'sidecar_integrity_sha256': sidecar['integrity_sha256'],
            'labels_sha256': hashlib.sha256(LABELS.encode('utf-8')).hexdigest(),
            'operations': [
                {'kind': 'split', 'source': 'c1', 'separator': '\n',
                 'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]},
                {'kind': 'merge', 'sources': ['c3', 'c4'], 'separator': '\n', 'target': 4}]}


def settings_losses(report):
    return [loss for loss in report['losses'] if loss['kind'] == 'webvtt_cue_settings']


class SettingsLossProvenanceTests(unittest.TestCase):
    def assert_mapped_losses(self, report):
        losses = settings_losses(report)
        self.assertEqual([{key: loss.get(key) for key in PROVENANCE[0]} for loss in losses], PROVENANCE)
        self.assertEqual([(loss['id'], loss['source_id'], loss['settings']) for loss in losses],
                         [(None, 'speaker', 'align:start'), (None, 'speaker', 'align:start'),
                          (None, None, 'align:end')])
        for loss in losses:
            cue = report['output_cues'][loss['srt_index'] - 1]
            for key in ('srt_index', 'line', 'mapping_operation', 'source_ids', 'id', 'source_id'):
                self.assertEqual(loss[key], cue[key])
            self.assertEqual(loss['settings'], cue['metadata']['settings'])
        self.assertNotIn('settings', report['output_cues'][2]['metadata'])

    def test_mixed_mapping_warn_and_error_name_the_same_affected_rows(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        mapping = mapping_for(sidecar)
        output, default = import_labels(LABELS, sidecar, mapping=mapping)
        self.assertEqual(output, EXPECTED_SRT)
        explicit, warned = import_labels(LABELS, sidecar, mapping=mapping, settings_loss_policy='warn')
        self.assertEqual((explicit, warned), (output, default))
        self.assertEqual(default['warnings'], [
            'Mapping operation 2: merged WebVTT identifiers retained in audit; output cue has no identifier',
            'WebVTT identifiers/header/notes remain in sidecar/report; SRT does not carry WebVTT metadata',
            'LOSS: WebVTT cue settings cannot be represented in SRT; see losses for each cue or choose --format vtt'])
        with self.assertRaises(BridgeError) as caught:
            import_labels(LABELS, sidecar, mapping=mapping, settings_loss_policy='error')
        strict = caught.exception.report
        self.assertEqual(strict['status'], 'error')
        self.assertEqual(strict['losses'], default['losses'])
        self.assertEqual(strict['output_cues'], default['output_cues'])
        self.assert_mapped_losses(default)
        self.assert_mapped_losses(strict)

    def test_dropped_settings_cue_creates_no_loss_in_mixed_mapping(self):
        _, sidecar = export_subtitles(SOURCE + b'\ndropped\n00:08.000 --> 00:09.000 align:center\nDrop\n', 'vtt')
        mapping = mapping_for(sidecar)
        output, report = import_labels(LABELS, sidecar, mapping=mapping, missing_policy='drop')
        self.assertEqual(output, EXPECTED_SRT)
        with self.assertRaises(BridgeError) as caught:
            import_labels(LABELS, sidecar, mapping=mapping, missing_policy='drop', settings_loss_policy='error')
        self.assertEqual(caught.exception.report['losses'], report['losses'])
        self.assertTrue(any(a.get('id') == 'c5' and a.get('policy') == 'drop' for a in report['actions']))
        self.assert_mapped_losses(report)
        self.assertFalse(any(loss.get('source_id') == 'dropped' for loss in report['losses']))

    def test_unmapped_kept_cues_use_final_indices_after_sorting(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        # The plain row moves before c1; ignored line 1 is not an output cue.
        labels = '0\t0.25\tignore\n0.5\t1\t[cue:c2] plain\n'
        _, report = import_labels(labels, sidecar, missing_policy='keep', new_policy='ignore')
        losses = settings_losses(report)
        self.assertEqual([(loss.get('srt_index'), loss['id']) for loss in losses],
                         [(2, 'c1'), (3, 'c3'), (4, 'c4')])
        for loss in losses:
            self.assertNotIn('mapping_operation', loss)
            self.assertNotIn('source_ids', loss)
            cue = report['output_cues'][loss['srt_index'] - 1]
            self.assertEqual((loss['id'], loss['source_id']), (cue['id'], cue['source_id']))
            self.assertIsNone(loss['line'])
            self.assertIsNone(cue['line'])
        with self.assertRaises(BridgeError) as caught:
            import_labels(labels, sidecar, missing_policy='keep', new_policy='ignore', settings_loss_policy='error')
        self.assertEqual(caught.exception.report['losses'], report['losses'])

    def test_mapped_vtt_restoration_does_not_add_settings_losses(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        mapping = mapping_for(sidecar)
        output, report = import_labels(LABELS, sidecar, mapping=mapping, output_format='vtt')
        strict_output, strict = import_labels(LABELS, sidecar, mapping=mapping, output_format='vtt',
                                              settings_loss_policy='error')
        self.assertEqual(output, strict_output)
        self.assertEqual(report['losses'], strict['losses'])
        self.assertEqual(settings_losses(report), [])
        self.assertEqual([(c.source_id, c.text, c.metadata) for c in parse_vtt(output)[0]],
                         [('speaker', 'Hello', {'settings': 'align:start'}),
                          ('speaker', 'world', {'settings': 'align:start'}),
                          (None, 'plain', {}), (None, 'Again\nLater', {'settings': 'align:end'})])
        self.assertTrue(all('srt_index' not in cue for cue in report['output_cues']))


if __name__ == '__main__':
    unittest.main()
