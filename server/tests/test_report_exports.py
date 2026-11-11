"""Cross-format content, privacy and actual PDF/XLSX integrity tests.

Run this file with --review-dir ABSOLUTE_PATH to produce local synthetic review
artifacts and PNG previews. The builder never opens or visually reviews them.
"""

from copy import deepcopy
import csv
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sys
import time
import unittest
from defusedxml.ElementTree import fromstring
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import load_workbook
import pymupdf

from app.report_store import content_hash
from app.services.report_exports import EXPORT_FORMATS, build_report_snapshot, render_export, safe_spreadsheet_text


PRIVATE_MARKER = "PRIVATE_ATTACHMENT_AND_TEACHER_NOTE"


def report_input(source_revision=7, *, rows=3):
    return {
        "source_revision": source_revision,
        "students": [{"id": "student", "name": "测试学生"}, {"id": "no-evidence", "name": "无作答学生"}],
        "goals": [{"id": "goal", "title": "方程学习目标", "version": 2, "class_id": "class", "course_id": "course",
                   "internal_note": PRIVATE_MARKER}],
        "questions": [{"id": f"q{number}", "version": 1, "family_id": f"family-{number}", "goal_ids": ["goal"],
                       "question_bank_id": "bank", "answer": PRIVATE_MARKER, "stem": PRIVATE_MARKER}
                      for number in range(1, rows + 1)],
        "evidence": [{"id": f"e{number}", "revision": 1, "student_id": "student", "goal_id": "goal",
                      "question_id": f"q{number}", "question_version": 1, "family_id": f"family-{number}",
                      "score_units": 10 if number % 3 else 5, "max_score_units": 10,
                      "assisted": False, "exposed": False, "retry": False, "status": "graded", "occurred_at": number,
                      "submitted_answer": PRIVATE_MARKER, "teacher_note": PRIVATE_MARKER}
                     for number in range(1, rows + 1)],
        "teacher_judgments": [],
        "sources": [{"id": "source-safe", "title": "测试教材", "locator": "第12页，式 x^2 + y^2 = z^2", "export_allowed": True,
                     "raw_text": PRIVATE_MARKER},
                    {"id": "source-denied", "title": PRIVATE_MARKER, "locator": PRIVATE_MARKER, "export_allowed": "denied"}],
        "attachments": [{"text": PRIVATE_MARKER}], "teacher_internal_notes": PRIVATE_MARKER,
    }


def scopes():
    return [
        {"kind": "class", "class_id": "class", "course_id": "course"},
        {"kind": "student", "student_id": "student", "class_id": "class", "course_id": "course"},
        {"kind": "goal", "goal_id": "goal", "class_id": "class", "course_id": "course"},
        {"kind": "question_bank", "question_bank_id": "bank", "class_id": "class", "course_id": "course"},
    ]


class DataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_data = False
        self.data = ""

    def handle_starttag(self, tag, attrs):
        self.in_data = tag == "script" and dict(attrs).get("id") == "report-data"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data = False

    def handle_data(self, data):
        if self.in_data:
            self.data += data


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value)
    return str(value)


class ReportExportsTests(unittest.TestCase):
    def test_four_scopes_and_five_formats_share_frozen_hash_and_tables(self):
        for scope in scopes():
            snapshot = build_report_snapshot(report_input(), scope)
            expected_hash = content_hash(snapshot)
            for format in EXPORT_FORMATS:
                with self.subTest(kind=scope["kind"], format=format):
                    artifact = render_export(snapshot, format)
                    self.assertEqual(artifact.manifest["snapshot_sha256"], expected_hash)
                    self.assertEqual(artifact.manifest["source_revision"], 7)
                    self.assertEqual(artifact.manifest["scope"], scope)
                    self.assertEqual(artifact.manifest["tables"], {name: {"row_count": len(table["rows"]), "columns": table["columns"]} for name, table in snapshot["tables"].items()})
                    self.assertNotIn(PRIVATE_MARKER.encode(), artifact.content)
                    if format == "json":
                        self.assertEqual(json.loads(artifact.content)["snapshot"], snapshot)
                    elif format == "html":
                        parser = DataParser()
                        parser.feed(artifact.content.decode())
                        self.assertEqual(json.loads(parser.data)["snapshot"], snapshot)
                    elif format == "csv":
                        self.check_csv(snapshot, artifact)
                    elif format == "xlsx":
                        self.check_xlsx(snapshot, artifact)
                    elif format == "pdf":
                        self.check_pdf(snapshot, artifact)

    def check_csv(self, snapshot, artifact):
        self.assertEqual(artifact.media_type, "application/zip")
        with ZipFile(io.BytesIO(artifact.content)) as archive:
            self.assertEqual(set(archive.namelist()), {name + ".csv" for name in snapshot["tables"]} | {"manifest.json"})
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest, artifact.manifest)
            for name, table in snapshot["tables"].items():
                payload = archive.read(name + ".csv")
                self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
                values = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
                keys = [column["key"] for column in table["columns"]]
                self.assertEqual(values[0], keys)
                self.assertEqual(values[1:], [[safe_spreadsheet_text(_csv_value(row[key])) for key in keys] for row in table["rows"]])
                self.assertEqual(manifest["files"][name + ".csv"]["row_count"], len(table["rows"]))
                self.assertNotIn(PRIVATE_MARKER, payload.decode("utf-8-sig"))

    def check_xlsx(self, snapshot, artifact):
        self.assertEqual(artifact.content[:2], b"PK")
        workbook = load_workbook(io.BytesIO(artifact.content), data_only=False, keep_links=True)
        self.addCleanup(workbook.close)
        self.assertEqual(len(workbook.worksheets), len(snapshot["tables"]) + 1)
        self.assertFalse(workbook._external_links)
        titles = {"summary": "报告概览", "students": "学生", "goals": "学习目标", "questions": "题目版本", "question_usage": "题目聚合使用", "mastery": "掌握度证据", "evidence": "作答证据", "sources": "来源", "rules": "规则与说明"}
        for name, table in snapshot["tables"].items():
            worksheet = workbook[titles[name]]
            self.assertEqual(str(worksheet.freeze_panes), "C5")
            self.assertEqual(worksheet.print_title_rows, "$4:$4")
            for number, row in enumerate(table["rows"], 5):
                for index, column in enumerate(table["columns"], 2):
                    expected = row[column["key"]]
                    if isinstance(expected, (dict, list)):
                        expected = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if isinstance(expected, str):
                        expected = safe_spreadsheet_text(expected) or None
                    self.assertEqual(worksheet.cell(number, index).value, expected)
        for worksheet in workbook:
            for row in worksheet.iter_rows():
                for cell in row:
                    self.assertNotIn(cell.data_type, {"f", "e"})
                    self.assertIsNone(cell.hyperlink)
                    self.assertNotIn(PRIVATE_MARKER, str(cell.value))

    def check_pdf(self, snapshot, artifact):
        self.assertTrue(artifact.content.startswith(b"%PDF-"))
        with pymupdf.open(stream=artifact.content, filetype="pdf") as document:
            self.assertGreater(document.page_count, 0)
            self.assertEqual(document.metadata["title"], snapshot["title"])
            text = "\n".join(page.get_text() for page in document)
            self.assertIn(snapshot["title"], text)
            self.assertIn("测试教材", text)
            self.assertIn("x^2 + y^2 = z^2", text)
            self.assertIn("默认规则参数未经校准", text)
            self.assertNotIn("\ufffd", text)
            self.assertNotIn(PRIVATE_MARKER, text)
            self.assertIn("report.json", document.embfile_names())
            self.assertEqual(json.loads(document.embfile_get("report.json"))["snapshot"], snapshot)
            self.assertEqual(json.loads(document.embfile_get("manifest.json"))["snapshot_sha256"], content_hash(snapshot))
            for page in document:
                self.assertTrue(page.get_text().strip())
                self.assertTrue(any(font[1] not in {"n/a", ""} for font in page.get_fonts()))

    def test_question_bank_is_course_material_metadata_and_aggregate_usage_only(self):
        source = report_input()
        source["question_usage"] = [{"question_id": "q1", "version": 1, "usage_count": 20,
                                     "independent_count": 10, "assisted_count": 3, "retry_count": 5, "exposed_count": 2,
                                     "student_ids": ["PRIVATE_STUDENT"], "teacher_note": PRIVATE_MARKER}]
        snapshot = build_report_snapshot(source, {"kind": "question_bank", "question_bank_id": "bank", "course_id": "course"})
        self.assertEqual(set(snapshot["tables"]), {"summary", "questions", "question_usage", "sources", "rules"})
        self.assertEqual(snapshot["tables"]["question_usage"]["rows"][0]["usage_count"], 20)
        text = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("测试学生", text)
        self.assertNotIn("no-evidence", text)
        self.assertNotIn("PRIVATE_STUDENT", text)
        self.assertNotIn(PRIVATE_MARKER, text)
        self.assertTrue(all(not row["goal_ids"] for row in snapshot["tables"]["questions"]["rows"]))

    def test_input_is_unchanged_and_snapshot_ignores_raw_attachments_and_notes(self):
        source = report_input()
        before = deepcopy(source)
        snapshot = build_report_snapshot(source, scopes()[0])
        self.assertEqual(source, before)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual([row["source_id"] for row in snapshot["tables"]["sources"]["rows"]], ["source-safe"])
        no_evidence = next(row for row in snapshot["tables"]["mastery"]["rows"] if row["student_id"] == "no-evidence")
        self.assertIsNone(no_evidence["percent"])

    def test_same_frozen_snapshot_exports_are_repeatable(self):
        snapshot = build_report_snapshot(report_input(), scopes()[1])
        first_exports = {format: render_export(snapshot, format) for format in EXPORT_FORMATS}
        first_second = int(time.time())
        time.sleep(1.1)
        self.assertGreater(int(time.time()), first_second)
        for format in EXPORT_FORMATS:
            with self.subTest(format=format):
                first = first_exports[format]
                second = render_export({"snapshot": deepcopy(snapshot)}, format)
                self.assertEqual(first.content, second.content)
                self.assertEqual(first.filename, second.filename)
                if format == "xlsx":
                    for artifact in (first, second):
                        self.check_xlsx(snapshot, artifact)
                        with ZipFile(io.BytesIO(artifact.content)) as archive:
                            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                            self.assertEqual(archive.comment, b"")
                            for entry in archive.infolist():
                                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                                self.assertEqual(entry.compress_type, ZIP_DEFLATED)
                                self.assertEqual(entry.external_attr, 0o600 << 16)
                                self.assertEqual(entry.extra, b"")
                                self.assertEqual(entry.comment, b"")
                            core_xml = archive.read("docProps/core.xml")
                            self.assertLessEqual(len(core_xml), 1024 * 1024)
                            self.assertNotIn(b"<!DOCTYPE", core_xml.upper())
                            self.assertNotIn(b"<!ENTITY", core_xml.upper())
                            core = fromstring(core_xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
                            for property_name in ("created", "modified"):
                                values = core.findall(f"{{http://purl.org/dc/terms/}}{property_name}")
                                self.assertEqual(len(values), 1)
                                self.assertEqual(values[0].text, "2000-01-01T00:00:00Z")
                                self.assertEqual(values[0].get("{http://www.w3.org/2001/XMLSchema-instance}type"),
                                                 "dcterms:W3CDTF")
                            self.assertFalse(any(name.startswith("xl/externalLinks/") for name in archive.namelist()))

    def test_spreadsheet_formula_injection_is_literal_in_excel_and_csv(self):
        for malicious in ("=HYPERLINK(\"https://example.invalid\")", "+cmd|' /C calc'!A0", "-1+2", "@SUM(A1:A2)", " \t=2+2", "\ttext", "\n=1"):
            source = report_input()
            source["students"][0]["name"] = malicious
            snapshot = build_report_snapshot(source, scopes()[1])
            xlsx = render_export(snapshot, "xlsx")
            workbook = load_workbook(io.BytesIO(xlsx.content))
            try:
                cell = workbook["学生"]["C5"]
                self.assertEqual(cell.value, "'" + malicious)
                self.assertEqual(cell.data_type, "s")
            finally:
                workbook.close()
            zipped = render_export(snapshot, "csv")
            with ZipFile(io.BytesIO(zipped.content)) as archive:
                rows = list(csv.DictReader(io.StringIO(archive.read("students.csv").decode("utf-8-sig"))))
                self.assertEqual(rows[0]["name"], "'" + malicious)

    def test_html_escapes_all_labels_and_embedded_closing_script(self):
        source = report_input()
        source["students"][0]["name"] = '</script><img src="https://example.invalid" onerror="alert(1)">'
        snapshot = build_report_snapshot(source, scopes()[1])
        artifact = render_export(snapshot, "html")
        text = artifact.content.decode()
        self.assertNotIn('<img src=', text)
        self.assertIn("&lt;/script&gt;", text)
        parser = DataParser()
        parser.feed(text)
        self.assertEqual(json.loads(parser.data)["snapshot"], snapshot)

    def test_pdf_long_table_repeats_header_and_wraps_long_attribution(self):
        source = report_input(rows=70)
        source["sources"][0]["locator"] = "第12页：" + "长出处说明与公式 x^2+y^2=z^2。" * 80
        snapshot = build_report_snapshot(source, scopes()[0])
        artifact = render_export(snapshot, "pdf")
        with pymupdf.open(stream=artifact.content, filetype="pdf") as document:
            text = "\n".join(page.get_text() for page in document)
            self.assertGreater(document.page_count, 5)
            self.assertGreater(text.count("证据标识"), 1)
            self.assertIn("e70", text)
            self.assertIn("q70", text)
            for page in document:
                for word in page.get_text("words"):
                    x0, y0, x1, y1, *_ = word
                    self.assertGreaterEqual(x0, 35)
                    self.assertLessEqual(x1, page.rect.width - 35)
                    self.assertGreaterEqual(y0, 25)
                    self.assertLessEqual(y1, page.rect.height - 15)

    def test_invalid_source_version_or_unknown_format_is_rejected(self):
        source = report_input()
        source["source_revision"] = "hash-is-not-a-monotonic-revision"
        with self.assertRaises(ValueError):
            build_report_snapshot(source, scopes()[0])
        with self.assertRaises(ValueError):
            render_export(build_report_snapshot(report_input(), scopes()[0]), "docx")


def write_review_artifacts(directory: str | Path) -> list[Path]:
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    snapshot = build_report_snapshot(report_input(rows=24), scopes()[0])
    outputs = []
    for format in EXPORT_FORMATS:
        artifact = render_export(snapshot, format)
        path = directory / artifact.filename
        path.write_bytes(artifact.content)
        outputs.append(path)
        if format == "pdf":
            with pymupdf.open(stream=artifact.content, filetype="pdf") as document:
                for index in sorted({0, 1, document.page_count - 1}):
                    target = directory / f"pdf-page-{index + 1:03}.png"
                    document[index].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(str(target))
                    outputs.append(target)
    return outputs


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--review-dir":
        print("\n".join(str(path) for path in write_review_artifacts(sys.argv[2])))
    else:
        unittest.main()
