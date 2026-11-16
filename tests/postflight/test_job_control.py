"""Local executable fixtures exercise real waits, signals and process-group cleanup."""
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

from mediawrench.postflight import Options, compare

ROOT = Path(__file__).resolve().parents[2]


def running(pid):
    try:
        # An orphan awaiting init's reap cannot execute or retain open files.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def wait_for_exit(pid):
    # SIGKILL delivery to a descendant is asynchronous; allow the kernel to
    # schedule it before asserting liveness. This still fails a surviving child.
    end = time.monotonic() + 1
    while running(pid) and time.monotonic() < end:
        time.sleep(0.01)
    return not running(pid)


@unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux process-group integration")
class JobControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="postflight-budget-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.source, self.output = self.root / "source.mkv", self.root / "output.mkv"
        self.source.write_bytes(b"local fixture input")
        self.output.write_bytes(b"local fixture output")
        self.pids = self.root / "pids.json"
        self.report = self.root / "report.json"
        self.tool = self.root / "fixture-tool"
        self.addCleanup(self.kill_fixture)

    def kill_fixture(self):
        if self.pids.exists():
            for pid in json.loads(self.pids.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def fixture(self, probe_delay=0, hang_decode=False, version_delay=0):
        # Headers and frame records follow the actual ffprobe JSON / framehash
        # formats. Delays/descendants are infrastructure fixtures, not media E2E.
        self.tool.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys, time
if '-version' in sys.argv:
    time.sleep({version_delay!r})
    print('local job-control fixture')
elif '-show_entries' in sys.argv:
    print('probe diagnostic before wait', file=sys.stderr, flush=True)
    time.sleep({probe_delay!r})
    print(json.dumps({{"streams": [{{"index": 0, "codec_type": "video"}}, {{"index": 1, "codec_type": "audio"}}], "format": {{"duration": "1"}}}}))
else:
    if {hang_decode!r} and sys.argv[sys.argv.index('-map') + 1] == '0:1':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        with open({str(self.pids) + '.tmp'!r}, 'w') as handle:
            json.dump([os.getpid(), child.pid], handle)
        os.replace({str(self.pids) + '.tmp'!r}, {str(self.pids)!r})
        print('decode diagnostic before wait', file=sys.stderr, flush=True)
        child.wait()
    print('#tb 0: 1/1\\n0,0,0,1,4,00000000')
''')
        self.tool.chmod(0o755)

    def command(self, *extra):
        return [sys.executable, "-m", "mediawrench.postflight", str(self.source), str(self.output),
                "--ffprobe", str(self.tool), "--ffmpeg", str(self.tool),
                "--timeout", "2", "--report", str(self.report), *extra]

    def assert_stopped(self, result, reason):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(self.report.exists(), result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(json.loads(result.stdout), report)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(report["termination"]["reason"], reason)
        self.assertEqual(self.source.read_bytes(), b"local fixture input")
        self.assertEqual(self.output.read_bytes(), b"local fixture output")
        self.assertFalse(list(self.spool.iterdir()))
        self.assertFalse(list(self.root.glob(".postflight-*.tmp")))
        if self.pids.exists():
            for pid in json.loads(self.pids.read_text()):
                self.assertTrue(wait_for_exit(pid), f"fixture process {pid} survived")
        return report

    def test_two_probes_share_one_job_deadline(self):
        self.fixture(probe_delay=0.65)
        start = time.monotonic()
        result = subprocess.run(self.command("--deadline", "1"), cwd=ROOT,
                                env=dict(os.environ, TMPDIR=str(self.spool)),
                                capture_output=True, text=True, timeout=120)
        self.assertLess(time.monotonic() - start, 2.5)
        report = self.assert_stopped(result, "deadline")
        probes = [c for c in report["commands"] if "-show_entries" in c["argv"]]
        self.assertEqual(len(probes), 2)
        self.assertEqual(probes[0]["returncode"], 0)
        self.assertEqual(probes[1]["stop_reason"], "deadline")
        self.assertIn("probe diagnostic before wait", probes[1]["stderr_excerpt"])
        self.assertTrue(report["progress"]["source"]["probe_completed"])
        self.assertFalse(report["progress"]["output"]["probe_completed"])
        self.assertEqual(report["progress"]["source"]["pending_streams"], [0, 1])

    def test_deadline_during_decode_preserves_completed_coverage(self):
        self.fixture(hang_decode=True)
        result = subprocess.run(self.command("--deadline", "1.2"), cwd=ROOT,
                                env=dict(os.environ, TMPDIR=str(self.spool)),
                                capture_output=True, text=True, timeout=120)
        report = self.assert_stopped(result, "deadline")
        self.assertTrue(self.pids.exists(), "decode was never reached")
        self.assertEqual(report["progress"]["source"]["completed_streams"], [0])
        self.assertEqual(report["progress"]["source"]["pending_streams"], [1])
        self.assertEqual(report["decoded"]["source"]["0"]["frames"], 1)
        self.assertIn("decode diagnostic before wait", report["commands"][-1]["stderr_excerpt"])
        self.assertEqual(report["mapping"]["status"], "PASS")
        completed, pending = report["stream_validation"]["source"]
        self.assertEqual(completed["decode_coverage"], "full")
        self.assertEqual(completed["duration_status"], "not_run")
        self.assertEqual(pending["decode_coverage"], "none")
        self.assertEqual(pending["decode_status"], "not_run")
        self.assertEqual(pending["peer_stream"], 1)

    def test_cli_signals_preserve_report_and_kill_descendants(self):
        self.fixture(hang_decode=True)
        for sig, library in ((signal.SIGINT, False), (signal.SIGTERM, False), (signal.SIGINT, True)):
            with self.subTest(signal=sig, library=library):
                self.pids.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                command = self.command()
                if library:
                    # Exercise Python's normal KeyboardInterrupt path as well as
                    # the CLI's cooperative signal handlers.
                    command = [sys.executable, "-c",
                               "import json, sys; from mediawrench.postflight import Options, compare, exit_code, write_report; "
                               "r=compare(sys.argv[1], sys.argv[2], Options(ffprobe=sys.argv[3], ffmpeg=sys.argv[3], timeout=2)); "
                               "write_report(r, sys.argv[4], sys.argv[1], sys.argv[2]); print(json.dumps(r)); sys.exit(exit_code(r))",
                               str(self.source), str(self.output), str(self.tool), str(self.report)]
                proc = subprocess.Popen(command, cwd=ROOT,
                                        env=dict(os.environ, TMPDIR=str(self.spool)),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    end = time.monotonic() + 5
                    while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                        time.sleep(0.01)
                    self.assertTrue(self.pids.exists(), "decode was never reached")
                    proc.send_signal(sig)
                    stdout, stderr = proc.communicate(timeout=60)
                    report = self.assert_stopped(subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr), "cancelled")
                    self.assertEqual(report["progress"]["source"]["completed_streams"], [0])
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                    self.kill_fixture()

    def test_library_pre_cancelled_job_starts_no_subprocess(self):
        self.fixture()
        cancelled = threading.Event()
        cancelled.set()
        report = compare(self.source, self.output,
                         Options(ffprobe=str(self.tool), ffmpeg=str(self.tool)), cancel_event=cancelled)
        self.assertEqual(report["termination"]["reason"], "cancelled")
        self.assertEqual(report["commands"], [])
        self.assertEqual(report["coverage"], "partial")

    def test_tool_versions_also_share_job_deadline(self):
        self.fixture(version_delay=0.65)
        result = subprocess.run(self.command("--deadline", "1"), cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        report = self.assert_stopped(result, "deadline")
        self.assertEqual(len(report["commands"]), 2)
        self.assertTrue(all("-version" in c["argv"] for c in report["commands"]))
        self.assertEqual(report["commands"][-1]["stop_reason"], "deadline")

    def test_library_active_event_cancellation(self):
        self.fixture(hang_decode=True)
        cancelled = threading.Event()

        def cancel_when_decoding():
            end = time.monotonic() + 5
            while not self.pids.exists() and time.monotonic() < end:
                time.sleep(0.01)
            cancelled.set()

        thread = threading.Thread(target=cancel_when_decoding)
        thread.start()
        try:
            report = compare(self.source, self.output,
                             Options(ffprobe=str(self.tool), ffmpeg=str(self.tool), timeout=2),
                             cancel_event=cancelled)
        finally:
            thread.join(timeout=6)
        self.assertTrue(self.pids.exists(), "decode was never reached")
        self.assertEqual(report["termination"]["reason"], "cancelled")
        self.assertEqual(report["progress"]["source"]["completed_streams"], [0])
        self.assertEqual(report["coverage"], "partial")
        for pid in json.loads(self.pids.read_text()):
            self.assertTrue(wait_for_exit(pid), f"fixture process {pid} survived")

    def test_invalid_deadlines_are_json_unknown(self):
        self.fixture()
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                result = subprocess.run(self.command("--deadline", value), cwd=ROOT,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 2)
                report = json.loads(result.stdout)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(report["commands"], [])
                self.assertEqual(report["coverage"], "partial")


if __name__ == "__main__":
    unittest.main()
