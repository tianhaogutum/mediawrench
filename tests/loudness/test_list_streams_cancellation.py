"""Worker Event cancellation for read-only inventories of real local Matroska."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import io
import json
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

from mediawrench.loudness import Toolchain, list_streams
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class ListStreamsCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = FFMPEG
        cls.ffprobe = FFPROBE
        scratch = ROOT / ".tools/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(prefix="inventory-event-", dir=scratch)
        cls.fixture = Path(cls.fixtures.name) / "tagged.mka"
        subprocess.run([cls.ffmpeg, "-v", "error", "-nostdin", "-threads", "1",
                        "-filter_threads", "1", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1:sample_rate=48000",
                        "-c:a", "pcm_s16le", "-metadata:s:a:0", "language=jpn",
                        "-metadata:s:a:0", "title=日本語 — café 🎧", "-disposition:a:0", "forced",
                        "-n", str(cls.fixture)], capture_output=True, check=True, timeout=120)
        cls.source_bytes = cls.fixture.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=self.fixtures.name)
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.protected = self.root / "read-only"
        self.protected.mkdir()
        self.source = self.protected / "tagged.mka"
        self.source.write_bytes(self.source_bytes)
        self.source.chmod(0o444)
        self.protected.chmod(0o555)
        self.addCleanup(self.protected.chmod, 0o755)
        self.event = threading.Event()
        self.tools = Toolchain(ffmpeg="/must-not-run", ffprobe=self.ffprobe)

    @contextmanager
    def read_only(self):
        """Deny Python writes, including staging/report attempts, while probing."""
        real_open = io.open
        real_os_open = os.open

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
            for name in ("mkdir", "remove", "unlink", "rmdir", "rename", "replace", "link", "symlink"):
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
        return list_streams(self.source, kwargs.pop("tools", self.tools),
                            cancel_event=kwargs.pop("cancel_event", self.event), **kwargs)

    def assert_cancelled(self, result):
        self.assertEqual(result["status"], "ERROR", result)
        self.assertNotIn("errno", result)
        self.assertNotIn("report_errno", result)
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertEqual(result["error"], "Job cancelled")
        self.assertFalse(result["published"])
        self.assertNotIn("streams", result)
        self.assertNotIn("format", result)
        self.assertTrue(self.event.is_set(), "must not clear caller Event")

    def test_preset_event_in_worker_starts_no_process_and_writes_nothing(self):
        self.event.set()
        with self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool, \
             patch.object(Path, "is_file", side_effect=AssertionError("preset must skip source probe")):
            result = pool.submit(self.worker).result(timeout=60)
        self.assert_cancelled(result)
        self.assertEqual(result["commands"], [])
        spawn.assert_not_called()

    def wrapper(self):
        probe = self.root / "ffprobe-delay"
        probe.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
Path({str(self.root / "ready")!r}).write_text(str(os.getpid()))
time.sleep(10)
os.execv({self.ffprobe!r}, [{self.ffprobe!r}, *sys.argv[1:]])
''')
        probe.chmod(0o755)
        return str(probe)

    def wait_ready(self, future):
        until = time.monotonic() + 30
        while not (self.root / "ready").exists() and not future.done() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue((self.root / "ready").exists(),
                        future.result() if future.done() else "probe never started")

    def assert_reaped(self):
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "ready").read_text()), 0)

    def test_event_cancels_live_probe_in_worker_and_reaps_before_return(self):
        tools = Toolchain(ffmpeg="/must-not-run", ffprobe=self.wrapper())
        handlers = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        with self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.worker, tools=tools)
            try:
                self.wait_ready(future)
                started = time.monotonic()
                self.event.set()
                result = future.result(timeout=60)
                self.assertLess(time.monotonic() - started, 4)
                self.assert_cancelled(result)
                self.assert_reaped()
            finally:
                self.event.set()
        spawn.assert_called_once()
        self.assertEqual(result["commands"], [spawn.call_args.args[0]])
        self.assertEqual([signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)], handlers)

    def test_invalid_tokens_fail_before_tools_or_source_probe(self):
        class DuckEvent:
            def is_set(self):
                raise AssertionError("must not call duck-typed Event")

        for token in (True, False, 1, "cancel", object(), DuckEvent(), threading.Condition()):
            with self.subTest(token=type(token).__name__), self.read_only() as spawn, \
                 patch.object(Path, "is_file", side_effect=AssertionError("must validate token first")):
                result = list_streams(self.source, Toolchain(timeout=0), cancel_event=token)
            self.assertEqual(result["status"], "ERROR")
            self.assertIn("threading.Event or None", result["error"])
            self.assertNotIn("error_kind", result)
            self.assertFalse(result["published"])
            self.assertEqual(result["commands"], [])
            spawn.assert_not_called()

    def test_none_and_unset_event_preserve_exact_inventory(self):
        with self.read_only() as spawn:
            baseline = list_streams(self.source, self.tools)
            with ThreadPoolExecutor(max_workers=1) as pool:
                for token in (None, self.event):
                    self.assertEqual(pool.submit(self.worker, cancel_event=token).result(timeout=60), baseline)
        self.assertEqual(baseline["status"], "OK", baseline)
        row = baseline["streams"][0]
        self.assertEqual(row["title"], "日本語 — café 🎧")
        self.assertEqual(row["language"], "jpn")
        self.assertTrue(row["forced"])
        self.assertEqual(spawn.call_count, 3)
        self.assertFalse(self.event.is_set())

    def test_unset_event_preserves_budget_validation_before_processes(self):
        for field, values in (("timeout", (0, -1, float("nan"), float("inf"), 3601)),
                              ("total_timeout", (0, -1, float("nan"), float("inf"), 86401))):
            for value in values:
                with self.subTest(field=field, value=value), self.read_only() as spawn:
                    result = list_streams(self.source, Toolchain(**{field: value}), cancel_event=self.event)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn(field, result["error"])
                self.assertEqual(result["commands"], [])
                spawn.assert_not_called()

    def test_event_after_probe_before_inventory_completion_discards_rows(self):
        loads = json.loads
        def cancel_after_parse(raw):
            info = loads(raw)
            self.event.set()
            return info
        with self.read_only() as spawn, patch("mediawrench.loudness.core.json.loads", cancel_after_parse):
            result = list_streams(self.source, self.tools, cancel_event=self.event)
        self.assert_cancelled(result)
        spawn.assert_called_once()
        self.assertEqual(result["commands"], [spawn.call_args.args[0]])

    def test_unset_event_keeps_process_and_total_timeout_caps(self):
        probe = self.wrapper()
        # Bounds are generous: Python/FFmpeg cold starts on CI runners (notably macOS) are slow.
        for cap, budget, kind in ((1.5, 20, None), (20, 2, "deadline_exceeded")):
            with self.subTest(timeout=cap, total_timeout=budget):
                started = time.monotonic()
                with self.read_only() as spawn, ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.worker, tools=Toolchain(ffprobe=probe, timeout=cap,
                                                                      total_timeout=budget))
                    self.wait_ready(future)
                    result = future.result(timeout=60)
                    self.assert_reaped()
                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(result["status"], "ERROR", result)
                self.assertFalse(result["published"])
                self.assertEqual(result.get("error_kind"), kind)
                self.assertNotIn("errno", result)
                self.assertNotIn("report_errno", result)
                if kind is None:
                    self.assertIn("Subprocess timed out after 1.5s", result["error"])
                spawn.assert_called_once()
                self.assertEqual(result["commands"], [spawn.call_args.args[0]])
                self.assertFalse(self.event.is_set())
                (self.root / "ready").unlink()


if __name__ == "__main__":
    unittest.main()
