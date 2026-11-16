"""Merged identifier losses name final outputs in either subtitle format."""

import hashlib
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels


SOURCE = (b'WEBVTT\n\nalpha\n00:01.000 --> 00:02.000\nA\n\n'
          b'beta\n00:02.000 --> 00:03.000\nB\n\n'
          b'00:04.000 --> 00:05.000\nplain\n\n'
          b'00:06.000 --> 00:07.000\nC\n\n'
          b'delta\n00:08.000 --> 00:09.000\nD\n')
# Output order reverses mapping-operation order and source order. Line 1 is ignored.
LABELS = '0\t0.1\tnew\n1\t3\tC / D\n4\t5\t[cue:c3] plain\n6\t9\tA / B\n'
EXPECTED_SRT = ('1\n00:00:01,000 --> 00:00:03,000\nC\nD\n\n'
                '2\n00:00:04,000 --> 00:00:05,000\nplain\n\n'
                '3\n00:00:06,000 --> 00:00:09,000\nA\nB\n\n')
EXPECTED_VTT = ('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nC\nD\n\n'
                '00:00:04.000 --> 00:00:05.000\nplain\n\n'
                '00:00:06.000 --> 00:00:09.000\nA\nB\n\n')
REASON = 'Merged cue has no single original identifier; all sources retained in actions'
MERGED_LOSSES = [
    {'kind': 'merged_cue_identifiers', 'mapping_operation': 1,
     'source_ids': ['c1', 'c2'], 'source_identifiers': ['alpha', 'beta'], 'reason': REASON},
    {'kind': 'merged_cue_identifiers', 'mapping_operation': 2,
     'source_ids': ['c4', 'c5'], 'source_identifiers': [None, 'delta'], 'reason': REASON}]
WARNINGS = [f'Mapping operation {i}: merged WebVTT identifiers retained in audit; output cue has no identifier'
            for i in (1, 2)]
SRT_WARNING = 'WebVTT identifiers/header/notes remain in sidecar/report; SRT does not carry WebVTT metadata'


def mapping_for(sidecar, labels=LABELS, last_target=4):
    return {'schema': 'audacity-subtitle-bridge-mapping/1',
            'sidecar_integrity_sha256': sidecar['integrity_sha256'],
            'labels_sha256': hashlib.sha256(labels.encode('utf-8')).hexdigest(),
            'operations': [
                {'kind': 'merge', 'sources': ['c1', 'c2'], 'separator': '\n', 'target': last_target},
                {'kind': 'merge', 'sources': ['c4', 'c5'], 'separator': '\n', 'target': 2}]}


def merged_losses(report):
    return [loss for loss in report['losses'] if loss['kind'] == 'merged_cue_identifiers']


class MergedIdentifierLossProvenanceTests(unittest.TestCase):
    def assert_merged_losses(self, report, indices=(3, 1), lines=(4, 2)):
        index_key = report['output_format'] + '_index'
        losses = merged_losses(report)
        self.assertEqual(losses, [dict(loss, **{index_key: index, "line": line})
                                 for loss, index, line in zip(MERGED_LOSSES, indices, lines)])
        for loss in losses:
            cue = report['output_cues'][loss[index_key] - 1]
            self.assertEqual({k: loss[k] for k in (index_key, 'line', 'mapping_operation', 'source_ids')},
                             {k: cue[k] for k in (index_key, 'line', 'mapping_operation', 'source_ids')})
            self.assertNotIn('vtt_index' if index_key == 'srt_index' else 'srt_index', loss)

    def test_reordered_merges_name_final_rows_under_both_formats_and_policies(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        for format, expected in [('srt', EXPECTED_SRT), ('vtt', EXPECTED_VTT)]:
            reports = []
            for policy in ('warn', 'error'):
                with self.subTest(format=format, policy=policy):
                    output, report = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar),
                        new_policy='ignore', output_format=format, settings_loss_policy=policy)
                    self.assertEqual(output, expected)
                    self.assertEqual(report['warnings'], WARNINGS + ([SRT_WARNING] if format == 'srt' else []))
                    self.assertEqual(report['losses'], merged_losses(report))
                    self.assert_merged_losses(report)
                    reports.append(report)
            if len(reports) == 2:
                self.assertEqual(reports[1], {**reports[0], 'settings_loss_policy': 'error'})

    def test_strict_settings_refusal_has_same_proposed_merge_indices(self):
        source = SOURCE.replace(b'00:03.000\nB', b'00:03.000 align:start\nB')
        source = source.replace(b'00:02.000\nA', b'00:02.000 align:start\nA')
        _, sidecar = export_subtitles(source, 'vtt')
        mapping = mapping_for(sidecar)
        output, warned = import_labels(LABELS, sidecar, mapping=mapping, new_policy='ignore')
        self.assertEqual(output, EXPECTED_SRT)
        with self.assertRaises(BridgeError) as caught:
            import_labels(LABELS, sidecar, mapping=mapping, new_policy='ignore', settings_loss_policy='error')
        strict = caught.exception.report
        self.assertEqual(strict['status'], 'error')
        self.assertEqual(strict['losses'], warned['losses'])
        self.assertEqual(strict['output_cues'], warned['output_cues'])
        self.assertEqual(warned['losses'][-1], {
            'kind': 'webvtt_cue_settings', 'id': None, 'source_id': None, 'srt_index': 3, 'line': 4,
            'mapping_operation': 1, 'source_ids': ['c1', 'c2'], 'settings': 'align:start',
            'source_identifiers': ['alpha', 'beta'],
            'reason': 'SRT cannot represent WebVTT cue settings'})
        self.assert_merged_losses(strict)
        self.assert_merged_losses(warned)

    def test_kept_cue_sorts_before_merges_and_new_row_changes_indices(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        labels = '0\t0.1\tnew\n6\t9\tC / D\n12\t15\tA / B\n'
        for format in ('srt', 'vtt'):
            for policy, offset in [('ignore', 0), ('add', 1)]:
                with self.subTest(format=format, new_policy=policy):
                    _, report = import_labels(labels, sidecar, mapping=mapping_for(sidecar, labels, 3),
                        missing_policy='keep', new_policy=policy, output_format=format,
                        settings_loss_policy='error')
                    self.assertIsNone(report['output_cues'][offset]['line'])
                    self.assertEqual(report['output_cues'][offset]['id'], 'c3')
                    self.assert_merged_losses(report, (3 + offset, 2 + offset), (3, 2))

    def test_dropped_cues_and_identifier_free_merges_do_not_invent_losses(self):
        for identifiers in (True, False):
            source = SOURCE if identifiers else SOURCE.replace(b'alpha\n', b'').replace(b'beta\n', b'').replace(b'delta\n', b'')
            source += b'\ndropped\n00:10.000 --> 00:11.000 align:end\nDrop\n'
            _, sidecar = export_subtitles(source, 'vtt')
            for format, expected in [('srt', EXPECTED_SRT), ('vtt', EXPECTED_VTT)]:
                with self.subTest(identifiers=identifiers, format=format):
                    output, report = import_labels(LABELS, sidecar, mapping=mapping_for(sidecar),
                        missing_policy='drop', new_policy='ignore', output_format=format,
                        settings_loss_policy='error')
                    self.assertEqual(output, expected)
                    self.assertIn({'kind': 'missing_cue', 'id': 'c6', 'policy': 'drop'}, report['actions'])
                    self.assertEqual(report['losses'], merged_losses(report))
                    if identifiers:
                        self.assert_merged_losses(report)
                    else:
                        self.assertEqual(report['losses'], [])
                        self.assertEqual(report['warnings'], [SRT_WARNING] if format == 'srt' else [])


if __name__ == '__main__':
    unittest.main()
