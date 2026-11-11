from __future__ import annotations

from collections.abc import Mapping
import csv
import io
import json
from typing import Any

from .content import StudyScope, scoped_kps, scoped_materials, scoped_records, scoped_students
from .insights import STATUS, build_insights

EXPORT_FIELDS = (
    "student_id", "student_name", "knowledge_point_id", "knowledge_point", "status", "right_count",
    "record_count", "evidence_ids", "material_id", "material_title", "reason", "next_observation", "sample_label",
)


def _analysis_rows(
    data: Mapping[str, Any], scope: StudyScope, insights: Mapping[str, Any],
) -> list[dict[str, Any]]:
    kp_by_id = {row["id"]: row for row in scoped_kps(data, scope)}
    rows = []
    for student in scoped_students(data, scope):
        for kp_id in scope.kp_ids:
            stat = insights["stats"][student["id"]][kp_id]
            material = insights["suggestions"][student["id"]].get(kp_id)
            rows.append({
                "student_id": student["id"], "student_name": student["name"],
                "knowledge_point_id": kp_id, "knowledge_point": kp_by_id[kp_id]["name"],
                "status": STATUS[stat["status"]]["label"], "right_count": stat["right"], "record_count": stat["n"],
                "evidence_ids": "|".join(row["id"] for row in stat["records"]),
                "material_id": material["id"] if material else None,
                "material_title": material["title"] if material else None,
                "reason": "先补充记录核实" if stat["status"] == "none" else "依据已登记的错误与知识点选择材料",
                "next_observation": "阅读讲解后完成下一题并说明步骤；新练习单独保存，不用于宣称学习效果",
                "sample_label": data["sampleLabel"],
            })
    return rows


def export_rows(data: Mapping[str, Any], scope: StudyScope) -> list[dict[str, Any]]:
    return _analysis_rows(data, scope, build_insights(data, scope))


def export_payload(data: Mapping[str, Any], scope: StudyScope) -> dict[str, Any]:
    rows = export_rows(data, scope)
    scope_info: dict[str, Any] = {
        "topic": scope.topic,
        "student_id": scope.student_ids[0] if len(scope.student_ids) == 1 else None,
    }
    # Retain the old JSON shape for legacy ranges, recording any narrower range explicitly.
    if set(scope.student_ids) != {row["id"] for row in data["students"]} and len(scope.student_ids) != 1:
        scope_info["student_ids"] = list(scope.student_ids)
    topic_kps = {row["id"] for row in data["kps"] if scope.topic == "all" or row["topic"] == scope.topic}
    if set(scope.kp_ids) != topic_kps:
        scope_info["kp_ids"] = list(scope.kp_ids)
    if scope.date_from is not None or scope.date_to is not None:
        scope_info.update(date_from=scope.date_from, date_to=scope.date_to)
    return {
        "sample_label": data["sampleLabel"], "as_of": data["asOf"], "rule": data["rule"],
        "scope": scope_info,
        "learning_contents": scoped_kps(data, scope), "materials": scoped_materials(data, scope),
        "students": scoped_students(data, scope), "learning_analysis": rows,
        "learning_recommendations": [
            {key: row[key] for key in ("student_id", "knowledge_point_id", "material_id", "reason", "next_observation")}
            for row in rows
        ],
        "records": scoped_records(data, scope),
        "field_notes": {
            "record_count": "该知识点有效记录数；零记录不是零分", "evidence_ids": "对应 records[].id，以 | 分隔",
            "material_id": "对应 materials[].id；教材引用是样例登记", "status": "演示阈值，非官方评测结论",
        },
    }


def export_json(data: Mapping[str, Any], scope: StudyScope) -> str:
    return json.dumps(export_payload(data, scope), ensure_ascii=False, indent=2)


def export_csv(data: Mapping[str, Any], scope: StudyScope) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    writer.writerows(export_rows(data, scope))
    return "\ufeff" + output.getvalue()
