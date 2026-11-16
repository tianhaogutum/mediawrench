"""Offline saved-export comparison contracts, independent of source recovery."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chapters import ChapterError, compare, dumps, json_text, loads, read_json

ROOT = Path(__file__).resolve().parents[2]


class CheckExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audit = self.root / 'audit.json'
        self.sidecar = self.root / 'supplied.sidecar'
        self.doc = loads('{"duration":3.000007,"archive":{"score":0.12345678901234567890123456789},'
                         '"chapters":[{"start_time":0.000001,"end_time":1.000002,"title":"开场\\nA",'
                         '"extra":[true,null,{"a/b~c":"保存"}]},'
                         '{"start_time":2.000003,"end_time":2.999999,"title":"End"}]}', 'json')
        self.save()

    def save(self, target='text', doc=None):
        self.export = dumps(self.doc if doc is None else doc, target, allow_lossy=True)
        self.audit.write_text(json_text(self.export.report), encoding='utf-8')
        self.sidecar.write_text(self.export.text, encoding='utf-8')

    def cli(self, code=0):
        from mediawrench.chapters.cli import main
        stdout, stderr = StringIO(), StringIO()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir() if p.is_file()}
        with redirect_stdout(stdout), redirect_stderr(stderr), patch('subprocess.run') as run:
            actual = main(['check-export', str(self.audit), str(self.sidecar)])
        run.assert_not_called()
        self.assertEqual(actual, code, stdout.getvalue() + stderr.getvalue())
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir() if p.is_file()}, before)
        self.assertEqual(set(self.root.iterdir()), set(before))
        self.assertNotIn('Traceback', stderr.getvalue())
        if code == 2:
            self.assertEqual(stdout.getvalue(), '')
            self.assertTrue(stderr.getvalue())
            return stderr.getvalue()
        self.assertEqual(stderr.getvalue(), '')
        result = read_json(stdout.getvalue())
        self.assertEqual(result['kind'], 'exported-sidecar-normalized')
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['equal'], code == 0)
        self.assertEqual(result['status'], 'passed' if code == 0 else 'different')
        return result

    def test_cli_saved_lossy_export_equal_without_source_or_probe(self):
        # Real subprocess regression; set MEDIAWRENCH_CLI to an installed console script to test it.
        command = ([os.environ['MEDIAWRENCH_CLI'], 'chapters'] if 'MEDIAWRENCH_CLI' in os.environ
                   else [sys.executable, '-m', 'mediawrench.chapters'])
        environment = {**os.environ, 'PATH': ''}
        environment.pop('PYTHONPATH', None)
        if 'MEDIAWRENCH_CLI' not in os.environ:
            environment['PYTHONPATH'] = str(ROOT / 'src')
        result = subprocess.run([*command, 'check-export', str(self.audit), str(self.sidecar)],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        saved = self.export.report
        self.assertFalse(saved['comparison']['equal'])
        report = read_json(result.stdout)
        self.assertTrue(report['equal'])
        self.assertEqual(report['comparison']['left'], saved['comparison']['right'])
        self.assertEqual(report['comparison']['right'], saved['comparison']['right'])
        self.assertEqual(report['comparison']['differences'], [])

    def test_library_and_cli_lossless_and_approved_lossy_targets(self):
        from mediawrench.chapters import check_export
        xml_doc = loads((ROOT / 'examples/chapters/hidden-chapters.matroska.xml').read_text(), 'matroska')
        for target, doc in [('json', self.doc), ('text', self.doc), ('ffmetadata', self.doc),
                            ('matroska', xml_doc), ('ffmetadata', xml_doc)]:
            with self.subTest(target=target):
                self.save(target, doc)
                result = self.cli()
                self.assertEqual(result, check_export(read_json(self.audit.read_text()), self.export.text))
                self.assertEqual(result['target_format'], target)
                self.assertEqual(result['comparison']['left'], self.export.report['comparison']['right'])
                if target in ('text', 'ffmetadata'):
                    self.assertFalse(self.export.report['comparison']['equal'])

    def test_precise_title_time_metadata_duration_and_count_differences(self):
        self.save('json')
        expected = loads(self.export.text, 'json')
        mutations = [('/chapters/0/title', lambda d: setattr(d.chapters[0], 'title', 'Edited')),
                     ('/chapters/0/start_us', lambda d: setattr(d.chapters[0], 'start_us', 2)),
                     ('/chapters/0/end_us', lambda d: setattr(d.chapters[0], 'end_us', 1_000_003)),
                     ('/metadata/archive/score', lambda d: d.metadata['archive'].update(score=Decimal('0.1'))),
                     ('/chapters/0/metadata/extra/2/a~1b~0c', lambda d: d.chapters[0].metadata['extra'][2].update({'a/b~c':'edit'})),
                     ('/duration_us', lambda d: setattr(d, 'duration_us', 4_000_000)),
                     ('/chapters', lambda d: d.chapters.pop())]
        for path, mutate in mutations:
            with self.subTest(path=path):
                actual = deepcopy(expected)
                mutate(actual)
                self.sidecar.write_text(dumps(actual, 'json').text, encoding='utf-8')
                result = self.cli(1)
                self.assertEqual(result['comparison'], compare(expected, actual))
                self.assertEqual(result['comparison']['differences'][0]['path'], path)

    def test_later_lossy_export_edits_detected(self):
        for target in ('text', 'ffmetadata'):
            with self.subTest(target=target):
                self.save(target)
                self.sidecar.write_text(self.export.text.replace('End', 'Edited'), encoding='utf-8')
                self.assertEqual(self.cli(1)['comparison']['differences'][0]['path'], '/chapters/1/title')
        self.save('text')
        self.sidecar.write_text(self.export.text.replace('00:00:02.000003', '00:00:02.000004'), encoding='utf-8')
        self.assertEqual({d['path'] for d in self.cli(1)['comparison']['differences']},
                         {'/chapters/0/end_us', '/chapters/1/start_us'})

    def test_saved_boundary_reconstructs_text_not_source_final_end(self):
        result = self.cli()
        actual = result['comparison']['right']
        self.assertEqual(actual['duration_us'], 3_000_007)
        self.assertEqual(actual['chapters'][-1]['end_us'], 3_000_007)
        self.assertEqual(self.doc.chapters[-1].end_us, 2_999_999)
        self.assertEqual(actual['chapters'][0]['end_us'], 2_000_003)

    def test_ignores_recorded_paths_source_and_old_diagnostics(self):
        saved = self.export.report
        for key in ('source', 'losses', 'lossless'):
            saved[key] = 'not consulted'
        saved['comparison']['left'] = 'not consulted'
        saved['comparison']['equal'] = False
        saved.update(output='/missing/recorded-export', media='/missing/recorded-media')
        self.audit.write_text(json_text(saved), encoding='utf-8')
        self.cli()
        del saved['source']
        self.audit.write_text(json_text(saved), encoding='utf-8')
        self.cli()

    def test_normalized_equality_ignores_bytes_comments_line_endings(self):
        self.save('ffmetadata')
        self.sidecar.write_bytes(self.export.text.replace(';FFMETADATA1\n', ';FFMETADATA1\n;comment\n')
                                 .replace('\n', '\r\n').encode('utf-8'))
        self.cli()
        self.save('json')
        self.sidecar.write_text(self.export.text.replace('  ', '\t'), encoding='utf-8')
        self.cli()

    def test_malformed_schema_target_comparison_and_expected_fields(self):
        valid = self.export.report
        cases = [None, [], {}, *[dict(valid, schema_version=v) for v in (True, Decimal('1.0'), '1', 0, 2, None)],
                 *[dict(valid, target_format=v) for v in (None, 'auto', {}, [], True)],
                 *[dict(valid, comparison=v) for v in (None, [], 'bad', {}, {'right':None}, {'right':[]})]]
        expected = valid['comparison']['right']
        for key in ('duration_us', 'metadata', 'chapters'):
            bad = deepcopy(expected); del bad[key]
            cases.append(dict(valid, comparison={'right':bad}))
        for key in ('start_us', 'end_us', 'title', 'metadata'):
            bad = deepcopy(expected); del bad['chapters'][0][key]
            cases.append(dict(valid, comparison={'right':bad}))
        for bad in cases:
            with self.subTest(bad=bad):
                self.audit.write_text(json_text(bad), encoding='utf-8')
                self.cli(2)

    def test_invalid_expected_models(self):
        valid = self.export.report
        changes = [('duration_us', True), ('duration_us', Decimal('3.0')), ('duration_us', '3000007'),
                   ('duration_us', -1), ('duration_us', 2**63), ('metadata', []), ('chapters', []),
                   ('chapters', {}), ('chapters', [None])]
        for key,value in changes:
            bad=deepcopy(valid); bad['comparison']['right'][key]=value
            self.audit.write_text(json_text(bad), encoding='utf-8'); self.cli(2)
        for key,value in [('start_us',False),('start_us',-1),('start_us',3_000_008),
                          ('end_us',0),('end_us',2**63),('title',None),('title','\x00'),
                          ('title','\ud800'),('metadata',[])]:
            bad=deepcopy(valid); bad['comparison']['right']['chapters'][0][key]=value
            if key == 'title' and value == '\ud800':
                # Encode the deliberate invalid Unicode as an ASCII JSON escape.
                data = json.loads(json_text(valid))
                data['comparison']['right']['chapters'][0]['title'] = value
                self.audit.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
            else:
                self.audit.write_text(json_text(bad), encoding='utf-8')
            self.cli(2)
        for start,end in [(0,1), (1_000_000,2_100_000)]:
            bad=deepcopy(valid)
            bad['comparison']['right']['chapters'][1].update(start_us=start,end_us=end)
            self.audit.write_text(json_text(bad), encoding='utf-8'); self.cli(2)

    def test_readback_duration_required_exact_numeric_consistent(self):
        valid = self.export.report
        for value in [None, True, '3.000007', [], {}, -1, Decimal('0.0000001'), Decimal('3.000008'), 2**63]:
            bad = dict(valid, readback_duration_seconds=value)
            self.audit.write_text(json_text(bad), encoding='utf-8'); self.cli(2)
        bad = dict(valid); del bad['readback_duration_seconds']
        self.audit.write_text(json_text(bad), encoding='utf-8'); self.cli(2)
        self.save('json')
        bad = dict(self.export.report, readback_duration_seconds=3)
        self.audit.write_text(json_text(bad), encoding='utf-8'); self.cli(2)

    def test_strict_audit_json(self):
        valid = json_text(self.export.report).encode()
        for raw in [b'not json',b'\xff', valid.replace(b'"schema_version": 1',b'"schema_version": 1, "schema_version": 1'),
                    valid.replace(b'"schema_version": 1',b'"schema_version": 1e0'),
                    valid.replace(b'"schema_version": 1',b'"schema_version": NaN'),
                    valid.replace(b'"schema_version": 1',b'"schema_version": Infinity'),
                    b'['*1100+b']'*1100]:
            self.audit.write_bytes(raw); self.cli(2)

    def test_deleted_directory_and_invalid_utf8_inputs(self):
        self.sidecar.unlink(); self.cli(2)
        self.sidecar.write_bytes(b'\xff'); self.cli(2)
        self.sidecar.unlink(); self.sidecar.mkdir()
        # Directory itself must not be written; helper snapshots files only.
        from mediawrench.chapters.cli import main
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()):
            self.assertEqual(main(['check-export',str(self.audit),str(self.sidecar)]),2)
        self.assertEqual(stdout.getvalue(),'')
        self.sidecar.rmdir(); self.save(); self.audit.unlink(); self.cli(2)

    def test_malformed_and_unsupported_sidecars(self):
        for target, raw in [('text','not chapters'), ('text','00:00:04\tBeyond saved boundary\n'),
                            ('json','{"chapters":[]}'), ('ffmetadata',';FFMETADATA1\n[STREAM]\n'),
                            ('matroska','<Chapters><EditionEntry/><EditionEntry/></Chapters>')]:
            self.save(target, loads((ROOT/'examples/chapters/hidden-chapters.matroska.xml').read_text(),'matroska') if target=='matroska' else None)
            self.sidecar.write_text(raw, encoding='utf-8'); self.cli(2)

    def test_read_permission_errors_are_reported_without_stdout(self):
        from mediawrench.chapters.cli import main
        for path in (self.audit, self.sidecar):
            original = Path.open
            def deny(p, *args, **kwargs):
                if p == path:
                    raise PermissionError('permission denied')
                return original(p, *args, **kwargs)
            with patch.object(Path, 'open', deny), redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main(['check-export',str(self.audit),str(self.sidecar)]),2)
            self.assertEqual(stdout.getvalue(),''); self.assertIn('permission denied',stderr.getvalue())

    def test_library_rejects_invalid_text_and_uses_shared_validation(self):
        from mediawrench.chapters import check_export
        for value in (None, b'00:00:00 A', 1):
            with self.assertRaises(ChapterError): check_export(self.export.report, value)
        bad = deepcopy(self.export.report)
        bad['comparison']['right']['metadata'] = {'bad':float('nan')}
        with self.assertRaises(ChapterError): check_export(bad, self.export.text)


if __name__ == '__main__':
    unittest.main()
