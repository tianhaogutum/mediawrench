import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.cutlist import (CutlistError, build_timeline, load_plan, output_paths,
                                read_srt, render_outputs, write_outputs)


ROOT = Path(__file__).resolve().parents[2]
SRT = ROOT / "examples/cutlist/interview.srt"
PLAN = ROOT / "examples/cutlist/selection.json"


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.paths = output_paths(self.directory / "out")
        self.timeline = build_timeline(read_srt(SRT), load_plan(PLAN))
        self.contents = render_outputs(self.timeline, "interview.wav")

    def test_real_llc_v2_csv_and_provenance_roundtrip(self):
        write_outputs(self.paths, self.contents)
        llc = json.loads(self.paths['llc'].read_text(encoding='utf-8'))
        with self.paths['csv'].open(encoding='utf-8', newline='') as handle:
            rows = list(csv.reader(handle))
        mapping = json.loads(self.paths['provenance'].read_text(encoding='utf-8'))
        self.assertEqual(set(llc), {'version', 'mediaFileName', 'cutSegments'})
        self.assertEqual(llc['version'], 2)
        self.assertEqual(llc['mediaFileName'], 'interview.wav')
        self.assertTrue(all(set(cut) == {'start', 'end', 'name'} for cut in llc['cutSegments']))
        segments = [(cut['start'], cut['end'], cut['name']) for cut in llc['cutSegments']]
        self.assertEqual([(float(a), float(b), name) for a, b, name in rows], segments)
        self.assertEqual([(cut['start'], cut['end'], cut['name']) for cut in mapping['cuts']], segments)
        self.assertEqual([(a, b) for a, b, _ in segments], [(0, 3.5), (4.5, 6.1), (8.9, 10)])
        self.assertEqual([cut['cueIds'] for cut in mapping['cuts']], [[1, 2], [4], [6]])
        self.assertEqual(mapping['selectionPlan'], load_plan(PLAN))
        self.assertEqual(mapping['normalizedSelection']['effectiveKeep'], [1, 2, 4, 6])
        self.assertIn('你好', mapping['cues'][0]['text'])
        self.assertEqual(rows[0][2], '开场 / Answer, first')
        self.assertEqual(mapping['cuts'][0]['contributions'][1]['retainedIntervalMs'], [1700, 3500])
        self.assertEqual(len(list(self.directory.iterdir())), 3)

    def test_existing_output_does_not_change_any_file(self):
        self.paths['csv'].write_text('existing', encoding='utf-8')
        with self.assertRaises(CutlistError):
            write_outputs(self.paths, self.contents)
        self.assertEqual(self.paths['csv'].read_text(), 'existing')
        self.assertEqual(list(self.directory.iterdir()), [self.paths['csv']])

    def test_output_path_alias_collision(self):
        paths = {**self.paths, 'csv': self.directory / '.' / 'out.llc'}
        with self.assertRaisesRegex(CutlistError, 'collision'):
            write_outputs(paths, self.contents)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_dangling_symlink_never_followed(self):
        self.paths['csv'].symlink_to(self.directory / 'absent')
        with self.assertRaises(CutlistError):
            write_outputs(self.paths, self.contents)
        self.assertTrue(self.paths['csv'].is_symlink())
        self.assertFalse((self.directory / 'absent').exists())

    def test_protected_input_and_media_paths(self):
        with self.assertRaisesRegex(CutlistError, 'input/media'):
            write_outputs(self.paths, self.contents, protected=(self.paths['llc'],))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_output_directory_is_not_created(self):
        with self.assertRaises(CutlistError):
            write_outputs(output_paths(self.directory / 'missing' / 'out'), self.contents)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stage_failure_writes_no_outputs(self):
        with patch('mediawrench.cutlist.export.os.fsync', side_effect=OSError('injected full disk')):
            with self.assertRaises(OSError):
                write_outputs(self.paths, self.contents)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_publish_failure_rolls_back_and_each_visible_file_is_complete(self):
        real_link = os.link
        calls = []

        def failing_link(source, target):
            if calls:
                raise OSError('injected publication failure')
            real_link(source, target)
            self.assertEqual(Path(target).read_text(encoding='utf-8'), self.contents['llc'])
            calls.append(target)

        with patch('mediawrench.cutlist.export.os.link', side_effect=failing_link):
            with self.assertRaises(OSError):
                write_outputs(self.paths, self.contents)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_racing_file_is_not_overwritten_and_previous_publications_rollback(self):
        real_link = os.link

        def race(source, target):
            if Path(target) == self.paths['csv']:
                Path(target).write_text('racing writer', encoding='utf-8')
            real_link(source, target)

        with patch('mediawrench.cutlist.export.os.link', side_effect=race):
            with self.assertRaises(FileExistsError):
                write_outputs(self.paths, self.contents)
        self.assertEqual(self.paths['csv'].read_text(), 'racing writer')
        self.assertEqual(list(self.directory.iterdir()), [self.paths['csv']])

    def test_invalid_output_keys_and_contents(self):
        for paths, contents in [({'llc': self.paths['llc']}, self.contents),
                                (self.paths, {'llc': 'partial'})]:
            with self.subTest(paths=paths), self.assertRaises(CutlistError):
                write_outputs(paths, contents)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_media_reference_rejects_paths_and_controls(self):
        for media in ['', '.', '..', '../media.wav', '/media.wav', 'C:\\media.wav', 'media\n.wav']:
            with self.subTest(media=media), self.assertRaises(CutlistError):
                render_outputs(self.timeline, media)

    def test_large_cut_time_never_silently_rounds(self):
        from mediawrench.cutlist import Cut, Timeline
        timeline = Timeline((), (Cut(0, 9007199254740989, (1,), 'large'),), '{}', (1,), (), 0, 9007199254740990)
        with self.assertRaisesRegex(CutlistError, 'rounding'):
            render_outputs(timeline, 'a.wav')


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.prefix = self.directory / 'interview-proj'

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', *map(str, args)],
                              capture_output=True, text=True, encoding='utf-8', cwd=ROOT, timeout=120)

    def export(self, *extra, plan=PLAN, media='interview.wav'):
        return self.run_cli(SRT, '--plan', plan, '--media-file', media,
                            '--output-prefix', self.prefix, *extra)

    def test_real_cli_e2e(self):
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['dryRun'])
        self.assertEqual(len(report['cuts']), 3)
        self.assertTrue(all(Path(path).is_file() for path in report['outputPaths'].values()))
        mapping = json.loads(output_paths(self.prefix)['provenance'].read_text(encoding='utf-8'))
        self.assertEqual(len(mapping['source']['srtSha256']), 64)
        self.assertEqual(mapping['source']['planFileName'], 'selection.json')
        self.assertEqual(report['cuts'], json.loads(output_paths(self.prefix)['llc'].read_text())['cutSegments'])

    def test_dry_run_validates_and_does_not_write(self):
        result = self.export('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['dryRun'])
        self.assertEqual(len(report['outputPaths']), 3)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_query_ambiguous_never_selects(self):
        result = self.run_cli(SRT, '--query', 'Keep this answer.')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([cue['id'] for cue in report['candidates']], [2, 4])
        self.assertIs(report['selected'], False)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_query_is_literal_case_sensitive_including_no_matches(self):
        for query, ids in [('你好', [1]), ('.*', []), ('keep this answer.', [])]:
            result = self.run_cli(SRT, '--query', query)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([cue['id'] for cue in json.loads(result.stdout)['candidates']], ids)

    def test_query_cannot_be_combined_with_export_or_empty(self):
        for args in [('--query', ''), ('--query', 'Keep', '--plan', PLAN),
                     ('--query', 'Keep', '--dry-run')]:
            result = self.run_cli(SRT, *args)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_or_unsupported_inputs_nonzero(self):
        for args in [(SRT,), (self.directory / 'absent.srt',), (SRT, '--unknown')]:
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(result.stderr)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_unknown_cue_and_unsupported_plan_nonzero_no_outputs(self):
        path = self.directory / 'bad.json'
        for plan in [{'keep': [999], 'duration': 10}, {'keep': ['1-3'], 'duration': 10},
                     {'keep': [], 'duration': 10}, {'keep': [1], 'duration': 9},
                     {'keep': [1], 'duration': 10, 'shell': 'anything'}]:
            path.write_text(json.dumps(plan))
            result = self.export(plan=path)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(output_paths(self.prefix)['llc'].exists())
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_repeat_export_and_dry_run_collisions_nonzero_no_overwrite(self):
        self.assertEqual(self.export().returncode, 0)
        before = {path: path.read_bytes() for path in self.directory.iterdir()}
        for args in [(), ('--dry-run',)]:
            result = self.export(*args)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(before, {path: path.read_bytes() for path in self.directory.iterdir()})

    def test_media_output_collision_nonzero(self):
        result = self.export(media='interview-proj.llc')
        self.assertEqual(result.returncode, 2)
        self.assertIn('input/media', result.stderr)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_media_reference_nonzero(self):
        result = self.export(media='../elsewhere.wav')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
