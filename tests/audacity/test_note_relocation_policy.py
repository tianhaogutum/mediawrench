"""NOTE placement refusal is independent of document/cue text loss decisions."""

import itertools
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from mediawrench.audacity.formats import parse_vtt
from .test_document_metadata_loss_policy import source_for, mapping_for, MAPPED


RELOCATION = {'kind': 'webvtt_notes_to_preamble',
              'reason': 'NOTE text retained in source order before cues'}
STRICT_LOSS = dict(settings_loss_policy='error', identifier_loss_policy='error',
                   document_metadata_loss_policy='error')


class NoteRelocationPolicyTests(unittest.TestCase):
    @staticmethod
    def relocation(sidecar):
        return {**RELOCATION, 'relocated_notes': [
            dict(note_index=i, before_cue=n['before_cue'], text=n['text'])
            for i, n in enumerate(sidecar['source']['metadata']['notes'], 1) if n['before_cue'] != 0]}

    def assert_refusal(self, labels, sidecar, **options):
        output, allowed = import_labels(labels, sidecar, output_format='vtt', **options)
        self.assertIn(self.relocation(sidecar), allowed['actions'])
        with self.assertRaisesRegex(BridgeError, 'WebVTT NOTE relocation') as caught:
            import_labels(labels, sidecar, output_format='vtt', note_relocation_policy='error', **options)
        self.assertIn('--note-relocation-policy allow', str(caught.exception))
        report = caught.exception.report
        self.assertEqual(report, {**allowed, 'status': 'error', 'note_relocation_policy': 'error'})
        return output, report

    def test_confirmed_gap_with_all_three_strict_loss_policies(self):
        source = (b'WEBVTT description\n\nNOTE preamble\n\n00:01.000 --> 00:02.000\nfirst\n\n'
                  b'NOTE between\n\n00:03.000 --> 00:04.000\nsecond\n\nNOTE trailing\n\n')
        labels, sidecar = export_subtitles(source, 'vtt')
        edited = labels.replace('1.000\t2.000', '1.250\t2.250').replace('3.000\t4.000', '3.250\t4.250')
        # First prove the installed probe's exact behavior still applies by default.
        output, audit = import_labels(edited, sidecar, output_format='vtt', **STRICT_LOSS)
        self.assertEqual(audit['losses'], [])
        self.assertEqual([a['kind'] for a in audit['actions']], ['moved', 'moved', 'webvtt_notes_to_preamble'])
        self.assertEqual(output, 'WEBVTT description\n\nNOTE preamble\n\nNOTE between\n\nNOTE trailing\n\n'
                                '00:00:01.250 --> 00:00:02.250\nfirst\n\n00:00:03.250 --> 00:00:04.250\nsecond\n\n')
        _, report = self.assert_refusal(edited, sidecar, source=source, allow_lossy=True, **STRICT_LOSS)
        self.assertEqual(report['output_count'], 2)
        self.assertEqual([c['line'] for c in report['output_cues']], [1, 2])
        self.assertTrue(report['source_verified'])

    def test_each_non_preamble_position_unchanged_timing_and_empty_deletion(self):
        for position in ('between', 'trailing', 'both'):
            source = ('WEBVTT\n\n00:01.000 --> 00:02.000\nA\n\n' +
                      ('NOTE inter\n exact 同じ  \n\n' if position != 'trailing' else '') +
                      '00:03.000 --> 00:04.000\nB\n\n' +
                      ('NOTE trailing\n\n' if position != 'between' else '')).encode()
            labels, sidecar = export_subtitles(source, 'vtt')
            for edited in (labels, labels.replace('1.000', '1.250'), ''):
                with self.subTest(position=position, edited=edited):
                    output, report = self.assert_refusal(edited, sidecar, missing_policy='drop', **STRICT_LOSS)
                    self.assertEqual(report['losses'], [])
                    self.assertEqual(report['output_count'], 2 if edited else 0)
                    self.assertEqual(report['actions'].count(self.relocation(sidecar)), 1)
                    if not edited:
                        self.assertEqual(report['output_cues'], [])
                        self.assertEqual(output, 'WEBVTT\n\n' + ''.join(n['text']+'\n\n' for n in sidecar['source']['metadata']['notes']))

    def test_split_merge_kept_dropped_new_ignored_and_complete_losses(self):
        for cue_metadata, missing, new in itertools.product((False, True), ('keep', 'drop'), ('add', 'ignore')):
            _, sidecar = export_subtitles(source_for(identifiers=cue_metadata, settings=cue_metadata), 'vtt')
            with self.subTest(metadata=cue_metadata, missing=missing, new=new):
                output, report = self.assert_refusal(MAPPED, sidecar, mapping=mapping_for(sidecar),
                    missing_policy=missing, new_policy=new, settings_loss_policy='error',
                    document_metadata_loss_policy='error')
                self.assertEqual(report['output_count'], 4 + (missing == 'keep') + (new == 'add'))
                self.assertEqual([x['kind'] for x in report['losses']], ['merged_cue_identifiers'] if cue_metadata else [])
                self.assertEqual(report['output_cues'][-1]['source_ids'], ['c4', 'c5'])
                self.assertEqual(report['output_cues'][-1]['line'], 5)
                kept = [c for c in report['output_cues'] if c['id'] == 'c3']
                self.assertEqual(len(kept), int(missing == 'keep'))
                if kept:
                    self.assertIsNone(kept[0]['line'])
                _, metadata = parse_vtt(output)
                self.assertEqual(metadata['header'], sidecar['source']['metadata']['header'])
                self.assertEqual([n['text'] for n in metadata['notes']], [n['text'] for n in sidecar['source']['metadata']['notes']])
                self.assertEqual([n['before_cue'] for n in metadata['notes']], [0, 0, 0])
                if cue_metadata:
                    self.assertEqual(report['losses'][0]['vtt_index'], report['output_count'])
                    self.assertEqual(report['losses'][0]['source_identifiers'], ['speaker-4', 'speaker-5'])

    def test_safe_negative_cases_and_allow_default(self):
        for source in [b'WEBVTT\n\n00:01.000 --> 00:02.000\ntext\n',
                       b'WEBVTT description\n\n00:01.000 --> 00:02.000\ntext\n',
                       b'WEBVTT description\n\nNOTE preamble\n exact  \n\nNOTE second preamble\n\n00:01.000 --> 00:02.000\ntext\n',
                       b'7\n00:00:01,000 --> 00:00:02,000\ntext\n']:
            format = 'vtt' if source.startswith(b'WEBVTT') else 'srt'
            labels, sidecar = export_subtitles(source, format)
            for edited in (labels, ''):
                for output_format in ('vtt', 'srt'):
                    with self.subTest(source=source, deleted=not edited, format=output_format):
                        options = dict(output_format=output_format, missing_policy='drop')
                        default = import_labels(edited, sidecar, **options)
                        self.assertEqual(default, import_labels(edited, sidecar, note_relocation_policy='allow', **options))
                        self.assertEqual(default[1]['note_relocation_policy'], 'allow')
                        output, report = import_labels(edited, sidecar, note_relocation_policy='error', **options)
                        self.assertEqual(output, default[0])
                        self.assertEqual(report, {**default[1], 'note_relocation_policy': 'error'})
                        self.assertNotIn(RELOCATION['kind'], [a['kind'] for a in report['actions']])
        for output_format in ('vtt', 'srt'):
            _, report = import_labels('1\t2\tplain\n', allow_lossy=True, output_format=output_format,
                                      note_relocation_policy='error', **STRICT_LOSS)
            self.assertEqual(report['losses'], [])
            self.assertNotIn(RELOCATION['kind'], [a['kind'] for a in report['actions']])

    def test_all_policy_combinations_independent(self):
        labels, sidecar = export_subtitles(source_for(identifiers=True, settings=True), 'vtt')
        for output_format, settings, identifiers, document, notes in itertools.product(
                ('srt', 'vtt'), ('warn', 'error'), ('warn', 'error'), ('warn', 'error'), ('allow', 'error')):
            options = dict(output_format=output_format, settings_loss_policy=settings,
                identifier_loss_policy=identifiers, document_metadata_loss_policy=document, note_relocation_policy=notes)
            _, allowed = import_labels(labels, sidecar, output_format=output_format)
            expected = {**allowed, **{k:v for k,v in options.items() if k != 'output_format'}}
            message = (('cue settings' if settings == 'error' else 'cue identifiers' if identifiers == 'error'
                       else 'document metadata' if document == 'error' else None) if output_format == 'srt'
                       else 'NOTE relocation' if notes == 'error' else None)
            with self.subTest(**options):
                if message:
                    with self.assertRaisesRegex(BridgeError, message) as caught:
                        import_labels(labels, sidecar, **options)
                    self.assertEqual(caught.exception.report, {**expected, 'status': 'error'})
                else:
                    self.assertEqual(import_labels(labels, sidecar, **options)[1], expected)
        # Identified merge refusal takes precedence, with both the complete loss and relocation action.
        with self.assertRaisesRegex(BridgeError, 'cue identifiers') as caught:
            import_labels(MAPPED, sidecar, output_format='vtt', mapping=mapping_for(sidecar),
                          missing_policy='keep', new_policy='add', note_relocation_policy='error', **STRICT_LOSS)
        self.assertIn(self.relocation(sidecar), caught.exception.report['actions'])
        self.assertEqual(caught.exception.report['losses'][0]['vtt_index'], 6)

    def test_legacy_sidecar_notes_also_refused(self):
        from .test_bridge import resign
        labels, sidecar = export_subtitles(source_for(), 'vtt')
        sidecar['schema'] = 'audacity-subtitle-bridge/1'
        resign(sidecar)
        self.assert_refusal(labels, sidecar, **STRICT_LOSS)

    def test_invalid_policy_and_initialized_error_audits(self):
        labels, sidecar = export_subtitles(source_for(), 'vtt')
        for invalid in ('warn', 'ignore', 'ERROR', '', None, 1, []):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(BridgeError, 'Unknown note-relocation policy'):
                import_labels(labels, sidecar, note_relocation_policy=invalid)
        for policy in ('allow', 'error'):
            for edited, options, message in [
                (labels.replace('[cue:c1]', '[cue:unknown]'), {}, 'unknown cue ID'),
                (labels.replace('Hello / world', 'changed'), {}, 'Unresolved edits'),
                (MAPPED, {'mapping': {**mapping_for(sidecar), 'labels_sha256': '0' * 64}}, 'hash mismatch'),
                (labels, {'source': b'wrong bytes'}, 'Source hash mismatch')]:
                with self.subTest(policy=policy, message=message), self.assertRaisesRegex(BridgeError, message) as caught:
                    import_labels(edited, sidecar, note_relocation_policy=policy, **options)
                self.assertEqual(caught.exception.report['note_relocation_policy'], policy)
                self.assertEqual(caught.exception.report['output_cues'], [])


if __name__ == '__main__':
    unittest.main()
