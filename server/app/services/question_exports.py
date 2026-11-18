"""Complete teacher-selected question packets from authorized immutable versions."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import hashlib
import html
from io import StringIO
import json
from typing import Any

from .report_exports import safe_spreadsheet_text

SCHEMA_VERSION = 1
MAX_QUESTIONS = 100
MAX_EXPORT_BYTES = 32 * 1024 * 1024
_CONTENT_FIELDS = (
    "type", "stem", "options", "answer", "explanation", "points", "rubric", "difficulty",
    "knowledge_points", "answer_format", "domain", "variables", "assumptions", "constraints",
    "generation_intent", "usage_scope", "content_sha", "family_sha", "pipeline_job_id", "source_revision",
)
_SOURCE_FIELDS = (
    "block_id", "file_id", "anchor", "sha256", "original_sha256", "source_revision", "license_code",
    "license_note", "title", "filename", "source_filename", "source_locator", "source_note",
    "usage_scope", "lifecycle_status", "export_allowed", "text", "kind", "provenance",
)
_PROVENANCE_FIELDS = ("origin", "import_id", "filename", "sha256", "row_number", "generation_intent", "usage_scope")
_CALL_FIELDS = ("request_id", "model", "reasoning_effort", "provider", "usage", "request_sent", "cost_pending", "billing_mode")
_TYPES = {"single_choice": "单选题", "multi_choice": "多选题", "judge": "判断题", "fill": "填空题", "text": "作答题"}
_COLUMNS = (
    "question_id", "version", "status", "type", "stem", "options", "answer", "explanation", "points",
    "rubric", "difficulty", "knowledge_points", "answer_format", "sources", "reviews", "provenance",
    "generation_intent", "content_sha", "selection_sha256", "packet_sha256",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def selection_hash(selection: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(list(selection)).encode("utf-8")).hexdigest()


def _projection(value: Any, fields: Sequence[str]) -> dict[str, Any]:
    return {name: value[name] for name in fields if name in value} if isinstance(value, Mapping) else {}


def _reviews(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = _projection(value, ("independent_solution", "recheck_count", "reviews", "comparisons"))
    calls = value.get("calls", {})
    if isinstance(calls, Mapping):
        result["calls"] = {str(stage): _projection(call, _CALL_FIELDS) for stage, call in calls.items()}
    return result


def build_packet(details: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(details, (list, tuple)) or not 1 <= len(details) <= MAX_QUESTIONS:
        raise ValueError("select one to one hundred exact question versions")
    questions, selection, seen = [], [], set()
    for detail in details:
        item = detail.get("question") if isinstance(detail, Mapping) else None
        if not isinstance(item, Mapping) or not isinstance(item.get("content"), Mapping):
            raise ValueError("complete authorized question detail required")
        identifier, version = item.get("question_id"), item.get("version")
        if not isinstance(identifier, str) or not identifier or type(version) is not int or version < 1:
            raise ValueError("exact question identity required")
        key = (identifier, version)
        if key in seen:
            raise ValueError("duplicate question version in export selection")
        seen.add(key)
        content = item["content"]
        if not content.get("stem") or "answer" not in content:
            raise ValueError("question content is incomplete; export was not created")
        selection.append({"question_id": identifier, "version": version})
        sources = detail.get("sources", content.get("source_anchors", []))
        if not isinstance(sources, list):
            raise ValueError("question sources must be an array")
        source_rows = []
        for source in sources:
            row = _projection(source, _SOURCE_FIELDS)
            # A locator and a hash identify the original; they do not grant export of its body.
            if source.get("export_allowed") is not True:
                row.pop("text", None)
            source_rows.append(row)
        questions.append({"question_id": identifier, "version": version, "status": item.get("status"),
                          "course_id": item.get("course_id", content.get("course_id")),
                          "content": _projection(content, _CONTENT_FIELDS), "sources": source_rows,
                          "reviews": _reviews(detail.get("reviews", content.get("review_evidence", {}))),
                          "provenance": _projection(content.get("provenance"), _PROVENANCE_FIELDS)})
    packet = {"schema_version": SCHEMA_VERSION, "title": "教师题目成果包", "questions": questions,
              "selection": selection, "question_count": len(questions), "selection_sha256": selection_hash(selection),
              "field_notes": {"answer": "单选为从0开始的索引，多选为索引数组；判断为布尔值；填空数组为单空的可接受答案。",
                              "rubric": "score_units 为百分之一分；所有评分点之和等于题目满分。",
                              "sources": "保留冻结的来源、定位和哈希；未获正文导出许可时不导出原文。",
                              "reviews": "保留独立求解和比较证据；队列完成不单独证明题目质量。",
                              "null": "null 表示未提供，不代表零。",
                              "csv": "嵌套字段使用 JSON；公式样文本前置英文单引号，JSON保留原值。"}}
    packet["packet_sha256"] = hashlib.sha256(canonical_json(packet).encode("utf-8")).hexdigest()
    return packet


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict, bool)):
        return canonical_json(value)
    return str(value)


def _answer(content: Mapping[str, Any]) -> str:
    value = content.get("answer")
    if content.get("type") == "single_choice" and type(value) is int:
        options = content.get("options", [])
        return f"{chr(65 + value)}. {options[value]}" if 0 <= value < len(options) else str(value)
    if content.get("type") == "multi_choice" and isinstance(value, list):
        return "、".join(chr(65 + index) for index in value if type(index) is int)
    if type(value) is bool:
        return "正确" if value else "错误"
    return " / ".join(map(str, value)) if isinstance(value, list) else _display(value)


def _html(packet: Mapping[str, Any]) -> bytes:
    def escape(value: Any) -> str:
        return html.escape(_display(value), quote=True)
    pieces = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
              '<title>教师题目成果包</title><style>body{font-family:"Microsoft YaHei",sans-serif;color:#172c3b;max-width:960px;margin:32px auto;padding:0 24px;line-height:1.7}h1{font-size:28px}h2{font-size:21px}h3{font-size:17px}article{border-top:2px solid #aecbdc;margin-top:28px;padding-top:16px}p,li,pre{white-space:pre-wrap;overflow-wrap:anywhere}.meta{font-size:13px;color:#466070}pre{font:inherit;background:#f2f7fa;padding:12px}details{margin:16px 0}summary{cursor:pointer}@media print{body{margin:0;max-width:none}article{break-before:auto}h2,h3{break-after:avoid}details> *{display:block}summary{display:none}}</style></head><body>',
              '<h1>教师题目成果包</h1><p>含参考答案和评分细则，请按授权范围保管。</p>',
              f'<p class="meta">题目版本数 {packet["question_count"]} · 选择摘要 {escape(packet["selection_sha256"])}<br>内容摘要 {escape(packet["packet_sha256"])}</p>']
    for index, row in enumerate(packet["questions"], 1):
        content = row["content"]
        pieces += [f'<article><h2>{index}. {escape(_TYPES.get(content.get("type"), content.get("type")))}</h2>',
                   f'<p class="meta">{escape(row["question_id"])} · 版本 {row["version"]} · 状态 {escape(row["status"])} · 满分 {escape(content.get("points"))}</p>',
                   f'<p>{escape(content["stem"])}</p>']
        if content.get("options"):
            pieces.append('<ol type="A">' + "".join(f'<li>{escape(option)}</li>' for option in content["options"]) + '</ol>')
        pieces += [f'<h3>参考答案</h3><p>{escape(_answer(content))}</p>',
                   f'<h3>解析</h3><p>{escape(content.get("explanation"))}</p>']
        if content.get("rubric"):
            pieces.append('<h3>评分细则</h3><ul>' + "".join(f'<li>{escape(point.get("point"))}（{point.get("score_units", 0) / 100:g} 分）</li>' for point in content["rubric"]) + '</ul>')
        pieces += [f'<h3>来源与定位</h3><pre>{escape(json.dumps(row["sources"], ensure_ascii=False, indent=2))}</pre>',
                   f'<details><summary>独立审查与生成记录</summary><pre>{escape(json.dumps({"reviews":row["reviews"],"provenance":row["provenance"]}, ensure_ascii=False, indent=2))}</pre></details></article>']
    pieces.append('<h2>字段说明</h2>' + "".join(f'<p><strong>{escape(name)}</strong>：{escape(note)}</p>' for name, note in packet["field_notes"].items()))
    pieces.append('</body></html>')
    return "".join(pieces).encode("utf-8")


def export_questions(details: Sequence[Mapping[str, Any]], format: str) -> tuple[bytes, str, str]:
    packet = build_packet(details)
    suffix = packet["selection_sha256"][:12]
    if format == "json":
        data = (json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        media_type = "application/json"
    elif format == "csv":
        stream = StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=_COLUMNS)
        writer.writeheader()
        for question in packet["questions"]:
            row = {**question["content"], **{key: question[key] for key in ("question_id", "version", "status", "sources", "reviews", "provenance")},
                   "selection_sha256": packet["selection_sha256"], "packet_sha256": packet["packet_sha256"]}
            writer.writerow({key: safe_spreadsheet_text(_display(row.get(key))) for key in _COLUMNS})
        data, media_type = stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    elif format == "html":
        data, media_type = _html(packet), "text/html; charset=utf-8"
    else:
        raise ValueError("question export format must be json, csv or html")
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError("question packet exceeds 32 MiB; select fewer versions")
    return data, media_type, f"questions-{suffix}.{format}"
