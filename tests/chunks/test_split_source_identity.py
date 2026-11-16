"""Ordinary split/plan-only must refuse mixed source-WAV revisions."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
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
from mediawrench.chunks.common import json_bytes, sha256
from mediawrench.chunks.stitch import verify_manifest
from .test_tool import write_wav
from .test_stitch_source_identity import states

implementation = importlib.import_module('mediawrench.chunks.split')


class SplitSourceIdentityTest(unittest.TestCase):
    def refusal(self, stage, mode, *, plan=False, existing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, output = root / 'source.wav', root / 'chunks'
            write_wav(source, frames=4000)
            raw, before = source.read_bytes(), source.stat()
            # Cleanup must leave unrelated staging-like paths alone.
            foreign = root / '.chunks-foreign'
            foreign.mkdir()
            (foreign / 'sentinel').write_bytes(b'foreign invocation')
            silence = root / 'silence.json'
            silence.write_bytes(json_bytes({'intervals': [{'start': '2.8', 'end': '3.2'}]}))
            expected, events, hashes = [], [], 0

            def change(restore=False):
                if restore or mode in ('inplace', 'restore-mtime', 'identical-write', 'transient'):
                    with source.open('r+b') as stream:
                        stream.seek(len(raw) - 1)
                        stream.write(raw[-1:] if restore or mode == 'identical-write'
                                     else bytes([raw[-1] ^ 1]))
                        stream.flush()
                        os.fsync(stream.fileno())
                    if mode == 'restore-mtime':
                        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertEqual((source.stat().st_ino, source.stat().st_size),
                                     (before.st_ino, before.st_size))
                elif mode == 'metadata':
                    source.chmod(before.st_mode ^ 0o100)
                elif mode == 'replace-identical':
                    replacement = root / 'replacement'
                    replacement.write_bytes(raw)
                    replacement.replace(source)
                    self.assertNotEqual(source.stat().st_ino, before.st_ino)
                else:
                    source.unlink()
                    if mode == 'symlink':
                        target = root / 'target.wav'
                        target.write_bytes(raw)
                        source.symlink_to(target)
                    elif mode == 'directory':
                        source.mkdir()
                    else:
                        self.assertEqual(mode, 'missing')
                if existing and not output.exists():
                    output.mkdir()
                    (output / 'sentinel').write_bytes(b'external output')
                # Staging is owned by this invocation; all other paths must match.
                expected[:] = [{k: v for k, v in states(root).items()
                                if not k.startswith('.chunks-') or k.startswith('.chunks-foreign')}]
                events.append('restore' if restore else 'change')

            original_hash, original_copy = implementation.sha256, implementation._write_chunk
            def hashed(path):
                nonlocal hashes
                result = original_hash(path)
                if Path(path) == source:
                    hashes += 1
                    if hashes == (2 if stage == 'final-hash' else 1) and stage in ('hash', 'final-hash'):
                        change()
                return result

            def copy(audio, path, info, start, end):
                if start == 0 and stage == 'copy-transient':
                    change()
                original_copy(audio, path, info, start, end)
                if end == 4000:
                    if stage == 'copy-transient' or stage == 'hash' and mode == 'transient':
                        change(restore=True)
                    elif stage == 'copy':
                        change()

            argv = ['split', str(source), str(output), '--max-seconds', '3', '--overlap-seconds', '0']
            if plan:
                argv.append('--plan-only')
            if stage == 'energy':
                argv.append('--detect-silence')
            if stage == 'supplied':
                argv.extend(['--silence-json', str(silence)])
            hooks = {'pcm': 'wav_info', 'energy': 'detect_silences',
                     'supplied': '_supplied_silence_snapshot', 'plan': 'plan_chunks',
                     'serialize': 'json_bytes'}
            stdout, stderr = io.StringIO(), io.StringIO()
            renamed = []
            original_rename = Path.rename
            def rename(path, target):
                renamed.append((path, target))
                return original_rename(path, target)
            with ExitStack() as stack:
                stack.enter_context(patch.object(implementation, 'sha256', hashed))
                stack.enter_context(patch.object(implementation, '_write_chunk', copy))
                staged = stack.enter_context(patch.object(implementation.tempfile, 'mkdtemp',
                                                         wraps=implementation.tempfile.mkdtemp))
                stack.enter_context(patch.object(Path, 'rename', rename))
                if stage in hooks:
                    name = hooks[stage]
                    original = getattr(implementation, name)
                    def wrapped(*args, **kwargs):
                        value = original(*args, **kwargs)
                        change()
                        return value
                    stack.enter_context(patch.object(implementation, name, wrapped))
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(argv)
            # The original round-14 transient hash/copy probe exits 0 here,
            # with the initial source hash and wrong last-chunk PCM. Fail first
            # on the intended refusal contract; the durable probe shows bytes.
            self.assertTrue(events)
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertIn('Source', stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(renamed, [])
            self.assertEqual(states(root), expected[0])
            if stage in ('hash', 'pcm', 'energy', 'supplied', 'plan'):
                staged.assert_not_called()
            else:
                staged.assert_called_once()
            self.assertEqual(output.exists(), existing)
            if events == ['change', 'restore']:
                self.assertEqual(source.read_bytes(), raw)

    def test_transient_source_probe_refused(self):
        self.refusal('hash', 'transient')

    def test_early_validation_before_staging(self):
        for plan in (False, True):
            for stage in ('hash', 'pcm', 'energy', 'supplied', 'plan'):
                for mode in ('inplace', 'replace-identical', 'metadata'):
                    with self.subTest(plan=plan, stage=stage, mode=mode):
                        self.refusal(stage, mode, plan=plan)

    def test_copy_and_final_publication(self):
        self.refusal('copy-transient', 'transient')
        for stage in ('copy', 'final-hash', 'serialize'):
            for mode in ('inplace', 'replace-identical', 'metadata'):
                for plan in ((False,) if stage == 'copy' else (False, True)):
                    with self.subTest(stage=stage, mode=mode, plan=plan):
                        self.refusal(stage, mode, plan=plan)

    def test_conservative_and_nonregular_changes_preserve_existing_paths(self):
        for mode in ('restore-mtime', 'identical-write', 'missing', 'symlink', 'directory'):
            with self.subTest(mode=mode):
                self.refusal('serialize', mode, existing=True)
        for stage in ('hash', 'copy'):
            self.refusal(stage, 'replace-identical', existing=True)

    def test_stable_alias_reader_plan_and_resume(self):
        for plan, resume in ((False, False), (True, False), (False, True)):
            with self.subTest(plan=plan, resume=resume), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, alias, output = root / 'source.wav', root / 'alias', root / 'chunks'
                write_wav(source, frames=4000)
                raw = source.read_bytes()
                s = source.stat()
                os.utime(source, ns=(1, s.st_mtime_ns))
                before = states(root)
                alias.symlink_to(source)
                manifest = split(alias, output, max_seconds='3', overlap_seconds='0',
                                 plan_only=plan, resume=resume)
                self.assertEqual(manifest['source']['sha256'], sha256(source))
                self.assertEqual(manifest['source']['path'], '../source.wav')
                self.assertEqual(manifest['complete'], not plan)
                self.assertEqual(states(root)['source.wav'], before['source.wav'])
                if plan:
                    self.assertEqual([p.name for p in output.iterdir()], ['manifest.json'])
                    self.assertTrue(all(c['sha256'] is None for c in manifest['chunks']))
                    with self.assertRaisesRegex(ManifestError, 'plan-only'):
                        verify_manifest(output / 'manifest.json')
                else:
                    self.assertEqual(verify_manifest(output / 'manifest.json'), manifest)
                    with wave.open(str(source), 'rb') as audio:
                        pcm = audio.readframes(4000)
                    for chunk in manifest['chunks']:
                        with wave.open(str(output / chunk['file']), 'rb') as audio:
                            self.assertEqual(audio.readframes(audio.getnframes()),
                                             pcm[chunk['start_sample'] * 2:chunk['end_sample'] * 2])
                if resume:
                    completed = states(output)
                    self.assertEqual(split(alias, output, max_seconds='3', overlap_seconds='0', resume=True), manifest)
                    self.assertEqual(states(output), completed)
                self.assertEqual(source.read_bytes(), raw)

    def test_initial_nonregular_source_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / 'directory'
            source.mkdir()
            with self.assertRaisesRegex(ManifestError, 'Source.*regular'):
                split(source, Path(directory).resolve() / 'chunks')


if __name__ == '__main__':
    unittest.main()
