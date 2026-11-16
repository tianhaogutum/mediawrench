"""Freshly exported WAV digests must remain valid through split completion."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from mediawrench.chunks import ManifestError, split
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json, sha256
from .test_tool import write_wav
from .test_stitch_source_identity import states

implementation = importlib.import_module('mediawrench.chunks.split')


class SplitChunkIdentityTest(unittest.TestCase):
    def exercise(self, resume=False, boundary='later-copy', mode='inplace', recovery=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, output = root / 'source.wav', root / 'chunks'
            total = 7000 if recovery else 4000
            write_wav(source, frames=total)
            source_before = states(root)['source.wav']
            foreign = root / '.chunks-foreign'
            foreign.mkdir()
            (foreign / 'sentinel').write_bytes(b'other invocation')
            original_copy = implementation._write_chunk
            if recovery:
                def seed(audio, path, info, start, end):
                    if start == 3000:
                        raise OSError('seed interruption')
                    original_copy(audio, path, info, start, end)
                with patch.object(implementation, '_write_chunk', seed):
                    with self.assertRaisesRegex(OSError, 'seed interruption'):
                        split(source, output, max_seconds='3', overlap_seconds='0', resume=True)
                if recovery == 'legacy':
                    base = read_json(output / 'manifest.json')
                    del base['checkpoint_format']
                    base['chunks'][0]['sha256'] = sha256(output / 'chunk-000001.wav')
                    (output / 'manifest.json').write_bytes(json_bytes(base))
                    (output / 'checkpoint.jsonl').unlink()
            prior = states(output) if recovery else {}
            target_name = 'chunk-000002.wav' if recovery else 'chunk-000001.wav'
            target = None
            changed, renames = [], []
            original_hash, original_json, original_rename = implementation.sha256, implementation.json_bytes, Path.rename
            source_hashes = 0

            def change(path):
                nonlocal target
                target = path
                raw, before = path.read_bytes(), path.stat()
                if mode == 'replace-identical':
                    replacement = root / 'replacement'
                    replacement.write_bytes(raw)
                    replacement.replace(path)
                    self.assertNotEqual(path.stat().st_ino, before.st_ino)
                elif mode == 'metadata':
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
                elif mode in ('missing', 'symlink', 'directory'):
                    path.unlink()
                    if mode == 'symlink':
                        path.symlink_to(source)
                    elif mode == 'directory':
                        path.mkdir()
                else:
                    with path.open('r+b') as stream:
                        stream.seek(len(raw) - 1)
                        stream.write(raw[-1:] if mode in ('identical', 'atime') else bytes([raw[-1] ^ 1]))
                    if mode == 'atime':
                        raise AssertionError('reader handled separately')
                    if mode == 'restored-mtime':
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertEqual((path.stat().st_dev, path.stat().st_ino, path.stat().st_size),
                                     (before.st_dev, before.st_ino, before.st_size))
                changed.append(states(path.parent).get(path.name))

            def copied(audio, path, info, start, end):
                original_copy(audio, path, info, start, end)
                if boundary == 'later-copy' and end == total:
                    change(Path(path).parent / target_name)

            def hashed(path):
                nonlocal source_hashes
                value = original_hash(path)
                p = Path(path)
                if boundary == 'hash' and p.name.lstrip('.').removesuffix('.partial') == target_name and not changed:
                    change(p)
                if p == source:
                    source_hashes += 1
                    if boundary == 'final-source-hash' and source_hashes == 2:
                        change(output / target_name if resume else next(root.glob('.chunks-*/*' + target_name)))
                return value

            def serialized(manifest):
                value = original_json(manifest)
                if boundary == 'serialize' and manifest.get('complete') is True:
                    change(output / target_name if resume else next(root.glob('.chunks-*/*' + target_name)))
                return value

            def renamed(path, destination):
                renames.append((path, destination))
                return original_rename(path, destination)

            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ['split', str(source), str(output), '--max-seconds', '3', '--overlap-seconds', '0']
            if resume:
                argv.append('--resume')
            with patch.object(implementation, '_write_chunk', copied), \
                    patch.object(implementation, 'sha256', hashed), \
                    patch.object(implementation, 'json_bytes', serialized), \
                    patch.object(Path, 'rename', renamed), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(argv)
            self.assertEqual(len(changed), 1)
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('chunk', stderr.getvalue())
            self.assertEqual(states(root)['source.wav'], source_before)
            self.assertEqual((foreign / 'sentinel').read_bytes(), b'other invocation')
            self.assertEqual(list(root.glob('.chunks-*')), [foreign])
            if not resume:
                self.assertFalse(output.exists())
                self.assertFalse(renames)
                return
            self.assertFalse(read_json(output / 'manifest.json')['complete'])
            self.assertEqual(states(output).get(target.name), changed[0])
            if recovery:
                self.assertEqual(states(output)['chunk-000001.wav'], prior['chunk-000001.wav'])
            # Prior commits are not rolled back. Changed committed PCM refuses
            # a stable retry before writes; identical replacement can be reused.
            before_retry = states(output)
            committed = boundary != 'hash'
            if mode in ('inplace', 'restored-mtime') and committed:
                with self.assertRaisesRegex(ManifestError, 'SHA256 mismatch'):
                    split(source, output, max_seconds='3', overlap_seconds='0', resume=True)
                self.assertEqual(states(output), before_retry)
            elif mode in ('replace-identical', 'metadata', 'identical'):
                manifest = split(source, output, max_seconds='3', overlap_seconds='0', resume=True)
                self.assertTrue(manifest['complete'])
                if committed:
                    self.assertEqual(states(output)[target.name], before_retry[target.name])
            elif boundary == 'hash' and mode == 'inplace':
                manifest = split(source, output, max_seconds='3', overlap_seconds='0', resume=True)
                self.assertTrue(manifest['complete'])
                self.assertEqual(sha256(output / target_name), manifest['chunks'][0 if not recovery else 1]['sha256'])

    def test_new_chunk_probe_refused(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.exercise(resume)

    def test_new_hash_and_final_validation_boundaries(self):
        for resume in (False, True):
            for boundary in ('hash', 'later-copy', 'final-source-hash', 'serialize'):
                for mode in ('inplace', 'replace-identical', 'identical', 'restored-mtime', 'metadata'):
                    with self.subTest(resume=resume, boundary=boundary, mode=mode):
                        self.exercise(resume, boundary, mode)

    def test_nonregular_new_chunk_replacements_refused(self):
        for resume in (False, True):
            for mode in ('missing', 'symlink', 'directory'):
                with self.subTest(resume=resume, mode=mode):
                    self.exercise(resume, mode=mode)

    def test_new_chunks_during_journal_and_legacy_recovery(self):
        for recovery in ('journal', 'legacy'):
            for boundary in ('later-copy', 'serialize'):
                for mode in ('inplace', 'replace-identical'):
                    with self.subTest(recovery=recovery, boundary=boundary, mode=mode):
                        self.exercise(True, boundary, mode, recovery)

    def test_stable_multi_chunk_and_reader_atime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'source.wav'
            write_wav(source, frames=7000)
            with wave.open(str(source), 'rb') as audio:
                pcm = audio.readframes(7000)
            original_hash = implementation.sha256
            def read_after_hash(path):
                value = original_hash(path)
                if Path(path).suffix == '.wav' and Path(path) != source:
                    Path(path).read_bytes()  # atime is not a writer revision
                return value
            for resume in (False, True):
                output = root / str(resume)
                with patch.object(implementation, 'sha256', read_after_hash):
                    manifest = split(source, output, max_seconds='3', overlap_seconds='0', resume=resume)
                self.assertTrue(manifest['complete'])
                self.assertEqual(len(manifest['chunks']), 3)
                for chunk in manifest['chunks']:
                    path = output / chunk['file']
                    self.assertEqual(sha256(path), chunk['sha256'])
                    with wave.open(str(path), 'rb') as audio:
                        self.assertEqual(audio.readframes(audio.getnframes()),
                                         pcm[chunk['start_sample'] * 2:chunk['end_sample'] * 2])
                if resume:
                    before = states(output)
                    with patch.object(implementation, '_write_chunk', side_effect=AssertionError('reuse')):
                        split(source, output, max_seconds='3', overlap_seconds='0', resume=True)
                    self.assertEqual(states(output), before)


if __name__ == '__main__':
    unittest.main()
