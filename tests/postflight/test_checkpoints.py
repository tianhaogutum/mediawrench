"""Atomic probe/stream checkpoints, including uncatchable CLI shutdown."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from mediawrench.postflight import Options, compare, exit_code, write_report
from . import test_job_control as fixtures

ROOT = fixtures.ROOT

NONFINITE_PROBES = (
    '{"streams":[{"index":2,"codec_type":"video","disposition":{"default":1e999}}],"format":{"duration":"1"}}',
    '{"streams":[{"index":2,"codec_type":"video","disposition":{"default":-1e999}}],"format":{"duration":"1"}}',
    '{"streams":[{"index":2,"codec_type":"video","disposition":{"default":NaN}}],"format":{"duration":"1"}}',
    '{"streams":[{"index":2,"codec_type":"video","disposition":{"default":Infinity}}],"format":{"duration":"1"}}',
    '{"streams":[{"index":2,"codec_type":"video","disposition":{"default":-Infinity}}],"format":{"duration":"1"}}',
    '{"streams":[{"index":2,"codec_type":"video"},{"index":9,"codec_type":"subtitle","extra":[{"nested":1e999}]}]}',
    '{"streams":[{"index":2,"codec_type":"video","tags":{"other":NaN}}]}',
    '{"streams":[{"index":2,"codec_type":"video"}],"format":{"duration":1e999}}',
    '{"streams":[{"index":2,"codec_type":"video"}],"format":{"extra":[Infinity]}}',
    '{"streams":[{"index":2,"codec_type":"video"}],"unrequested":{"nested":[-Infinity]}}',
)


class CheckpointTests(unittest.TestCase):
    setUp = fixtures.JobControlTests.setUp
    kill_fixture = fixtures.JobControlTests.kill_fixture
    fixture = fixtures.JobControlTests.fixture
    command = fixtures.JobControlTests.command

    def options(self, **kwargs):
        return Options(ffmpeg=str(self.tool), ffprobe=str(self.tool), **kwargs)

    def probe_boundary_fixture(self, block_stage="decode", invalid_role=None, invalid_probe=None,
                               output_language="eng", probe_failures=None):
        # Noncontiguous absolute AV indices plus an excluded subtitle index.
        inventory = {"streams": [{"index": 2, "codec_type": "video"},
                                  {"index": 7, "codec_type": "audio", "tags": {"language": "eng"}},
                                  {"index": 9, "codec_type": "subtitle"}],
                     "format": {"duration": "1"}}
        failures = probe_failures if probe_failures is not None else {invalid_role: invalid_probe}
        self.tool.write_text(f'''#!{sys.executable}
import json, os, sys, time
if '-version' in sys.argv:
    print('local probe-boundary fixture')
else:
    probe = '-show_entries' in sys.argv
    role = 'source' if sys.argv[-1] == {str(self.source)!r} else 'output'
    if ({block_stage!r} == 'probe' and probe and role == 'output') or ({block_stage!r} == 'decode' and not probe):
        with open({str(self.pids) + '.tmp'!r}, 'w') as handle:
            json.dump([os.getpid()], handle)
        os.replace({str(self.pids) + '.tmp'!r}, {str(self.pids)!r})
        time.sleep(60)
    if probe:
        failures = {failures!r}
        if role in failures:
            failure = failures[role]
            if failure in ('exit', 'unsupported'):
                print('invalid media' if failure == 'exit' else 'decoder not found', file=sys.stderr)
                sys.exit(1)
            print(failure)
        else:
            inventory = json.loads({json.dumps(inventory)!r})
            if role == 'output':
                inventory['streams'][1]['tags']['language'] = {output_language!r}
            print(json.dumps(inventory))
    else:
        print('#tb 0: 1/1\\n0,0,0,1,4,00000000')
''')
        self.tool.chmod(0o755)

    def assert_probe_only_snapshot(self, report, completed_roles):
        self.assertEqual(report["report_state"], "in_progress")
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(report["coverage"], "partial")
        self.assertIsNone(report["termination"])
        self.assertEqual(report["decoded"], {"source": {}, "output": {}})
        self.assertEqual(report["mapping"]["status"], "not_run")
        self.assertFalse(any("duration" in c["name"] or "decode" in c["name"] for c in report["checks"]))
        self.assertFalse(any("-map" in c["argv"] for c in report["commands"]))
        for role in ("source", "output"):
            expected = role in completed_roles
            progress = report["progress"][role]
            self.assertEqual(progress["probe_completed"], expected)
            self.assertEqual(progress["discovered_streams"], [2, 7] if expected else None)
            self.assertEqual(progress["pending_streams"], [2, 7] if expected else None)
            self.assertEqual(progress["completed_streams"], [])
            self.assertEqual(progress["attempted_streams"], [])
            if expected:
                rows = report["stream_validation"][role]
                self.assertEqual([s["stream_index"] for s in rows], [2, 7, 9])
                self.assertTrue(all(s["decode_coverage"] == "none" for s in rows))
                self.assertTrue(all(s["decode_status"] == "not_run" for s in rows[:2]))
                self.assertTrue(all(s["duration_status"] != "PASS" for s in rows))
            else:
                self.assertNotIn(role, report["media"])
                self.assertIsNone(report["stream_validation"][role])

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux signal integration")
    def test_probe_checkpoints_survive_stop_between_probes(self):
        for stage, stop in (("probe", signal.SIGKILL), ("probe", signal.SIGTERM)):
            with self.subTest(stage=stage, stop=stop):
                self.pids.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                self.probe_boundary_fixture(block_stage=stage)
                checkpoint = self.root / "checkpoint.json"
                checkpoint.write_text('{"verdict":"PASS","stale":true}')
                proc = subprocess.Popen(self.command("--checkpoint-report", str(checkpoint), "--timeout", "30",
                                        "--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"),
                                        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    end = time.monotonic() + 5
                    while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                        json.loads(checkpoint.read_text())
                        time.sleep(0.01)
                    self.assertTrue(self.pids.exists(), "fixture did not reach requested boundary")
                    before = checkpoint.read_bytes()
                    proc.send_signal(stop)
                    stdout, stderr = proc.communicate(timeout=60)
                    if stop == signal.SIGKILL:
                        self.assertEqual(proc.returncode, -signal.SIGKILL, stderr)
                        self.assertEqual(stdout, "")
                        self.assertFalse(self.report.exists())
                        self.assertEqual(checkpoint.read_bytes(), before)
                    else:
                        self.assertEqual(proc.returncode, 2, stderr)
                        final = json.loads(stdout)
                        self.assertEqual(final, json.loads(checkpoint.read_text()))
                        self.assertEqual(final, json.loads(self.report.read_text()))
                        self.assertEqual(final["termination"]["reason"], "cancelled")
                        self.assertEqual(final["report_state"], "complete")
                        self.assertEqual(final["progress"]["source"]["pending_streams"], [2, 7])
                        self.assertIsNone(final["stream_validation"]["output"])
                        for pid in json.loads(self.pids.read_text()):
                            self.assertTrue(fixtures.wait_for_exit(pid))
                    saved = json.loads(before)
                    self.assert_probe_only_snapshot(saved, {"source"})
                    self.assertFalse(any(c["name"] == "language_tag" for c in saved["checks"]))
                    self.assertEqual(saved["stream_validation"]["source"][1]["language_tag_status"], "not_run")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=60)
                    # Uncatchable shutdown cannot clean up its active tool.
                    self.kill_fixture()

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux SIGKILL integration")
    def test_sigkill_before_first_decode_retains_mapping_and_language_decisions(self):
        for language, expected in (("deu", "FAIL"), ("eng", "UNKNOWN")):
            with self.subTest(output_language=language):
                self.pids.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                self.probe_boundary_fixture(output_language=language)
                checkpoint = self.root / "checkpoint.json"
                proc = subprocess.Popen(self.command("--checkpoint-report", str(checkpoint),
                    "--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"),
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    end = time.monotonic() + 5
                    while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                        time.sleep(0.01)
                    self.assertTrue(self.pids.exists(), "first decode did not start")
                    before = checkpoint.read_bytes()
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, -signal.SIGKILL, stderr)
                    self.assertEqual(stdout, "")
                    self.assertFalse(self.report.exists())
                    self.assertEqual(checkpoint.read_bytes(), before)
                    report = json.loads(before)
                    self.assertEqual(report["mapping"]["status"], "PASS")
                    self.assertEqual([(p["source_stream"], p["output_stream"])
                                      for p in report["mapping"]["pairs"]], [(2, 2), (7, 7)])
                    self.assertEqual(report["verdict"], expected)
                    self.assertEqual(report["report_state"], "in_progress")
                    self.assertEqual(report["coverage"], "partial")
                    self.assertIsNone(report["termination"])
                    self.assertEqual(report["decoded"], {"source": {}, "output": {}})
                    tags = [c for c in report["checks"] if c["name"] == "language_tag"]
                    self.assertEqual([(c["expected"], c["actual"], c["status"]) for c in tags],
                                     [(None, None, "PASS"), ("eng", language, "FAIL" if language == "deu" else "PASS")])
                    self.assertFalse(any("duration" in c["name"] or "decode" in c["name"] for c in report["checks"]))
                    self.assertFalse(any("-map" in c["argv"] for c in report["commands"]))
                    for role in ("source", "output"):
                        self.assertTrue(report["progress"][role]["probe_completed"])
                        self.assertEqual(report["progress"][role]["pending_streams"], [2, 7])
                        self.assertEqual(report["progress"][role]["completed_streams"], [])
                        self.assertEqual(report["progress"][role]["attempted_streams"], [])
                        rows = report["stream_validation"][role]
                        self.assertTrue(all(s["decode_coverage"] == "none" for s in rows))
                        self.assertTrue(all(s["decode_status"] == "not_run" and s["duration_status"] == "not_run"
                                            for s in rows[:2]))
                        self.assertEqual(rows[1]["language_tag_status"], "FAIL" if language == "deu" else "PASS")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=60)
                    self.kill_fixture()

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux SIGKILL integration")
    def test_sigkill_retains_latest_completed_stream_checkpoint(self):
        self.fixture(hang_decode=True)
        checkpoint = self.root / "checkpoint.json"
        checkpoint.write_text('{"verdict":"PASS","stale":true}')
        proc = subprocess.Popen(self.command("--checkpoint-report", str(checkpoint), "--timeout", "30"),
                                cwd=ROOT, env=dict(os.environ, TMPDIR=str(self.spool)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            end = time.monotonic() + 5
            while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                # Every observed replacement must be a whole JSON document.
                json.loads(checkpoint.read_text())
                time.sleep(0.01)
            if not self.pids.exists():
                proc.kill()
                stdout, stderr = proc.communicate(timeout=60)
                self.fail(f"Second decode never reached: {stdout} {stderr}")
            before = checkpoint.read_bytes()
            proc.kill()
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            self.assertEqual(stdout, "")
            self.assertFalse(self.report.exists(), "final shutdown unexpectedly ran")
            self.assertEqual(checkpoint.read_bytes(), before)
            report = json.loads(before)
            self.assertEqual(report["report_state"], "in_progress")
            self.assertEqual(report["verdict"], "UNKNOWN")
            self.assertEqual(report["coverage"], "partial")
            self.assertIsNone(report["termination"])
            self.assertEqual(report["progress"]["source"]["completed_streams"], [0])
            self.assertEqual(report["progress"]["source"]["pending_streams"], [1])
            self.assertEqual(report["progress"]["output"]["pending_streams"], [0, 1])
            self.assertEqual(set(report["decoded"]["source"]), {"0"})
            self.assertEqual(report["decoded"]["source"]["0"]["frames"], 1)
            complete, pending = report["stream_validation"]["source"]
            self.assertEqual(complete["decode_coverage"], "full")
            self.assertEqual(complete["duration_status"], "not_run")
            self.assertEqual(pending["decode_coverage"], "none")
            self.assertEqual(pending["decode_status"], "not_run")
            self.assertEqual(self.source.read_bytes(), b"local fixture input")
            self.assertEqual(self.output.read_bytes(), b"local fixture output")
            self.assertFalse(list(self.root.glob(".postflight-*.tmp")))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=60)
            # SIGKILL cannot run group cleanup. Test owns orphan fixture cleanup.
            self.kill_fixture()

    def assert_failed_probe_snapshot(self, report, role, status, completed_roles=(), language_tags="preserve"):
        self.assertEqual(report["report_state"], "in_progress")
        self.assertEqual(report["verdict"], status)
        self.assertEqual(report["coverage"], "partial")
        self.assertIsNone(report["termination"])
        self.assertEqual(report["mapping"]["status"], "not_run")
        self.assertEqual(report["mapping"]["pairs"], [])
        self.assertEqual(report["decoded"], {"source": {}, "output": {}})
        checks = [c for c in report["checks"] if c["name"] == "probe" and c["role"] == role]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], status)
        self.assertFalse(any("duration" in c["name"] or "decode" in c["name"] or
                             c["name"] == "language_tag" for c in report["checks"]))
        self.assertFalse(any("-map" in c["argv"] for c in report["commands"]))
        probes = [c for c in report["commands"] if "-show_entries" in c["argv"]]
        self.assertEqual(len(probes), 1 if role == "source" else 2)
        self.assertEqual(probes[-1]["argv"][-1], str(getattr(self, role)))
        self.assertIn("returncode", probes[-1])
        self.assertIn("elapsed_seconds", probes[-1])
        if "stderr_excerpt" in probes[-1]:
            self.assertEqual(checks[0]["actual"], probes[-1]["stderr_excerpt"])
        for side in ("source", "output"):
            progress = report["progress"][side]
            known = side in completed_roles
            self.assertEqual(progress["probe_completed"], known)
            self.assertEqual(progress["discovered_streams"], [2, 7] if known else None)
            self.assertEqual(progress["pending_streams"], [2, 7] if known else None)
            self.assertEqual(progress["attempted_streams"], [])
            self.assertEqual(progress["completed_streams"], [])
            if known:
                rows = report["stream_validation"][side]
                self.assertTrue(all(s["decode_coverage"] == "none" and
                                    s["language_tag_status"] == ("not_run" if language_tags == "preserve" else "not_required")
                                    for s in rows[:2]))
            else:
                self.assertNotIn(side, report["media"])
                self.assertIsNone(report["stream_validation"][side])

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux SIGKILL integration")
    def test_sigkill_after_failed_probe_retains_diagnostic_before_next_tool(self):
        cases = (("source", "exit", "FAIL"), ("source", "unsupported", "UNKNOWN"),
                 ("source", "not json", "UNKNOWN"),
                 ("source", '{"streams":[{"index":2},{"index":2}]}', "UNKNOWN"),
                 ("source", '{"streams":[{"index":"2"}]}', "UNKNOWN"),
                 ("output", "exit", "FAIL"), ("output", "not json", "UNKNOWN"),
                 ("source", '{"streams":[{"index":2,"codec_type":"video","tags":null}]}', "UNKNOWN"),
                 ("output", '{"streams":[{"index":2,"codec_type":"video","tags":null}]}', "UNKNOWN"),
                 ("source", '{"streams":[{"index":2,"tags":{"language":1}}]}', "UNKNOWN"),
                 ("output", '{"streams":[{"index":2,"tags":{"language":1}}]}', "UNKNOWN"),
                 ("source", '{"streams":[{"index":2,"codec_type":"video"}],"format":null}', "UNKNOWN"),
                 ("output", '{"streams":[{"index":2,"codec_type":"video"}],"format":null}', "UNKNOWN"))
        cases += tuple((role, payload, "UNKNOWN") for role in ("source", "output")
                       for payload in (NONFINITE_PROBES[0], NONFINITE_PROBES[2]))
        for role, failure, status in cases:
            with self.subTest(role=role, failure=failure):
                self.pids.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                self.probe_boundary_fixture(block_stage="probe" if role == "source" else "decode",
                                            invalid_role=role, invalid_probe=failure)
                checkpoint = self.root / "checkpoint.json"
                proc = subprocess.Popen(self.command("--checkpoint-report", str(checkpoint), "--timeout", "30",
                    "--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"),
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    end = time.monotonic() + 5
                    while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                        time.sleep(0.01)
                    self.assertTrue(self.pids.exists(), "next tool did not start")
                    before = checkpoint.read_bytes()
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, -signal.SIGKILL, stderr)
                    self.assertEqual(stdout, "")
                    self.assertFalse(self.report.exists())
                    self.assertEqual(checkpoint.read_bytes(), before)
                    self.assert_failed_probe_snapshot(json.loads(before), role, status,
                                                      {"source"} if role == "output" else ())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=60)
                    self.kill_fixture()

    def test_failed_probe_checkpoint_replace_failure_stops_and_preserves_previous(self):
        replace = os.replace
        for role in ("source", "output"):
            for failure, status in (("exit", "FAIL"), ("unsupported", "UNKNOWN"), ("not json", "UNKNOWN"),
                                    ('{"streams":[{"index":2,"tags":null}]}', "UNKNOWN"),
                                    ('{"streams":[{"index":2,"tags":{"language":1}}]}', "UNKNOWN"),
                                    ('{"streams":[{"index":2,"codec_type":"video"}],"format":null}', "UNKNOWN"),
                                    (NONFINITE_PROBES[0], "UNKNOWN"), (NONFINITE_PROBES[3], "UNKNOWN")):
                with self.subTest(role=role, failure=failure):
                    self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=failure)
                    replacements, prior, candidates = [], [], []
                    boundary = 2 if role == "source" else 3
                    def fail_replace(src, dst):
                        replacements.append(src)
                        if len(replacements) == boundary:
                            prior.append(self.report.read_bytes())
                            candidates.append(json.loads(Path(src).read_text()))
                            raise OSError("fixture failed-probe checkpoint replacement failure")
                        return replace(src, dst)
                    with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
                        report = compare(self.source, self.output,
                            self.options(stream_map=((2, 2), (7, 7)), language_tags="preserve"),
                            checkpoint_report=self.report)
                    self.assertEqual(exit_code(report), 2)
                    self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                    self.assertEqual(len(replacements), boundary, "failed sink must not be retried")
                    self.assertEqual(self.report.read_bytes(), prior[0])
                    previous = json.loads(prior[0])
                    self.assertEqual(previous["verdict"], "UNKNOWN")
                    self.assertEqual(previous["media"], {} if role == "source" else {"source": report["media"]["source"]})
                    self.assertFalse(any(c["name"] == "probe" and c["role"] == role for c in previous["checks"]))
                    self.assert_failed_probe_snapshot(candidates[0], role, status,
                                                      {"source"} if role == "output" else ())
                    self.assertEqual(report["commands"], candidates[0]["commands"], "no subsequent tool may start")
                    self.assertEqual(report["decoded"], {"source": {}, "output": {}})
                    self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_failed_probe_final_copies_precedence_quick_and_opt_out(self):
        for failures, code in (({"source": "exit", "output": "not json"}, 1),
                               ({"output": "unsupported"}, 2),
                               ({"source": "exit", "output": '{"streams":[{"index":2,"tags":null}]}'}, 1),
                               ({"source": '{"streams":[{"index":2,"tags":{"language":1}}]}', "output": "exit"}, 1),
                               ({"source": "exit", "output": '{"streams":[{"index":2}],"format":null}'}, 1),
                               ({"source": '{"streams":[{"index":2}],"format":null}', "output": "exit"}, 1),
                               ({"source": "exit", "output": NONFINITE_PROBES[0]}, 1),
                               ({"source": NONFINITE_PROBES[0], "output": "exit"}, 1),
                               ({"source": "exit", "output": NONFINITE_PROBES[2]}, 1),
                               ({"source": NONFINITE_PROBES[3], "output": "exit"}, 1)):
            for quick in ([], ["--quick", "0.25"]):
                with self.subTest(failures=failures, quick=quick):
                    self.probe_boundary_fixture(block_stage=None, probe_failures=failures)
                    checkpoint = self.root / "checkpoint.json"
                    result = subprocess.run(self.command("--checkpoint-report", str(checkpoint), *quick),
                                            cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, code, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report, json.loads(checkpoint.read_text()))
                    self.assertEqual(report, json.loads(self.report.read_text()))
                    self.assertEqual(report["report_state"], "complete")
                    self.assertEqual(report["coverage"], "partial")
                    self.assertEqual(report["mapping"]["status"], "not_run")
                    snapshots = []
                    def save(value, *args):
                        snapshots.append(copy.deepcopy(value))
                        write_report(value, *args)
                    with patch("mediawrench.postflight.core.write_report", side_effect=save):
                        compare(self.source, self.output, self.options(quick_seconds=0.25 if quick else None),
                                checkpoint_report=checkpoint)
                    after_probes = snapshots[2]
                    self.assertEqual(after_probes["verdict"], "FAIL" if code == 1 else "UNKNOWN")
                    self.assertEqual(len([c for c in after_probes["checks"] if c["name"] == "probe"]), 2)
                    self.assertFalse(any("-map" in c["argv"] for c in after_probes["commands"]))
                    self.assertEqual(report["progress"]["source"]["completed_streams"], [2, 7] if code == 2 else [])
                    if code == 2:
                        rows = report["stream_validation"]["source"][:2]
                        self.assertTrue(all(s["decode_coverage"] == ("partial" if quick else "full") for s in rows))
                    with patch("mediawrench.postflight.core.write_report") as sink:
                        without = compare(self.source, self.output, self.options())
                    sink.assert_not_called()
                    self.assertEqual(exit_code(without), code)

    def test_malformed_probe_language_tags_rejected_before_inventory(self):
        invalid_tags = (None, [], "eng", 1, False, {"language": None},
                        {"language": False}, {"language": 1}, {"language": []}, {"language": {}})
        for role in ("source", "output"):
            for policy in ("ignore", "preserve"):
                for enabled in (False, True):
                    for tags in invalid_tags:
                        with self.subTest(role=role, policy=policy, checkpoint=enabled, tags=tags):
                            # A valid first stream must not partially certify an inventory
                            # whose later (even non-AV) stream has malformed metadata.
                            payload = json.dumps({"streams": [{"index": 2, "codec_type": "video"},
                                {"index": 9, "codec_type": "subtitle", "tags": tags}]})
                            self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=payload)
                            snapshots = []
                            def save(value, *args):
                                snapshots.append(copy.deepcopy(value))
                                write_report(value, *args)
                            with patch("mediawrench.postflight.core.write_report", side_effect=save):
                                report = compare(self.source, self.output,
                                    self.options(stream_map=((2, 2), (7, 7)), language_tags=policy),
                                    checkpoint_report=self.report if enabled else None)
                            self.assertEqual(exit_code(report), 2)
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            checks = [c for c in report["checks"] if c["name"] == "probe" and c["role"] == role]
                            self.assertEqual([c["status"] for c in checks], ["UNKNOWN"])
                            self.assertIn("language tags", checks[0]["actual"])
                            if enabled:
                                self.assertEqual(json.loads(self.report.read_text()), json.loads(json.dumps(report)))
                                self.assert_failed_probe_snapshot(snapshots[1 if role == "source" else 2],
                                    role, "UNKNOWN", {"source"} if role == "output" else (), language_tags=policy)
                            else:
                                self.assertEqual(snapshots, [])
                            for value in [report, *snapshots]:
                                self.assertNotIn(role, value["media"])
                                self.assertIsNone(value["stream_validation"][role])
                                self.assertFalse(value["progress"][role]["probe_completed"])
                                self.assertIsNone(value["progress"][role]["discovered_streams"])
                                self.assertIsNone(value["progress"][role]["pending_streams"])
                                self.assertEqual(value["progress"][role]["attempted_streams"], [])
                                self.assertEqual(value["progress"][role]["completed_streams"], [])
                                self.assertEqual(value["decoded"][role], {})
                                self.assertEqual(value["mapping"]["status"], "not_run")
                                self.assertFalse(any("duration" in c["name"] or c["name"] == "language_tag"
                                                     for c in value["checks"]))

    def test_nonfinite_probe_numbers_rejected_before_inventory(self):
        for role in ("source", "output"):
            for policy in ("ignore", "preserve"):
                for enabled in (False, True):
                    for payload in NONFINITE_PROBES:
                        with self.subTest(role=role, policy=policy, checkpoint=enabled, payload=payload):
                            self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=payload)
                            snapshots = []
                            def save(value, *args):
                                snapshots.append(copy.deepcopy(value))
                                write_report(value, *args)
                            with patch("mediawrench.postflight.core.write_report", side_effect=save):
                                report = compare(self.source, self.output,
                                    self.options(stream_map=((2, 2), (7, 7)), language_tags=policy),
                                    checkpoint_report=self.report if enabled else None)
                            self.assertEqual(exit_code(report), 2)
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            checks = [c for c in report["checks"] if c["name"] == "probe" and c["role"] == role]
                            self.assertEqual([c["status"] for c in checks], ["UNKNOWN"])
                            self.assertIn("Nonfinite number", checks[0]["actual"])
                            if enabled:
                                self.assertEqual(json.loads(self.report.read_text()), json.loads(json.dumps(report)))
                                self.assert_failed_probe_snapshot(snapshots[1 if role == "source" else 2],
                                    role, "UNKNOWN", {"source"} if role == "output" else (), language_tags=policy)
                            else:
                                self.assertEqual(snapshots, [])
                            other = "output" if role == "source" else "source"
                            self.assertEqual(report["progress"][other]["completed_streams"], [2, 7])
                            self.assertFalse(any("-map" in c["argv"] and c["argv"][c["argv"].index("-i") + 1] == str(getattr(self, role))
                                                 for c in report["commands"]))
                            for value in [report, *snapshots]:
                                json.dumps(value, allow_nan=False)
                                self.assertNotIn(role, value["media"])
                                self.assertIsNone(value["stream_validation"][role])
                                self.assertFalse(value["progress"][role]["probe_completed"])
                                self.assertIsNone(value["progress"][role]["discovered_streams"])
                                self.assertIsNone(value["progress"][role]["pending_streams"])
                                self.assertEqual(value["progress"][role]["attempted_streams"], [])
                                self.assertEqual(value["progress"][role]["completed_streams"], [])
                                self.assertEqual(value["decoded"][role], {})
                                self.assertEqual(value["mapping"]["status"], "not_run")
                                self.assertFalse(any("duration" in c["name"] or c["name"] == "language_tag"
                                                     for c in value["checks"]))

    def test_malformed_probe_format_rejected_before_inventory(self):
        invalid_formats = (None, [], ["duration", "1"], "matroska", 1, False)
        for role in ("source", "output"):
            for policy in ("ignore", "preserve"):
                for enabled in (False, True):
                    for container in invalid_formats:
                        with self.subTest(role=role, policy=policy, checkpoint=enabled, format=container):
                            payload = json.dumps({"streams": [{"index": 2, "codec_type": "video"},
                                {"index": 7, "codec_type": "audio", "tags": {"language": "eng"}}],
                                "format": container})
                            self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=payload)
                            snapshots = []
                            def save(value, *args):
                                snapshots.append(copy.deepcopy(value))
                                write_report(value, *args)
                            with patch("mediawrench.postflight.core.write_report", side_effect=save):
                                report = compare(self.source, self.output,
                                    self.options(stream_map=((2, 2), (7, 7)), language_tags=policy),
                                    checkpoint_report=self.report if enabled else None)
                            self.assertEqual(exit_code(report), 2)
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            checks = [c for c in report["checks"] if c["name"] == "probe" and c["role"] == role]
                            self.assertEqual([c["status"] for c in checks], ["UNKNOWN"])
                            self.assertIn("format", checks[0]["actual"])
                            if enabled:
                                self.assertEqual(json.loads(self.report.read_text()), json.loads(json.dumps(report)))
                                self.assert_failed_probe_snapshot(snapshots[1 if role == "source" else 2],
                                    role, "UNKNOWN", {"source"} if role == "output" else (), language_tags=policy)
                            else:
                                self.assertEqual(snapshots, [])
                            for value in [report, *snapshots]:
                                self.assertNotIn(role, value["media"])
                                self.assertIsNone(value["stream_validation"][role])
                                self.assertFalse(value["progress"][role]["probe_completed"])
                                self.assertIsNone(value["progress"][role]["discovered_streams"])
                                self.assertIsNone(value["progress"][role]["pending_streams"])
                                self.assertEqual(value["progress"][role]["attempted_streams"], [])
                                self.assertEqual(value["progress"][role]["completed_streams"], [])
                                self.assertEqual(value["decoded"][role], {})
                                self.assertEqual(value["mapping"]["status"], "not_run")
                                self.assertFalse(any("duration" in c["name"] or c["name"] == "language_tag"
                                                     for c in value["checks"]))

    def tolerance_fixture(self, source=1, output=1, affected="both", independent=None):
        inventories = {side: {"streams": [
            {"index": 0, "codec_type": "video", "tags": {"language":
                "deu" if independent == "tag" and side == "output" else "eng"}},
            {"index": 1, "codec_type": "audio"}],
            "format": {"duration": span if affected in ("container", "both") else 0.5}}
            for side, span in (("source", source), ("output", output))}
        self.tool.write_text(f'''#!{sys.executable}
import json, sys
if '-version' in sys.argv:
    print('local tolerance arithmetic fixture')
elif '-show_entries' in sys.argv:
    side = 'source' if sys.argv[-1] == {str(self.source)!r} else 'output'
    print(json.dumps({inventories!r}[side]))
else:
    side = 'source' if sys.argv[sys.argv.index('-i')+1] == {str(self.source)!r} else 'output'
    index = sys.argv[sys.argv.index('-map')+1]
    if {independent!r} == 'decode' and side == 'output' and index == '0:1':
        print('invalid media: independent audio decode failure', file=sys.stderr)
        sys.exit(1)
    span = {source!r} if side == 'source' else {output!r}
    ticks = int(span * 2) if index == '0:0' and {affected!r} in ('decoded', 'both') else 1
    print(f'#tb 0: 1/2\\n0,0,0,{{ticks}},4,00000000')
''')
        self.tool.chmod(0o755)
        return inventories

    def test_duration_tolerance_overflow_retains_evidence_and_final_copies(self):
        # Both source/output duration directions; isolate each shared-helper caller.
        cases = (("sum", 1, 1.5, 1e308), ("sum", 1.5, 1, 1e308),
                 ("product", 2, 3, 0), ("product", 3, 2, 0))
        for arithmetic, source, output, absolute in cases:
            for affected in ("decoded", "container", "both"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(arithmetic=arithmetic, source=source, output=output,
                                          affected=affected, checkpoint=enabled, independent=independent):
                            inventories = self.tolerance_fixture(source, output, affected, independent)
                            checkpoint = self.root / "tolerance-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--absolute-tolerance", str(absolute), "--relative-tolerance", "1e308",
                                    "--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial" if independent == "decode" else "full")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["media"], inventories)
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            duration_checks = {c["name"]: c for c in report["checks"] if "duration" in c["name"]}
                            for name, target in (("video_decoded_duration", "decoded"), ("container_duration", "container")):
                                check = duration_checks[name]
                                overflow = affected in (target, "both")
                                self.assertEqual(check["status"], "UNKNOWN" if overflow else "PASS")
                                if overflow:
                                    self.assertEqual((check["expected"], check["actual"]), (source, output))
                                    self.assertEqual(check["delta_seconds"], abs(source-output))
                                    self.assertIsNone(check["tolerance_seconds"])
                                    self.assertIn("not representable", check["diagnostic"])
                                else:
                                    self.assertEqual(check["tolerance_seconds"], absolute + 0.5e308)
                            self.assertEqual(duration_checks["audio_decoded_duration"]["status"],
                                             "UNKNOWN" if independent == "decode" else "PASS")
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertFalse(any(c["name"] in ("checkpoint_write", "preflight") for c in report["checks"]))
                            for side in ("source", "output"):
                                failed = side == "output" and independent == "decode"
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [0, 1])
                                self.assertEqual(progress["attempted_streams"], [0, 1])
                                self.assertEqual(progress["completed_streams"], [0] if failed else [0, 1])
                                self.assertEqual(progress["pending_streams"], [1] if failed else [])
                                self.assertEqual(set(report["decoded"][side]), {"0"} if failed else {"0", "1"})
                                video, audio = report["stream_validation"][side]
                                self.assertEqual(video["duration_status"], duration_checks["video_decoded_duration"]["status"])
                                self.assertEqual(video["decode_status"], "PASS")
                                self.assertEqual(video["decode_coverage"], "full")
                                self.assertEqual(video["peer_stream"], 0)
                                self.assertEqual(audio["peer_stream"], 1)
                                self.assertEqual(video["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                                self.assertEqual(report["decoded"][side]["0"]["frames"], 1)
                                self.assertEqual(report["decoded"][side]["0"]["duration_seconds"],
                                                 (source if side == "source" else output) if affected != "container" else 0.5)
                            self.assertEqual(len([c for c in report["commands"] if "-map" in c["argv"]]), 4)
                            json.dumps(report, allow_nan=False)
                            self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_duration_tolerance_representable_controls(self):
        cases = ((1, 1, (), "PASS", 0.27),
                 (1, 2, (), "FAIL", 0.27),
                 (1, 2, ("--absolute-tolerance", "5e307", "--relative-tolerance", "5e307"), "PASS", 1e308),
                 (2, 1, ("--absolute-tolerance", "1e308", "--relative-tolerance", "0"), "PASS", 1e308))
        for source, output, tolerances, status, tolerance in cases:
            for enabled in (False, True):
                with self.subTest(source=source, output=output, tolerances=tolerances, checkpoint=enabled):
                    self.tolerance_fixture(source, output)
                    checkpoint = self.root / "control-checkpoint.json"
                    checkpoint.unlink(missing_ok=True)
                    args = [*tolerances, "--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"]
                    if enabled:
                        args += ["--checkpoint-report", str(checkpoint)]
                    result = subprocess.run(self.command(*args), cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0 if status == "PASS" else 1, result.stderr)
                    self.assertEqual(result.stderr, "")
                    report = json.loads(result.stdout)
                    self.assertEqual(report["verdict"], status)
                    self.assertEqual(report, json.loads(self.report.read_text()))
                    if enabled:
                        self.assertEqual(report, json.loads(checkpoint.read_text()))
                    else:
                        self.assertFalse(checkpoint.exists())
                    for check in report["checks"]:
                        if check["name"] in ("video_decoded_duration", "container_duration"):
                            self.assertEqual(check["status"], status)
                            self.assertEqual(check["tolerance_seconds"], tolerance)
                            self.assertEqual(check["delta_seconds"], abs(source-output))
                    self.assertTrue(all(p["completed_streams"] == [0, 1] for p in report["progress"].values()))
                    json.dumps(report, allow_nan=False)

    def test_tolerance_overflow_checkpoint_failure_preserves_previous_json(self):
        replace = os.replace
        for independent in (None, "tag"):
            with self.subTest(independent=independent):
                self.tolerance_fixture(independent=independent)
                replacements, previous = [], []
                def fail_final_replace(src, dst):
                    replacements.append(str(dst))
                    if len(replacements) == 9:  # final, after four completed-stream snapshots
                        previous.append(self.report.read_bytes())
                        raise OSError("fixture final checkpoint replacement failure")
                    return replace(src, dst)
                with patch("mediawrench.postflight.core.os.replace", side_effect=fail_final_replace):
                    report = compare(self.source, self.output,
                                     self.options(absolute_tolerance=1e308, relative_tolerance=1e308,
                                                  stream_map=((0, 0), (1, 1)), language_tags="preserve"),
                                     checkpoint_report=self.report)
                report = json.loads(json.dumps(report, allow_nan=False))
                self.assertEqual(len(replacements), 9)  # failed sink never retried
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(exit_code(report), 2)
                self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                self.assertEqual(self.report.read_bytes(), previous[0])
                saved = json.loads(previous[0])
                self.assertEqual(saved["report_state"], "in_progress")
                self.assertEqual(saved["verdict"], "FAIL" if independent else "UNKNOWN")
                self.assertFalse(any("duration" in c["name"] for c in saved["checks"]))
                self.assertEqual(report["decoded"], saved["decoded"])
                self.assertTrue(all(p["completed_streams"] == [0, 1] for p in report["progress"].values()))
                self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                 ["language_tag"] if independent else [])
                checks = [c for c in report["checks"] if c["name"] == "checkpoint_write"]
                self.assertEqual(len(checks), 1)
                self.assertIn("fixture final checkpoint replacement failure", checks[0]["actual"])
                self.assertEqual([c["status"] for c in report["checks"] if c["name"] == "container_duration"], ["UNKNOWN"])
                destination = self.root / "separate-report.json"
                write_report(report, destination, self.source, self.output)
                self.assertEqual(report, json.loads(destination.read_text()))
                self.assertEqual(self.report.read_bytes(), previous[0])
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def framehash_range_fixture(self, framehash, role=None, independent=None, indices=(0, 1)):
        inventory = {"streams": [{"index": indices[0], "codec_type": "video", "tags": {"language": "eng"}},
                                  {"index": indices[1], "codec_type": "audio"}], "format": {"duration": "1"}}
        self.tool.write_text(f'''#!{sys.executable}
import json, sys
if '-version' in sys.argv:
    print('local framehash-range fixture')
elif '-show_entries' in sys.argv:
    inventory = json.loads({json.dumps(inventory)!r})
    if {independent!r} == 'tag' and sys.argv[-1] == {str(self.output)!r}:
        inventory['streams'][0]['tags']['language'] = 'deu'
    print(json.dumps(inventory))
else:
    side = 'source' if sys.argv[sys.argv.index('-i')+1] == {str(self.source)!r} else 'output'
    index = sys.argv[sys.argv.index('-map')+1]
    if {independent!r} == 'decode' and side != {role!r} and index == {'0:' + str(indices[1])!r}:
        print('invalid media: independent audio decode failure', file=sys.stderr)
        sys.exit(1)
    target = {role!r}
    print({framehash!r} if index == {'0:' + str(indices[0])!r} and (target is None or side == target)
          else '#tb 0: 1/1\\n0,0,0,1,4,00000000')
''')
        self.tool.chmod(0o755)

    def test_framehash_out_of_float_range_keeps_pending_and_final_copies(self):
        # End overflow; start overflow; finite endpoints but overflowed span.
        for pts, duration in ((0, 10**400), (-(10**400), 1), (-(10**308), 2 * 10**308)):
            framehash = f"#tb 0: 1/1\n0,0,{pts},{duration},4,00000000"
            for role in ("source", "output"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(pts=pts, duration=duration, role=role,
                                          checkpoint=enabled, independent=independent):
                            self.framehash_range_fixture(framehash, role, independent)
                            checkpoint = self.root / "range-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            checks = [c for c in report["checks"] if c["name"] == "video_decode"]
                            self.assertEqual(len(checks), 1)
                            self.assertEqual((checks[0]["status"], checks[0]["role"]), ("UNKNOWN", role))
                            self.assertIn("Cannot interpret decoded framehash", checks[0]["actual"])
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertEqual(next(c for c in report["checks"] if c["name"] == "container_duration")["status"], "PASS")
                            for side in ("source", "output"):
                                failed = [0] if side == role else [1] if independent == "decode" else []
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [0, 1])
                                self.assertEqual(progress["attempted_streams"], [0, 1])
                                self.assertEqual(progress["pending_streams"], failed)
                                self.assertEqual(progress["completed_streams"], [i for i in (0, 1) if i not in failed])
                                self.assertEqual(set(report["decoded"][side]), {str(i) for i in (0, 1) if i not in failed})
                                self.assertEqual(report["media"][side]["format"], {"duration": "1"})
                                rows = report["stream_validation"][side]
                                for row in rows:
                                    index = row["stream_index"]
                                    self.assertEqual(row["decode_coverage"], "none" if index in failed else "full")
                                    self.assertEqual(row["decode_status"], "UNKNOWN" if side == role and index == 0 else
                                                     "FAIL" if index in failed else "PASS")
                                self.assertEqual(rows[0]["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                            commands = [c for c in report["commands"] if "-map" in c["argv"]]
                            self.assertEqual(len(commands), 4)
                            self.assertTrue(all("elapsed_seconds" in c for c in commands))
                            json.dumps(report, allow_nan=False)

    def test_positive_decoded_span_underflow_keeps_pending_and_final_copies(self):
        for pts, duration in ((0, 1),):
            framehash = f"#tb 0: 1/{10**400}\n0,0,{pts},{duration},4,00000000"
            for role in ("source", "output"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(pts=pts, duration=duration, role=role,
                                          checkpoint=enabled, independent=independent):
                            self.framehash_range_fixture(framehash, role, independent)
                            checkpoint = self.root / "range-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            checks = [c for c in report["checks"] if c["name"] == "video_decode"]
                            self.assertEqual(len(checks), 1)
                            self.assertEqual((checks[0]["status"], checks[0]["role"]), ("UNKNOWN", role))
                            self.assertIn("positive decoded timestamp span underflows to zero seconds", checks[0]["actual"])
                            self.assertEqual([(p["source_stream"], p["output_stream"]) for p in report["mapping"]["pairs"]], [(0, 0), (1, 1)])
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertEqual(next(c for c in report["checks"] if c["name"] == "container_duration")["status"], "PASS")
                            for side in ("source", "output"):
                                failed = [0] if side == role else [1] if independent == "decode" else []
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [0, 1])
                                self.assertEqual(progress["attempted_streams"], [0, 1])
                                self.assertEqual(progress["pending_streams"], failed)
                                self.assertEqual(progress["completed_streams"], [i for i in (0, 1) if i not in failed])
                                self.assertEqual(set(report["decoded"][side]), {str(i) for i in (0, 1) if i not in failed})
                                self.assertEqual(report["media"][side]["format"], {"duration": "1"})
                                rows = report["stream_validation"][side]
                                for row in rows:
                                    index = row["stream_index"]
                                    self.assertEqual(row["mapping_status"], "mapped")
                                    self.assertEqual(row["peer_stream"], index)
                                    self.assertEqual(row["decode_coverage"], "none" if index in failed else "full")
                                    self.assertEqual(row["decode_status"], "UNKNOWN" if side == role and index == 0 else
                                                     "FAIL" if index in failed else "PASS")
                                self.assertEqual(rows[0]["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                            commands = [c for c in report["commands"] if "-map" in c["argv"]]
                            self.assertEqual(len(commands), 4)
                            self.assertTrue(all("elapsed_seconds" in c for c in commands))
                            json.dumps(report, allow_nan=False)

    def test_unexpected_framehash_stream_keeps_pending_and_final_copies(self):
        for identifier in ("1", "-1", "not-a-stream"):
            framehash = f"#tb 0: 1/1\n0,0,0,1,4,00000000\n{identifier},0,0,1,4,00000000"
            for role in ("source", "output"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(identifier=identifier, role=role,
                                          checkpoint=enabled, independent=independent):
                            self.framehash_range_fixture(framehash, role, independent, indices=(2, 7))
                            checkpoint = self.root / "range-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            checks = [c for c in report["checks"] if c["name"] == "video_decode"]
                            self.assertEqual(len(checks), 1)
                            self.assertEqual((checks[0]["status"], checks[0]["role"]), ("UNKNOWN", role))
                            self.assertIn("unexpected framehash stream identifier", checks[0]["actual"])
                            self.assertEqual([(p["source_stream"], p["output_stream"]) for p in report["mapping"]["pairs"]], [(2, 2), (7, 7)])
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertEqual(next(c for c in report["checks"] if c["name"] == "container_duration")["status"], "PASS")
                            for side in ("source", "output"):
                                failed = [2] if side == role else [7] if independent == "decode" else []
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [2, 7])
                                self.assertEqual(progress["attempted_streams"], [2, 7])
                                self.assertEqual(progress["pending_streams"], failed)
                                self.assertEqual(progress["completed_streams"], [i for i in (2, 7) if i not in failed])
                                self.assertEqual(set(report["decoded"][side]), {str(i) for i in (2, 7) if i not in failed})
                                self.assertEqual(report["media"][side]["format"], {"duration": "1"})
                                rows = report["stream_validation"][side]
                                for row in rows:
                                    index = row["stream_index"]
                                    self.assertEqual(row["mapping_status"], "mapped")
                                    self.assertEqual(row["peer_stream"], index)
                                    self.assertEqual(row["decode_coverage"], "none" if index in failed else "full")
                                    self.assertEqual(row["decode_status"], "UNKNOWN" if side == role and index == 2 else
                                                     "FAIL" if index in failed else "PASS")
                                self.assertEqual(rows[0]["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                            commands = [c for c in report["commands"] if "-map" in c["argv"]]
                            self.assertEqual(len(commands), 4)
                            self.assertTrue(all("elapsed_seconds" in c for c in commands))
                            json.dumps(report, allow_nan=False)

    def test_conflicting_framehash_timebase_keeps_pending_and_final_copies(self):
        for between in ("", "0,0,0,1,4,00000000\n"):
            framehash = f"#tb 0: 1/1\n{between}#tb 0: 1/2\n0,0,0,1,4,00000000"
            for role in ("source", "output"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(between=between, role=role,
                                          checkpoint=enabled, independent=independent):
                            self.framehash_range_fixture(framehash, role, independent, indices=(2, 7))
                            checkpoint = self.root / "range-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            checks = [c for c in report["checks"] if c["name"] == "video_decode"]
                            self.assertEqual(len(checks), 1)
                            self.assertEqual((checks[0]["status"], checks[0]["role"]), ("UNKNOWN", role))
                            self.assertIn("conflicting framehash timebase", checks[0]["actual"])
                            self.assertEqual([(p["source_stream"], p["output_stream"]) for p in report["mapping"]["pairs"]], [(2, 2), (7, 7)])
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertEqual(next(c for c in report["checks"] if c["name"] == "container_duration")["status"], "PASS")
                            for side in ("source", "output"):
                                failed = [2] if side == role else [7] if independent == "decode" else []
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [2, 7])
                                self.assertEqual(progress["attempted_streams"], [2, 7])
                                self.assertEqual(progress["pending_streams"], failed)
                                self.assertEqual(progress["completed_streams"], [i for i in (2, 7) if i not in failed])
                                self.assertEqual(set(report["decoded"][side]), {str(i) for i in (2, 7) if i not in failed})
                                self.assertEqual(report["media"][side]["format"], {"duration": "1"})
                                rows = report["stream_validation"][side]
                                for row in rows:
                                    index = row["stream_index"]
                                    self.assertEqual(row["mapping_status"], "mapped")
                                    self.assertEqual(row["peer_stream"], index)
                                    self.assertEqual(row["decode_coverage"], "none" if index in failed else "full")
                                    self.assertEqual(row["decode_status"], "UNKNOWN" if side == role and index == 2 else
                                                     "FAIL" if index in failed else "PASS")
                                self.assertEqual(rows[0]["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                            commands = [c for c in report["commands"] if "-map" in c["argv"]]
                            self.assertEqual(len(commands), 4)
                            self.assertTrue(all("elapsed_seconds" in c for c in commands))
                            json.dumps(report, allow_nan=False)

    def test_malformed_framehash_dts_keeps_pending_and_final_copies(self):
        for dts, between in (("", ""), ("not-a-timestamp", ""),
                             ("", "0,0,0,1,4,00000000\n"),
                             ("not-a-timestamp", "0,0,0,1,4,00000000\n")):
            framehash = f"#tb 0: 1/1\n{between}0,{dts},0,1,4,00000000"
            for role in ("source", "output"):
                for enabled in (False, True):
                    for independent in (None, "tag", "decode"):
                        with self.subTest(dts=dts, between=between, role=role,
                                          checkpoint=enabled, independent=independent):
                            self.framehash_range_fixture(framehash, role, independent, indices=(2, 7))
                            checkpoint = self.root / "range-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if independent else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if independent else "UNKNOWN")
                            self.assertEqual(report["coverage"], "partial")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            checks = [c for c in report["checks"] if c["name"] == "video_decode"]
                            self.assertEqual(len(checks), 1)
                            self.assertEqual((checks[0]["status"], checks[0]["role"]), ("UNKNOWN", role))
                            self.assertIn("malformed framehash DTS", checks[0]["actual"])
                            self.assertEqual([(p["source_stream"], p["output_stream"]) for p in report["mapping"]["pairs"]], [(2, 2), (7, 7)])
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if independent == "tag" else
                                             ["audio_decode"] if independent == "decode" else [])
                            self.assertEqual(next(c for c in report["checks"] if c["name"] == "container_duration")["status"], "PASS")
                            for side in ("source", "output"):
                                failed = [2] if side == role else [7] if independent == "decode" else []
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [2, 7])
                                self.assertEqual(progress["attempted_streams"], [2, 7])
                                self.assertEqual(progress["pending_streams"], failed)
                                self.assertEqual(progress["completed_streams"], [i for i in (2, 7) if i not in failed])
                                self.assertEqual(set(report["decoded"][side]), {str(i) for i in (2, 7) if i not in failed})
                                self.assertEqual(report["media"][side]["format"], {"duration": "1"})
                                rows = report["stream_validation"][side]
                                for row in rows:
                                    index = row["stream_index"]
                                    self.assertEqual(row["mapping_status"], "mapped")
                                    self.assertEqual(row["peer_stream"], index)
                                    self.assertEqual(row["decode_coverage"], "none" if index in failed else "full")
                                    self.assertEqual(row["decode_status"], "UNKNOWN" if side == role and index == 2 else
                                                     "FAIL" if index in failed else "PASS")
                                self.assertEqual(rows[0]["language_tag_status"], "FAIL" if independent == "tag" else "PASS")
                            commands = [c for c in report["commands"] if "-map" in c["argv"]]
                            self.assertEqual(len(commands), 4)
                            self.assertTrue(all("elapsed_seconds" in c for c in commands))
                            json.dumps(report, allow_nan=False)

    def test_framehash_representable_ratios_and_offsets_keep_facts(self):
        for pts, duration, timebase, expected in (
                (1, 3, "1/4", (0.25, 1.0, 0.75)),
                (0, 1, f"1/{2**1074}", (0.0, 2**-1074, 2**-1074)),
                (10, 1, "1/1", (10.0, 11.0, 1.0)),
                (-10, 1, "1/1", (-10.0, -9.0, 1.0)),
                (10**400, 10**400, f"1/{10**400}", (1.0, 2.0, 1.0))):
            for enabled in (False, True):
                with self.subTest(pts=pts, timebase=timebase, checkpoint=enabled):
                    self.framehash_range_fixture(f"#tb 0: {timebase}\n0,0,{pts},{duration},4,00000000")
                    checkpoint = self.root / "ratio-checkpoint.json"
                    checkpoint.unlink(missing_ok=True)
                    args = ["--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"]
                    if enabled:
                        args += ["--checkpoint-report", str(checkpoint)]
                    result = subprocess.run(self.command(*args), cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["verdict"], "PASS")
                    self.assertEqual(report["coverage"], "full")
                    self.assertEqual(report, json.loads(self.report.read_text()))
                    if enabled:
                        self.assertEqual(report, json.loads(checkpoint.read_text()))
                    else:
                        self.assertFalse(checkpoint.exists())
                    for side in ("source", "output"):
                        facts = report["decoded"][side]["0"]
                        self.assertEqual(tuple(facts[key] for key in ("start_seconds", "end_seconds", "duration_seconds")), expected)
                        self.assertEqual(report["progress"][side]["completed_streams"], [0, 1])
                        self.assertEqual(report["progress"][side]["pending_streams"], [])

    def test_valid_framehash_dts_keeps_facts_and_final_copies(self):
        for dts in ("0", "+0", "-0", "001", " +42 ", "-42", str(10**400), str(-10**400)):
            for enabled in (False, True):
                with self.subTest(dts=dts, checkpoint=enabled):
                    self.framehash_range_fixture(f"#tb 0: 1/1\n0,{dts},0,1,4,00000000", indices=(2, 7))
                    checkpoint = self.root / "dts-checkpoint.json"
                    checkpoint.unlink(missing_ok=True)
                    args = ["--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"]
                    if enabled:
                        args += ["--checkpoint-report", str(checkpoint)]
                    result = subprocess.run(self.command(*args), cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report, json.loads(self.report.read_text()))
                    if enabled:
                        self.assertEqual(report, json.loads(checkpoint.read_text()))
                    else:
                        self.assertFalse(checkpoint.exists())
                    self.assertEqual((report["verdict"], report["coverage"]), ("PASS", "full"))
                    for role in ("source", "output"):
                        self.assertEqual(report["progress"][role]["pending_streams"], [])
                        self.assertEqual(report["decoded"][role]["2"], dict(frames=1, decoded_bytes=4,
                            start_seconds=0.0, end_seconds=1.0, duration_seconds=1.0))

    def test_framehash_zero_output_index_with_nonzero_input_indices(self):
        for enabled in (False, True):
            self.framehash_range_fixture("#tb 0: 1/1\n0,0,0,1,4,00000000", indices=(2, 7))
            report = compare(self.source, self.output,
                self.options(stream_map=((2, 2), (7, 7)), language_tags="preserve"),
                checkpoint_report=self.report if enabled else None)
            self.assertEqual(exit_code(report), 0)
            self.assertEqual(report["coverage"], "full")
            for role in ("source", "output"):
                self.assertEqual(report["progress"][role]["completed_streams"], [2, 7])
                self.assertEqual(set(report["decoded"][role]), {2, 7})
                self.assertTrue(all(f["frames"] == 1 for f in report["decoded"][role].values()))
            if enabled:
                self.assertEqual(json.loads(json.dumps(report)), json.loads(self.report.read_text()))

    def test_huge_integer_declared_duration_preserves_evidence_and_final_copies(self):
        for role in ("source", "output"):
            for enabled in (False, True):
                for duration in (10**400, -(10**400)):
                    for mismatch in (False, True):
                        with self.subTest(role=role, checkpoint=enabled, duration=duration, mismatch=mismatch):
                            inventories = {side: {"streams": [
                                {"index": 2, "codec_type": "video"},
                                {"index": 7, "codec_type": "audio", "tags": {
                                    "language": "deu" if side == "output" and mismatch else "eng"}},
                                {"index": 9, "codec_type": "subtitle"}],
                                "format": {"duration": duration if side == role else "1"}}
                                for side in ("source", "output")}
                            self.probe_boundary_fixture(block_stage=None, probe_failures={
                                side: json.dumps(value) for side, value in inventories.items()})
                            checkpoint = self.root / "duration-checkpoint.json"
                            checkpoint.unlink(missing_ok=True)
                            args = ["--stream-map", "2:2", "--stream-map", "7:7", "--language-tags", "preserve"]
                            if enabled:
                                args += ["--checkpoint-report", str(checkpoint)]
                            result = subprocess.run(self.command(*args), cwd=ROOT,
                                                    capture_output=True, text=True, timeout=120)
                            self.assertEqual(result.returncode, 1 if mismatch else 2, result.stderr)
                            self.assertEqual(result.stderr, "")
                            report = json.loads(result.stdout)
                            self.assertEqual(report, json.loads(self.report.read_text()))
                            if enabled:
                                self.assertEqual(report, json.loads(checkpoint.read_text()))
                            else:
                                self.assertFalse(checkpoint.exists())
                            self.assertEqual(report["verdict"], "FAIL" if mismatch else "UNKNOWN")
                            self.assertEqual(report["report_state"], "complete")
                            self.assertEqual(report["coverage"], "full")
                            self.assertIsNone(report["termination"])
                            self.assertEqual(report["media"], inventories)
                            self.assertEqual(report["mapping"]["status"], "PASS")
                            self.assertEqual([(p["source_stream"], p["output_stream"])
                                for p in report["mapping"]["pairs"]], [(2, 2), (7, 7)])
                            unknown = [c for c in report["checks"] if c["status"] == "UNKNOWN"]
                            self.assertEqual([c["name"] for c in unknown], ["container_duration"])
                            self.assertEqual(unknown[0]["source"], None if role == "source" else 1.0)
                            self.assertEqual(unknown[0]["actual"], None if role == "output" else 1.0)
                            self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "FAIL"],
                                             ["language_tag"] if mismatch else [])
                            for side in ("source", "output"):
                                progress = report["progress"][side]
                                self.assertTrue(progress["probe_completed"])
                                self.assertEqual(progress["discovered_streams"], [2, 7])
                                self.assertEqual(progress["attempted_streams"], [2, 7])
                                self.assertEqual(progress["completed_streams"], [2, 7])
                                self.assertEqual(progress["pending_streams"], [])
                                self.assertEqual(set(report["decoded"][side]), {"2", "7"})
                                rows = report["stream_validation"][side][:2]
                                self.assertTrue(all(r["decode_status"] == r["duration_status"] == "PASS" for r in rows))
                                self.assertTrue(all(r["decode_coverage"] == "full" for r in rows))
                                self.assertEqual(rows[1]["language_tag_status"], "FAIL" if mismatch else "PASS")
                            json.dumps(report, allow_nan=False)

    def test_finite_probe_numbers_and_string_sentinels_keep_inventory(self):
        for duration, code in ((1, 0), (1.0, 0), ("1", 0), (str(10**400), 2), ("NaN", 2), ("Infinity", 2), ("-Infinity", 2), ("1e999", 2)):
            for enabled in (False, True):
                with self.subTest(duration=duration, checkpoint=enabled):
                    fields = {"disposition": {"default": 1, "forced": 0, "finite": 1e308,
                        "tiny": 1e-308, "underflow": 0.0, "flag": True, "negative": -1.5},
                        "tags": {"language": "NaN"}, "extra": [None, False, 1.25]}
                    payload = json.dumps({"streams": [{"index": 2, "codec_type": "video", **fields}],
                                          "format": {"duration": duration}}).replace('"underflow": 0.0', '"underflow": 1e-999')
                    self.probe_boundary_fixture(block_stage=None, probe_failures={"source": payload, "output": payload})
                    report = compare(self.source, self.output,
                        self.options(stream_map=((2, 2),), language_tags="preserve"),
                        checkpoint_report=self.report if enabled else None)
                    self.assertEqual(exit_code(report), code)
                    self.assertEqual(report["coverage"], "full")
                    self.assertEqual(report["mapping"]["status"], "PASS")
                    for role in ("source", "output"):
                        self.assertTrue(report["progress"][role]["probe_completed"])
                        row = report["stream_validation"][role][0]
                        self.assertEqual(row["disposition"], fields["disposition"])
                        self.assertEqual(row["language"], "NaN")
                        self.assertEqual(row["language_tag_status"], "PASS")
                        self.assertEqual(row["decode_status"], "PASS")
                    self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "UNKNOWN"],
                                     ["container_duration"] if code == 2 else [])
                    json.dumps(report, allow_nan=False)
                    if enabled:
                        self.assertEqual(json.loads(json.dumps(report)), json.loads(self.report.read_text()))

    def test_optional_probe_format_and_unknown_duration_keep_inventory(self):
        fields = ({}, {"format": {}}, *({"format": {"duration": value}}
                  for value in (None, "N/A", "0", "-1", "NaN", "Infinity", "-Infinity")))
        for role in ("source", "output"):
            for enabled in (False, True):
                for container in fields:
                    with self.subTest(role=role, checkpoint=enabled, fields=container):
                        payload = json.dumps({"streams": [{"index": 2, "codec_type": "video"},
                            {"index": 7, "codec_type": "audio", "tags": {"language": "eng"}}], **container})
                        self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=payload)
                        report = compare(self.source, self.output,
                            self.options(stream_map=((2, 2), (7, 7)), language_tags="preserve"),
                            checkpoint_report=self.report if enabled else None)
                        self.assertEqual(exit_code(report), 2)
                        self.assertIsNone(report["termination"])
                        # Unknown declared duration does not invalidate an otherwise valid inventory.
                        self.assertEqual(report["coverage"], "full")
                        self.assertEqual(report["mapping"]["status"], "PASS")
                        self.assertEqual(report["media"][role], json.loads(payload))
                        for side in ("source", "output"):
                            self.assertTrue(report["progress"][side]["probe_completed"])
                            self.assertEqual(report["progress"][side]["completed_streams"], [2, 7])
                            self.assertEqual(report["progress"][side]["pending_streams"], [])
                            self.assertTrue(all(row["language_tag_status"] == "PASS"
                                                for row in report["stream_validation"][side][:2]))
                        unknown = [c for c in report["checks"] if c["status"] == "UNKNOWN"]
                        self.assertEqual([c["name"] for c in unknown], ["container_duration"])
                        if enabled:
                            self.assertEqual(json.loads(json.dumps(report)), json.loads(self.report.read_text()))

    def test_valid_optional_probe_language_tags_keep_exact_strings(self):
        for fields in ({}, {"tags": {}}, {"tags": {"unrequested": None}},
                       {"tags": {"language": ""}}, {"tags": {"language": "en"}},
                       {"tags": {"language": "ENG"}}):
            with self.subTest(fields=fields):
                payload = json.dumps({"streams": [{"index": 2, "codec_type": "video", **fields}],
                                      "format": {"duration": "1"}})
                self.probe_boundary_fixture(block_stage=None, probe_failures={"source": payload, "output": payload})
                report = compare(self.source, self.output,
                    self.options(stream_map=((2, 2),), language_tags="preserve"), checkpoint_report=self.report)
                self.assertEqual(exit_code(report), 0)
                self.assertEqual(report["coverage"], "full")
                expected = fields.get("tags", {}).get("language")
                for role in ("source", "output"):
                    self.assertEqual(report["stream_validation"][role][0]["language"], expected)
                    self.assertEqual(report["stream_validation"][role][0]["language_tag_status"], "PASS")
                check = next(c for c in report["checks"] if c["name"] == "language_tag")
                self.assertEqual((check["actual"], check["expected"]), (expected, expected))
                self.assertEqual(json.loads(json.dumps(report)), json.loads(self.report.read_text()))

    def test_rejected_probe_checkpoint_failure_overrides_earlier_fail(self):
        for payload in ('{"streams":[{"index":2,"tags":null}]}',
                        '{"streams":[{"index":2}],"format":null}', *NONFINITE_PROBES[:5]):
            with self.subTest(payload=payload):
                self.assert_rejected_probe_checkpoint_failure_overrides_fail(payload)

    def assert_rejected_probe_checkpoint_failure_overrides_fail(self, payload):
        self.probe_boundary_fixture(block_stage=None, probe_failures={
            "source": "exit", "output": payload})
        replace, calls, prior, candidates = os.replace, [], [], []
        def fail_replace(src, dst):
            calls.append(src)
            if len(calls) == 3:
                prior.append(self.report.read_bytes())
                candidates.append(json.loads(Path(src).read_text()))
                raise OSError("fixture rejected-probe replacement failure after media FAIL")
            return replace(src, dst)
        with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
            report = compare(self.source, self.output, self.options(), checkpoint_report=self.report)
        self.assertEqual(exit_code(report), 2)
        self.assertEqual(report["termination"]["reason"], "checkpoint_write")
        self.assertEqual(len(calls), 3, "failed sink must not be retried")
        self.assertEqual(self.report.read_bytes(), prior[0])
        self.assert_failed_probe_snapshot(json.loads(prior[0]), "source", "FAIL")
        candidate = candidates[0]
        self.assertEqual(candidate["verdict"], "FAIL")
        self.assertEqual([(c["role"], c["status"]) for c in candidate["checks"]],
                         [("source", "FAIL"), ("output", "UNKNOWN")])
        self.assertEqual(report["commands"], candidate["commands"], "no subsequent tool may start")
        self.assertEqual(candidate["media"], {})
        self.assertEqual(candidate["decoded"], {"source": {}, "output": {}})
        self.assertEqual(candidate["mapping"]["status"], "not_run")
        self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def failed_decode_fixture(self, failure="invalid", block_next=True, tag_mismatch=False):
        inventory = {"streams": [{"index": 0, "codec_type": "video", "tags": {"language": "eng"}},
                                  {"index": 1, "codec_type": "audio"}], "format": {"duration": "1"}}
        self.tool.write_text(f'''#!{sys.executable}
import json, os, sys, time
if '-version' in sys.argv:
    print('local failed-decode boundary fixture')
elif '-show_entries' in sys.argv:
    inventory = json.loads({json.dumps(inventory)!r})
    if {tag_mismatch!r} and sys.argv[-1] == {str(self.output)!r}:
        inventory['streams'][0]['tags']['language'] = 'deu'
    print(json.dumps(inventory))
else:
    source = sys.argv[sys.argv.index('-i')+1] == {str(self.source)!r}
    index = sys.argv[sys.argv.index('-map')+1]
    if source and index == '0:0':
        if {failure!r} == 'range':
            print('#tb 0: 1/1\\n0,0,0,' + {str(10**400)!r} + ',4,00000000')
        elif {failure!r} == 'underflow':
            print('#tb 0: 1/' + {str(10**400)!r} + '\\n0,0,0,1,4,00000000')
        elif {failure!r} in ('dts-empty', 'dts-nonnumeric'):
            dts = '' if {failure!r} == 'dts-empty' else 'not-a-timestamp'
            print('#tb 0: 1/1\\n0,0,0,1,4,00000000\\n0,' + dts + ',0,1,4,00000000')
        elif {failure!r} == 'timebase':
            print('#tb 0: 1/1\\n0,0,0,1,4,00000000\\n#tb 0: 1/2\\n0,0,0,1,4,00000000')
        elif {failure!r} == 'stream':
            print('#tb 0: 1/1\\n0,0,0,1,4,00000000\\n1,0,0,1,4,00000000')
        elif {failure!r} == 'malformed':
            print('#tb 0: 1/1\\n0,0,0,1,4,00000000\\ninvalid framehash')
        else:
            print('decoder not found' if {failure!r} == 'unsupported' else 'invalid media: deterministic decode failure', file=sys.stderr)
            sys.exit(0 if {failure!r} == 'stderr' else 1)
    else:
        if {block_next!r} and source and index == '0:1':
            with open({str(self.pids) + '.tmp'!r}, 'w') as handle:
                json.dump([os.getpid()], handle)
            os.replace({str(self.pids) + '.tmp'!r}, {str(self.pids)!r})
            time.sleep(60)
        print('#tb 0: 1/1\\n0,0,0,1,4,00000000')
''')
        self.tool.chmod(0o755)

    def assert_failed_decode_snapshot(self, report, expected, status, returncode):
        self.assertEqual(report["report_state"], "in_progress")
        self.assertEqual(report["verdict"], expected)
        self.assertEqual(report["coverage"], "partial")
        self.assertIsNone(report["termination"])
        self.assertEqual(report["mapping"]["status"], "PASS")
        self.assertEqual(report["decoded"], {"source": {}, "output": {}})
        checks = [c for c in report["checks"] if c["name"] == "video_decode"]
        self.assertEqual(len(checks), 1)
        self.assertEqual((checks[0]["status"], checks[0]["role"], checks[0]["stream_index"]), (status, "source", 0))
        self.assertFalse(any("duration" in c["name"] or "decoded_frames" in c["name"] for c in report["checks"]))
        commands = [c for c in report["commands"] if "-map" in c["argv"]]
        self.assertEqual(len(commands), 1, "active subsequent stream must not be published")
        self.assertEqual(commands[0]["argv"][commands[0]["argv"].index("-map")+1], "0:0")
        self.assertEqual(commands[0]["returncode"], returncode)
        self.assertIn("elapsed_seconds", commands[0])
        if "stderr_excerpt" in commands[0]:
            self.assertEqual(checks[0]["actual"], commands[0]["stderr_excerpt"])
        else:
            self.assertIn("Cannot interpret decoded framehash", checks[0]["actual"])
        for role in ("source", "output"):
            progress = report["progress"][role]
            self.assertTrue(progress["probe_completed"])
            self.assertEqual(progress["discovered_streams"], [0, 1])
            self.assertEqual(progress["attempted_streams"], [0] if role == "source" else [])
            self.assertEqual(progress["completed_streams"], [])
            self.assertEqual(progress["pending_streams"], [0, 1])
            rows = report["stream_validation"][role]
            self.assertEqual([s["decode_status"] for s in rows], [status, "not_run"] if role == "source" else ["not_run", "not_run"])
            self.assertTrue(all(s["decode_coverage"] == "none" and s["duration_status"] == "not_run" for s in rows))

    @unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux SIGKILL integration")
    def test_sigkill_after_failed_decode_retains_diagnostic_before_next_stream(self):
        for failure, mismatch, expected, status, code in (
                ("invalid", False, "FAIL", "FAIL", 1),
                ("stderr", False, "FAIL", "FAIL", 0),
                ("unsupported", False, "UNKNOWN", "UNKNOWN", 1),
                ("malformed", False, "UNKNOWN", "UNKNOWN", 0),
                ("range", False, "UNKNOWN", "UNKNOWN", 0),
                ("range", True, "FAIL", "UNKNOWN", 0),
                ("underflow", False, "UNKNOWN", "UNKNOWN", 0),
                ("underflow", True, "FAIL", "UNKNOWN", 0),
                ("stream", False, "UNKNOWN", "UNKNOWN", 0),
                ("stream", True, "FAIL", "UNKNOWN", 0),
                ("dts-empty", False, "UNKNOWN", "UNKNOWN", 0),
                ("dts-empty", True, "FAIL", "UNKNOWN", 0),
                ("dts-nonnumeric", False, "UNKNOWN", "UNKNOWN", 0),
                ("dts-nonnumeric", True, "FAIL", "UNKNOWN", 0),
                ("timebase", False, "UNKNOWN", "UNKNOWN", 0),
                ("timebase", True, "FAIL", "UNKNOWN", 0),
                ("unsupported", True, "FAIL", "UNKNOWN", 1)):
            with self.subTest(failure=failure, mismatch=mismatch):
                self.pids.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                self.failed_decode_fixture(failure, tag_mismatch=mismatch)
                checkpoint = self.root / "checkpoint.json"
                proc = subprocess.Popen(self.command("--checkpoint-report", str(checkpoint),
                    "--stream-map", "0:0", "--stream-map", "1:1", "--language-tags", "preserve"),
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    end = time.monotonic() + 5
                    while not self.pids.exists() and proc.poll() is None and time.monotonic() < end:
                        time.sleep(0.01)
                    self.assertTrue(self.pids.exists(), "subsequent audio decode did not start")
                    before = checkpoint.read_bytes()
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, -signal.SIGKILL, stderr)
                    self.assertEqual(stdout, "")
                    self.assertFalse(self.report.exists())
                    self.assertEqual(checkpoint.read_bytes(), before)
                    self.assert_failed_decode_snapshot(json.loads(before), expected, status, code)
                    if failure.startswith("dts-"):
                        self.assertIn("malformed framehash DTS", next(c for c in json.loads(before)["checks"] if c["name"] == "video_decode")["actual"])
                    if failure == "timebase":
                        self.assertIn("conflicting framehash timebase", next(c for c in json.loads(before)["checks"] if c["name"] == "video_decode")["actual"])
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=60)
                    self.kill_fixture()

    def test_failed_decode_checkpoint_replace_failure_stops_and_preserves_mapping(self):
        replace = os.replace
        for failure, mismatch, expected in (("invalid", False, "FAIL"),
                                             ("unsupported", False, "UNKNOWN"),
                                             ("unsupported", True, "FAIL"),
                                             ("range", False, "UNKNOWN"),
                                             ("range", True, "FAIL"),
                                             ("underflow", False, "UNKNOWN"),
                                             ("underflow", True, "FAIL"),
                                             ("stream", False, "UNKNOWN"),
                                             ("stream", True, "FAIL"),
                                             ("dts-empty", False, "UNKNOWN"),
                                             ("dts-empty", True, "FAIL"),
                                             ("dts-nonnumeric", False, "UNKNOWN"),
                                             ("dts-nonnumeric", True, "FAIL"),
                                             ("timebase", False, "UNKNOWN"),
                                             ("timebase", True, "FAIL")):
            with self.subTest(failure=failure, mismatch=mismatch):
                self.failed_decode_fixture(failure, block_next=False, tag_mismatch=mismatch)
                replacements, prior, candidates = [], [], []
                def fail_replace(src, dst):
                    replacements.append(src)
                    if len(replacements) == 5:
                        prior.append(self.report.read_bytes())
                        candidates.append(json.loads(Path(src).read_text()))
                        raise OSError("fixture failed-decode checkpoint replacement failure")
                    return replace(src, dst)
                with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
                    report = compare(self.source, self.output,
                        self.options(stream_map=((0, 0), (1, 1)), language_tags="preserve"), checkpoint_report=self.report)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(exit_code(report), 2)
                self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                self.assertEqual(len(replacements), 5, "failed sink must not be retried")
                self.assertEqual(self.report.read_bytes(), prior[0])
                saved = json.loads(prior[0])
                self.assertEqual(saved["mapping"]["status"], "PASS")
                self.assertEqual(saved["verdict"], "FAIL" if mismatch else "UNKNOWN")
                self.assertFalse(any("-map" in c["argv"] for c in saved["commands"]))
                self.assertEqual(saved["progress"]["source"]["attempted_streams"], [])
                if failure.startswith("dts-"):
                    self.assertIn("malformed framehash DTS", next(c for c in candidates[0]["checks"] if c["name"] == "video_decode")["actual"])
                if failure == "timebase":
                    self.assertIn("conflicting framehash timebase", next(c for c in candidates[0]["checks"] if c["name"] == "video_decode")["actual"])
                self.assert_failed_decode_snapshot(candidates[0], expected, "FAIL" if failure == "invalid" else "UNKNOWN",
                                                   0 if failure in ("range", "underflow", "stream", "timebase", "dts-empty", "dts-nonnumeric") else 1)
                self.assertEqual(len([c for c in report["commands"] if "-map" in c["argv"]]), 1)
                self.assertEqual(report["progress"]["source"]["attempted_streams"], [0])
                self.assertEqual(report["progress"]["source"]["completed_streams"], [])
                self.assertEqual(report["decoded"], {"source": {}, "output": {}})
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_failed_decode_final_copies_quick_and_opt_out(self):
        for failure, code in (("invalid", 1), ("unsupported", 2)):
            for quick in ([], ["--quick", "0.25"]):
                with self.subTest(failure=failure, quick=quick):
                    self.failed_decode_fixture(failure, block_next=False)
                    checkpoint = self.root / "checkpoint.json"
                    result = subprocess.run(self.command("--checkpoint-report", str(checkpoint), *quick),
                                            cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, code, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report, json.loads(checkpoint.read_text()))
                    self.assertEqual(report, json.loads(self.report.read_text()))
                    self.assertEqual(report["report_state"], "complete")
                    self.assertEqual(report["coverage"], "partial")
                    self.assertEqual(report["progress"]["source"]["completed_streams"], [1])
                    self.assertEqual(report["progress"]["source"]["pending_streams"], [0])
                    self.assertEqual(report["progress"]["output"]["completed_streams"], [0, 1])
                    self.assertEqual(report["stream_validation"]["source"][1]["decode_coverage"], "partial" if quick else "full")
                    with patch("mediawrench.postflight.core.write_report") as save:
                        without = compare(self.source, self.output, self.options())
                    save.assert_not_called()
                    self.assertEqual(exit_code(without), code)

    def test_initial_each_completed_stream_and_final_snapshots(self):
        self.fixture()
        for quick in (None, 0.25):
            with self.subTest(quick=quick):
                snapshots = []
                def save(report, *args):
                    snapshots.append(copy.deepcopy(report))
                    write_report(report, *args)
                with patch("mediawrench.postflight.core.write_report", side_effect=save):
                    report = compare(self.source, self.output, self.options(quick_seconds=quick),
                                     checkpoint_report=self.report)
                self.assertEqual(report["verdict"], "PASS")
                self.assertEqual(len(snapshots), 9)  # Initial, two probes, mapping, four streams, final.
                self.assertEqual(snapshots[0]["commands"], [])
                self.assertEqual(snapshots[0]["report_state"], "in_progress")
                self.assertEqual(snapshots[0]["verdict"], "UNKNOWN")
                self.assertEqual(snapshots[0]["stream_validation"], {"source": None, "output": None})
                for count, snapshot in enumerate(snapshots[1:3], 1):
                    self.assertEqual(len(snapshot["media"]), count)
                    self.assertEqual(snapshot["decoded"], {"source": {}, "output": {}})
                    self.assertEqual(snapshot["mapping"]["status"], "not_run")
                    self.assertEqual(snapshot["verdict"], "UNKNOWN")
                self.assertEqual(snapshots[3]["mapping"]["status"], "PASS")
                self.assertEqual(snapshots[3]["decoded"], {"source": {}, "output": {}})
                self.assertEqual(snapshots[3]["verdict"], "UNKNOWN")
                self.assertFalse(any(c["name"] == "language_tag" for c in snapshots[3]["checks"]))
                for count, snapshot in enumerate(snapshots[4:-1], 1):
                    self.assertEqual(sum(len(p["completed_streams"]) for p in snapshot["progress"].values()), count)
                    self.assertEqual(snapshot["verdict"], "UNKNOWN")
                    self.assertEqual(snapshot["coverage"], "partial")
                    self.assertEqual(snapshot["stream_validation"]["source"][0]["decode_coverage"],
                                     "full" if quick is None else "partial")
                self.assertEqual(report["report_state"], "complete")
                self.assertEqual(json.loads(self.report.read_text()), json.loads(json.dumps(report)))
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_disabled_checkpoints_do_not_write(self):
        self.fixture()
        with patch("mediawrench.postflight.core.write_report") as save:
            self.assertEqual(compare(self.source, self.output, self.options())["verdict"], "PASS")
        save.assert_not_called()

    def test_unsafe_or_unwritable_destination_starts_no_tools(self):
        self.fixture()
        hard = self.root / "hard"
        os.link(self.source, hard)
        symlink = self.root / "symlink"
        symlink.symlink_to(self.output)
        ordinary = self.root / "ordinary.json"
        ordinary.write_text("keep")
        report_link = self.root / "report-link"
        report_link.symlink_to(ordinary)
        for path in (self.source, self.output, hard, symlink, report_link,
                     self.root, self.root / "missing" / "report.json"):
            with self.subTest(path=path):
                report = compare(self.source, self.output, self.options(), checkpoint_report=path)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(report["commands"], [])
                self.assertTrue(any(c["name"] == "checkpoint_write" for c in report["checks"]))
        self.assertEqual(ordinary.read_text(), "keep")
        self.assertEqual(self.source.read_bytes(), b"local fixture input")
        self.assertEqual(self.output.read_bytes(), b"local fixture output")
        self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_failed_atomic_replace_keeps_previous_checkpoint_and_stops(self):
        self.fixture()
        replace = os.replace
        replacements = []
        def fail_after_first_stream(src, dst):
            if Path(dst) == self.report:
                replacements.append(src)
                if len(replacements) >= 5:
                    raise OSError("fixture destination became unwritable")
            return replace(src, dst)
        with patch("mediawrench.postflight.core.os.replace", side_effect=fail_after_first_stream):
            report = compare(self.source, self.output, self.options(), checkpoint_report=self.report)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(len([c for c in report["commands"] if "-map" in c["argv"]]), 1)
        self.assertEqual(report["progress"]["source"]["completed_streams"], [0])
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved["report_state"], "in_progress")
        self.assertEqual(saved["decoded"], {"source": {}, "output": {}})
        self.assertEqual(saved["mapping"]["status"], "PASS")
        self.assertEqual(len(replacements), 5, "failed sink must not be retried")
        self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_failed_probe_checkpoint_stops_before_next_tool_and_preserves_previous_json(self):
        self.probe_boundary_fixture(block_stage=None)
        replace = os.replace
        for fail_at in (2, 3):
            with self.subTest(fail_at=fail_at):
                replacements = []
                def fail_replace(src, dst):
                    replacements.append(src)
                    if len(replacements) == fail_at:
                        raise OSError("fixture probe checkpoint failure")
                    return replace(src, dst)
                with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
                    report = compare(self.source, self.output, self.options(), checkpoint_report=self.report)
                self.assertEqual(exit_code(report), 2)
                self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                self.assertEqual(len(replacements), fail_at, "failed sink must not be retried")
                self.assertEqual(len([c for c in report["commands"] if "-show_entries" in c["argv"]]), fail_at - 1)
                self.assertFalse(any("-map" in c["argv"] for c in report["commands"]))
                self.assert_probe_only_snapshot(json.loads(self.report.read_text()),
                                                set() if fail_at == 2 else {"source"})
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_invalid_probes_never_publish_inventories(self):
        invalid = ("exit", "not JSON", "{}", '{"streams":null}', '{"streams":{}}',
                   '{"streams":[null]}', '{"streams":[{"index":-1}]}',
                   '{"streams":[{"index":2},{"index":2}]}')
        for role in ("source", "output"):
            for payload in invalid:
                with self.subTest(role=role, payload=payload):
                    self.probe_boundary_fixture(block_stage=None, invalid_role=role, invalid_probe=payload)
                    snapshots = []
                    def save(report, *args):
                        snapshots.append(copy.deepcopy(report))
                        write_report(report, *args)
                    with patch("mediawrench.postflight.core.write_report", side_effect=save):
                        report = compare(self.source, self.output, self.options(), checkpoint_report=self.report)
                    self.assertEqual(report["verdict"], "FAIL" if payload == "exit" else "UNKNOWN")
                    other = "output" if role == "source" else "source"
                    # Initial, both probe outcomes, two successful-side streams, final.
                    self.assertEqual(len(snapshots), 6)
                    known = snapshots[2 if role == "source" else 1]
                    self.assertEqual(known["progress"][other]["pending_streams"], [2, 7])
                    failed = snapshots[1 if role == "source" else 2]
                    self.assertEqual(failed["checks"][-1]["name"], "probe")
                    self.assertEqual(failed["checks"][-1]["status"], "FAIL" if payload == "exit" else "UNKNOWN")
                    self.assertEqual(failed["checks"][-1]["role"], role)
                    for snapshot in snapshots:
                        self.assertEqual(snapshot["mapping"]["status"], "not_run")
                        self.assertFalse(any(c["name"] == "language_tag" for c in snapshot["checks"]))
                        if any(c["status"] == "FAIL" for c in snapshot["checks"]):
                            self.assertEqual(snapshot["verdict"], "FAIL")
                        self.assertNotIn(role, snapshot["media"])
                        self.assertIsNone(snapshot["stream_validation"][role])
                        self.assertFalse(snapshot["progress"][role]["probe_completed"])
                        self.assertIsNone(snapshot["progress"][role]["discovered_streams"])
                        self.assertIsNone(snapshot["progress"][role]["pending_streams"])
                        self.assertEqual(snapshot["decoded"][role], {})

    def test_failed_mapping_checkpoint_stops_before_decode_and_preserves_probe_snapshot(self):
        self.probe_boundary_fixture(block_stage=None, output_language="deu")
        replace = os.replace
        for mapping, policy, expected_mapping, expected_tags in (
                (((2, 2), (7, 7)), "preserve", "PASS", ["PASS", "FAIL"]),
                (((2, 7), (7, 2)), "preserve", "FAIL", []),
                (((2, 2), (7, 7)), "ignore", "PASS", [])):
            with self.subTest(mapping=mapping, policy=policy):
                replacements, prior, candidates = [], [], []
                def fail_replace(src, dst):
                    replacements.append(src)
                    if len(replacements) == 4:
                        prior.append(self.report.read_bytes())
                        candidates.append(json.loads(Path(src).read_text()))
                        raise OSError("fixture mapping checkpoint failure")
                    return replace(src, dst)
                with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
                    report = compare(self.source, self.output,
                                     self.options(stream_map=mapping, language_tags=policy),
                                     checkpoint_report=self.report)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(exit_code(report), 2)
                self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                self.assertEqual(len(replacements), 4, "failed sink must not be retried")
                self.assertEqual(self.report.read_bytes(), prior[0])
                self.assert_probe_only_snapshot(json.loads(prior[0]), {"source", "output"})
                candidate = candidates[0]
                self.assertEqual(candidate["report_state"], "in_progress")
                self.assertEqual(candidate["verdict"], "UNKNOWN" if policy == "ignore" else "FAIL")
                self.assertEqual(candidate["coverage"], "partial")
                self.assertEqual(candidate["mapping"]["status"], expected_mapping)
                self.assertEqual([c["status"] for c in candidate["checks"] if c["name"] == "language_tag"], expected_tags)
                self.assertFalse(any("duration" in c["name"] or "decode" in c["name"] for c in candidate["checks"]))
                self.assertEqual(report["mapping"]["status"], expected_mapping)
                self.assertEqual([c["status"] for c in report["checks"] if c["name"] == "language_tag"], expected_tags)
                if expected_mapping == "FAIL":
                    self.assertEqual(report["mapping"]["pairs"], [])
                    self.assertTrue(all(s["language_tag_status"] == "unmapped"
                                        for side in report["stream_validation"].values() for s in side[:2]))
                self.assertFalse(any("-map" in c["argv"] for c in report["commands"]))
                self.assertEqual(report["decoded"], {"source": {}, "output": {}})
                for progress in report["progress"].values():
                    self.assertEqual(progress["attempted_streams"], [])
                    self.assertEqual(progress["completed_streams"], [])
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_orderly_deadline_finalizes_checkpoint_and_stdout(self):
        self.fixture(hang_decode=True)
        # Five stub-tool cold starts must fit before the deadline; slow (macOS) runners need room.
        result = subprocess.run(self.command("--checkpoint-report", str(self.report), "--deadline", "6",
                                             "--timeout", "30"),
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads(self.report.read_text()))
        self.assertEqual(report["report_state"], "complete")
        self.assertEqual(report["termination"]["reason"], "deadline")
        self.assertEqual(report["progress"]["source"]["completed_streams"], [0])

    def test_write_failure_overrides_media_fail_and_final_pass(self):
        self.fixture()
        replace = os.replace
        for fail_at, mapping in ((5, ((0, 1), (1, 0))), (9, None)):
            with self.subTest(fail_at=fail_at):
                replacements = []
                def fail_replace(src, dst):
                    replacements.append(src)
                    if len(replacements) == fail_at:
                        raise OSError("fixture checkpoint failure")
                    return replace(src, dst)
                with patch("mediawrench.postflight.core.os.replace", side_effect=fail_replace):
                    report = compare(self.source, self.output, self.options(stream_map=mapping),
                                     checkpoint_report=self.report)
                self.assertEqual(report["verdict"], "UNKNOWN")
                self.assertEqual(report["report_state"], "complete")
                self.assertEqual(report["termination"]["reason"], "checkpoint_write")
                if fail_at == 5:
                    self.assertEqual(report["mapping"]["status"], "FAIL")
                else:
                    self.assertEqual(report["progress"]["output"]["completed_streams"], [0, 1])
                    saved = json.loads(self.report.read_text())
                    self.assertEqual(saved["verdict"], "UNKNOWN")
                    self.assertEqual(saved["report_state"], "in_progress")
                    self.assertEqual(saved["coverage"], "partial")
                self.assertFalse(list(self.root.glob(".postflight-*.tmp")))

    def test_cli_checkpoint_failure_exits_two_with_json(self):
        self.fixture()
        result = subprocess.run(self.command("--checkpoint-report", str(self.root / "missing" / "report.json")),
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["verdict"], "UNKNOWN")
        self.assertEqual(report["commands"], [])
        self.assertEqual(report["termination"]["reason"], "checkpoint_write")


if __name__ == "__main__":
    unittest.main()
