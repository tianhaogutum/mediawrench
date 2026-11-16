"""Identifier refusal depends on complete reconciled losses, independently of settings."""

import unittest

from mediawrench.audacity import (
    BridgeError, create_mapping_template, export_subtitles, import_labels,
)
from mediawrench.audacity.formats import parse_srt, parse_vtt
from .test_merged_settings_identifiers import CASES, source_for
from .test_merged_identifier_loss_provenance import LABELS, mapping_for


SOURCE = ('WEBVTT description\n\nNOTE source note\n\n  voix / 同じ  \n'
          '00:01.000 --> 00:03.000\nHello\nworld\n\n'
          '00:04.000 --> 00:05.000\nplain\n\n'
          'guest\n00:06.000 --> 00:07.000\nTail\n').encode()


class IdentifierLossPolicyTests(unittest.TestCase):
    def assert_refusal(self, labels, sidecar, **options):
        _, warned = import_labels(labels, sidecar, **options)
        with self.assertRaisesRegex(BridgeError, 'WebVTT cue identifiers') as caught:
            import_labels(labels, sidecar, identifier_loss_policy='error', **options)
        self.assertIn('--identifier-loss-policy warn', str(caught.exception))
        report = caught.exception.report
        self.assertEqual(report, {**warned, 'identifier_loss_policy': 'error', 'status': 'error'})
        return report

    def test_ordinary_identifiers_refuse_srt_under_strict_settings(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        output, default = import_labels(labels, sidecar, settings_loss_policy='error')
        explicit, warned = import_labels(labels, sidecar, settings_loss_policy='error',
                                         identifier_loss_policy='warn')
        self.assertEqual((output, default), (explicit, warned))
        self.assertEqual(default['identifier_loss_policy'], 'warn')
        report = self.assert_refusal(labels, sidecar, settings_loss_policy='error')
        self.assertEqual([x['source_id'] for x in report['losses']
                          if x['kind'] == 'webvtt_cue_identifier'], ['  voix / 同じ  ', 'guest'])
        self.assertEqual(report['output_count'], 3)
        self.assertEqual([c.text for c in parse_srt(output)[0]], ['Hello\nworld', 'plain', 'Tail'])

    def test_split_and_ordinary_vtt_restore_while_srt_refuses(self):
        _, sidecar = export_subtitles(SOURCE, 'vtt')
        labels = ('0\t0.1\tnew\n1\t2\tHello\n2\t3\tworld\n'
                  '4\t5\t[cue:c2] plain\n6\t7\t[cue:c3] Tail\n')
        # Templates use strict reconciliation; bind before adding the new row.
        from mediawrench.audacity.core import sha256
        mapping = create_mapping_template(labels.split('\n', 1)[1], sidecar, [
            {'kind': 'split', 'source': 'c1', 'separator': '\n',
             'targets': [{'line': 1, 'text': 'Hello'}, {'line': 2, 'text': 'world'}]}])
        mapping['labels_sha256'] = sha256(labels.encode())
        for target in mapping['operations'][0]['targets']:
            target['line'] += 1
        for new in ('ignore', 'add'):
            with self.subTest(new=new):
                report = self.assert_refusal(labels, sidecar, mapping=mapping, new_policy=new)
                losses = [x for x in report['losses'] if x['kind'] == 'webvtt_cue_identifier']
                shift = int(new == 'add')
                self.assertEqual([(x['srt_index'], x['line']) for x in losses],
                                 [(1 + shift, 2), (2 + shift, 3), (4 + shift, 5)])
                self.assertEqual([x.get('source_ids') for x in losses], [['c1'], ['c1'], None])
                output, audit = import_labels(labels, sidecar, mapping=mapping, new_policy=new,
                    output_format='vtt', identifier_loss_policy='error', settings_loss_policy='error')
                self.assertEqual([c.source_id for c in parse_vtt(output)[0]],
                                 ([None] if shift else []) + ['  voix / 同じ  '] * 2 + [None, 'guest'])
                self.assertEqual(audit['losses'], [])

    def test_merges_refuse_both_formats_only_when_an_original_identifier_exists(self):
        for identifiers in CASES:
            for settings in (False, True):
                _, sidecar = export_subtitles(source_for(identifiers, settings), 'vtt')
                for new in ('ignore', 'add'):
                    for format in ('srt', 'vtt'):
                        with self.subTest(identifiers=identifiers, settings=settings, new=new, format=format):
                            options = dict(mapping=mapping_for(sidecar), new_policy=new, output_format=format)
                            has_ids = any(v is not None for pair in identifiers for v in pair)
                            if has_ids:
                                report = self.assert_refusal(LABELS, sidecar, **options)
                                losses = [x for x in report['losses'] if x['kind'] == 'merged_cue_identifiers']
                                shift = int(new == 'add')
                                expected = [(pair, index + shift, line) for pair, index, line in
                                            [(identifiers[0], 3, 4), (identifiers[1], 1, 2)]
                                            if any(v is not None for v in pair)]
                                self.assertEqual([(x['source_identifiers'], x[format + '_index'], x['line'])
                                                  for x in losses], expected)
                                # VTT must retain its complete proposed metadata/actions too.
                                if settings and format == 'srt':
                                    with self.assertRaises(BridgeError) as caught:
                                        import_labels(LABELS, sidecar, **options,
                                            identifier_loss_policy='error', settings_loss_policy='error')
                                    self.assertEqual(caught.exception.report, {**report, 'settings_loss_policy': 'error'})
                            else:
                                _, report = import_labels(LABELS, sidecar, **options,
                                                          identifier_loss_policy='error')
                                self.assertFalse(any(x['kind'] == 'merged_cue_identifiers'
                                                     for x in report['losses']))
                            if format == 'vtt':
                                # Settings never turn a safe VTT output into a refusal.
                                if has_ids:
                                    self.assert_refusal(LABELS, sidecar, **options, settings_loss_policy='error')
                                else:
                                    import_labels(LABELS, sidecar, **options,
                                        identifier_loss_policy='error', settings_loss_policy='error')

    def test_kept_identifiers_count_dropped_originals_and_new_cues_do_not(self):
        labels, sidecar = export_subtitles(SOURCE, 'vtt')
        edited = '0\t0.1\tnew\n' + labels.splitlines()[1] + '\n'
        for new in ('ignore', 'add'):
            with self.subTest(new=new):
                report = self.assert_refusal(edited, sidecar, missing_policy='keep', new_policy=new,
                                             allow_lossy=True)
                losses = [x for x in report['losses'] if x['kind'] == 'webvtt_cue_identifier']
                self.assertEqual([x['line'] for x in losses], [None, None])
                self.assertEqual([x['srt_index'] for x in losses],
                                 [1 + int(new == 'add'), 3 + int(new == 'add')])
                _, audit = import_labels(edited, sidecar, missing_policy='drop', new_policy=new,
                                          identifier_loss_policy='error')
                self.assertEqual([x['kind'] for x in audit['losses']], ['webvtt_document_metadata'])
        output, report = import_labels('', sidecar, missing_policy='drop', identifier_loss_policy='error')
        self.assertEqual(output, '')
        self.assertEqual(report['output_count'], 0)

    def test_settings_are_independent_and_pre_reconciliation_errors_record_policy(self):
        source = b'WEBVTT\n\n00:01.000 --> 00:02.000 align:start\ntext\n'
        labels, sidecar = export_subtitles(source, 'vtt')
        _, report = import_labels(labels, sidecar, identifier_loss_policy='error')
        self.assertEqual([x['kind'] for x in report['losses']], ['webvtt_cue_settings'])
        with self.assertRaisesRegex(BridgeError, 'WebVTT cue settings') as caught:
            import_labels(labels, sidecar, identifier_loss_policy='error', settings_loss_policy='error')
        self.assertEqual(caught.exception.report['identifier_loss_policy'], 'error')
        with self.assertRaises(BridgeError) as caught:
            import_labels('1\t2\t[cue:unknown] text', sidecar, identifier_loss_policy='error')
        self.assertEqual(caught.exception.report['identifier_loss_policy'], 'error')
        self.assertEqual(caught.exception.report['output_cues'], [])
        for invalid in ('ignore', '', None, 1):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(BridgeError, 'Unknown identifier-loss policy'):
                import_labels(labels, sidecar, identifier_loss_policy=invalid)

    def test_srt_numbers_legacy_sidecars_and_plain_labels(self):
        for format, source in [('srt', b'7\n00:00:01,000 --> 00:00:02,000\ntext\n'),
                               ('vtt', b'WEBVTT\n\nid\n00:01.000 --> 00:02.000\ntext\n')]:
            labels, sidecar = export_subtitles(source, format)
            if format == 'vtt':
                from .test_bridge import resign
                sidecar['schema'] = 'audacity-subtitle-bridge/1'
                resign(sidecar)
                self.assert_refusal(labels, sidecar)
            else:
                for output_format in ('srt', 'vtt'):
                    _, audit = import_labels(labels, sidecar, output_format=output_format,
                                              identifier_loss_policy='error')
                    self.assertEqual(audit['losses'], [])
        _, report = import_labels('1\t2\tplain', allow_lossy=True, identifier_loss_policy='error')
        self.assertEqual(report['losses'], [])


if __name__ == '__main__':
    unittest.main()
