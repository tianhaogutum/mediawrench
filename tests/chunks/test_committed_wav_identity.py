"""Reuse SHA256 and PCM validation must observe a stable committed WAV revision."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from mediawrench.chunks import split
from mediawrench.chunks.cli import main
from mediawrench.chunks.common import json_bytes, read_json, sha256
from . import test_journal_identity as journal_tests

implementation = importlib.import_module('mediawrench.chunks.split')


class CommittedWavIdentityTest(unittest.TestCase):
    # Share only the fixture helpers, not the journal test cases.
    setUp = journal_tests.JournalIdentityTest.setUp
    seed = journal_tests.JournalIdentityTest.seed
    snapshot = journal_tests.JournalIdentityTest.snapshot

    def refuse(self, mode='inplace', state='interrupted', boundary='before-pcm', tail=b'{"chunk_id":'):
        output = self.root / ('case-' + str(len(list(self.root.iterdir()))))
        if state.startswith('completed'):
            split(self.source, output, max_seconds='3', overlap_seconds='0', resume=True)
        else:
            self.seed(output, all_committed=state == 'all-committed')
        journal, first = output / 'checkpoint.jsonl', output / 'chunk-000001.wav'
        if state.endswith('legacy'):
            base = read_json(output / 'manifest.json')
            del base['checkpoint_format']
            base['chunks'][0]['sha256'] = sha256(first)
            (output / 'manifest.json').write_bytes(json_bytes(base))
            journal.unlink()
        elif not state.startswith('completed'):
            with journal.open('ab') as stream:
                stream.write(tail)
        # Refusal must not clean partials or overwrite an uncommitted orphan.
        (output / '.chunk-000002.wav.partial').write_bytes(b'partial sentinel')
        if state in ('interrupted', 'legacy'):
            (output / 'chunk-000002.wav').write_bytes(b'orphan sentinel')
        raw, original_stat, changed_states = first.read_bytes(), first.stat(), []

        def change():
            changed = raw[:-1] + bytes([raw[-1] ^ 1])
            if mode in ('inplace', 'restored-mtime', 'identical'):
                with first.open('r+b') as stream:
                    stream.seek(len(raw) - 1)
                    stream.write(raw[-1:] if mode == 'identical' else changed[-1:])
                if mode == 'restored-mtime':
                    os.utime(first, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                self.assertEqual(first.stat().st_ino, original_stat.st_ino)
                self.assertEqual(first.stat().st_size, original_stat.st_size)
            elif mode == 'metadata':
                os.utime(first, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1))
            elif mode == 'atime':
                first.read_bytes()
            else:
                temporary = self.root / 'replacement'
                if mode == 'missing':
                    first.unlink()
                elif mode == 'symlink':
                    temporary.symlink_to(self.source)
                    temporary.replace(first)
                else:
                    temporary.write_bytes(raw if mode == 'replace-identical' else changed)
                    temporary.replace(first)
                    self.assertNotEqual(first.stat().st_ino, original_stat.st_ino)
            changed_states.append(self.snapshot(output))

        original_info, original_hash = implementation.wav_info, implementation.sha256
        original_load = implementation._load_checkpoint
        source_hashes = 0

        def inspect(path):
            target = output / 'chunk-000002.wav' if boundary == 'later-chunk' else first
            if Path(path) == target and not changed_states and boundary == 'before-pcm':
                change()
            result = original_info(path)
            if Path(path) == target and not changed_states and boundary in ('after-pcm', 'later-chunk'):
                change()
            return result

        def hashed(path):
            nonlocal source_hashes
            result = original_hash(path)
            if Path(path) == first and boundary == 'after-hash' and not changed_states:
                change()
            if Path(path) == self.source:
                source_hashes += 1
                if source_hashes == 2 and boundary == 'final-source-check':
                    change()
            return result

        def loaded(*args):
            result = original_load(*args)
            if boundary == 'after-load':
                change()
            return result

        # An ordinary read may advance atime without changing ctime. os.utime
        # itself changes ctime, so set old atime before validation starts.
        if mode == 'atime':
            os.utime(first, ns=(1, original_stat.st_mtime_ns))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(implementation, 'wav_info', inspect), \
                patch.object(implementation, 'sha256', hashed), \
                patch.object(implementation, '_load_checkpoint', loaded), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['split', str(self.source), str(output), '--max-seconds', '3',
                         '--overlap-seconds', '0', '--resume'])
        self.assertEqual(len(changed_states), 1)
        if mode == 'atime':
            self.assertEqual(code, 0, stderr.getvalue())
            if sys.platform.startswith('linux'):
                # relatime advances an epoch atime on read; other platforms may not update atime.
                self.assertGreater(first.stat().st_atime_ns, 1)
        else:
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertIn('Completed chunk', stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(self.snapshot(output), changed_states[0])

    def test_equal_size_pcm_change_after_hash_before_pcm_refuses(self):
        self.refuse()

    def test_identity_or_metadata_changes_during_validation_refuse(self):
        for mode in ('inplace', 'restored-mtime', 'identical', 'metadata',
                     'replace', 'replace-identical', 'missing', 'symlink'):
            for boundary in ('after-hash', 'after-pcm'):
                with self.subTest(mode=mode, boundary=boundary):
                    self.refuse(mode, boundary=boundary)

    def test_all_committed_and_completed_noop_cannot_accept_stale_digest(self):
        for state in ('all-committed', 'completed', 'legacy', 'completed-legacy'):
            for mode in ('inplace', 'replace-identical'):
                with self.subTest(state=state, mode=mode):
                    self.refuse(mode, state=state)

    def test_recheck_all_chunks_before_first_mutation(self):
        for state in ('interrupted', 'all-committed', 'completed', 'legacy'):
            with self.subTest(state=state):
                self.refuse(state=state, boundary='after-load')
        self.refuse(state='all-committed', boundary='later-chunk')

    def test_final_publication_and_noop_return_recheck_reused_chunks(self):
        for state in ('all-committed', 'completed', 'completed-legacy'):
            with self.subTest(state=state):
                self.refuse(state=state, boundary='final-source-check', tail=b'')

    def test_atime_only_reader_does_not_refuse_completed_reuse(self):
        self.refuse('atime', state='completed', boundary='after-pcm')


if __name__ == '__main__':
    unittest.main()
