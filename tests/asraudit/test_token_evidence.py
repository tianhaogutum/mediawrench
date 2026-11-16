"""Contract: pinned native tokens are read-only flag evidence, never scores.

Missing tokens -> null; [] -> an empty view. Token order, original fields and
zero-based source paths are exact. Optional timing pairs agree in milliseconds;
no token times, p, or t_dtw produce flags, scores, or navigation cues. Malformed
native tokens fail even on unflagged segments, before CLI sidecars are created.
Normalized-format token metadata is outside this pinned schema/view.
"""

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mediawrench.asraudit import AuditError, audit, parse_json
from mediawrench.asraudit.output import csv_flags, review_srt

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "asraudit" / "fixtures"
FORMAT = "whisper.cpp-json-v1.7.6"


class TokenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.native = parse_json((FIXTURES / "whisper_cpp_full_v1_7_6.json").read_bytes())

    def test_flag_view_paths_raw_fields_and_independent_copies(self):
        original = deepcopy(self.native)
        report = audit(self.native, input_format=FORMAT)
        self.assertEqual([f["segment"] for f in report["flags"]], [0, 1])
        for flag in report["flags"]:
            i = flag["segment"]
            path = f"transcription[{i}].tokens"
            view = flag["token_evidence"]
            self.assertEqual(view, {"source_path": path, "tokens": [
                {"source_path": f"{path}[{j}]", "token": token}
                for j, token in enumerate(original["transcription"][i]["tokens"])]})
            self.assertIsNone(flag["no_speech_prob"])
            self.assertIsNone(flag["avg_logprob"])
            self.assertEqual(flag["reasons"], ["adjacent_repeat"])
        self.assertNotIn("token_evidence", report["segments"][2])
        self.assertEqual(report["source_document"], original)
        self.assertEqual(report["normalized_document"]["segments"][0]["tokens"],
                         original["transcription"][0]["tokens"])
        report["flags"][0]["token_evidence"]["tokens"][0]["token"]["p"] = .5
        self.assertEqual(report["source_document"], original)
        self.assertEqual(report["normalized_document"]["segments"][0]["tokens"],
                         original["transcription"][0]["tokens"])
        self.assertEqual(self.native, original)

    def test_probability_and_token_timing_never_change_segment_evidence(self):
        report = audit(self.native, input_format=FORMAT)
        altered = deepcopy(self.native)
        for segment in altered["transcription"]:
            for token in segment["tokens"]:
                token.update(p=1, t_dtw=-1)
                token.pop("offsets", None)
                token.pop("timestamps", None)
        other = audit(altered, input_format=FORMAT)
        self.assertEqual(report["segments"], other["segments"])
        self.assertEqual(review_srt(report), review_srt(other))
        self.assertEqual(report["speech_corroborated_repeats"], [])
        self.assertEqual(report["segments"][2]["reasons"], [])  # p=0 alone does not flag
        self.native["transcription"][0].update(no_speech_prob=.9, avg_logprob=-2)
        flagged = audit(self.native, input_format=FORMAT)["flags"][0]
        self.assertEqual((flagged["no_speech_prob"], flagged["avg_logprob"]), (.9, -2))
        self.assertEqual(flagged["token_evidence"]["tokens"][0]["token"]["p"], .125)

    def test_missing_empty_and_normalized_metadata_are_distinct(self):
        del self.native["transcription"][0]["tokens"]
        self.native["transcription"][1]["tokens"] = []
        report = audit(self.native, input_format=FORMAT)
        self.assertIsNone(report["flags"][0]["token_evidence"])
        self.assertEqual(report["flags"][1]["token_evidence"], {
            "source_path": "transcription[1].tokens", "tokens": []})
        rows = list(csv.DictReader(io.StringIO(csv_flags(report))))
        self.assertEqual(rows[0]["token_evidence"], "")
        self.assertEqual(json.loads(rows[1]["token_evidence"])["tokens"], [])
        normalized = {"segments": [{"start": 0, "end": 0, "text": "x", "tokens": "opaque"}]}
        native_metadata = audit(normalized)
        self.assertIsNone(native_metadata["flags"][0]["token_evidence"])
        self.assertEqual(native_metadata["source_document"], normalized)

    def test_untimed_special_and_reversed_out_of_segment_times_remain_raw(self):
        record = self.native["transcription"][0]
        record["offsets"]["to"] = 0
        record["timestamps"]["to"] = "00:00:00,000"
        token = record["tokens"][0]
        token["offsets"] = {"from": 12000, "to": 11000}
        token["timestamps"] = {"from": "00:00:12,000", "to": "00:00:11,000"}
        report = audit(self.native, input_format=FORMAT)
        flag = report["flags"][0]
        self.assertEqual(flag["reasons"], ["invalid_timestamp"])
        self.assertEqual(flag["token_evidence"]["tokens"][0]["token"], token)
        self.assertEqual(flag["token_evidence"]["tokens"][1]["token"], record["tokens"][1])
        self.assertIn(0, report["unplaced_segments"])
        self.assertFalse(any(0 in r["segments"] for r in report["review_ranges"]))

    def test_raw_token_milliseconds_need_no_lossy_seconds_conversion(self):
        token = self.native["transcription"][0]["tokens"][0]
        millis = 9007199254740991
        hours, remainder = divmod(millis, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, ms = divmod(remainder, 1000)
        token["offsets"] = {"from": millis, "to": millis}
        token["timestamps"] = dict.fromkeys(("from", "to"), f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}")
        report = audit(self.native, input_format=FORMAT)
        self.assertEqual(report["flags"][0]["token_evidence"]["tokens"][0]["token"], token)

    def test_malformed_tokens_have_exact_paths_even_when_unflagged(self):
        cases = [(None, "tokens"), ({}, "tokens"), ([None], "tokens[0]")]
        base = self.native["transcription"][2]["tokens"][0]
        for field, values in {
            "text": [None, 3], "id": [True, -1, 1.5, "3", 2**31],
            "p": [None, True, "0.1", -.01, 1.01, float("nan"), float("inf")],
            "t_dtw": [None, True, "-1", -2, float("inf")],
            "offsets": [None, {"from": 0}, {"from": 0, "to": "10"}],
            "timestamps": [None, {"from": "00:00:00.000", "to": "00:00:00,010"}],
        }.items():
            for value in values:
                token = deepcopy(base)
                token[field] = value
                cases.append(([token], f"tokens[0].{field}"))
        for field in ("text", "id", "p", "t_dtw"):
            token = deepcopy(base)
            del token[field]
            cases.append(([token], f"tokens[0].{field}"))
        for field in ("offsets", "timestamps"):
            token = deepcopy(base)
            del token[field]
            cases.append(([token], f"tokens[0].{field}"))
        token = deepcopy(base)
        token["offsets"]["to"] += 10
        cases.append(([token], "tokens[0].offsets.to"))
        for tokens, suffix in cases:
            with self.subTest(tokens=tokens, path=suffix):
                doc = deepcopy(self.native)
                doc["transcription"][2]["tokens"] = tokens
                with self.assertRaises(AuditError) as caught:
                    audit(doc, input_format=FORMAT)
                self.assertIn("transcription[2]." + suffix, str(caught.exception))

    def test_cli_json_csv_dry_run_hash_and_errors_without_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "native.json"
            source.write_text(json.dumps(self.native, ensure_ascii=False), encoding="utf-8")
            before = source.read_bytes()
            outputs = [directory / f"report.{suffix}" for suffix in ("json", "csv", "srt")]
            args = [sys.executable, "-m", "mediawrench.asraudit", str(source),
                    "--input-format", FORMAT, "--json", str(outputs[0]),
                    "--csv", str(outputs[1]), "--review-srt", str(outputs[2])]
            def run(extra=()):
                return subprocess.run([*args, *extra], cwd=ROOT, capture_output=True,
                                      text=True, timeout=120)
            dry = run(["--dry-run"])
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(json.loads(dry.stdout)["flags"][0]["token_evidence"]["tokens"][0]["token"]["p"], .125)
            self.assertFalse(any(p.exists() for p in outputs))
            done = run()
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(outputs[0].read_text())
            rows = list(csv.DictReader(io.StringIO(outputs[1].read_text())))
            self.assertEqual(json.loads(rows[0]["token_evidence"]), report["flags"][0]["token_evidence"])
            self.assertEqual(rows[0]["no_speech_prob"], "")
            self.assertEqual(report["source"]["sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(source.read_bytes(), before)
            self.assertIn("00:00:00,000 --> 00:00:20,000", outputs[2].read_text())
            for path in outputs:
                path.unlink()
            for tokens, path in ((None, "tokens"), ([{"text": "x", "id": 1, "p": "0", "t_dtw": -1}], "tokens[0].p")):
                self.native["transcription"][2]["tokens"] = tokens
                source.write_text(json.dumps(self.native), encoding="utf-8")
                done = run()
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("transcription[2]." + path, done.stderr)
                self.assertFalse(any(p.exists() for p in outputs))


if __name__ == "__main__":
    unittest.main()
