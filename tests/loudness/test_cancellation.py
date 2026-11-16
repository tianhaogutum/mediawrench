"""Worker-thread Event cancellation with real media and controlled I/O boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from examples.loudness.synthetic import write_tone
from mediawrench.loudness import Profile, Toolchain, normalize
from mediawrench.loudness.core import Runner, sha256
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class CancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / ".tools/test-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(dir=scratch)
        cls.source = Path(cls.fixtures.name) / "tone.wav"
        write_tone(cls.source, 6)
        cls.source_hash = sha256(cls.source)
        cls.ffmpeg = FFMPEG
        cls.ffprobe = FFPROBE
        cls.profile = Profile(-16, -1.5, 0.5)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.fixtures.name)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.output = self.root / "output.wav"
        self.report = self.root / "report.json"
        self.event = threading.Event()

    def normalize(self, **kwargs):
        self.assertIsNot(threading.current_thread(), threading.main_thread())
        return normalize(self.source, self.output, self.profile, self.report,
                         kwargs.pop("tools", Toolchain(self.ffmpeg, self.ffprobe)),
                         cancel_event=self.event, **kwargs)

    def worker(self, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self.normalize, **kwargs).result(timeout=60)

    def assert_cancelled(self, result, stage):
        self.assertEqual(result["status"], "ERROR", result)
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertEqual(result["error"], "Job cancelled")
        self.assertEqual(result["failed_stage"], stage)
        self.assertFalse(result["published"])
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertFalse(list(self.root.glob(".loudnorm-*")))
        self.assertEqual(sha256(self.source), self.source_hash)
        self.assertTrue(self.event.is_set(), "library must not clear caller Event")
        self.assertEqual(sum(result["timings"]["stages"].values()), result["timings"]["total"])

    def test_preset_event_in_worker_skips_tools_and_retains_failure_report(self):
        self.event.set()
        with patch.object(Runner, "run", side_effect=AssertionError("must not start tools")):
            result = self.worker()
        self.assertEqual(result["commands"], [])
        self.assert_cancelled(result, "preparation")

    def wrapper(self, stage):
        wrapper = self.root / "ffmpeg-delay"
        wrapper.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
a = sys.argv[1:]
stage = "preparation" if "-version" in a else None
if "-af" in a:
    stage = "encoding" if "pcm_s24le" in a else ("verification" if Path(a[a.index("-i")+1]).name == "candidate.wav" else "analysis")
if stage == {stage!r}:
    Path({str(self.root / "ready")!r}).write_text(str(os.getpid()))
    time.sleep(5)
os.execv({self.ffmpeg!r}, [{self.ffmpeg!r}, *a])
''')
        wrapper.chmod(0o755)
        return str(wrapper)

    def test_event_cancels_live_worker_subprocess_in_each_stage(self):
        handlers = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        for stage in ("preparation", "analysis", "encoding", "verification"):
            with self.subTest(stage=stage), ThreadPoolExecutor(max_workers=1) as pool:
                self.event.clear()
                future = pool.submit(self.normalize, tools=Toolchain(self.wrapper(stage), self.ffprobe))
                try:
                    until = time.monotonic() + 30
                    while not (self.root / "ready").exists() and not future.done() and time.monotonic() < until:
                        time.sleep(0.01)
                    self.assertTrue((self.root / "ready").exists(), future.result() if future.done() else "wrapper never started")
                    started = time.monotonic()
                    self.event.set()
                    result = future.result(timeout=60)
                    self.assertLess(time.monotonic() - started, 4, "must not wait for the 5s wrapper or 120s cap")
                    self.assert_cancelled(result, stage)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((self.root / "ready").read_text()), 0)
                finally:
                    self.event.set()
                self.report.unlink()
                (self.root / "ready").unlink()
        self.assertEqual([signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)], handlers)

    def test_event_during_copy_and_hash_stops_between_blocks(self):
        for operation in ("copy", "hash"):
            with self.subTest(operation=operation):
                self.event.clear()
                ready, release = threading.Event(), threading.Event()
                reads = []
                real_open = Path.open
                event_source = self.root / "large.wav"
                event_source.write_bytes(b"x" * (3 * 1024 * 1024))
                original_hash = sha256(event_source)

                class Reader:
                    def __init__(inner, stream):
                        inner.stream = stream
                    def __enter__(inner):
                        inner.stream.__enter__()
                        return inner
                    def __exit__(inner, *args):
                        return inner.stream.__exit__(*args)
                    def read(inner, size):
                        reads.append(size)
                        block = inner.stream.read(size)
                        ready.set()
                        if not release.wait(5):
                            raise AssertionError("controller did not release reader")
                        return block

                def gated_open(path, mode="r", *args, **kwargs):
                    stream = real_open(path, mode, *args, **kwargs)
                    target = path == event_source if operation == "copy" else path.name == "source.audio"
                    return Reader(stream) if mode == "rb" and target else stream

                with patch.object(Path, "open", gated_open), ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(normalize, event_source, self.output, self.profile,
                                         self.report, Toolchain(self.ffmpeg, self.ffprobe), cancel_event=self.event)
                    try:
                        self.assertTrue(ready.wait(5))
                        self.event.set()
                    finally:
                        release.set()
                    result = future.result(timeout=60)
                self.assertEqual(reads, [1024 * 1024], "must not read a second block")
                self.assertEqual(sha256(event_source), original_hash)
                self.assert_cancelled(result, "preparation")
                self.report.unlink()
                event_source.unlink()

    def test_event_after_publication_links_and_report_temp_unlink_rolls_back(self):
        real_link, real_unlink = os.link, Path.unlink
        for boundary in ("audio-link", "report-link", "report-temp-unlink"):
            with self.subTest(boundary=boundary):
                self.event.clear()
                def cancel_link(source, destination, *args, **kwargs):
                    real_link(source, destination, *args, **kwargs)
                    if ((boundary == "audio-link" and Path(destination) == self.output) or
                            (boundary == "report-link" and Path(destination) == self.report)):
                        self.event.set()
                def cancel_unlink(path, *args, **kwargs):
                    real_unlink(path, *args, **kwargs)
                    if boundary == "report-temp-unlink" and path.name.startswith(".loudnorm-report-"):
                        self.event.set()
                with patch("mediawrench.loudness.core.os.link", cancel_link), patch.object(Path, "unlink", cancel_unlink):
                    result = self.worker()
                self.assert_cancelled(result, "publication")
                self.report.unlink()

    def test_unset_event_passes_and_remains_unset(self):
        result = self.worker()
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["published"])
        self.assertFalse(self.event.is_set())
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertEqual(sha256(self.source), self.source_hash)
        self.assertFalse(list(self.root.glob(".loudnorm-*")))

    def test_invalid_event_returns_error_before_tools(self):
        for value in (False, True, "cancel", object()):
            with self.subTest(value=value):
                with patch.object(Runner, "run", side_effect=AssertionError("must not run tools")):
                    result = normalize(self.source, self.output, self.profile, self.report,
                                       cancel_event=value)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn("threading.Event or None", result["error"])
                self.assertEqual(result["commands"], [])
                self.assertFalse(self.output.exists())
                self.assertEqual(json.loads(self.report.read_text()), result)
                self.report.unlink()

    def test_preset_event_preserves_existing_output_and_unsafe_reports(self):
        self.event.set()
        self.output.write_bytes(b"existing audio")
        result = self.worker()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["commands"], [])
        self.assertEqual(self.output.read_bytes(), b"existing audio")
        self.report.unlink()
        self.output.unlink()
        for destination in (self.report, self.source):
            with self.subTest(report=destination):
                if destination == self.report:
                    self.report.write_bytes(b"existing report")
                before = destination.read_bytes()
                result = normalize(self.source, self.output, self.profile, destination, cancel_event=self.event)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn("report_error", result)
                self.assertEqual(result["commands"], [])
                self.assertFalse(self.output.exists())
                self.assertEqual(destination.read_bytes(), before)

    def test_poll_retries_keep_process_and_total_deadlines(self):
        # Bounds are generous: Python/FFmpeg cold starts on CI runners (notably macOS) are slow.
        for cap, budget, kind in ((1.5, 20, None), (20, 3, "deadline_exceeded")):
            with self.subTest(timeout=cap, total_timeout=budget):
                started = time.monotonic()
                result = self.worker(tools=Toolchain(self.wrapper("analysis"), self.ffprobe, cap, budget))
                self.assertLess(time.monotonic() - started, 4.5)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result.get("error_kind"), kind)
                if kind is None:
                    self.assertIn("Subprocess timed out after 1.5s", result["error"])
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".loudnorm-*")))
                self.assertEqual(json.loads(self.report.read_text()), result)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((self.root / "ready").read_text()), 0)
                self.report.unlink()
                (self.root / "ready").unlink()


if __name__ == "__main__":
    unittest.main()
