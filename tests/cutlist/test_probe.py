"""Real local ffprobe integration; absence is a skip, never a passing probe test."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[2]
# Real ffprobe integration: TRANSCRIPT_CUTLIST_FFPROBE if set, else ffprobe on PATH; skipped when neither exists.
FFPROBE = os.environ.get('TRANSCRIPT_CUTLIST_FFPROBE') or shutil.which('ffprobe')


def write_wav(path, frames=20000):
    with wave.open(str(path), 'wb') as media:
        media.setnchannels(1)
        media.setsampwidth(2)
        media.setframerate(8000)
        media.writeframes(b'\0\0' * frames)


class ProbeFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.media = self.directory / '-短 clip.wav'
        write_wav(self.media)
        self.media_before = self.media.read_bytes()
        self.srt = self.directory / 'cues.srt'
        self.srt.write_text('1\n00:00:00,100 --> 00:00:02,400\nkeep\n', encoding='utf-8')
        self.plan = self.directory / 'plan.json'
        self.set_plan({'keep': [1], 'padding': 0.2})
        self.prefix = self.directory / 'cuts'

    def set_plan(self, plan):
        self.plan.write_text(json.dumps(plan), encoding='utf-8')

    def run_cli(self, probe=None, *extra):
        args = [sys.executable, '-m', 'mediawrench.cutlist', str(self.srt),
                '--plan', str(self.plan), '--media-file', self.media.name,
                '--output-prefix', str(self.prefix)]
        if probe is not None:
            args += ['--ffprobe', str(probe)]
        return subprocess.run(args + list(extra), cwd=ROOT, capture_output=True,
                              text=True, encoding='utf-8', timeout=120)

    def assert_failure(self, result, message):
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('mediawrench cutlist: ', result.stderr)
        self.assertIn(message, result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        self.assertEqual(self.media.read_bytes(), self.media_before)


class ProbeCLITests(ProbeFixture, unittest.TestCase):
    def test_nonexecutable_probe_does_not_fall_back_to_declared_duration(self):
        self.set_plan({'keep': [1], 'duration': 2.5})
        probe = self.directory / 'nonexecutable'
        probe.write_text('not executable')
        self.assert_failure(self.run_cli(probe), 'executable')

    def test_missing_probe_does_not_search_path_or_guess(self):
        self.assert_failure(self.run_cli(self.directory / 'absent'), 'ffprobe')

    def test_legacy_mode_still_requires_duration(self):
        self.assert_failure(self.run_cli(), 'requires keep and duration')

    def test_invalid_executable_format_is_explicit(self):
        probe = self.directory / 'invalid-executable'
        probe.write_text('not an executable format')
        probe.chmod(0o755)
        self.assert_failure(self.run_cli(probe), 'Cannot execute explicit ffprobe')

    def test_query_disallows_probe_option(self):
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', str(self.srt),
                                 '--query', 'keep', '--ffprobe', '/absent'],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assert_failure(result, 'discovery only')

    def test_legacy_mode_needs_no_external_binary(self):
        self.set_plan({'keep': [1], 'duration': 2.5, 'padding': 0.2})
        result = subprocess.run([sys.executable, '-m', 'mediawrench.cutlist', str(self.srt),
                                 '--plan', str(self.plan), '--media-file', 'absent.wav',
                                 '--output-prefix', str(self.prefix)],
                                env={**os.environ, 'PATH': ''}, cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        self.assertNotIn('durationProbe', mapping)


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real ffprobe integration')
class RealProbeTests(ProbeFixture, unittest.TestCase):
    def test_duration_bounds_all_formats_and_replayable_evidence(self):
        result = self.run_cli(FFPROBE)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        mapping = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        llc = json.loads(self.prefix.with_suffix('.llc').read_text())
        rows = list(csv.reader(io.StringIO(self.prefix.with_suffix('.csv').read_text())))
        self.assertEqual([(c['start'], c['end']) for c in llc['cutSegments']], [(0, 2.5)])
        self.assertEqual([(float(a), float(b)) for a, b, _ in rows], [(0, 2.5)])
        self.assertEqual(report['cuts'], llc['cutSegments'])
        self.assertEqual(mapping['selectionPlan'], {'keep': [1], 'padding': 0.2})
        self.assertEqual(mapping['normalizedSelection']['durationMs'], 2500)
        evidence = mapping['durationProbe']
        self.assertEqual(evidence['durationMs'], 2500)
        self.assertEqual(evidence['mediaSha256'], hashlib.sha256(self.media_before).hexdigest())
        self.assertEqual(evidence['ffprobeSha256'], hashlib.sha256(Path(FFPROBE).read_bytes()).hexdigest())
        self.assertEqual(evidence['rounding'], 'floor-to-milliseconds')
        self.assertEqual(evidence['returnCode'], 0)
        raw = evidence['stdout'].encode('utf-8')
        self.assertEqual(evidence['stdoutSha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(evidence['version'])
        replay = subprocess.run(evidence['argv'], capture_output=True, timeout=120, check=True)
        self.assertEqual(replay.stdout, raw)
        self.assertEqual(self.media.read_bytes(), self.media_before)

    def test_dry_run_includes_evidence_without_outputs(self):
        result = self.run_cli(FFPROBE, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['durationProbe']['durationMs'], 2500)
        self.assertFalse(list(self.directory.glob('cuts.*')))

    def test_matching_declared_duration_accepted(self):
        self.set_plan({'keep': [1], 'duration': 2.5})
        result = self.run_cli(FFPROBE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_declared_duration_conflict_fails_even_if_cues_fit(self):
        self.set_plan({'keep': [1], 'duration': 3})
        self.assert_failure(self.run_cli(FFPROBE), 'conflicts')

    def test_missing_container_duration_fails_without_stream_guess(self):
        # One raw image is valid media with no format.duration in ffprobe JSON.
        self.media.write_bytes(b'P5\n1 1\n255\n\0')
        self.media_before = self.media.read_bytes()
        self.set_plan({'keep': [1], 'duration': 2.5})
        self.assert_failure(self.run_cli(FFPROBE), 'format.duration')

    def test_probe_failure_is_explicit(self):
        self.media.write_bytes(b'not media')
        self.media_before = self.media.read_bytes()
        self.assert_failure(self.run_cli(FFPROBE), 'ffprobe failed')

    def test_unselected_cue_beyond_probed_duration_rejected(self):
        with self.srt.open('a') as srt:
            srt.write('\n2\n00:00:02,500 --> 00:00:02,501\noutside\n')
        self.assert_failure(self.run_cli(FFPROBE), 'outside duration')

    def test_submillisecond_media_duration_floored_conservatively(self):
        write_wav(self.media, frames=20004)  # 2.5005 seconds
        result = self.run_cli(FFPROBE)
        self.assertEqual(result.returncode, 0, result.stderr)
        mapping = json.loads(Path(str(self.prefix) + '.provenance.json').read_text())
        self.assertEqual(mapping['durationProbe']['durationSeconds'], '2.500500')
        self.assertEqual(mapping['durationProbe']['durationMs'], 2500)
        self.assertEqual(mapping['cuts'][0]['endMs'], 2500)

    def test_library_entry_keeps_plan_unchanged(self):
        from mediawrench.cutlist import build_probed_timeline, read_srt, render_outputs
        plan = {'keep': [1], 'padding': 0.2}
        timeline = build_probed_timeline(read_srt(self.srt), plan,
                                         media_path=self.media, ffprobe_path=FFPROBE)
        mapping = json.loads(render_outputs(timeline, self.media.name)['provenance'])
        self.assertEqual(plan, {'keep': [1], 'padding': 0.2})
        self.assertEqual(mapping['selectionPlan'], plan)
        self.assertEqual(timeline.duration_ms, 2500)

    def test_missing_media_is_explicit(self):
        self.media.unlink()
        result = self.run_cli(FFPROBE)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Cannot read local probe input', result.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))


class ProbeDurationParserTests(unittest.TestCase):
    def test_invalid_duration_values_never_guessed(self):
        from mediawrench.cutlist import CutlistError
        from mediawrench.cutlist.probe import _duration_ms
        for value in [None, '', 'N/A', 'NaN', 'Infinity', '-1', '0', '0.000999',
                      2.5, True, {}, '1e3', '9007199254741', '1' * 65]:
            with self.subTest(value=value), self.assertRaises(CutlistError):
                _duration_ms(value)

    def test_exact_floor_at_millisecond_boundaries(self):
        from mediawrench.cutlist.probe import _duration_ms
        self.assertEqual(_duration_ms('0.001000'), 1)
        self.assertEqual(_duration_ms('2.500999'), 2500)


if __name__ == '__main__':
    unittest.main()
