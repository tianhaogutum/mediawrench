"""Callers can require origin proof instead of accepting weaker saved evidence."""

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from mediawrench.cutlist import CutlistError, render_outputs, verify_probe_evidence
from .test_origin_probe import OriginFixture
from .test_probe import ROOT


class RequiredOriginTests(OriginFixture, unittest.TestCase):
    def cli(self, *extra):
        return subprocess.run([sys.executable, '-m', 'mediawrench.cutlist',
                               'verify-probe-evidence', str(self.saved), *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def save(self, document):
        self.saved.write_text(json.dumps(document))

    def test_legacy_evidence_rejected_without_replay_but_default_preserved(self):
        for index, schema in [(None, 1), (2, 2)]:
            timeline = self.build(video_stream_index=index, verify_zero_origin=False)
            document = json.loads(render_outputs(timeline, self.media.name)['provenance'])
            self.assertEqual(document['durationProbe']['schemaVersion'], schema)
            self.save(document)
            self.response_file.write_bytes(self.raw)
            before = {p: p.read_bytes() for p in self.directory.iterdir()}
            with self.subTest(schema=schema):
                result = self.cli('--require-zero-origin')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('requires durationProbe schemaVersion 3', result.stderr)
                self.assertFalse(result.stdout)
                self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})
                with patch('mediawrench.cutlist.probe.subprocess.run') as run:
                    with self.assertRaisesRegex(CutlistError, 'requires durationProbe schemaVersion 3'):
                        verify_probe_evidence(self.saved, require_zero_origin=True)
                    run.assert_not_called()
                # Defaults still accept supported evidence without origin proof.
                for kwargs in [{}, {'require_zero_origin': False}]:
                    report = verify_probe_evidence(self.saved, **kwargs)
                    self.assertEqual(report['status'], 'verified')
                self.assertEqual(self.cli().returncode, 0)
                self.calls.unlink()
                document['durationProbe']['mediaPath'] = '/unavailable/clip.wav'
                document['durationProbe']['argv'][-1] = '/unavailable/clip.wav'
                self.save(document)
                with patch('mediawrench.cutlist.probe._sha256') as hashed:
                    with self.assertRaisesRegex(CutlistError, 'requires durationProbe schemaVersion 3'):
                        verify_probe_evidence(self.saved, require_zero_origin=True)
                    hashed.assert_not_called()

    def test_schema3_required_success_is_read_only_and_exact_replay(self):
        self.record()
        self.response_file.write_bytes(self.raw)
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        result = self.cli('--require-zero-origin', '--srt', str(self.srt), '--plan', str(self.plan))
        # render_outputs in this fixture has no source hashes; add explicit ones.
        self.assertEqual(result.returncode, 2)
        self.assertIn('source.srtSha256', result.stderr)
        self.assertFalse(self.calls.exists())
        self.document['source'] = {'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
                                   'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        self.save(self.document)
        before[self.saved] = self.saved.read_bytes()
        result = self.cli('--require-zero-origin', '--srt', str(self.srt), '--plan', str(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'verified')
        self.assertTrue(report['replayed'])
        self.assertEqual(report['issues'], [])
        self.assertEqual(json.loads(self.calls.read_text()), self.argv)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(set(self.directory.iterdir()), set(before) | {self.calls})
        self.assertEqual(verify_probe_evidence(self.saved, require_zero_origin=True), report)

    def test_requirement_is_strict_boolean_before_file_access(self):
        for value in [None, 0, 1, 'true', [], {}]:
            with self.subTest(value=value), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                with self.assertRaisesRegex(CutlistError, 'require_zero_origin must be a boolean'):
                    verify_probe_evidence(self.saved, require_zero_origin=value)
                run.assert_not_called()

    def test_requirement_does_not_bypass_invalid_or_inconsistent_schema3(self):
        original = self.record()
        for mutation in ['summary-missing', 'policy', 'index', 'argv', 'grid-missing', 'grid-bool',
                         'grid-nonzero', 'summary-nonzero', 'raw-nonzero', 'raw-missing',
                         'rate', 'plan', 'grid-rate']:
            document = copy.deepcopy(original)
            evidence = document['durationProbe']
            origin = evidence['zeroOriginValidation']
            invalid = mutation in ['summary-missing', 'policy', 'index', 'argv', 'grid-missing', 'grid-bool']
            if mutation == 'summary-missing':
                del evidence['zeroOriginValidation']
            elif mutation == 'policy':
                origin['policy'] = 'near-zero'
            elif mutation == 'index':
                origin['streamIndex'] = 0
            elif mutation == 'argv':
                evidence['argv'] += ['-show_frames']
            elif mutation == 'grid-missing':
                del document['quantization']['originSeconds']
            elif mutation.startswith('grid-') and mutation != 'grid-rate':
                document['quantization']['originSeconds'] = False if mutation == 'grid-bool' else 1
            elif mutation == 'summary-nonzero':
                origin['startTimeSeconds'] = '1.000000'
            elif mutation.startswith('raw-'):
                response = json.loads(evidence['stdout'])
                if mutation == 'raw-missing':
                    del response['streams'][0]['start_time']
                else:
                    response['streams'][0]['start_time'] = '1.000000'
                evidence['stdout'] = json.dumps(response)
                evidence['stdoutSha256'] = hashlib.sha256(evidence['stdout'].encode()).hexdigest()
            elif mutation == 'rate':
                evidence['frameRateValidation']['avgFrameRate'] = '25/1'
            elif mutation == 'plan':
                document['selectionPlan']['frameRate'] = '25'
            else:
                document['quantization']['frameRate']['numerator'] = 25
            self.save(document)
            with self.subTest(mutation=mutation):
                with patch('mediawrench.cutlist.probe.subprocess.run') as run:
                    if invalid:
                        with self.assertRaises(CutlistError):
                            verify_probe_evidence(self.saved, require_zero_origin=True)
                    else:
                        report = verify_probe_evidence(self.saved, require_zero_origin=True)
                        self.assertTrue(report['stale'])
                        self.assertFalse(report['replayed'])
                        code = 'recorded-frame-rate' if mutation in ['rate', 'plan', 'grid-rate'] else 'recorded-zero-origin'
                        self.assertIn(code, [x['code'] for x in report['issues']])
                    run.assert_not_called()
                result = self.cli('--require-zero-origin')
                self.assertEqual(result.returncode, 2 if invalid else 1, result.stderr)
                self.assertFalse(self.calls.exists())

    def test_required_replay_changed_or_unknown_origin_never_verifies(self):
        self.record()
        for value in ['1.000000', 'N/A', None, '0']:
            self.response['streams'][0]['start_time'] = value
            self.response_file.write_text(json.dumps(self.response))
            with self.subTest(value=value):
                result = self.cli('--require-zero-origin')
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(report['replayed'])
                self.assertIn('replay-zero-origin', [x['code'] for x in report['issues']])

    def test_required_origin_retains_media_executable_and_source_preflight(self):
        self.record()
        self.document['source'] = {'srtSha256': hashlib.sha256(self.srt.read_bytes()).hexdigest(),
                                   'planSha256': hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        self.save(self.document)
        for current, code in [(self.media, 'media-sha256'), (self.probe, 'ffprobe-sha256'),
                              (self.srt, 'srt-sha256'), (self.plan, 'plan-sha256')]:
            before = current.read_bytes()
            current.write_bytes(before + b' ')
            with self.subTest(code=code), patch('mediawrench.cutlist.probe.subprocess.run') as run:
                report = verify_probe_evidence(self.saved, require_zero_origin=True,
                                               srt_path=self.srt, plan_path=self.plan)
                self.assertFalse(report['replayed'])
                self.assertIn(code, [x['code'] for x in report['issues']])
                run.assert_not_called()
            current.write_bytes(before)

    def test_required_cli_still_rejects_incomplete_source_pair(self):
        self.record()
        result = self.cli('--require-zero-origin', '--srt', str(self.srt))
        self.assertEqual(result.returncode, 2)
        self.assertIn('--srt and --plan must be supplied together', result.stderr)
        self.assertFalse(result.stdout)
        self.assertFalse(self.calls.exists())
