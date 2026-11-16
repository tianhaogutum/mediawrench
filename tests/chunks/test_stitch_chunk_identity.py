"""Chunk WAV revisions must stay stable until stitch publisher entry."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import ManifestError, stitch
from mediawrench.chunks.cli import main
from mediawrench.chunks.stitch import verify_manifest
from . import test_stitch_source_identity as source_fixture

implementation = source_fixture.implementation
states = source_fixture.states


class StitchChunkIdentityTest(unittest.TestCase):
    fixture = source_fixture.StitchSourceIdentityTest.fixture
    change = source_fixture.StitchSourceIdentityTest.change

    def hook(self, target, stage, change):
        name = dict(hash='sha256', **{'before-pcm': 'wav_info'}, pcm='wav_info',
                    chunk='wav_info', transcript='read_transcript', serialize='to_srt')[stage]
        original = getattr(implementation, name)
        def wrapped(*args, **kwargs):
            path = Path(args[0]) if stage != 'serialize' else None
            if stage == 'before-pcm' and path == target:
                change()
            value = original(*args, **kwargs)
            if ((stage in ('hash', 'pcm') and path == target)
                    or (stage == 'chunk' and path.name == 'chunk-000002.wav')
                    or (stage == 'transcript' and path.name == 'chunk-000002.json')
                    or stage == 'serialize'):
                change()
            return value
        return patch.object(implementation, name, wrapped)

    def refusal(self, stage, mode, *, native=False, existing=False, verify=False, last=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, manifest_path, transcripts, output, manifest = self.fixture(root, native)
            target = manifest_path.parent / manifest['chunks'][-1 if last else 0]['file']
            if existing:
                for suffix in ('.json', '.srt', '.report.json'):
                    output.with_suffix(suffix).write_bytes(b'existing output sentinel\n')
            captured = []
            def change():
                self.assertFalse(captured)
                self.change(target, mode)
                captured.append(states(root))
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.hook(target, stage, change), patch.object(
                    implementation, 'publish_files', wraps=implementation.publish_files) as publish:
                if verify:
                    with self.assertRaisesRegex(ManifestError, 'Chunk.*(changed|regular)'):
                        verify_manifest(manifest_path)
                else:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(['stitch', str(manifest_path), str(transcripts), str(output)]
                                    + (['--native-json'] if native else []))
                    self.assertEqual(code, 2, stderr.getvalue())
                    self.assertRegex(stderr.getvalue(), 'Chunk.*(changed|regular)')
                    self.assertEqual(stdout.getvalue(), '')
                publish.assert_not_called()
            self.assertEqual(len(captured), 1)
            self.assertEqual(states(root), captured[0])
            self.assertFalse(list(root.rglob('.acm-*')))

    def test_hash_and_pcm_bound_to_each_chunk_revision(self):
        for last in (False, True):
            for stage in ('hash', 'before-pcm', 'pcm'):
                for mode in ('inplace', 'replace-identical'):
                    with self.subTest(last=last, stage=stage, mode=mode):
                        self.refusal(stage, mode, last=last)

    def test_earlier_chunk_changes_during_later_validation(self):
        for stage in ('chunk', 'transcript', 'serialize'):
            for mode in ('inplace', 'replace-identical'):
                for native in (False, True):
                    with self.subTest(stage=stage, mode=mode, native=native):
                        self.refusal(stage, mode, native=native)

    def test_conservative_metadata_and_nonregular_changes(self):
        for mode in ('restore-mtime', 'identical-write', 'metadata', 'missing', 'symlink', 'directory'):
            with self.subTest(mode=mode):
                self.refusal('transcript', mode)

    def test_existing_outputs_are_untouched(self):
        for mode in ('inplace', 'replace-identical'):
            with self.subTest(mode=mode):
                self.refusal('transcript', mode, existing=True)

    def test_verify_manifest_checks_all_chunks_and_keeps_value_api(self):
        for stage in ('hash', 'pcm', 'chunk'):
            for mode in ('inplace', 'replace-identical'):
                with self.subTest(stage=stage, mode=mode):
                    self.refusal(stage, mode, verify=True)
        with tempfile.TemporaryDirectory() as directory:
            _, path, _, _, manifest = self.fixture(Path(directory).resolve())
            result = verify_manifest(path)
            self.assertIs(type(result), dict)
            self.assertEqual(result, manifest)

    def test_initial_nonregular_chunk_refused_without_reading(self):
        for mode in ('symlink', 'directory', 'fifo'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, path, transcripts, output, _ = self.fixture(root)
                target = path.parent / 'chunk-000001.wav'
                if mode == 'fifo':
                    target.unlink()
                    os.mkfifo(target)
                else:
                    self.change(target, mode)
                with patch.object(implementation, 'sha256', wraps=implementation.sha256) as hashed:
                    with self.assertRaisesRegex(ManifestError, 'Chunk.*regular'):
                        stitch(path, transcripts, output)
                    self.assertNotIn(target, [call.args[0] for call in hashed.call_args_list])
                self.assertFalse(output.exists())

    def test_stable_strict_native_and_reader_atime(self):
        for native in (False, True):
            with self.subTest(native=native), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, path, transcripts, output, manifest = self.fixture(root, native)
                for chunk in manifest['chunks']:
                    target = path.parent / chunk['file']
                    os.utime(target, ns=(1, target.stat().st_mtime_ns))
                before = states(root)
                result = stitch(path, transcripts, output, native_json=native)
                self.assertEqual([(s['start_sample'], s['end_sample']) for s in result['segments']],
                                 [(100, 500), (3100, 3500)])
                self.assertEqual(result['report']['identity']['mode'], 'native_json' if native else 'strict')
                self.assertEqual(verify_manifest(path), manifest)
                after = states(root)
                for key, value in before.items():
                    self.assertEqual(after[key], value)
                self.assertEqual(sorted(p.name for p in root.glob('merged.*')),
                                 ['merged.json', 'merged.report.json', 'merged.srt'])


if __name__ == '__main__':
    unittest.main()
