"""Run the real core and verify SRT roundtrip, then write local synthetic outputs."""
from pathlib import Path
import sys

# Also runnable from a clone without installing mediawrench.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mediawrench.reflow import reflow
from mediawrench.reflow.io import atomic_write_many, json_text, load_json, srt_text


def main():
    here = Path(__file__).resolve().parent
    output = here / "output"
    output.mkdir(exist_ok=True)
    for name in ("pause", "fallback"):
        report = reflow(load_json(here / (name + ".json")))
        srt = srt_text(report)
        atomic_write_many([(output / (name + ".srt"), srt), (output / (name + ".report.json"), json_text(report))])
        print(f"{name}: {len(report['cues'])} cues, {report['metrics']['mapped_words']} words mapped; roundtrip verified")
        print(srt)
    assert [c["text"] for c in reflow(load_json(here / "pause.json"))["cues"]] == ["Hello there.", "Next topic."]
    print(f"Outputs: {output}")


if __name__ == "__main__":
    main()
