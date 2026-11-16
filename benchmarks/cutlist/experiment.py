"""Offline, serial synthetic format E2E and provenance sweep experiment.

Run from the repository root: python benchmarks/cutlist/experiment.py
No media, network, external process or persistent output is used.
"""

import argparse
import csv
import json
from pathlib import Path
import platform
import statistics
import tempfile
import sys
import time

# Allow running this script directly from a clone without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

import pysubs2  # noqa: E402

from mediawrench.cutlist import build_timeline, load_plan, output_paths, read_srt, render_outputs, write_outputs  # noqa: E402
from mediawrench.cutlist.core import _overlap_ids  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


def naive_overlaps(intervals, spans):
    return [tuple(sorted(cue_id for a, b, cue_id in spans if a < end and start < b))
            for start, end in intervals]


def timed(function, repeats):
    measurements = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = function()
        measurements.append(time.perf_counter() - start)
    return result, statistics.median(measurements)


def example_metrics():
    cues = read_srt(ROOT / 'examples/cutlist/interview.srt')
    plan = load_plan(ROOT / 'examples/cutlist/selection.json')
    timeline = build_timeline(cues, plan)
    kept = [cue for cue in cues if cue.id in set(timeline.keep_ids) - set(timeline.drop_ids)]
    excluded = {ms for cue in cues if cue.id in timeline.drop_ids for ms in range(cue.start_ms, cue.end_ms)}
    baseline_ms = {ms for cue in kept for ms in range(cue.start_ms, cue.end_ms)}
    normalized_ms = {ms for cut in timeline.cuts for ms in range(cut.start_ms, cut.end_ms)}
    assert len(baseline_ms & excluded) == 1000
    assert not normalized_ms & excluded
    return {'plainSelectedCueImportCuts': len(kept), 'normalizedCuts': len(timeline.cuts),
            'plainSelectedCueImportExcludedOverlapMs': len(baseline_ms & excluded),
            'normalizedExcludedOverlapMs': len(normalized_ms & excluded)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=5000)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.size <= 20000 or not 1 <= args.repeats <= 10:
        parser.error('size must be 1..20000; repeats must be 1..10')
    with tempfile.TemporaryDirectory(prefix='transcript-cutlist-experiment-') as directory:
        directory = Path(directory)
        srt = directory / 'synthetic.srt'
        subs = pysubs2.SSAFile()
        for i in range(args.size):
            subs.append(pysubs2.SSAEvent(start=i * 1000, end=i * 1000 + 400, text=f'Synthetic cue {i + 1} 你好'))
        subs.save(str(srt), format_='srt', encoding='utf-8')
        plan = {'keep': [{'from': 1, 'to': args.size}], 'drop': list(range(5, args.size + 1, 5)),
                'padding': 0.1, 'duration': args.size}
        start = time.perf_counter()
        cues = read_srt(srt)
        timeline = build_timeline(cues, plan)
        contents = render_outputs(timeline, 'synthetic.wav')
        paths = output_paths(directory / 'synthetic-proj')
        write_outputs(paths, contents)
        llc = json.loads(paths['llc'].read_text(encoding='utf-8'))
        with paths['csv'].open(encoding='utf-8', newline='') as handle:
            rows = list(csv.reader(handle))
        mapping = json.loads(paths['provenance'].read_text(encoding='utf-8'))
        expected = [(max(0, i * 1000 - 100) / 1000, (i * 1000 + 500) / 1000)
                    for i in range(args.size) if (i + 1) % 5]
        assert [(cut['start'], cut['end']) for cut in llc['cutSegments']] == expected
        assert [(float(row[0]), float(row[1])) for row in rows] == expected
        assert [(cut['start'], cut['end']) for cut in mapping['cuts']] == expected
        e2e_seconds = time.perf_counter() - start
        intervals = [(cut.start_ms, cut.end_ms) for cut in timeline.cuts]
        spans = [(cue.start_ms, cue.end_ms, cue.id) for cue in cues]
        baseline, baseline_seconds = timed(lambda: naive_overlaps(intervals, spans), args.repeats)
        sweep, sweep_seconds = timed(lambda: _overlap_ids(intervals, spans), args.repeats)
        assert sweep == baseline
        result = {
            'python': platform.python_version(), 'pysubs2': pysubs2.__version__,
            'platform': platform.platform(), 'syntheticExample': example_metrics(),
            'scaling': {'inputCues': len(cues), 'finalCuts': len(timeline.cuts),
                        'outputBytes': sum(path.stat().st_size for path in paths.values()),
                        'formatE2eSeconds': round(e2e_seconds, 6), 'threeFormatsAgree': True,
                        'repeatsSerial': args.repeats, 'naiveOverlapComparisons': len(intervals) * len(spans),
                        'naiveOverlapMedianSeconds': round(baseline_seconds, 6),
                        'sweepOverlapMedianSeconds': round(sweep_seconds, 6),
                        'overlapMappingsEqual': sweep == baseline},
            'scope': 'Synthetic workload; isolated provenance lookup timings, not a media cutting benchmark. No timing threshold asserted.'}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
