"""Teacher question queries, local structured imports and review-job projections.

Question versions remain solely in LearningStore. Import previews are local
content-job drafts; confirming them creates independently reviewed candidates.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import csv
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
from typing import Any

from ..content_store import ContentConflict
from .material_parser import MaterialParseError, MaterialParser, ParseLimits, validate_filename
from .question_bank import QuestionBank, QuestionValidationError, question_fingerprints, validate_course_question

_TYPES = {"single_choice", "multi_choice", "judge", "fill", "text"}
_STATUSES = {"candidate", "reviewing", "approved", "quarantined", "retired"}
_DIFFICULTIES = {"easy", "medium", "hard"}
IMPORT_MAX_BYTES = 2 * 1024 * 1024
IMPORT_MAX_ROWS = 100
IMPORT_COLUMNS = ("type", "stem", "options", "answer", "explanation", "points", "rubric", "difficulty", "knowledge_points", "answer_format")


def _question_item(row) -> dict[str, Any]:
    return {key: row[key] for key in ("question_id", "version", "owner_id", "course_id", "status", "created_at")} | {
        "content": json.loads(row["content_json"]),
    }


def _can_read_question(learning, actor_id, item) -> bool:
    if learning._access_checker is None:
        return item["owner_id"] == actor_id
    try:
        learning._require_access(actor_id, {"kind": "question", "course_id": item["course_id"],
            "owner_id": item["owner_id"], "class_id": item["content"].get("class_id")})
        return True
    except (LookupError, PermissionError):
        return False


def _question_row(learning, db, course_id, question_id, version, actor_id):
    row = db.execute("SELECT * FROM question_versions WHERE course_id=? AND question_id=? AND version=?",
                     (course_id, question_id, version)).fetchone()
    if row is None or not _can_read_question(learning, actor_id, _question_item(row)):
        raise LookupError("question not found")
    return row


def question_usage(learning, question_id, version, actor_id) -> dict[str, Any]:
    """Count frozen task references and actual attempts, after current task authorization."""
    with learning._using_connection() as db:
        rows = db.execute("""SELECT t.*,g.owner_id,g.course_id,g.class_id,ti.frozen_json
            FROM task_items ti JOIN learning_tasks t ON t.id=ti.task_id JOIN learning_goals g ON g.id=t.goal_id
            WHERE ti.question_id=? AND ti.question_version=?""", (question_id, version)).fetchall()
        tasks = []
        for row in rows:
            try:
                if learning._access_checker:
                    learning._require_access(actor_id, {"kind": "task", **dict(row)})
                elif actor_id != row["owner_id"]:
                    continue
            except (LookupError, PermissionError):
                continue
            tasks.append(row)
        ids = [row["id"] for row in tasks]
        attempts = db.execute("""SELECT a.kind,a.submitted_at,ai.status,ai.score_units,ti.frozen_json
            FROM attempts a JOIN attempt_items ai ON ai.attempt_id=a.id
            JOIN task_items ti ON ti.task_id=ai.task_id AND ti.question_id=ai.question_id
            WHERE ai.question_id=? AND ti.question_version=? AND a.task_id IN (SELECT value FROM json_each(?))""",
            (question_id, version, json.dumps(ids))).fetchall() if ids else []
    graded = [row for row in attempts if row["status"] == "graded" and row["score_units"] is not None]
    first = [row for row in attempts if row["kind"] == "first"]
    first_graded = [row for row in graded if row["kind"] == "first"]
    ratios = [row["score_units"] / json.loads(row["frozen_json"])["points_units"] for row in graded]
    used = [row["created_at"] for row in tasks] + [row["submitted_at"] for row in attempts]
    return {"task_count": len(tasks), "student_count": len({row["student_id"] for row in tasks}),
            "attempt_count": len(attempts), "graded_count": len(graded),
            "first_attempt_count": len(first), "first_graded_count": len(first_graded),
            "average_score": sum(row["score_units"] for row in graded) / len(graded) / 100 if graded else None,
            "average_score_rate": sum(ratios) / len(ratios) if ratios else None,
            "first_average_score": sum(row["score_units"] for row in first_graded) / len(first_graded) / 100 if first_graded else None,
            "last_used_at": max(used) if used else None}


def list_question_library(learning, course_id, actor_id, *, q=None, question_type=None, status=None,
                          knowledge_point=None, difficulty=None, class_id=None, limit=100, offset=0) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError("invalid question pagination")
    if question_type is not None and question_type not in _TYPES:
        raise ValueError("unsupported question type")
    if status is not None and status not in _STATUSES:
        raise ValueError("unsupported question status")
    if difficulty is not None and difficulty not in _DIFFICULTIES:
        raise ValueError("unsupported difficulty")
    if q is not None and (not isinstance(q, str) or len(q) > 200):
        raise ValueError("search text exceeds limits")
    if knowledge_point is not None and (not isinstance(knowledge_point, str) or len(knowledge_point) > 200):
        raise ValueError("knowledge point exceeds limits")
    if learning._access_checker:
        learning._require_access(actor_id, {"kind": "question", "course_id": course_id, "class_id": class_id})
    with learning._using_connection() as db:
        # Latest first, THEN status/type filters: an older approved version cannot reappear after retirement/editing.
        rows = db.execute("""SELECT q.* FROM question_versions q WHERE q.course_id=?
            AND NOT EXISTS(SELECT 1 FROM question_versions newer WHERE newer.question_id=q.question_id AND newer.version>q.version)
            ORDER BY q.created_at DESC,q.question_id""", (course_id,)).fetchall()
    visible = []
    search = (q or "").strip().casefold()
    for row in rows:
        item = _question_item(row)
        content = item["content"]
        if not _can_read_question(learning, actor_id, item):
            continue
        if class_id is not None and content.get("class_id") not in (None, class_id):
            continue
        haystack = " ".join([item["question_id"], str(content.get("stem", "")), str(content.get("explanation", "")),
                             *map(str, content.get("options", [])), *map(str, content.get("knowledge_points", []))]).casefold()
        if search and search not in haystack:
            continue
        visible.append(item)
    facets = {}
    for name, values in (("types", [row["content"].get("type") for row in visible]),
                         ("statuses", [row["status"] for row in visible]),
                         ("difficulties", [row["content"].get("difficulty") for row in visible]),
                         ("knowledge_points", [value for row in visible for value in set(row["content"].get("knowledge_points", []))])):
        facets[name] = [{"value": value, "count": count} for value, count in sorted(Counter(value for value in values if value).items())]
    matches = [item for item in visible if (question_type is None or item["content"].get("type") == question_type)
               and (status is None or item["status"] == status)
               and (difficulty is None or item["content"].get("difficulty") == difficulty)
               and (knowledge_point is None or knowledge_point in item["content"].get("knowledge_points", []))]
    selected = matches[offset:offset + limit]
    for item in selected:
        item["usage"] = question_usage(learning, item["question_id"], item["version"], actor_id)
    return {"items": selected, "total": len(matches), "limit": limit, "offset": offset, "facets": facets}


def question_detail(learning, course_id, question_id, version, actor_id) -> dict[str, Any]:
    with learning._using_connection() as db:
        item = _question_item(_question_row(learning, db, course_id, question_id, version, actor_id))
        versions = []
        for row in db.execute("SELECT * FROM question_versions WHERE course_id=? AND question_id=? ORDER BY version DESC", (course_id, question_id)):
            history = _question_item(row)
            if _can_read_question(learning, actor_id, history):
                versions.append({key: history[key] for key in ("question_id", "version", "status", "created_at")} | {
                    "type": history["content"].get("type"), "difficulty": history["content"].get("difficulty")})
    usage = question_usage(learning, question_id, version, actor_id)
    item["usage"] = usage
    return {"question": item, "sources": item["content"].get("source_anchors", []),
            "reviews": item["content"].get("review_evidence", {}), "versions": versions, "usage": usage}


def question_job_view(runtime, job) -> dict[str, Any]:
    from .access import job_view
    queue_id = f"content:{job['id']}"
    queued = None
    queue = getattr(runtime, "queue", None)
    if queue is not None:
        try:
            queued = queue.get_job(queue_id)
        except LookupError:
            pass
    result = {**job, "queue_job_id": queue_id, "queue_status": queued.status if queued else "not_dispatched",
              "queue": job_view(queued) if queued else None, "can_cancel": False, "can_resume": False,
              "requires_paid_confirmation": False}
    if queued:
        result["can_cancel"] = queued.status not in {"succeeded", "failed", "cancelled"} and not queued.cancel_requested
        result["can_resume"] = (queued.status in {"failed", "awaiting_confirmation"} and not queued.cancel_requested
                                and not queued.reconciliation_required and not getattr(queued, "cost_pending", False)
                                and job["status"] in {"pending", "running"})
        result["requires_paid_confirmation"] = bool(queued.paid and result["can_resume"])
    return result


def _loads(value: str):
    def invalid_constant(_):
        raise ValueError("non-finite JSON number is not supported")
    return json.loads(value, parse_constant=invalid_constant)


def _table_question(row: Mapping[str, Any]) -> dict[str, Any]:
    question = {key: value for key, value in row.items() if value not in (None, "")}
    for key in ("options", "answer", "rubric", "knowledge_points"):
        if key in question:
            if not isinstance(question[key], str):
                raise ValueError(f"{key} must use JSON text in the template cell")
            question[key] = _loads(question[key])
    if "points" in question:
        if isinstance(question["points"], bool):
            raise ValueError("points must be numeric")
        question["points"] = float(question["points"])
    return question


def parse_question_import(filename: str, data: bytes) -> list[dict[str, Any]]:
    """Bounded JSON/CSV/fixed XLSX parsing. No formulas, external links or model calls."""
    suffix = Path(filename).suffix.lower()
    # Reuse the strict filename rules without broadening the material parser's accepted formats.
    validate_filename(filename[:-5] + ".txt" if suffix == ".json" else filename)
    if suffix not in {".csv", ".json", ".xlsx"}:
        raise MaterialParseError("unsupported_format", "题目导入仅支持 CSV、JSON 和固定模板 XLSX")
    if not data or len(data) > IMPORT_MAX_BYTES:
        raise MaterialParseError("file_too_large" if data else "empty_file", "题目文件为空或超过 2 MiB")
    if suffix == ".json":
        try:
            value = _loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise MaterialParseError("invalid_json", "JSON 格式无效") from error
        if isinstance(value, dict) and set(value) <= {"questions", "template_version"}:
            value = value.get("questions")
        if not isinstance(value, list) or not 1 <= len(value) <= IMPORT_MAX_ROWS:
            raise MaterialParseError("invalid_rows", "JSON 必须是包含 1 到 100 题的数组或 questions 对象")
        return [{"row_number": index, "question": item} for index, item in enumerate(value, 1)]
    if suffix == ".csv":
        try:
            reader = csv.reader(StringIO(data.decode("utf-8-sig")), strict=True)
            table = []
            for row in reader:
                table.append(row)
                if len(table) > IMPORT_MAX_ROWS + 1:
                    raise MaterialParseError("row_limit", "每次最多导入 100 道题")
        except (UnicodeError, csv.Error) as error:
            raise MaterialParseError("invalid_csv", "CSV 必须是 UTF-8 编码且符合模板") from error
    else:
        limits = ParseLimits(max_bytes=IMPORT_MAX_BYTES, max_expanded_bytes=8 * 1024 * 1024,
                            max_member_bytes=4 * 1024 * 1024, max_rows=IMPORT_MAX_ROWS + 1,
                            max_columns=len(IMPORT_COLUMNS), max_cells=(IMPORT_MAX_ROWS + 1) * len(IMPORT_COLUMNS), max_pages=1)
        parsed = MaterialParser(limits).parse(filename, data)
        if parsed["issues"]:
            raise MaterialParseError("invalid_template", "Excel 必须使用无公式、无隐藏内容的固定模板")
        if len(parsed["blocks"]) != 1 or parsed["blocks"][0]["data"].get("sheet") != "questions":
            raise MaterialParseError("invalid_template", "Excel 必须只有名为 questions 的模板工作表")
        table = parsed["blocks"][0]["data"].get("rows", [])
    if not table or tuple(str(value or "").strip() for value in table[0]) != IMPORT_COLUMNS:
        raise MaterialParseError("invalid_template", "表头必须与下载模板完全一致")
    result = []
    for number, values in enumerate(table[1:], 2):
        if all(value in (None, "") for value in values):
            continue
        item: dict[str, Any] = {"row_number": number}
        try:
            if len(values) != len(IMPORT_COLUMNS):
                raise ValueError("列数必须与模板一致")
            item["question"] = _table_question(dict(zip(IMPORT_COLUMNS, values)))
        except (ValueError, TypeError, RecursionError) as error:
            item["error"] = str(error)[:500]
        result.append(item)
    if not result:
        raise MaterialParseError("empty_file", "文件中没有题目")
    return result


def import_preview(content, learning, *, course_id, owner_id, source_block_ids, filename, data, class_id=None):
    # Preview never authorizes external processing. Source rights and visibility are checked separately by the route.
    sources = content.confirmed_sources(course_id, source_block_ids, purpose="generation", class_id=class_id)
    if not 1 <= len(sources) <= 8:
        raise ValueError("select one to eight sources")
    rows, exact_seen, family_seen = [], set(), set()
    for entry in parse_question_import(filename, data):
        row = {"row_number": entry["row_number"], "status": "invalid", "errors": [], "question": None, "duplicate": None}
        try:
            if entry.get("error"):
                raise ValueError(entry["error"])
            question = validate_course_question(content, course_id, entry.get("question"))
            row["question"] = question
            exact, family = question_fingerprints(question)
            duplicate = learning.find_question_duplicate(course_id, exact, family)
            if exact in exact_seen or family in family_seen:
                row.update(status="duplicate", duplicate={"scope": "file"})
            elif duplicate:
                # Do not reveal protected question bodies or another class's identifiers.
                row.update(status="duplicate", duplicate={"scope": "library"})
            else:
                row["status"] = "valid"
            exact_seen.add(exact)
            family_seen.add(family)
        except (ValueError, TypeError, KeyError) as error:
            row["errors"].append(str(error)[:500])
        rows.append(row)
    counts = Counter(row["status"] for row in rows)
    payload = {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "source_block_ids": list(source_block_ids)}
    preview = {"rows": rows, "summary": {"total": len(rows), "valid": counts["valid"], "invalid": counts["invalid"], "duplicate": counts["duplicate"]}}
    return content.create_question_import_preview(course_id=course_id, owner_id=owner_id, class_id=class_id, payload=payload, preview=preview)


def preview_view(job):
    return {"import_id": job["id"], "course_id": job["course_id"], "class_id": job["class_id"],
            "status": "confirmed" if job["stage"] == "import_confirmed" else "preview",
            "filename": job["input"]["filename"], "source_block_ids": job["input"]["source_block_ids"],
            **job["state"], "external_processing_allowed": False, "committed": job["stage"] == "import_confirmed"}


def confirm_import(content, learning, job, *, external_processing_allowed, row_numbers=None):
    if external_processing_allowed is not True:
        raise PermissionError("explicit external processing permission is required for independent question review")
    rows = job["state"]["rows"]
    selected = row_numbers if row_numbers is not None else [row["row_number"] for row in rows if row["status"] == "valid"]
    if (not isinstance(selected, list) or not selected or len(selected) > IMPORT_MAX_ROWS
            or any(type(number) is not int for number in selected) or len(set(selected)) != len(selected)):
        raise ValueError("select unique valid row numbers")
    by_number = {row["row_number"]: row for row in rows}
    if any(number not in by_number for number in selected):
        raise ValueError("row number is outside this preview")
    if job["stage"] == "import_confirmed":
        if sorted(selected) != job["state"]["confirmation"]["row_numbers"]:
            raise ContentConflict("import was confirmed with different selected rows")
        return job
    bank = QuestionBank(content, learning)
    candidates, failures = [], []
    for number in selected:
        row = by_number[number]
        try:
            if row["status"] != "valid":
                raise QuestionValidationError("only locally valid, nonduplicate rows may enter review")
            payload = bank.candidate_payload(course_id=job["course_id"], owner_id=job["owner_id"], class_id=job["class_id"],
                question=row["question"], source_block_ids=job["input"]["source_block_ids"], external_processing_allowed=True,
                provenance={"origin": "import", "import_id": job["id"], "filename": job["input"]["filename"],
                            "sha256": job["input"]["sha256"], "row_number": number})
            candidates.append({"row_number": number, "payload": payload})
        except (ValueError, TypeError, PermissionError) as error:
            failures.append({"row_number": number, "error": str(error)[:500]})
    return content.confirm_question_import(job["id"], expected_revision=job["revision"], row_numbers=selected,
                                           candidates=candidates, failures=failures)


def question_import_template(format: str) -> tuple[bytes, str, str]:
    samples = [
        {"type": "single_choice", "stem": "示例：二加三等于多少？", "options": ["四", "五"], "answer": 1,
         "explanation": "二加三等于五。", "points": 1, "rubric": [], "difficulty": "easy", "knowledge_points": []},
        {"type": "judge", "stem": "示例：零是偶数。", "options": [], "answer": True,
         "explanation": "零可以被二整除。", "points": 1, "rubric": [], "difficulty": "easy", "knowledge_points": []},
        {"type": "fill", "stem": "示例：中华人民共和国的首都是____。", "options": [], "answer": ["北京", "北京市"],
         "explanation": "数组为单个空的可接受答案。", "points": 1, "rubric": [], "difficulty": "easy", "knowledge_points": [], "answer_format": "text"},
        {"type": "text", "stem": "示例：解释为什么零是偶数。", "options": [], "answer": "因为零是二的整数倍。",
         "explanation": "依据偶数的定义。", "points": 2, "rubric": [{"point": "指出零等于二乘零，并联系偶数定义", "score_units": 200}],
         "difficulty": "medium", "knowledge_points": [], "answer_format": "text"},
    ]
    if format == "json":
        return json.dumps({"template_version": 1, "questions": samples}, ensure_ascii=False, indent=2).encode(), "application/json", "question-template.json"
    records = []
    for sample in samples:
        records.append([json.dumps(sample.get(key, []), ensure_ascii=False) if key in {"options", "answer", "rubric", "knowledge_points"}
                        else sample.get(key, "") for key in IMPORT_COLUMNS])
    if format == "csv":
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(IMPORT_COLUMNS)
        writer.writerows(records)
        return stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "question-template.csv"
    if format == "xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "questions"
        sheet.append(IMPORT_COLUMNS)
        for values in records:
            sheet.append(values)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        return stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "question-template.xlsx"
    raise ValueError("template format must be csv, json or xlsx")
