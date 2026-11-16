"""Read-only companion comparisons use saved authoritative cut boundaries."""

import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.cutlist import Cue, CutlistError, build_timeline, render_outputs

ROOT = Path(__file__).resolve().parents[2]


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {k: self.root / ('saved.' + ext) for k, ext in
                      [('provenance', 'json'), ('llc', 'llc'), ('csv', 'csv')]}
        self.export()

    def export(self, rate=None):
        plan = {'keep': [1, 2], 'duration': 2.5,
                'labels': {'1': 'α, "quoted"', '2': '尾'}}
        if rate: plan['frameRate'] = rate
        docs = render_outputs(build_timeline((Cue(1, 1, 100, 900, 'one'),
                                             Cue(2, 2, 1300, 2400, 'two')), plan), 'missing.mp4')
        # The exporter disallows control characters in plan labels; the verifier also
        # accepts valid companion CSV labels containing quoted newlines.
        for key in ('llc', 'provenance'):
            doc = json.loads(docs[key]); doc['cuts' if key == 'provenance' else 'cutSegments'][0]['name'] += '\nnext'
            docs[key] = json.dumps(doc, ensure_ascii=False)
        rows = list(csv.reader(io.StringIO(docs['csv'], newline='')))
        rows[0][2] += '\nnext'
        stream = io.StringIO(newline=''); csv.writer(stream, lineterminator='\n').writerows(rows)
        docs['csv'] = stream.getvalue()
        for key, text in docs.items(): self.paths[key].write_text(text, encoding='utf-8', newline='')
        return json.loads(docs['provenance'])

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                for p in self.root.iterdir()}

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-companions',
                               str(self.paths['provenance']), '--llc', str(self.paths['llc']),
                               '--csv', str(self.paths['csv']), *extra], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)

    def verify(self):
        from mediawrench.cutlist import verify_companions
        return verify_companions(self.paths['provenance'], llc_path=self.paths['llc'],
                                 csv_path=self.paths['csv'])

    def edit_json(self, key, change):
        doc = json.loads(self.paths[key].read_text()); change(doc)
        self.paths[key].write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')

    def test_cli_edited_companion_is_mismatch_and_read_only(self):
        self.edit_json('llc', lambda d: d['cutSegments'][0].update(name='edited'))
        before = self.snapshot()
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['status'], report['matches'], report['replayed']), ('mismatch', False, False))
        self.assertEqual(report['issues'], [{'code': 'cut-name', 'file': 'llc',
                         'path': str(self.paths['llc']), 'field': 'name', 'cutIndex': 1,
                         'expected': 'α, "quoted"\nnext', 'actual': 'edited'}])
        self.assertEqual(self.snapshot(), before)

    def test_plain_and_both_rational_grids_match_without_execution_or_source_access(self):
        for rate in (None, '24', '30000/1001'):
            with self.subTest(rate=rate):
                self.export(rate)
                before = self.snapshot()
                with patch('subprocess.run', side_effect=AssertionError('unexpected execution')):
                    report = self.verify()
                self.assertEqual((report['status'], report['matches'], report['issues']), ('matched', True, []))
                self.assertEqual(report['files']['csv']['mediaReference'], 'not-represented')
                self.assertEqual(report['files']['llc']['cutCount'], 2)
                self.assertEqual(self.snapshot(), before)
                result = self.cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), report)

    def test_each_represented_field_has_expected_actual_and_file_cut_location(self):
        for kind in ('llc', 'csv'):
            for field in ('start', 'end', 'name', 'count', 'order', 'media'):
                if kind == 'csv' and field == 'media': continue
                with self.subTest(kind=kind, field=field):
                    self.export()
                    if kind == 'llc':
                        doc = json.loads(self.paths[kind].read_text()); rows = doc['cutSegments']
                        if field == 'media': doc['mediaFileName'] = 'changed.mp4'
                        elif field == 'count': rows.pop()
                        elif field == 'order': rows.reverse()
                        else: rows[0][field] = {'start': .101, 'end': .899, 'name': 'edited'}[field]
                        self.paths[kind].write_text(json.dumps(doc))
                    else:
                        rows = list(csv.reader(io.StringIO(self.paths[kind].read_text(), newline='')))
                        if field == 'count': rows.pop()
                        elif field == 'order': rows.reverse()
                        else: rows[0][{'start': 0, 'end': 1, 'name': 2}[field]] = {
                            'start': '.101', 'end': '.899', 'name': 'edited'}[field]
                        with self.paths[kind].open('w', newline='') as out: csv.writer(out).writerows(rows)
                    before = self.snapshot(); report = self.verify()
                    issues = report['issues']
                    self.assertTrue(issues); self.assertFalse(report['matches'])
                    self.assertTrue(all(i['file'] == kind and i['path'] == str(self.paths[kind])
                                        and 'expected' in i and 'actual' in i for i in issues))
                    code = 'media-file-name' if field == 'media' else 'cut-' + ('count' if field == 'count' else 'start' if field == 'order' else field)
                    self.assertEqual(issues[0]['code'], code)
                    if field not in ('media', 'count'): self.assertEqual(issues[0]['cutIndex'], 1)
                    self.assertEqual(self.snapshot(), before)

    def test_frame_precision_policy_and_exact_millisecond_comparison(self):
        from fractions import Fraction
        for rate in ('24', '30000/1001'):
            doc = self.export(rate)
            exact = Fraction(doc['cuts'][0]['startExactSeconds']['numerator'],
                             doc['cuts'][0]['startExactSeconds']['denominator'])
            for delta, matches in [('0.0000000005', True), ('0.000000002', False)]:
                # Use a decimal spelling distinct from the exporter but on the same frame.
                from decimal import Decimal, localcontext
                with localcontext() as context:
                    context.prec = 60
                    value = Decimal(exact.numerator) / Decimal(exact.denominator) + Decimal(delta)
                self.edit_json('llc', lambda d: d['cutSegments'][0].update(start=float(value)))
                report = self.verify()
                self.assertEqual(report['matches'], matches)
                if not matches:
                    self.assertEqual(report['issues'][0]['expected'], doc['cuts'][0]['startExactSeconds'])
                    self.assertEqual(report['issues'][0]['code'], 'cut-start')
        self.export()
        text = self.paths['llc'].read_text().replace('"start": 0.1', '"start": 0.100000000000000001', 1)
        self.paths['llc'].write_text(text)
        self.assertEqual(self.verify()['issues'][0]['actual'], '0.100000000000000001')

    def test_cached_provenance_seconds_are_not_authoritative(self):
        for rate in (None, '30000/1001'):
            self.export(rate)
            self.edit_json('provenance', lambda d: d['cuts'][0].update(start=100, end=200))
            self.assertTrue(self.verify()['matches'])

    def test_consistently_edited_names_are_not_authenticated(self):
        self.edit_json('provenance', lambda d: d['cuts'][0].update(name='edited'))
        self.edit_json('llc', lambda d: d['cutSegments'][0].update(name='edited'))
        rows = list(csv.reader(io.StringIO(self.paths['csv'].read_text(), newline='')))
        rows[0][2] = 'edited'
        with self.paths['csv'].open('w', newline='') as out: csv.writer(out).writerows(rows)
        self.assertTrue(self.verify()['matches'])

    def test_malformed_json_and_unsupported_versions_fail_without_execution(self):
        for kind in ('provenance', 'llc'):
            for text in ('{', '[]', '{"x":1,"x":2}', '{"unused":NaN}',
                         '{"unused":Infinity}', '{"unused":1e999}'):
                with self.subTest(kind=kind, text=text):
                    self.export(); self.paths[kind].write_text(text)
                    before = self.snapshot()
                    with patch('subprocess.run', side_effect=AssertionError('execution')):
                        with self.assertRaises(CutlistError): self.verify()
                    result = self.cli()
                    self.assertEqual((result.returncode, result.stdout), (2, ''))
                    self.assertTrue(result.stderr); self.assertEqual(self.snapshot(), before)
        for kind, key, versions in [('provenance', 'schemaVersion', [0, 3, True, '1', 1.0]),
                                    ('llc', 'version', [1, 3, True, '2', 2.0])]:
            for version in versions:
                self.export(); self.edit_json(kind, lambda d: d.update({key: version}))
                with self.assertRaises(CutlistError): self.verify()

    def test_invalid_authoritative_millisecond_fields_fail_before_mismatch(self):
        mutations = [lambda d: d['cuts'][0].update(startMs=True),
                     lambda d: d['cuts'][0].update(startMs=-1),
                     lambda d: d['cuts'][0].update(endMs=0),
                     lambda d: d['cuts'][0].update(endMs=3000),
                     lambda d: d['cuts'][0].update(startMs=.1),
                     lambda d: d['cuts'][0].pop('startMs'),
                     lambda d: d['cuts'][1].update(startMs=500),
                     lambda d: d['cuts'][1].update(cutId=1),
                     lambda d: d['cuts'][0].update(name=None),
                     lambda d: d.update(cuts=[]),
                     lambda d: d['normalizedSelection'].update(durationMs=False),
                     lambda d: d.update(mediaFileName='../missing.mp4')]
        for mutate in mutations:
            self.export(); self.edit_json('provenance', mutate)
            self.edit_json('llc', lambda d: d.update(mediaFileName='other.mp4'))
            with self.assertRaises(CutlistError): self.verify()

    def test_invalid_authoritative_frame_fields_and_policy_fail(self):
        mutations = [lambda d: d['quantization'].update(frameRate={'numerator': 0, 'denominator': 1}),
                     lambda d: d['quantization'].update(frameRate={'numerator': 30000, 'denominator': 0}),
                     lambda d: d['quantization'].update(frameRate={'numerator': True, 'denominator': 1}),
                     lambda d: d['quantization'].update(frameRate={'numerator': 10**9+1, 'denominator': 1}),
                     lambda d: d['quantization'].update(timeBase={'numerator': 1, 'denominator': 24}),
                     lambda d: d['quantization'].update(originSeconds=1),
                     lambda d: d['quantization'].update(originSeconds=False),
                     lambda d: d['quantization'].update(durationFrameFloor=73),
                     lambda d: d['quantization'].update(durationFrameFloor=75),
                     lambda d: d['quantization'].update(secondsSerializationMaxErrorInclusive={'numerator': 1, 'denominator': 100}),
                     lambda d: d['cuts'][0].update(startFrame=True),
                     lambda d: d['cuts'][0].update(endFrame=2**53),
                     lambda d: d['cuts'][0].pop('startExactSeconds'),
                     lambda d: d['cuts'][0].update(startExactSeconds={'numerator': 1, 'denominator': 2}),
                     lambda d: d['cuts'][0].update(startExactSeconds={'numerator': 2002, 'denominator': 20000})]
        for mutate in mutations:
            self.export('30000/1001'); self.edit_json('provenance', mutate)
            before = self.snapshot()
            with self.assertRaises(CutlistError): self.verify()
            self.assertEqual(self.snapshot(), before)

    def test_malformed_csv_and_llc_fields_are_invalid_not_mismatches(self):
        invalid_csv = ['start,end,name\n', '0,1\n', '0,1,n,extra\n', '\n',
                       '0,1,"unterminated', '0,1,stray"quote\n', '0,1,"name"junk\n',
                       'NaN,1,n\n', '0,Infinity,n\n', '0,1e999,n\n', '-1,1,n\n',
                       '1,0,n\n', '0, 1,n\n', '0x0,1,n\n']
        for text in invalid_csv:
            self.export(); self.paths['csv'].write_text(text)
            before = self.snapshot()
            with self.assertRaises(CutlistError): self.verify()
            result = self.cli()
            self.assertEqual((result.returncode, result.stdout), (2, ''), text)
            self.assertEqual(self.snapshot(), before)
        for mutate in (lambda d: d['cutSegments'][0].update(start='0.1'),
                       lambda d: d['cutSegments'][0].update(end=False),
                       lambda d: d['cutSegments'][0].pop('name'),
                       lambda d: d.update(cutSegments=None), lambda d: d.pop('mediaFileName')):
            self.export(); self.edit_json('llc', mutate)
            with self.assertRaises(CutlistError): self.verify()
        # Complete validation takes precedence over an otherwise reportable LLC mismatch.
        self.export(); self.edit_json('llc', lambda d: d.update(mediaFileName='other.mp4'))
        self.paths['csv'].write_text('bad')
        with self.assertRaises(CutlistError): self.verify()

    def test_invalid_utf8_inputs_are_malformed_not_raw_decode_errors(self):
        for kind, data in [('csv', b'0,1,\xff\n'),
                           ('llc', b'{"version":2,"mediaFileName":"a","cutSegments":[\xff]}'),
                           ('provenance', b'{"schemaVersion":\xff}')]:
            with self.subTest(kind=kind):
                self.export(); self.paths[kind].write_bytes(data)
                before = self.snapshot()
                with self.assertRaises(CutlistError): self.verify()
                result = self.cli()
                self.assertEqual((result.returncode, result.stdout), (2, ''))
                self.assertEqual(self.snapshot(), before)

    def test_empty_companions_are_count_mismatches_and_all_issues_are_retained(self):
        self.edit_json('llc', lambda d: d.update(cutSegments=[], mediaFileName='other.mp4'))
        self.paths['csv'].write_text('')
        self.assertEqual([(i['file'], i['code']) for i in self.verify()['issues']],
                         [('llc', 'media-file-name'), ('llc', 'cut-count'), ('csv', 'cut-count')])

    def test_explicit_paths_required_before_file_access_and_cli_rejects_probe_options(self):
        from mediawrench.cutlist import verify_companions
        with patch('pathlib.Path.open', side_effect=AssertionError('file access')):
            with self.assertRaises(CutlistError): verify_companions('missing', llc_path=None, csv_path='x')
            with self.assertRaises(CutlistError): verify_companions('missing', llc_path='x', csv_path=None)
        for extra in (['--ffprobe', 'missing'], ['--preflight-only']):
            self.assertEqual((result := self.cli(*extra)).returncode, 2)
            self.assertEqual(result.stdout, '')
        for flags in ([], ['--llc', str(self.paths['llc'])], ['--csv', str(self.paths['csv'])]):
            result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', 'verify-companions',
                                     str(self.paths['provenance']), *flags], cwd=ROOT,
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.paths['csv'].unlink()
        with self.assertRaises(CutlistError): self.verify()
        self.paths['csv'].mkdir()
        with self.assertRaises(CutlistError): self.verify()

    def test_bom_crlf_and_multiline_names_preserved_exactly(self):
        for key in self.paths:
            text = self.paths[key].read_text()
            if key == 'csv':
                # CRLF row separators, embedded LF label unchanged.
                rows = list(csv.reader(io.StringIO(text, newline='')))
                stream = io.StringIO(newline=''); csv.writer(stream, lineterminator='\r\n').writerows(rows)
                text = stream.getvalue()
            self.paths[key].write_bytes(b'\xef\xbb\xbf' + text.encode())
        self.assertTrue(self.verify()['matches'])
