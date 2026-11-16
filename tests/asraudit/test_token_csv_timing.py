"""Timing availability filters the token CSV only."""

from copy import deepcopy
import csv
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.asraudit import AuditError, audit, output
from .test_token_csv import FIELDS, FIXTURE, FORMAT, ROOT
from .test_token_timed_only import mixed_native

HEADER = ','.join(FIELDS) + '\n'


def rows(text):
    return list(csv.DictReader(io.StringIO(text, newline='')))


def paths(*positions):
    return [f'transcription[{s}].tokens[{t}]' for s, t in positions]


class TokenCSVTimingTests(unittest.TestCase):
    def assert_filters(self, document, timed, untimed):
        original = deepcopy(document)
        report = audit(document, input_format=FORMAT)
        before = deepcopy(report)
        flag_csv, srt = output.csv_flags(report), output.review_srt(report)
        all_csv = output.csv_tokens(report)
        all_rows = rows(all_csv)
        self.assertEqual(output.csv_tokens(report, timing='all'), all_csv)
        for mode, expected in (('timed', timed), ('untimed', untimed)):
            text = output.csv_tokens(report, timing=mode)
            actual = rows(text)
            self.assertEqual([r['source_path'] for r in actual], expected)
            self.assertEqual(expected, report['token_evidence_summary'][mode + '_token_source_paths'])
            self.assertEqual(actual, [r for r in all_rows if r['source_path'] in expected])
            # Preserve complete raw row serialization, indices and header.
            reference = io.StringIO(newline='')
            writer = csv.DictWriter(reference, fieldnames=FIELDS, lineterminator='\n')
            writer.writeheader()
            writer.writerows(actual)
            self.assertEqual(text, reference.getvalue())
            if not expected:
                self.assertEqual(text, HEADER)
        self.assertEqual(report, before)
        self.assertEqual(output.csv_flags(report), flag_csv)
        self.assertEqual(output.review_srt(report), srt)
        self.assertEqual(document, original)
        return report

    def test_exact_readme_fixture_outputs(self):
        report = self.assert_filters(json.loads(FIXTURE.read_bytes()), paths((0, 0), (1, 0)),
                                     paths((0, 1), (0, 2)))
        self.assertEqual([r['token_index'] for r in rows(output.csv_tokens(report, timing='untimed'))], ['1', '2'])
        self.assertEqual([r['text'] for r in rows(output.csv_tokens(report, timing='untimed'))],
                         ['[_EOT_]', ' 世界, "quoted"\n=1+1'])
        self.assertEqual(report['token_evidence_summary']['token_rows'], 4)

    def test_source_order_across_missing_empty_and_unflagged_views(self):
        self.assert_filters(mixed_native(), paths((2, 0), (5, 0), (7, 1), (10, 0)),
                            paths((2, 1), (2, 2), (3, 0), (3, 1), (7, 0), (8, 0)))
        base = mixed_native()['transcription'][3]
        base['tokens'] = [deepcopy(base['tokens'][0]) for _ in range(12)]
        self.assert_filters({'transcription': [base]}, [], paths(*((0, i) for i in range(12))))

    def test_all_timing_masks_ordinary_special_and_mixed_identity(self):
        base = mixed_native()['transcription'][3]
        for identities in ((0, 0, 0), (1, 1, 1), (0, 1, 0)):
            for mask in itertools.product((False, True), repeat=3):
                with self.subTest(identities=identities, mask=mask):
                    record = deepcopy(base)
                    record['tokens'] = [deepcopy(base['tokens'][i]) for i in identities]
                    for i, (token, timed) in enumerate(zip(record['tokens'], mask)):
                        token.update(p=i % 2, t_dtw=1e200 if i % 2 else -1)
                        if timed:
                            token.update(offsets={'from': 0, 'to': 0},
                                         timestamps=dict.fromkeys(('from', 'to'), '00:00:00,000'))
                    self.assert_filters({'transcription': [record]},
                        paths(*((0, i) for i, timed in enumerate(mask) if timed)),
                        paths(*((0, i) for i, timed in enumerate(mask) if not timed)))

    def test_unusual_pairs_remain_timed_even_on_invalid_segments(self):
        pairs = [(0, 0, '00:00:00,000', '00:00:00,000'),
                 (12000, 11000, '00:00:12,000', '00:00:11,000'),
                 (9007199254740991, 11000, '2501999792:59:00,991', '00:00:11,000')]
        for start, end, a, b in pairs:
            for invalid in (False, True):
                with self.subTest(start=start, invalid=invalid):
                    record = deepcopy(mixed_native()['transcription'][3])
                    record['tokens'][1].update(offsets={'from': start, 'to': end},
                                                timestamps={'from': a, 'to': b})
                    if invalid:
                        record['offsets']['to'] = record['offsets']['from']
                        record['timestamps']['to'] = record['timestamps']['from']
                    self.assert_filters({'transcription': [record]}, paths((0, 1)), paths((0, 0)))

    def test_header_only_and_default_format_gate(self):
        native = mixed_native()
        for document in ({'transcription': []}, *({'transcription': [native['transcription'][i]]}
                                                 for i in (0, 1, 4))):
            self.assert_filters(document, [], [])
        default = audit({'segments': [dict(start=0, end=0, text='x', tokens=[])]})
        for mode in ('all', 'timed', 'untimed'):
            with self.assertRaisesRegex(AuditError, 'whisper.cpp-json-v1.7.6'):
                output.csv_tokens(default, timing=mode)

    def test_renderer_rejects_unknown_values(self):
        report = audit(json.loads(FIXTURE.read_bytes()), input_format=FORMAT)
        for value in ('', 'Timed', 'unknown', None, 0):
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, 'timing'):
                output.csv_tokens(report, timing=value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'source.json'
        self.source.write_bytes(FIXTURE.read_bytes())
        self.targets = [self.directory / name for name in ('report.json', 'flags.csv', 'tokens.csv', 'review.srt')]

    def cli(self, *extra, tokens=True, native=True):
        args = [sys.executable, '-m', 'mediawrench.asraudit', str(self.source)]
        if native:
            args += ['--input-format', FORMAT]
        for opt, path in zip(('--json', '--csv', '--token-csv', '--review-srt'), self.targets):
            if tokens or opt != '--token-csv':
                args += [opt, str(path)]
        return subprocess.run([*args, *map(str, extra)], cwd=ROOT, capture_output=True, text=True, timeout=120)

    def assert_no_outputs(self):
        self.assertFalse(any(p.exists() for p in self.targets))
        self.assertEqual(list(self.directory.glob('.asr-audit-*')), [])

    def test_cli_filters_only_csv_and_dry_run_preserves_full_json(self):
        snapshots = self.source.read_bytes()
        baseline = None
        for mode in ('all', 'timed', 'untimed'):
            dry = self.cli('--token-csv-timing', mode, '--dry-run')
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assert_no_outputs()
            done = self.cli('--token-csv-timing', mode)
            self.assertEqual(done.returncode, 0, done.stderr)
            contents = [p.read_bytes() for p in self.targets]
            report = json.loads(contents[0])
            self.assertEqual(json.loads(dry.stdout), report)
            unchanged = [contents[i] for i in (0, 1, 3)]
            if baseline is None:
                baseline = unchanged
            self.assertEqual(unchanged, baseline)
            expected = paths((0, 0), (0, 1), (0, 2), (1, 0)) if mode == 'all' else report['token_evidence_summary'][mode + '_token_source_paths']
            self.assertEqual([r['source_path'] for r in rows(contents[2].decode())], expected)
            self.assertEqual(report['token_evidence_summary']['token_rows'], 4)
            for p in self.targets:
                p.unlink()
        self.assertEqual(self.source.read_bytes(), snapshots)

    def test_cli_rejects_bad_option_combinations_before_outputs(self):
        for dry in ((), ('--dry-run',)):
            for mode in ('all', 'timed', 'untimed'):
                done = self.cli('--token-csv-timing', mode, *dry, tokens=False)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn('--token-csv-timing requires --token-csv', done.stderr)
                self.assertEqual(done.stdout, '')
                self.assert_no_outputs()
            done = self.cli('--token-csv-timing', 'unknown', *dry)
            self.assertEqual(done.returncode, 2)
            self.assertIn('invalid choice', done.stderr)
            self.assert_no_outputs()
            done = self.cli('--token-csv-timing', 'timed', *dry, native=False)
            self.assertEqual(done.returncode, 2)
            self.assertIn('--token-csv requires --input-format', done.stderr)
            self.assert_no_outputs()

    def test_excluded_and_unflagged_native_rows_still_validate(self):
        for mode in ('timed', 'untimed'):
            for segment in (0, 2):
                for field, value in (('p', '0'), ('timestamps', None), ('offsets', {'from': 1, 'to': 1})):
                    with self.subTest(mode=mode, segment=segment, field=field):
                        document = json.loads(FIXTURE.read_bytes())
                        # Row 0 would be excluded by untimed; untimed row 1 by timed.
                        index = 1 if segment == 0 and mode == 'timed' else 0
                        document['transcription'][segment]['tokens'][index][field] = value
                        self.source.write_text(json.dumps(document), encoding='utf-8')
                        done = self.cli('--token-csv-timing', mode)
                        self.assertEqual(done.returncode, 2)
                        self.assertIn(f'transcription[{segment}].tokens[{index}].', done.stderr)
                        self.assert_no_outputs()

    def test_filtered_destinations_keep_no_overwrite_and_alias_protection(self):
        before = self.source.read_bytes()
        for mode in ('timed', 'untimed'):
            self.targets[2].write_text('keep')
            done = self.cli('--token-csv-timing', mode)
            self.assertEqual(done.returncode, 2)
            self.assertIn('already exists', done.stderr)
            self.assertEqual(self.targets[2].read_text(), 'keep')
            self.targets[2].unlink()
            for kind in ('direct', 'symlink', 'hardlink'):
                destination = self.source if kind == 'direct' else self.directory / kind
                if kind == 'symlink':
                    destination.symlink_to(self.source)
                elif kind == 'hardlink':
                    destination.hardlink_to(self.source)
                done = self.cli('--token-csv-timing', mode, '--token-csv', destination, '--dry-run')
                self.assertEqual(done.returncode, 2)
                self.assertIn('aliases an input', done.stderr)
                if kind != 'direct':
                    destination.unlink()
                self.assert_no_outputs()
        self.assertEqual(self.source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
