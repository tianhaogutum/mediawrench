"""Deterministic, offline synthetic reconciliation/scale experiment, no files written."""

import json
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mediawrench.audacity import export_subtitles, import_labels
from mediawrench.audacity.formats import Cue, parse_srt, render_srt


def run(count):
    # Repeated payloads intentionally defeat text-only identity matching.
    cues = [Cue(str(i + 1), i * 2000 + 1000, i * 2000 + 1800, "identical\n中文 \\N\ttext")
            for i in range(count)]
    source = render_srt(cues).encode()
    started = time.perf_counter()
    labels, sidecar = export_subtitles(source, "srt")
    exported = time.perf_counter()
    rows = []
    for line in labels.splitlines():
        start, end, text = line.split("\t")
        # Offset by exactly 250ms using integer arithmetic, avoiding float drift.
        start_ms = int(start.replace(".", "")) + 250
        end_ms = int(end.replace(".", "")) + 250
        rows.append(f"{start_ms // 1000}.{start_ms % 1000:03d}\t"
                    f"{end_ms // 1000}.{end_ms % 1000:03d}\t{text}\n")
    edited = "".join(rows)
    import_started = time.perf_counter()
    output, report = import_labels(edited, sidecar)
    finished = time.perf_counter()
    result, _ = parse_srt(output)
    if len(result) != count or report["output_count"] != count:
        raise RuntimeError("Cue count changed")
    for i, cue in enumerate(result):
        if (cue.start_ms, cue.end_ms, cue.text) != (cues[i].start_ms + 250,
                                                   cues[i].end_ms + 250, cues[i].text):
            raise RuntimeError(f"Roundtrip changed cue {i}")
    return {"cues": count, "exact_payloads": count, "moved_cues": len(report["actions"]),
            "export_seconds": round(exported - started, 6),
            "import_seconds": round(finished - import_started, 6)}


if __name__ == "__main__":
    print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                      "runs": [run(n) for n in (1000, 5000, 10000)]}, indent=2))
