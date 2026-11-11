"""One safe frozen report model rendered as HTML, PDF, XLSX, JSON or CSV ZIP.

Only allowlisted identifiers, scores and attribution metadata enter the frozen
model. Question stems, answer keys, submitted answers, attachment bodies and
teacher-only notes never enter an export. Rendering never reads a live store or
fetches an external resource. PDF/XLSX dependencies are imported on demand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from xml.dom import minidom
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ..report_store import SNAPSHOT_SCHEMA_VERSION, canonical_json, content_hash, normalize_scope
from .mastery import DEFAULT_RULE_VERSION, build_mastery, get_rule, normalize_evidence


EXPORT_SCHEMA_VERSION = "report.export.v1"
EXPORT_FORMATS = ("html", "pdf", "xlsx", "json", "csv")
_FORMULA = "percent = round_half_up(100 * mean(first independent score_ratio per question family), 1); percent = null if independent families < 3"
_FIELD_NOTES = {
    "percent": "证据不足为空，不是零分。",
    "independent": "每个题族仅首次且无提示、未暴露答案的作答可贡献独立证据；后续作答均视为重做。",
    "teacher_judgment": "教师判断独立显示，不覆盖自动计算；新证据会重新计算并标记判断所依据证据是否变化。",
    "privacy": "本报告只含许可范围内的标识、评分与来源元数据；不含题目答案、附件原文、学生作答原文和教师内部批注。",
    "spreadsheet_text": "CSV/Excel 中可能触发公式的文本前加单引号，全部公式表达式均作为文本。",
}
_TITLES = {"class": "班级学习报告", "student": "学生学习报告", "goal": "学习目标报告", "question_bank": "题库学习报告"}
_TABLE_TITLES = {"summary": "报告概览", "students": "学生", "goals": "学习目标", "questions": "题目版本", "question_usage": "题目聚合使用", "mastery": "掌握度证据", "evidence": "作答证据", "sources": "来源", "rules": "规则与说明"}
# Generated with the PDF skill's palette.cascade (minimal, neutral); all PDF
# colors derive from that palette. XLSX follows templates/base.py design tokens.
_PDF_COLORS = {"header": "554d35", "text": "1a1a18", "muted": "88867e", "stripe": "f2f2f0", "border": "d0ccc0", "background": "f2f2f1"}
_XLSX_COLORS = {"primary": "1B2A4A", "text": "37352F", "muted": "8C8A84", "line": "E9E9E8", "stripe": "F7F7F5", "white": "FFFFFF"}
_CONTROL = re.compile('[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]')


class ExportDependencyUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    content: bytes
    media_type: str
    filename: str
    manifest: dict[str, Any]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _rows(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"report input {key} must be an array of objects")
    return list(value)


def _identifier(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{field} must be a non-empty identifier")
    return _CONTROL.sub("", value.strip())


def _label(value: Any, *, maximum: int = 4096) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("report labels must be bounded text")
    return _CONTROL.sub(" ", value)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _allowed(value: Any) -> bool:
    # Never interpret arbitrary non-empty consent strings (e.g. 'denied') as true.
    return value is True or (isinstance(value, str) and value in {"approved", "allowed"})


def _table(columns: Sequence[tuple[str, str]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in columns]
    return {"columns": [{"key": key, "label": label} for key, label in columns],
            "rows": [{key: row.get(key) for key in keys} for row in rows]}


def build_report_snapshot(
    report_input: Mapping[str, Any], scope: Mapping[str, Any], *, rule_version: str = DEFAULT_RULE_VERSION,
) -> dict[str, Any]:
    """Create a canonical report from an authorized LearningStore report_input.

    The input boundary is a mapping, not a database schema. Scoping here can only
    narrow it. Source revision must be the producer's monotonic revision; a digest
    or generation timestamp is not substituted for that concurrency token.
    """
    if not isinstance(report_input, Mapping):
        raise ValueError("report input must be a mapping")
    scope = normalize_scope(scope)
    source_revision = _nonnegative_int(report_input.get("source_revision"), "source_revision")
    if report_input.get("scope") is not None and normalize_scope(report_input["scope"]) != scope:
        raise ValueError("report input does not match the requested scope")
    rule = get_rule(rule_version)
    question_bank = scope["kind"] == "question_bank"
    students = []
    for row in ([] if question_bank else _rows(report_input, "students")):
        students.append({"student_id": _identifier(row.get("id", row.get("student_id")), "student_id"),
                         "name": _label(row.get("display_name", row.get("name")))})
    goals = []
    for row in ([] if question_bank else _rows(report_input, "goals")):
        goals.append({"goal_id": _identifier(row.get("id", row.get("goal_id")), "goal_id"),
                      "title": _label(row.get("title", row.get("name"))),
                      "version": _nonnegative_int(row.get("version", 1), "goal version"),
                      "class_id": _identifier(row.get("class_id"), "class_id", optional=True),
                      "course_id": _identifier(row.get("course_id"), "course_id", optional=True)})
    questions = []
    for row in _rows(report_input, "questions"):
        goal_ids = row.get("goal_ids", [row["goal_id"]] if row.get("goal_id") else [])
        if not isinstance(goal_ids, (list, tuple)):
            raise ValueError("question goal_ids must be an array")
        questions.append({
            "question_id": _identifier(row.get("id", row.get("question_id")), "question_id"),
            "version": _nonnegative_int(row.get("version", row.get("question_version", 1)), "question version"),
            "family_id": _identifier(row.get("family_id", row.get("question_family_id")), "family_id", optional=True),
            "question_bank_id": _identifier(row.get("question_bank_id", row.get("bank_id")), "question_bank_id", optional=True),
            "goal_ids": [] if question_bank else sorted({_identifier(value, "goal_id") for value in goal_ids}),
            "status": _label(row.get("status", "unknown"), maximum=100),
        })
    if "student_id" in scope:
        students = [row for row in students if row["student_id"] == scope["student_id"]]
    if "goal_id" in scope:
        goals = [row for row in goals if row["goal_id"] == scope["goal_id"]]
    for key in ("class_id", "course_id"):
        if key in scope:
            goals = [row for row in goals if row[key] in {None, scope[key]}]
    if "question_bank_id" in scope:
        questions = [row for row in questions if row["question_bank_id"] == scope["question_bank_id"]]
    student_ids = {row["student_id"] for row in students}
    goal_ids = {row["goal_id"] for row in goals}
    question_by_version = {(row["question_id"], row["version"]): row for row in questions}
    by_question: dict[str, list[dict[str, Any]]] = {}
    for row in questions:
        by_question.setdefault(row["question_id"], []).append(row)
    prepared = []
    for row in ([] if question_bank else _rows(report_input, "evidence")):
        value = dict(row)
        if "family_id" not in value:
            value["family_id"] = value.get("question_family_id")
        if "goal_ids" not in value and value.get("goal_id") is None:
            raise ValueError("report evidence must identify its goal")
        qid = value.get("question_id")
        qversion = value.get("question_version", value.get("version"))
        question = question_by_version.get((qid, qversion))
        if question is None and len(by_question.get(qid, [])) == 1:
            question = by_question[qid][0]
        if question is not None:
            if value.get("family_id") is not None and question["family_id"] is not None and value["family_id"] != question["family_id"]:
                raise ValueError("evidence question family conflicts with its frozen question version")
            if value.get("family_id") is None:
                value["family_id"] = question["family_id"]
        # Unknown students/goals cannot be introduced by an evidence row.
        row_goals = value.get("goal_ids", [value.get("goal_id")])
        if not isinstance(row_goals, (list, tuple)):
            raise ValueError("evidence goal_ids must be an array")
        if value.get("student_id") not in student_ids:
            continue
        value["goal_ids"] = [goal for goal in row_goals if goal in goal_ids]
        if not value["goal_ids"]:
            continue
        if scope["kind"] == "question_bank" and qid not in by_question:
            continue
        prepared.append(value)
    evidence = normalize_evidence(prepared)
    judgments = [] if question_bank else _rows(report_input, "teacher_judgments")
    mastery = build_mastery(
        evidence, students=sorted(student_ids), goals=sorted(goal_ids), teacher_judgments=judgments,
        rule_version=rule_version, source_revision=source_revision,
    )
    # Keep all explicitly scoped questions, even when there are no attempts.
    if scope["kind"] != "question_bank":
        questions = [row for row in questions if not row["goal_ids"] or goal_ids.intersection(row["goal_ids"])]
    questions.sort(key=lambda row: (row["question_id"], row["version"]))
    students.sort(key=lambda row: row["student_id"])
    goals.sort(key=lambda row: row["goal_id"])
    if len({row["student_id"] for row in students}) != len(students) or len({row["goal_id"] for row in goals}) != len(goals):
        raise ValueError("report students and goals must be unique")
    if len({(row["question_id"], row["version"]) for row in questions}) != len(questions):
        raise ValueError("report question versions must be unique")
    usage_rows = []
    if question_bank:
        for row in _rows(report_input, "question_usage"):
            question_id = _identifier(row.get("question_id"), "question_id")
            version = _nonnegative_int(row.get("version", row.get("question_version", 1)), "question version")
            if (question_id, version) not in question_by_version:
                continue
            usage_rows.append({"question_id": question_id, "version": version,
                               "usage_count": _nonnegative_int(row.get("usage_count", 0), "usage_count"),
                               "independent_count": _nonnegative_int(row.get("independent_count", 0), "independent_count"),
                               "assisted_count": _nonnegative_int(row.get("assisted_count", 0), "assisted_count"),
                               "retry_count": _nonnegative_int(row.get("retry_count", 0), "retry_count"),
                               "exposed_count": _nonnegative_int(row.get("exposed_count", 0), "exposed_count")})
        usage_rows.sort(key=lambda row: (row["question_id"], row["version"]))
    source_rows = []
    for row in _rows(report_input, "sources"):
        if not _allowed(row.get("export_allowed", row.get("permission"))):
            continue
        source_rows.append({"source_id": _identifier(row.get("id", row.get("source_id")), "source_id"),
                            "title": _label(row.get("title")), "locator": _label(row.get("locator"), maximum=2048)})
    source_rows.sort(key=lambda row: (row["source_id"], row["locator"]))
    mastery_rows = []
    for row in ([] if question_bank else mastery):
        marker = row["teacher_judgment"] or {}
        mastery_rows.append({
            "student_id": row["student_id"], "goal_id": row["goal_id"], "percent": row["percent"], "status": row["status"],
            "independent_families": row["independent_families"], "observed_families": row["observed_families"],
            "independent_observations": row["observations"]["independent"], "hinted_observations": row["observations"]["hinted"],
            "redo_observations": row["observations"]["redo"], "exposed_observations": row["observations"]["answer_exposed"],
            "unknown_observations": row["observations"]["unknown"], "evidence_revision": row["evidence_revision"],
            "evidence_refs": row["evidence_refs"], "teacher_judgment": marker.get("judgment"),
            "teacher_judgment_revision": marker.get("revision"), "teacher_evidence_changed": marker.get("evidence_changed"),
        })
    summary = {"students": len(students), "goals": len(goals), "question_versions": len(questions), "evidence": len(evidence),
               "mastery_rows": len(mastery), "insufficient_evidence_rows": sum(row["percent"] is None for row in mastery)}
    if question_bank:
        summary = {"question_versions": len(questions), "usage_rows": len(usage_rows)}
    tables = {
        "summary": _table((("metric", "指标"), ("value", "值")), [{"metric": key, "value": value} for key, value in summary.items()]),
        "students": _table((("student_id", "学生标识"), ("name", "姓名")), students),
        "goals": _table((("goal_id", "目标标识"), ("title", "目标"), ("version", "版本"), ("class_id", "班级标识"), ("course_id", "课程标识")), goals),
        "questions": _table((("question_id", "题目标识"), ("version", "版本"), ("family_id", "题族"), ("question_bank_id", "题库标识"), ("goal_ids", "目标标识"), ("status", "审查状态")), questions),
        "question_usage": _table((("question_id", "题目标识"), ("version", "版本"), ("usage_count", "使用次数"),
                                   ("independent_count", "独立作答次数"), ("assisted_count", "提示作答次数"),
                                   ("retry_count", "重做次数"), ("exposed_count", "答案暴露次数")), usage_rows),
        "mastery": _table(tuple((key, label) for key, label in (
            ("student_id", "学生标识"), ("goal_id", "目标标识"), ("percent", "掌握度百分比"), ("status", "证据状态"),
            ("independent_families", "独立题族数"), ("observed_families", "观察题族数"), ("independent_observations", "独立作答"),
            ("hinted_observations", "提示作答"), ("redo_observations", "重做作答"), ("exposed_observations", "答案暴露作答"),
            ("unknown_observations", "暴露未知作答"), ("evidence_revision", "证据修订摘要"), ("evidence_refs", "证据引用"),
            ("teacher_judgment", "教师独立判断"), ("teacher_judgment_revision", "教师判断版本"), ("teacher_evidence_changed", "判断后证据已变化"),
        )), mastery_rows),
        "evidence": _table((("evidence_id", "证据标识"), ("revision", "证据修订"), ("student_id", "学生标识"), ("goal_ids", "目标标识"),
                            ("question_id", "题目标识"), ("family_id", "题族"), ("attempt_id", "作答标识"), ("attempt_number", "作答次数"),
                            ("observed_at", "观察时间"), ("exposure", "作答暴露"), ("score_ratio", "得分比例"), ("status", "评分状态")), evidence),
        "sources": _table((("source_id", "来源标识"), ("title", "来源"), ("locator", "出处位置")), source_rows),
        "rules": _table((("field", "字段"), ("value", "规则与说明")),
                        [{"field": key, "value": value} for key, value in rule.to_dict().items()]
                        + [{"field": "formula", "value": _FORMULA}]
                        + [{"field": key, "value": value} for key, value in _FIELD_NOTES.items()]),
    }
    if question_bank:
        tables = {name: tables[name] for name in ("summary", "questions", "question_usage", "sources", "rules")}
    return {"schema_version": SNAPSHOT_SCHEMA_VERSION, "title": _TITLES[scope["kind"]], "scope": scope,
            "source_revision": source_revision, "rule": rule.to_dict(), "summary": summary, "tables": tables,
            "field_notes": dict(_FIELD_NOTES), "formula_text": _FORMULA}


def _snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("report export requires a frozen snapshot or revision")
    value = value.get("snapshot", value)
    if not isinstance(value, Mapping):
        raise ValueError("invalid report snapshot")
    result = json.loads(canonical_json(dict(value)))
    if result.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported report snapshot schema")
    normalize_scope(result.get("scope", {}))
    _nonnegative_int(result.get("source_revision"), "source_revision")
    get_rule(result.get("rule", {}).get("version"))
    tables = result.get("tables")
    expected_tables = {"summary", "questions", "question_usage", "sources", "rules"} if result["scope"]["kind"] == "question_bank" else set(_TABLE_TITLES)
    if not isinstance(tables, dict) or set(tables) != expected_tables:
        raise ValueError("report snapshot table schema is incomplete")
    for name, table in tables.items():
        columns, rows = table.get("columns"), table.get("rows")
        if not isinstance(columns, list) or not columns or not isinstance(rows, list):
            raise ValueError(f"invalid report table: {name}")
        keys = [column.get("key") for column in columns]
        if any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key) for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("invalid report column schema")
        if any(not isinstance(row, dict) or set(row) != set(keys) for row in rows):
            raise ValueError("report row does not match its columns")
    return result


def export_manifest(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": EXPORT_SCHEMA_VERSION, "snapshot_schema_version": snapshot["schema_version"],
            "snapshot_sha256": content_hash(snapshot), "scope": snapshot["scope"], "source_revision": snapshot["source_revision"],
            "rule_version": snapshot["rule"]["version"], "calibrated": snapshot["rule"]["calibrated"],
            "tables": {name: {"row_count": len(table["rows"]), "columns": table["columns"]} for name, table in snapshot["tables"].items()},
            "spreadsheet_text_policy": "formula-like text is prefixed with an apostrophe; no executable spreadsheet formulas",
            "null_policy": "JSON null; empty spreadsheet/CSV/PDF/HTML value means insufficient or unavailable evidence, not zero"}


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    return str(value)


def safe_spreadsheet_text(value: str) -> str:
    if len(value) > 32767:
        raise ValueError("spreadsheet text exceeds the Excel cell limit")
    value = _CONTROL.sub(" ", value)
    candidate = value.lstrip(" \t\r\n﻿​")
    if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _spreadsheet_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 10**15 else str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("report numbers must be finite")
        return value
    return safe_spreadsheet_text(_display(value))


def _json_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    return (json.dumps({"schema_version": EXPORT_SCHEMA_VERSION, "manifest": manifest, "snapshot": snapshot},
                       ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _html_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    def escape(value: Any) -> str:
        return html.escape(_display(value), quote=True)
    pieces = ["<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
              "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
              "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'\">",
              f"<title>{escape(snapshot['title'])}</title>",
              "<style>body{font-family:'Microsoft YaHei',sans-serif;margin:2rem;color:#1a1a18;background:#f2f2f1}"
              "h1{font-size:1.8rem}h2{margin-top:2rem;break-after:avoid}table{border-collapse:collapse;width:100%;margin:1rem auto;table-layout:fixed}"
              "th,td{padding:.6rem;text-align:left;vertical-align:top;border-bottom:1px solid #d0ccc0;overflow-wrap:anywhere;white-space:pre-wrap}"
              "th{background:#554d35;color:white}tr:nth-child(even){background:#f2f2f0}thead{display:table-header-group}"
              ".table{overflow-x:auto}.meta{overflow-wrap:anywhere}code{white-space:pre-wrap;overflow-wrap:anywhere}"
              "@media print{body{margin:0;background:white}thead{display:table-header-group}tr{break-inside:avoid}.table{overflow:visible}}"
              "</style></head><body>", f"<h1>{escape(snapshot['title'])}</h1>",
              f"<p class=\"meta\">源修订 {snapshot['source_revision']} · 规则 {escape(snapshot['rule']['version'])} · 快照 SHA256 {manifest['snapshot_sha256']}</p>",
              f"<p>{escape(snapshot['rule']['notice'])}</p>", f"<p><code>{escape(snapshot['formula_text'])}</code></p>"]
    for name, table in snapshot["tables"].items():
        pieces.append(f"<h2>{_TABLE_TITLES[name]}</h2><div class=\"table\"><table data-table=\"{name}\"><thead><tr>")
        pieces.extend(f"<th scope=\"col\">{escape(column['label'])}</th>" for column in table["columns"])
        pieces.append("</tr></thead><tbody>")
        for row in table["rows"]:
            pieces.append("<tr>" + "".join(f"<td>{escape(row[column['key']])}</td>" for column in table["columns"]) + "</tr>")
        pieces.append("</tbody></table></div>")
    embedded = canonical_json({"schema_version": EXPORT_SCHEMA_VERSION, "manifest": manifest, "snapshot": snapshot})
    for character in ("&", "<", ">", chr(8232), chr(8233)):
        embedded = embedded.replace(character, chr(92) + "u" + f"{ord(character):04x}")
    pieces.append(f"<script type=\"application/json\" id=\"report-data\">{embedded}</script></body></html>")
    return "\n".join(pieces).encode("utf-8")


def _zip_entry(archive: ZipFile, name: str, payload: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


def _csv_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    output = io.BytesIO()
    manifest = json.loads(canonical_json(manifest))
    manifest["files"] = {}
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, table in snapshot["tables"].items():
            text = io.StringIO(newline="")
            writer = csv.writer(text, lineterminator="\r\n")
            keys = [column["key"] for column in table["columns"]]
            writer.writerow(keys)
            for row in table["rows"]:
                writer.writerow([_spreadsheet_value(row[key]) for key in keys])
            payload = text.getvalue().encode("utf-8-sig")
            filename = name + ".csv"
            _zip_entry(archive, filename, payload)
            manifest["files"][filename] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                                            "row_count": len(table["rows"]), "encoding": "utf-8-sig"}
        _zip_entry(archive, "manifest.json", (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return output.getvalue(), manifest


def _xlsx_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as error:
        raise ExportDependencyUnavailable("Excel export requires openpyxl>=3.1,<4") from error
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.properties.creator = "Z.ai"
    workbook.properties.title = snapshot["title"]
    workbook.properties.subject = f"Frozen report {manifest['snapshot_sha256']}"
    workbook.properties.created = workbook.properties.modified = datetime(2000, 1, 1, tzinfo=timezone.utc)
    # Styles follow the spreadsheet skill templates/base.py factories. Keeping
    # their small token set here avoids a production dependency on CLI plugins.
    font_name = "Microsoft YaHei" if os.name == "nt" else "Noto Sans CJK SC"
    header_font = Font(name=font_name, size=11, bold=True, color=_XLSX_COLORS["white"])
    body_font = Font(name=font_name, size=11, color=_XLSX_COLORS["text"])
    title_font = Font(name=font_name, size=16, bold=True, color=_XLSX_COLORS["primary"])
    header_fill = PatternFill("solid", fgColor=_XLSX_COLORS["primary"])
    header_border = Border(bottom=Side(style="thin", color=_XLSX_COLORS["line"]))

    def sheet(name: str, title: str, columns: list[dict[str, str]], rows: list[dict[str, Any]]) -> None:
        ws = workbook.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws.column_dimensions["A"].width = 3
        ws.row_dimensions[1].height = 15
        ws.row_dimensions[2].height = 32
        ws.row_dimensions[3].height = 18
        last_col = len(columns) + 1
        ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=last_col)
        ws["B2"] = safe_spreadsheet_text(title)
        ws["B2"].font = title_font
        ws["B3"] = f"源修订 {snapshot['source_revision']} · {snapshot['rule']['version']} · {name}"
        for index, column in enumerate(columns, 2):
            cell = ws.cell(4, index, safe_spreadsheet_text(column["label"]))
            cell.font, cell.fill, cell.border = header_font, header_fill, header_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[4].height = 32
        for number, row in enumerate(rows, 5):
            for index, column in enumerate(columns, 2):
                value = _spreadsheet_value(row[column["key"]])
                cell = ws.cell(number, index, value)
                if isinstance(value, str):
                    cell.data_type = "s"
                    cell.quotePrefix = True
                cell.font = body_font
                cell.alignment = Alignment(horizontal="right" if isinstance(value, (int, float)) and not isinstance(value, bool) else "left",
                                           vertical="top", wrap_text=True)
                if number % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor=_XLSX_COLORS["stripe"])
            ws.row_dimensions[number].height = 24
        # Fit columns from actual data, then grow wrapped rows; no silent clipping.
        for index, column in enumerate(columns, 2):
            texts = [_display(row[column["key"]]) for row in rows]
            width = max([len(column["label"]) * 2 + 2, 10] + [min(38, sum(2 if ord(char) > 255 else 1 for char in text) + 2) for text in texts])
            ws.column_dimensions[get_column_letter(index)].width = min(38, width)
        for number in range(5, len(rows) + 5):
            lines = 1
            for index in range(2, last_col + 1):
                value = _display(ws.cell(number, index).value)
                width = ws.column_dimensions[get_column_letter(index)].width
                lines = max(lines, sum(max(1, math.ceil(sum(2 if ord(char) > 255 else 1 for char in line) / max(1, width - 2))) for line in value.split("\n")))
            ws.row_dimensions[number].height = min(409, max(24, lines * 16 + 8))
        end = max(4, len(rows) + 4)
        ws.auto_filter.ref = f"B4:{get_column_letter(last_col)}{end}"
        ws.freeze_panes = "C5"
        ws.print_title_rows = "4:4"
        ws.print_area = f"B2:{get_column_letter(last_col)}{end}"
        ws.page_setup.orientation = "landscape" if len(columns) > 5 else "portrait"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        # Verify each completed sheet before the next is constructed.
        for row in ws.iter_rows(min_row=4, max_row=end, min_col=2, max_col=last_col):
            if any(cell.data_type in {"f", "e"} or cell.hyperlink is not None for cell in row):
                raise ValueError("unsafe formula or external link in report workbook")

    for name, table in snapshot["tables"].items():
        sheet(name, _TABLE_TITLES[name], table["columns"], table["rows"])
    manifest_rows = [{"key": key, "value": value} for key, value in manifest.items() if key != "tables"]
    manifest_rows += [{"key": "table." + name, "value": value} for name, value in manifest["tables"].items()]
    sheet("manifest", "导出清单", [{"key": "key", "label": "字段"}, {"key": "value", "label": "值"}], manifest_rows)
    buffer = io.BytesIO()
    workbook.save(buffer)
    payload = buffer.getvalue()
    # openpyxl 3.1.5 overwrites modified during save. Normalize the detached
    # core properties by namespace, and fix ZIP metadata before verifying the output.
    stable = io.BytesIO()
    with ZipFile(io.BytesIO(payload)) as original, ZipFile(stable, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(original.namelist()):
            content = original.read(name)
            if name == "docProps/core.xml":
                with minidom.parseString(content) as core:
                    for property_name in ("created", "modified"):
                        elements = core.getElementsByTagNameNS("http://purl.org/dc/terms/", property_name)
                        if len(elements) != 1:
                            raise ValueError("report workbook core timestamps are missing or duplicated")
                        element = elements[0]
                        for child in list(element.childNodes):
                            element.removeChild(child)
                            child.unlink()
                        element.appendChild(core.createTextNode("2000-01-01T00:00:00Z"))
                    content = core.toxml(encoding="utf-8")
            if name.startswith("xl/externalLinks/"):
                raise ValueError("report workbook contains external links")
            if name.endswith(".rels"):
                with minidom.parseString(content) as relationships:
                    if any(item.getAttribute("TargetMode") == "External" for item in relationships.getElementsByTagNameNS(
                        "http://schemas.openxmlformats.org/package/2006/relationships", "Relationship",
                    )):
                        raise ValueError("report workbook contains external relationships")
            _zip_entry(archive, name, content)
    payload = stable.getvalue()
    check = load_workbook(io.BytesIO(payload), data_only=False, keep_links=True)
    try:
        if len(check.worksheets) != len(snapshot["tables"]) + 1:
            raise ValueError("report workbook table count mismatch")
        if check._external_links or any(cell.hyperlink is not None for ws in check for row in ws.iter_rows() for cell in row):
            raise ValueError("report workbook contains external links")
        if any(cell.data_type in {"f", "e"} for ws in check for row in ws.iter_rows() for cell in row):
            raise ValueError("report workbook contains executable formulas or cell errors")
    finally:
        check.close()
    return payload


def _font_candidates() -> list[Path]:
    return [Path("C:/Windows/Fonts/simhei.ttf"), Path("C:/Windows/Fonts/msyh.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"), Path("/usr/share/fonts/truetype/chinese/SimHei.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/System/Library/Fonts/PingFang.ttc")]


def _column_groups(columns: list[dict[str, str]], *, maximum: int = 4) -> list[list[dict[str, str]]]:
    if len(columns) <= maximum:
        return [columns]
    return [[columns[0]] + columns[index:index + maximum - 1] for index in range(1, len(columns), maximum - 1)]


def _pdf_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    try:
        import reportlab  # noqa: F401 -- optional, lazy engine detection
    except ImportError:
        return _pymupdf_export(snapshot, manifest)
    return _reportlab_export(snapshot, manifest)


def _reportlab_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import CondPageBreak, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    font_name = "ReportCJK"
    for path in _font_candidates():
        if path.is_file():
            try:
                if font_name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(font_name, str(path)))
                break
            except Exception:
                continue
    else:
        # The installed PyMuPDF engine has an embeddable CJK fallback font.
        return _pymupdf_export(snapshot, manifest)
    pdfmetrics.registerFontFamily(font_name, normal=font_name, bold=font_name)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=48, bottomMargin=48,
                            title=snapshot["title"], author="智慧学伴", creator="Learning reports", subject="Frozen evidence report")
    body = ParagraphStyle("ReportBody", fontName=font_name, fontSize=10.5, leading=16, wordWrap="CJK", textColor=colors.HexColor(_PDF_COLORS["text"]))
    heading = ParagraphStyle("ReportHeading", parent=body, fontSize=17, leading=25, spaceAfter=10)
    cell = ParagraphStyle("ReportCell", parent=body, fontSize=9, leading=13)
    header = ParagraphStyle("ReportHeader", parent=cell, textColor=colors.white)

    def p(value: Any, style=body):
        return Paragraph(html.escape(_display(value)).replace("\n", "<br/>"), style)

    story = [p(snapshot["title"], heading), p(f"源修订 {snapshot['source_revision']} · 规则 {snapshot['rule']['version']}"),
             p("快照 SHA256 " + manifest["snapshot_sha256"]), p(snapshot["rule"]["notice"]), p(snapshot["formula_text"]), Spacer(1, 12)]
    for name, table in snapshot["tables"].items():
        for part, columns in enumerate(_column_groups(table["columns"]), 1):
            story.extend([CondPageBreak(doc.height * .25), p(_TABLE_TITLES[name] + (f" ({part})" if part > 1 else ""), heading)])
            data = [[p(column["label"], header) for column in columns]]
            data += [[p(row[column["key"]], cell) for column in columns] for row in table["rows"]]
            if len(data) == 1:
                data.append([p("无记录" if index == 0 else "", cell) for index in range(len(columns))])
            widths = [doc.width / len(columns)] * len(columns)
            if sum(widths) > doc.width + .01:
                raise ValueError("PDF table exceeds page width")
            rendered = LongTable(data, colWidths=widths, repeatRows=1, hAlign="CENTER", splitByRow=1, splitInRow=1)
            rendered.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_PDF_COLORS["header"])),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(_PDF_COLORS["stripe"])]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), .4, colors.HexColor(_PDF_COLORS["border"])),
            ]))
            story.extend([rendered, Spacer(1, 12)])

    def footer(pdf, _doc):
        pdf.saveState()
        pdf.setFont(font_name, 8)
        pdf.drawString(40, 24, f"源修订 {snapshot['source_revision']} · {snapshot['rule']['version']} · SHA256 {manifest['snapshot_sha256'][:16]}")
        pdf.drawRightString(A4[0] - 40, 24, str(pdf.getPageNumber()))
        pdf.restoreState()

    def invariant_canvas(*args, **kwargs):
        kwargs["invariant"] = 1
        return canvas.Canvas(*args, **kwargs)

    doc.build(story, onFirstPage=footer, onLaterPages=footer, canvasmaker=invariant_canvas)
    # Attach only the already-redacted frozen model, never a source attachment.
    try:
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(buffer.getvalue())))
        writer.add_metadata({"/Title": snapshot["title"], "/Author": "Z.ai", "/Creator": "Learning reports", "/Subject": "Frozen evidence report"})
        writer.add_attachment("report.json", _json_export(snapshot, manifest))
        writer.add_attachment("manifest.json", canonical_json(manifest).encode("utf-8"))
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except ImportError as error:
        raise ExportDependencyUnavailable("ReportLab PDF metadata/attachments require pypdf>=6,<7") from error


def _pymupdf_export(snapshot: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    try:
        import pymupdf
    except ImportError as error:
        raise ExportDependencyUnavailable("PDF export requires reportlab>=4.2,<5 with a CJK font, or PyMuPDF>=1.24,<2") from error
    all_text = canonical_json(snapshot)
    font = None
    for path in _font_candidates():
        if path.is_file():
            try:
                candidate = pymupdf.Font(fontfile=str(path))
                if all(candidate.has_glyph(ord(char)) for char in set(all_text) if not char.isspace()):
                    font = candidate
                    break
            except Exception:
                continue
    if font is None:
        font = pymupdf.Font("cjk")
    if any(not font.has_glyph(ord(char)) for char in set(all_text) if not char.isspace()):
        raise ValueError("PDF font does not cover all report characters")
    document = pymupdf.open()
    document.set_metadata({"title": snapshot["title"], "author": "智慧学伴", "creator": "Learning reports", "subject": "Frozen evidence report"})
    page_width, page_height, margin = 595.28, 841.89, 40.0
    available = page_width - 2 * margin
    bottom = page_height - 48
    page = None
    y = 0.0

    def rgb(key: str) -> tuple[float, float, float]:
        return tuple(int(_PDF_COLORS[key][index:index + 2], 16) / 255 for index in (0, 2, 4))

    def new_page():
        nonlocal page, y
        page = document.new_page(width=page_width, height=page_height)
        page.insert_font(fontname="ReportCJK", fontbuffer=font.buffer)
        y = 48.0
        footer = f"源修订 {snapshot['source_revision']} · {snapshot['rule']['version']} · SHA256 {manifest['snapshot_sha256'][:16]}"
        page.insert_text((margin, page_height - 24), footer, fontname="ReportCJK", fontsize=8, color=rgb("text"))
        page.insert_text((page_width - margin - 24, page_height - 24), str(document.page_count), fontname="ReportCJK", fontsize=8, color=rgb("text"))

    def wrap(value: Any, width: float, size: float) -> list[str]:
        lines = []
        for paragraph in _display(value).split("\n"):
            line = ""
            for char in paragraph:
                if line and font.text_length(line + char, fontsize=size) > width:
                    lines.append(line)
                    line = ""
                line += char
            lines.append(line)
        return lines or [""]

    def text(value: Any, *, size=10.5, spacing=8.0):
        nonlocal y
        for line in wrap(value, available, size):
            if page is None or y + size * 1.6 > bottom:
                new_page()
            page.insert_text((margin, y + size), line, fontname="ReportCJK", fontsize=size, color=rgb("text"))
            y += size * 1.6
        y += spacing

    new_page()
    text(snapshot["title"], size=21)
    text(f"源修订 {snapshot['source_revision']} · 规则 {snapshot['rule']['version']}")
    text("快照 SHA256 " + manifest["snapshot_sha256"])
    text(snapshot["rule"]["notice"])
    text(snapshot["formula_text"])
    for name, table in snapshot["tables"].items():
        for part, columns in enumerate(_column_groups(table["columns"]), 1):
            if y > bottom - 170:
                new_page()
            text(_TABLE_TITLES[name] + (f" ({part})" if part > 1 else ""), size=15, spacing=8)
            width = available / len(columns)
            size, leading, padding = 9.5, 14.5, 6.0
            if width * len(columns) > available + .01:
                raise ValueError("PDF table exceeds page width")
            header_lines = [wrap(column["label"], width - 2 * padding, size) for column in columns]
            header_height = max(map(len, header_lines)) * leading + 2 * padding

            def draw_cells(lines, height, *, header=False, stripe=False):
                nonlocal y
                if header or stripe:
                    page.draw_rect(pymupdf.Rect(margin, y, page_width - margin, y + height),
                                   color=None, fill=rgb("header" if header else "stripe"))
                for index, cell_lines in enumerate(lines):
                    for offset, line in enumerate(cell_lines):
                        page.insert_text((margin + index * width + padding, y + padding + size + offset * leading), line,
                                         fontname="ReportCJK", fontsize=size, color=(1, 1, 1) if header else rgb("text"))
                y += height
                page.draw_line((margin, y), (page_width - margin, y), color=rgb("border"), width=.3)

            def repeat_header():
                if y + header_height + leading + 2 * padding > bottom:
                    new_page()
                draw_cells(header_lines, header_height, header=True)

            repeat_header()
            rows = table["rows"] or [{column["key"]: "无记录" if index == 0 else "" for index, column in enumerate(columns)}]
            for row_index, row in enumerate(rows):
                lines = [wrap(row[column["key"]], width - 2 * padding, size) for column in columns]
                total = max(map(len, lines))
                offset = 0
                while offset < total:
                    capacity = int((bottom - y - 2 * padding) // leading)
                    if capacity < 1:
                        new_page()
                        repeat_header()
                        capacity = int((bottom - y - 2 * padding) // leading)
                    take = min(total - offset, capacity)
                    chunk = [cell_lines[offset:offset + take] for cell_lines in lines]
                    draw_cells(chunk, take * leading + 2 * padding, stripe=row_index % 2 == 1)
                    offset += take
                    if offset < total:
                        new_page()
                        repeat_header()
            y += 14
    document.embfile_add("report.json", _json_export(snapshot, manifest), filename="report.json", desc="Safe frozen report snapshot")
    document.embfile_add("manifest.json", canonical_json(manifest).encode("utf-8"), filename="manifest.json", desc="Report export manifest")
    for xref in range(1, document.xref_length()):
        kind, value = document.xref_get_key(xref, "Type")
        if kind == "name" and value == "/EmbeddedFile":
            document.xref_set_key(xref, "Params/CreationDate", "(D:20000101000000Z)")
            document.xref_set_key(xref, "Params/ModDate", "(D:20000101000000Z)")
    try:
        document.subset_fonts()
        return document.tobytes(garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()


def render_export(snapshot_or_revision: Mapping[str, Any], format: str) -> ExportArtifact:
    """Render only the supplied frozen snapshot. CSV means a multi-table ZIP."""
    if format == "csv_zip":
        format = "csv"
    if format not in EXPORT_FORMATS:
        raise ValueError("unsupported report export format")
    snapshot = _snapshot(snapshot_or_revision)
    manifest = export_manifest(snapshot)
    stem = f"report-{snapshot['scope']['kind']}-r{snapshot['source_revision']}-{manifest['snapshot_sha256'][:12]}"
    if format == "json":
        payload, media_type, suffix = _json_export(snapshot, manifest), "application/json", ".json"
    elif format == "html":
        payload, media_type, suffix = _html_export(snapshot, manifest), "text/html; charset=utf-8", ".html"
    elif format == "xlsx":
        payload, media_type, suffix = _xlsx_export(snapshot, manifest), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"
    elif format == "pdf":
        payload, media_type, suffix = _pdf_export(snapshot, manifest), "application/pdf", ".pdf"
    else:
        payload, manifest = _csv_export(snapshot, manifest)
        media_type, suffix = "application/zip", "-csv.zip"
    return ExportArtifact(payload, media_type, stem + suffix, manifest)
