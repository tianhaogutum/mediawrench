"""Real absolute-stream selection with competing zero/nonzero video origins."""

import copy
import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from mediawrench.cutlist import verify_probe_evidence
from .test_probe import FFPROBE, ROOT, ProbeFixture


@unittest.skipUnless(FFPROBE, 'needs ffprobe on PATH or TRANSCRIPT_CUTLIST_FFPROBE for real multistream integration')
class RealMultistreamProbeTests(ProbeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.media = self.directory / 'origin-multistream.mp4'
        shutil.copyfile(ROOT / 'tests/cutlist/fixtures/origin-multistream.mp4', self.media)
        self.media_before = self.media.read_bytes()
        self.set_plan({'keep': [1], 'frameRate': '24'})

    def query(self, index):
        # Independent explicit copy of the supported query, not a runtime helper.
        return [str(Path(FFPROBE).resolve()), '-v', 'error', '-protocol_whitelist', 'file',
                '-select_streams', str(index), '-show_program_version', '-show_entries',
                'format=duration,format_name,size:program_version=version:'
                'stream=index,codec_type,avg_frame_rate,r_frame_rate,start_time',
                '-of', 'json', '-i', str(self.media.resolve())]

    def selected_cli(self, index, *extra):
        return self.run_cli(FFPROBE, '--verify-frame-rate-stream', str(index),
                            '--verify-zero-origin', *extra)

    def export(self):
        result = self.selected_cli(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = Path(str(self.prefix) + '.provenance.json')
        return result, saved, json.loads(saved.read_text())

    def test_supported_queries_independently_establish_fixture_stream_contract(self):
        # Only the properties the tool relies on are pinned: absolute index, stream type,
        # video rates and video origins. The container duration (3.5 s with FFmpeg 7.0) and
        # the audio stream's own metadata are incidental and may vary across FFmpeg builds.
        for index, kind, start in [(0, 'audio', None), (1, 'video', 1),
                                   (2, 'video', 0), (3, None, None)]:
            with self.subTest(index=index):
                result = subprocess.run(self.query(index), capture_output=True, check=True, timeout=120)
                data = json.loads(result.stdout)
                self.assertAlmostEqual(float(data['format']['duration']), 3.5, delta=0.05)
                self.assertEqual(len(data['streams']), 0 if kind is None else 1)
                if kind is not None:
                    stream = data['streams'][0]
                    self.assertEqual((stream['index'], stream['codec_type']), (index, kind))
                    if kind == 'video':
                        self.assertEqual(Decimal(stream['start_time']), start)
                        self.assertEqual((stream['avg_frame_rate'], stream['r_frame_rate']), ('24/1', '24/1'))
        self.assertEqual(self.media.read_bytes(), self.media_before)

    def test_zero_video_index2_exports_and_required_proof_replays_exact_index_read_only(self):
        dry = self.selected_cli(2, '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(list(self.directory.glob('cuts.*')))
        result, saved, document = self.export()
        evidence = document['durationProbe']
        self.assertEqual(json.loads(dry.stdout)['durationProbe'], evidence)
        self.assertEqual(evidence['schemaVersion'], 3)
        self.assertEqual(evidence['argv'], self.query(2))
        self.assertEqual(evidence['frameRateValidation']['streamIndex'], 2)
        origin = dict(evidence['zeroOriginValidation'])
        self.assertEqual(Decimal(origin.pop('startTimeSeconds')), 0)
        self.assertEqual(origin, {'streamIndex': 2, 'policy': 'exact-zero-stream-start-time'})
        cut = document['cuts'][0]
        self.assertEqual((cut['startFrame'], cut['endFrame']), (2, 58))
        llc = json.loads(self.prefix.with_suffix('.llc').read_text())
        rows = list(csv.reader(self.prefix.with_suffix('.csv').read_text().splitlines()))
        self.assertEqual(llc['cutSegments'], json.loads(result.stdout)['cuts'])
        self.assertEqual([float(x) for x in rows[0][:2]], [cut['start'], cut['end']])
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        with patch('mediawrench.cutlist.probe.subprocess.run', wraps=subprocess.run) as replay:
            report = verify_probe_evidence(saved, require_zero_origin=True,
                                            srt_path=self.srt, plan_path=self.plan)
        self.assertEqual(report['status'], 'verified')
        self.assertTrue(report['replayed'])
        self.assertEqual(report['issues'], [])
        replay.assert_called_once()
        self.assertEqual(replay.call_args.args[0], self.query(2))
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_nonzero_audio_and_missing_absolute_indices_fail_without_fallback_even_dry_run(self):
        # The raw start_time text after the colon is whatever this ffprobe build prints.
        for index, message in [(1, 'Video stream 1 start_time must be exactly zero: 1'),
                               (0, 'selected video stream index 0'),
                               (3, 'selected video stream index 3')]:
            for extra in [[], ['--dry-run']]:
                with self.subTest(index=index, extra=extra):
                    result = self.selected_cli(index, *extra)
                    self.assert_failure(result, message)
                    self.assertEqual(result.stdout, '')

    def test_rate_matching_and_opt_out_queries_remain_unchanged(self):
        self.set_plan({'keep': [1], 'frameRate': '25'})
        for extra in [[], ['--dry-run']]:
            self.assert_failure(self.selected_cli(2, *extra), 'Video frame rate mismatch for stream 2')
        self.set_plan({'keep': [1], 'frameRate': '24'})
        for index, schema in [(None, 1), (1, 2)]:
            extra = [] if index is None else ['--verify-frame-rate-stream', str(index)]
            result = self.run_cli(FFPROBE, *extra, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(result.stdout)['durationProbe']
            self.assertEqual(evidence['schemaVersion'], schema)
            self.assertNotIn('zeroOriginValidation', evidence)
            self.assertNotIn('start_time', evidence['argv'][evidence['argv'].index('-show_entries') + 1])
            if index is not None:
                self.assertEqual(evidence['frameRateValidation']['streamIndex'], index)
        self.assertFalse(list(self.directory.glob('cuts.*')))

    def test_required_saved_proof_cannot_be_retargeted_to_another_real_stream(self):
        _, saved, original = self.export()
        # Even coherent edits to both index summaries and argv cannot reinterpret
        # raw index2 evidence as evidence about index1, audio0 or nonexistent3.
        for index in [1, 0, 3]:
            document = copy.deepcopy(original)
            evidence = document['durationProbe']
            for key in ['frameRateValidation', 'zeroOriginValidation']:
                evidence[key]['streamIndex'] = index
            evidence['argv'] = self.query(index)
            saved.write_text(json.dumps(document))
            with self.subTest(index=index), patch('mediawrench.cutlist.probe.subprocess.run') as replay:
                report = verify_probe_evidence(saved, require_zero_origin=True,
                                                srt_path=self.srt, plan_path=self.plan)
                self.assertEqual(report['status'], 'stale')
                self.assertFalse(report['replayed'])
                self.assertTrue({'recorded-frame-rate', 'recorded-zero-origin'} <=
                                {issue['code'] for issue in report['issues']})
                replay.assert_not_called()
        self.assertEqual(self.media.read_bytes(), self.media_before)
