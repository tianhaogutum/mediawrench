"""Stitch source revisions must remain stable through validation/publication."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mediawrench.chunks import ManifestError, split, stitch
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json, sha256
from mediawrench.chunks.stitch import verify_manifest
from .test_tool import write_wav

implementation = importlib.import_module('mediawrench.chunks.stitch')


def states(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            s = path.stat()
            result[str(path.relative_to(root))] = (s.st_dev, s.st_ino, s.st_mode, s.st_size,
                                                  s.st_mtime_ns, s.st_ctime_ns, path.read_bytes())
        elif path.is_symlink():
            result[str(path.relative_to(root))] = ('symlink', os.readlink(path))
        elif path.is_dir():
            result[str(path.relative_to(root))] = ('directory',)
    return result


class StitchSourceIdentityTest(unittest.TestCase):
    def fixture(self, root, native=False):
        source = root / 'source.wav'
        write_wav(source, frames=4000)
        manifest = split(source, root / 'chunks', max_seconds='3', overlap_seconds='0')
        manifest_path = root / 'chunks/manifest.json'
        transcripts = root / 'transcripts'
        transcripts.mkdir()
        for chunk in manifest['chunks']:
            data = dict(segments=[dict(start='0.1', end='0.5', text=chunk['id'])])
            if not native:
                data.update(recording_sha256=manifest['source']['sha256'],
                            manifest_sha256=sha256(manifest_path), chunk_id=chunk['id'],
                            audio_sha256=chunk['sha256'])
            (transcripts / (chunk['id'] + '.json')).write_bytes(json_bytes(data))
        return source, manifest_path, transcripts, root / 'merged.json', manifest

    def change(self, source, mode):
        before = source.stat()
        raw = source.read_bytes()
        if mode in ('inplace', 'restore-mtime', 'identical-write'):
            with source.open('r+b') as stream:
                stream.seek(len(raw) - 1)
                stream.write(raw[-1:] if mode == 'identical-write' else bytes([raw[-1] ^ 1]))
                os.fsync(stream.fileno())
            if mode == 'restore-mtime':
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual((source.stat().st_dev, source.stat().st_ino, source.stat().st_size),
                             (before.st_dev, before.st_ino, before.st_size))
        elif mode == 'metadata':
            source.chmod(before.st_mode ^ 0o100)
        elif mode == 'replace-identical':
            replacement = source.with_suffix('.new')
            replacement.write_bytes(raw)
            replacement.replace(source)
            self.assertNotEqual(source.stat().st_ino, before.st_ino)
        else:
            source.unlink()
            if mode == 'symlink':
                target = source.with_suffix('.target')
                target.write_bytes(raw)
                source.symlink_to(target)
            elif mode == 'directory':
                source.mkdir()
            else:
                self.assertEqual(mode, 'missing')

    def hook(self, source, stage, change):
        name = dict(hash='sha256', pcm='wav_info', chunk='wav_info',
                    transcript='read_transcript', serialize='to_srt')[stage]
        original = getattr(implementation, name)
        def wrapped(*args, **kwargs):
            value = original(*args, **kwargs)
            path = Path(args[0]) if stage != 'serialize' else None
            matches = ((stage in ('hash', 'pcm') and path.resolve() == source)
                       or (stage == 'chunk' and path.name == 'chunk-000002.wav')
                       or (stage == 'transcript' and path.name == 'chunk-000002.json')
                       or stage == 'serialize')
            if matches:
                change()
            return value
        return patch.object(implementation, name, wrapped)

    def refusal(self, stage, mode, *, native=False, existing=False, verify=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, manifest_path, transcripts, output, _ = self.fixture(root, native)
            if existing:
                for suffix in ('.json', '.srt', '.report.json'):
                    output.with_suffix(suffix).write_bytes(b'existing output sentinel\n')
            captured = []
            def change():
                self.assertFalse(captured)
                self.change(source, mode)
                captured.append(states(root))
            stdout, stderr = io.StringIO(), io.StringIO()
            # Publisher must never be entered, even if existing output paths
            # would independently cause a no-clobber error.
            with self.hook(source, stage, change), patch.object(
                    implementation, 'publish_files', wraps=implementation.publish_files) as publish:
                if verify:
                    with self.assertRaisesRegex(ManifestError, 'Source.*(changed|regular)'):
                        verify_manifest(manifest_path)
                else:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main(['stitch', str(manifest_path), str(transcripts), str(output)]
                                    + (['--native-json'] if native else []))
                    self.assertEqual(code, 2, stderr.getvalue())
                    self.assertRegex(stderr.getvalue(), 'Source.*(changed|regular)')
                    self.assertEqual(stdout.getvalue(), '')
                publish.assert_not_called()
            self.assertEqual(len(captured), 1)
            self.assertEqual(states(root), captured[0])
            self.assertFalse(list(root.rglob('.acm-*')))

    def test_after_source_hash_before_pcm(self):
        for mode in ('inplace', 'replace-identical'):
            with self.subTest(mode=mode):
                self.refusal('hash', mode)

    def test_source_changes_during_later_validation(self):
        for stage in ('pcm', 'chunk', 'transcript', 'serialize'):
            for mode in ('inplace', 'replace-identical'):
                for native in (False, True):
                    with self.subTest(stage=stage, mode=mode, native=native):
                        self.refusal(stage, mode, native=native)

    def test_conservative_metadata_and_nonregular_paths(self):
        for mode in ('restore-mtime', 'identical-write', 'metadata', 'missing', 'symlink', 'directory'):
            with self.subTest(mode=mode):
                self.refusal('transcript', mode)

    def test_existing_outputs_are_untouched(self):
        for mode in ('inplace', 'replace-identical'):
            with self.subTest(mode=mode):
                self.refusal('transcript', mode, existing=True)

    def test_verify_manifest_checks_source_and_keeps_value_api(self):
        for stage in ('hash', 'pcm', 'chunk'):
            for mode in ('inplace', 'replace-identical'):
                with self.subTest(stage=stage, mode=mode):
                    self.refusal(stage, mode, verify=True)
        with tempfile.TemporaryDirectory() as directory:
            _, path, _, _, manifest = self.fixture(Path(directory).resolve())
            result = verify_manifest(path)
            self.assertIs(type(result), dict)
            self.assertEqual(result, manifest)

    def test_stable_native_strict_and_ordinary_reader(self):
        for native in (False, True):
            with self.subTest(native=native), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, path, transcripts, output, manifest = self.fixture(root, native)
                # Old atime makes real validation reads update atime on relatime
                # filesystems, without inducing the ctime change of utime midrun.
                s = source.stat()
                os.utime(source, ns=(1, s.st_mtime_ns))
                # Initial aliases remain supported; later checks track resolved path.
                alias = source.with_suffix('.alias')
                alias.symlink_to(source)
                manifest['source']['path'] = str(alias)
                path.write_bytes(json_bytes(manifest))
                if not native:
                    for transcript in transcripts.glob('*.json'):
                        data = read_json(transcript)
                        data['manifest_sha256'] = sha256(path)
                        transcript.write_bytes(json_bytes(data))
                before = states(root)
                result = stitch(path, transcripts, output, native_json=native)
                self.assertEqual(result['source'], manifest['source'])
                self.assertEqual([(s['start_sample'], s['end_sample']) for s in result['segments']],
                                 [(100, 500), (3100, 3500)])
                after = states(root)
                for key, value in before.items():
                    self.assertEqual(after[key], value)
                self.assertEqual(verify_manifest(path), manifest)
                self.assertEqual(result['report']['identity']['mode'], 'native_json' if native else 'strict')
                self.assertEqual(sorted(p.name for p in root.glob('merged.*')),
                                 ['merged.json', 'merged.report.json', 'merged.srt'])


if __name__ == '__main__':
    unittest.main()
