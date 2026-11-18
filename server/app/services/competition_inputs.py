"""Bounded organizer tables. Imports never enter the live learning repositories."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Mapping
from zipfile import BadZipFile, ZipFile

SCHEMA_VERSION = "competition.organizer.v1"
TABLES = {
    "课程要求": ("主题编号", "主题", "主题目标", "指标编号", "观察指标", "课标整理编号", "课标文件页／正文页", "教材正文页"),
    "题目与材料": ("材料编号", "主题编号", "材料类型", "题型", "题干或内容", "选项", "参考答案", "参考解析", "满分", "评分点", "指标编号", "来源或资料文件"),
    "模拟学生": ("学生编号", "模拟班级编号", "年级"),
    "测验节点": ("节点编号", "模拟测验日期", "已教及测量主题", "说明", "可用题目编号"),
    "作答记录": ("记录编号", "学生编号", "材料编号", "主题编号", "节点编号", "作答事件编号", "修订版本", "模拟提交时间", "评分状态", "得分", "满分", "学生最终作答", "解答过程", "评分点得分"),
    "字段说明": ("数据表", "字段名", "类型", "含义", "取值或格式", "示例", "为空时"),
    "数据说明": ("项目", "说明"),
}
REQUIRED_TABLES = tuple(name for name in TABLES if name != "数据说明")
LIMITS = {"max_files": 8, "max_file_bytes": 12 * 1024 * 1024, "max_total_bytes": 24 * 1024 * 1024,
          "max_rows_per_table": 30000, "max_total_rows": 50000, "max_cell_characters": 16000,
          "max_xlsx_uncompressed_bytes": 48 * 1024 * 1024, "max_xlsx_entries": 256}
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_IDENT = re.compile(r"[^\s\x00-\x1f]{1,120}\Z")


class CompetitionInputError(ValueError):
    """A bounded, public error without any uploaded text or filesystem path."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def split_ids(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[、；;,，\n]+", value) if part.strip()]


def integer(value: str) -> int | None:
    text = value.strip()
    return int(text) if len(text) <= 12 and _INTEGER.fullmatch(text) else None


def material_is_usable(row: Mapping[str, Any]) -> bool:
    """Unmapped textbooks may be confirmed by an operator; invalid definitions may not."""
    return not (set(row.get("anomalies", [])) - {"unmapped_textbook"})


def schema_description() -> dict:
    return {"schema_version": SCHEMA_VERSION, "tables": [
        {"name": name, "filename": name + ".csv", "required": name in REQUIRED_TABLES, "columns": list(columns)}
        for name, columns in TABLES.items()], "limits": LIMITS,
        "encodings": ["utf-8", "utf-8-sig", "gb18030"],
        "notes": ["列名必须与学情数据格式一致，允许改变列顺序；所有原值按文本保留。",
                  "接受六张CSV或包含对应工作表的XLSX；数据说明是索引特有的可选工作表，也可粘贴为数据说明.csv。",
                  "同一事件取最大整数修订；最新修订异常或待评分不会回退为旧分数。",
                  "缺失不计作零；帮助与独立完成情况未采集；得分比例不是掌握度。"]}


def _safe_filename(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 180 or any(c in name for c in "/\\:\x00") or _CONTROL.search(name):
        raise CompetitionInputError("文件名必须为不含路径的CSV或XLSX名称")
    if not name.lower().endswith((".csv", ".xlsx")):
        raise CompetitionInputError("只接受学情数据CSV或XLSX索引")
    return name


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, datetime, date)):
        raise CompetitionInputError("单元格类型不符合学情数据表结构")
    text = value.isoformat() if isinstance(value, (datetime, date)) else str(value)
    if len(text) > LIMITS["max_cell_characters"] or _CONTROL.search(text):
        raise CompetitionInputError("单元格过长或含不支持的控制字符")
    return text


def _read_rows(name: str, values, *, source: dict, issues: list[dict]) -> list[dict]:
    iterator = iter(values)
    try:
        headers = [_cell(v) for v in next(iterator)]
    except StopIteration as error:
        raise CompetitionInputError("数据表缺少表头") from error
    if len(headers) != len(set(headers)) or set(headers) != set(TABLES[name]):
        raise CompetitionInputError("数据表列名不符合已知学情数据模式；请查看字段模板")
    rows = []
    for line, values in enumerate(iterator, 2):
        if line > LIMITS["max_rows_per_table"] + 1:
            raise CompetitionInputError("数据表行数超过导入上限")
        cells = [_cell(value) for value in values]
        if not any(cells):
            continue
        if len(cells) != len(headers):
            raise CompetitionInputError("CSV或工作表的行宽与表头不一致")
        original = dict(zip(headers, cells))
        ref = {"source_id": source["id"], "file_sha256": source["sha256"], "table": name,
               "row": line, "record_id": original.get("记录编号") or None}
        rows.append({"original": original, "ref": ref})
    return rows


def parse_organizer_inputs(files: Mapping[str, bytes]) -> dict:
    """Read only allowlisted tables; no workbook formula, macro, link or ZIP extraction."""
    if not files or len(files) > LIMITS["max_files"]:
        raise CompetitionInputError("请选择学情数据表格，文件数量超过上限")
    if sum(len(value) for value in files.values()) > LIMITS["max_total_bytes"]:
        raise CompetitionInputError("输入总大小超过上限")
    tables: dict[str, list] = {}
    sources, originals, issues = [], [], []
    for filename, content in files.items():
        _safe_filename(filename)
        if not isinstance(content, bytes) or not content or len(content) > LIMITS["max_file_bytes"]:
            raise CompetitionInputError("文件为空或超过大小上限")
        sha = hashlib.sha256(content).hexdigest()
        source = {"id": "src-" + sha[:24], "filename": filename, "sha256": sha, "bytes": len(content)}
        sources.append(source)
        originals.append({**source, "content_base64": base64.b64encode(content).decode("ascii")})
        incoming = {}
        if filename.lower().endswith(".csv"):
            name = filename[:-4]
            if name not in TABLES:
                raise CompetitionInputError("CSV名称必须使用学情数据数据表名称")
            for encoding in ("utf-8-sig", "gb18030"):
                try:
                    text = content.decode(encoding)
                    source["encoding"] = encoding
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise CompetitionInputError("CSV编码应为UTF-8或GB18030")
            try:
                incoming[name] = _read_rows(name, csv.reader(io.StringIO(text, newline=""), strict=True), source=source, issues=issues)
            except (csv.Error, UnicodeError) as error:
                raise CompetitionInputError("CSV格式不完整或字段过长") from error
        else:
            incoming = _read_workbook(content, source, issues)
        for name, rows in incoming.items():
            if name in tables:
                # A CSV and its workbook sheet must agree, never silently choose one.
                if [r["original"] for r in tables[name]] != [r["original"] for r in rows]:
                    raise CompetitionInputError("多个输入中的同名数据表内容不同，请明确选择一个版本")
                issues.append({"code": "identical_table_copy", "severity": "info", "table": name,
                               "message": "重复表内容一致，统计只使用一份；所有输入文件仍保留。", "refs": [r["ref"] for r in rows[:1]]})
            else:
                tables[name] = rows
        if sum(map(len, tables.values())) > LIMITS["max_total_rows"]:
            raise CompetitionInputError("输入总行数超过上限")
    if set(REQUIRED_TABLES) - tables.keys():
        raise CompetitionInputError("缺少必要学情数据表格，请提供六张CSV或完整索引XLSX")
    if not all(tables[name] for name in REQUIRED_TABLES if name != "作答记录"):
        raise CompetitionInputError("课程、材料、学生、节点和字段说明表不可为空")
    if not tables.get("数据说明"):
        issues.append({"code": "index_notes_absent", "severity": "warning", "table": "数据说明", "refs": [],
                       "message": "未提供索引特有备注；请查阅原索引确认评分点依赖和教材映射说明，必要时补充数据说明.csv。"})
    parsed = {"schema_version": SCHEMA_VERSION, "sources": sources, "original_files": originals,
              "tables": tables, "issues": issues}
    parsed["input_sha256"] = digest(sorted((s["filename"], s["sha256"]) for s in sources))
    _validate(parsed)
    return parsed


def _read_workbook(content: bytes, source: dict, issues: list[dict]) -> dict:
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > LIMITS["max_xlsx_entries"] or sum(e.file_size for e in entries) > LIMITS["max_xlsx_uncompressed_bytes"]:
                raise CompetitionInputError("工作簿解压大小超过上限")
            if len({e.filename for e in entries}) != len(entries) or any(
                e.flag_bits & 1 or e.filename.startswith(("/", "\\")) or ".." in e.filename.split("/")
                or "vbaProject" in e.filename or "/externalLinks/" in e.filename or "/embeddings/" in e.filename for e in entries
            ):
                raise CompetitionInputError("工作簿含不支持的宏、外部链接或嵌入内容")
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            if len(workbook.worksheets) > len(TABLES) or any(sheet.title not in TABLES for sheet in workbook):
                raise CompetitionInputError("工作簿仅支持学情数据已知工作表")
            result = {}
            for sheet in workbook:
                if sheet.max_row is not None and sheet.max_row > LIMITS["max_rows_per_table"] + 1:
                    raise CompetitionInputError("工作表行数超过上限")
                if sheet.max_column is not None and sheet.max_column > len(TABLES[sheet.title]):
                    raise CompetitionInputError("工作表列数不符合学情数据模式")
                sheet.reset_dimensions()
                def values():
                    for row in sheet.iter_rows():
                        if len(row) > len(TABLES[sheet.title]):
                            raise CompetitionInputError("工作表列数不符合学情数据模式")
                        if any(cell.data_type in {"f", "e"} for cell in row):
                            raise CompetitionInputError("工作簿公式或错误单元格不作为数据导入")
                        yield [cell.value for cell in row]
                result[sheet.title] = _read_rows(sheet.title, values(), source=source, issues=issues)
            return result
        finally:
            workbook.close()
    except CompetitionInputError:
        raise
    except Exception as error:
        raise CompetitionInputError("无法安全读取XLSX，请检查学情数据索引或改用CSV") from error


def _validate(parsed: dict) -> None:
    tables, issues = parsed["tables"], parsed["issues"]
    def issue(code, table, row, message, severity="warning"):
        item = {"code": code, "severity": severity, "table": table, "message": message,
                "refs": [row["ref"]] if row else []}
        issues.append(item)
        if row is not None:
            row.setdefault("anomalies", []).append(code)
    def unique(table, key):
        grouped = defaultdict(list)
        for row in tables[table]:
            grouped[row["original"][key].strip()].append(row)
        result = {}
        for identifier, rows in grouped.items():
            for row in rows:
                row["id"] = identifier
                if not _IDENT.fullmatch(identifier):
                    issue("invalid_identifier", table, row, "编号为空、过长或含空白，排除关联。")
                elif len(rows) != 1:
                    issue("duplicate_identifier", table, row, "实体或记录编号重复，保守排除全部冲突行。")
                else:
                    result[identifier] = row
        return result
    requirements = unique("课程要求", "指标编号")
    students = unique("模拟学生", "学生编号")
    materials = unique("题目与材料", "材料编号")
    nodes = unique("测验节点", "节点编号")
    records = unique("作答记录", "记录编号")
    topics = {r["original"]["主题编号"].strip() for r in requirements.values()}
    topic_definitions = defaultdict(set)
    for row in requirements.values():
        o = row["original"]
        row.update(topic_id=o["主题编号"].strip(), indicator_id=o["指标编号"].strip())
        if not _IDENT.fullmatch(row["topic_id"]):
            issue("invalid_topic", "课程要求", row, "主题编号不合法。")
        topic_definitions[row["topic_id"]].add((o["主题"], o["主题目标"]))
    for row in requirements.values():
        if len(topic_definitions[row["topic_id"]]) > 1:
            issue("conflicting_topic_definition", "课程要求", row, "同主题的名称或目标不一致，相关映射待核实。")
    for row in students.values():
        o = row["original"]
        row.update(student_id=row["id"], class_id=o["模拟班级编号"].strip(), grade=integer(o["年级"]))
        if not _IDENT.fullmatch(row["class_id"]) or row["grade"] is None or row["grade"] < 1:
            issue("invalid_student_metadata", "模拟学生", row, "班级或年级不合法，排除该学生关联。")
    for row in materials.values():
        o = row["original"]
        row.update(material_id=row["id"], topic_id=o["主题编号"].strip() or None,
                   indicator_ids=split_ids(o["指标编号"]), kind=o["材料类型"].strip(), max_score=integer(o["满分"]), rubric=[])
        if row["kind"] not in {"题目", "学习卡", "教材"}:
            issue("unknown_material_type", "题目与材料", row, "材料类型不在学情数据取值内。")
        if row["topic_id"] and row["topic_id"] not in topics:
            issue("unknown_material_topic", "题目与材料", row, "材料主题不存在。")
        if row["kind"] != "教材" and (not row["topic_id"] or not row["indicator_ids"]):
            issue("missing_material_mapping", "题目与材料", row, "题目或学习卡缺少主题或指标。")
        if any(i not in requirements or requirements[i]["topic_id"] != row["topic_id"] or requirements[i].get("anomalies") for i in row["indicator_ids"]):
            issue("invalid_indicator_reference", "题目与材料", row, "材料指标不存在、主题冲突或要求定义异常。")
        if row["kind"] == "教材" and not row["indicator_ids"]:
            issue("unmapped_textbook", "题目与材料", row, "教材未标注对应指标；只列为待核实候选，不自动声称关联。", "info")
        if row["kind"] == "题目":
            if row["max_score"] is None or row["max_score"] <= 0 or len(row["indicator_ids"]) != 1:
                issue("invalid_question_metadata", "题目与材料", row, "题目满分须为正整数且对应一个指标。")
            for line in o["评分点"].splitlines():
                match = re.fullmatch(r"\s*(\S+)\s+(.+?)\s+(\d+)分\s*", line)
                if not match:
                    issue("invalid_rubric_definition", "题目与材料", row, "评分点不符合编号、内容、整数分值格式。")
                    continue
                pid, label, maximum = match.groups()
                if not pid.startswith(row["id"] + "P") or integer(maximum) is None:
                    issue("invalid_rubric_definition", "题目与材料", row, "评分点编号与题目不一致。")
                row["rubric"].append({"id": pid, "label": label, "max_score": int(maximum)})
            if not row["rubric"] or len({p["id"] for p in row["rubric"]}) != len(row["rubric"]) or sum(p["max_score"] for p in row["rubric"]) != row["max_score"]:
                issue("rubric_total_mismatch", "题目与材料", row, "评分点重复、缺失或合计与满分不一致。")
    questions = {k: v for k, v in materials.items() if v["kind"] == "题目"}
    for row in nodes.values():
        o = row["original"]
        row.update(node_id=row["id"], topic_ids=split_ids(o["已教及测量主题"]), question_ids=split_ids(o["可用题目编号"]))
        try:
            date.fromisoformat(o["模拟测验日期"])
        except ValueError:
            issue("invalid_node_date", "测验节点", row, "测验日期不是YYYY-MM-DD。")
        if not row["topic_ids"] or len(set(row["topic_ids"])) != len(row["topic_ids"]) or any(t not in topics for t in row["topic_ids"]):
            issue("invalid_node_topics", "测验节点", row, "节点主题缺失、重复或不存在。")
        if not row["question_ids"] or len(set(row["question_ids"])) != len(row["question_ids"]) or any(q not in questions or questions[q].get("anomalies") or questions[q]["topic_id"] not in row["topic_ids"] for q in row["question_ids"]):
            issue("invalid_node_questions", "测验节点", row, "节点题集缺失、重复或存在题目、主题、评分定义异常。")
    dictionary = {(r["original"]["数据表"].strip(), r["original"]["字段名"].strip()): r for r in tables["字段说明"]}
    expected = {(name, field) for name in REQUIRED_TABLES if name != "字段说明" for field in TABLES[name]}
    if not expected.issubset(dictionary):
        issue("incomplete_field_dictionary", "字段说明", None, "字段说明缺少必需字段；按固定学情数据模式解析，缺项已保留。")
    if len(dictionary) != len(tables["字段说明"]):
        issue("duplicate_field_dictionary", "字段说明", None, "字段字典存在重复条目；原文保留供核查。")
    for row in tables["作答记录"]:
        o = row["original"]
        row.update(record_id=o["记录编号"].strip(), student_id=o["学生编号"].strip(), question_id=o["材料编号"].strip(),
                   topic_id=o["主题编号"].strip(), node_id=o["节点编号"].strip(), event_id=o["作答事件编号"].strip(),
                   revision=integer(o["修订版本"]), score=integer(o["得分"]), max_score=integer(o["满分"]),
                   status=o["评分状态"].strip(), rubric_scores={}, selection="excluded", quantitative_eligible=False)
        if row["revision"] is None or row["revision"] < 1:
            issue("invalid_revision", "作答记录", row, "修订版本须为从1开始的整数，相关事件不确定。")
        expected_event = f'{row["student_id"]}-{row["node_id"]}-{row["question_id"]}'
        if row["event_id"] != expected_event:
            issue("event_identity_mismatch", "作答记录", row, "作答事件编号与学生、节点、题目不一致。")
        if row["student_id"] not in students or students.get(row["student_id"], {}).get("anomalies"):
            issue("unknown_student", "作答记录", row, "学生关联不存在或冲突。")
        question, node = questions.get(row["question_id"]), nodes.get(row["node_id"])
        if question is None or question.get("anomalies"):
            issue("unknown_or_invalid_question", "作答记录", row, "题目不存在或定义存在异常。")
        if node is None or node.get("anomalies"):
            issue("unknown_or_invalid_node", "作答记录", row, "测验节点不存在或定义存在异常。")
        if question and row["topic_id"] != question["topic_id"]:
            issue("record_topic_mismatch", "作答记录", row, "作答主题与题目主题不一致。")
        if node and (row["question_id"] not in node["question_ids"] or row["topic_id"] not in node["topic_ids"]):
            issue("outside_node_scope", "作答记录", row, "题目或主题不在该节点已教测量范围。")
        try:
            when = datetime.fromisoformat(o["模拟提交时间"])
            if when.utcoffset() is None:
                raise ValueError
            row["timestamp"] = when.isoformat()
        except ValueError:
            issue("invalid_submission_time", "作答记录", row, "提交时间须为带时区的ISO8601。")
        if question and row["max_score"] != question["max_score"]:
            issue("record_max_mismatch", "作答记录", row, "作答满分与题目定义不一致。")
        if row["status"] not in {"已评分", "待评分", "未作答"}:
            issue("unknown_score_status", "作答记录", row, "评分状态不是已评分、待评分或未作答。")
        if row["status"] == "已评分":
            if row["score"] is None or row["max_score"] is None or not 0 <= row["score"] <= row["max_score"]:
                issue("invalid_score", "作答记录", row, "已评分记录得分缺失、非整数或超出满分。")
            if not o["学生最终作答"].strip():
                issue("scored_answer_absent", "作答记录", row, "已评分记录缺少最终作答，暂不作为量化证据。")
            if question and question["original"]["题型"] == "解答题" and not o["解答过程"].strip():
                issue("solution_process_absent", "作答记录", row, "解答题缺少步骤，暂不作为量化证据。")
            parts = split_ids(o["评分点得分"])
            for part in parts:
                match = re.fullmatch(r"([^=\s]+)\s*=\s*(\d+)", part)
                if not match or integer(match.group(2)) is None:
                    issue("invalid_rubric_scores", "作答记录", row, "评分点得分格式不合法。")
                    continue
                pid, points = match.groups()
                if pid in row["rubric_scores"]:
                    issue("duplicate_rubric_score", "作答记录", row, "同一评分点得分重复。")
                row["rubric_scores"][pid] = int(points)
            definitions = {p["id"]: p["max_score"] for p in question["rubric"]} if question else {}
            if set(row["rubric_scores"]) != set(definitions) or any(v > definitions.get(k, -1) for k, v in row["rubric_scores"].items()) or sum(row["rubric_scores"].values()) != row["score"]:
                issue("rubric_score_mismatch", "作答记录", row, "评分点缺失、越界、未知或合计与得分不一致。")
        elif (o["得分"].strip() or o["评分点得分"].strip()
              or (row["status"] == "未作答" and (o["学生最终作答"].strip() or o["解答过程"].strip()))):
            issue("score_status_conflict", "作答记录", row, "未评分状态与分数或作答内容矛盾；空值保持为空。")
    events = defaultdict(list)
    tuples = defaultdict(set)
    for row in tables["作答记录"]:
        events[row["event_id"]].append(row)
        tuples[(row["student_id"], row["node_id"], row["question_id"])].add(row["event_id"])
    for event_id, rows in events.items():
        # Invalid latest revisions never cause fallback to an earlier high score.
        identity = {(r["student_id"], r["node_id"], r["question_id"]) for r in rows}
        ambiguous = len(identity) != 1 or any(r["revision"] is None or r["revision"] < 1 or r["record_id"] not in records or "event_identity_mismatch" in r.get("anomalies", []) for r in rows)
        if any(len(tuples[key]) != 1 for key in identity):
            ambiguous = True
        revisions = Counter(r["revision"] for r in rows)
        if any(count > 1 for count in revisions.values()):
            ambiguous = True
        if ambiguous:
            for row in rows:
                issue("ambiguous_event", "作答记录", row, "事件身份、记录编号或修订版本冲突，整个事件排除。")
            continue
        latest = max(rows, key=lambda r: r["revision"])
        for row in rows:
            row["selection"] = "current" if row is latest else "superseded"
        versions = sorted(revisions)
        if versions != list(range(1, versions[-1] + 1)):
            issue("revision_gap", "作答记录", latest, "修订历史不完整，当前修订仍保留；历史变化不可完整推断。", "info")
        latest["quantitative_eligible"] = latest["status"] == "已评分" and not any(code != "revision_gap" for code in latest.get("anomalies", []))
    parsed["entities"] = {"requirements": list(requirements.values()), "students": [r for r in students.values() if not r.get("anomalies")],
                          "materials": list(materials.values()), "nodes": list(nodes.values())}
    parsed["issues_summary"] = {"total": len(issues), "by_code": dict(sorted(Counter(i["code"] for i in issues).items())),
                                "by_severity": dict(Counter(i["severity"] for i in issues))}


def input_preview(parsed: dict) -> dict:
    e = parsed["entities"]
    records = parsed["tables"]["作答记录"]
    return {"schema_version": SCHEMA_VERSION, "input_sha256": parsed["input_sha256"],
            "summary": {"table_rows": {k: len(v) for k, v in parsed["tables"].items()},
                        "students": len(e["students"]), "classes": len({s["class_id"] for s in e["students"]}),
                        "current_events": sum(r["selection"] == "current" for r in records),
                        "eligible_scored_events": sum(r["quantitative_eligible"] for r in records),
                        "superseded_records": sum(r["selection"] == "superseded" for r in records),
                        "excluded_records": sum(r["selection"] == "excluded" or (r["selection"] == "current" and not r["quantitative_eligible"]) for r in records),
                        "current_non_scored_records": sum(r["selection"] == "current" and not r["quantitative_eligible"] for r in records)},
            "sources": parsed["sources"], "issues_summary": parsed["issues_summary"], "issues": parsed["issues"],
            "notes": [r["original"] for r in parsed["tables"].get("数据说明", [])],
            "field_dictionary": [r["original"] for r in parsed["tables"]["字段说明"]],
            "auto_suggestions": parsed.get("auto_suggestions", []),
            "options": {"nodes": [{"id": n["node_id"], "date": n["original"]["模拟测验日期"], "description": n["original"]["说明"],
                                    "topic_ids": n["topic_ids"], "question_ids": n["question_ids"], "selectable": not bool(n.get("anomalies"))} for n in e["nodes"]],
                        "classes": [{"id": c, "student_count": sum(s["class_id"] == c for s in e["students"])} for c in sorted({s["class_id"] for s in e["students"]})],
                        "students": [{"id": s["student_id"], "class_id": s["class_id"], "grade": s["grade"]} for s in e["students"]],
                        "indicators": [{"id": r["indicator_id"], "topic_id": r["topic_id"], "label": r["original"]["观察指标"],
                                        "selectable": not bool(r.get("anomalies")), "source_ref": r["ref"]} for r in e["requirements"]],
                        "materials": [{"id": m["material_id"], "kind": m["kind"], "topic_id": m["topic_id"],
                                       "indicator_ids": m["indicator_ids"], "label": m["original"]["题干或内容"],
                                       "source": m["original"]["来源或资料文件"], "source_ref": m["ref"],
                                       "selectable": material_is_usable(m),
                                       "mapping_status": "source_mapped" if m["topic_id"] and m["indicator_ids"] else "pending_operator_mapping"}
                                      for m in e["materials"] if m["kind"] in {"学习卡", "教材"}],
                        "auto_suggestions": parsed.get("auto_suggestions", []),
                        "unmapped_textbooks": [{"id": m["material_id"], "label": m["original"]["题干或内容"], "source": m["original"]["来源或资料文件"],
                                               "topic_id": m["topic_id"], "source_ref": m["ref"], "selectable": material_is_usable(m)}
                                               for m in e["materials"] if m["kind"] == "教材" and not m["indicator_ids"]]},
            "rules": schema_description()["notes"]}
