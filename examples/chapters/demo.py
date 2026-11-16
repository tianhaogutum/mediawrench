"""Real offline sidecar roundtrips and a reproducible loss-control experiment."""
from pathlib import Path
from decimal import Decimal
import sys
import tempfile
import time

# Also runs from a source clone without installing mediawrench.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mediawrench.chapters import compare, dumps, json_text, loads, read_json, restore


def main():
    source = Path(__file__).with_name("chapters.info.json").read_text(encoding="utf-8")
    original = loads(source, "json")
    with tempfile.TemporaryDirectory(prefix="mediawrench-chapters-demo-") as directory:
        root = Path(directory)
        ff = dumps(original, "ffmetadata")
        metadata = root / "chapters.ffmetadata"
        metadata.write_text(ff.text, encoding="utf-8")
        reloaded = loads(metadata.read_text(encoding="utf-8"), "ffmetadata")
        json_result = dumps(reloaded, "json")
        saved_json = root / "restored.info.json"
        saved_json.write_text(json_result.text, encoding="utf-8")
        assert compare(original, loads(saved_json.read_text(encoding="utf-8"), "json"))["equal"]

        # A synthetic gap, unknown nested metadata, and a multiline title expose
        # failures of naive timestamp/title conversion. No media is downloaded.
        challenging = loads('''{"duration":12,"archive_id":"synthetic-demo",
          "chapters":[{"start_time":0,"end_time":3,"title":"A\\nB",
                       "custom":{"rating":0.1234567890123456789}},
                      {"start_time":5,"end_time":10,"title":"章节"}]}''', "json")
        text = dumps(challenging, "text", allow_lossy=True)
        sidecar = root / "loss-report.json"
        sidecar.write_text(json_text(text.report), encoding="utf-8")
        # Simulate standalone text read with the explicitly supplied duration.
        naive = loads(text.text, "text", duration="12")
        naive_diff = compare(challenging, naive)
        recovered = restore(read_json(sidecar.read_text(encoding="utf-8")))
        assert not naive_diff["equal"]
        assert compare(challenging, recovered)["equal"]

        start = time.perf_counter()
        for _ in range(1000):
            again = dumps(original, "ffmetadata")
            assert again.report["comparison"]["equal"]
        elapsed = time.perf_counter() - start
        result = {"json_ffmetadata_json_equal": True, "chapters": len(original.chapters),
                  "naive_text_normalized_differences": len(naive_diff["differences"]),
                  "audited_text_loss_events": len(text.report["losses"]),
                  "sidecar_source_recovery_equal": True,
                  "benchmark": {"iterations": 1000, "seconds": Decimal(f"{elapsed:.6f}"),
                                "note": "local synthetic timing, not a general performance claim"}}
        print(json_text(result), end="")


if __name__ == "__main__":
    main()
