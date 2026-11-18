"""Independent, immutable organizer-data analyses. Never read live submissions."""
from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import html
import io
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .competition_inputs import (canonical, digest, input_preview, material_is_usable,
                                 parse_organizer_inputs, schema_description)

RUN_SCHEMA_VERSION = "competition.run.v1"
EXPORT_SCHEMA_VERSION = "competition.export.v1"
FORMATS = {"json", "csv", "zip", "html"}
FORMULA_INDEPENDENCE = "助理使用情况和独立完成情况未采集；不推断。得分比例不是掌握度、能力结论或因果解释。"
SAFE_TOKEN = re.compile(r"[^\x00-\x20]{1,120}\Z")
MAX_GROUPS = 100
MAX_SELECTED_IDS = 5000
MAX_MAPPINGS = 1000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class CompetitionRunError(ValueError):
    pass


class CompetitionConflict(RuntimeError):
    pass


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _ratio(points: int, maximum: int) -> float | None:
    return round(points / maximum, 4) if maximum > 0 else None


def _record_ref(row: Mapping[str, Any]) -> dict:
    return {"record_id": row["record_id"], "event_id": row["event_id"], "revision": row["revision"],
            "source_id": row["ref"]["source_id"], "file_sha256": row["ref"]["file_sha256"],
            "table": row["ref"]["table"], "row": row["ref"]["row"]}


def _list(value, name, available, *, allow_empty=True):
    if not isinstance(value, list) or len(value) > MAX_SELECTED_IDS or (not allow_empty and not value):
        raise CompetitionRunError(f"{name} must be a bounded list")
    if any(not isinstance(item, str) or not SAFE_TOKEN.fullmatch(item) for item in value) or len(value) != len(set(value)):
        raise CompetitionRunError(f"{name} must contain unique non-empty loaded identifiers")
    if not set(value) <= available:
        raise CompetitionRunError(f"{name} contains identifiers not present in this run")
    return sorted(value)


def _rows_for_class(node_id, class_id, student_ids, parsed):
    students = {row["student_id"]: row for row in parsed["entities"]["students"]}
    return [r for r in parsed["tables"]["作答记录"]
            if r["selection"] == "current" and students.get(r["student_id"], {}).get("class_id") == class_id
            and (not student_ids or r["student_id"] in student_ids) and r["node_id"] == node_id]


def _attempt_aggregate(rows, questions):
    eligible = [r for r in rows if r["quantitative_eligible"]]
    maximum = sum(r["max_score"] for r in eligible)
    points = sum(r["score"] for r in eligible)
    return {"current_events": len(rows), "current_scored_events": len(eligible), "scored_questions": len({r["question_id"] for r in eligible}),
            "current_non_scored_events": sum(not r["quantitative_eligible"] for r in rows),
            "earned_points": points if eligible else None, "scored_max_points": maximum or None,
            "score_percentage": round(points / maximum * 100, 2) if maximum else None,
            "questions_available": len(questions), "expected_event_denominator": len(questions),
            "event_coverage_denominator": len(questions),
            "scored_question_coverage_denominator": len(questions),
            "event_coverage": round(len(rows) / len(questions), 4) if questions else None,
            "scored_question_coverage": round(len({r["question_id"] for r in eligible}) / len(questions), 4) if questions else None,
            "evidence_refs": [_record_ref(r) for r in sorted(rows, key=lambda r: (r["question_id"], r["record_id"]))]}


def _build_case_suggestions(parsed, node_ids, allowed_students=None):
    students = [student for student in parsed["entities"]["students"]
                if allowed_students is None or student["student_id"] in allowed_students]
    records = [r for r in parsed["tables"]["作答记录"] if r["node_id"] in node_ids and r["selection"] == "current"
               and (allowed_students is None or r["student_id"] in allowed_students)]
    by_student = defaultdict(list)
    for row in records:
        by_student[row["student_id"]].append(row)
    materials = {row["material_id"]: row for row in parsed["entities"]["materials"]}
    partial, secure, sparse = [], [], []
    for student in students:
        rows = by_student[student["student_id"]]
        eligible = [r for r in rows if r["quantitative_eligible"]]
        partial_rows = [r for r in eligible if 0 < r["score"] < r["max_score"]]
        has_pattern = any(r["rubric_scores"] and materials.get(r["question_id"], {}).get("rubric") for r in partial_rows)
        if partial_rows and has_pattern:
            partial.append((len(partial_rows), len(eligible), student, partial_rows))
        if len(eligible) >= 2 and all(r["score"] == r["max_score"] for r in eligible):
            secure.append((len(eligible), student, eligible))
        if not eligible:
            sparse.append((len(rows), student, rows))
    partial.sort(key=lambda x: (-x[0], -x[1], x[2]["student_id"]))
    secure.sort(key=lambda x: (-x[0], x[1]["student_id"]))
    sparse.sort(key=lambda x: (x[0], x[1]["student_id"]))
    chosen, used = [], set()
    for label, pool in (("解答题部分得分，观察步骤表现", partial), ("多题均得分满分，展示具体作答证据", secure), ("所选节点量化记录不足", sparse)):
        candidate = next((entry for entry in pool if entry[-2]["student_id"] not in used), None) if pool else None
        if candidate:
            student = candidate[-2]
            rows = candidate[-1]
            chosen.append({"student_id": student["student_id"], "class_id": student["class_id"], "role": label,
                           "automatic": True, "selection_reason": f"根据所选节点的当前有效记录确定；证据数={len(rows)}。",
                           "evidence_refs": [_record_ref(r) for r in rows]})
            used.add(student["student_id"])
    remaining = [s for s in students if s["student_id"] not in used]
    while len(chosen) < min(3, len(students)) and remaining:
        s = min(remaining, key=lambda row: (len(by_student[row["student_id"]]), row["student_id"]))
        rows = by_student[s["student_id"]]
        chosen.append({"student_id": s["student_id"], "class_id": s["class_id"], "role": "所选节点的代表性补充案例",
                       "automatic": True, "selection_reason": "按当前记录数升序、编号确定性排序；此排序不代表学业水平。",
                       "evidence_refs": [_record_ref(r) for r in rows]})
        used.add(s["student_id"])
        remaining = [row for row in remaining if row["student_id"] not in used]
    return chosen[:3]


def _material_reference(material, *, mapping="explicit_source_mapping"):
    return {"id": material["material_id"], "kind": material["kind"],
            "label": material["original"]["题干或内容"], "source": material["original"]["来源或资料文件"],
            "mapping": mapping, "source_ref": material["ref"]}


def _related_materials(materials, scope, topic_id, indicator_id):
    related = []
    for material in materials.values():
        if material["kind"] not in {"学习卡", "教材"} or not material_is_usable(material):
            continue
        selected = scope["material_mappings"].get(material["material_id"])
        # An explicit operator selection narrows this run's use of a material.
        mapping = selected or {"topic_id": material["topic_id"], "indicator_ids": material["indicator_ids"]}
        if mapping["topic_id"] == topic_id and indicator_id in mapping["indicator_ids"]:
            related.append(_material_reference(material, mapping="operator_selected" if selected else "explicit_source_mapping"))
    return sorted(related, key=lambda item: (item["kind"] == "教材", item["id"]))


def _individual_recommendations(parsed, scope, individual_rows):
    """Describe observed rubric evidence; propose bounded actions without diagnosing causes."""
    materials = {row["material_id"]: row for row in parsed["entities"]["materials"]}
    requirements = {row["indicator_id"]: row for row in parsed["entities"]["requirements"]}
    nodes = {row["node_id"]: row for row in parsed["entities"]["nodes"]}
    records = {(row["student_id"], row["node_id"], row["question_id"]): row
               for row in parsed["tables"]["作答记录"] if row["selection"] == "current"}
    representative_ids = set(scope["representative_student_ids"])
    suggestions = []
    for individual in individual_rows:
        if individual["student_id"] not in representative_ids:
            continue
        student_id, node_id, topic_id = (individual[key] for key in ("student_id", "node_id", "topic_id"))
        for metric in individual["indicators"]:
            indicator_id = metric["indicator_id"]
            question_ids = sorted(q for q in nodes[node_id]["question_ids"]
                                  if q in materials and material_is_usable(materials[q])
                                  and materials[q]["topic_id"] == topic_id and indicator_id in materials[q]["indicator_ids"])
            if not question_ids:
                continue
            current = [records[(student_id, node_id, q)] for q in question_ids if (student_id, node_id, q) in records]
            scored = [row for row in current if row["quantitative_eligible"]]
            rubric_observations = []
            for row in scored:
                for point in materials[row["question_id"]]["rubric"]:
                    rubric_observations.append({"question_id": row["question_id"], "rubric_id": point["id"],
                                                "label": point["label"], "earned_points": row["rubric_scores"][point["id"]],
                                                "max_score": point["max_score"], "evidence_ref": _record_ref(row)})
            shortfalls = [point for point in rubric_observations if point["earned_points"] < point["max_score"]]
            # Earliest unmet rubric point comes first, then deterministically the greatest missing points.
            shortfalls.sort(key=lambda point: (next(i for i, p in enumerate(materials[point["question_id"]]["rubric"])
                                                         if p["id"] == point["rubric_id"]),
                                               -(point["max_score"] - point["earned_points"]), point["question_id"]))
            focus_point = shortfalls[0] if shortfalls else (rubric_observations[0] if rubric_observations else None)
            focus_question = focus_point["question_id"] if focus_point else question_ids[0]
            focus_label = focus_point["label"] if focus_point else requirements[indicator_id]["original"]["观察指标"]
            references = _related_materials(materials, scope, topic_id, indicator_id)
            learning = list(references)
            seen = {row["question_id"] for row in scored}
            practice_ids = sorted(question_ids, key=lambda q: (q in seen, q == focus_question, q))
            practice_question = practice_ids[0]
            used_questions = sorted(seen | {focus_question, practice_question})
            references.extend(_material_reference(materials[q]) for q in used_questions)
            total_points = sum(row["score"] for row in scored)
            total_max = sum(row["max_score"] for row in scored)
            fact = (f"本节点本指标 {len(scored)}/{len(question_ids)} 个预期作答有有效评分，合计 {total_points}/{total_max} 分。"
                    if scored else f"本节点本指标预期 {len(question_ids)} 题，当前有 {len(current)} 条记录，没有可用于量化的已评分记录。")
            if shortfalls:
                status = "rubric_shortfall_observed"
                reason = fact + f"其中 {focus_question} 的评分点“{focus_label}”为 {focus_point['earned_points']}/{focus_point['max_score']} 分；只据此确定复核步骤，不推断失分原因。"
                action = f"先让该学生重现 {focus_question} 中“{focus_label}”的步骤并说明依据，再对照评分定义逐项核对。"
                hint = f"先问“这一步“{focus_label}”需要哪些题目条件？”；仍无法推进时只指出评分点要求，再让学生自行补写并解释，不直接提供答案。"
            elif scored:
                status = "scored_evidence_observed"
                reason = fact + "当前已评分记录未出现评分点失分；其余题目、其他节点和独立完成情况仍须分别观察。"
                action = f"让该学生解释 {focus_question} 中“{focus_label}”的依据，再完成 {practice_question}，观察相同指标在另一作答中的表现。"
                hint = f"先请学生用自己的话说明“{focus_label}”；需要帮助时提醒核对题目条件和评分点，记录每次提示，不提前展示参考解答。"
            else:
                status = "evidence_insufficient"
                reason = fact + "未作答、待评分和异常记录均不当作零分，不能据此判断薄弱。"
                action = f"安排该学生完成 {focus_question} 并保留最终作答与过程；按来源中的评分点记录后再决定是否补救。"
                hint = "先说明题目要求并请学生尝试；必要时分步提醒记录条件与操作依据，记录提示内容，评分前不展示参考答案。"
            resource_step = (f"阅读 {learning[0]['id']}：{learning[0]['label']}；指出其中与“{focus_label}”相对应的做法。"
                             if learning else f"对照已载入的 {focus_question} 题干与评分点要求；本指标没有已确认映射的学习卡或教材，教材需教师先完成映射。")
            practice_note = ("本次所选节点内已有该题评分，属于复做观察，不作为新题迁移证据。" if practice_question in seen
                             else "本次所选节点内尚无该学生该题有效评分；是否曾做过、是否获提示仍未知。")
            observation = {"question_id": practice_question, "question_ids": [practice_question],
                           "conditions": [f"保持节点 {node_id}、指标 {indicator_id} 和原评分点口径。", practice_note,
                                          "先保存尝试，再按需提示；如使用提示，记录内容、顺序和时间。"],
                           "record_fields": ["学生编号", "节点编号", "材料编号", "提交时间", "最终作答", "解答过程", "逐评分点得分", "提示内容与使用时间", "是否复做"],
                           "success_criteria": [f"能够说明并完成“{focus_label}”，由教师依据该题原评分点复核。",
                                                "同时报告实际评分分子、分母和未覆盖记录，不由单次结果推断稳定能力。"]}
            suggestions.append({"id": "rec-" + digest([student_id, node_id, topic_id, indicator_id])[:24],
                                "audience": "individual", "student_id": student_id, "class_id": individual["class_id"],
                                "node_id": node_id, "topic_id": topic_id, "indicator_id": indicator_id,
                                "indicator_label": requirements[indicator_id]["original"]["观察指标"], "order": 0, "status": status,
                                "reason": reason, "action": action, "sequence": [action, resource_step,
                                    f"完成 {practice_question} 并保留逐步作答；{practice_note}",
                                    "教师对照原评分点记录结果、提示使用和日期，再决定继续练习或补充观察。"],
                                "hint": hint, "observe": f"观察 {practice_question} 的“{focus_label}”及其余评分点；记录原始作答、实际提示和复做情况，以同口径后续证据复核本次建议。",
                                "references": references, "material_status": "mapped_material_available" if learning else "pending_learning_material_mapping",
                                "focus": {"question_id": focus_question, "rubric_id": focus_point["rubric_id"] if focus_point else None, "label": focus_label},
                                "observation_plan": observation,
                                "evidence": {"eligible_events": len(scored), "expected_event_denominator": len(question_ids),
                                             "current_events": len(current), "question_ids": question_ids,
                                             "earned_points": total_points if scored else None, "scored_max_points": total_max or None,
                                             "score_percentage": round(total_points * 100 / total_max, 2) if total_max else None,
                                             "evidence_refs": [_record_ref(row) for row in current],
                                             "rubric_observations": rubric_observations}})
    suggestions.sort(key=lambda item: (item["student_id"], item["node_id"],
                                      {"rubric_shortfall_observed": 0, "evidence_insufficient": 1, "scored_evidence_observed": 2}[item["status"]],
                                      item["evidence"]["score_percentage"] if item["evidence"]["score_percentage"] is not None else -1,
                                      item["topic_id"], item["indicator_id"]))
    student_orders = Counter()
    for item in suggestions:
        student_orders[item["student_id"]] += 1
        item["order"] = student_orders[item["student_id"]]
    return suggestions


def _output_schema():
    return {"schema_version": EXPORT_SCHEMA_VERSION,
            "envelope": [{"field": "schema_version", "type": "string", "meaning": "导出包模式版本"},
                         {"field": "run", "type": "object", "meaning": "冻结的分析结果，包括 summary、scope、issues、sections、provenance"},
                         {"field": "provenance", "type": "object", "meaning": "分析记录、课程、输入摘要与预览修订；不包含登录凭证或创建请求标识"}],
            "sections": [{"field": field, "meaning": label, "type": kind} for field, label, kind in [
                ("requirements_and_material_mappings", "课程要求、指标、实际材料出处与教师确认映射", "object"),
                ("individuals_by_topic_and_node", "所选学生逐节点、主题与指标的分子、分母及作答定位", "array"),
                ("class_and_selected_group_performance", "逐节点班级和所选组汇总", "array"),
                ("comparable_class_metrics", "同节点共同题目上的班级比较", "array"),
                ("representative_cases", "代表学生全部当前有效证据、个人观察与建议链接", "array"),
                ("individual_learning_recommendations", "代表学生逐节点与指标的材料、行动、顺序、提示及观察条件", "array"),
                ("evidence_based_recommendations", "所选群体教学建议，audience 为 selected_cohort", "array"),
                ("node_coverage_and_limits", "所选测量节点与可比限制", "array"),
                ("methods", "计算、修订选择、覆盖及解释边界", "object"),
                ("materials_used", "本次载入的可用材料登记与实际行引用；不表示已读原始教材全文", "array"),
                ("field_dictionary", "原输入字段字典及源行定位", "array"),
                ("data_notes", "原输入索引备注及源行定位；缺失时为空数组", "array"),
                ("sources", "上传文件名称、字节数、编码和SHA-256", "array"),
                ("source_permissions", "来源与权限采集状态，未采集不会转为已获许可", "array"),
                ("input_schema", "可接受输入的精确表头与限制", "object"),
                ("output_schema", "导出字段说明与关联规则", "object"),
                ("operation_guide", "复核、阅读、机器重算与人工文档交付操作说明", "array")]],
            "common_fields": [{"field": field, "type": kind, "meaning": meaning} for field, kind, meaning in [
                ("student_id/class_id/node_id/topic_id/indicator_id/question_id", "string", "只使用本次载入的相应实体编号"),
                ("earned_points/scored_max_points", "integer|null", "有效已评分记录得分和实际满分；没有有效评分时为null"),
                ("score_percentage", "number|null", "100×有效得分/有效满分，保留两位；不是掌握度"),
                ("expected_event_denominator/possible_event_denominator", "integer", "对应所选范围的原定题数或人数×题数"),
                ("scored_event_coverage/event_coverage", "number|null", "实际已评分事件或当前事件/预期事件分母，单位0—1"),
                ("class_equal_question_mean_percentage", "number|null", "各班同节点共同题目逐题百分比的等权均值；无共同题时为空"),
                ("evidence_refs/evidence_ref/source_ref", "array|object", "source_id、file_sha256、table、row；作答另含record_id、event_id、revision"),
                ("recommendation_ids/recommendations/observations.recommendation_id", "array|string", "通过id关联代表案例、个人建议和事实观察"),
                ("references", "array", "实际载入材料的id、kind、label、source、mapping及source_ref"),
                ("order", "integer", "个人建议在每个学生内排序；节点内先观察到评分点未满，再补证，再观察迁移"),
                ("focus/rubric_observations", "object|array", "原题评分点及该学生实际分数，不推断失分原因"),
                ("observation_plan", "object", "实际节点内题号、观察条件、应记录字段和教师复核标准"),
                ("null", "null", "未采集、未评分或分母为零；不得补为零"),
            ]],
            "links": ["案例recommendation_ids → individual_learning_recommendations.id；案例recommendations含同一条建议的完整副本。",
                      "references.id → materials_used.id；source_ref.source_id → sources.id。",
                      "表格行号从1开始包含表头；原始文件不打入公开导出，操作者按输入SHA-256自行核对本地原件。"],
            "csv": {"encoding": "utf-8-sig", "nested_fields": "JSON文本；CSV公式样文本加一个前置英文单引号",
                    "long_cells": "超过12000字符按large_cells.chunks.csv分片；escaped=true去除恰好一个前置单引号，按part拼接并核对各表chunks.manifest.json中的SHA-256。",
                    "original_values": "result.json保留原始字段值；各CSV为便于表格查看的安全表示。"}}


def _operation_guide():
    return [{"step": 1, "title": "导入与核对", "instruction": "导入本次获准的六张CSV或完整索引XLSX；核对文件SHA-256、字段说明、数据说明与问题清单。索引备注缺失时回到原件补充数据说明.csv。"},
            {"step": 2, "title": "确认范围与材料", "instruction": "选择测量节点、班级、学生、分组和最多三名代表学生；教材缺少指标时从实际材料与要求中明确映射。复核后冻结分析；更换输入或范围请新建分析。"},
            {"step": 3, "title": "阅读分析成果", "instruction": "先读课程要求与出处，再看各节点个体、班级和共同题比较，最后逐学生讲清原作答、实际评分点、材料、行动顺序、分步提示与后续观察。"},
            {"step": 4, "title": "保存成果包与复算", "instruction": "保存ZIP、JSON及HTML。ZIP含中文报告、各CSV、字段与模式说明、输入摘要和manifest.json。按manifest核对每个文件SHA-256；长单元格按分片规则恢复。CSV单独导出仅为摘要，完整数据使用ZIP或JSON。"},
            {"step": 5, "title": "人工制作PDF与Word", "instruction": "打开report.html并使用浏览器打印为PDF；检查分页、长表与作答过程。Word可打开HTML或复制所需中文分节和表格，人工调整页眉、目录与引用后保存DOCX。保留分析编号和输入摘要，检查教材与作答的使用范围后再共享。"},
            {"step": 6, "title": "人工制作教学汇报PPT与演示", "instruction": "按问题与范围、分析成果、三名代表证据与个人建议、两班共同题比较、方法与局限顺序制作PPT。只引用本报告真实表格和案例；讲清群体建议与个人建议的区别，并按教学安排调整页数与演示时长。"},
            {"step": 7, "title": "后续观察与资料权限", "instruction": "按每名学生的observation_plan收集后续题目、步骤、评分点、提示和复做情况。当前报告不证明提分或独立完成。来源和许可未采集的项目继续待核实；数据与文档不在此流程上传外部模型，导出不附原始文件或登录、请求凭证。"}]


def _build_result(parsed, run_id, course_id, title, scope, confirmed_by):
    entities = parsed["entities"]
    req = {r["indicator_id"]: r for r in entities["requirements"]}
    materials = {m["material_id"]: m for m in entities["materials"]}
    nodes = {n["node_id"]: n for n in entities["nodes"]}
    students = {s["student_id"]: s for s in entities["students"]}
    records = parsed["tables"]["作答记录"]
    selected_students = scope["student_ids"] or sorted(students)
    selected_classes = scope["class_ids"] or sorted({students[s]["class_id"] for s in selected_students if s in students})
    members = {c: [s for s in selected_students if students.get(s, {}).get("class_id") == c] for c in selected_classes}
    group_members = {g["id"]: g["student_ids"] for g in scope["groups"]}
    allowed_students = set(selected_students)
    current_by_key = {(r["student_id"], r["node_id"], r["question_id"]): r
                      for r in records if r["selection"] == "current" and r["student_id"] in allowed_students
                      and r["node_id"] in scope["node_ids"]}
    current_rows = list(current_by_key.values())
    eligible_rows = [row for row in current_rows if row["quantitative_eligible"]]
    by_student_node, by_node, scored_by_node, by_node_question = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for row in current_rows:
        by_student_node[(row["student_id"], row["node_id"])].append(row)
        by_node[row["node_id"]].append(row)
        if row["quantitative_eligible"]:
            scored_by_node[row["node_id"]].append(row)
            by_node_question[(row["node_id"], row["question_id"])].append(row)
    requirement_rows = []
    for topic_id in sorted({r["topic_id"] for r in req.values()}):
        indicators = [r for r in req.values() if r["topic_id"] == topic_id]
        topic_name = indicators[0]["original"]["主题"]
        requirement_rows.append({"topic_id": topic_id, "topic": topic_name, "objective": indicators[0]["original"]["主题目标"],
                                 "indicators": [{"id": i["indicator_id"], "label": i["original"]["观察指标"],
                                                 "standard_ref": i["original"]["课标整理编号"],
                                                 "standard_page": i["original"]["课标文件页／正文页"],
                                                 "textbook_page": i["original"]["教材正文页"],
                                                 "source_ref": i["ref"],
                                                 "question_ids": sorted(m["material_id"] for m in entities["materials"] if m["kind"] == "题目" and material_is_usable(m) and i["indicator_id"] in m["indicator_ids"]),
                                                 "learning_material_ids": [m["id"] for m in _related_materials(materials, scope, topic_id, i["indicator_id"]) if m["kind"] == "学习卡"],
                                                 "operator_mapped_textbook_ids": [m["id"] for m in _related_materials(materials, scope, topic_id, i["indicator_id"]) if m["kind"] == "教材" and m["mapping"] == "operator_selected"],
                                                 "material_references": _related_materials(materials, scope, topic_id, i["indicator_id"])} for i in indicators]})
    indicators_by_topic = defaultdict(list)
    for row in req.values():
        indicators_by_topic[row["topic_id"]].append(row["indicator_id"])
    node_sections, individual_rows, group_rows, comparison_rows = [], [], [], []
    recommendations = []
    selected_nodes = [nodes[n] for n in scope["node_ids"]]
    for node in selected_nodes:
        node_id, node_question_ids, node_topics = node["node_id"], node["question_ids"], node["topic_ids"]
        valid_questions = [q for q in node_question_ids if q in materials and materials[q].get("kind") == "题目" and not materials[q].get("anomalies")]
        baseline = {"node_id": node_id, "node_date": node["original"]["模拟测验日期"],
                    "node_description": node["original"]["说明"], "node_expected_question_count": len(node_question_ids),
                    "selected_roster_count": len(selected_students), "topics": node_topics,
                    "recorded_events": len(by_node[node_id]),
                    "scored_event_denominator": len(selected_students) * len(node_question_ids),
                    "scored_events": len(scored_by_node[node_id]),
                    "coverage_note": "覆盖分母=所选花名册人数×节点原定题目数；得分比例另以当前有效、已评分题目实际满分为分母。"}
        for student_id in selected_students:
            student = students.get(student_id)
            if not student:
                continue
            current = by_student_node[(student_id, node_id)]
            for topic_id in node_topics:
                topic_questions = [q for q in valid_questions if materials[q]["topic_id"] == topic_id]
                topic_records = [r for r in current if r["question_id"] in topic_questions]
                eligible = [r for r in topic_records if r["quantitative_eligible"]]
                points = sum(r["score"] for r in eligible)
                maximum = sum(r["max_score"] for r in eligible)
                indicator_items = []
                for indicator_id in sorted(indicators_by_topic.get(topic_id, [])):
                    mapped_q = [q for q in topic_questions if indicator_id in materials[q]["indicator_ids"]]
                    ir = [r for r in topic_records if r["question_id"] in mapped_q]
                    ie = [r for r in ir if r["quantitative_eligible"]]
                    ipoints, imax = sum(r["score"] for r in ie), sum(r["max_score"] for r in ie)
                    item = {"indicator_id": indicator_id, "available_questions": len(mapped_q), "expected_event_denominator": len(mapped_q),
                            "current_events": len(ir), "scored_events": len(ie), "scored_event_denominator": len(mapped_q),
                            "earned_points": ipoints if ie else None, "scored_max_points": imax or None,
                            "score_percentage": round(ipoints / imax * 100, 2) if imax else None,
                            "evidence_refs": [_record_ref(r) for r in sorted(ir, key=lambda r: r["question_id"])]}
                    indicator_items.append(item)
                individual_rows.append({"student_id": student_id, "class_id": student["class_id"], "node_id": node_id,
                                       "topic_id": topic_id, "topic": next((v["original"]["主题"] for v in req.values() if v["topic_id"] == topic_id), topic_id),
                                       "available_questions": len(topic_questions), "expected_event_denominator": len(topic_questions),
                                       "current_events": len(topic_records), "scored_events": len(eligible),
                                       "scored_event_denominator": len(topic_questions),
                                       "scored_question_coverage": round(len({r["question_id"] for r in eligible}) / len(topic_questions), 4) if topic_questions else None,
                                       "earned_points": points if eligible else None, "scored_max_points": maximum or None,
                                       "score_percentage": round(points / maximum * 100, 2) if maximum else None,
                                       "indicators": indicator_items,
                                       "evidence_refs": [_record_ref(r) for r in sorted(topic_records, key=lambda r: r["question_id"])]})
        for kind, label, class_id, student_ids in [("class", "班级", c, members[c]) for c in selected_classes] + [("selected_group", g["label"], g["id"], group_members[g["id"]]) for g in scope["groups"]]:
            if not student_ids:
                continue
            selected_set = set(student_ids)
            selected = [r for r in by_node[node_id] if r["student_id"] in selected_set]
            qrows = []
            for q in valid_questions:
                qr = [r for r in selected if r["question_id"] == q and r["quantitative_eligible"]]
                qp, qm = sum(r["score"] for r in qr), sum(r["max_score"] for r in qr)
                qrows.append({"question_id": q, "topic_id": materials[q]["topic_id"], "eligible_student_count": len({r["student_id"] for r in qr}),
                              "eligible_event_count": len(qr), "possible_event_denominator": len(student_ids),
                              "earned_points": qp if qr else None, "scored_max_points": qm or None,
                              "score_percentage": round(qp / qm * 100, 2) if qm else None,
                              "evidence_refs": [_record_ref(r) for r in sorted(qr, key=lambda r: r["student_id"]) ]})
            valid_rows = [r for r in selected if r["quantitative_eligible"]]
            denominator = len(student_ids) * len(node_question_ids)
            group_rows.append({"id": class_id, "label": label, "group_kind": kind,
                               "student_ids": sorted(student_ids), "student_count_denominator": len(student_ids), "node_id": node_id,
                               "available_questions": len(node_question_ids), "possible_event_denominator": denominator,
                               "current_events": len(selected), "eligible_scored_events": len(valid_rows),
                               "scored_max_points": sum(r["max_score"] for r in valid_rows) or None,
                               "earned_points": sum(r["score"] for r in valid_rows) if valid_rows else None,
                               "scored_point_weighted_percentage": round(sum(r["score"] for r in valid_rows) * 100 / sum(r["max_score"] for r in valid_rows), 2) if valid_rows else None,
                               "event_coverage": round(len([r for r in selected if r["status"] in {"已评分", "待评分", "未作答"}]) / denominator, 4) if denominator else None,
                               "scored_event_coverage": round(len(valid_rows) / denominator, 4) if denominator else None,
                               "questions": qrows, "evidence_refs": [_record_ref(r) for r in sorted(selected, key=lambda r: (r["student_id"], r["question_id"]))]})
        classes_present = [(c, members[c]) for c in selected_classes if members[c]]
        class_sets = {c: set(sids) for c, sids in classes_present}
        eligible_q_by_class = {c: {r["question_id"] for r in scored_by_node[node_id] if r["student_id"] in class_sets[c]} for c, _ in classes_present}
        common = sorted(set.intersection(*(eligible_q_by_class[c] for c, _ in classes_present))) if classes_present else []
        common_rows = []
        if len(classes_present) >= 2:
            for c, sids in classes_present:
                for q in common:
                    qr = [r for r in by_node_question[(node_id, q)] if r["student_id"] in class_sets[c]]
                    qp, qm = sum(r["score"] for r in qr), sum(r["max_score"] for r in qr)
                    common_rows.append({"node_id": node_id, "class_id": c, "question_id": q,
                                        "common_question_count_denominator": len(common), "class_student_count_denominator": len(sids),
                                        "eligible_event_count": len(qr), "earned_points": qp,
                                        "scored_max_points": qm, "score_percentage": round(qp * 100 / qm, 2) if qm else None,
                                        "evidence_refs": [_record_ref(r) for r in sorted(qr, key=lambda r: r["student_id"])]})
            for c, sids in classes_present:
                rows = [r for r in common_rows if r["class_id"] == c]
                common_set = set(common)
                current_comparable = [r for r in by_node[node_id] if r["student_id"] in class_sets[c] and r["question_id"] in common_set]
                scored_comparable = [r for r in current_comparable if r["quantitative_eligible"]]
                comparison_rows.append({"node_id": node_id, "class_id": c, "common_question_ids": common,
                                        "common_question_count_denominator": len(common), "student_count_denominator": len(sids),
                                        "possible_event_denominator": len(sids) * len(common),
                                        "current_events": len(current_comparable), "scored_events": len(scored_comparable),
                                        "event_coverage": round(len(current_comparable) / (len(sids) * len(common)), 4) if common and sids else None,
                                        "scored_event_coverage": round(len(scored_comparable) / (len(sids) * len(common)), 4) if common and sids else None,
                                        "class_equal_question_mean_percentage": _mean([r["score_percentage"] for r in rows if r["score_percentage"] is not None]),
                                        "question_metrics": rows, "evidence_refs": [ref for r in rows for ref in r["evidence_refs"]]})
        node_sections.append({**baseline, "class_comparison_method": "仅在同一节点且各所选班级均有至少一份有效已评分作答的共同题目上比较；每题按实际评分点得分/分值加权，班级汇总为共同题目百分比的等权均值。与不同节点或不同题集的分数不可直接比较。",
                              "common_question_ids": common, "cross_node_comparison": False})
        for topic_id in node_topics:
            for indicator in indicators_by_topic.get(topic_id, []):
                question_ids = [q for q in valid_questions if materials[q]["topic_id"] == topic_id and indicator in materials[q]["indicator_ids"]]
                question_set = set(question_ids)
                candidate_records = [r for r in scored_by_node[node_id] if r["topic_id"] == topic_id and r["question_id"] in question_set]
                if not question_ids:
                    continue
                if not candidate_records:
                    recommendations.append({"order": 0, "node_id": node_id, "topic_id": topic_id, "indicator_id": indicator,
                                            "status": "evidence_insufficient", "evidence": {"eligible_events": 0, "available_questions": len(question_ids), "student_denominator": len(selected_students)},
                                            "references": [], "reason": "该指标有节点题目，但所选范围没有有效评分记录；不能据此判定薄弱或推荐针对性补救。",
                                            "action": "补充该指标的可观察作答并按同一评分标准记录。", "sequence": ["补充作答", "按评分点记录", "复核后再决定资源"],
                                            "hint": "分步提示操作步骤和检查依据，不直接给答案。", "observe": "下一次记录覆盖人数、题目和评分点；实际提示使用情况尚未采集。"})
                    continue
                p, m = sum(r["score"] for r in candidate_records), sum(r["max_score"] for r in candidate_records)
                percentage = p / m * 100 if m else None
                related_materials = _related_materials(materials, scope, topic_id, indicator)
                related_questions = sorted({r["question_id"] for r in candidate_records})
                case_refs = [_record_ref(r) for r in sorted(candidate_records, key=lambda r: (r["student_id"], r["question_id"]))]
                evidence_detail = {"eligible_events": len(candidate_records), "eligible_event_denominator": len(selected_students) * len(question_ids),
                                   "students_with_evidence": len({r["student_id"] for r in candidate_records}), "student_denominator": len(selected_students),
                                   "questions_available": len(question_ids), "question_ids": related_questions, "earned_points": p,
                                   "scored_max_points": m, "score_percentage": round(percentage, 2),
                                   "partial_credit_events": sum(0 < r["score"] < r["max_score"] for r in candidate_records), "evidence_refs": case_refs}
                sequence = ["重现该指标涉及的题目要求", "选用一条已确认关联的学习材料或教材片段", "分步完成同技能练习，不直接呈现参考答案",
                            "记录步骤、评分点、补做情况及实际提示使用情况"]
                if percentage < 70 and evidence_detail["students_with_evidence"] > 0:
                    reason = f"所选范围 {evidence_detail['students_with_evidence']}/{len(selected_students)} 名学生有本指标有效评分；{len(candidate_records)}/{evidence_detail['eligible_event_denominator']} 个预期作答有分，合计 {p}/{m} 分。低比例只描述所观测评分记录。"
                    status, action = "evidence_observed", "先核对典型作答步骤，再用对应材料做分步讲解和同技能练习。"
                elif evidence_detail["partial_credit_events"]:
                    reason = f"观察到 {evidence_detail['partial_credit_events']}/{len(candidate_records)} 个有效评分事件有部分得分；记录只支持指出可继续观察的步骤，不足以断定薄弱。"
                    status, action = "partial_credit_observed", "对照已载明的评分点追问每一步的依据，再布置同技能变式题观察。"
                else:
                    reason = f"当前纳入的 {len(candidate_records)}/{evidence_detail['eligible_event_denominator']} 个预期作答有评分；该比例是证据覆盖描述，不代表掌握度。"
                    status, action = "evidence_observed", "在另一道同指标变式题观察步骤迁移，并核对其余未覆盖记录。"
                recommendations.append({"order": 0, "node_id": node_id, "topic_id": topic_id, "indicator_id": indicator,
                                        "status": status, "evidence": evidence_detail,
                                        "references": related_materials + [_material_reference(materials[q]) for q in related_questions],
                                        "reason": reason, "action": action if related_materials else "先核对课程要求与相关作答，再补充经教师确认适用的学习材料；不自动推断教材对应关系。",
                                        "sequence": sequence, "hint": "按识别条件、写出操作步骤、独立复述理由顺序给提示，不直接给答案。",
                                        "observe": "后续记录相同评分点的作答、分母、时间和实际提示使用情况；当前资料不能推断学生是否求助或独立完成。"})
        # Make ordering stable and comparable without pretending that a tiny N
        # estimates a student capability.
    recommendations.sort(key=lambda r: (r["node_id"], r["status"] == "evidence_insufficient",
                                        -(r.get("evidence", {}).get("eligible_events", 0)), r.get("topic_id", ""), r.get("indicator_id", "")))
    for index, item in enumerate(recommendations, 1):
        item["order"] = index
        item["audience"] = "selected_cohort"
        item["id"] = "group-rec-" + digest([item["node_id"], item["topic_id"], item["indicator_id"]])[:24]
    personal_recommendations = _individual_recommendations(parsed, scope, individual_rows)
    recommendations_by_student = defaultdict(list)
    for recommendation in personal_recommendations:
        recommendations_by_student[recommendation["student_id"]].append(recommendation)
    question_map = {m["material_id"]: m for m in entities["materials"] if m["kind"] == "题目"}
    selected_cases = []
    for student_id in scope["representative_student_ids"]:
        student = students[student_id]
        selected_records = [r for node_id in scope["node_ids"] for r in by_student_node[(student_id, node_id)]
                            if not (set(r.get("anomalies", [])) - {"revision_gap"})]
        auto = next((s for s in scope["auto_suggestions"] if s["student_id"] == student_id), None)
        personal = recommendations_by_student[student_id]
        observations = [{"node_id": item["node_id"], "topic_id": item["topic_id"], "indicator_id": item["indicator_id"],
                         "indicator_label": item["indicator_label"], "score_percentage": item["evidence"]["score_percentage"],
                         "scored_events": item["evidence"]["eligible_events"],
                         "expected_event_denominator": item["evidence"]["expected_event_denominator"],
                         "fact": item["reason"], "evidence_refs": item["evidence"]["evidence_refs"],
                         "recommendation_id": item["id"]} for item in personal]
        selected_cases.append({"student_id": student_id, "class_id": student["class_id"], "selection": "automatic" if auto else "operator_selected",
                              "role": auto["role"] if auto else "教师从本次导入名单中选择",
                              "reason": auto["selection_reason"] if auto else "所选学生来源于本次导入名单，按冻结节点重算案例。",
                              "node_ids": scope["node_ids"], "evidence": [{"question_id": r["question_id"], "topic_id": r["topic_id"], "node_id": r["node_id"],
                                  "status": r["status"], "score": r["score"] if r["quantitative_eligible"] else None,
                                  "question_max_score": r["max_score"] if r["quantitative_eligible"] else None,
                                  "final_answer": r["original"]["学生最终作答"], "solution_process": r["original"]["解答过程"],
                                  "rubric_scores": r["rubric_scores"] if r["quantitative_eligible"] else None,
                                  "question_type": question_map.get(r["question_id"], {}).get("original", {}).get("题型"),
                                  "rubric": question_map.get(r["question_id"], {}).get("rubric", []),
                                  "evidence_ref": _record_ref(r)} for r in sorted(selected_records, key=lambda r: (r["node_id"], r["question_id"]))],
                              "observations": observations, "recommendation_ids": [item["id"] for item in personal],
                              "recommendations": personal,
                              "excluded_current_evidence_refs": [_record_ref(r) for node_id in scope["node_ids"] for r in by_student_node[(student_id, node_id)]
                                                                 if set(r.get("anomalies", [])) - {"revision_gap"}],
                              "limitations": ["助理使用情况未知。", "是否独立完成未知。", "自动案例角色只描述被选节点现有记录。",
                                              "证据正文保留所选节点全部当前有效作答；异常当前记录单列定位，不用来诊断。"]})
    sections = {"requirements_and_material_mappings": {"items": requirement_rows,
                 "material_mappings": scope["material_mappings"],
                 "unmapped_textbook_candidates": [item for item in input_preview(parsed)["options"]["unmapped_textbooks"]
                                                  if item["id"] not in scope["material_mappings"]],
                 "mapping_note": "教材编号仅在教师本次明确选择主题和指标后纳入关联；无主题或指标材料不会自动归因。"},
                "individuals_by_topic_and_node": individual_rows,
                "class_and_selected_group_performance": group_rows,
                "comparable_class_metrics": comparison_rows, "representative_cases": selected_cases,
                "evidence_based_recommendations": recommendations,
                "individual_learning_recommendations": personal_recommendations,
                "node_coverage_and_limits": node_sections,
                "field_dictionary": [{**row["original"], "source_ref": row["ref"]} for row in parsed["tables"]["字段说明"]],
                "data_notes": [{**row["original"], "source_ref": row["ref"]} for row in parsed["tables"].get("数据说明", [])],
                "input_schema": schema_description(), "output_schema": _output_schema(), "operation_guide": _operation_guide(),
                "source_permissions": [{"source_id": src["id"], "filename": src["filename"],
                                        "origin": "operator_uploaded_organizer_table", "publisher_verified": False,
                                        "license_status": "not_collected", "external_processing_permission": "not_collected",
                                        "redistribution_permission": "not_collected", "external_processing_performed": False,
                                        "note": "仅处理操作者本次上传的表格；来源文件名和引用页不代表系统已读取原课标、教材全文或取得再分发许可。"}
                                       for src in parsed["sources"]],
                "materials_used": [_material_reference(m) | {"topic_id": m["topic_id"], "indicator_ids": m["indicator_ids"],
                                   "run_mapping": scope["material_mappings"].get(m["material_id"])}
                                   for m in sorted(entities["materials"], key=lambda row: row["material_id"]) if material_is_usable(m)],
                "methods": {"scoring": "当前每个作答事件仅取最大整数修订版本。事件中最新版本异常、重复修订或ID冲突则不回退到历史高分。有效得分/满分按实际入选已评分事件分值加权；缺失与零严格区分。",
                            "coverage": "事件覆盖分母为所选学生人数×节点原定题目数；指标和学生覆盖分母按该节点来源中对应题目数计算。覆盖小于1不填零，不以未作答代替零分。",
                            "independence_and_assistance": FORMULA_INDEPENDENCE,
                            "comparability": "同节点共同题目上的类别比较不跨班级差异题、不同题集或不同测验节点；仅描述可观察作答。",
                            "mastery": "不计算掌握度、能力、独立性或因果效果；小样本分值不得解读成稳定学生特征。",
                            "recommendation_order": "个人建议只使用该代表学生在所选节点内的当前有效证据；节点内先复核实际未满分评分点，再补充缺失证据，再观察已有满分记录的解释与迁移。评分点按原定义顺序确定复核重点。群体建议另列，不替代个人建议。",
                            "materials": "材料引用仅来自本次载入的题目、学习卡和教材登记行；教师确认映射按本分析记录，未映射教材继续待定。原始标准文件和教材全文未由此表格导入功能读取。",
                            "follow_up": "后续练习只列所选节点原定题集。已有评分的题明确标为复做；未见评分不意味着学生从未做过。教师须记录实际提示、时间和复做情况后再判断。"},
                "sources": [{k: v for k, v in src.items() if k != "content_base64"} for src in parsed["sources"]]}
    warnings = list(parsed["issues"])
    if any(len(n["topic_ids"]) < len({r["topic_id"] for r in entities["requirements"]}) for n in selected_nodes):
        warnings.append({"code": "cross_node_topic_limit", "severity": "info", "table": "测验节点", "refs": [],
                         "message": "不同节点测量主题或题集不同，仅在每个节点单独统计；跨节点差异可能是课程进度、覆盖内容或题目所致，不视为同题进步或退步。"})
    summary = {"course_id": course_id, "title": title, "run_id": run_id, "scope": {"node_ids": scope["node_ids"], "class_ids": selected_classes,
               "student_count_denominator": len(selected_students), "representative_student_ids": scope["representative_student_ids"],
               "group_count": len(scope["groups"]), "material_mapping_count": len(scope["material_mappings"])},
               "source_sha256": parsed["input_sha256"], "issue_count": len(warnings),
               "current_event_count": sum(r["selection"] == "current" and r["student_id"] in allowed_students and r["node_id"] in scope["node_ids"] for r in records),
               "eligible_scored_event_count": sum(r["quantitative_eligible"] and r["student_id"] in allowed_students and r["node_id"] in scope["node_ids"] for r in records),
               "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    snapshot = {"schema_version": RUN_SCHEMA_VERSION, "summary": summary, "scope": scope, "issues": warnings,
                "sections": sections, "provenance": {"input_sha256": parsed["input_sha256"], "sources": sections["sources"],
                                                       "confirmation_actor": confirmed_by, "formula_version": "organizer-analysis.v1"}}
    return snapshot


def create_scope(parsed: dict, request: Mapping[str, Any]) -> dict:
    if not isinstance(request, Mapping):
        raise CompetitionRunError("scope must be an object")
    allowed_fields = {"node_ids", "class_ids", "student_ids", "groups", "representative_student_ids", "material_mappings"}
    if set(request) - allowed_fields:
        raise CompetitionRunError("scope contains unsupported fields")
    e = parsed["entities"]
    node_ids_all = {n["node_id"] for n in e["nodes"] if not n.get("anomalies")}
    class_ids_all = {s["class_id"] for s in e["students"]}
    student_ids_all = {s["student_id"] for s in e["students"]}
    material_ids = {m["material_id"] for m in e["materials"] if m["kind"] in {"学习卡", "教材"} and material_is_usable(m)}
    nodes = _list(request.get("node_ids", sorted(node_ids_all)), "node_ids", node_ids_all, allow_empty=False)
    classes = _list(request.get("class_ids", sorted(class_ids_all)), "class_ids", class_ids_all)
    selected_students = _list(request.get("student_ids", []), "student_ids", student_ids_all)
    class_of = {s["student_id"]: s["class_id"] for s in e["students"]}
    roster_classes = set(classes) if classes else set(class_ids_all)
    if selected_students:
        if classes and any(class_of.get(s) not in roster_classes for s in selected_students):
            raise CompetitionRunError("selected students must belong to selected imported classes")
        roster_classes = {class_of[s] for s in selected_students}
    student_ids = sorted(s for s in student_ids_all if class_of.get(s) in roster_classes and (not selected_students or s in set(selected_students)))
    if not student_ids:
        raise CompetitionRunError("analysis scope must include at least one imported student")
    if selected_students and any(class_of.get(s) not in roster_classes for s in selected_students):
        raise CompetitionRunError("selected students must belong to the selected imported class scope")
    groups_raw = request.get("groups", [])
    if not isinstance(groups_raw, list) or len(groups_raw) > MAX_GROUPS:
        raise CompetitionRunError("groups must be a bounded list")
    group_ids, groups = set(), []
    for raw in groups_raw:
        if not isinstance(raw, Mapping) or set(raw) - {"id", "label", "student_ids"}:
            raise CompetitionRunError("each group requires only id, label and student_ids")
        gid, label = raw.get("id"), raw.get("label")
        if not isinstance(gid, str) or not SAFE_TOKEN.fullmatch(gid) or gid in group_ids or gid in class_ids_all:
            raise CompetitionRunError("group IDs must be unique loaded-independent identifiers")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100 or CONTROL.search(label):
            raise CompetitionRunError("group label must be bounded text")
        group_members = _list(raw.get("student_ids"), "group student_ids", set(student_ids), allow_empty=False)
        group_ids.add(gid)
        groups.append({"id": gid, "label": CONTROL.sub(" ", label.strip()), "student_ids": group_members})
    suggestions = _build_case_suggestions(parsed, nodes, set(student_ids))
    reps = _list(request.get("representative_student_ids", [s["student_id"] for s in suggestions]),
                 "representative_student_ids", set(student_ids), allow_empty=True)
    if len(reps) > 3:
        raise CompetitionRunError("representative_student_ids can select at most three imported students")
    mappings_raw = request.get("material_mappings", [])
    if not isinstance(mappings_raw, list) or len(mappings_raw) > MAX_MAPPINGS:
        raise CompetitionRunError("material_mappings must be a bounded list")
    indicators = {r["indicator_id"] for r in e["requirements"] if not r.get("anomalies")}
    mlookup = {m["material_id"]: m for m in e["materials"]}
    mappings = {}
    for row in mappings_raw:
        if not isinstance(row, Mapping) or set(row) - {"material_id", "topic_id", "indicator_ids"}:
            raise CompetitionRunError("each material mapping requires material_id, topic_id and indicator_ids")
        mid, topic = row.get("material_id"), row.get("topic_id")
        if mid not in material_ids or mid in mappings:
            raise CompetitionRunError("material mapping must select a unique loaded card or textbook")
        if not isinstance(topic, str) or not SAFE_TOKEN.fullmatch(topic) or topic not in {r["topic_id"] for r in e["requirements"]}:
            raise CompetitionRunError("mapped topic must come from loaded course requirements")
        indicator_ids = _list(row.get("indicator_ids"), "material indicator_ids", indicators, allow_empty=False)
        if any(next(r["topic_id"] for r in e["requirements"] if r["indicator_id"] == i) != topic for i in indicator_ids):
            raise CompetitionRunError("mapped indicators must belong to the chosen loaded topic")
        if mlookup[mid]["topic_id"] and mlookup[mid]["topic_id"] != topic:
            raise CompetitionRunError("cannot override a source-declared material topic")
        if mlookup[mid]["indicator_ids"] and not set(indicator_ids) <= set(mlookup[mid]["indicator_ids"]):
            raise CompetitionRunError("cannot override source-declared material indicators")
        mappings[mid] = {"topic_id": topic, "indicator_ids": indicator_ids, "mapping_actor_confirmed": True,
                         "source_ref": mlookup[mid]["ref"]}
    return {"node_ids": nodes, "class_ids": sorted(roster_classes), "student_ids": student_ids,
            "groups": sorted(groups, key=lambda g: g["id"]), "representative_student_ids": reps,
            "auto_suggestions": suggestions,
            "material_mappings": {k: mappings[k] for k in sorted(mappings)}}


class CompetitionRunStore:
    """A separate sqlite file stores only competition imports, preview revisions and frozen runs."""
    SCHEMA_VERSION = 1
    _COLUMNS = [("id", "TEXT", 0, 1), ("course_id", "TEXT", 1, 0), ("created_by", "TEXT", 1, 0),
                ("title", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("revision", "INTEGER", 1, 0),
                ("preview_json", "TEXT", 1, 0), ("inputs_json", "TEXT", 1, 0), ("scope_json", "TEXT", 0, 0),
                ("result_json", "TEXT", 0, 0), ("request_sha256", "TEXT", 0, 0), ("created_at", "TEXT", 1, 0),
                ("confirmed_at", "TEXT", 0, 0)]

    def __init__(self, db_path: str | Path):
        if str(db_path) == ":memory:":
            raise ValueError("CompetitionRunStore requires a file-backed database for WAL")
        path = Path(db_path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("competition database must be an ordinary file")
        self.db_path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size:
            # This read-only preflight runs before any WAL or version-setting pragma.
            try:
                connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=20)
                try:
                    self._check_database(connection)
                finally:
                    connection.close()
            except sqlite3.Error as error:
                raise RuntimeError("existing database is not a readable competition database") from error
        with self._connect() as connection:
            self._check_database(connection)
            if str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            connection.execute("BEGIN IMMEDIATE")
            version = self._check_database(connection)
            if version == 0:
                connection.execute("""CREATE TABLE competition_runs (
                    id TEXT PRIMARY KEY, course_id TEXT NOT NULL, created_by TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('preview','confirmed')), revision INTEGER NOT NULL,
                    preview_json TEXT NOT NULL, inputs_json TEXT NOT NULL, scope_json TEXT,
                    result_json TEXT, request_sha256 TEXT, created_at TEXT NOT NULL, confirmed_at TEXT)""")
                connection.execute("CREATE INDEX competition_runs_course_created ON competition_runs(course_id, created_at DESC, id DESC)")
                connection.execute("PRAGMA user_version=1")
                self._check_database(connection)

    @classmethod
    def _check_database(cls, connection):
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != 0:
            raise RuntimeError("existing database belongs to a different application")
        if version > cls.SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {version} is newer than supported version {cls.SCHEMA_VERSION}")
        objects = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if version == 0:
            if objects:
                raise RuntimeError("refusing to initialize an unversioned, non-empty database")
            return version
        if version != 1 or {(r[0], r[1], r[2]) for r in objects} != {
                ("table", "competition_runs", "competition_runs"),
                ("index", "competition_runs_course_created", "competition_runs")}:
            raise RuntimeError("existing database does not match the competition schema")
        columns = connection.execute("PRAGMA table_xinfo(competition_runs)").fetchall()
        if ([(row[1], row[2].upper(), row[3], row[5]) for row in columns] != cls._COLUMNS
                or any(row[4] is not None or row[6] != 0 for row in columns)):
            raise RuntimeError("existing database has an incompatible competition table shape")
        definitions = {row[0]: re.sub(r"\s+", "", row[3] or "").lower() for row in objects}
        if "check(statusin('preview','confirmed'))" not in definitions["table"] or "virtualtable" in definitions["table"]:
            raise RuntimeError("existing database has incompatible competition constraints")
        if not definitions["index"].endswith("oncompetition_runs(course_id,created_atdesc,iddesc)"):
            raise RuntimeError("existing database has an incompatible competition index")
        if connection.execute("PRAGMA foreign_key_list(competition_runs)").fetchall():
            raise RuntimeError("competition schema must not reference other databases")
        return version

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=20, isolation_level="IMMEDIATE")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load(row):
        if row is None:
            return None
        return {"id": row["id"], "course_id": row["course_id"], "created_by": row["created_by"], "title": row["title"],
                "status": row["status"], "revision": row["revision"], "preview": json.loads(row["preview_json"]),
                "scope": json.loads(row["scope_json"]) if row["scope_json"] else None,
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "request_sha256": row["request_sha256"], "created_at": row["created_at"], "confirmed_at": row["confirmed_at"]}

    @property
    def schema_version(self):
        with self._connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self):
        with self._connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @staticmethod
    def _creation_run_id(course_id, actor_id, request_id):
        if not isinstance(request_id, str) or not SAFE_TOKEN.fullmatch(request_id):
            raise CompetitionRunError("request_id must be a bounded non-empty token")
        return "cr_" + digest(["competition.create.v1", actor_id, course_id, request_id])

    def create_preview(self, course_id, actor_id, title, parsed, request_id=None, *, access_check=None):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160 or CONTROL.search(title):
            raise CompetitionRunError("title must be bounded plain text")
        parsed = dict(parsed)
        run_id = self._creation_run_id(course_id, actor_id, request_id) if request_id is not None else "cr_" + uuid.uuid4().hex
        request_digest = digest({"course_id": course_id, "actor_id": actor_id, "title": title.strip(),
                                 "schema_version": parsed["schema_version"], "input_sha256": parsed["input_sha256"]})
        preview = input_preview(parsed)
        preview["options"]["auto_suggestions"] = create_scope(parsed, {})["auto_suggestions"]
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        inputs = {"schema_version": parsed["schema_version"], "input_sha256": parsed["input_sha256"],
                  "sources": parsed["sources"], "original_files": parsed["original_files"],
                  "tables": parsed["tables"], "entities": parsed["entities"], "issues": parsed["issues"],
                  "issues_summary": parsed["issues_summary"]}
        if request_id is not None:
            inputs["creation_request"] = {"request_id": request_id, "request_digest": request_digest}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM competition_runs WHERE id=?", (run_id,)).fetchone()
            if access_check is not None:
                access_check()
            if previous is not None:
                creation = json.loads(previous["inputs_json"]).get("creation_request", {})
                if (previous["course_id"] != course_id or previous["created_by"] != actor_id
                        or creation.get("request_id") != request_id or creation.get("request_digest") != request_digest):
                    raise CompetitionConflict("creation request was already used for different inputs")
                return self._load(previous)
            connection.execute("INSERT INTO competition_runs(id,course_id,created_by,title,status,revision,preview_json,inputs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (run_id, course_id, actor_id, title.strip(), "preview", 1,
                                canonical(preview), canonical(inputs), now))
            return self._load(connection.execute("SELECT * FROM competition_runs WHERE id=?", (run_id,)).fetchone())

    def get_by_request(self, course_id, actor_id, request_id):
        run_id = self._creation_run_id(course_id, actor_id, request_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM competition_runs WHERE id=? AND course_id=? AND created_by=?",
                                     (run_id, course_id, actor_id)).fetchone()
            if row is None or json.loads(row["inputs_json"]).get("creation_request", {}).get("request_id") != request_id:
                return None
            return self._load(row)

    def get(self, run_id):
        with self._connect() as connection:
            return self._load(connection.execute("SELECT * FROM competition_runs WHERE id=?", (run_id,)).fetchone())

    def list_course(self, course_id):
        with self._connect() as connection:
            rows = connection.execute("SELECT id,course_id,created_by,title,status,revision,preview_json,scope_json,result_json,request_sha256,created_at,confirmed_at FROM competition_runs WHERE course_id=? ORDER BY created_at DESC,id DESC", (course_id,)).fetchall()
            return [{"id": r["id"], "course_id": r["course_id"], "title": r["title"], "status": r["status"], "revision": r["revision"],
                     "source_sha256": json.loads(r["preview_json"]).get("input_sha256"), "scope": json.loads(r["scope_json"]) if r["scope_json"] else None,
                     "summary": json.loads(r["result_json"])["summary"] if r["result_json"] else json.loads(r["preview_json"])["summary"],
                     "created_at": r["created_at"], "confirmed_at": r["confirmed_at"]} for r in rows]

    def confirm(self, run_id, course_id, actor_id, title, expected_revision, scope_request, *, access_check=None):
        if type(expected_revision) is not int or expected_revision < 1:
            raise CompetitionRunError("expected_revision must be an integer")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM competition_runs WHERE id=? AND course_id=?", (run_id, course_id)).fetchone()
            if row is None:
                return None
            previous = self._load(row)
            scope = create_scope(json.loads(row["inputs_json"]), scope_request)
            request_hash = digest({"revision": expected_revision, "scope": scope})
            if row["status"] == "confirmed":
                if row["request_sha256"] == request_hash:
                    if access_check is not None:
                        access_check()
                    return previous
                raise CompetitionConflict("confirmed run is immutable")
            if row["revision"] != expected_revision:
                raise CompetitionConflict("preview revision changed")
            parsed = json.loads(row["inputs_json"])
            result = _build_result(parsed, run_id, course_id, row["title"], scope, actor_id)
            confirmed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if access_check is not None:
                access_check()
            cursor = connection.execute("UPDATE competition_runs SET status='confirmed',scope_json=?,result_json=?,request_sha256=?,confirmed_at=? WHERE id=? AND status='preview' AND revision=?",
                                        (canonical(scope), canonical(result), request_hash, confirmed_at, run_id, expected_revision))
            if cursor.rowcount != 1:
                raise CompetitionConflict("preview revision changed")
            row = connection.execute("SELECT * FROM competition_runs WHERE id=?", (run_id,)).fetchone()
            return self._load(row)

    def export_snapshot(self, run):
        return {"schema_version": EXPORT_SCHEMA_VERSION, "run": run["result"],
                "provenance": {"run_id": run["id"], "course_id": run["course_id"], "title": run["title"],
                               "input_sha256": run["preview"]["input_sha256"], "run_revision": run["revision"]}}


def _neutralize_nested(value):
    if isinstance(value, str):
        candidate = value.lstrip(" \t\r\n\ufeff\u200b")
        return "'" + value if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
    if isinstance(value, list):
        return [_neutralize_nested(item) for item in value]
    if isinstance(value, dict):
        return {key: _neutralize_nested(item) for key, item in value.items()}
    return value


def _safe_cell(value: Any):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = canonical(_neutralize_nested(value))
    text = CONTROL.sub(" ", value if isinstance(value, str) else str(value))
    candidate = text.lstrip(" \t\r\n\ufeff\u200b")
    if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


def _html_report(run, model):
    result, sections = model["run"], model["run"]["sections"]
    def safe(value):
        return html.escape(str(value if value is not None else "未采集"), quote=True)
    def text(value):
        if value is None:
            return '<span class="muted">无有效分母或未采集</span>'
        if isinstance(value, bool):
            return "是" if value else "否"
        return safe(value)
    def items(values, *, ordered=False):
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{text(value)}</li>" for value in values) + f"</{tag}>" if values else '<span class="muted">无</span>'
    def refs(values):
        if isinstance(values, Mapping):
            values = [values]
        if not values:
            return '<span class="muted">无可用记录</span>'
        labels = [f"{r.get('source_id', '')} · {r.get('table', '')} 第{r.get('row', '')}行"
                  + (f" · {r['record_id']} / 修订{r.get('revision', '')}" if r.get("record_id") else "") for r in values]
        return f"<details><summary>{len(values)} 条原始位置</summary>{items(labels)}</details>"
    def table(rows, columns):
        if not rows:
            return '<p class="muted">当前范围无相应记录。</p>'
        headings = "".join(f'<th scope="col">{safe(label)}</th>' for _, label, *_ in columns)
        body = []
        for row in rows:
            cells = []
            for column in columns:
                key, _, *renderer = column
                value = row.get(key)
                cells.append("<td>" + (renderer[0](value, row) if renderer else text(value)) + "</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        return '<div class="table-wrap"><table><thead><tr>' + headings + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    def resources(values):
        return table(values or [], [("id", "材料编号"), ("kind", "类型"), ("label", "题干或内容"),
                                  ("source", "来源原文"), ("mapping", "关联依据", lambda value, _: "教师本次确认" if value == "operator_selected" else "输入明确映射"),
                                  ("source_ref", "原始位置", lambda value, _: refs(value))])
    def point_table(values):
        return table(values or [], [("question_id", "题目"), ("rubric_id", "评分点"), ("label", "要求"),
                                   ("earned_points", "实际得分"), ("max_score", "原定满分"),
                                   ("evidence_ref", "作答位置", lambda value, _: refs(value))])
    def recommendation(item):
        evidence = item.get("evidence", {})
        focus = item.get("focus", {})
        observation = item.get("observation_plan", {})
        status_labels = {"rubric_shortfall_observed": "观察到评分点未满分", "scored_evidence_observed": "已有评分，继续观察解释与迁移",
                         "evidence_insufficient": "证据不足，先补充作答", "partial_credit_observed": "观察到部分得分", "evidence_observed": "已有评分证据"}
        body = [f'<article class="recommendation" id="{safe(item.get("id", ""))}">',
                f'<h4>{safe(item.get("student_id", "所选群体"))} · 第{safe(item.get("order"))}项 · {safe(item.get("node_id"))} / {safe(item.get("indicator_id"))}</h4>',
                f'<p class="muted">{safe(item.get("indicator_label", ""))} · {safe(status_labels.get(item.get("status"), item.get("status")))}</p>',
                f'<p><strong>观察事实与理由：</strong>{safe(item.get("reason"))}</p>',
                f'<p><strong>具体行动：</strong>{safe(item.get("action"))}</p>']
        if evidence:
            body.append(table([evidence], [("eligible_events", "有效评分事件"), ("expected_event_denominator", "预期作答分母"),
                                          ("earned_points", "得分合计"), ("scored_max_points", "有效满分"), ("score_percentage", "得分百分比（%）")]))
        if focus:
            body.append(f'<p><strong>本次复核重点：</strong>{safe(focus.get("question_id"))} · {safe(focus.get("rubric_id"))} · {safe(focus.get("label"))}</p>')
        body.extend(["<h5>材料和练习</h5>", resources(item.get("references")), "<h5>建议顺序</h5>", items(item.get("sequence", []), ordered=True),
                     f'<p><strong>分步提示：</strong>{safe(item.get("hint"))}</p>',
                     f'<p><strong>后续观察：</strong>{safe(item.get("observe"))}</p>'])
        if item.get("material_status") == "pending_learning_material_mapping":
            body.append('<p class="notice">本指标没有已确认映射的学习卡或教材；已列实际题目供复核，教材关联待教师确认。</p>')
        if observation:
            body.extend([f'<h5>观察任务 · {safe(observation.get("question_id"))}</h5>', items(observation.get("conditions", [])),
                         "<p><strong>需记录：</strong>" + safe("、".join(observation.get("record_fields", []))) + "</p>",
                         "<p><strong>教师复核条件</strong></p>", items(observation.get("success_criteria", []))])
        body.append(refs(evidence.get("evidence_refs", [])))
        if evidence.get("rubric_observations"):
            body.append('<details><summary>查看全部原评分点与实际得分</summary>' + point_table(evidence["rubric_observations"]) + "</details>")
        body.append("</article>")
        return "".join(body)
    page = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
            f'<title>{safe(run["title"])} · 分析成果报告</title>',
            '<style>body{font:15px/1.65 "Microsoft YaHei",sans-serif;max-width:1280px;margin:2rem auto;padding:0 1.2rem;color:#1f2933;background:#fff}'
            'h1{font-size:1.9rem}h2{border-bottom:2px solid #345b69;padding-bottom:.4rem;margin-top:2.5rem}h3{margin-top:1.6rem}h4{font-size:1.08rem}h5{font-size:1rem;margin:1rem 0 .4rem}'
            'table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;vertical-align:top;padding:.55rem;border:1px solid #d9e1e5;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#eef4f5}'
            '.table-wrap{overflow:auto;margin:1rem 0}.muted{color:#52606d}.case,.recommendation{border:1px solid #bdcdd3;border-radius:.5rem;padding:1rem;margin:1.2rem 0}.recommendation{background:#f8fbfb}'
            '.notice{padding:.8rem;border-left:4px solid #977638;background:#fff9ed}details{margin:.6rem 0}summary{cursor:pointer;color:#284b5a}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.82rem}a{color:#245466}dt{font-weight:bold}dd{margin:0 0 1rem}nav a{margin-right:1rem}'
            '@media print{body{max-width:none;font-size:10pt;margin:0;padding:0}.table-wrap{overflow:visible}table{font-size:9pt}thead{display:table-header-group}tr{break-inside:avoid}.recommendation{background:#fff}h2,h3,h4{break-after:avoid}nav{display:none}details{display:block}details>*{display:block}}</style></head><body>',
            f'<header><h1>{safe(run["title"])}</h1><p>课程学情分析 · 已冻结分析成果</p><p class="muted">分析记录 {safe(run["id"])} · 课程 {safe(run["course_id"])} · 生成 {safe(result["summary"]["generated_at"])}</p>',
            f'<p class="muted">输入 SHA-256：{safe(result["provenance"]["input_sha256"])}<br>结果模式：{safe(result["schema_version"])} · 导出模式：{safe(model["schema_version"])}</p></header>',
            '<nav><a href="#requirements">学习要求</a><a href="#performance">学情表现</a><a href="#cases">代表学生</a><a href="#personal">学习建议</a><a href="#methods">方法与限制</a><a href="#sources">来源与字段</a><a href="#guide">操作说明</a></nav>',
            f'<p class="notice">{safe(FORMULA_INDEPENDENCE)} 只处理本次上传的表格；所列出处不代表已读取原始课标或教材全文。缺失、待评分与异常记录不补为零。</p>',
            '<section><h2>本次分析范围</h2>',
            table([result["summary"]["scope"]], [("node_ids", "测量节点", lambda value, _: items(value)), ("class_ids", "班级", lambda value, _: items(value)),
                                                   ("student_count_denominator", "学生人数分母"), ("representative_student_ids", "代表学生", lambda value, _: items(value)),
                                                   ("group_count", "自选组数"), ("material_mapping_count", "人工映射材料数")]),
            table([result["summary"]], [("current_event_count", "所选当前事件"), ("eligible_scored_event_count", "有效已评分事件"), ("issue_count", "输入与范围问题数")]),
            '</section><section id="requirements"><h2>学习要求 · 学习内容、要求与材料出处</h2>']
    requirements = sections["requirements_and_material_mappings"]
    for topic in requirements["items"]:
        page.extend([f'<h3>{safe(topic["topic_id"])} · {safe(topic["topic"])}</h3><p><strong>主题目标：</strong>{safe(topic["objective"])}</p>',
                     table(topic["indicators"], [("id", "指标"), ("label", "观察要求"), ("standard_ref", "课标整理编号"),
                                                ("standard_page", "课标文件页／正文页"), ("textbook_page", "教材正文页"),
                                                ("question_ids", "实际关联题目", lambda value, _: items(value)), ("source_ref", "要求原始位置", lambda value, _: refs(value))])])
        for indicator in topic["indicators"]:
            page.append(f'<details><summary>{safe(indicator["id"])} · 已确认学习材料与教材</summary>{resources(indicator.get("material_references", []))}</details>')
    mapping_rows = [{"material_id": mid, **mapping} for mid, mapping in requirements.get("material_mappings", {}).items()]
    page.extend([f'<p class="notice">{safe(requirements["mapping_note"])}</p>', "<h3>教师本次确认映射</h3>",
                 table(mapping_rows, [("material_id", "材料"), ("topic_id", "主题"), ("indicator_ids", "指标", lambda value, _: items(value)),
                                      ("source_ref", "材料登记位置", lambda value, _: refs(value))]), "<h3>待核实的教材候选</h3>",
                 table(requirements.get("unmapped_textbook_candidates", []), [("id", "教材编号"), ("label", "登记内容"), ("source", "来源原文"),
                                                                            ("source_ref", "原始位置", lambda value, _: refs(value))]),
                 '</section><section id="performance"><h2>学情表现 · 个体、班级与所选群体表现</h2>',
                 "<h3>测量节点及覆盖范围</h3>",
                 table(sections["node_coverage_and_limits"], [("node_id", "节点"), ("node_date", "日期"), ("node_description", "节点说明"),
                     ("topics", "测量主题", lambda value, _: items(value)), ("node_expected_question_count", "原定题数"), ("selected_roster_count", "人数分母"),
                     ("recorded_events", "当前事件"), ("scored_events", "有效评分事件"), ("scored_event_denominator", "预期事件分母"), ("coverage_note", "分母说明")]),
                 "<h3>逐学生、节点和主题摘要</h3>",
                 table(sections["individuals_by_topic_and_node"], [("student_id", "学生"), ("class_id", "班级"), ("node_id", "节点"), ("topic", "主题"),
                     ("scored_events", "有效评分事件"), ("expected_event_denominator", "预期题数"), ("earned_points", "得分"), ("scored_max_points", "有效满分"),
                     ("score_percentage", "得分百分比（%）"), ("indicators", "指标明细", lambda value, _: '<details><summary>指标与证据</summary>' + table(value or [], [
                         ("indicator_id", "指标"), ("scored_events", "有效评分"), ("expected_event_denominator", "预期题数"), ("earned_points", "得分"),
                         ("scored_max_points", "有效满分"), ("score_percentage", "得分百分比（%）"), ("evidence_refs", "记录定位", lambda refs_value, _: refs(refs_value))]) + "</details>")]),
                 "<h3>班级和教师所选组</h3>",
                 table(sections["class_and_selected_group_performance"], [("id", "班级或组"), ("group_kind", "类型", lambda value, _: "班级" if value == "class" else "教师所选组"),
                     ("label", "组别说明"), ("node_id", "节点"), ("student_count_denominator", "人数分母"), ("eligible_scored_events", "有效评分事件"),
                     ("possible_event_denominator", "预期事件分母"), ("earned_points", "得分"), ("scored_max_points", "有效满分"),
                     ("scored_point_weighted_percentage", "分值加权百分比（%）"), ("scored_event_coverage", "评分覆盖（0—1）")]),
                 "<h3>同节点、共同题目上的班级比较</h3><p>逐题使用各班实际已评分分值计算百分比，再对共同题目等权取均值。各班覆盖率另列；不跨节点、不同题集推断进步或退步。</p>",
                 table(sections["comparable_class_metrics"], [("node_id", "节点"), ("class_id", "班级"), ("common_question_ids", "共同题目", lambda value, _: items(value)),
                     ("student_count_denominator", "人数分母"), ("scored_events", "有效评分事件"), ("possible_event_denominator", "预期事件分母"),
                     ("class_equal_question_mean_percentage", "共同题等权均值（%）"), ("scored_event_coverage", "评分覆盖（0—1）"),
                     ("question_metrics", "逐题依据", lambda value, _: '<details><summary>共同题得分与证据</summary>' + table(value or [], [("question_id", "题目"),
                         ("eligible_event_count", "已评分事件"), ("class_student_count_denominator", "人数分母"), ("earned_points", "得分"),
                         ("scored_max_points", "有效满分"), ("score_percentage", "百分比（%）"), ("evidence_refs", "位置", lambda refs_value, _: refs(refs_value))]) + "</details>")]),
                 '</section><section id="cases"><h2>代表学生 · 原始作答、评分点与个人观察</h2>'])
    for case in sections["representative_cases"]:
        page.extend([f'<article class="case"><h3>{safe(case["student_id"])} · {safe(case["class_id"])}</h3><p><strong>选择说明：</strong>{safe(case["role"])}；{safe(case["reason"])}</p>',
                     table(case.get("observations", []), [("node_id", "节点"), ("indicator_id", "指标"), ("indicator_label", "观察要求"), ("fact", "可核对事实"),
                         ("scored_events", "有效评分事件"), ("expected_event_denominator", "预期题数"), ("score_percentage", "百分比（%）"),
                         ("recommendation_id", "个人行动", lambda value, _: f'<a href="#{safe(value)}">查看材料、行动与观察条件</a>')]),
                     "<h4>所选节点全部当前有效作答</h4>",
                     table(case["evidence"], [("node_id", "节点"), ("question_id", "题目"), ("question_type", "题型"), ("status", "记录状态"),
                         ("score", "有效得分"), ("question_max_score", "有效满分"), ("final_answer", "学生最终作答"), ("solution_process", "学生解答过程"),
                         ("rubric", "原评分点与实际得分", lambda value, row: table([{"id": point["id"], "label": point["label"], "max_score": point["max_score"],
                             "actual": (row.get("rubric_scores") or {}).get(point["id"])} for point in (value or [])], [("id", "评分点"), ("label", "要求"), ("actual", "实际得分"), ("max_score", "原定满分")])),
                         ("evidence_ref", "证据定位", lambda value, _: refs(value))]),
                     "<p><strong>解释边界</strong></p>", items(case["limitations"])])
        if case.get("excluded_current_evidence_refs"):
            page.append('<p>异常当前记录仅保留定位，未用来诊断。</p>' + refs(case["excluded_current_evidence_refs"]))
        page.append("</article>")
    page.append('</section><section id="personal"><h2>学习建议 · 每名代表学生的个性化材料与行动建议</h2><p>以下每条建议只使用对应学生、所选节点与指标的记录；顺序在各学生内单独编号。材料与练习均可回到本次导入的实际登记行。</p>')
    personal = sections.get("individual_learning_recommendations", [])
    page.extend(recommendation(item) for item in personal)
    if not personal:
        page.append('<p class="muted">本次未选择代表学生，或所选节点没有可关联题目；未生成个人建议。</p>')
    page.append('<details><summary>所选群体教学建议</summary><p>下列建议描述所选群体，个人建议见上方各学生条目。</p>')
    page.extend(recommendation(item) for item in sections["evidence_based_recommendations"])
    page.append('</details></section><section id="methods"><h2>计算方法、问题与解释限制</h2><dl>')
    method_labels = {"scoring": "评分与修订选择", "coverage": "证据覆盖与分母", "independence_and_assistance": "帮助与独立性", "comparability": "比较范围",
                     "mastery": "解释边界", "recommendation_order": "个人建议顺序", "materials": "材料引用范围", "follow_up": "后续观察"}
    for key, value in sections["methods"].items():
        page.append(f'<dt>{safe(method_labels.get(key, key))}</dt><dd>{safe(value)}</dd>')
    page.extend(["</dl><h3>输入与范围问题清单</h3>", table(result["issues"], [("code", "问题编号"), ("severity", "级别"), ("table", "数据表"), ("message", "处理说明"),
                                                                                    ("refs", "原始位置", lambda value, _: refs(value))]),
                 '</section><section id="sources"><h2>来源、许可、输入说明与字段字典</h2><h3>输入文件与内容摘要</h3>',
                 table(sections["sources"], [("id", "来源编号"), ("filename", "上传文件名"), ("bytes", "字节数"), ("sha256", "SHA-256"), ("encoding", "编码")]),
                 "<h3>来源与许可状态</h3>", table(sections.get("source_permissions", []), [("filename", "输入文件"), ("license_status", "许可状态", lambda _, row: "输入表未采集"),
                     ("external_processing_permission", "外部处理许可", lambda _, row: "输入表未采集"), ("redistribution_permission", "再分发许可", lambda _, row: "输入表未采集"), ("note", "事实边界")]),
                 "<h3>输入索引备注</h3>", table(sections.get("data_notes", []), [("项目", "项目"), ("说明", "原文说明"), ("source_ref", "原始位置", lambda value, _: refs(value))]),
                 "<h3>原输入字段字典</h3>", table(sections.get("field_dictionary", []), [("数据表", "数据表"), ("字段名", "字段名"), ("类型", "类型"), ("含义", "含义"),
                     ("取值或格式", "取值或格式"), ("示例", "示例"), ("为空时", "为空时"), ("source_ref", "原始位置", lambda value, _: refs(value))]),
                 "<h3>成果输出字段说明</h3>", table(sections.get("output_schema", {}).get("sections", []), [("field", "结果分节"), ("type", "结构"), ("meaning", "含义")]),
                 table(sections.get("output_schema", {}).get("common_fields", []), [("field", "公共字段"), ("type", "类型"), ("meaning", "口径与含义")]),
                 '<details><summary>输入表头及导出关联规则</summary>',
                 table(sections.get("input_schema", {}).get("tables", []), [("name", "数据表"), ("filename", "CSV文件名"), ("required", "必需"), ("columns", "精确列名", lambda value, _: items(value))]),
                 items(sections.get("output_schema", {}).get("links", [])), '</details></section><section id="guide"><h2>阅读、复核与人工文档交付操作说明</h2>',
                 table(sections.get("operation_guide", []), [("step", "顺序"), ("title", "操作"), ("instruction", "说明")]),
                 '<p class="notice">本报告含模拟学生作答和材料登记内容；分享前按实际资料使用许可核对范围。报告不附上传原件、登录凭证或创建请求标识。完整机器结果与分片恢复规则见ZIP中的result.json、output_schema.json及manifest.json。</p>',
                 '</section></body></html>'])
    return "\n".join(page).encode("utf-8")


def export_run(run, fmt):
    if fmt not in FORMATS or run.get("status") != "confirmed" or not run.get("result"):
        raise CompetitionRunError("run must be confirmed before export")
    model = CompetitionRunStore.export_snapshot(None, run)
    raw = (json.dumps(model, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    token = "competition-" + run["id"].removeprefix("cr_")[:12]
    if fmt == "json":
        return raw, "application/json", token + ".json"
    sections = model["run"]["sections"]
    tables = {
        "summary": [model["run"]["summary"]], "issues": model["run"]["issues"],
        "requirements": sections["requirements_and_material_mappings"]["items"],
        "individuals": sections["individuals_by_topic_and_node"],
        "groups": sections["class_and_selected_group_performance"],
        "class_comparisons": sections["comparable_class_metrics"],
        "representative_cases": sections["representative_cases"],
        "recommendations": sections["evidence_based_recommendations"],
        "individual_recommendations": sections.get("individual_learning_recommendations", []),
        "node_coverage": sections["node_coverage_and_limits"],
        "field_dictionary": sections.get("field_dictionary", []), "data_notes": sections.get("data_notes", []),
        "sources": sections["sources"], "source_permissions": sections.get("source_permissions", []),
        "materials": sections.get("materials_used", []),
        "material_mappings": [{"material_id": mid, **mapping} for mid, mapping in sections["requirements_and_material_mappings"].get("material_mappings", {}).items()],
        "pending_textbook_mappings": sections["requirements_and_material_mappings"].get("unmapped_textbook_candidates", []),
        "methods": [{"field": field, "description": description} for field, description in sections["methods"].items()],
        "output_fields": sections.get("output_schema", {}).get("common_fields", []),
        "operation_guide": sections.get("operation_guide", []),
    }
    report = _html_report(run, model)
    if fmt == "html":
        return report, "text/html; charset=utf-8", token + ".html"
    filenames = {}
    chunk_rows = []
    for name, rows in tables.items():
        fields = sorted({key for row in rows for key in row}) if rows else []
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(fields or ["status"])
        chunk_manifest = []
        for row_number, row in enumerate(rows, 2):
            rendered = []
            for key in fields:
                cell = _safe_cell(row.get(key))
                if len(cell) > 12000:
                    pieces = [cell[offset:offset + 12000] for offset in range(0, len(cell), 12000)]
                    cell_sha256 = hashlib.sha256(cell.encode("utf-8")).hexdigest()
                    chunk_manifest.append({"row": row_number, "field": key, "chunks": len(pieces), "encoding": "utf-8", "sha256": cell_sha256})
                    rendered.append(f"[see large_cells.chunks.csv: {name} row {row_number} field {key}; {len(pieces)} parts; sha256={cell_sha256}]")
                    for part_number, part in enumerate(pieces, 1):
                        safe_part = _safe_cell(part)
                        chunk_rows.append({"table": name, "row": row_number, "field": key, "part": part_number,
                                           "part_count": len(pieces), "value": safe_part, "escaped": safe_part != part})
                else:
                    rendered.append(cell)
            writer.writerow(rendered)
        filenames[name + ".csv"] = output.getvalue().encode("utf-8-sig")
        if chunk_manifest:
            filenames[name + ".chunks.manifest.json"] = (json.dumps(chunk_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if chunk_rows:
        chunk_stream = io.StringIO(newline="")
        chunk_writer = csv.writer(chunk_stream, lineterminator="\r\n")
        chunk_writer.writerow(["table", "row", "field", "part", "part_count", "value", "escaped"])
        for item in chunk_rows:
            chunk_writer.writerow([item["table"], item["row"], item["field"], item["part"], item["part_count"], item["value"], str(item["escaped"]).lower()])
        filenames["large_cells.chunks.csv"] = chunk_stream.getvalue().encode("utf-8-sig")
    if fmt == "csv":
        return filenames["summary.csv"], "text/csv; charset=utf-8", token + "-summary.csv"
    filenames["result.json"] = raw
    filenames["report.html"] = report
    for name, value in {
            "input_schema.json": sections.get("input_schema", schema_description()),
            "output_schema.json": sections.get("output_schema", _output_schema()),
            "methods.json": sections["methods"],
            "field_dictionary.json": sections.get("field_dictionary", []),
            "data_notes.json": sections.get("data_notes", []),
            "source_permissions.json": sections.get("source_permissions", []),
            "input_manifest.json": {"schema_version": run["preview"]["schema_version"],
                                    "input_sha256": run["preview"]["input_sha256"], "sources": sections["sources"],
                                    "tables": run["preview"]["summary"]["table_rows"],
                                    "input_hash_rule": "SHA-256 of canonical JSON of sorted (filename, file SHA-256) pairs; UTF-8, sorted keys, compact separators.",
                                    "original_files_included": False},
            "scope.json": model["run"]["scope"],
    }.items():
        filenames[name] = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    filenames["README.txt"] = ("先打开 report.html 阅读分析成果；完整机器结果见 result.json，各 CSV 使用相同冻结分析与范围。\n"
                              "individual_recommendations.csv 为逐代表学生个人建议；recommendations.csv 为群体建议。空值不代表零。\n"
                              "字段、模式、原始字典、索引备注、计算方法、材料映射、来源许可和输入摘要均单列保存；来源未采集的许可不得推断为已获授权。\n"
                              "嵌套字段以 JSON 表示；超长单元格通过 large_cells.chunks.csv 分片，按表、行、字段及 part 重组。\n"
                              "escaped=true 时先去除 value 开头一个英文单引号，再连接所有分片，并核对分片清单的 sha256。\n"
                              "CSV 中公式样文本已前置英文单引号；result.json 保留原始字段值。manifest.json 含包内文件的SHA-256。\n"
                              "浏览器打印 report.html 可生成PDF；Word可打开HTML后人工整理保存DOCX；教学汇报PPT按分析成果、代表案例、共同题比较、方法与限制人工编排。操作步骤见 operation_guide.csv。\n"
                              "本包含模拟作答及材料登记内容，不附上传原件、登录凭证或创建请求标识。分享前按实际资料使用许可复核。\n").encode("utf-8")
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, value in sorted(filenames.items()):
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = ZIP_DEFLATED, 0o600 << 16
            archive.writestr(info, value)
        manifest = {"schema_version": EXPORT_SCHEMA_VERSION, "run_id": run["id"],
                    "input_sha256": run["preview"]["input_sha256"],
                    "run_schema_version": model["run"]["schema_version"],
                    "input_schema_version": run["preview"]["schema_version"],
                    "source_permissions": sections.get("source_permissions", []),
                    "privacy": {"original_files_included": False, "creation_request_tokens_included": False,
                                "contains": "scoped simulated student work and loaded material registration text"},
                    "large_cells": {"file": "large_cells.chunks.csv", "order": "part ascending, grouped by table/row/field",
                                    "escape_rule": "When escaped=true, remove exactly one leading apostrophe from value before concatenating. Compare sha256 with the table chunk manifest.",
                                    "value_rule": "Nested spreadsheet strings are formula-neutralized; result.json preserves original values."},
                    "files": {n: hashlib.sha256(v).hexdigest() for n, v in sorted(filenames.items())}}
        info = ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
        info.compress_type, info.external_attr = ZIP_DEFLATED, 0o600 << 16
        archive.writestr(info, (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return stream.getvalue(), "application/zip", token + ".zip"
