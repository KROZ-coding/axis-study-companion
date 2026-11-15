"""Bounded, read-only parsers. Uploads are data, never scripts or public assets."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO, StringIO
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import tempfile
from typing import Any, Mapping, Sequence
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from ..utils.files import validate_uploaded_content


class MaterialParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParseLimits:
    max_bytes: int = 20 * 1024 * 1024
    max_archive_members: int = 1500
    max_member_bytes: int = 12 * 1024 * 1024
    max_expanded_bytes: int = 60 * 1024 * 1024
    max_compression_ratio: int = 200
    max_pages: int = 300
    max_blocks: int = 5000
    max_text_chars: int = 2_000_000
    max_rows: int = 20_000
    max_columns: int = 150
    max_cells: int = 200_000
    max_xml_nodes: int = 200_000
    max_xml_depth: int = 100
    max_pdf_objects: int = 100_000


SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".pdf", ".docx", ".pptx", ".xlsx", ".xml", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})


def is_answer_content(text: str) -> bool:
    """Conservative publication guard; human answer-free attestation is still required."""
    return bool(re.search(r"(?:参考答案|标准答案|正确答案|答案\s*[:：]|(?:^|\n)\s*(?:[#*]+\s*)?(?:答案|解答|answer(?:\s+key)?|solution)\s*(?:[:：\n]|$))",text,re.I))


def validate_filename(filename: str) -> str:
    if not isinstance(filename,str) or not filename or len(filename) > 240:
        raise MaterialParseError("invalid_filename", "文件名无效")
    if (filename != filename.strip() or any(ord(c)<32 or ord(c)==127 for c in filename)
            or any(c in filename for c in '/\\:<>"|?*') or filename in {".",".."}
            or PureWindowsPath(filename).name != filename or PurePosixPath(filename).name != filename
            or filename.endswith(("."," "))):
        raise MaterialParseError("unsafe_filename", "文件名包含路径或不安全字符")
    if filename.split(".")[0].upper() in {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}:
        raise MaterialParseError("unsafe_filename", "保留文件名不可上传")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise MaterialParseError("unsupported_format", "不支持该格式；含宏 Office 文件不可上传")
    return suffix


class MaterialParser:
    def __init__(self, limits: ParseLimits | None = None) -> None:
        self.limits = limits or ParseLimits()

    def parse(self, filename: str, content: bytes) -> dict[str, Any]:
        suffix = validate_filename(filename)
        if not isinstance(content,bytes) or not content:
            raise MaterialParseError("empty_file", "文件为空")
        if len(content) > self.limits.max_bytes:
            raise MaterialParseError("file_too_large", "文件超过上传大小限制")
        # One empty directory per upload; no interpreter/build command runs here.
        with tempfile.TemporaryDirectory(prefix="material-parse-") as directory:
            (Path(directory)/"upload.bin").write_bytes(content)
            try:
                result = self._parse(suffix,content)
            except MaterialParseError:
                raise
            except (UnicodeError, BadZipFile, DefusedXmlException, SafeET.ParseError, csv.Error) as error:
                raise MaterialParseError("invalid_document", "文件格式无效或包含不安全 XML") from error
            except (ValueError, KeyError, IndexError, OverflowError, TypeError, RecursionError, RuntimeError, NotImplementedError, zlib.error) as error:
                raise MaterialParseError("invalid_document", "文件内容无法安全解析") from error
        blocks = result["blocks"]
        if len(blocks) > self.limits.max_blocks or sum(len(b.get("text", "")) for b in blocks) > self.limits.max_text_chars:
            raise MaterialParseError("content_limit", "解析内容超过块数或文本长度限制")
        if not blocks:
            blocks.append(self._block("document:1","image",""))
            result["issues"].append(self._issue("no_extractable_text","无法提取文字，需要人工录入","manual_required","document:1"))
        result["status"] = "manual_required" if any(i["severity"] == "manual_required" for i in result["issues"]) else "parsed"
        result["format"] = suffix.lstrip(".")
        result["sha256"] = hashlib.sha256(content).hexdigest()
        result.setdefault("metadata", {})["ocr_configured"] = False
        return result

    @staticmethod
    def _block(anchor: str, kind: str, text: str, data: Mapping[str,Any] | None = None) -> dict[str,Any]:
        if kind in {"text","table"} and is_answer_content(text):
            kind = "answer"
        return {"anchor":anchor,"kind":kind,"text":text,"data":dict(data or {})}

    @staticmethod
    def _issue(code: str, message: str, severity: str = "blocking", anchor: str | None = None) -> dict[str,Any]:
        result: dict[str,Any] = {"code":code,"message":message,"severity":severity}
        if anchor is not None:
            result["anchor"] = anchor
        return result

    def _parse(self, suffix: str, content: bytes) -> dict[str,Any]:
        if suffix in {".txt",".md",".markdown"}:
            if suffix in {".md",".markdown"}:
                validate_uploaded_content(".md",content)
            text = content.decode("utf-8-sig")
            if "\x00" in text:
                raise MaterialParseError("binary_text", "文本文件包含二进制内容")
            blocks = []
            lines = text.splitlines()
            start, pending = 1, []
            for index,line in enumerate(lines,1):
                if not line.strip():
                    if pending:
                        blocks.append(self._block(f"lines:{start}-{index-1}","text","\n".join(pending)))
                        pending = []
                else:
                    if not pending:
                        start = index
                    pending.append(line)
            if pending:
                blocks.append(self._block(f"lines:{start}-{len(lines)}","text","\n".join(pending)))
            return {"blocks":blocks,"issues":[],"metadata":{"encoding":"utf-8-sig","render_as":"plain_text"}}
        if suffix in IMAGE_SUFFIXES:
            self._validate_image(suffix,content)
            return {"blocks":[self._block("image:1","image","")],"issues":[self._issue("ocr_not_configured","OCR 未配置，请人工转录后确认","manual_required","image:1")],"metadata":{}}
        if suffix == ".csv":
            return self._csv(content)
        if suffix == ".pdf":
            validate_uploaded_content(suffix,content)
            return self._pdf(content)
        if suffix == ".xml":
            root = self._xml(content)
            values, stack = [], [(root,"/"+self._local(root.tag)+"[1]")]
            while stack:
                element,path = stack.pop()
                value = (element.text or "").strip()
                if value:
                    values.append(self._block(path,"text",value,{"attributes":dict(element.attrib)}))
                counts: dict[str,int] = {}
                children = []
                for child in element:
                    name = self._local(child.tag)
                    counts[name] = counts.get(name,0)+1
                    children.append((child,f"{path}/{name}[{counts[name]}]"))
                stack.extend(reversed(children))
            return {"blocks":values,"issues":[],"metadata":{}}
        with self._archive(content) as archive:
            if suffix == ".docx":
                validate_uploaded_content(suffix,content,self.limits.max_expanded_bytes)
                return self._docx(archive)
            if suffix == ".pptx":
                return self._pptx(archive)
            return self._xlsx(content,archive)

    @staticmethod
    def _validate_image(suffix: str, content: bytes) -> None:
        valid = ((suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                 or (suffix in {".jpg",".jpeg"} and content.startswith(b"\xff\xd8\xff"))
                 or (suffix in {".tif",".tiff"} and content.startswith((b"II*\x00",b"MM\x00*")))
                 or (suffix == ".bmp" and content.startswith(b"BM"))
                 or (suffix == ".webp" and len(content)>=12 and content[:4]==b"RIFF" and content[8:12]==b"WEBP"))
        if not valid:
            raise MaterialParseError("invalid_image", "图片签名与扩展名不符")

    @staticmethod
    def _local(tag: str) -> str:
        return tag.rsplit("}",1)[-1]

    def _xml(self, content: bytes):
        if len(content) > self.limits.max_member_bytes:
            raise MaterialParseError("xml_limit", "XML 超过大小限制")
        root = SafeET.fromstring(content,forbid_dtd=True,forbid_entities=True,forbid_external=True)
        stack, count = [(root,1)], 0
        while stack:
            element,depth = stack.pop()
            count += 1
            if count > self.limits.max_xml_nodes or depth > self.limits.max_xml_depth:
                raise MaterialParseError("xml_limit", "XML 深度或节点数超过限制")
            for key,value in element.attrib.items():
                if self._local(key).lower() in {"href","src","schemalocation","nonamespaceschemalocation"} and re.search(r"^(?:[a-z][a-z0-9+.-]*:|//|\\\\)",value.strip(),re.I):
                    raise MaterialParseError("external_link", "XML 包含外部链接")
            stack.extend((child,depth+1) for child in element)
        return root

    def _archive(self, content: bytes) -> ZipFile:
        archive = ZipFile(BytesIO(content))
        try:
            entries = archive.infolist()
            if len(entries) > self.limits.max_archive_members:
                raise MaterialParseError("archive_members", "压缩包成员过多")
            total, names = 0, set()
            for item in entries:
                name = item.filename
                parts = PurePosixPath(name).parts
                if (not name or name in names or name.casefold() in names or "\\" in name or ":" in name
                    or name.startswith("/") or any(part in {".",".."} for part in name.split("/"))
                    or any(ord(c)<32 for c in name) or PureWindowsPath(name).is_absolute()
                    or ((item.external_attr>>16)&0o170000)==0o120000):
                    raise MaterialParseError("archive_path", "压缩包包含不安全或重复路径")
                names.add(name)
                names.add(name.casefold())
                if item.flag_bits & 1 or item.compress_type not in {ZIP_STORED,ZIP_DEFLATED}:
                    raise MaterialParseError("archive_encoding", "不支持加密或特殊压缩成员")
                lower = name.lower()
                if (any(token in lower for token in ("vbaproject","vbadata","macros/","activex/","embeddings/","externallinks/","querytables/"))
                        or any(token in lower for token in ("%2e","%2f","%5c")) or "#" in lower or "?" in lower
                        or lower.endswith((".exe",".dll",".com",".bat",".cmd",".ps1",".js",".vbs",".scr",".bin"))
                        or lower.endswith("connections.xml")):
                    raise MaterialParseError("active_content", "压缩文档包含宏、嵌入程序或外部数据连接")
                total += item.file_size
                if item.file_size > self.limits.max_member_bytes or total > self.limits.max_expanded_bytes or item.file_size > max(item.compress_size,1)*self.limits.max_compression_ratio:
                    raise MaterialParseError("archive_bomb", "压缩包展开大小或压缩比超过限制")
            # Bounded streaming verifies actual output/CRC; never extract member paths to disk.
            for item in entries:
                if item.is_dir():
                    continue
                with archive.open(item) as stream:
                    seen = 0
                    while True:
                        chunk = stream.read(min(65536,self.limits.max_member_bytes-seen+1))
                        if not chunk:
                            break
                        seen += len(chunk)
                        if seen > self.limits.max_member_bytes or seen > item.file_size:
                            raise MaterialParseError("archive_bomb", "压缩成员实际展开超过声明大小")
                if seen != item.file_size:
                    raise MaterialParseError("archive_size", "压缩成员大小不一致")
                if item.filename.lower().endswith((".xml",".rels")):
                    root = self._xml(archive.read(item))
                    if item.filename.lower().endswith(".rels"):
                        for element in root.iter():
                            if self._local(element.tag) != "Relationship":
                                continue
                            target = element.attrib.get("Target","").strip()
                            relation_type = element.attrib.get("Type","").lower()
                            if (element.attrib.get("TargetMode","").lower()=="external" or
                                    re.search(r"^(?:[a-z][a-z0-9+.-]*:|//|\\\\)",target,re.I) or "\\" in target
                                    or any(token in target.casefold() for token in ("%2e","%2f","%5c"))
                                    or relation_type.rsplit("/",1)[-1] in {"vbaproject","oleobject","package","attachedtemplate"}):
                                raise MaterialParseError("external_link", "Office 文档包含外链、宏或嵌入对象")
                            folder = posixpath.dirname(posixpath.dirname(item.filename))
                            normalized = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(folder,target))
                            if normalized.startswith("../") or normalized == ".." or ":" in normalized:
                                raise MaterialParseError("archive_path", "Office 关系指向包外路径")
                    if item.filename=="[Content_Types].xml":
                        serialized = archive.read(item).lower()
                        if b"macroenabled" in serialized or b"vbaproject" in serialized:
                            raise MaterialParseError("active_content", "文档声明包含宏")
            return archive
        except BaseException:
            archive.close()
            raise

    def _table(self, anchor: str, rows: list[list[Any]], *, sheet: str | None = None) -> dict[str,Any]:
        if len(rows)>self.limits.max_rows or any(len(r)>self.limits.max_columns for r in rows) or sum(len(r) for r in rows)>self.limits.max_cells:
            raise MaterialParseError("table_limit", "表格行数、列数或单元格数超过限制")
        return self._block(anchor,"table","\n".join("\t".join("" if v is None else str(v) for v in row) for row in rows),{"rows":rows,**({"sheet":sheet} if sheet else {})})

    def _csv(self, content: bytes) -> dict[str,Any]:
        text = content.decode("utf-8-sig")
        if "\x00" in text:
            raise MaterialParseError("binary_text", "CSV 包含二进制内容")
        try:
            dialect = csv.Sniffer().sniff(text[:8192],delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows, cells = [], 0
        for row in csv.reader(StringIO(text,newline=""),dialect=dialect,strict=True):
            if len(rows)>=self.limits.max_rows or len(row)>self.limits.max_columns:
                raise MaterialParseError("table_limit", "CSV 行列超过限制")
            # Formula-like CSV cells remain literal data; they are never evaluated or exported here.
            rows.append(row)
            cells += len(row)
            if cells>self.limits.max_cells:
                raise MaterialParseError("table_limit", "CSV 单元格超过限制")
        return {"blocks":[self._table("csv:rows:1-"+str(len(rows)),rows)],"issues":[],"metadata":{"encoding":"utf-8-sig","formula_execution":False}}

    def _docx(self, archive: ZipFile) -> dict[str,Any]:
        if "word/document.xml" not in archive.namelist():
            raise MaterialParseError("invalid_docx", "DOCX 缺少正文")
        root = self._xml(archive.read("word/document.xml"))
        body = next((n for n in root.iter() if self._local(n.tag)=="body"),None)
        if body is None:
            raise MaterialParseError("invalid_docx", "DOCX 缺少正文")
        blocks, issues, paragraphs, tables, pages = [], [], 0, 0, 1
        for element in body:
            name = self._local(element.tag)
            if name == "p":
                paragraphs += 1
                anchor = f"word/document.xml#p:{paragraphs}"
                text = "".join(n.text or "" for n in element.iter() if self._local(n.tag)=="t").strip()
                if text:
                    blocks.append(self._block(anchor,"text",text))
                drawings = any(self._local(n.tag) in {"drawing","pict","object"} for n in element.iter())
                if drawings:
                    image_anchor = anchor+"/image"
                    blocks.append(self._block(image_anchor,"image",""))
                    issues.append(self._issue("ocr_not_configured","段落含图片，OCR 未配置，请人工转录","manual_required",image_anchor))
            elif name == "tbl":
                tables += 1
                rows = [["".join(n.text or "" for n in cell.iter() if self._local(n.tag)=="t") for cell in row if self._local(cell.tag)=="tc"] for row in element if self._local(row.tag)=="tr"]
                blocks.append(self._table(f"word/document.xml#table:{tables}",rows))
            pages += sum(1 for n in element.iter() if self._local(n.tag)=="lastRenderedPageBreak" or (self._local(n.tag)=="br" and any(self._local(k)=="type" and v=="page" for k,v in n.attrib.items())))
            if pages>self.limits.max_pages or len(blocks)>self.limits.max_blocks:
                raise MaterialParseError("page_limit", "DOCX 页数或内容块超过限制")
        return {"blocks":blocks,"issues":issues,"metadata":{"paragraphs":paragraphs,"tables":tables,"rendered_page_breaks":pages-1}}

    def _pptx(self, archive: ZipFile) -> dict[str,Any]:
        slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml",n)),key=lambda n:int(re.search(r"slide(\d+)\.xml",n).group(1)))
        if not slides or "ppt/presentation.xml" not in archive.namelist():
            raise MaterialParseError("invalid_pptx", "PPTX 缺少演示文稿或幻灯片")
        if len(slides)>self.limits.max_pages:
            raise MaterialParseError("page_limit", "幻灯片数量超过限制")
        blocks, issues = [], []
        for index,name in enumerate(slides,1):
            root = self._xml(archive.read(name))
            paragraphs = ["".join(n.text or "" for n in p.iter() if self._local(n.tag)=="t") for p in root.iter() if self._local(p.tag)=="p"]
            text = "\n".join(p for p in paragraphs if p.strip())
            if text:
                blocks.append(self._block(f"slide:{index}","text",text,{"member":name}))
            for image_index,_ in enumerate((n for n in root.iter() if self._local(n.tag)=="pic"),1):
                anchor = f"slide:{index}/image:{image_index}"
                blocks.append(self._block(anchor,"image",""))
                issues.append(self._issue("ocr_not_configured","幻灯片含图片，OCR 未配置","manual_required",anchor))
        return {"blocks":blocks,"issues":issues,"metadata":{"slides":len(slides)}}

    def _xlsx(self, content: bytes, archive: ZipFile) -> dict[str,Any]:
        from openpyxl import load_workbook
        if "xl/workbook.xml" not in archive.namelist():
            raise MaterialParseError("invalid_xlsx", "Excel 缺少工作簿")
        workbook = load_workbook(BytesIO(content),read_only=True,data_only=False,keep_links=False)
        blocks, issues, total_cells = [], [], 0
        try:
            if len(workbook.worksheets)>self.limits.max_pages:
                raise MaterialParseError("page_limit", "工作表数量超过限制")
            for index,sheet in enumerate(workbook.worksheets,1):
                if (sheet.max_row or 0)>self.limits.max_rows or (sheet.max_column or 0)>self.limits.max_columns:
                    raise MaterialParseError("table_limit", "Excel 工作表维度超过限制")
                anchor, rows, formulas = f"sheet:{index}", [], []
                for row in sheet.iter_rows():
                    values = []
                    for cell in row:
                        total_cells += 1
                        if total_cells>self.limits.max_cells:
                            raise MaterialParseError("table_limit", "Excel 单元格数量超过限制")
                        value = cell.value
                        if cell.data_type == "f":
                            formula = str(value)
                            if re.search(r"\[[^\]]+\]|(?:https?|file|ftp):|\\\\|(?:HYPERLINK|WEBSERVICE|RTD|DDE|EXEC|CALL|REGISTER)\s*\(",formula,re.I):
                                raise MaterialParseError("external_link", "Excel 公式包含外部链接或活动内容")
                            formulas.append(cell.coordinate)
                        if isinstance(value,(datetime,date)):
                            value = value.isoformat()
                        elif isinstance(value,float) and not math.isfinite(value):
                            raise MaterialParseError("invalid_number", "Excel 包含非有限数值")
                        elif value is not None and not isinstance(value,(str,int,float,bool)):
                            value = str(value)
                        values.append(value)
                    rows.append(values)
                    if len(rows)>self.limits.max_rows:
                        raise MaterialParseError("table_limit", "Excel 行数超过限制")
                blocks.append(self._table(anchor,rows,sheet=sheet.title))
                if formulas:
                    issues.append(self._issue("formula_not_evaluated","表格含公式，未执行且不可当作成绩；请人工核对或替换公式值","blocking",anchor))
                if sheet.sheet_state != "visible":
                    issues.append(self._issue("hidden_sheet","隐藏工作表需要人工核对","blocking",anchor))
        finally:
            workbook.close()
        return {"blocks":blocks,"issues":issues,"metadata":{"sheets":len(blocks),"read_only":True,"formula_execution":False}}

    def _pdf(self, content: bytes) -> dict[str,Any]:
        from pypdf import PdfReader
        from pypdf.generic import DictionaryObject, IndirectObject, StreamObject
        try:
            reader = PdfReader(BytesIO(content),strict=True)
            if reader.is_encrypted:
                raise MaterialParseError("encrypted_pdf", "不接受加密 PDF")
            if len(reader.pages)>self.limits.max_pages:
                raise MaterialParseError("page_limit", "PDF 页数超过限制")
            visited, stack, count, decoded_total = set(), [reader.trailer], 0, 0
            forbidden = {"/JavaScript","/JS","/Launch","/EmbeddedFiles","/OpenAction","/AA","/RichMedia","/XFA"}
            while stack:
                item = stack.pop()
                if isinstance(item,IndirectObject):
                    key = (item.idnum,item.generation)
                    if key in visited:
                        continue
                    visited.add(key)
                    item = item.get_object()
                count += 1
                if count>self.limits.max_pdf_objects:
                    raise MaterialParseError("pdf_limit", "PDF 对象数超过限制")
                if isinstance(item,DictionaryObject):
                    if forbidden.intersection(item.keys()) or str(item.get("/S","")) in {"/JavaScript","/Launch","/URI","/GoToR","/SubmitForm","/ImportData"}:
                        raise MaterialParseError("active_content", "PDF 包含动作、外部链接或嵌入内容")
                    if isinstance(item,StreamObject):
                        raw = getattr(item,"_data",b"")
                        filters = item.get("/Filter")
                        if str(filters) in {"/FlateDecode","/Fl"}:
                            decoder = zlib.decompressobj()
                            decoded = decoder.decompress(raw,self.limits.max_member_bytes+1)
                            if len(decoded)>self.limits.max_member_bytes or decoder.unconsumed_tail:
                                raise MaterialParseError("pdf_bomb", "PDF 流展开超过限制")
                            decoded_total += len(decoded)
                        elif not filters:
                            decoded_total += len(raw)
                        elif str(item.get("/Subtype","")) not in {"/Image"}:
                            # Text streams using chains/special compression cannot be safely bounded here.
                            raise MaterialParseError("pdf_filter", "PDF 文本流采用不支持的压缩方式")
                        if decoded_total>self.limits.max_expanded_bytes:
                            raise MaterialParseError("pdf_bomb", "PDF 总展开内容超过限制")
                    stack.extend(item.values())
                elif isinstance(item,(list,tuple)):
                    stack.extend(item)
            blocks, issues = [], []
            for number,page in enumerate(reader.pages,1):
                anchor = f"page:{number}"
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append(self._block(anchor,"text",text))
                else:
                    blocks.append(self._block(anchor,"image",""))
                    issues.append(self._issue("ocr_not_configured","该页无可提取文字，OCR 未配置，请人工转录","manual_required",anchor))
            return {"blocks":blocks,"issues":issues,"metadata":{"pages":len(reader.pages)}}
        except MaterialParseError:
            raise
        except Exception as error:
            # pypdf has format-specific exceptions outside PdfReadError; expose no local paths.
            raise MaterialParseError("invalid_pdf", "PDF 无法安全解析") from error


def parse_material(filename: str, content: bytes, *, limits: ParseLimits | None = None) -> dict[str,Any]:
    return MaterialParser(limits).parse(filename,content)


def preview_grade_mapping(rows: Sequence[Sequence[Any]], mapping: Mapping[str,Any], *,
                          existing: Sequence[Mapping[str,Any]] = ()) -> dict[str,Any]:
    """Explicit zero-based mapping only. Missing/invalid scores stay null, never fabricated as zero."""
    student_column, score_columns = mapping.get("student_number_column"), mapping.get("score_columns")
    header_rows = mapping.get("header_rows",1)
    if (isinstance(student_column,bool) or not isinstance(student_column,int) or student_column<0
            or not isinstance(score_columns,dict) or not score_columns
            or isinstance(header_rows,bool) or not isinstance(header_rows,int) or not 0<=header_rows<=10):
        raise ValueError("explicit student number and score column mapping required")
    if any(not isinstance(name,str) or not name.strip() or isinstance(index,bool) or not isinstance(index,int) or index<0 for name,index in score_columns.items()):
        raise ValueError("invalid score column mapping")
    all_columns = [student_column,*score_columns.values()]
    if len(set(all_columns)) != len(all_columns) or max(all_columns)>149 or len(rows)>20_000:
        raise ValueError("mapping columns overlap or exceed limits")
    maximums = mapping.get("maximums",{})
    if not isinstance(maximums,dict) or set(maximums)-set(score_columns):
        raise ValueError("invalid score maximum mapping")
    limits = {}
    for subject,maximum in maximums.items():
        try:
            number = Decimal(str(maximum))
        except InvalidOperation as error:
            raise ValueError("invalid score maximum") from error
        if not number.is_finite() or number<=0:
            raise ValueError("score maximum must be positive and finite")
        limits[subject] = number
    prior = {(str(item.get("student_number","")).strip(),str(item.get("subject",""))) for item in existing}
    seen: set[tuple[str,str]] = set()
    records, issues = [], []
    for row_index,row in enumerate(rows[header_rows:],header_rows+1):
        if not isinstance(row,(list,tuple)):
            raise ValueError("invalid table row")
        if not any(value is not None and str(value).strip() for value in row):
            continue
        raw_student = row[student_column] if student_column<len(row) else None
        student_number = "" if raw_student is None else str(raw_student).strip()
        id_valid = isinstance(raw_student,str) and bool(student_number)
        if not id_valid:
            issues.append({"row":row_index,"code":"student_number_required" if not student_number else "numeric_student_number","message":"学号必须是原始文本；不补造缺失前导零"})
        for subject,column in score_columns.items():
            raw = row[column] if column<len(row) else None
            score, valid = None, id_valid
            code = None
            if raw is None or (isinstance(raw,str) and not raw.strip()):
                code = "missing_score"
            elif isinstance(raw,bool):
                code = "invalid_score"
            else:
                try:
                    value = Decimal(str(raw).strip())
                    if not value.is_finite() or value<0 or (subject in limits and value>limits[subject]) or not math.isfinite(float(value)):
                        code = "invalid_score"
                    else:
                        score = float(value)
                except (InvalidOperation,ValueError):
                    code = "invalid_score"
            if code:
                valid = False
                issues.append({"row":row_index,"subject":subject,"code":code,"message":"成绩缺失或无效，保留为空等待核对"})
            key = (student_number,subject)
            duplicate = key in seen or key in prior
            if duplicate:
                valid = False
                issues.append({"row":row_index,"subject":subject,"code":"duplicate","message":"学号与科目重复，等待人工选择"})
            if student_number:
                seen.add(key)
            records.append({"row":row_index,"student_number":student_number,"subject":subject,"score":score,"raw_score":raw,"valid":valid,"duplicate":duplicate})
    duplicate_keys = {(item["student_number"],item["subject"]) for item in records if item["duplicate"]}
    for item in records:
        if (item["student_number"],item["subject"]) in duplicate_keys:
            item["valid"] = False
            item["duplicate"] = True
    return {"records":records,"issues":issues,"ready":bool(records) and all(item["valid"] for item in records),"mapping":dict(mapping),"committed":False}


def prepare_input(store, job_id: str) -> dict[str,Any]:
    job = store.get_job(job_id)
    if job is None or job["kind"]!="material_parse" or job["status"]!="pending":
        raise ValueError("pending material parse job required")
    material = store.get_material(job["input"]["file_id"])
    if material is None or material["revision"]!=job["input"]["material_revision"]:
        raise ValueError("material parse input revision changed")
    return {"job_id":job_id,"revision":job["revision"],"kind":"local_material_parse","filename":material["filename"],"content":store.original_bytes(material["id"]),"sha256":material["sha256"]}


def apply_result(store, job_id: str, result: Mapping[str,Any], *, expected_revision: int) -> dict[str,Any]:
    return store.apply_parse_result(job_id,result,expected_revision=expected_revision)
