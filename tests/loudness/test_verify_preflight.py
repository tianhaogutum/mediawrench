"""Bounded candidate path checks: never open special files or start real tools."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from mediawrench.loudness import Profile, Toolchain, verify_candidate
from mediawrench.loudness.cli import main
from mediawrench.loudness.core import AudioError, DeadlineExceeded, JobCancelled


class VerifyPreflightTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.directory = self.root / 'directory'
        self.directory.mkdir()
        self.missing = self.root / 'missing.wav'
        self.profile = Profile(-16, -1.5, 0.5)
        self.loop = self.root / 'self-loop.wav'
        self.loop.symlink_to(self.loop.name)
        self.cycle = self.root / 'cycle-a.wav'
        other = self.root / 'cycle-b.wav'
        self.cycle.symlink_to(other.name)
        other.symlink_to(self.cycle.name)

    @contextmanager
    def metadata_only(self):
        with ExitStack() as stack:
            for name in ('builtins.open', 'io.open', 'os.open'):
                stack.enter_context(patch(name, side_effect=AssertionError('must not open candidate')))
            for name in ('mkdir', 'remove', 'unlink', 'rmdir', 'rename', 'replace', 'link', 'symlink', 'chmod'):
                stack.enter_context(patch('os.' + name, side_effect=AssertionError('must not mutate files')))
            for name in ('TemporaryDirectory', 'mkstemp'):
                stack.enter_context(patch('mediawrench.loudness.core.tempfile.' + name,
                                          side_effect=AssertionError('must not stage files')))
            spawn = stack.enter_context(patch('mediawrench.loudness.core.subprocess.Popen',
                                              side_effect=AssertionError('must reject before ffprobe')))
            yield spawn
            spawn.assert_not_called()

    def check_rejected(self, paths):
        for path in paths:
            for token in (None, Event()):
                with self.subTest(path=path, token=token), self.metadata_only():
                    with self.assertRaisesRegex(AudioError, 'regular file') as caught:
                        verify_candidate(path, self.profile, cancel_event=token)
                    self.assertEqual(caught.exception.status, 'ERROR')
            with self.subTest(cli=path), self.metadata_only():
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main([str(path), '--verify-only', '--target-i', '-16',
                                 '--target-tp', '-1.5', '--tolerance', '0.5'])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 4)
                self.assertEqual(result['status'], 'ERROR')
                self.assertFalse(result['published'])
                self.assertIn('regular file', result['error'])
                self.assertFalse(set(result) & {'media', 'measurement', 'checks', 'commands'})

    def test_directory_and_symlink_rejected_before_tools(self):
        link = self.root / 'directory-link'
        link.symlink_to(self.directory, target_is_directory=True)
        self.check_rejected((self.directory, link))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires local FIFO support')
    def test_no_writer_fifo_and_symlink_rejected_before_tools(self):
        fifo = self.root / 'no-writer.wav'
        os.mkfifo(fifo)
        link = self.root / 'fifo-link.wav'
        link.symlink_to(fifo)
        self.check_rejected((fifo, link))

    def test_missing_and_dangling_symlink_preserve_filenotfound_before_tools(self):
        dangling = self.root / 'dangling.wav'
        dangling.symlink_to(self.missing)
        for path in (self.missing, dangling):
            for token in (None, Event()):
                with self.subTest(path=path, token=token), self.metadata_only():
                    with self.assertRaises(FileNotFoundError) as caught:
                        verify_candidate(path, self.profile, cancel_event=token)
                    self.assertEqual(caught.exception.filename, str(self.missing))

    def test_symlink_loops_raise_oserror_before_tools(self):
        for path in (self.loop, self.cycle):
            for token in (None, Event()):
                with self.subTest(path=path, token=token), self.metadata_only():
                    with self.assertRaises(OSError) as caught:
                        verify_candidate(path, self.profile, cancel_event=token)
                    self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_symlink_loops_cli_json_error_before_tools(self):
        for path in (self.loop, self.cycle):
            with self.subTest(path=path), self.metadata_only():
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main([str(path), '--verify-only', '--target-i', '-16',
                                 '--target-tp', '-1.5', '--tolerance', '0.5'])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 4)
                self.assertEqual(result['status'], 'ERROR')
                self.assertFalse(result['published'])
                self.assertEqual(result['source'], str(path.absolute()))
                self.assertTrue(result['error'])
                self.assertFalse(set(result) & {'media', 'measurement', 'checks', 'commands'})

    def assert_native_cli_error(self, path, failure):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(path), '--verify-only', '--target-i', '-16',
                         '--target-tp', '-1.5', '--tolerance', '0.5'])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stdout.getvalue()), {
            'schema_version': 1, 'source': str(path.absolute()), 'published': False,
            'status': 'ERROR', 'error': str(failure), 'errno': failure.errno})

    def test_native_path_errno_cli_json_matches_library_before_tools(self):
        dangling = self.root / 'dangling.wav'
        dangling.symlink_to(self.missing)
        for path, code in ((self.missing, errno.ENOENT), (dangling, errno.ENOENT),
                           (self.loop, errno.ELOOP), (self.cycle, errno.ELOOP)):
            with self.subTest(path=path), self.metadata_only():
                with self.assertRaises(OSError) as caught:
                    verify_candidate(path, self.profile)
                self.assertEqual(caught.exception.errno, code)
                self.assert_native_cli_error(path, caught.exception)

    def test_injected_native_errno_cli_json_preserves_library_exception_identity(self):
        regular = self.root / 'regular.wav'
        regular.write_bytes(b'preflight only')
        for stage in ('os.path.realpath', 'pathlib.Path.stat'):
            for failure in (PermissionError(errno.EACCES, 'native permission denied', str(regular)),
                            PermissionError(errno.EPERM, 'native operation denied', str(regular)),
                            OSError('native error without code')):
                with self.subTest(stage=stage, errno=failure.errno), self.metadata_only(), \
                     patch(stage, side_effect=failure):
                    for token in (None, Event()):
                        with self.assertRaises(OSError) as caught:
                            verify_candidate(regular, self.profile, cancel_event=token)
                        self.assertIs(caught.exception, failure)
                    self.assert_native_cli_error(regular, failure)

    def test_non_oserror_cli_failures_do_not_infer_errno_from_cause_or_attribute(self):
        for failure, status, kind in (
                (AudioError('invalid'), 'ERROR', None),
                (AudioError('unsupported', 'UNSUPPORTED'), 'UNSUPPORTED', None),
                (ValueError('invalid value'), 'ERROR', None),
                (JobCancelled('Job cancelled'), 'ERROR', 'cancelled'),
                (KeyboardInterrupt(), 'ERROR', 'cancelled'),
                (DeadlineExceeded('budget spent'), 'ERROR', 'deadline_exceeded')):
            failure.__cause__ = PermissionError(errno.EACCES, 'underlying error')
            failure.errno = errno.EPERM
            with self.subTest(failure=type(failure).__name__), self.metadata_only(), \
                 patch('mediawrench.loudness.cli.verify_candidate', side_effect=failure):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main([str(self.missing), '--verify-only', '--target-i', '-16',
                                 '--target-tp', '-1.5', '--tolerance', '0.5'])
                expected = {'schema_version': 1, 'source': str(self.missing.absolute()),
                            'published': False, 'status': status,
                            'error': 'Job cancelled' if isinstance(failure, KeyboardInterrupt) else str(failure)}
                if kind:
                    expected['error_kind'] = kind
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(code, 3 if status == 'UNSUPPORTED' else 4)

    def test_unexpected_runtime_errors_propagate_from_resolution_probe_measure_and_cli(self):
        regular = self.root / 'regular.wav'
        regular.write_bytes(b'probe is mocked')
        for stage in ('os.path.realpath', 'mediawrench.loudness.core.probe',
                      'mediawrench.loudness.core.measure', 'mediawrench.loudness.cli.verify_candidate'):
            failure = RuntimeError('unexpected programming error')
            calls = ('cli',) if stage.endswith('cli.verify_candidate') else ('library', 'cli')
            for call in calls:
                with self.subTest(stage=stage, call=call), self.metadata_only(), ExitStack() as stack:
                    if stage.endswith('.measure'):
                        stack.enter_context(patch('mediawrench.loudness.core.probe', return_value={}))
                    stack.enter_context(patch(stage, side_effect=failure))
                    stdout = io.StringIO()
                    with redirect_stdout(stdout), self.assertRaises(RuntimeError) as caught:
                        if call == 'library':
                            verify_candidate(regular, self.profile)
                        else:
                            main([str(regular), '--verify-only', '--target-i', '-16',
                                  '--target-tp', '-1.5', '--tolerance', '0.5'])
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(stdout.getvalue(), '')

    def test_token_settings_and_preset_event_precedence_before_path(self):
        stop = Event()
        stop.set()
        cases = [(object(), Profile(-4, -1.5, 0.5), Toolchain(timeout=0), AudioError, 'threading.Event'),
                 (stop, Profile(-4, -1.5, 0.5), Toolchain(timeout=0), AudioError, 'target_i'),
                 (stop, self.profile, Toolchain(timeout=0), AudioError, 'timeout'),
                 (stop, self.profile, Toolchain(total_timeout=0), AudioError, 'total_timeout'),
                 (stop, self.profile, Toolchain(), JobCancelled, '^Job cancelled$')]
        for path in (self.directory, self.missing, self.loop, self.cycle):
            for token, profile, tools, exception, diagnostic in cases:
                with self.subTest(path=path, diagnostic=diagnostic), self.metadata_only(), \
                     patch.object(Path, 'resolve', side_effect=AssertionError('must skip resolution')), \
                     patch('os.path.realpath', side_effect=AssertionError('must skip realpath')), \
                     patch.object(Path, 'stat', side_effect=AssertionError('must skip stat')):
                    with self.assertRaisesRegex(exception, diagnostic) as caught:
                        verify_candidate(path, profile, tools, cancel_event=token)
                    self.assertEqual(type(caught.exception), exception)
        self.assertTrue(stop.is_set())

    def test_loop_cli_settings_precede_resolution(self):
        for path in (self.loop, self.cycle):
            for flag, value, diagnostic in (('--target-i', '-4', 'target_i'),
                                            ('--timeout', '0', 'timeout'),
                                            ('--total-timeout', '0', 'total_timeout')):
                with self.subTest(path=path, flag=flag), self.metadata_only(), \
                     patch('os.path.realpath', side_effect=AssertionError('must skip resolution')):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        code = main([str(path), '--verify-only', '--target-i', '-16',
                                     '--target-tp', '-1.5', '--tolerance', '0.5', flag, value])
                    result = json.loads(stdout.getvalue())
                    self.assertEqual(code, 4)
                    self.assertEqual(result['status'], 'ERROR')
                    self.assertIn(diagnostic, result['error'])
                    self.assertNotIn('errno', result)
                    self.assertNotIn('report_errno', result)


if __name__ == '__main__':
    unittest.main()
