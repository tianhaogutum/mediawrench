"""Document loss refusal follows reconciliation and is independent of cue policies."""

import itertools
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.core import sha256
from mediawrench.audacity.formats import parse_vtt


def source_for(header='WEBVTT description', notes=True, identifiers=False, settings=False):
    blocks = [header]
    if notes:
        blocks.append('NOTE preamble\n  exact 同じ  ')
    for i, (start, end, text) in enumerate([(1, 3, 'Hello\nworld'), (4, 5, 'plain'),
                                           (6, 7, 'Tail'), (8, 9, 'Again'), (10, 11, 'Later')], 1):
        blocks.append((f'speaker-{i}\n' if identifiers else '') +
                      f'00:{start:02d}.000 --> 00:{end:02d}.000' +
                      (' align:start' if settings else '') + '\n' + text)
        if notes and i == 1:
            blocks.append('NOTE\tinter-cue\nsecond line')
    if notes:
        blocks.append('NOTE trailing')
    return ('\n\n'.join(blocks) + '\n\n').encode()


MAPPED = ('0\t0.1\tnew\n1.25\t2\tHello\n2\t3.25\tworld\n'
          '4.25\t5.25\t[cue:c2] plain\n8\t11\tAgain / Later\n')


def mapping_for(sidecar):
    return {'schema': 'audacity-subtitle-bridge-mapping/1',
            'sidecar_integrity_sha256': sidecar['integrity_sha256'],
            'labels_sha256': sha256(MAPPED.encode()),
            'operations': [
                {'kind': 'merge', 'sources': ['c4', 'c5'], 'separator': '\n', 'target': 5},
                {'kind': 'split', 'source': 'c1', 'separator': '\n',
                 'targets': [{'line': 2, 'text': 'Hello'}, {'line': 3, 'text': 'world'}]}]}


class DocumentMetadataLossPolicyTests(unittest.TestCase):
    def assert_refusal(self, labels, sidecar, **options):
        output, warned = import_labels(labels, sidecar, **options)
        with self.assertRaisesRegex(BridgeError, 'WebVTT document metadata') as caught:
            import_labels(labels, sidecar, document_metadata_loss_policy='error', **options)
        self.assertIn('--format vtt', str(caught.exception))
        self.assertIn('--document-metadata-loss-policy warn', str(caught.exception))
        report = caught.exception.report
        self.assertEqual(report, {**warned, 'document_metadata_loss_policy': 'error', 'status': 'error'})
        return output, report

    def test_confirmed_header_note_only_gap_under_both_cue_error_policies(self):
        source = b'WEBVTT description\n\nNOTE source note\n\n00:01.000 --> 00:02.000\ntext\n'
        labels, sidecar = export_subtitles(source, 'vtt')
        output, report = self.assert_refusal(labels, sidecar,
            settings_loss_policy='error', identifier_loss_policy='error')
        self.assertEqual(output, '1\n00:00:01,000 --> 00:00:02,000\ntext\n\n')
        self.assertEqual(report['losses'], [{'kind': 'webvtt_document_metadata',
            'fields': ['header', 'notes'], 'reason': 'SRT cannot represent WebVTT header/notes',
            'source_header': 'WEBVTT description',
            'source_notes': [{'note_index': 1, 'before_cue': 0, 'text': 'NOTE source note'}]}])
        self.assertEqual(report['output_count'], 1)
        self.assertEqual(report['output_cues'][0]['line'], 1)

    def test_header_only_note_positions_and_combined_including_deletion(self):
        for header, notes in [('WEBVTT description', False), ('WEBVTT', True),
                              ('WEBVTT description', True)]:
            labels, sidecar = export_subtitles(source_for(header, notes), 'vtt')
            fields = (['header'] if header != 'WEBVTT' else []) + (['notes'] if notes else [])
            for edited in (labels, ''):
                with self.subTest(header=header, notes=notes, deleted=not edited):
                    output, report = self.assert_refusal(edited, sidecar, missing_policy='drop',
                        settings_loss_policy='error', identifier_loss_policy='error', allow_lossy=True)
                    self.assertEqual(report['losses'][0]['fields'], fields)
                    self.assertEqual(report['output_count'], 5 if edited else 0)
                    if not edited:
                        self.assertEqual(output, '')
                        self.assertEqual(report['output_cues'], [])
        # Each non-preamble NOTE position independently counts, even without a header description.
        for note in ['NOTE pre', 'NOTE\tinter\ntext', 'NOTE trailing']:
            source = ('WEBVTT\n\n' + ('NOTE pre\n\n' if note == 'NOTE pre' else '') +
                      '00:01.000 --> 00:02.000\nA\n\n' +
                      (note + '\n\n' if note != 'NOTE pre' else '') +
                      ('00:03.000 --> 00:04.000\nB\n\n' if 'inter' in note else '')).encode()
            labels, sidecar = export_subtitles(source, 'vtt')
            _, report = self.assert_refusal(labels, sidecar)
            self.assertEqual(report['losses'][0]['fields'], ['notes'])

    def test_mapped_kept_dropped_and_new_outputs_restore_vtt_document_text(self):
        _, sidecar = export_subtitles(source_for(), 'vtt')
        for missing, new in itertools.product(('keep', 'drop'), ('add', 'ignore')):
            with self.subTest(missing=missing, new=new):
                options = dict(mapping=mapping_for(sidecar), missing_policy=missing, new_policy=new,
                               settings_loss_policy='error', identifier_loss_policy='error')
                _, refused = self.assert_refusal(MAPPED, sidecar, **options)
                self.assertEqual(refused['output_count'], 4 + (missing == 'keep') + (new == 'add'))
                self.assertEqual(refused['output_cues'][-1]['source_ids'], ['c4', 'c5'])
                self.assertEqual(refused['output_cues'][-1]['line'], 5)
                kept = [x for x in refused['output_cues'] if x['id'] == 'c3']
                self.assertEqual(len(kept), int(missing == 'keep'))
                if kept:
                    self.assertIsNone(kept[0]['line'])
                output, report = import_labels(MAPPED, sidecar, **options, output_format='vtt',
                                                document_metadata_loss_policy='error')
                cues, metadata = parse_vtt(output)
                self.assertEqual(metadata['header'], sidecar['source']['metadata']['header'])
                self.assertEqual([n['text'] for n in metadata['notes']],
                                 [n['text'] for n in sidecar['source']['metadata']['notes']])
                self.assertEqual([n['before_cue'] for n in metadata['notes']], [0, 0, 0])
                self.assertIn({'kind': 'webvtt_notes_to_preamble',
                    'reason': 'NOTE text retained in source order before cues',
                    'relocated_notes': [
                        {'note_index': 2, 'before_cue': 1, 'text': 'NOTE\tinter-cue\nsecond line'},
                        {'note_index': 3, 'before_cue': 5, 'text': 'NOTE trailing'}]}, report['actions'])
                self.assertEqual(report['losses'], [])
                self.assertEqual([(c.start_ms, c.end_ms) for c in cues if c.text == 'Hello'], [(1250, 2000)])
        # Deliberate VTT deletion still carries all document text and the relocation audit.
        output, report = import_labels('', sidecar, missing_policy='drop', output_format='vtt',
                                       document_metadata_loss_policy='error')
        self.assertEqual(output, '\n\n'.join([sidecar['source']['metadata']['header'],
            *(n['text'] for n in sidecar['source']['metadata']['notes'])]) + '\n\n')
        self.assertEqual(report['output_cues'], [])
        self.assertEqual(report['losses'], [])

    def test_three_policies_are_independent_and_errors_include_all_losses(self):
        labels, sidecar = export_subtitles(source_for(identifiers=True, settings=True), 'vtt')
        _, warned = import_labels(labels, sidecar)
        for settings, identifiers, document in itertools.product(('warn', 'error'), repeat=3):
            options = dict(settings_loss_policy=settings, identifier_loss_policy=identifiers,
                           document_metadata_loss_policy=document)
            expected = {**warned, **options}
            if 'error' in options.values():
                with self.subTest(options=options), self.assertRaises(BridgeError) as caught:
                    import_labels(labels, sidecar, **options)
                self.assertEqual(caught.exception.report, {**expected, 'status': 'error'})
                message = ('cue settings' if settings == 'error' else 'cue identifiers'
                           if identifiers == 'error' else 'document metadata')
                self.assertIn(message, str(caught.exception))
            else:
                self.assertEqual(import_labels(labels, sidecar, **options)[1], expected)
        # Document strictness alone accepts cue losses, including identified merges in VTT.
        labels, plain = export_subtitles(source_for(header='WEBVTT', notes=False,
                                                   identifiers=True, settings=True), 'vtt')
        _, report = import_labels(labels, plain, document_metadata_loss_policy='error')
        self.assertEqual({x['kind'] for x in report['losses']},
                         {'webvtt_cue_identifier', 'webvtt_cue_settings'})
        _, report = import_labels(MAPPED, sidecar, mapping=mapping_for(sidecar), new_policy='add',
            missing_policy='keep', output_format='vtt', document_metadata_loss_policy='error')
        self.assertEqual([x['kind'] for x in report['losses']], ['merged_cue_identifiers'])
        # Conversely, document refusal on composed SRT includes every mapped cue loss.
        _, report = self.assert_refusal(MAPPED, sidecar, mapping=mapping_for(sidecar),
                                        new_policy='add', missing_policy='keep')
        self.assertEqual({x['kind'] for x in report['losses']}, {'merged_cue_identifiers',
            'webvtt_document_metadata', 'webvtt_cue_identifier', 'webvtt_cue_settings'})
        self.assertEqual(report['losses'][0]['srt_index'], report['output_count'])

    def test_warn_defaults_legacy_plain_and_unknown_metadata(self):
        for source_format, source in [('vtt', source_for()),
             ('vtt', source_for(header='WEBVTT', notes=False)),
             ('srt', b'7\n00:00:01,000 --> 00:00:02,000\ntext\n')]:
            labels, sidecar = export_subtitles(source, source_format)
            for output_format in ('srt', 'vtt'):
                default = import_labels(labels, sidecar, output_format=output_format)
                self.assertEqual(default, import_labels(labels, sidecar, output_format=output_format,
                                                        document_metadata_loss_policy='warn'))
                self.assertEqual(default[1]['document_metadata_loss_policy'], 'warn')
                if source == source_for() and output_format == 'srt':
                    from .test_bridge import resign
                    sidecar['schema'] = 'audacity-subtitle-bridge/1'
                    resign(sidecar)
                    self.assert_refusal(labels, sidecar)
                else:
                    import_labels(labels, sidecar, output_format=output_format,
                        document_metadata_loss_policy='error', settings_loss_policy='error',
                        identifier_loss_policy='error')
        for format in ('srt', 'vtt'):
            _, audit = import_labels('1\t2\tplain', allow_lossy=True, output_format=format,
                document_metadata_loss_policy='error', settings_loss_policy='error', identifier_loss_policy='error')
            self.assertEqual(audit['losses'], [])

    def test_invalid_policies_and_early_errors(self):
        labels, sidecar = export_subtitles(source_for(), 'vtt')
        for invalid in ('ignore', 'ERROR', '', None, 1, []):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(BridgeError, 'Unknown document-metadata-loss policy'):
                import_labels(labels, sidecar, document_metadata_loss_policy=invalid)
        for edited, options, message in [
            (labels.replace('[cue:c1]', '[cue:unknown]'), {}, 'unknown cue ID'),
            (labels.replace('Hello / world', 'changed'), {}, 'Unresolved edits'),
            (MAPPED, {'mapping': {**mapping_for(sidecar), 'labels_sha256': '0' * 64}}, 'hash mismatch')]:
            with self.subTest(message=message), self.assertRaisesRegex(BridgeError, message) as caught:
                import_labels(edited, sidecar, document_metadata_loss_policy='error', **options)
            self.assertEqual(caught.exception.report['document_metadata_loss_policy'], 'error')
            self.assertEqual(caught.exception.report['output_cues'], [])


if __name__ == '__main__':
    unittest.main()
