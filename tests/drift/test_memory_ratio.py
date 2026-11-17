from .tools import ffmpeg, require_runtime, speech_like

require_runtime()

import importlib

import numpy as np
import soundfile as sf

from mediawrench.drift.media import probe
from mediawrench.drift.render import render
from mediawrench.drift.review import SCHEMA


def test_high_rate_ratio_keeps_actual_resampler_chunks_small(tmp_path, monkeypatch):
    reference, target = tmp_path / "384k.wav", tmp_path / "1k.wav"
    source = np.zeros((2000, 2), dtype="float32")
    source[500] = 0.5
    sf.write(target, source, 1000, subtype="FLOAT")
    sf.write(reference, np.zeros((768000, 2), dtype="float32"), 384000, subtype="FLOAT")
    session = {"schema": SCHEMA, "reference": probe(reference), "target": probe(target),
               "anchors": [{"id": "start", "reference_seconds": 0.1, "target_seconds": 0.1, "accepted": True},
                           {"id": "end", "reference_seconds": 1.5, "target_seconds": 1.5, "accepted": True}]}
    module = importlib.import_module("mediawrench.drift.render")
    original = module.soxr.ResampleStream
    sizes = []
    class MeasuredStream:
        def __init__(self, *args, **kwargs):
            self.stream = original(*args, **kwargs)
        def resample_chunk(self, *args, **kwargs):
            output = self.stream.resample_chunk(*args, **kwargs)
            sizes.append(output.nbytes)
            return output
    monkeypatch.setattr(module.soxr, "ResampleStream", MeasuredStream)
    output = tmp_path / "bounded.wav"
    result = render(session, output, verify=False)
    assert len(sizes) > 2
    assert max(sizes) < 4 * 1024 * 1024
    assert result["output"]["frames"] == 768000
    audio, _ = sf.read(output, always_2d=True)
    assert abs(np.argmax(np.abs(audio[:, 0])) - 192000) <= 1
