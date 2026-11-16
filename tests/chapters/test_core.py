from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
import unittest

from mediawrench.chapters import (Chapter, ChapterError, Document, LossError, compare,
                               dumps, json_text, loads, read_json, restore, seconds_us)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "chapters"


def basic():
    return loads((EXAMPLES / "chapters.info.json").read_text(encoding="utf-8"), "json")


def ff(body):
    return ";FFMETADATA1\n[CHAPTER]\n" + body


class ExactTimeTests(unittest.TestCase):
    def test_decimal_and_fraction_are_exact(self):
        self.assertEqual(seconds_us(Decimal("30.000001")), 30_000_001)
        self.assertEqual(seconds_us(Fraction(1001, 1000)), 1_001_000)

    def test_decimal_context_does_not_round(self):
        with localcontext() as context:
            context.prec = 2
            doc = loads('{"chapters":[{"start_time":0.000001,"end_time":123456.123456}]}', "json")
            self.assertEqual(doc.chapters[0].end_us, 123456123456)
            self.assertEqual(loads(dumps(doc, "json").text, "json"), doc)

    def test_submicrosecond_rejected(self):
        for value in ("0.0000001", Fraction(1, 3)):
            with self.subTest(value=value), self.assertRaisesRegex(ChapterError, "sub-microsecond"):
                seconds_us(value)

    def test_invalid_times(self):
        for value in (True, 0.1, "NaN", "Infinity", "-1", "x", "9223372036855"):
            with self.subTest(value=value), self.assertRaises(ChapterError):
                seconds_us(value)

    def test_json_numeric_time_only(self):
        for value in ('"1"', 'null', 'true', '{}'):
            with self.subTest(value=value), self.assertRaisesRegex(ChapterError, "JSON number"):
                loads('{"duration":3,"chapters":[{"start_time":' + value + '}]}', "json")


class JsonTests(unittest.TestCase):
    def test_json_ffmetadata_json_retains_times_titles(self):
        original = basic()
        exported = dumps(original, "ffmetadata")
        self.assertTrue(exported.report["lossless"])
        self.assertTrue(exported.report["comparison"]["equal"])
        self.assertIn("TIMEBASE=1/1000000", exported.text)
        self.assertIn(r"Intro\; A\=B \#1 \\ archive", exported.text)
        restored = loads(dumps(loads(exported.text, "ffmetadata"), "json").text, "json")
        self.assertEqual(restored, original)

    def test_infer_next_and_final_end(self):
        doc = loads('{"duration":9,"chapters":[{"start_time":0},{"start_time":4}]}', "json")
        self.assertEqual([c.end_us for c in doc.chapters], [4_000_000, 9_000_000])
        self.assertEqual(doc.chapters[0].title, "")

    def test_supplied_duration(self):
        doc = loads('{"chapters":[{"start_time":0}]}', "json", duration="1.000001")
        self.assertEqual(doc.chapters[0].end_us, 1_000_001)

    def test_duration_conflict(self):
        with self.assertRaisesRegex(ChapterError, "conflicts"):
            loads('{"duration":2,"chapters":[{"start_time":0}]}', "json", duration="3")

    def test_explicit_end_defines_extent(self):
        doc = loads('{"chapters":[{"start_time":2,"end_time":4}]}', "json")
        self.assertEqual(doc.duration_us, 4_000_000)

    def test_unknown_nested_metadata_stays_exact(self):
        raw = '{"extra":true,"chapters":[{"start_time":0,"end_time":1,"score":0.12345678901234567890123456789,"nested":[null,{"a":1}]}]}'
        doc = loads(raw, "json")
        exported = dumps(doc, "json")
        self.assertIn("0.12345678901234567890123456789", exported.text)
        self.assertEqual(loads(exported.text, "json"), doc)

    def test_duplicate_keys_rejected(self):
        for raw in ('{"chapters":[],"chapters":[]}',
                    '{"chapters":[{"start_time":0,"start_time":1}]}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ChapterError, "duplicate"):
                loads(raw, "json")

    def test_bad_structure(self):
        for raw in ('[]', '{}', '{"chapters":[]}', '{"chapters":[0]}',
                    '{"chapters":[{}]}', '{"duration":1,"chapters":[{"start_time":0,"title":1}]}'):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "json")

    def test_nonfinite_numbers_rejected_even_in_metadata(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ChapterError):
                loads('{"chapters":[],"x":' + value + '}', "json")

    def test_title_nul_and_surrogate_rejected(self):
        for escape in (r"\u0000", r"\ud800"):
            with self.subTest(escape=escape), self.assertRaises(ChapterError):
                loads('{"duration":1,"chapters":[{"start_time":0,"title":"' + escape + '"}]}', "json")

    def test_surrogate_unknown_metadata_rejected(self):
        with self.assertRaises(ChapterError):
            loads(r'{"duration":1,"chapters":[{"start_time":0,"x":"\ud800"}]}', "json")


class ValidationTests(unittest.TestCase):
    def test_missing_last_end(self):
        with self.assertRaisesRegex(ChapterError, "missing last end"):
            loads('{"chapters":[{"start_time":0}]}', "json")

    def test_never_sort_out_of_order_starts(self):
        for starts in ((1, 1), (2, 1)):
            with self.subTest(starts=starts), self.assertRaisesRegex(ChapterError, "strictly increasing"):
                loads('{"duration":5,"chapters":[{"start_time":%s},{"start_time":%s}]}' % starts, "json")

    def test_overlapping_ends(self):
        with self.assertRaisesRegex(ChapterError, "overlaps"):
            loads('{"duration":5,"chapters":[{"start_time":0,"end_time":3},{"start_time":2}]}', "json")

    def test_ends_and_duration(self):
        for start, end, duration in ((1, 1, 3), (2, 1, 3), (0, 4, 3), (-1, 1, 3)):
            with self.subTest(start=start, end=end), self.assertRaises(ChapterError):
                loads('{"duration":%s,"chapters":[{"start_time":%s,"end_time":%s}]}' % (duration, start, end), "json")

    def test_valid_gaps_are_preserved(self):
        doc = loads('{"duration":6,"chapters":[{"start_time":1,"end_time":2},{"start_time":4,"end_time":5}]}', "json")
        self.assertEqual(doc.chapters[0].end_us, 2_000_000)
        with self.assertRaises(LossError):
            dumps(doc, "ffmetadata")  # tail boundary must be audited too
        converted = dumps(doc, "ffmetadata", allow_lossy=True)
        self.assertEqual(converted.report["losses"][0]["path"], "/duration_us")
        self.assertEqual(restore(converted.report), doc)

    def test_direct_api_validation(self):
        for doc in (Document([], 0), Document([Chapter(0.0, 1)], 1),
                    Document([Chapter(0, 1, metadata=[])], 1), Document([Chapter(0, 1)], True)):
            with self.subTest(doc=doc), self.assertRaises(ChapterError):
                dumps(doc, "json")


class FFmetadataTests(unittest.TestCase):
    def test_fractional_timebase_fixture(self):
        raw = (EXAMPLES / "fractional.ffmetadata").read_text(encoding="utf-8")
        doc = loads(raw, "ffmetadata")
        self.assertEqual(doc.chapters[0].end_us, 1_001_000)
        self.assertEqual(doc.duration_us, 2_002_000)
        self.assertEqual(doc.chapters[0].metadata, {"language": "eng"})
        self.assertEqual(loads(dumps(doc, "json").text, "json"), doc)
        self.assertEqual(loads(dumps(doc, "ffmetadata").text, "ffmetadata"), doc)

    def test_nanosecond_default_timebase(self):
        doc = loads(ff("START=1000\nEND=2000\ntitle=Tiny\n"), "ffmetadata")
        self.assertEqual(doc.chapters[0].start_us, 1)
        self.assertEqual(doc.chapters[0].end_us, 2)

    def test_submicrosecond_timebase_is_rejected(self):
        with self.assertRaisesRegex(ChapterError, "sub-microsecond"):
            loads(ff("TIMEBASE=1/3\nSTART=0\nEND=1\n"), "ffmetadata")

    def test_all_escaped_delimiters_and_unicode_multiline(self):
        doc = Document([Chapter(0, 1_000_000, "Intro; A=B #\\\n章节", {"a=;#\\\n": "b=;#\\\n"})],
                       1_000_000, {"#leading": " global "})
        encoded = dumps(doc, "ffmetadata").text
        self.assertEqual(loads(encoded, "ffmetadata"), doc)
        self.assertIn("\\\n", encoded)

    def test_whitespace_is_part_of_tag(self):
        doc = loads(ff("TIMEBASE=1/1000\nSTART=0\nEND=1\nfoo = bar \ntitle= x \n"), "ffmetadata")
        self.assertEqual(doc.chapters[0].metadata, {"foo ": " bar "})
        self.assertEqual(doc.chapters[0].title, " x ")

    def test_crlf_and_comments(self):
        raw = ff("TIMEBASE=1/1000\nSTART=0\n;comment\n#comment\nEND=2\n")
        self.assertEqual(loads(raw.replace("\n", "\r\n"), "ffmetadata").duration_us, 2000)

    def test_ff_missing_ends_infer_only_from_next_or_duration(self):
        raw = ff("TIMEBASE=1/1000\nSTART=0\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\n")
        doc = loads(raw, "ffmetadata", duration="2")
        self.assertEqual([c.end_us for c in doc.chapters], [1_000_000, 2_000_000])
        with self.assertRaisesRegex(ChapterError, "missing last end"):
            loads(raw, "ffmetadata")

    def test_malformed_escapes(self):
        for suffix in ("title=bad\\q\n", "title=bad\\", "title=A=B\n", "title=A;B\n", "title=A#B\n"):
            with self.subTest(suffix=suffix), self.assertRaises(ChapterError):
                loads(ff("TIMEBASE=1/1000\nSTART=0\nEND=1\n" + suffix), "ffmetadata")

    def test_invalid_timebases(self):
        for base in ("0/1", "1/0", "-1/2", "1.0/2", "1", "1/2/3"):
            with self.subTest(base=base), self.assertRaises(ChapterError):
                loads(ff(f"TIMEBASE={base}\nSTART=0\nEND=10\n"), "ffmetadata")

    def test_bad_header_sections_duplicates_and_times(self):
        cases = ("[CHAPTER]\nSTART=0\nEND=1\n", ";FFMETADATA2\n",
                 ";FFMETADATA1\n[STREAM]\ntitle=x\n", ff("START=0\nSTART=1\nEND=2\n"),
                 ff("END=1000\n"), ff("START=-1\nEND=1000\n"), ff("START=0.0\nEND=1000\n"),
                 ff("START=0\nEND=1000\nx\n"))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "ffmetadata")

    def test_unknown_string_tags_preserved(self):
        doc = loads(ff("TIMEBASE=1/1000\nSTART=0\nEND=1\nlanguage=eng\ncustom=value\n"), "ffmetadata")
        self.assertEqual(loads(dumps(doc, "json").text, "json"), doc)


class TextAndAuditTests(unittest.TestCase):
    def test_text_fixture_and_space_separator(self):
        doc = loads((EXAMPLES / "chapters.txt").read_text(encoding="utf-8"), "text", duration="60")
        self.assertEqual(doc, basic())
        other = loads("00:00:00.1 a\n00:01:00.000001\tb\n", "text", duration="61")
        self.assertEqual(other.chapters[0].start_us, 100_000)
        self.assertEqual(other.chapters[1].start_us, 60_000_001)

    def test_text_requires_final_duration(self):
        with self.assertRaisesRegex(ChapterError, "missing last end"):
            loads("00:00:00\tIntro\n", "text")

    def test_text_bad_syntax(self):
        for raw in ("00:60:00\tx", "00:00:60\tx", "0:0:0 x", "00:00:00.0000001 x",
                    "00:00:00", "00:00:00\tx\ny", "# comment", "00:00:00\tx\r"):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "text", duration="1")

    def test_text_lossy_boundary_is_always_audited(self):
        doc = basic()
        with self.assertRaises(LossError):
            dumps(doc, "text")
        result = dumps(doc, "text", allow_lossy=True)
        self.assertFalse(result.report["lossless"])
        self.assertTrue(result.report["comparison"]["equal"])
        self.assertEqual(result.report["readback_duration_seconds"], 60)
        self.assertEqual(loads(result.text, "text", duration="60"), doc)

    def test_multiline_gaps_and_metadata_audited_and_recoverable(self):
        doc = Document([Chapter(0, 2_000_000, "A\nB", {"extra": [Decimal("0.123456789")]})],
                       3_000_000, {"video_id": "synthetic"})
        with self.assertRaises(LossError) as error:
            dumps(doc, "text")
        self.assertEqual(len(error.exception.losses), 5)
        result = dumps(doc, "text", allow_lossy=True)
        self.assertEqual(len(result.report["losses"]), 5)
        self.assertFalse(result.report["comparison"]["equal"])
        self.assertEqual(restore(read_json(json_text(result.report))), doc)

    def test_ff_nonstring_metadata_requires_loss_approval(self):
        doc = Document([Chapter(0, 1, "x", {"custom": {"n": 1}})], 1)
        with self.assertRaisesRegex(LossError, "custom"):
            dumps(doc, "ffmetadata")
        result = dumps(doc, "ffmetadata", allow_lossy=True)
        self.assertNotIn("custom=", result.text)
        self.assertEqual(restore(result.report), doc)

    def test_destination_reserved_field_collisions_audited(self):
        doc = loads(ff("TIMEBASE=1/1000\nSTART=0\nEND=1\nstart_time=untouched\n"), "ffmetadata")
        with self.assertRaises(LossError):
            dumps(doc, "json")
        result = dumps(doc, "json", allow_lossy=True)
        self.assertEqual(restore(result.report), doc)
        self.assertEqual(len(result.report["losses"]), 1)

    def test_ff_carriage_return_and_bad_tag_keys_audited(self):
        doc = Document([Chapter(0, 1, "a\rb", {"": "x", "foo": "x\r"})], 1)
        with self.assertRaises(LossError):
            dumps(doc, "ffmetadata")
        result = dumps(doc, "ffmetadata", allow_lossy=True)
        self.assertEqual(len(result.report["losses"]), 3)
        self.assertEqual(restore(result.report), doc)

    def test_compare_reports_specific_path_and_preserves_order(self):
        doc = basic()
        other = basic()
        other.chapters[1].title = "changed"
        result = compare(doc, other)
        self.assertFalse(result["equal"])
        self.assertEqual(result["differences"][0]["path"], "/chapters/1/title")

    def test_bool_not_same_as_integer_metadata(self):
        left, right = basic(), basic()
        left.metadata["x"], right.metadata["x"] = True, 1
        self.assertFalse(compare(left, right)["equal"])

    def test_json_pointer_escaping(self):
        doc = basic()
        doc.chapters[0].metadata["a/b~c"] = [1]
        result = dumps(doc, "ffmetadata", allow_lossy=True)
        self.assertEqual(result.report["losses"][0]["path"], "/chapters/0/metadata/a~1b~0c")

    def test_malformed_restore_report(self):
        for report in ({}, {"schema_version": 2}, {"schema_version": 1, "source": {}},
                       {"schema_version": 1, "source": None}):
            with self.subTest(report=report), self.assertRaises(ChapterError):
                restore(report)

    def test_unknown_formats_rejected(self):
        for action in (lambda: loads("", "auto"), lambda: dumps(basic(), "xml")):
            with self.assertRaisesRegex(ChapterError, "unsupported format"):
                action()
