"""Independent presentation formats preserve mandatory JSON and local results."""

from contextlib import redirect_stderr, redirect_stdout
import html
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pysubs2

from mediawrench.speakers import BridgeError, merge, parse_rttm, run_manifest
from mediawrench.speakers.cli import main
from mediawrench.speakers.export import render_markdown, write_atomic

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / 'examples' / 'speakers' / 'batch'


class ManifestPresentationTests(unittest.TestCase):
    def save(self, root, entries):
        path = root / 'manifest.json'
        path.write_text(json.dumps(dict(schema_version=1, entries=entries)), encoding='utf-8')
        return path

    def entry(self, recording='room-a', channel=1, exports=None, **extra):
        return dict(recording=recording, channel=channel, asr=str(BATCH / 'room-a.json'),
                    rttm=str(BATCH / 'rooms.rttm'),
                    exports=['markdown', 'srt'] if exports is None else exports, **extra)

    def cli(self, path, output, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.speakers', '--manifest',
                               str(path), '--output-dir', str(output), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_overflow_retains_markdown_json_and_later_srt_in_either_request_order(self):
        source = json.loads((BATCH / 'manifest-presentations.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for order in (['srt', 'markdown'], ['markdown', 'srt']):
                with self.subTest(order=order):
                    entries = json.loads(json.dumps(source['entries']))
                    for entry in entries:
                        entry['exports'] = order
                        for key in ('asr', 'rttm'):
                            entry[key] = str(BATCH / entry[key])
                    path = self.save(root, entries)
                    output = root / order[0]
                    summary = run_manifest(path, output)
                    self.assertEqual(summary, json.loads((output / 'summary.json').read_text()))
                    self.assertEqual(summary['status'], 'partial')
                    self.assertTrue(summary['partial_results'])
                    self.assertEqual(summary['counts'], dict(total=3, succeeded=2, failed=1, items=6, turns=6))
                    late = summary['entries'][0]
                    self.assertEqual(late['status'], 'partial')
                    self.assertEqual(late['exports']['srt'], dict(status='error', file=None))
                    self.assertEqual(late['export_errors'], dict(srt=late['error']))
                    self.assertEqual(late['error']['stage'], 'export:srt')
                    self.assertIn('entries[0] (after-limit/1) export:srt:', late['error']['message'])
                    self.assertIn('99:59:59.999', late['error']['message'])
                    self.assertFalse((output / 'entry-0000.srt').exists())
                    self.assertIn('- [360000.000–360001.000] **Late** (word): Late',
                                  (output / 'entry-0000.md').read_text())
                    for entry in summary['entries']:
                        result = json.loads((output / entry['result']).read_text())
                        asr = json.loads(Path(entry['asr']).read_text())
                        self.assertEqual(result, merge(asr, parse_rttm(Path(entry['rttm']).read_text(),
                                                                      entry['recording'], entry['channel']),
                                                       **entry['options']))
                        self.assertEqual(result['source_asr'], asr)
                        filename = f"entry-{entry['index']:04d}.md"
                        self.assertEqual(entry['exports']['markdown'], dict(status='ok', file=filename))
                        self.assertEqual((output / filename).read_text(), render_markdown(result))
                    expected = [[(0, 1000, 'A: First'), (1000, 3000, 'AMBIGUOUS: boundary'),
                                 (3000, 4000, 'B: last'), (4000, 4001, 'UNKNOWN: point')],
                                [(0, 1000, 'Bob: Goodbye')]]
                    for index, cues in enumerate(expected, 1):
                        actual = pysubs2.load(str(output / f'entry-{index:04d}.srt'), format_='srt',
                                              keep_html_tags=True)
                        self.assertEqual([(e.start, e.end, html.unescape(e.text)) for e in actual], cues)
                    clipped = json.loads((output / 'entry-0001.json').read_text())
                    self.assertEqual([r['clipping_status'] for r in clipped['corrected_rttm']],
                                     ['dropped', 'clipped', 'unchanged', 'clipped', 'dropped'])
                    self.assertEqual(len(list(output.iterdir())), 9)

    def test_markdown_only_escapes_text_aliases_and_marks_coarse_precision_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asr = root / 'asr.json'
            asr.write_text(json.dumps(dict(segments=[dict(start=.125, end=.875, text=' Hi <b> *x* [y] & \\N ')])))
            aliases = root / 'aliases.json'
            aliases.write_text(json.dumps({'Alice': 'Guest_[1]'}))
            first = self.entry(exports=['markdown'], aliases=str(aliases))
            first['asr'] = str(asr)
            path = self.save(root, [first, self.entry(channel=2, exports=['markdown'])])
            output = root / 'out'
            summary = run_manifest(path, output)
            self.assertEqual(summary['status'], 'complete')
            self.assertIn('- [0.125–0.875] **Guest\\_\\[1\\]** (segment): '
                          'Hi &lt;b&gt; \\*x\\* \\[y\\] &amp; \\\\N',
                          (output / 'entry-0000.md').read_text())
            self.assertIn('**Carol** (word): Hello', (output / 'entry-0001.md').read_text())
            result = json.loads((output / 'entry-0000.json').read_text())
            self.assertEqual(result['source_asr'], json.loads(asr.read_text()))
            self.assertEqual((result['items'][0]['start'], result['items'][0]['end']), (.125, .875))
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ['entry-0000.json', 'entry-0000.md', 'entry-0001.json', 'entry-0001.md', 'summary.json'])

    def test_markdown_failure_still_attempts_srt_and_keeps_later_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def selective_failure(result):
                if result['recording'] == 'room-a':
                    raise BridgeError('simulated Markdown render failure')
                return render_markdown(result)
            for order in (['markdown', 'srt'], ['srt', 'markdown']):
                with self.subTest(order=order):
                    path = self.save(root, [self.entry(exports=order), self.entry('room-b')])
                    output = root / order[0]
                    with patch('mediawrench.speakers.manifest.render_markdown', side_effect=selective_failure):
                        summary = run_manifest(path, output)
                    first, second = summary['entries']
                    self.assertEqual(summary['counts'], dict(total=2, succeeded=1, failed=1, items=2, turns=2))
                    self.assertEqual(first['exports']['markdown'], dict(status='error', file=None))
                    self.assertEqual(first['exports']['srt'], dict(status='ok', file='entry-0000.srt'))
                    self.assertEqual(first['export_errors'], dict(markdown=first['error']))
                    self.assertEqual(first['error']['stage'], 'export:markdown')
                    self.assertEqual(second['status'], 'ok')
                    self.assertTrue((output / 'entry-0000.json').is_file())
                    self.assertFalse((output / 'entry-0000.md').exists())
                    self.assertEqual(pysubs2.load(str(output / 'entry-0000.srt'))[0].text, 'Alice: Hello')

    def test_dual_export_failures_have_all_diagnostics_and_keep_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = json.loads((BATCH / 'manifest-presentations.json').read_text())['entries'][0]
            for key in ('asr', 'rttm'):
                entry[key] = str(BATCH / entry[key])
            entry['exports'] = ['markdown', 'srt']
            path = self.save(root, [entry])
            output = root / 'out'
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('mediawrench.speakers.manifest.render_markdown', side_effect=BridgeError('Markdown failed')):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(['--manifest', str(path), '--output-dir', str(output)])
            self.assertEqual(code, 1)
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'partial')
            self.assertEqual(summary['counts'], dict(total=1, succeeded=0, failed=1, items=1, turns=1))
            entry = summary['entries'][0]
            self.assertEqual(entry['error'], entry['export_errors']['markdown'])
            for format_ in ('markdown', 'srt'):
                self.assertEqual(entry['exports'][format_], dict(status='error', file=None))
                self.assertEqual(entry['export_errors'][format_]['stage'], f'export:{format_}')
                self.assertEqual(stderr.getvalue().count(f'entries[0] (after-limit/1) export:{format_}:'), 1)
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['entry-0000.json', 'summary.json'])

    def test_invalid_exports_reject_before_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for invalid in (['markdown', 'markdown'], ['srt', 'markdown', 'srt'],
                            ['Markdown'], ['markdown', 'json'], ['markdown', {}], ['markdown', []]):
                with self.subTest(exports=invalid):
                    path = self.save(root, [self.entry(), self.entry('room-b', exports=invalid)])
                    with patch('mediawrench.speakers.manifest.merge') as merging:
                        with self.assertRaisesRegex(BridgeError, r'entries\[1\].exports'):
                            run_manifest(path, root / 'out')
                        merging.assert_not_called()
                    for extra in ([], ['--dry-run']):
                        proc = self.cli(path, root / 'out', *extra)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertIn('entries[1].exports', proc.stderr)
                        self.assertNotIn('Traceback', proc.stderr)
                    self.assertFalse((root / 'out').exists())

    def test_prerequisite_errors_skip_both_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / 'nonfinite.json'
            metadata.write_text('{"words":[{"word":"Hi","start":0,"end":1}],"metadata":1e309}')
            for stage, change in (('asr', dict(asr='missing')), ('aliases', dict(aliases='missing')),
                                  ('rttm', dict(recording='absent')), ('merge', dict(options=dict(min_coverage=2))),
                                  ('render', dict(asr=str(metadata)))):
                with self.subTest(stage=stage):
                    entry = self.entry()
                    entry.update(change)
                    path = self.save(root, [entry, self.entry('room-b')])
                    summary = run_manifest(path, root / stage)
                    failed, success = summary['entries']
                    self.assertEqual(failed['status'], 'error')
                    self.assertIsNone(failed['result'])
                    self.assertEqual(failed['error']['stage'], stage)
                    self.assertNotIn('export_errors', failed)
                    self.assertEqual(failed['exports'], {f: dict(status='skipped', file=None) for f in ('srt', 'markdown')})
                    self.assertEqual(success['status'], 'ok')
                    self.assertEqual(sorted(p.name for p in (root / stage).iterdir()),
                                     ['entry-0001.json', 'entry-0001.md', 'entry-0001.srt', 'summary.json'])

    def test_dry_run_parity_and_directory_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = BATCH / 'manifest-presentations.json'
            before = {p: p.read_bytes() for p in (path, BATCH / 'late.json', ROOT / 'examples' / 'speakers' / 'clipping-asr.json')}
            output = root / 'out'
            dry = self.cli(path, output, '--dry-run')
            self.assertEqual(dry.returncode, 1, dry.stderr)
            self.assertIn('no output written', dry.stdout)
            self.assertEqual(list(root.iterdir()), [])
            planned = run_manifest(path, output, dry_run=True)
            real = self.cli(path, output)
            self.assertEqual(real.returncode, 1, real.stderr)
            self.assertEqual(real.stderr, dry.stderr)
            planned['dry_run'] = False
            self.assertEqual(planned, json.loads((output / 'summary.json').read_text()))
            contents = {p.name: p.read_bytes() for p in output.iterdir()}
            file = root / 'file'
            file.write_text('keep')
            for target in (output, file, root / 'absent'):
                link = root / 'link'
                link.symlink_to(target)
                for candidate in (link, target) if target.exists() else (link,):
                    for extra in ([], ['--dry-run']):
                        proc = self.cli(path, candidate, *extra)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertTrue(link.is_symlink())
                link.unlink()
            self.assertEqual(file.read_text(), 'keep')
            self.assertEqual(contents, {p.name: p.read_bytes() for p in output.iterdir()})
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            proc = self.cli(path, root / 'missing' / 'out')
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertFalse((root / 'missing').exists())

    def test_markdown_disk_failure_is_run_level_and_cleans_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.save(root, [self.entry()])
            output = root / 'out'
            def fail_markdown(destination, text):
                if destination.suffix == '.md':
                    self.assertTrue((output / 'entry-0000.json').exists())
                    raise OSError('simulated Markdown disk failure')
                write_atomic(destination, text)
            stderr = io.StringIO()
            with patch('mediawrench.speakers.manifest.write_atomic', side_effect=fail_markdown):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = main(['--manifest', str(path), '--output-dir', str(output)])
            self.assertEqual(code, 2)
            self.assertIn('Markdown disk failure', stderr.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(run_manifest(path, output)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
