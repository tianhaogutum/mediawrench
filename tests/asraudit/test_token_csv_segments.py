"""Segment set selection affects only the token CSV, never audit coverage."""

from copy import deepcopy
import itertools
import json
import unittest

from mediawrench.asraudit import AuditError, audit, output
from .test_token_csv import FIXTURE, FORMAT
from .test_token_csv_timing import HEADER, paths, rows
from . import test_token_csv_timing as timing_helpers
from .test_token_timed_only import mixed_native


class TokenCSVSegmentRendererTests(unittest.TestCase):
    def test_exact_fixture_selection_duplicates_and_timing_intersection(self):
        document = json.loads(FIXTURE.read_bytes())
        report = audit(document, input_format=FORMAT)
        before, source = deepcopy(report), deepcopy(document)
        sidecars = (output.csv_flags(report), output.review_srt(report))
        for timing in ('all', 'timed', 'untimed'):
            baseline = output.csv_tokens(report, timing=timing)
            self.assertEqual(output.csv_tokens(report, timing=timing, segment_indices=None), baseline)
            self.assertEqual(output.csv_tokens(report, timing=timing, segment_indices=[1, 0, 1]), baseline)
            for selection in ([1], [0], [4, 3, 2], [], [4], [2], [3]):
                with self.subTest(timing=timing, selection=selection):
                    text = output.csv_tokens(report, timing=timing, segment_indices=selection)
                    self.assertEqual(rows(text), [r for r in rows(baseline) if int(r['segment']) in selection])
                    if not rows(text):
                        self.assertEqual(text, HEADER)
        selected = rows(output.csv_tokens(report, segment_indices=[1, 0, 1], timing='untimed'))
        self.assertEqual([r['source_path'] for r in selected], paths((0, 1), (0, 2)))
        self.assertEqual([r['token_index'] for r in selected], ['1', '2'])
        self.assertEqual(selected[1]['text'], ' 世界, "quoted"\n=1+1')
        self.assertEqual(report, before)
        self.assertEqual(document, source)
        self.assertEqual(sidecars, (output.csv_flags(report), output.review_srt(report)))

    def test_all_source_subsets_remain_source_ordered(self):
        document = mixed_native()
        report = audit(document, input_format=FORMAT)
        # All 2**11 subsets include sparse/unflagged/missing/empty source views;
        # reversed selectors plus duplicates must never sort by selector or time.
        for mask in itertools.product((False, True), repeat=len(report['segments'])):
            selected = [i for i, present in enumerate(mask) if present]
            selectors = list(reversed(selected)) + selected
            for timing in ('all', 'timed', 'untimed'):
                baseline = rows(output.csv_tokens(report, timing=timing))
                text = output.csv_tokens(report, timing=timing, segment_indices=selectors)
                self.assertEqual(rows(text), [r for r in baseline if int(r['segment']) in selected])
                if not rows(text):
                    self.assertEqual(text, HEADER)
        for container in ((10, 2, 10), {10, 2}, iter([10, 2, 10])):
            text = output.csv_tokens(report, segment_indices=container)
            self.assertEqual([r['source_path'] for r in rows(text)], paths((2, 0), (2, 1), (2, 2), (10, 0)))

    def test_empty_source_no_flags_and_native_gate(self):
        for doc in ({'transcription': []}, {'transcription': [mixed_native()['transcription'][1]]}):
            report = audit(doc, input_format=FORMAT)
            for selection in (None, [], *([[0]] if doc['transcription'] else [])):
                self.assertEqual(output.csv_tokens(report, segment_indices=selection), HEADER)
        default = audit({'segments': []})
        with self.assertRaisesRegex(AuditError, 'whisper.cpp-json-v1.7.6'):
            output.csv_tokens(default, segment_indices=[])

    def test_invalid_indices_including_after_valid_are_rejected(self):
        report = audit(json.loads(FIXTURE.read_bytes()), input_format=FORMAT)
        for value in (-1, 5, 10**100, True, False, 1.0, '1', None, {}, []):
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, 'segment'):
                output.csv_tokens(report, segment_indices=[0, value])
        for selection in (0, 1.5, True, '01'):
            with self.subTest(selection=selection), self.assertRaisesRegex(AuditError, 'segment'):
                output.csv_tokens(report, segment_indices=selection)
        with self.assertRaisesRegex(AuditError, 'segment'):
            output.csv_tokens(audit({'transcription': []}, input_format=FORMAT), segment_indices=[0])


# Reuse only CLI fixture helpers; do not inherit/rerun the timing-filter tests here.
class TokenCSVSegmentCLITests(unittest.TestCase):
    setUp = timing_helpers.TokenCSVTimingTests.setUp
    cli = timing_helpers.TokenCSVTimingTests.cli
    assert_no_outputs = timing_helpers.TokenCSVTimingTests.assert_no_outputs

    def test_cli_only_token_csv_changes_and_dry_run_is_complete(self):
        original = self.source.read_bytes()
        baseline = None
        for selection, timing, expected in (
            ([], 'all', paths((0, 0), (0, 1), (0, 2), (1, 0))),
            ([1, 0, 1], 'all', paths((0, 0), (0, 1), (0, 2), (1, 0))),
            ([1], 'timed', paths((1, 0))),
            ([1], 'untimed', []),
            ([1, 0, 1], 'untimed', paths((0, 1), (0, 2))),
            ([4, 2, 3, 4], 'all', [])):
            extra = ['--token-csv-timing', timing]
            for i in selection:
                extra += ['--token-csv-segment', str(i)]
            dry = self.cli(*extra, '--dry-run')
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assert_no_outputs()
            done = self.cli(*extra)
            self.assertEqual(done.returncode, 0, done.stderr)
            contents = [p.read_bytes() for p in self.targets]
            report = json.loads(contents[0])
            self.assertEqual(json.loads(dry.stdout), report)
            unchanged = [contents[i] for i in (0, 1, 3)]
            if baseline is None:
                baseline = unchanged
            self.assertEqual(unchanged, baseline)
            self.assertEqual([r['source_path'] for r in rows(contents[2].decode())], expected)
            if not expected:
                self.assertEqual(contents[2].decode(), HEADER)
            self.assertEqual(report['token_evidence_summary']['token_rows'], 4)
            for p in self.targets:
                p.unlink()
        self.assertEqual(self.source.read_bytes(), original)

    def test_cli_rejects_invalid_selectors_and_missing_csv_before_outputs(self):
        for dry in ((), ('--dry-run',)):
            for value in ('-1', '5', '999999999999999999999999', '1.0', 'abc', ''):
                with self.subTest(value=value, dry=dry):
                    done = self.cli('--token-csv-segment', '0', '--token-csv-segment', value, *dry)
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn('segment', done.stderr)
                    self.assertEqual(done.stdout, '')
                    self.assert_no_outputs()
            done = self.cli('--token-csv-segment', '0', *dry, tokens=False)
            self.assertEqual(done.returncode, 2)
            self.assertIn('--token-csv-segment requires --token-csv', done.stderr)
            self.assert_no_outputs()
            done = self.cli('--token-csv-segment', '0', *dry, native=False)
            self.assertEqual(done.returncode, 2)
            self.assertIn('--token-csv requires --input-format', done.stderr)
            self.assert_no_outputs()
        self.source.write_text('{"transcription":[]}')
        done = self.cli('--token-csv-segment', '0', '--dry-run')
        self.assertEqual(done.returncode, 2)
        self.assertIn('segment', done.stderr)
        self.assert_no_outputs()

    def test_excluded_flagged_and_unflagged_rows_still_validate(self):
        for dry in ((), ('--dry-run',)):
            for segment, index in ((0, 0), (0, 1), (2, 0)):
                for field, value in (('p', '0'), ('timestamps', None), ('offsets', {'from': 1, 'to': 1})):
                    doc = json.loads(FIXTURE.read_bytes())
                    doc['transcription'][segment]['tokens'][index][field] = value
                    self.source.write_text(json.dumps(doc))
                    # Select empty view: all rows excluded by segment selector.
                    done = self.cli('--token-csv-segment', '4', '--token-csv-timing', 'untimed', *dry)
                    self.assertEqual(done.returncode, 2)
                    self.assertIn(f'transcription[{segment}].tokens[{index}].', done.stderr)
                    self.assert_no_outputs()

    def test_selected_csv_retains_destination_protections(self):
        original = self.source.read_bytes()
        vad, config = self.directory / 'vad.json', self.directory / 'config.json'
        vad.write_text('[[0,50]]')
        config.write_text('{}')
        extra = ('--vad', vad, '--loop-config', config, '--token-csv-segment', '4')
        self.targets[2].write_text('keep')
        for dry in ((), ('--dry-run',)):
            done = self.cli(*extra, *dry)
            self.assertEqual(done.returncode, 2)
            self.assertIn('already exists', done.stderr)
            self.assertEqual(self.targets[2].read_text(), 'keep')
        self.targets[2].unlink()
        for source in (self.source, vad, config):
            for kind in ('direct', 'symlink', 'hardlink'):
                target = source if kind == 'direct' else self.directory / kind
                if kind == 'symlink':
                    target.symlink_to(source)
                elif kind == 'hardlink':
                    target.hardlink_to(source)
                done = self.cli(*extra, '--token-csv', target, '--dry-run')
                self.assertEqual(done.returncode, 2)
                self.assertIn('aliases an input', done.stderr)
                if kind != 'direct':
                    target.unlink()
                self.assert_no_outputs()
        done = self.cli(*extra, '--token-csv', self.targets[0])
        self.assertEqual(done.returncode, 2)
        self.assertIn('duplicate output', done.stderr)
        self.assert_no_outputs()
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
