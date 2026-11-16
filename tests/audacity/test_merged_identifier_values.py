"""Merged losses retain original identifiers in source order, including nulls."""

from copy import deepcopy
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_merged_identifier_loss_provenance import (
    SOURCE, LABELS, EXPECTED_SRT, EXPECTED_VTT, MERGED_LOSSES, WARNINGS,
    SRT_WARNING, mapping_for, merged_losses,
)


class MergedIdentifierValuesTests(unittest.TestCase):
    def test_exact_repeated_identifiers_and_null_positions_across_formats_and_policies(self):
        identifier = '  voix / 同じ  '
        repeated = SOURCE.replace(b'alpha\n', (identifier + '\n').encode('utf-8'))
        repeated = repeated.replace(b'beta\n', (identifier + '\n').encode('utf-8'))
        trailing_null = SOURCE.replace(b'beta\n', b'').replace(b'delta\n', b'')
        trailing_null = trailing_null.replace(b'00:06.000', b'gamma\n00:06.000')
        cases = [(repeated, [[identifier, identifier], [None, 'delta']]),
                 (trailing_null, [['alpha', None], ['gamma', None]])]
        for source, identifiers in cases:
            for settings in (False, True):
                payload = source.replace(b'.000\n', b'.000 align:start\n') if settings else source
                _, sidecar = export_subtitles(payload, 'vtt')
                mapping = mapping_for(sidecar)
                original_sidecar, original_mapping = deepcopy(sidecar), deepcopy(mapping)
                for format in ('srt', 'vtt'):
                    warned = None
                    for policy in ('warn', 'error'):
                        with self.subTest(identifiers=identifiers, settings=settings,
                                          format=format, policy=policy):
                            strict_failure = settings and format == 'srt' and policy == 'error'
                            options = dict(mapping=mapping, new_policy='ignore',
                                           output_format=format, settings_loss_policy=policy)
                            if strict_failure:
                                with self.assertRaises(BridgeError) as caught:
                                    import_labels(LABELS, sidecar, **options)
                                report = caught.exception.report
                                self.assertEqual(report['status'], 'error')
                            else:
                                output, report = import_labels(LABELS, sidecar, **options)
                                expected = EXPECTED_SRT if format == 'srt' else EXPECTED_VTT
                                if settings and format == 'vtt':
                                    expected = expected.replace('.000\n', '.000 align:start\n')
                                self.assertEqual(output, expected)
                            if policy == 'warn':
                                warned = report
                            else:
                                self.assertEqual(report, {**warned, 'settings_loss_policy': 'error',
                                    'status': 'error' if strict_failure else 'ok'})
                            warnings = WARNINGS + ([SRT_WARNING] if format == 'srt' else [])
                            if settings and format == 'srt':
                                warnings += ['LOSS: WebVTT cue settings cannot be represented in SRT; '
                                             'see losses for each cue or choose --format vtt']
                            self.assertEqual(report['warnings'], warnings)
                            actions = [a for a in report['actions'] if a['kind'] == 'merge']
                            self.assertEqual(actions, [
                                {'kind': 'merge', 'mapping_operation': i, 'separator': '\n',
                                 'sources': [sidecar['cues'][j] for j in positions],
                                 'targets': [{'line': line, 'text': text}]}
                                for i, positions, line, text in
                                [(1, (0, 1), 4, 'A\nB'), (2, (3, 4), 2, 'C\nD')]])
                            # Keep every old field exact, adding only the aligned identifier array.
                            expected_losses = [
                                {**loss, format + '_index': index, 'line': line, 'source_identifiers': values}
                                for loss, index, line, values in zip(MERGED_LOSSES, (3, 1), (4, 2), identifiers)]
                            self.assertEqual(merged_losses(report), expected_losses)
                            for loss in merged_losses(report):
                                cue = report['output_cues'][loss[format + '_index'] - 1]
                                self.assertEqual(cue['source_ids'], loss['source_ids'])
                                self.assertEqual(cue['mapping_operation'], loss['mapping_operation'])
                            self.assertEqual(sidecar, original_sidecar)
                            self.assertEqual(mapping, original_mapping)


if __name__ == '__main__':
    unittest.main()
