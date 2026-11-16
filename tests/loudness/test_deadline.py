"""Synthetic real-media tests with local executable delay/signal injection."""
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

from examples.loudness.synthetic import write_tone
from mediawrench.loudness import Profile, Toolchain, normalize, verify_candidate
from mediawrench.loudness.core import sha256
from .tools import FFMPEG, FFPROBE, requires_ffmpeg

ROOT = Path(__file__).resolve().parents[2]


@requires_ffmpeg
class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = ROOT / ".tools/test-tmp"
        cls.scratch.mkdir(parents=True, exist_ok=True)
        cls.fixtures = tempfile.TemporaryDirectory(dir=cls.scratch)
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

    def wrapper(self, stage, delay=5, baseline=0):
        wrapper = self.root / "ffmpeg-delay"
        # exec retains the PID: a timeout/cancel must reap this actual process.
        wrapper.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
a = sys.argv[1:]
stage = None
if "-af" in a:
    stage = "encoding" if "pcm_s24le" in a else ("verification" if Path(a[a.index("-i")+1]).name == "candidate.wav" else "analysis")
if stage:
    time.sleep({baseline!r})
if stage == {stage!r}:
    Path({str(self.root / "ready")!r}).write_text(str(os.getpid()))
    time.sleep({delay!r})
os.execv({self.ffmpeg!r}, [{self.ffmpeg!r}, *a])
''')
        wrapper.chmod(0o755)
        return str(wrapper)

    def argv(self, ffmpeg, budget=10):
        return [sys.executable, "-m", "mediawrench.loudness", str(self.source), str(self.output),
                "--report", str(self.report), "--target-i", "-16", "--target-tp", "-1.5",
                "--tolerance", "0.5", "--ffmpeg", ffmpeg, "--ffprobe", self.ffprobe,
                "--timeout", "20", "--total-timeout", str(budget)]

    def assert_clean(self, result):
        self.assertEqual(json.loads(self.report.read_text()), result)
        self.assertFalse(list(self.root.glob(".loudnorm-*")))
        self.assertEqual(sha256(self.source), self.source_hash)
        timings = result["timings"]
        self.assertEqual(timings["unit"], "milliseconds")
        self.assertEqual(sum(timings["stages"].values()), timings["total"])
        self.assertTrue(all(x >= 0 for x in timings["stages"].values()))
        ready = self.root / "ready"
        if ready.exists():
            with self.assertRaises(ProcessLookupError):
                os.kill(int(ready.read_text()), 0)

    def test_slow_analysis_encode_and_final_measurement_share_one_budget(self):
        for stage in ("analysis", "encoding", "verification"):
            with self.subTest(stage=stage):
                started = time.monotonic()
                # Scaled-up budget: each delayed stage must be reachable before it expires on slow runners.
                run = subprocess.run(self.argv(self.wrapper(stage, delay=30, baseline=0.2), budget=6),
                                     cwd=ROOT, capture_output=True, text=True, timeout=120)
                elapsed = time.monotonic() - started
                self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
                result = json.loads(run.stdout)
                self.assertEqual(result["error_kind"], "deadline_exceeded")
                self.assertEqual(result["failed_stage"], stage)
                self.assertTrue((self.root / "ready").exists(), "must actually reach delayed stage")
                self.assertGreaterEqual(elapsed, 5.8)
                self.assertLess(elapsed, 10, "must use remaining budget rather than a new 6s allowance")
                self.assertFalse(result["published"])
                self.assertFalse(self.output.exists())
                self.assertLess(result["timings"]["total"], 7000,
                                "later passes must not receive a fresh budget")
                self.assert_clean(result)
                self.report.unlink()
                (self.root / "ready").unlink()

    def test_normal_short_audio_and_exact_timing_sum(self):
        run = subprocess.run(self.argv(self.ffmpeg), cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(verify_candidate(self.output, self.profile,
                                        Toolchain(self.ffmpeg, self.ffprobe))["status"], "PASS")
        self.assertTrue(result["published"])
        self.assert_clean(result)

    def test_sigint_and_sigterm_during_processing_clean_and_report(self):
        for sig, stage in ((signal.SIGINT, "encoding"), (signal.SIGTERM, "verification")):
            with self.subTest(signal=sig):
                proc = subprocess.Popen(self.argv(self.wrapper(stage)), cwd=ROOT,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    until = time.monotonic() + 30
                    while not (self.root / "ready").exists() and proc.poll() is None and time.monotonic() < until:
                        time.sleep(0.01)
                    self.assertTrue((self.root / "ready").exists())
                    proc.send_signal(sig)
                    stdout, stderr = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, 4, stdout + stderr)
                    result = json.loads(stdout)
                    self.assertEqual(result["error_kind"], "cancelled")
                    self.assertEqual(result["failed_stage"], stage)
                    self.assertFalse(self.output.exists())
                    self.assert_clean(result)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                self.report.unlink()
                (self.root / "ready").unlink()

    def test_existing_output_survives_cancellation_or_timeout_configuration(self):
        self.output.write_bytes(b"old audio")
        run = subprocess.run(self.argv(self.wrapper("analysis"), budget=0.01), cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
        self.assertEqual(self.output.read_bytes(), b"old audio")
        self.assertEqual(json.loads(run.stdout)["commands"], [])
        self.assert_clean(json.loads(run.stdout))

    def test_keyboard_interrupt_after_audio_link_rolls_back(self):
        real_link = os.link
        def interrupt_link(source, destination, *args, **kwargs):
            real_link(source, destination, *args, **kwargs)
            if Path(destination) == self.output:
                raise KeyboardInterrupt
        with patch("mediawrench.loudness.core.os.link", interrupt_link):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe))
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_deadline_in_report_preparation_rolls_back_audio_and_retains_error(self):
        real_fsync = os.fsync
        def slow_fsync(fd):
            time.sleep(6.2)
            real_fsync(fd)
        with patch("mediawrench.loudness.core.os.fsync", slow_fsync):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe, total_timeout=6))
        self.assertEqual(result["error_kind"], "deadline_exceeded")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_interrupt_after_report_link_rolls_back_both_publications(self):
        real_link = os.link
        interrupted = False
        def interrupt_link(source, destination, *args, **kwargs):
            nonlocal interrupted
            real_link(source, destination, *args, **kwargs)
            if Path(destination) == self.report and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
        with patch("mediawrench.loudness.core.os.link", interrupt_link):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe))
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_cancel_during_directory_cleanup_retries_removal(self):
        cleanup = tempfile.TemporaryDirectory.cleanup
        interrupted = False
        def interrupt_cleanup(work):
            nonlocal interrupted
            if Path(work.name).name.startswith(".loudnorm-") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            cleanup(work)
        with patch.object(tempfile.TemporaryDirectory, "cleanup", interrupt_cleanup):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe))
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_cancel_after_report_temp_unlink_retains_failure_report(self):
        real_unlink = Path.unlink
        interrupted = False
        def interrupt_unlink(path, *args, **kwargs):
            nonlocal interrupted
            real_unlink(path, *args, **kwargs)
            if path.name.startswith(".loudnorm-report-") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
        with patch.object(Path, "unlink", interrupt_unlink):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe))
        self.assertEqual(result["error_kind"], "cancelled")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_expiry_inside_audio_link_rolls_back_only_own_inode(self):
        real_link = os.link
        def slow_link(source, destination, *args, **kwargs):
            real_link(source, destination, *args, **kwargs)
            if Path(destination) == self.output:
                time.sleep(6.2)
        # The real passes must finish inside the budget so the expiry lands in publication.
        with patch("mediawrench.loudness.core.os.link", slow_link):
            result = normalize(self.source, self.output, self.profile, self.report,
                               Toolchain(self.ffmpeg, self.ffprobe, total_timeout=6))
        self.assertEqual(result["error_kind"], "deadline_exceeded")
        self.assertEqual(result["failed_stage"], "publication")
        self.assertFalse(self.output.exists())
        self.assert_clean(result)

    def test_shorter_per_process_cap_is_still_enforced(self):
        result = normalize(self.source, self.output, self.profile, self.report,
                           Toolchain(self.wrapper("analysis"), self.ffprobe, timeout=0.3, total_timeout=10))
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("Subprocess timed out after 0.3s", result["error"])
        self.assertFalse(self.output.exists())
        self.assert_clean(result)


if __name__ == "__main__":
    unittest.main()
