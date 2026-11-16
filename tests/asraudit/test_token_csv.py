"""Flat token CSV review export contract.

Synthetic native fixture: flags 0/1 have 4 timed/untimed/special/quoted Unicode
records; flag 3 has no tokens; flag 4 has []; segment 2 (p=0) stays unflagged.
Missing timing is an empty CSV cell. No scores/reasons or numeric aggregation.
"""

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.asraudit import audit
from mediawrench.asraudit import output

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/asraudit/fixtures/whisper_cpp_token_csv_v1_7_6.json'
FORMAT = 'whisper.cpp-json-v1.7.6'
FIELDS = ['segment', 'token_index', 'source_path', 'text', 'id', 'p', 't_dtw', 'offsets', 'timestamps']
SUMMARY = dict(flagged_segments=4, unavailable_segments=1, empty_segments=1,
               segments_with_tokens=2, token_rows=4, timed_token_rows=2, untimed_token_rows=2,
               unavailable_segment_indices=[3], empty_segment_indices=[4], untimed_only_segment_indices=[], mixed_timing_segment_indices=[0], timed_only_segment_indices=[1],
            timed_token_source_paths=['transcription[0].tokens[0]', 'transcription[1].tokens[0]'],
            untimed_token_source_paths=['transcription[0].tokens[1]', 'transcription[0].tokens[2]'],
            segment_token_counts=[
                {'segment': 0, 'token_rows': 3, 'timed_token_rows': 1, 'untimed_token_rows': 2},
                {'segment': 1, 'token_rows': 1, 'timed_token_rows': 1, 'untimed_token_rows': 0}])


class TokenCSVTests(unittest.TestCase):
    def setUp(self):
        self.native = json.loads(FIXTURE.read_bytes())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'source.json'
        self.source.write_bytes(FIXTURE.read_bytes())
        self.paths = [self.directory / name for name in ('report.json', 'flags.csv', 'tokens.csv', 'review.srt')]

    def run_cli(self, *extra, native=True, outputs=True):
        args = [sys.executable, '-m', 'mediawrench.asraudit', str(self.source)]
        if native:
            args += ['--input-format', FORMAT]
        if outputs:
            for option, path in zip(('--json', '--csv', '--token-csv', '--review-srt'), self.paths):
                args += [option, str(path)]
        return subprocess.run([*args, *map(str, extra)], cwd=ROOT, capture_output=True, text=True, timeout=120)

    def assert_rows(self, report, text):
        reader = csv.DictReader(io.StringIO(text, newline=''))
        rows = list(reader)
        self.assertEqual(reader.fieldnames, FIELDS)
        expected = [(f['segment'], j, token) for f in report['flags']
                    if f['token_evidence'] is not None
                    for j, token in enumerate(f['token_evidence']['tokens'])]
        self.assertEqual(len(rows), len(expected))
        for row, (segment, index, entry) in zip(rows, expected):
            self.assertEqual((int(row['segment']), int(row['token_index'])), (segment, index))
            self.assertEqual(row['source_path'], entry['source_path'])
            raw = report['source_document']['transcription'][segment]['tokens'][index]
            self.assertEqual(raw, entry['token'])
            self.assertEqual(row['text'], raw['text'])
            for field in ('id', 'p', 't_dtw'):
                self.assertEqual(json.loads(row[field]), raw[field])
            for field in ('offsets', 'timestamps'):
                self.assertEqual(json.loads(row[field]) if row[field] else None, raw.get(field))
        return rows

    def test_flat_rows_match_only_flagged_original_tokens(self):
        before = deepcopy(self.native)
        report = audit(self.native, input_format=FORMAT)
        self.assertEqual(report['token_evidence_summary'], SUMMARY)
        rows = self.assert_rows(report, output.csv_tokens(report))
        self.assertEqual([r['source_path'] for r in rows], [
            'transcription[0].tokens[0]', 'transcription[0].tokens[1]',
            'transcription[0].tokens[2]', 'transcription[1].tokens[0]'])
        self.assertEqual(rows[1]['text'], '[_EOT_]')
        self.assertEqual(rows[1]['offsets'], '')
        self.assertEqual(rows[2]['text'], ' 世界, "quoted"\n=1+1')
        self.assertEqual(self.native, before)
        self.assertEqual(report['source_document'], before)

    def test_counts_and_header_for_unavailable_empty_and_no_flags(self):
        zero = dict(flagged_segments=0, unavailable_segments=0, empty_segments=0,
                    segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
                    unavailable_segment_indices=[], empty_segment_indices=[], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
                    untimed_token_source_paths=[], segment_token_counts=[])
        cases = [({'transcription': []}, zero),
                 ({'transcription': [self.native['transcription'][2]]}, zero)]
        for tokens, unavailable, empty in ((None, 1, 0), ([], 0, 1)):
            doc = deepcopy(self.native)
            record = doc['transcription'][3]
            if tokens is not None:
                record['tokens'] = tokens
            cases.append(({'transcription': [record]}, dict(flagged_segments=1,
                unavailable_segments=unavailable, empty_segments=empty, segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
                unavailable_segment_indices=[0] if unavailable else [], empty_segment_indices=[0] if empty else [], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
                untimed_token_source_paths=[], segment_token_counts=[])))
        for doc, summary in cases:
            with self.subTest(doc=doc):
                report = audit(doc, input_format=FORMAT)
                self.assertEqual(report['token_evidence_summary'], summary)
                self.assertEqual(output.csv_tokens(report), ','.join(FIELDS) + '\n')
        normalized = audit({'segments': [{'start': 0, 'end': 0, 'text': 'x', 'tokens': []}]})
        self.assertEqual(normalized['token_evidence_summary'], dict(flagged_segments=1,
            unavailable_segments=1, empty_segments=0, segments_with_tokens=0, token_rows=0, timed_token_rows=0, untimed_token_rows=0,
            unavailable_segment_indices=[0], empty_segment_indices=[], untimed_only_segment_indices=[], mixed_timing_segment_indices=[], timed_only_segment_indices=[], timed_token_source_paths=[],
            untimed_token_source_paths=[], segment_token_counts=[]))
        with self.assertRaisesRegex(ValueError, 'whisper.cpp-json-v1.7.6'):
            output.csv_tokens(normalized)

    def test_raw_reversed_and_large_timing_and_nonmutation(self):
        token = self.native['transcription'][0]['tokens'][0]
        token['offsets'] = {'from': 9007199254740991, 'to': 11000}
        token['timestamps'] = {'from': '2501999792:59:00,991', 'to': '00:00:11,000'}
        report = audit(self.native, input_format=FORMAT)
        before = deepcopy(report)
        self.assert_rows(report, output.csv_tokens(report))
        self.assertEqual(report, before)

    def test_bare_cr_and_crlf_text_round_trip_as_one_record(self):
        for text in ('before\rafter', 'before\r\nafter', '\r', ''):
            with self.subTest(text=text):
                self.native['transcription'][0]['tokens'][0]['text'] = text
                report = audit(self.native, input_format=FORMAT)
                self.assert_rows(report, output.csv_tokens(report))

    def test_export_does_not_change_scores_flags_or_existing_sidecars(self):
        report = audit(self.native, input_format=FORMAT)
        before = deepcopy(report)
        prior_csv, prior_srt = output.csv_flags(report), output.review_srt(report)
        output.csv_tokens(report)
        self.assertEqual(report, before)
        self.assertEqual(output.csv_flags(report), prior_csv)
        self.assertEqual(output.review_srt(report), prior_srt)
        self.assertEqual(report['segments'][2]['reasons'], [])
        self.assertTrue(all(f['avg_logprob'] is None for f in report['flags']))
        self.assertTrue(all(f['no_speech_prob'] is None for f in report['flags'][:2]))
        self.assertEqual(report['flags'][0]['reasons'], ['adjacent_repeat'])
        self.assertEqual(report['flags'][2]['no_speech_prob'], .9)
        changed = deepcopy(self.native)
        for segment in changed['transcription']:
            for token in segment.get('tokens', []):
                token['p'] = 1
        other = audit(changed, input_format=FORMAT)
        self.assertEqual(report['segments'], other['segments'])
        self.assertEqual(report['token_evidence_summary'], other['token_evidence_summary'])

    def test_cli_dry_run_sidecars_and_hashes(self):
        vad, config = self.directory / 'vad.json', self.directory / 'config.json'
        vad.write_text('[[0,50]]')
        config.write_text('{"default":{"max_pattern_tokens":2}}')
        snapshots = {p: p.read_bytes() for p in (self.source, vad, config)}
        args = ('--vad', vad, '--loop-config', config, '--outside-speech-fraction', '.2')
        dry = self.run_cli(*args, '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)['token_evidence_summary'], SUMMARY)
        self.assertFalse(any(p.exists() for p in self.paths))
        done = self.run_cli(*args)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, '')
        report = json.loads(self.paths[0].read_bytes())
        self.assert_rows(report, self.paths[2].read_text(encoding='utf-8'))
        self.assertEqual(report['token_evidence_summary'], SUMMARY)
        for path, field in ((self.source, 'source'), (vad, 'vad_source'), (config, 'loop_config_source')):
            self.assertEqual(path.read_bytes(), snapshots[path])
            self.assertEqual(report[field]['sha256'], hashlib.sha256(snapshots[path]).hexdigest())
            self.assertEqual(report[field]['bytes'], len(snapshots[path]))
        flag_rows = list(csv.DictReader(io.StringIO(self.paths[1].read_text())))
        self.assertEqual([json.loads(r['token_evidence']) if r['token_evidence'] else None
                          for r in flag_rows], [f['token_evidence'] for f in report['flags']])
        self.assertEqual(list(self.directory.glob('.asr-audit-*')), [])

    def test_invalid_native_tokens_prevent_all_outputs(self):
        cases = [(None, 'tokens'), ([{'text': 'x', 'id': 1, 'p': '0', 't_dtw': -1}], 'tokens[0].p')]
        token = deepcopy(self.native['transcription'][2]['tokens'][0])
        del token['timestamps']
        cases.append(([token], 'tokens[0].timestamps'))
        token = deepcopy(self.native['transcription'][2]['tokens'][0])
        token['offsets']['to'] += 1
        cases.append(([token], 'tokens[0].offsets.to'))
        for tokens, suffix in cases:
            with self.subTest(suffix=suffix):
                doc = deepcopy(self.native)
                doc['transcription'][2]['tokens'] = tokens  # unflagged tokens validate too
                self.source.write_text(json.dumps(doc), encoding='utf-8')
                done = self.run_cli()
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn('transcription[2].' + suffix, done.stderr)
                self.assertFalse(any(p.exists() for p in self.paths))
        self.source.write_text('{"transcription":[],"transcription":[]}')
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn('duplicate', done.stderr)
        self.assertFalse(any(p.exists() for p in self.paths))

    def test_destinations_existing_duplicate_and_all_input_aliases(self):
        vad, config = self.directory / 'vad.json', self.directory / 'config.json'
        vad.write_text('[[0,50]]')
        config.write_text('{}')
        inputs = (self.source, vad, config)
        before = {p: p.read_bytes() for p in inputs}
        token_path = self.paths[2]
        for source in inputs:
            for kind in ('direct', 'symlink', 'hardlink'):
                with self.subTest(source=source, kind=kind):
                    if kind == 'direct':
                        target = source
                    else:
                        target = self.directory / (source.name + '-' + kind)
                        if kind == 'symlink':
                            target.symlink_to(source)
                        else:
                            target.hardlink_to(source)
                    self.paths[2] = target
                    done = self.run_cli('--vad', vad, '--loop-config', config, '--dry-run')
                    self.assertEqual(done.returncode, 2)
                    self.assertIn('aliases an input', done.stderr)
                    self.assertFalse(any(p.exists() for p in (self.paths[0], self.paths[1], self.paths[3])))
                    if kind != 'direct':
                        target.unlink()
        self.paths[2] = token_path
        token_path.write_text('keep')
        for extra in ((), ('--dry-run',)):
            done = self.run_cli(*extra)
            self.assertEqual(done.returncode, 2)
            self.assertIn('already exists', done.stderr)
            self.assertEqual(token_path.read_text(), 'keep')
        token_path.unlink()
        self.paths[2] = self.paths[0]
        done = self.run_cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn('duplicate output', done.stderr)
        self.assertFalse(any(p.exists() for p in self.paths))
        for p in inputs:
            self.assertEqual(p.read_bytes(), before[p])

    def test_cli_requires_explicit_native_format_even_in_dry_run(self):
        self.source.write_text('{"segments":[]}')
        for extra in ((), ('--dry-run',)):
            done = self.run_cli(*extra, native=False)
            self.assertEqual(done.returncode, 2)
            self.assertIn('--token-csv requires --input-format whisper.cpp-json-v1.7.6', done.stderr)
            self.assertFalse(any(p.exists() for p in self.paths))


if __name__ == '__main__':
    unittest.main()
