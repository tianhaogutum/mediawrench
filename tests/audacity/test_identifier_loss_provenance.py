"""Individual identifier losses identify final rows without tightening policies."""

import hashlib
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels


SOURCE = (b'WEBVTT\n\nspeaker\n00:01.000 --> 00:03.000\nHello\nworld\n\n'
          b'00:01.500 --> 00:02.000\nplain\n\n'
          b'guest\n00:04.000 --> 00:05.000\nTail\n\n'
          b'again\n00:06.000 --> 00:07.000\nAgain\n\n'
          b'later\n00:08.000 --> 00:09.000\nLater\n')
LABELS = ('1\t1.5\tHello\n1.5\t2\t[cue:c2] plain\n2\t3\tworld\n'
          '4\t5\t[cue:c3] Tail\n6\t9\tAgain / Later\n')
EXPECTED_SRT = ('1\n00:00:01,000 --> 00:00:01,500\nHello\n\n'
                '2\n00:00:01,500 --> 00:00:02,000\nplain\n\n'
                '3\n00:00:02,000 --> 00:00:03,000\nworld\n\n'
                '4\n00:00:04,000 --> 00:00:05,000\nTail\n\n'
                '5\n00:00:06,000 --> 00:00:09,000\nAgain\nLater\n\n')
EXPECTED_VTT = ('WEBVTT\n\nspeaker\n00:00:01.000 --> 00:00:01.500\nHello\n\n'
                '00:00:01.500 --> 00:00:02.000\nplain\n\n'
                'speaker\n00:00:02.000 --> 00:00:03.000\nworld\n\n'
                'guest\n00:00:04.000 --> 00:00:05.000\nTail\n\n'
                '00:00:06.000 --> 00:00:09.000\nAgain\nLater\n\n')
REASON = 'SRT indices replace WebVTT identifiers'
IDENTIFIER_LOSSES = [
    {'kind': 'webvtt_cue_identifier', 'id': None, 'source_id': 'speaker', 'reason': REASON,
     'srt_index': 1, 'line': 1, 'mapping_operation': 1, 'source_ids': ['c1']},
    {'kind': 'webvtt_cue_identifier', 'id': None, 'source_id': 'speaker', 'reason': REASON,
     'srt_index': 3, 'line': 3, 'mapping_operation': 1, 'source_ids': ['c1']},
    {'kind': 'webvtt_cue_identifier', 'id': 'c3', 'source_id': 'guest', 'reason': REASON,
     'srt_index': 4, 'line': 4}]
MERGED_LOSS = {'kind': 'merged_cue_identifiers', 'mapping_operation': 2,
               'line': 5, 'source_ids': ['c4', 'c5'], 'source_identifiers': ['again', 'later'],
               'reason': 'Merged cue has no single original identifier; all sources retained in actions'}
WARNINGS = [
    'Mapping operation 2: merged WebVTT identifiers retained in audit; output cue has no identifier',
    'WebVTT identifiers/header/notes remain in sidecar/report; SRT does not carry WebVTT metadata']


def mapping_for(sidecar):
    return {'schema': 'audacity-subtitle-bridge-mapping/1',
            'sidecar_integrity_sha256': sidecar['integrity_sha256'],
            'labels_sha256': hashlib.sha256(LABELS.encode('utf-8')).hexdigest(),
            'operations': [
                {'kind': 'split', 'source': 'c1', 'separator': '\n',
                 'targets': [{'line': 1, 'text': 'Hello'}, {'line': 3, 'text': 'world'}]},
                {'kind': 'merge', 'sources': ['c4', 'c5'], 'separator': '\n', 'target': 5}]}


def identifier_losses(report):
    return [loss for loss in report['losses'] if loss['kind'] == 'webvtt_cue_identifier']


class IdentifierLossProvenanceTests(unittest.TestCase):
    def assert_identifier_losses(self, report):
        self.assertEqual(identifier_losses(report), IDENTIFIER_LOSSES)
        for loss in identifier_losses(report):
            cue = report['output_cues'][loss['srt_index'] - 1]
            for key in ('srt_index', 'line', 'id', 'source_id', 'mapping_operation', 'source_ids'):
                self.assertEqual(loss.get(key), cue.get(key))

    def test_mixed_split_unmapped_and_merge_remain_warning_only(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        # First prove direct, uncomposed losses also carry output indices.
        _, direct = import_labels(labels, sidecar)
        self.assertEqual([(x.get('srt_index'), x['id']) for x in identifier_losses(direct)],
                         [(1, 'c1'), (3, 'c3'), (4, 'c4'), (5, 'c5')])
        output, default = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar))
        self.assertEqual(output, EXPECTED_SRT)
        self.assertEqual(default['warnings'], WARNINGS)
        self.assertEqual(default['losses'], [{**MERGED_LOSS, 'srt_index': 5}, *IDENTIFIER_LOSSES])
        for policy in ('warn', 'error'):
            with self.subTest(policy=policy):
                result, report = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar),
                                               settings_loss_policy=policy)
                self.assertEqual(result, output)
                self.assertEqual(report, {**default, 'settings_loss_policy': policy})
                self.assert_identifier_losses(report)

    def test_dropped_identifier_and_settings_do_not_generate_losses(self):
        _, sidecar = export_subtitles(
            SOURCE + b'\ndropped\n00:10.000 --> 00:11.000 align:end\nDrop\n', 'vtt')
        for policy in ('warn', 'error'):
            with self.subTest(policy=policy):
                output, report = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar),
                                               missing_policy='drop', settings_loss_policy=policy)
                self.assertEqual(output, EXPECTED_SRT)
                self.assertEqual(report['losses'], [{**MERGED_LOSS, 'srt_index': 5}, *IDENTIFIER_LOSSES])
                self.assertIn({'kind': 'missing_cue', 'id': 'c6', 'policy': 'drop'}, report['actions'])
                self.assert_identifier_losses(report)

    def test_reordered_and_kept_unmapped_cues_use_final_sorted_indices(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        labels = '0\t0.1\tnew\n0.5\t0.75\t[cue:c2] plain\n0.8\t0.9\t[cue:c3] Tail\n'
        for new_policy, offset in [('ignore', 0), ('add', 1)]:
            for policy in ('warn', 'error'):
                with self.subTest(new_policy=new_policy, policy=policy):
                    _, report = import_labels(labels, sidecar, missing_policy='keep',
                                               new_policy=new_policy, settings_loss_policy=policy)
                    losses = identifier_losses(report)
                    self.assertEqual([(loss.get('srt_index'), loss['id']) for loss in losses],
                                     [(2 + offset, 'c3'), (3 + offset, 'c1'),
                                      (4 + offset, 'c4'), (5 + offset, 'c5')])
                    for loss in losses:
                        self.assertNotIn('mapping_operation', loss)
                        self.assertNotIn('source_ids', loss)
                        cue = report['output_cues'][loss['srt_index'] - 1]
                        self.assertEqual((loss['id'], loss['source_id']), (cue['id'], cue['source_id']))
                        self.assertEqual(loss['line'], 3 if loss['id'] == 'c3' else None)
                        self.assertEqual(loss['line'], cue['line'])

    def test_strict_settings_error_keeps_the_same_identifier_provenance(self):
        source = SOURCE.replace(b'00:03.000\nHello', b'00:03.000 align:start\nHello')
        _, sidecar = export_subtitles(source, 'vtt')
        _, warned = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar))
        with self.assertRaises(BridgeError) as caught:
            import_labels(LABELS, sidecar, mapping=mapping_for(sidecar), settings_loss_policy='error')
        strict = caught.exception.report
        self.assertEqual(strict['status'], 'error')
        self.assertEqual(strict['losses'], warned['losses'])
        self.assertEqual(strict['output_cues'], warned['output_cues'])
        self.assert_identifier_losses(warned)
        self.assert_identifier_losses(strict)

    def test_vtt_restoration_and_merged_identifier_loss_are_unchanged(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        for policy in ('warn', 'error'):
            with self.subTest(policy=policy):
                output, report = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar),
                                               output_format='vtt', settings_loss_policy=policy)
                self.assertEqual(output, EXPECTED_VTT)
                self.assertEqual(report['losses'], [{**MERGED_LOSS, 'vtt_index': 5}])
                self.assertEqual(report['warnings'], WARNINGS[:1])
                self.assertTrue(all('srt_index' not in cue for cue in report['output_cues']))


if __name__ == '__main__':
    unittest.main()
