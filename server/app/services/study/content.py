from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException


class ScopeNotFoundError(KeyError):
    """A requested student, topic or knowledge point is outside the dataset."""


@dataclass(frozen=True, slots=True)
class StudyScope:
    topic: str
    student_ids: tuple[str, ...]
    kp_ids: tuple[str, ...]
    date_from: str | None = None
    date_to: str | None = None


def resolve_scope(
    data: Mapping[str, Any],
    *,
    topic: str = "all",
    student_ids: Sequence[str] | None = None,
    kp_ids: Sequence[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> StudyScope:
    """Validate an explicit range; None selects all, while [] selects none."""
    if topic != "all" and topic not in {row["id"] for row in data["topics"]}:
        raise ScopeNotFoundError("主题不存在")
    all_students = [row["id"] for row in data["students"]]
    if student_ids is not None and set(student_ids) - set(all_students):
        raise ScopeNotFoundError("样例中没有该学生")
    all_kps = [row["id"] for row in data["kps"] if topic == "all" or row["topic"] == topic]
    if kp_ids is not None and set(kp_ids) - set(all_kps):
        raise ScopeNotFoundError("范围中没有该知识点")
    if date_from is not None:
        date_from = date.fromisoformat(date_from).isoformat()
    if date_to is not None:
        date_to = date.fromisoformat(date_to).isoformat()
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("开始日期不能晚于结束日期")
    return StudyScope(
        topic=topic,
        student_ids=tuple(sid for sid in all_students if student_ids is None or sid in student_ids),
        kp_ids=tuple(kid for kid in all_kps if kp_ids is None or kid in kp_ids),
        date_from=date_from,
        date_to=date_to,
    )


def scoped_students(data: Mapping[str, Any], scope: StudyScope) -> list[dict[str, Any]]:
    return deepcopy([row for row in data["students"] if row["id"] in scope.student_ids])


def scoped_kps(data: Mapping[str, Any], scope: StudyScope) -> list[dict[str, Any]]:
    return deepcopy([row for row in data["kps"] if row["id"] in scope.kp_ids])


def scoped_records(data: Mapping[str, Any], scope: StudyScope) -> list[dict[str, Any]]:
    rows = [
        row for row in data["records"]
        if row["sid"] in scope.student_ids and row["kp"] in scope.kp_ids
        and (scope.date_from is None or row["date"] >= scope.date_from)
        and (scope.date_to is None or row["date"] <= scope.date_to)
    ]
    return deepcopy(sorted(rows, key=lambda row: row["date"]))


def scoped_materials(data: Mapping[str, Any], scope: StudyScope) -> list[dict[str, Any]]:
    topics = {row["topic"] for row in data["kps"] if row["id"] in scope.kp_ids}
    return deepcopy([
        row for row in data["materials"]
        if row.get("kp") in scope.kp_ids or (row.get("kp") is None and row["topic"] in topics)
    ])


def build_context(data: Mapping[str, Any], scope: StudyScope) -> dict[str, Any]:
    """Return scoped navigation metadata, never full records or answer keys."""
    topic_ids = {row["topic"] for row in data["kps"] if row["id"] in scope.kp_ids}
    return {
        **{key: deepcopy(data[key]) for key in ("classId", "className", "asOf", "minN", "rule", "sampleLabel") if key in data},
        "scope": {
            "topic": scope.topic, "student_ids": list(scope.student_ids), "kp_ids": list(scope.kp_ids),
            "date_from": scope.date_from, "date_to": scope.date_to,
        },
        "students": scoped_students(data, scope),
        "topics": deepcopy([row for row in data["topics"] if row["id"] in topic_ids]),
        "kps": scoped_kps(data, scope),
    }


def get_material(data: Mapping[str, Any], material_id: str) -> dict[str, Any]:
    material = next((row for row in data["materials"] if row["id"] == material_id), None)
    if material is None:
        raise HTTPException(status_code=404, detail="材料不存在")
    return deepcopy(material)


def get_practice(
    data: Mapping[str, Any], practice_id: str, *, include_answer: bool = False,
) -> dict[str, Any]:
    practice = next((row for row in data["practice"].values() if row["id"] == practice_id), None)
    if practice is None:
        raise HTTPException(status_code=404, detail="练习不存在")
    if include_answer:
        return deepcopy(practice)
    # Whitelist student-facing content so later grading fields cannot leak by default.
    return {key: deepcopy(practice[key]) for key in ("id", "stem", "options")}
