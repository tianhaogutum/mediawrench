"""Synthetic integration cases against the pinned MKVToolNix 92.0 XML shape."""
import copy
from decimal import Decimal
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from mediawrench.chapters import (Chapter, ChapterError, Document, LossError, compare,
                               dumps, json_text, loads, read_json, restore)

FIXTURE = Path(__file__).resolve().parents[2] / "examples/chapters/multilingual.matroska.xml"
DEFAULT_FIXTURE = FIXTURE.with_name("default-edition.matroska.xml")
DEFAULT_JSON = FIXTURE.with_name("default-edition.chapters.json")
HIDDEN_FIXTURE = FIXTURE.with_name("hidden-edition.matroska.xml")
HIDDEN_JSON = FIXTURE.with_name("hidden-edition.chapters.json")
HIDDEN_CHAPTERS = FIXTURE.with_name("hidden-chapters.matroska.xml")
HIDDEN_CHAPTERS_JSON = FIXTURE.with_name("hidden-chapters.chapters.json")


def xml(atom, edition=""):
    return f"<Chapters><EditionEntry>{edition}{atom}</EditionEntry></Chapters>"


def atom(start="00:00:00", end="00:00:01", extra=""):
    return (f"<ChapterAtom><ChapterTimeStart>{start}</ChapterTimeStart>"
            + (f"<ChapterTimeEnd>{end}</ChapterTimeEnd>" if end is not None else "")
            + extra + "</ChapterAtom>")


class MatroskaTests(unittest.TestCase):
    def fixture(self):
        return loads(FIXTURE.read_text(encoding="utf-8"), "matroska")

    def test_hidden_chapters_xml_json_xml_preserves_independent_states(self):
        doc = loads(HIDDEN_CHAPTERS.read_text(encoding="utf-8"), "matroska")
        self.assertEqual(doc, loads(HIDDEN_CHAPTERS_JSON.read_text(encoding="utf-8"), "json"))
        bridge = dumps(doc, "json")
        self.assertTrue(bridge.report["lossless"])
        hidden = read_json(bridge.text)["chapters"][0]["matroska"]["hidden"]
        self.assertIs(type(hidden), int)
        self.assertEqual(hidden, 1)
        result = dumps(loads(bridge.text, "json"), "matroska")
        self.assertTrue(result.report["lossless"])
        self.assertTrue(result.report["comparison"]["equal"])
        nodes = ET.fromstring(result.text).findall("./EditionEntry/ChapterAtom")
        self.assertEqual([node.findtext("ChapterFlagHidden") for node in nodes], ["1", None])
        self.assertEqual(loads(result.text, "matroska"), doc)

    def test_chapter_hidden_combinations_normalize_independently(self):
        for edition_hidden, edition_default in ((0, 0), (0, 1), (1, 0), (1, 1)):
            edition = (f"<EditionFlagHidden>{edition_hidden}</EditionFlagHidden>"
                       f"<EditionFlagDefault>{edition_default}</EditionFlagDefault>")
            root = {key: 1 for key, value in (("edition_hidden", edition_hidden),
                                              ("edition_default", edition_default)) if value}
            for uid in (False, True):
                for states in ((0, 0), (0, 1), (1, 0), (1, 1)):
                    with self.subTest(root=root, uid=uid, states=states):
                        chapters, expected = [], []
                        for i, hidden in enumerate(states):
                            extra = f"<ChapterUID>{101 + i}</ChapterUID>" if uid else ""
                            extra += (f"<ChapterFlagHidden> \n{hidden}\t</ChapterFlagHidden>"
                                      "<ChapterFlagEnabled>1</ChapterFlagEnabled>")
                            chapters.append(atom(f"00:00:0{i}", f"00:00:0{i + 1}", extra))
                            info = {"uid": 101 + i} if uid else {}
                            if hidden:
                                info["hidden"] = 1
                            expected.append({"matroska": info} if info else {})
                        doc = loads(xml("".join(chapters), edition), "matroska")
                        self.assertEqual(doc.metadata, {"matroska": root} if root else {})
                        self.assertEqual([c.metadata for c in doc.chapters], expected)
                        bridge = dumps(doc, "json")
                        result = dumps(loads(bridge.text, "json"), "matroska")
                        nodes = ET.fromstring(result.text).findall("./EditionEntry/ChapterAtom")
                        self.assertEqual([n.findtext("ChapterFlagHidden") for n in nodes],
                                         ["1" if value else None for value in states])
                        self.assertEqual(loads(result.text, "matroska"), doc)
                        self.assertTrue(result.report["lossless"])
            # Zero/absence normalize away without removing other chapter metadata.
            for extra in ("", "<ChapterUID>101</ChapterUID>",
                          "<ChapterDisplay><ChapterString>x</ChapterString>"
                          "<ChapterLanguage>fra</ChapterLanguage></ChapterDisplay>"):
                expected_doc = loads(xml(atom(extra=extra), edition), "matroska")
                doc = loads(xml(atom(extra=extra + "<ChapterFlagHidden>0</ChapterFlagHidden>"),
                                edition), "matroska")
                self.assertEqual(doc, expected_doc)
                self.assertNotIn("ChapterFlagHidden", dumps(doc, "matroska").text)

    def test_chapter_hidden_loss_audit_serialized_recovery(self):
        documents = [loads(HIDDEN_CHAPTERS.read_text(encoding="utf-8"), "matroska")]
        # A chapter flag alone also requires loss approval, without UIDs/displays/root flags.
        documents.append(loads(xml(atom(extra="<ChapterFlagHidden>1</ChapterFlagHidden>")),
                               "matroska"))
        for doc in documents:
            for target in ("ffmetadata", "text"):
                with self.subTest(doc=doc, target=target):
                    with self.assertRaises(LossError) as caught:
                        dumps(doc, target)
                    report = read_json(json_text(dumps(doc, target, allow_lossy=True).report))
                    expected = {"/chapters/0/metadata/matroska": doc.chapters[0].metadata["matroska"]}
                    if doc.metadata:
                        expected["/metadata/matroska"] = doc.metadata["matroska"]
                    if target == "text":
                        expected = {path.removesuffix("/matroska"): {"matroska": value}
                                    for path, value in expected.items()}
                        expected["/duration_us"] = doc.duration_us
                    self.assertEqual({loss["path"]: loss["value"] for loss in report["losses"]}, expected)
                    self.assertEqual(report["losses"], caught.exception.losses)
                    self.assertEqual(report["source"], doc.normalized())
                    self.assertFalse(report["lossless"])
                    self.assertFalse(report["comparison"]["equal"])
                    self.assertEqual(report["comparison"]["right"]["chapters"][0]["metadata"], {})
                    self.assertEqual(restore(report), doc)
                    self.assertEqual(loads(dumps(restore(report), "matroska").text, "matroska"), doc)

    def test_chapter_hidden_invalid_flags_and_layouts(self):
        flag = "<ChapterFlagHidden>1</ChapterFlagHidden>"
        for value in ("", "2", "-1", "true", "false", "01", "+1", "1.0"):
            with self.subTest(value=value), self.assertRaisesRegex(ChapterError, "ChapterFlagHidden"):
                loads(xml(atom(extra=f"<ChapterFlagHidden>{value}</ChapterFlagHidden>")), "matroska")
        for extra in (flag, "<ChapterFlagHidden>0</ChapterFlagHidden>",
                      "<ChapterFlagEnabled>0</ChapterFlagEnabled>",
                      "<ChapterSegmentUID>abc</ChapterSegmentUID>", atom()):
            with self.subTest(extra=extra), self.assertRaises(ChapterError):
                loads(xml(atom(extra=flag + extra)), "matroska")
        for invalid in ('<ChapterFlagHidden x="1">1</ChapterFlagHidden>',
                        "<ChapterFlagHidden><Value>1</Value></ChapterFlagHidden>"):
            with self.subTest(invalid=invalid), self.assertRaises(ChapterError):
                loads(xml(atom(extra=invalid)), "matroska")

    def test_chapter_hidden_reserved_metadata_requires_integer_one(self):
        for value in (0, -1, 2, True, False, "1", None, Decimal("1.0"), [], {}):
            doc = Document([Chapter(0, 1, "x", {"matroska": {"hidden": value}})], 1)
            doc = loads(dumps(doc, "json").text, "json")
            for allow_lossy in (False, True):
                with self.subTest(value=value, allow_lossy=allow_lossy), self.assertRaisesRegex(
                        ChapterError, "hidden"):
                    dumps(doc, "matroska", allow_lossy=allow_lossy)

    def test_hidden_edition_xml_json_xml_preserves_flags(self):
        raw = HIDDEN_FIXTURE.read_text(encoding="utf-8")
        expected = loads(HIDDEN_JSON.read_text(encoding="utf-8"), "json")
        self.assertEqual(loads(raw, "matroska"), expected)
        for uid in (False, True):
            for hidden, default in ((1, 0), (0, 1), (1, 1)):
                flags = "<EditionUID>42</EditionUID>" if uid else ""
                flags += f"<EditionFlagHidden>{hidden}</EditionFlagHidden>"
                flags += f"<EditionFlagDefault>{default}</EditionFlagDefault>"
                metadata = {"edition_uid": 42} if uid else {}
                if hidden:
                    metadata["edition_hidden"] = 1
                if default:
                    metadata["edition_default"] = 1
                with self.subTest(uid=uid, hidden=hidden, default=default):
                    doc = loads(xml(atom(), flags), "matroska")
                    self.assertEqual(doc.metadata, {"matroska": metadata})
                    bridge = dumps(doc, "json")
                    self.assertTrue(bridge.report["lossless"])
                    self.assertEqual(read_json(bridge.text)["matroska"], metadata)
                    result = dumps(loads(bridge.text, "json"), "matroska")
                    self.assertTrue(result.report["lossless"])
                    self.assertTrue(result.report["comparison"]["equal"])
                    for name, enabled in (("Hidden", hidden), ("Default", default)):
                        self.assertEqual(f"<EditionFlag{name}>1</EditionFlag{name}>" in result.text,
                                         bool(enabled))
                    self.assertEqual(loads(result.text, "matroska"), doc)

    def test_default_edition_xml_json_xml_preserves_flag(self):
        doc = loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"), "matroska")
        expected = loads(DEFAULT_JSON.read_text(encoding="utf-8"), "json")
        self.assertEqual(doc, expected)
        bridged = dumps(doc, "json")
        self.assertTrue(bridged.report["lossless"])
        self.assertEqual(read_json(bridged.text)["matroska"], {"edition_default": 1})
        for source in (doc, loads(bridged.text, "json")):
            result = dumps(source, "matroska")
            self.assertTrue(result.report["lossless"])
            self.assertTrue(result.report["comparison"]["equal"])
            self.assertIn("<EditionFlagDefault>1</EditionFlagDefault>", result.text)
            self.assertEqual(loads(result.text, "matroska"), expected)
        # The flag is independent of optional UIDs and multilingual displays.
        raw = FIXTURE.read_text(encoding="utf-8").replace(
            "<EditionFlagDefault>0", "<EditionFlagDefault>1")
        combined = loads(raw, "matroska")
        self.assertEqual(combined.metadata,
                         {"matroska": {"edition_uid": 42, "edition_default": 1}})
        self.assertEqual(loads(dumps(loads(dumps(combined, "json").text, "json"),
                                    "matroska").text, "matroska"), combined)

    def test_default_edition_zero_and_absence_normalize_without_metadata(self):
        absent = loads(xml(atom()), "matroska")
        for flag in ("", "<EditionFlagDefault>0</EditionFlagDefault>"):
            with self.subTest(flag=flag):
                doc = loads(xml(atom(), flag), "matroska")
                self.assertEqual(doc, absent)
                self.assertEqual(doc.metadata, {})
                self.assertNotIn("EditionFlagDefault", dumps(doc, "matroska").text)
        self.assertEqual(loads(xml(atom(), "<EditionFlagDefault> \n1\t</EditionFlagDefault>"),
                               "matroska").metadata, {"matroska": {"edition_default": 1}})

    def test_edition_flags_loss_audit_preserves_exact_source(self):
        for fixture in (DEFAULT_FIXTURE, HIDDEN_FIXTURE):
            doc = loads(fixture.read_text(encoding="utf-8"), "matroska")
            for target, path in (("ffmetadata", "/metadata/matroska"), ("text", "/metadata")):
                with self.subTest(fixture=fixture.name, target=target):
                    with self.assertRaises(LossError) as caught:
                        dumps(doc, target)
                    result = dumps(doc, target, allow_lossy=True)
                    report = read_json(json_text(result.report))
                    self.assertEqual(caught.exception.losses, report["losses"])
                    self.assertEqual({item["path"] for item in report["losses"]},
                                     {path} if target == "ffmetadata" else {path, "/duration_us"})
                    loss = next(item for item in report["losses"] if item["path"] == path)
                    self.assertEqual(loss["value"], doc.metadata["matroska"] if target == "ffmetadata"
                                     else doc.metadata)
                    self.assertFalse(report["lossless"])
                    self.assertFalse(report["comparison"]["equal"])
                    self.assertEqual(report["source"]["metadata"], doc.metadata)
                    self.assertEqual(report["comparison"]["right"]["metadata"], {})
                    restored = restore(report)
                    self.assertEqual(restored, doc)
                    self.assertEqual(loads(dumps(restored, "matroska").text, "matroska"), doc)

    def test_hidden_edition_zero_and_absence_normalize_independently(self):
        for default in ("", "<EditionFlagDefault>0</EditionFlagDefault>",
                        "<EditionFlagDefault>1</EditionFlagDefault>"):
            expected = loads(xml(atom(), default), "matroska")
            for hidden in ("", "<EditionFlagHidden>0</EditionFlagHidden>",
                           "<EditionFlagHidden> \n0\t</EditionFlagHidden>"):
                with self.subTest(default=default, hidden=hidden):
                    doc = loads(xml(atom(), hidden + default), "matroska")
                    self.assertEqual(doc, expected)
                    self.assertNotIn("EditionFlagHidden", dumps(doc, "matroska").text)
        self.assertEqual(loads(xml(atom(), "<EditionFlagHidden> \n1\t</EditionFlagHidden>"),
                               "matroska").metadata, {"matroska": {"edition_hidden": 1}})
        # A hidden-only edition is also loss-audited without depending on UID/default.
        doc = loads(xml(atom(), "<EditionFlagHidden>1</EditionFlagHidden>"), "matroska")
        for target in ("ffmetadata", "text"):
            with self.subTest(target=target):
                with self.assertRaises(LossError):
                    dumps(doc, target)
                report = read_json(json_text(dumps(doc, target, allow_lossy=True).report))
                self.assertEqual(report["source"]["metadata"], {"matroska": {"edition_hidden": 1}})
                self.assertEqual(restore(report), doc)

    def test_hidden_edition_invalid_xml_values_and_layouts_rejected(self):
        for value in ("", "2", "-1", "true", "false", "01", "+1", "1.0"):
            with self.subTest(value=value), self.assertRaisesRegex(ChapterError, "EditionFlagHidden"):
                loads(xml(atom(), f"<EditionFlagHidden>{value}</EditionFlagHidden>"), "matroska")
        flags = "<EditionFlagHidden>1</EditionFlagHidden><EditionFlagDefault>1</EditionFlagDefault>"
        for raw in (xml(atom(), flags + "<EditionFlagHidden>1</EditionFlagHidden>"),
                    xml(atom(), flags + "<EditionFlagHidden>0</EditionFlagHidden>"),
                    xml(atom(), '<EditionFlagHidden value="1">1</EditionFlagHidden>'),
                    xml(atom(), "<EditionFlagHidden><Value>1</Value></EditionFlagHidden>"),
                    xml(atom(), flags + "<EditionFlagOrdered>1</EditionFlagOrdered>"),
                    xml(atom(), flags + "<EditionEntry/>"),
                    xml(atom(extra=atom()), flags),
                    xml(atom(extra="<ChapterFlagHidden>2</ChapterFlagHidden>"), flags),
                    xml(atom(extra="<ChapterFlagEnabled>0</ChapterFlagEnabled>"), flags),
                    xml(atom(extra="<ChapterSegmentUID>abc</ChapterSegmentUID>"), flags),
                    "<Chapters><EditionEntry>" + flags + atom() +
                    "</EditionEntry><EditionEntry>" + atom() + "</EditionEntry></Chapters>"):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "matroska")

    def test_default_edition_invalid_xml_values_and_layouts_rejected(self):
        for flag in ("", "2", "-1", "true", "false", "01", "+1", "1.0"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ChapterError, "EditionFlagDefault"):
                loads(xml(atom(), f"<EditionFlagDefault>{flag}</EditionFlagDefault>"), "matroska")
        flag = "<EditionFlagDefault>1</EditionFlagDefault>"
        for raw in (xml(atom(), flag * 2),
                    xml(atom(), flag + "<EditionFlagDefault>0</EditionFlagDefault>"),
                    xml(atom(), '<EditionFlagDefault value="1">1</EditionFlagDefault>'),
                    xml(atom(), "<EditionFlagDefault><Value>1</Value></EditionFlagDefault>"),
                    xml(atom(), flag + "<EditionFlagOrdered>1</EditionFlagOrdered>"),
                    xml(atom(extra=atom()), flag),
                    "<Chapters><EditionEntry>" + flag + atom() +
                    "</EditionEntry><EditionEntry>" + atom() + "</EditionEntry></Chapters>"):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "matroska")

    def test_edition_flags_reserved_metadata_requires_integer_one(self):
        # JSON remains a general metadata carrier; XML enforces its reserved contract.
        for key in ("edition_default", "edition_hidden"):
            for value in (0, -1, 2, True, False, "1", None, Decimal("1.0"), [], {}):
                doc = Document([Chapter(0, 1, "x")], 1, {"matroska": {key: value}})
                doc = loads(dumps(doc, "json").text, "json")
                for allow_lossy in (False, True):
                    with self.subTest(key=key, value=value, allow_lossy=allow_lossy), self.assertRaisesRegex(
                            ChapterError, key):
                        dumps(doc, "matroska", allow_lossy=allow_lossy)

    def test_fixture_roundtrip_and_json_bridge(self):
        doc = self.fixture()
        self.assertEqual(doc.metadata, {"matroska": {"edition_uid": 42}})
        self.assertEqual([(c.start_us, c.end_us) for c in doc.chapters],
                         [(1, 1_000_001), (1_500_000, 2_000_001)])
        self.assertEqual(doc.chapters[0].title, 'Intro & <archive> "A"')
        details = doc.chapters[0].metadata["matroska"]
        self.assertEqual(details["uid"], 101)
        self.assertEqual(details["displays"][1], {
            "string": "开场 & <档案> '甲'", "language": "zho", "language_ietf": "zh-Hans"})
        result = dumps(doc, "matroska")
        self.assertTrue(result.report["lossless"])
        self.assertTrue(result.report["comparison"]["equal"])
        self.assertIn("&amp; &lt;archive&gt;", result.text)
        self.assertEqual(loads(result.text, "matroska"), doc)
        bridged = loads(dumps(doc, "json").text, "json")
        self.assertTrue(compare(doc, loads(dumps(bridged, "matroska").text, "matroska"))["equal"])

    def test_other_formats_require_loss_approval_and_retain_source(self):
        doc = self.fixture()
        for target in ("ffmetadata", "text"):
            with self.subTest(target=target):
                with self.assertRaises(LossError):
                    dumps(doc, target)
                result = dumps(doc, target, allow_lossy=True)
                report = read_json(json_text(result.report))
                self.assertFalse(report["lossless"])
                self.assertFalse(report["comparison"]["equal"])
                self.assertEqual(restore(report), doc)
                self.assertEqual(report["source"]["chapters"][0]["metadata"], doc.chapters[0].metadata)
                self.assertTrue(any("matroska" in item["path"] or item["path"] == "/metadata"
                                    for item in report["losses"]))

    def test_plain_model_default_display_and_uid_absence(self):
        doc = Document([Chapter(0, 1_000_000, "A & <B>\r\n\tC")], 1_000_000)
        result = dumps(doc, "matroska")
        self.assertEqual(loads(result.text, "matroska"), doc)
        self.assertNotIn("ChapterUID", result.text)
        self.assertIn("&#13;", result.text)
        self.assertEqual(loads(xml(atom()), "matroska").chapters[0].title, "")

    def test_missing_ends_infer_and_tail_duration_is_audited(self):
        raw = xml(atom(end=None) + atom("00:00:01", None))
        with self.assertRaisesRegex(ChapterError, "missing last end"):
            loads(raw, "matroska")
        doc = loads(raw, "matroska", duration="2")
        self.assertEqual([c.end_us for c in doc.chapters], [1_000_000, 2_000_000])
        doc.duration_us = 3_000_000
        with self.assertRaises(LossError):
            dumps(doc, "matroska")
        result = dumps(doc, "matroska", allow_lossy=True)
        self.assertEqual(result.report["losses"], [{"path": "/duration_us",
            "reason": "Matroska chapter XML has no media duration boundary", "value": 3_000_000}])
        self.assertEqual(restore(result.report), doc)

    def test_invalid_times_order_and_overlaps_rejected(self):
        for stamp in ("-00:00:01", "00:60:00", "00:00:60", "0:00:00", "1",
                      "00:00:00.000000001", "00:00:00.1234567890", "999999:00:00",
                      "9" * 5000 + ":00:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ChapterError):
                loads(xml(atom(start=stamp)), "matroska")
        for raw in (atom(end="00:00:00"), atom(start="00:00:02"),
                    atom() + atom(), atom(end="00:00:02") + atom("00:00:01", "00:00:03")):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(xml(raw), "matroska")

    def test_duplicate_and_invalid_uids_rejected(self):
        for uid in ("0", "-1", "1.0", "18446744073709551616", "true", "9" * 5000):
            for tag in ("ChapterUID", "EditionUID"):
                extra = f"<{tag}>{uid}</{tag}>"
                with self.subTest(uid=uid, tag=tag), self.assertRaises(ChapterError):
                    loads(xml(atom(extra=extra)) if tag == "ChapterUID" else xml(atom(), extra), "matroska")
        for raw in (xml(atom(extra="<ChapterUID>1</ChapterUID><ChapterUID>2</ChapterUID>")),
                    xml(atom(extra="<ChapterUID>1</ChapterUID>") +
                        atom("00:00:01", "00:00:02", "<ChapterUID>01</ChapterUID>")),
                    xml(atom(), "<EditionUID>1</EditionUID><EditionUID>1</EditionUID>")):
            with self.subTest(raw=raw), self.assertRaisesRegex(ChapterError, "duplicate"):
                loads(raw, "matroska")
        doc = self.fixture()
        doc.chapters[1].metadata["matroska"]["uid"] = 101
        with self.assertRaisesRegex(ChapterError, "duplicate"):
            dumps(doc, "matroska", allow_lossy=True)

    def test_complex_layouts_and_unrecognized_xml_always_rejected(self):
        for raw in ("<Chapters/>", "<Chapters><EditionEntry/><EditionEntry/></Chapters>",
                    xml(atom(extra=atom())), xml(atom(), "<EditionEntry/>"),
                    xml(atom(), "<EditionFlagOrdered>1</EditionFlagOrdered>"),
                    xml(atom(extra="<ChapterSegmentUID>abc</ChapterSegmentUID>")),
                    xml(atom(extra="<ChapterFlagHidden>2</ChapterFlagHidden>")),
                    xml(atom(extra="<ChapterFlagEnabled>0</ChapterFlagEnabled>")),
                    xml(atom(extra="<ChapterTrack/>")), xml(atom(extra="<ChapterProcess/>")),
                    xml(atom(), "<EditionDisplay/>"), xml(atom(extra="<Unknown/>")),
                    xml(atom()).replace("<ChapterAtom>", '<ChapterAtom x="1">'),
                    xml(atom()).replace("<Chapters>", '<Chapters xmlns="urn:other">'),
                    xml(atom()).replace("<EditionEntry>", "<EditionEntry>ignored text"),
                    xml(atom(extra="<ChapterDisplay><ChapterString>x<b>y</b></ChapterString></ChapterDisplay>")),
                    xml(atom(extra="<?ignored value?>")), "<Chapters>"):
            with self.subTest(raw=raw), self.assertRaises(ChapterError):
                loads(raw, "matroska")

    def test_dtd_and_entity_declarations_rejected(self):
        for prefix in ('<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">',
                       '<!DOCTYPE Chapters [<!ENTITY x "expanded">]>'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ChapterError, "DTD"):
                loads(prefix + xml(atom()), "matroska")

    def test_display_subset_and_duplicate_languages_rejected(self):
        for display in ("<ChapterDisplay/>",
                        "<ChapterDisplay><ChapterString>x</ChapterString><ChapterCountry>us</ChapterCountry></ChapterDisplay>",
                        "<ChapterDisplay><ChapterString>x</ChapterString><ChapterLanguage>en</ChapterLanguage></ChapterDisplay>",
                        "<ChapterDisplay><ChapterString>x</ChapterString><ChapterLanguage>eng</ChapterLanguage><ChapterLanguage>fra</ChapterLanguage></ChapterDisplay>",
                        "<ChapterDisplay><ChapterString>x</ChapterString></ChapterDisplay>" * 2):
            with self.subTest(display=display), self.assertRaises(ChapterError):
                loads(xml(atom(extra=display)), "matroska")

    def test_invalid_reserved_model_data_is_not_silently_dropped(self):
        doc = self.fixture()
        for change in (lambda d: d.metadata["matroska"].update(unknown=1),
                       lambda d: d.chapters[0].metadata["matroska"].update(uid=True),
                       lambda d: d.chapters[0].metadata["matroska"].update(displays=[]),
                       lambda d: setattr(d.chapters[0], "title", "changed")):
            changed = copy.deepcopy(doc)
            change(changed)
            with self.subTest(doc=changed), self.assertRaises(ChapterError):
                dumps(changed, "matroska", allow_lossy=True)

    def test_unrepresentable_metadata_requires_audit(self):
        doc = Document([Chapter(0, 1, "x", {"custom": [1, 2]})], 1, {"origin": "local"})
        with self.assertRaises(LossError):
            dumps(doc, "matroska")
        result = dumps(doc, "matroska", allow_lossy=True)
        self.assertEqual({item["path"] for item in result.report["losses"]},
                         {"/metadata/origin", "/chapters/0/metadata/custom"})
        self.assertEqual(restore(read_json(json_text(result.report))), doc)

    def test_max_uid_and_time_boundaries(self):
        doc = loads(xml(atom(end="5124095:34:33.709551000", extra=
                        "<ChapterUID>18446744073709551615</ChapterUID>")), "matroska")
        self.assertEqual(loads(dumps(doc, "matroska").text, "matroska"), doc)
        with self.assertRaisesRegex(ChapterError, "64-bit"):
            loads(xml(atom(end="5124095:34:33.709552000")), "matroska")
        doc.chapters[0].end_us += 1
        doc.duration_us += 1
        with self.assertRaisesRegex(ChapterError, "64-bit"):
            dumps(doc, "matroska", allow_lossy=True)

    def test_xml_forbidden_characters_and_display_types_rejected(self):
        for title in ("bad\x01title", "bad\ufffetitle"):
            with self.subTest(title=title), self.assertRaisesRegex(ChapterError, "XML 1.0"):
                dumps(Document([Chapter(0, 1, title)], 1), "matroska", allow_lossy=True)
        for value in (None, True, 1, "en_XX", "x"):
            doc = self.fixture()
            doc.chapters[0].metadata["matroska"]["displays"][0]["language_ietf"] = value
            with self.subTest(value=value), self.assertRaises(ChapterError):
                dumps(doc, "matroska", allow_lossy=True)

    def test_whitespace_and_literal_entity_text_retained(self):
        for title in ("   ", "\t\n\r", "&lt; &#13; &amp;", "A ]]> B"):
            doc = Document([Chapter(0, 1, title)], 1)
            with self.subTest(title=title):
                self.assertEqual(loads(dumps(doc, "matroska").text, "matroska"), doc)
