"""Read-only candidate CLI: real WAVs, strict mode separation and exception JSON."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mediawrench.loudness import Profile, Toolchain, verify_candidate
from mediawrench.loudness.cli import main
from mediawrench.loudness.core import AudioError, DeadlineExceeded, JobCancelled
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class VerifyCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG
        cls.ffprobe = FFPROBE
        scratch = ROOT / '.tools/test-tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix='verify-cli-', dir=scratch)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=self.fixtures.name)
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.protected = self.root / 'read-only'
        self.protected.mkdir()
        self.source = self.protected / '候補 café.wav'
        self.silence = self.protected / 'silence.wav'
        self.unsupported = self.protected / '16bit.wav'
        self.short = self.protected / 'short.wav'
        for path, source, codec in (
            (self.source, 'sine=frequency=440:duration=4:sample_rate=48000', 'pcm_s24le'),
            (self.silence, 'anullsrc=r=48000:cl=mono:d=4', 'pcm_s24le'),
            (self.unsupported, 'sine=duration=4:sample_rate=48000', 'pcm_s16le'),
            (self.short, 'sine=duration=0.1:sample_rate=48000', 'pcm_s24le'),
        ):
            subprocess.run([self.ffmpeg, '-v', 'error', '-nostdin', '-threads', '1',
                            '-filter_threads', '1', '-f', 'lavfi', '-i', source,
                            '-c:a', codec, '-n', str(path)], capture_output=True, check=True, timeout=120)
        self.corrupt = self.protected / 'corrupt.wav'
        self.corrupt.write_bytes(b'not a WAV')
        self.before = {p.name: p.read_bytes() for p in self.protected.iterdir()}
        for p in self.protected.iterdir():
            p.chmod(0o444)
        self.protected.chmod(0o555)
        self.addCleanup(self.protected.chmod, 0o755)

    def tearDown(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.protected.iterdir()}, self.before)
        self.assertEqual(self.protected.stat().st_mode & 0o777, 0o555)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o444 for p in self.protected.iterdir()))

    def argv(self, source=None, *extra):
        return [str(source or self.source), '--verify-only', '--target-i', '-21.75',
                '--target-tp', '-1.5', '--tolerance', '0.5', '--ffmpeg', self.ffmpeg,
                '--ffprobe', self.ffprobe, *extra]

    @contextmanager
    def no_writes(self):
        real_open, real_os_open = io.open, os.open
        def open_read(path, mode='r', *args, **kwargs):
            self.assertFalse(any(c in mode for c in 'wax+'), (path, mode))
            return real_open(path, mode, *args, **kwargs)
        def os_open_read(path, flags, *args, **kwargs):
            if path != os.devnull:
                self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return real_os_open(path, flags, *args, **kwargs)
        with ExitStack() as stack:
            for name in ('io.open', 'builtins.open'):
                stack.enter_context(patch(name, open_read))
            stack.enter_context(patch('os.open', os_open_read))
            for name in ('mkdir', 'remove', 'unlink', 'rmdir', 'rename', 'replace', 'link', 'symlink', 'chmod'):
                stack.enter_context(patch('os.' + name, side_effect=AssertionError('file mutation: ' + name)))
            for name in ('TemporaryDirectory', 'mkstemp'):
                stack.enter_context(patch('mediawrench.loudness.core.tempfile.' + name,
                                          side_effect=AssertionError('file creation: ' + name)))
            yield

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.no_writes():
            code = main(argv)
        self.assertEqual(stderr.getvalue(), '')
        return code, json.loads(stdout.getvalue())

    def assert_error(self, result, status='ERROR', message=None):
        self.assertEqual(result['status'], status, result)
        self.assertFalse(result['published'])
        self.assertNotIn('report_errno', result)
        if message:
            self.assertIn(message, result['error'])
        for name in ('checks', 'media', 'measurement', 'commands'):
            self.assertNotIn(name, result)

    def test_verify_only_real_pass_fail_matches_library_without_writes(self):
        for target, expected, code in ((-21.75, 'PASS', 0), (-16, 'FAIL', 2)):
            with self.subTest(target=target):
                library = verify_candidate(self.source, Profile(float(target), -1.5, 0.5),
                                           Toolchain(self.ffmpeg, self.ffprobe))
                actual_code, result = self.call(self.argv(None, '--target-i', str(target)))
                self.assertEqual(actual_code, code)
                self.assertEqual(result, {'schema_version': 1, 'source': str(self.source.absolute()),
                                          'published': False, **library})
                self.assertEqual(result['status'], expected)
                self.assertEqual(len(result['commands']), 2)
                self.assertIn('-f', result['commands'][1])
                self.assertEqual(result['commands'][1][-3:], ['-f', 'null', '-'])

    def test_unsupported_silent_short_corrupt_missing_and_tools_errors(self):
        cases = [(self.unsupported, (), 3, 'UNSUPPORTED', 'Candidate must be'),
                 (self.silence, (), 3, 'UNSUPPORTED', 'Nonfinite'),
                 (self.short, (), 3, 'UNSUPPORTED', 'shorter than 3s'),
                 (self.corrupt, (), 4, 'ERROR', 'exited'),
                 (self.protected / 'missing.wav', (), 4, 'ERROR', 'missing.wav'),
                 (self.source, ('--ffprobe', '/no-such/ffprobe'), 4, 'ERROR', 'Executable not found'),
                 (self.source, ('--ffmpeg', '/no-such/ffmpeg'), 4, 'ERROR', 'Executable not found')]
        for path, extra, code, status, message in cases:
            with self.subTest(path=path, extra=extra):
                actual, result = self.call(self.argv(path, *extra))
                self.assertEqual(actual, code)
                self.assert_error(result, status, message)
                if path != self.protected / 'missing.wav':
                    self.assertNotIn('errno', result)

    def test_symlink_to_regular_candidate_matches_library_and_cli(self):
        link = self.root / 'candidate-link.wav'
        link.symlink_to(self.source)
        for target, status, code in ((-21.75, 'PASS', 0), (-16.0, 'FAIL', 2)):
            profile = Profile(target, -1.5, 0.5)
            with self.subTest(target=target), self.no_writes():
                direct = verify_candidate(self.source, profile, Toolchain(self.ffmpeg, self.ffprobe))
                self.assertEqual(verify_candidate(link, profile, Toolchain(self.ffmpeg, self.ffprobe)), direct)
                actual, result = self.call(self.argv(link, '--target-i', str(target)))
            self.assertEqual(actual, code)
            self.assertEqual(result, {'schema_version': 1, 'source': str(link.absolute()),
                                      'published': False, **direct})
            self.assertEqual(result['status'], status)
        self.assertTrue(link.is_symlink())

    def test_invalid_profile_and_budgets_fail_before_tools(self):
        for flag, value in (('--target-i', '-4'), ('--target-tp', 'nan'), ('--tolerance', '0'),
                            ('--target-lra', '51'), ('--timeout', '0'), ('--timeout', '3601'),
                            ('--total-timeout', 'inf'), ('--total-timeout', '86401')):
            with self.subTest(flag=flag), patch('mediawrench.loudness.core.subprocess.Popen') as spawn:
                code, result = self.call(self.argv(self.protected / 'missing.wav', flag, value))
                self.assertEqual(code, 4)
                self.assert_error(result, message=flag[2:].replace('-', '_'))
                spawn.assert_not_called()

    def test_mixed_modes_and_missing_arguments_are_usage_errors_before_tools(self):
        valid = self.argv()
        cases = [valid + ['--list-streams'], valid + [str(self.source)],
                 valid + ['--report', str(self.source)], valid + ['--audio-stream', '0'],
                 [str(self.source), '--verify-only'],
                 [str(self.source), '--verify-only', '--target-i', '-16', '--target-tp', '-1.5'],
                 [str(self.source), '--list-streams', '--target-i', '-16'],
                 [str(self.source), '--target-i', '-16', '--target-tp', '-1.5', '--tolerance', '0.5']]
        for argv in cases:
            with self.subTest(argv=argv), patch('mediawrench.loudness.core.subprocess.Popen') as spawn:
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), self.no_writes():
                    with self.assertRaises(SystemExit) as caught:
                        main(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('error:', stderr.getvalue())
                spawn.assert_not_called()

    def test_signal_handlers_restored_and_worker_installs_none(self):
        handlers = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        code, _ = self.call(self.argv())
        self.assertEqual(code, 0)
        self.assertEqual([signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)], handlers)
        with patch('mediawrench.loudness.cli.signal.signal', side_effect=AssertionError('worker signal handler')), \
             ThreadPoolExecutor(max_workers=1) as pool:
            code, _ = pool.submit(self.call, self.argv()).result(timeout=60)
        self.assertEqual(code, 0)

    def test_expected_exceptions_map_without_changing_library_contract(self):
        for exc, status, code, kind in ((JobCancelled('Job cancelled'), 'ERROR', 4, 'cancelled'),
                                        (KeyboardInterrupt(), 'ERROR', 4, 'cancelled'),
                                        (DeadlineExceeded('budget spent'), 'ERROR', 4, 'deadline_exceeded'),
                                        (AudioError('unsupported', 'UNSUPPORTED'), 'UNSUPPORTED', 3, None),
                                        (PermissionError('unreadable'), 'ERROR', 4, None)):
            with self.subTest(exc=type(exc).__name__), \
                 patch('mediawrench.loudness.cli.verify_candidate', side_effect=exc):
                actual, result = self.call(self.argv())
                self.assertEqual(actual, code)
                self.assert_error(result, status)
                self.assertEqual(result.get('error_kind'), kind)
        with patch('mediawrench.loudness.cli.verify_candidate', side_effect=RuntimeError('bug')):
            with self.assertRaisesRegex(RuntimeError, 'bug'):
                self.call(self.argv())

    def wrapper(self, name, executable, delay=10):
        path = self.root / name
        path.write_text(f'#!{sys.executable}\nimport os, sys, time\nfrom pathlib import Path\n'
                        f'Path({str(self.root / "ready")!r}).write_text(str(os.getpid()))\n'
                        f'time.sleep({delay!r})\nos.execv({executable!r}, [{executable!r}, *sys.argv[1:]])\n')
        path.chmod(0o755)
        return str(path)

    def delayed_cli(self, argv, sig=None):
        ready = self.root / 'ready'
        ready.unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'mediawrench.loudness', *argv], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 30
            while not ready.exists() and proc.poll() is None and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'must enter delayed live tool')
            if sig:
                proc.send_signal(sig)
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 4, stdout + stderr)
            self.assertEqual(stderr, '')
            result = json.loads(stdout)
            self.assert_error(result)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(ready.read_text()), 0)
            return result
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_sigint_sigterm_reap_probe_and_measurement_without_writes(self):
        for stage, flag, executable in (('probe', '--ffprobe', self.ffprobe),
                                         ('measurement', '--ffmpeg', self.ffmpeg)):
            wrapper = self.wrapper(stage, executable)
            for sig in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(stage=stage, sig=sig):
                    result = self.delayed_cli(self.argv(None, flag, wrapper), sig)
                    self.assertEqual(result['error_kind'], 'cancelled')
                    self.assertEqual(result['error'], 'Job cancelled')

    def test_timeout_and_shared_deadline_reap_both_tools(self):
        for flag, executable in (('--ffprobe', self.ffprobe), ('--ffmpeg', self.ffmpeg)):
            wrapper = self.wrapper(flag[2:], executable)
            for budget_flag, kind in (('--timeout', None), ('--total-timeout', 'deadline_exceeded')):
                with self.subTest(stage=flag, budget=budget_flag):
                    # Generous budget: the real probe and Python cold starts must fit first on slow runners.
                    result = self.delayed_cli(self.argv(None, flag, wrapper, budget_flag, '2.5'))
                    self.assertEqual(result.get('error_kind'), kind)
                    if kind is None:
                        self.assertIn('Subprocess timed out', result['error'])


if __name__ == '__main__':
    unittest.main()
