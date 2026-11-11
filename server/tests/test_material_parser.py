"""Read-only parsing and malicious input regressions; every fixture is generated locally."""
from dataclasses import replace
from io import BytesIO
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from pypdf import PdfWriter

from app.services.material_parser import (
    MaterialParseError, ParseLimits, parse_material, preview_grade_mapping,
)


def archive_bytes(members):
    stream = BytesIO()
    with ZipFile(stream,"w",compression=ZIP_DEFLATED) as archive:
        for name,data in members.items():
            archive.writestr(name,data)
    return stream.getvalue()


def docx_bytes(text="材料正文", **extra):
    return archive_bytes({"word/document.xml":'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>',**extra})


class MaterialParserTests(unittest.TestCase):
    def test_text_markdown_and_xml_preserve_original_anchors_without_rendering(self):
        parsed = parse_material("chapter.md","# 标题\n正文\n\n第二段 <script>text only</script>".encode())
        self.assertEqual([b["anchor"] for b in parsed["blocks"]],["lines:1-2","lines:4-4"])
        self.assertEqual(parsed["metadata"]["render_as"],"plain_text")
        self.assertEqual(len(parsed["sha256"]),64)
        xml = parse_material("outline.xml",b'<root><item code="A">first</item><item>second</item></root>')
        self.assertEqual([b["anchor"] for b in xml["blocks"]],["/root[1]/item[1]","/root[1]/item[2]"])

    def test_filename_byte_and_xml_entity_limits(self):
        for filename in ("../book.docx","C:\\book.docx","book.docm","CON.txt","file.xlsx ","x\x00.txt"):
            with self.subTest(filename=filename),self.assertRaises(MaterialParseError):
                parse_material(filename,b"body")
        with self.assertRaises(MaterialParseError):
            parse_material("x.txt",b"12345",limits=replace(ParseLimits(),max_bytes=4))
        for xml in (b'<!DOCTYPE root [<!ENTITY x SYSTEM "file:///private">]><root>&x;</root>',
                    b'<!DOCTYPE root><root/>', b'<root><a href="https://invalid.example/"/></root>'):
            with self.subTest(xml=xml),self.assertRaises(MaterialParseError):
                parse_material("x.xml",xml)

    def test_archive_traversal_macros_external_links_and_bombs_are_rejected(self):
        dangerous = [
            {"../escape.xml":"x"}, {"word/../../escape":"x"}, {"C:/escape":"x"},
            {"word\\escape.xml":"x"}, {"word/vbaProject.bin":b"macro"},
            {"word/embeddings/object.bin":b"object"},
            {"word/_rels/document.xml.rels":'<Relationships><Relationship Id="x" TargetMode="External" Target="https://invalid.example"/></Relationships>'},
            {"word/_rels/document.xml.rels":'<Relationships><Relationship Id="x" Target="../../../escape"/></Relationships>'},
            {"[Content_Types].xml":'<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'},
            {"word/huge.xml":"A"*50000},
        ]
        for members in dangerous:
            with self.subTest(members=list(members)),self.assertRaises(MaterialParseError):
                parse_material("book.docx",docx_bytes(**members))
        with self.assertRaises(MaterialParseError):
            parse_material("book.docx",docx_bytes(**{"word/extra.xml":"<x/>"}),limits=replace(ParseLimits(),max_archive_members=1))

    def test_docx_paragraph_table_and_image_are_separate_blocks(self):
        content = archive_bytes({"word/document.xml":'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
            <w:p><w:r><w:t>公开正文</w:t></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>0012</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>80</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            <w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>'''})
        result = parse_material("book.docx",content)
        self.assertEqual([b["kind"] for b in result["blocks"]],["text","table","image"])
        self.assertEqual(result["blocks"][1]["data"]["rows"],[["0012","80"]])
        self.assertEqual(result["status"],"manual_required")
        self.assertEqual(result["issues"][0]["anchor"],result["blocks"][-1]["anchor"])

    def test_pptx_text_and_image_require_manual_work(self):
        content = archive_bytes({"ppt/presentation.xml":"<presentation/>","ppt/slides/slide1.xml":'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:spTree><p:sp><a:p><a:r><a:t>第一课</a:t></a:r></a:p></p:sp><p:pic/></p:spTree></p:sld>'''})
        result = parse_material("lesson.pptx",content)
        self.assertEqual(result["blocks"][0]["text"],"第一课")
        self.assertEqual(result["status"],"manual_required")

    def test_csv_preserves_identifiers_and_formula_text_without_execution(self):
        result = parse_material("scores.csv","学号,数学\n0012,0\n0013,\n0012,=1+1\n".encode())
        rows = result["blocks"][0]["data"]["rows"]
        self.assertEqual(rows[1],["0012","0"])
        self.assertEqual(rows[3][1],"=1+1")
        preview = preview_grade_mapping(rows,{"student_number_column":0,"score_columns":{"数学":1},"maximums":{"数学":100}})
        self.assertFalse(preview["ready"])
        self.assertFalse(preview["committed"])
        self.assertEqual(preview["records"][0]["score"],0)
        self.assertIsNone(preview["records"][1]["score"])
        self.assertIsNone(preview["records"][2]["score"])
        self.assertTrue(preview["records"][0]["duplicate"])
        numeric = preview_grade_mapping([["学号","分数"],[12,80]],{"student_number_column":0,"score_columns":{"score":1}})
        self.assertEqual(numeric["records"][0]["student_number"],"12")
        self.assertFalse(numeric["ready"])
        self.assertIn("numeric_student_number",[i["code"] for i in numeric["issues"]])

    def test_mapping_rejects_overlaps_nonfinite_negative_and_out_of_range_scores(self):
        rows = [["id","score"],["001",float("nan")],["002",-1],["003",101],["004",0]]
        preview = preview_grade_mapping(rows,{"student_number_column":0,"score_columns":{"math":1},"maximums":{"math":100}})
        self.assertEqual([r["score"] for r in preview["records"]],[None,None,None,0])
        self.assertTrue(preview["records"][-1]["valid"])
        with self.assertRaises(ValueError):
            preview_grade_mapping(rows,{"student_number_column":0,"score_columns":{"math":0}})
        with self.assertRaises(ValueError):
            preview_grade_mapping(rows,{"student_number_column":0,"score_columns":{"math":1},"maximums":{"math":float("inf")}})

    def test_xlsx_is_read_only_preserves_text_ids_and_quarantines_formulas(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "成绩"
        sheet.append(["学号","数学"])
        sheet.append(["0012",0])
        sheet.append(["0013","=2+2"])
        stream = BytesIO(); workbook.save(stream); workbook.close()
        result = parse_material("scores.xlsx",stream.getvalue())
        self.assertTrue(result["metadata"]["read_only"])
        self.assertFalse(result["metadata"]["formula_execution"])
        self.assertEqual(result["blocks"][0]["data"]["rows"][1],["0012",0])
        self.assertIn("formula_not_evaluated",[i["code"] for i in result["issues"]])

    def test_xlsx_external_formula_and_dimensions_are_bounded(self):
        for value in ("=HYPERLINK(\"https://invalid.example\",\"x\")","='[outside.xlsx]Sheet1'!A1"):
            workbook = Workbook(); workbook.active.append([value]); stream = BytesIO(); workbook.save(stream); workbook.close()
            with self.subTest(formula=value),self.assertRaises(MaterialParseError):
                parse_material("x.xlsx",stream.getvalue())
        workbook = Workbook(); workbook.active.cell(100,1,"x"); stream = BytesIO(); workbook.save(stream); workbook.close()
        with self.assertRaises(MaterialParseError):
            parse_material("x.xlsx",stream.getvalue(),limits=replace(ParseLimits(),max_rows=10))

    def test_scan_pdf_and_images_explicitly_require_manual_transcription(self):
        writer = PdfWriter(); writer.add_blank_page(width=200,height=200); stream = BytesIO(); writer.write(stream)
        result = parse_material("scan.pdf",stream.getvalue())
        self.assertEqual(result["status"],"manual_required")
        self.assertEqual(result["blocks"][0]["anchor"],"page:1")
        self.assertEqual(result["issues"][0]["code"],"ocr_not_configured")
        png = parse_material("scan.png",b"\x89PNG\r\n\x1a\n"+b"private-image")
        self.assertEqual(png["status"],"manual_required")
        self.assertFalse(png["metadata"]["ocr_configured"])

    def test_pdf_active_content_encryption_and_page_limits_are_rejected(self):
        writer = PdfWriter(); writer.add_blank_page(width=200,height=200); writer.add_js("app.alert('untrusted')"); stream = BytesIO(); writer.write(stream)
        with self.assertRaises(MaterialParseError):
            parse_material("active.pdf",stream.getvalue())
        writer = PdfWriter(); writer.add_blank_page(width=200,height=200); writer.encrypt("local-test"); stream = BytesIO(); writer.write(stream)
        with self.assertRaises(MaterialParseError):
            parse_material("encrypted.pdf",stream.getvalue())
        writer = PdfWriter(); writer.add_blank_page(width=200,height=200); writer.add_blank_page(width=200,height=200); stream = BytesIO(); writer.write(stream)
        with self.assertRaises(MaterialParseError):
            parse_material("many.pdf",stream.getvalue(),limits=replace(ParseLimits(),max_pages=1))


if __name__=="__main__":
    unittest.main()
