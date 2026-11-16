"""Read-only candidate verification with caller-owned worker cancellation Events."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mediawrench.loudness import Profile, Toolchain, verify_candidate
from mediawrench.loudness.core import AudioError, DeadlineExceeded, JobCancelled
import mediawrench.loudness.core as core
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class VerifyCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG
        cls.ffprobe = FFPROBE
        scratch = ROOT / ".tools/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix="verify-event-", dir=scratch)
        cls.tone = Path(cls.fixtures.name) / "tone.wav"
        cls.silence = Path(cls.fixtures.name) / "silence.wav"
        cls.unsupported = Path(cls.fixtures.name) / "16bit.wav"
        for path, source, codec in ((cls.tone, "sine=frequency=440:duration=4:sample_rate=48000", "pcm_s24le"),
                                    (cls.silence, "anullsrc=r=48000:cl=mono:d=4", "pcm_s24le"),
                                    (cls.unsupported, "sine=duration=4:sample_rate=48000", "pcm_s16le")):
            subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-threads", "1",
                            "-filter_threads", "1", "-f", "lavfi", "-i", source,
                            "-c:a", codec, "-n", str(path)], capture_output=True, check=True, timeout=120)
        cls.source_bytes = cls.tone.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=self.fixtures.name)
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.protected = self.root / "read-only"
        self.protected.mkdir()
        self.source = self.protected / "候補 café.wav"
        self.source.write_bytes(self.source_bytes)
        self.source.chmod(0o444)
        self.protected.chmod(0o555)
        self.addCleanup(self.protected.chmod, 0o755)
        self.event = threading.Event()
        self.profile = Profile(-21, -1.5, 0.5)
        self.tools = Toolchain(self.ffmpeg, self.ffprobe)

    @contextmanager
    def read_only(self):
        """Reject any Python file write/staging/publication attempt during the API call."""
        real_open, real_os_open = io.open, os.open

        def open_read(path, mode="r", *args, **kwargs):
            self.assertFalse(any(flag in mode for flag in "wax+"), (path, mode))
            return real_open(path, mode, *args, **kwargs)

        def os_open_read(path, flags, *args, **kwargs):
            if path != os.devnull:
                self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return real_os_open(path, flags, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch("io.open", open_read))
            stack.enter_context(patch("builtins.open", open_read))
            stack.enter_context(patch("os.open", os_open_read))
            for name in ("mkdir", "remove", "unlink", "rmdir", "rename", "replace", "link", "symlink", "chmod"):
                stack.enter_context(patch(f"os.{name}", side_effect=AssertionError(f"file mutation: {name}")))
            for name in ("TemporaryDirectory", "mkstemp"):
                stack.enter_context(patch(f"mediawrench.loudness.core.tempfile.{name}",
                                          side_effect=AssertionError(f"file creation: {name}")))
            spawn = stack.enter_context(patch("mediawrench.loudness.core.subprocess.Popen", wraps=subprocess.Popen))
            yield spawn
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertEqual(list(self.protected.iterdir()), [self.source])
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o444)
        self.assertEqual(self.protected.stat().st_mode & 0o777, 0o555)

    def worker(self, **kwargs):
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        return verify_candidate(kwargs.pop("path", self.source), kwargs.pop("profile", self.profile),
                                kwargs.pop("tools", self.tools),
                                cancel_event=kwargs.pop("cancel_event", self.event), **kwargs)

    def wrapper(self, name, executable, delay=10):
        probe = self.root / name
        probe.write_text(f"""#!{sys.executable}
import os, sys, time
from pathlib import Path
Path({str(self.root / 'ready')!r}).write_text(str(os.getpid()))
time.sleep({delay!r})
os.execv({executable!r}, [{executable!r}, *sys.argv[1:]])
""")
        probe.chmod(0o755)
        return str(probe)

    def wait_ready(self, future):
        until = time.monotonic() + 30
        while not (self.root / "ready").exists() and not future.done() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue((self.root / "ready").exists(),
                        future.result() if future.done() else "tool never started")

    def assert_reaped(self):
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "ready").read_text()), 0)

    def test_preset_event_skips_resolution_and_subprocesses(self):
        stop = threading.Event()
        stop.set()
        with patch.object(Path, "resolve", side_effect=AssertionError("must skip resolution")), \
             patch("mediawrench.loudness.core.subprocess.Popen", side_effect=AssertionError("must not spawn")) as spawn, \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(verify_candidate, "missing.wav", Profile(-16, -1.5, 0.5),
                                 Toolchain(), cancel_event=stop)
            with self.assertRaisesRegex(JobCancelled, "^Job cancelled$") as caught:
                future.result(timeout=60)
        self.assertIsInstance(caught.exception, AudioError)
        self.assertEqual(caught.exception.status, "ERROR")
        self.assertTrue(stop.is_set())
        spawn.assert_not_called()

    def test_invalid_token_precedes_profile_tools_and_path(self):
        class DuckEvent:
            def is_set(self):
                raise AssertionError("must not poll duck Event")

        for token in (True, False, 1, "cancel", object(), DuckEvent(), threading.Condition()):
            with self.subTest(token=type(token).__name__), self.read_only() as spawn, \
                 patch.object(Profile, "validate", side_effect=AssertionError("token must be first")), \
                 patch.object(Path, "resolve", side_effect=AssertionError("must skip resolution")):
                with self.assertRaisesRegex(AudioError, "threading.Event or None") as caught:
                    verify_candidate(self.source, self.profile, Toolchain(timeout=0), cancel_event=token)
            self.assertNotIsInstance(caught.exception, JobCancelled)
            spawn.assert_not_called()

    def test_valid_tokens_preserve_settings_validation_before_resolution(self):
        self.event.set()
        for token in (None, threading.Event(), self.event):
            for profile, tools, diagnostic in [(Profile(-4, -1.5, 0.5), self.tools, "target_i"),
                                              (self.profile, Toolchain(timeout=0), "timeout"),
                                              (self.profile, Toolchain(total_timeout=0), "total_timeout")]:
                with self.subTest(token=token, diagnostic=diagnostic), self.read_only() as spawn, \
                     patch.object(Path, "resolve", side_effect=AssertionError("must validate first")):
                    with self.assertRaisesRegex(AudioError, diagnostic) as caught:
                        verify_candidate(self.source, profile, tools, cancel_event=token)
                self.assertNotIsInstance(caught.exception, JobCancelled)
                spawn.assert_not_called()
        for field, values in (("timeout", (-1, float("nan"), float("inf"), 3601)),
                              ("total_timeout", (-1, float("nan"), float("inf"), 86401))):
            for value in values:
                with self.subTest(field=field, value=value), self.read_only() as spawn:
                    with self.assertRaisesRegex(AudioError, field):
                        verify_candidate(self.source, self.profile, Toolchain(**{field: value}),
                                         cancel_event=threading.Event())
                spawn.assert_not_called()

    def test_none_and_unset_preserve_exact_pass_fail_results(self):
        with self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool:
            for profile, status in ((Profile(-21.75, -1.5, 0.5), "PASS"), (Profile(-16, -1.5, 0.5), "FAIL")):
                baseline = verify_candidate(self.source, profile, self.tools)
                self.assertEqual(baseline["status"], status, baseline)
                self.assertEqual(baseline["media"]["codec"], "pcm_s24le")
                for token in (None, self.event):
                    self.assertEqual(pool.submit(self.worker, profile=profile,
                                                 cancel_event=token).result(timeout=60), baseline)
        self.assertEqual(spawn.call_count, 12)
        self.assertFalse(self.event.is_set())

    def test_cancels_live_probe_and_measurement_in_worker_and_reaps(self):
        handlers = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        for stage in ("probe", "measurement"):
            self.event.clear()
            tools = (Toolchain(self.ffmpeg, self.wrapper("probe-delay", self.ffprobe)) if stage == "probe"
                     else Toolchain(self.wrapper("measure-delay", self.ffmpeg), self.ffprobe))
            with self.subTest(stage=stage), self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.worker, tools=tools)
                try:
                    self.wait_ready(future)
                    started = time.monotonic()
                    self.event.set()
                    with self.assertRaisesRegex(JobCancelled, "^Job cancelled$") as caught:
                        future.result(timeout=60)
                    self.assertEqual(caught.exception.status, "ERROR")
                    self.assertLess(time.monotonic() - started, 4)
                    self.assert_reaped()
                finally:
                    self.event.set()
            self.assertEqual(spawn.call_count, 1 if stage == "probe" else 2)
            self.assertTrue(self.event.is_set())
            (self.root / "ready").unlink()
        self.assertEqual([signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)], handlers)

    def test_cancellation_after_probe_parsing_skips_measurement(self):
        original = core.probe
        def cancel_after_probe(*args, **kwargs):
            media = original(*args, **kwargs)
            self.event.set()
            return media
        with self.read_only() as spawn, patch.object(core, "probe", cancel_after_probe), \
             patch.object(core, "measure", side_effect=AssertionError("must skip measurement")):
            with self.assertRaises(JobCancelled):
                verify_candidate(self.source, self.profile, self.tools, cancel_event=self.event)
        spawn.assert_called_once()
        self.assertTrue(self.event.is_set())

    def test_cancellation_at_final_gate_discards_result(self):
        original = core.gate
        def cancel_after_gate(*args):
            checks = original(*args)
            self.event.set()
            return checks
        with self.read_only() as spawn, patch.object(core, "gate", cancel_after_gate):
            with self.assertRaises(JobCancelled):
                verify_candidate(self.source, self.profile, self.tools, cancel_event=self.event)
        self.assertEqual(spawn.call_count, 2)
        self.assertTrue(self.event.is_set())

    def test_unset_preserves_process_caps_and_shared_deadline_in_both_tools(self):
        for stage in ("probe", "measurement"):
            delayed = self.wrapper(stage + "-delay", self.ffprobe if stage == "probe" else self.ffmpeg)
            # Bounds are generous: Python/FFmpeg cold starts on CI runners (notably macOS) are slow.
            for cap, budget, exception in ((1.5, 20, AudioError), (20, 2.5, DeadlineExceeded)):
                tools = Toolchain(self.ffmpeg if stage == "probe" else delayed,
                                  delayed if stage == "probe" else self.ffprobe, cap, budget)
                with self.subTest(stage=stage, cap=cap, budget=budget), self.read_only() as spawn, \
                     ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.worker, tools=tools)
                    self.wait_ready(future)
                    with self.assertRaises(exception) as caught:
                        future.result(timeout=60)
                    self.assert_reaped()
                self.assertNotIsInstance(caught.exception, JobCancelled)
                if exception is AudioError:
                    self.assertIn("Subprocess timed out after 1.5s", str(caught.exception))
                self.assertEqual(spawn.call_count, 1 if stage == "probe" else 2)
                self.assertFalse(self.event.is_set())
                (self.root / "ready").unlink()

    def test_probe_and_measurement_spend_one_total_budget(self):
        # Scaled-up delays: the probe must leave budget to start measurement even on slow runners.
        tools = Toolchain(self.wrapper("measure-slow", self.ffmpeg, delay=4),
                          self.wrapper("probe-slow", self.ffprobe, delay=1.2),
                          timeout=10, total_timeout=3.5)
        with self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.worker, tools=tools)
            with self.assertRaises(DeadlineExceeded):
                future.result(timeout=60)
            self.assert_reaped()
        self.assertEqual(spawn.call_count, 2, "must reach measurement with the remaining budget")
        self.assertFalse(self.event.is_set())

    def test_none_unset_preserve_measurement_and_filesystem_exceptions(self):
        cases = [(self.silence, self.tools, AudioError, "Nonfinite"),
                 (self.unsupported, self.tools, AudioError, "Candidate must be"),
                 (self.root / "missing.wav", self.tools, FileNotFoundError, "missing.wav"),
                 (self.source, Toolchain(self.ffmpeg, "/no-such/ffprobe"), AudioError, "Executable not found"),
                 (self.source, Toolchain("/no-such/ffmpeg", self.ffprobe), AudioError, "Executable not found")]
        for path, tools, exception, message in cases:
            with self.read_only():
                with self.assertRaises(exception) as baseline:
                    verify_candidate(path, self.profile, tools)
                for token in (None, self.event):
                    with self.subTest(path=path, token=token), self.assertRaisesRegex(exception, message) as caught:
                        verify_candidate(path, self.profile, tools, cancel_event=token)
                    self.assertEqual(str(caught.exception), str(baseline.exception))
                    if isinstance(caught.exception, AudioError):
                        self.assertEqual(caught.exception.status, baseline.exception.status)
        self.assertFalse(self.event.is_set())

if __name__ == "__main__":
    unittest.main()
