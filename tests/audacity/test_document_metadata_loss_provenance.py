"""Document loss entries name all omitted original text, including preamble notes."""

from copy import deepcopy
import itertools
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_document_metadata_loss_policy import source_for, mapping_for, MAPPED
from .test_note_relocation_provenance import rich_source, POSITIONS, TEXTS, EXPECTED_NOTES, relocation_actions


HEADER = 'WEBVTT\t original 同じ e\u0301 😀  \t'
ALL_NOTES = [dict(note_index=i, before_cue=p, text=t)
             for i, (p, t) in enumerate(zip(POSITIONS, TEXTS), 1)]


def document_losses(report):
    return [loss for loss in report['losses'] if loss['kind'] == 'webvtt_document_metadata']


def expected_loss(header=True, notes=True):
    return dict(kind='webvtt_document_metadata',
                fields=(['header'] if header else []) + (['notes'] if notes else []),
                reason='SRT cannot represent WebVTT header/notes',
                **({'source_header': HEADER} if header else {}),
                **({'source_notes': ALL_NOTES} if notes else {}))


class DocumentMetadataLossProvenanceTests(unittest.TestCase):
    def test_round_16_installed_probe_empty_deletion(self):
        _, sidecar = export_subtitles(rich_source(), 'vtt')
        output, warned = import_labels('', sidecar, missing_policy='drop',
            settings_loss_policy='error', identifier_loss_policy='error', note_relocation_policy='error')
        self.assertEqual(output, '')
        self.assertEqual(warned['source']['metadata'], dict(header='WEBVTT description',
            notes=[dict(before_cue=p, text=t) for p, t in zip(POSITIONS, TEXTS)]))
        self.assertEqual(document_losses(warned), [{**expected_loss(), 'source_header': 'WEBVTT description'}])
        with self.assertRaisesRegex(BridgeError, 'document metadata') as caught:
            import_labels('', sidecar, missing_policy='drop', document_metadata_loss_policy='error',
                settings_loss_policy='error', identifier_loss_policy='error', note_relocation_policy='error')
        self.assertEqual(caught.exception.report, {**warned, 'status': 'error', 'document_metadata_loss_policy': 'error'})

    def test_applicable_original_values_only_after_reconciliation(self):
        for header, notes in ((True, False), (False, True), (True, True)):
            source = rich_source().replace(b'WEBVTT description', (HEADER if header else 'WEBVTT').encode())
            if not notes:
                source = source_for(header=HEADER, notes=False)
            labels, sidecar = export_subtitles(source, 'vtt')
            before = deepcopy(sidecar)
            cases = [(labels.replace('1.000', '1.250'), {}), ('', dict(missing_policy='drop'))]
            cases += [(MAPPED, dict(mapping=mapping_for(sidecar), missing_policy=missing, new_policy=new))
                      for missing, new in itertools.product(('keep', 'drop'), ('add', 'ignore'))]
            for edited, options in cases:
                for relocation in ('allow', 'error'):
                    with self.subTest(header=header, notes=notes, edited=edited, options=options, relocation=relocation):
                        opts = dict(**options, note_relocation_policy=relocation,
                                    settings_loss_policy='error', identifier_loss_policy='error')
                        output, warned = import_labels(edited, sidecar, **opts)
                        self.assertEqual(document_losses(warned), [expected_loss(header, notes)])
                        self.assertEqual(relocation_actions(warned), [])
                        with self.assertRaisesRegex(BridgeError, 'document metadata') as caught:
                            import_labels(edited, sidecar, document_metadata_loss_policy='error', **opts)
                        self.assertEqual(caught.exception.report, {**warned, 'status': 'error',
                            'document_metadata_loss_policy': 'error'})
                        self.assertEqual(sidecar, before)
                        if not edited:
                            self.assertEqual(output, '')
                            self.assertEqual(warned['output_cues'], [])
                        elif edited == MAPPED:
                            self.assertEqual(warned['output_count'], 4 + (options['missing_policy'] == 'keep') +
                                             (options['new_policy'] == 'add'))

    def test_earlier_cue_refusals_include_complete_document_loss(self):
        labels, sidecar = export_subtitles(rich_source(True).replace(b'WEBVTT description', HEADER.encode()), 'vtt')
        for edited, options in [(labels.replace('1.000', '1.250'), {}),
            (MAPPED, dict(mapping=mapping_for(sidecar), missing_policy='keep', new_policy='add'))]:
            _, warned = import_labels(edited, sidecar, **options)
            for settings, identifiers, document, relocation in itertools.product(
                    ('warn', 'error'), ('warn', 'error'), ('warn', 'error'), ('allow', 'error')):
                policies = dict(settings_loss_policy=settings, identifier_loss_policy=identifiers,
                                document_metadata_loss_policy=document, note_relocation_policy=relocation)
                with self.subTest(policies=policies, edited=edited):
                    if 'error' in (settings, identifiers, document):
                        with self.assertRaises(BridgeError) as caught:
                            import_labels(edited, sidecar, **options, **policies)
                        report = caught.exception.report
                        self.assertEqual(report, {**warned, **policies, 'status': 'error'})
                        self.assertIn('cue settings' if settings == 'error' else 'cue identifiers'
                                      if identifiers == 'error' else 'document metadata', str(caught.exception))
                    else:
                        _, report = import_labels(edited, sidecar, **options, **policies)
                        self.assertEqual(report, {**warned, **policies})
                    self.assertEqual(document_losses(report), [expected_loss()])

    def test_no_document_loss_boundaries_and_unchanged_relocation(self):
        for source_format, source in [('vtt', source_for(header='WEBVTT', notes=False)),
                ('srt', b'7\n00:00:01,000 --> 00:00:02,000\ntext\n\n'), ('vtt', rich_source())]:
            labels, sidecar = export_subtitles(source, source_format)
            for output_format in ('srt', 'vtt'):
                if source == rich_source() and output_format == 'srt':
                    continue
                _, report = import_labels(labels, sidecar, output_format=output_format,
                                          document_metadata_loss_policy='error')
                self.assertEqual(document_losses(report), [])
                if source == rich_source():
                    self.assertEqual(relocation_actions(report)[0]['relocated_notes'], EXPECTED_NOTES)
        for output_format in ('srt', 'vtt'):
            _, report = import_labels('1\t2\tplain\n', allow_lossy=True, output_format=output_format,
                                      document_metadata_loss_policy='error', note_relocation_policy='error')
            self.assertEqual(document_losses(report), [])

    def test_legacy_sidecar_preserves_all_note_indices(self):
        from .test_bridge import resign
        labels, sidecar = export_subtitles(rich_source().replace(b'WEBVTT description', HEADER.encode()), 'vtt')
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        resign(sidecar)
        _, report = import_labels(labels, sidecar)
        self.assertEqual(document_losses(report), [expected_loss()])


if __name__ == '__main__':
    unittest.main()
