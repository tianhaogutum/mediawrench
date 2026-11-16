"""Source hashing, PCM planning and checkpoint reuse require one stable revision."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mediawrench.chunks import split
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json, sha256
from . import test_journal_identity as journal_tests

implementation = importlib.import_module('mediawrench.chunks.split')


class SourceIdentityTest(unittest.TestCase):
    seed = journal_tests.JournalIdentityTest.seed
    snapshot = journal_tests.JournalIdentityTest.snapshot

    def setUp(self):
        journal_tests.JournalIdentityTest.setUp(self)
        self.source_bytes = self.source.read_bytes()

    def refuse(self, mode='inplace', state='interrupted', boundary='before-pcm'):
        self.source.unlink(missing_ok=True)
        self.source.write_bytes(self.source_bytes)
        output = self.root / ('case-' + str(len(list(self.root.iterdir()))))
        options = dict(max_seconds='3', overlap_seconds='0', resume=True)
        if boundary == 'energy':
            options['detect_silence'] = True
        if state.startswith('completed'):
            split(self.source, output, **options)
        elif boundary == 'energy':
            original_write = implementation._write_chunk
            calls = 0
            def interrupted(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('seed interruption')
                return original_write(*args)
            with patch.object(implementation, '_write_chunk', interrupted), self.assertRaises(OSError):
                split(self.source, output, **options)
        elif state != 'fresh':
            self.seed(output, all_committed=state == 'all-committed')
        if state != 'fresh':
            journal = output / 'checkpoint.jsonl'
            if state.endswith('legacy'):
                base = read_json(output / 'manifest.json')
                del base['checkpoint_format']
                base['chunks'][0]['sha256'] = sha256(output / 'chunk-000001.wav')
                (output / 'manifest.json').write_bytes(json_bytes(base))
                journal.unlink()
            elif state == 'completed-no-journal':
                journal.unlink()
            elif not state.startswith('completed') and boundary != 'final-hash':
                with journal.open('ab') as stream:
                    stream.write(b'{"chunk_id":')
            (output / '.chunk-000002.wav.partial').write_bytes(b'partial sentinel')
            if state in ('interrupted', 'legacy'):
                (output / 'chunk-000002.wav').write_bytes(b'orphan sentinel')

        def snapshot():
            return dict(output=self.snapshot(output) if output.exists() else None,
                        source=self.snapshot(self.root).get('source.wav'))

        raw, original_stat, changed_states = self.source.read_bytes(), self.source.stat(), []
        if mode == 'atime':
            os.utime(self.source, ns=(1, original_stat.st_mtime_ns))

        def change():
            changed = raw[:-1] + bytes([raw[-1] ^ 1])
            if mode in ('inplace', 'restored-mtime', 'identical'):
                with self.source.open('r+b') as stream:
                    stream.seek(len(raw) - 1)
                    stream.write(raw[-1:] if mode == 'identical' else changed[-1:])
                if mode == 'restored-mtime':
                    os.utime(self.source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                self.assertEqual(self.source.stat().st_ino, original_stat.st_ino)
                self.assertEqual(self.source.stat().st_size, original_stat.st_size)
            elif mode == 'metadata':
                os.utime(self.source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1))
            elif mode == 'atime':
                self.source.read_bytes()
            elif mode == 'missing':
                self.source.unlink()
            else:
                temporary = self.root / 'replacement'
                if mode == 'symlink':
                    target = self.root / 'target.wav'
                    target.write_bytes(raw)
                    temporary.symlink_to(target)
                elif mode == 'directory':
                    temporary.mkdir()
                    self.source.unlink()
                else:
                    temporary.write_bytes(raw if mode == 'replace-identical' else changed)
                temporary.replace(self.source)
            changed_states.append(snapshot())

        original_hash, original_info = implementation.sha256, implementation.wav_info
        original_load, original_detect = implementation._load_checkpoint, implementation.detect_silences
        source_hashes = 0

        def hashed(path):
            nonlocal source_hashes
            result = original_hash(path)
            if Path(path) == self.source:
                source_hashes += 1
                if (source_hashes == 1 and boundary == 'after-hash'
                        or source_hashes == 2 and boundary == 'final-hash'):
                    change()
            return result

        def inspect(path):
            if Path(path) == self.source and boundary == 'before-pcm':
                change()
            result = original_info(path)
            if (Path(path) == self.source and boundary == 'after-pcm'
                    or Path(path) == output / 'chunk-000001.wav' and boundary == 'committed-pcm'):
                change()
            return result

        def loaded(*args):
            result = original_load(*args)
            if boundary == 'after-load':
                change()
            return result

        def detect(*args, **kwargs):
            result = original_detect(*args, **kwargs)
            change()
            return result

        argv = ['split', str(self.source), str(output), '--max-seconds', '3',
                '--overlap-seconds', '0', '--resume']
        if boundary == 'energy':
            argv.append('--detect-silence')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(implementation, 'sha256', hashed), \
                patch.object(implementation, 'wav_info', inspect), \
                patch.object(implementation, '_load_checkpoint', loaded), \
                patch.object(implementation, 'detect_silences', detect), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        self.assertEqual(len(changed_states), 1)
        self.assertEqual(code, 0 if mode == 'atime' else 2, stderr.getvalue())
        if mode != 'atime':
            self.assertIn('Source', stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(snapshot(), changed_states[0])

    def test_source_change_after_initial_hash_before_pcm_preserves_checkpoint(self):
        self.refuse()

    def test_path_replacement_and_same_inode_revisions_during_validation(self):
        for mode in ('inplace', 'replace', 'replace-identical', 'restored-mtime',
                     'identical', 'metadata', 'missing', 'symlink'):
            for boundary in ('after-hash', 'after-pcm', 'committed-pcm'):
                with self.subTest(mode=mode, boundary=boundary):
                    self.refuse(mode, boundary=boundary)
        self.refuse('directory', boundary='committed-pcm')

    def test_all_committed_completed_and_legacy_reuse(self):
        for state in ('all-committed', 'completed', 'completed-no-journal', 'legacy', 'completed-legacy'):
            for mode in ('inplace', 'replace-identical'):
                with self.subTest(state=state, mode=mode):
                    self.refuse(mode, state=state, boundary='committed-pcm')

    def test_recheck_before_recovery_mutation_and_after_energy_planning(self):
        for state in ('interrupted', 'all-committed', 'completed', 'legacy'):
            with self.subTest(state=state):
                self.refuse(state=state, boundary='after-load')
        self.refuse(boundary='energy')
        self.refuse(state='fresh', boundary='after-pcm')

    def test_final_publication_and_noop_recheck_after_source_hash(self):
        for state in ('all-committed', 'completed', 'completed-no-journal', 'completed-legacy'):
            for mode in ('inplace', 'replace-identical'):
                with self.subTest(state=state, mode=mode):
                    self.refuse(mode, state=state, boundary='final-hash')

    def test_ordinary_reader_atime_does_not_refuse_completed_reuse(self):
        self.refuse('atime', state='completed', boundary='committed-pcm')


if __name__ == '__main__':
    unittest.main()
