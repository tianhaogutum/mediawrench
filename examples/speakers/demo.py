"""Run the real library and pysubs2 SRT roundtrip on synthetic local files."""
from pathlib import Path
import json
import sys

# Allow running this script directly from a clone without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pysubs2  # noqa: E402
from mediawrench.speakers import merge, parse_rttm, render_json, render_markdown, render_srt, write_atomic  # noqa: E402
from mediawrench.speakers.core import load_json  # noqa: E402


def main():
    here = Path(__file__).resolve().parent
    asr = load_json((here / "whisper.json").read_text(encoding="utf-8"))
    rttm = parse_rttm((here / "interview.rttm").read_text(encoding="utf-8"))
    aliases = load_json((here / "aliases.json").read_text(encoding="utf-8"))
    result = merge(asr, rttm, aliases=aliases)
    assert [item["speaker"] for item in result["items"]] == [
        "Alice", "AMBIGUOUS", "Bob", "UNKNOWN", "Alice", "Alice", "Alice", "Alice", "Alice"]
    assert [item["source_word"] for item in result["items"] if item["source_word"] is not None] == list(range(7))
    for si, segment in enumerate(asr["segments"]):
        assert "".join(i["text"] for i in result["items"] if i["source_segment"] == si) == segment["text"]
    out = here / "output"
    out.mkdir(exist_ok=True)
    srt = render_srt(result)
    reparsed = pysubs2.SSAFile.from_string(srt, "srt", keep_html_tags=True)
    assert len(reparsed) == len(result["turns"])
    for suffix, text in (("json", render_json(result)), ("md", render_markdown(result)), ("srt", srt)):
        write_atomic(out / f"interview.{suffix}", text)
    print(json.dumps({"items": len(result["items"]), "source_words": 7,
                      "turns": len(result["turns"]), "srt_reparsed_cues": len(reparsed),
                      "segment_precision_items": sum(i["precision"] == "segment" for i in result["items"]),
                      "output": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
