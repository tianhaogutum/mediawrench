"""Merged settings losses expose exact original identifiers without an audit join."""

from copy import deepcopy
import unittest

from mediawrench.audacity import BridgeError, export_subtitles, import_labels
from .test_merged_identifier_loss_provenance import (
    SOURCE, LABELS, EXPECTED_SRT, EXPECTED_VTT, MERGED_LOSSES, SRT_WARNING,
    mapping_for,
)


IDENTIFIER = '  voix / 同じ  '
CASES = [([IDENTIFIER, IDENTIFIER], [None, 'delta']),
         (['alpha', None], ['gamma', None]),
         ([None, None], [None, None])]


def source_for(identifiers, settings):
    source = SOURCE
    for old, value in zip((b'alpha\n', b'beta\n', b'00:06.000', b'delta\n'),
                          identifiers[0] + identifiers[1]):
        replacement = (value + '\n').encode() if value is not None else b''
        if old == b'00:06.000':
            replacement += old
        source = source.replace(old, replacement)
    return source.replace(b'.000\n', b'.000 align:start\n') if settings else source


class MergedSettingsIdentifiersTests(unittest.TestCase):
    def test_exact_arrays_for_reordered_merges_under_warn_and_strict_policies(self):
        for identifiers in CASES:
            for settings in (False, True):
                _, sidecar = export_subtitles(source_for(identifiers, settings), 'vtt')
                mapping = mapping_for(sidecar)
                originals = deepcopy((sidecar, mapping))
                for new in ('ignore', 'add'):
                    offset = int(new == 'add')
                    for format in ('srt', 'vtt'):
                        warned = None
                        for policy in ('warn', 'error'):
                            with self.subTest(identifiers=identifiers, settings=settings,
                                              new=new, format=format, policy=policy):
                                options = dict(mapping=mapping, new_policy=new,
                                    output_format=format, settings_loss_policy=policy)
                                strict = settings and format == 'srt' and policy == 'error'
                                if strict:
                                    with self.assertRaises(BridgeError) as caught:
                                        import_labels(LABELS, sidecar, **options)
                                    report = caught.exception.report
                                else:
                                    output, report = import_labels(LABELS, sidecar, **options)
                                    expected = EXPECTED_SRT if format == 'srt' else EXPECTED_VTT
                                    if offset:
                                        if format == 'srt':
                                            for index in (3, 2, 1):
                                                expected = expected.replace(f'{index}\n00:', f'{index + 1}\n00:')
                                            expected = '1\n00:00:00,000 --> 00:00:00,100\nnew\n\n' + expected
                                        else:
                                            expected = expected.replace('WEBVTT\n\n',
                                                'WEBVTT\n\n00:00:00.000 --> 00:00:00.100\nnew\n\n')
                                    if settings and format == 'vtt':
                                        # New cues never inherit source settings.
                                        expected = expected.replace('.000\n', '.000 align:start\n')
                                    self.assertEqual(output, expected)
                                if policy == 'warn':
                                    warned = report
                                else:
                                    self.assertEqual(report, {**warned, 'settings_loss_policy': 'error',
                                        'status': 'error' if strict else 'ok'})
                                expected_losses, warnings = [], []
                                for operation, values, index, line in zip(
                                        (1, 2), identifiers, (3 + offset, 1 + offset), (4, 2)):
                                    if any(value is not None for value in values):
                                        expected_losses.append({**MERGED_LOSSES[operation - 1],
                                            'source_identifiers': values, format + '_index': index, 'line': line})
                                        warnings.append(f'Mapping operation {operation}: merged WebVTT identifiers '
                                                        'retained in audit; output cue has no identifier')
                                if format == 'srt':
                                    warnings.append(SRT_WARNING)
                                    if settings:
                                        warnings.append('LOSS: WebVTT cue settings cannot be represented in SRT; '
                                                        'see losses for each cue or choose --format vtt')
                                        for index, line, operation in [(1 + offset, 2, 2),
                                                                      (2 + offset, 3, None),
                                                                      (3 + offset, 4, 1)]:
                                            expected_losses.append({
                                                'kind': 'webvtt_cue_settings',
                                                'id': None if operation else 'c3', 'source_id': None,
                                                'settings': 'align:start', 'srt_index': index, 'line': line,
                                                **({'mapping_operation': operation,
                                                    'source_ids': ['c1', 'c2'] if operation == 1 else ['c4', 'c5'],
                                                    'source_identifiers': identifiers[operation - 1]} if operation else {}),
                                                'reason': 'SRT cannot represent WebVTT cue settings'})
                                self.assertEqual(report['losses'], expected_losses)
                                self.assertEqual(report['warnings'], warnings)
                                self.assertEqual([c['line'] for c in report['output_cues']],
                                                 ([1] if offset else []) + [2, 3, 4])
                                self.assertEqual((sidecar, mapping), originals)


if __name__ == '__main__':
    unittest.main()
