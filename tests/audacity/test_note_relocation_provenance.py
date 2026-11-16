"""Relocation names original NOTE records, independent of output cue placement."""

from copy import deepcopy
import itertools
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_document_metadata_loss_policy import source_for, mapping_for, MAPPED
from .test_note_relocation_policy import STRICT_LOSS


NOTE = 'NOTE duplicate\n  exact 同じ  '
RICH_NOTE = 'NOTE\t同じ e\u0301\n \t exact 😀  \t'
POSITIONS = (0, 0, 1, 1, 3, 5, 5)
TEXTS = (RICH_NOTE, NOTE, RICH_NOTE, RICH_NOTE, 'NOTE\n\t  ', NOTE, RICH_NOTE)
EXPECTED_NOTES = [dict(note_index=i, before_cue=position, text=text)
                  for i, (position, text) in enumerate(zip(POSITIONS, TEXTS), 1) if position]


def rich_source(cue_metadata=False):
    # Insert multiple identical notes at the same and different positions.
    blocks = source_for(notes=False, identifiers=cue_metadata, settings=cue_metadata).decode().strip().split('\n\n')
    result = [blocks[0]]
    for position in range(6):
        result.extend(text for p, text in zip(POSITIONS, TEXTS) if p == position)
        if position < 5:
            result.append(blocks[position + 1])
    return ('\n\n'.join(result) + '\n\n').encode()


def relocation_actions(report):
    return [a for a in report['actions'] if a['kind'] == 'webvtt_notes_to_preamble']


class NoteRelocationProvenanceTests(unittest.TestCase):
    def test_round_15_installed_probe_identical_notes(self):
        source = ('WEBVTT description\n\n' + NOTE + '\n\n00:01.000 --> 00:02.000\nA\n\n' +
                  NOTE + '\n\n00:03.000 --> 00:04.000\nB\n\n' + NOTE + '\n\n').encode()
        labels, sidecar = export_subtitles(source, 'vtt')
        edited = labels.replace('1.000\t2.000', '1.250\t2.250').replace('3.000\t4.000', '3.250\t4.250')
        output, allowed = import_labels(edited, sidecar, output_format='vtt', **STRICT_LOSS)
        self.assertEqual(allowed['source']['metadata']['notes'],
                         [dict(before_cue=i, text=NOTE) for i in (0, 1, 2)])
        self.assertEqual(output, 'WEBVTT description\n\n' + (NOTE + '\n\n') * 3 +
                         '00:00:01.250 --> 00:00:02.250\nA\n\n00:00:03.250 --> 00:00:04.250\nB\n\n')
        self.assertEqual(allowed['losses'], [])
        self.assertEqual(allowed['warnings'], [])
        self.assertEqual(relocation_actions(allowed), [dict(
            kind='webvtt_notes_to_preamble', reason='NOTE text retained in source order before cues',
            relocated_notes=[dict(note_index=2, before_cue=1, text=NOTE),
                             dict(note_index=3, before_cue=2, text=NOTE)])])

    def test_exact_original_indices_text_order_after_reconciliation(self):
        for cue_metadata in (False, True):
            source = rich_source(cue_metadata)
            labels, sidecar = export_subtitles(source, 'vtt')
            before = deepcopy(sidecar)
            cases = [(labels, {}), (labels.replace('1.000', '1.250'), {}),
                     ('', dict(missing_policy='drop'))]
            cases += [(MAPPED, dict(mapping=mapping_for(sidecar), missing_policy=missing, new_policy=new))
                      for missing, new in itertools.product(('keep', 'drop'), ('add', 'ignore'))]
            for edited, options in cases:
                with self.subTest(metadata=cue_metadata, labels=edited, options=options):
                    options.update(output_format='vtt', settings_loss_policy='error', document_metadata_loss_policy='error')
                    output, allowed = import_labels(edited, sidecar, **options)
                    self.assertEqual(relocation_actions(allowed)[0]['relocated_notes'], EXPECTED_NOTES)
                    self.assertEqual(len(relocation_actions(allowed)), 1)
                    with self.assertRaisesRegex(BridgeError, 'NOTE relocation') as caught:
                        import_labels(edited, sidecar, note_relocation_policy='error', **options)
                    self.assertEqual(caught.exception.report,
                                     {**allowed, 'status': 'error', 'note_relocation_policy': 'error'})
                    self.assertEqual(sidecar, before)
                    preamble = 'WEBVTT description\n\n' + ''.join(text + '\n\n' for text in TEXTS)
                    self.assertTrue(output.startswith(preamble))
                    if not edited:
                        self.assertEqual(output, preamble)
                        self.assertEqual(allowed['output_cues'], [])
                    elif edited == MAPPED:
                        self.assertEqual(allowed['output_count'], 4 + (options['missing_policy'] == 'keep') + (options['new_policy'] == 'add'))
                        self.assertEqual(allowed['output_cues'][-1]['source_ids'], ['c4', 'c5'])

    def test_independent_identifier_error_contains_same_original_notes(self):
        _, sidecar = export_subtitles(rich_source(True), 'vtt')
        options = dict(mapping=mapping_for(sidecar), missing_policy='keep', new_policy='add', output_format='vtt')
        _, allowed = import_labels(MAPPED, sidecar, **options)
        for policy in ('allow', 'error'):
            with self.subTest(policy=policy), self.assertRaisesRegex(BridgeError, 'cue identifiers') as caught:
                import_labels(MAPPED, sidecar, identifier_loss_policy='error', note_relocation_policy=policy, **options)
            self.assertEqual(caught.exception.report, {**allowed, 'status': 'error',
                'identifier_loss_policy': 'error', 'note_relocation_policy': policy})
            self.assertEqual(relocation_actions(caught.exception.report)[0]['relocated_notes'], EXPECTED_NOTES)

    def test_legacy_sidecar_keeps_original_indices(self):
        from .test_bridge import resign
        labels, sidecar = export_subtitles(rich_source(), 'vtt')
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        resign(sidecar)
        _, report = import_labels(labels, sidecar, output_format='vtt', **STRICT_LOSS)
        self.assertEqual(relocation_actions(report)[0]['relocated_notes'], EXPECTED_NOTES)

    def test_no_relocation_action_in_srt_even_on_independent_refusal(self):
        labels, sidecar = export_subtitles(rich_source(True), 'vtt')
        for policy in ('allow', 'error'):
            _, allowed = import_labels(labels, sidecar, note_relocation_policy=policy)
            self.assertEqual(relocation_actions(allowed), [])
            with self.assertRaisesRegex(BridgeError, 'document metadata') as caught:
                import_labels(labels, sidecar, note_relocation_policy=policy, document_metadata_loss_policy='error')
            self.assertEqual(relocation_actions(caught.exception.report), [])


if __name__ == '__main__':
    unittest.main()
