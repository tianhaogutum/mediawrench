from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from mediawrench.asraudit import AuditError, Thresholds, audit, parse_json


FIXTURES = Path(__file__).resolve().parent / "fixtures"
FORMAT = "whisper.cpp-json-v1.7.6"


class WhisperCppTests(unittest.TestCase):
    def setUp(self):
        self.native = parse_json((FIXTURES / "whisper_cpp_v1_7_6.json").read_bytes())

    def test_pinned_fixture_matches_normalized_audit_and_expected_evidence(self):
        provenance = parse_json((FIXTURES / "whisper_cpp_provenance.json").read_bytes())
        for filename, checksum in provenance["files"].items():
            self.assertEqual(hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest(), checksum)
        normalized = parse_json((FIXTURES / "whisper_cpp_normalized.json").read_bytes())
        speech = parse_json((FIXTURES / "whisper_cpp_speech.json").read_bytes())
        config = Thresholds(outside_speech_fraction=.2)
        report = audit(self.native, speech, config, input_format=FORMAT)
        expected = audit(normalized, speech, config)
        for key in ("segments", "flags", "repeat_runs", "review_ranges", "unplaced_segments",
                    "speech_intervals", "speech_corroborated_repeats", "thresholds"):
            self.assertEqual(report[key], expected[key], key)
        self.assertEqual(report["normalized_document"], normalized)
        self.assertEqual(report["source_document"], self.native)
        self.assertEqual(report["review_ranges"], provenance["expected"]["review_ranges"])
        self.assertEqual([s["outside_speech_fraction"] for s in report["segments"]], [.2, .2, 0])
        self.assertEqual([f["segment"] for f in report["flags"]], [0, 1])
        self.assertEqual(report["conversion"]["upstream"], {
            k: provenance["upstream"][k] for k in ("version", "commit", "source")})
        endpoint = report["conversion"]["segments"][2]["start"]
        self.assertEqual(endpoint, {"offset_path": "transcription[2].offsets.from",
                                    "timestamp_path": "transcription[2].timestamps.from",
                                    "milliseconds": 21120, "seconds": 21.12})

    def test_explicit_selection_required_and_unknown_format_rejected(self):
        with self.assertRaises(AuditError):
            audit(self.native)
        with self.assertRaisesRegex(AuditError, "input_format"):
            audit(self.native, input_format="guess")

    def test_text_identifiers_full_output_metadata_and_input_are_preserved(self):
        self.native["source_id"] = "interview/083"
        record = self.native["transcription"][0]
        record["id"] = "clip-A/segment-9"
        record["speaker"] = "0"
        record["tokens"] = [{"id": 50364, "text": "[_BEG_]", "p": .8, "t_dtw": -1}]
        original = deepcopy(self.native)
        report = audit(self.native, input_format=FORMAT)
        self.assertEqual(report["source_document"], original)
        self.assertEqual(report["normalized_document"]["source_id"], "interview/083")
        converted = report["normalized_document"]["segments"][0]
        for key, value in record.items():
            self.assertEqual(converted[key], value, key)
        self.assertEqual(report["segments"][0]["text"], record["text"])
        self.assertIsNone(report["segments"][0]["no_speech_prob"])
        self.assertIsNone(report["segments"][0]["avg_logprob"])
        report["source_document"]["transcription"][0]["text"] = "changed copy"
        converted["tokens"][0]["text"] = "changed copy"
        self.assertEqual(self.native, original)

    def test_unsupported_variants_and_unit_conflicts_have_concrete_paths(self):
        cases = [
            ("offsets", None, "transcription[0].offsets"),
            ("offsets", {"from": 0, "to": "10000"}, "transcription[0].offsets.to"),
            ("offsets", {"from": False, "to": 10000}, "transcription[0].offsets.from"),
            ("offsets", {"from": 0, "to": 10}, "transcription[0].offsets.to"),
            ("offsets", {"from": 0, "to": 10000.5}, "transcription[0].offsets.to"),
            ("offsets", {"from": 0, "to": 10000, "unit": "seconds"}, "transcription[0].offsets.unit"),
            ("offsets", {"from": 0}, "transcription[0].offsets.to"),
            ("timestamps", None, "transcription[0].timestamps"),
            ("timestamps", {"from": "00:00:00.000", "to": "00:00:10,000"}, "transcription[0].timestamps.from"),
            ("timestamps", {"from": "00:60:00,000", "to": "00:00:10,000"}, "transcription[0].timestamps.from"),
            ("timestamps", {"from": 0, "to": 10}, "transcription[0].timestamps.from"),
            ("timestamps", {"from": "9" * 5000 + ":00:00,000", "to": "00:00:10,000"}, "transcription[0].timestamps.from"),
            ("start", 0, "transcription[0].start"),
            ("time_unit", "seconds", "transcription[0].time_unit"),
            ("text", None, "transcription[0].text"),
        ]
        for key, value, path in cases:
            with self.subTest(key=key, value=value):
                doc = deepcopy(self.native)
                doc["transcription"][0][key] = value
                with self.assertRaises(AuditError) as caught:
                    audit(doc, input_format=FORMAT)
                self.assertIn(path, str(caught.exception))
        for key in ("segments", "time_unit"):
            doc = deepcopy(self.native)
            doc[key] = [] if key == "segments" else "seconds"
            with self.assertRaises(AuditError) as caught:
                audit(doc, input_format=FORMAT)
            self.assertIn("input." + key, str(caught.exception))
        for doc, path in (([], "input"), ({}, "transcription"),
                          ({"transcription": [None]}, "transcription[0]")):
            with self.assertRaises(AuditError) as caught:
                audit(doc, input_format=FORMAT)
            self.assertIn(path, str(caught.exception))

    def test_missing_fields_nonfinite_values_and_overflow_report_paths(self):
        for field in ("offsets", "timestamps", "text"):
            doc = deepcopy(self.native)
            del doc["transcription"][0][field]
            with self.assertRaises(AuditError) as caught:
                audit(doc, input_format=FORMAT)
            self.assertIn("transcription[0]." + field, str(caught.exception))
        for value in (float("nan"), float("inf"), -float("inf"), -1, 10**30):
            doc = deepcopy(self.native)
            doc["transcription"][0]["offsets"]["to"] = value
            with self.assertRaises(AuditError) as caught:
                audit(doc, input_format=FORMAT)
            self.assertIn("transcription[0].offsets.to", str(caught.exception))
        # Parsing non-standard constants and exponent overflow must locate them,
        # even before the adapter runs in the CLI.
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            raw = '{"transcription":[{"offsets":{"to":' + literal + '}}]}'
            with self.assertRaises(AuditError) as caught:
                parse_json(raw)
            self.assertIn("transcription[0].offsets.to", str(caught.exception))

    def test_empty_native_and_invalid_finite_timing_follow_normalized_contract(self):
        self.assertEqual(audit({"transcription": []}, input_format=FORMAT)["flags"], [])
        record = self.native["transcription"][0]
        record["offsets"]["to"] = 0
        record["timestamps"]["to"] = "00:00:00,000"
        result = audit(self.native, input_format=FORMAT)
        self.assertIn(0, result["unplaced_segments"])
        self.assertEqual(result["segments"][0]["reasons"], ["invalid_timestamp"])

    def test_long_hours_supported_but_lossy_second_conversion_rejected(self):
        record = self.native["transcription"][0]
        record["offsets"]["to"] = 360000010
        record["timestamps"]["to"] = "100:00:00,010"
        result = audit(self.native, input_format=FORMAT)
        self.assertEqual(result["segments"][0]["end"], 360000.01)
        millis = 9007199254740991
        hours, remainder = divmod(millis, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, ms = divmod(remainder, 1000)
        record["offsets"]["to"] = millis
        record["timestamps"]["to"] = f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"
        with self.assertRaisesRegex(AuditError, r"transcription\[0\].offsets.to:.*precision loss"):
            audit(self.native, input_format=FORMAT)


if __name__ == "__main__":
    unittest.main()
