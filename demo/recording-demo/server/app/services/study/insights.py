from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .content import ScopeNotFoundError, StudyScope, resolve_scope, scoped_records

MIN_N = 3
RULE = "有效记录少于 3 条显示“依据不足”；正确率低于 50% 为薄弱，50% 至 80% 为待巩固，80% 及以上为已掌握。"
STATUS = {
    "weak": {"label": "薄弱", "rank": 0},
    "shaky": {"label": "待巩固", "rank": 1},
    "none": {"label": "依据不足", "rank": 2},
    "ok": {"label": "已掌握", "rank": 3},
}


def status_for(n: int, right: int, *, min_n: int = MIN_N) -> str:
    if n == 0 or n < min_n:
        return "none"
    rate = right / n
    return "weak" if rate < 0.5 else "shaky" if rate < 0.8 else "ok"


def focus_report(
    student_stats: Mapping[str, dict[str, Any]], kp_ids: Sequence[str],
) -> dict[str, Any]:
    stats = [student_stats[kp_id] for kp_id in kp_ids]

    def rate(stat: dict[str, Any]) -> float:
        return stat["right"] / stat["n"] if stat["n"] else 1.0

    need = sorted(
        (stat for stat in stats if stat["status"] in {"weak", "shaky"}),
        key=lambda stat: (STATUS[stat["status"]]["rank"], rate(stat), -stat["n"]),
    )
    none = [stat for stat in stats if stat["status"] == "none"]
    overall = need[0]["status"] if need else "none" if none or not stats else "ok"
    return {"focus": need[0] if need else None, "need": need, "none": none, "stats": stats, "overall": overall}


def build_insights(data: Mapping[str, Any], scope: StudyScope) -> dict[str, Any]:
    """Recompute history inside the range; never fold in new practice facts."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in scoped_records(data, scope):
        grouped.setdefault((record["sid"], record["kp"]), []).append(record)
    stats: dict[str, dict[str, dict[str, Any]]] = {}
    for sid in scope.student_ids:
        stats[sid] = {}
        for kp_id in scope.kp_ids:
            rows = grouped.get((sid, kp_id), [])
            wrongs = [row for row in rows if not row["correct"]]
            counts = Counter(row["item"] for row in wrongs)
            top_item = max(
                counts,
                key=lambda item_id: (counts[item_id], max(i for i, row in enumerate(wrongs) if row["item"] == item_id)),
            ) if counts else None
            right = sum(row["correct"] for row in rows)
            stats[sid][kp_id] = {
                "sid": sid, "kp": kp_id, "n": len(rows), "right": right,
                "status": status_for(len(rows), right, min_n=data.get("minN", MIN_N)),
                "records": rows, "wrongs": wrongs, "topItem": top_item,
                "topCount": counts[top_item] if top_item else 0, "lastWrong": wrongs[-1] if wrongs else None,
            }

    materials = data.get("materials", [])
    material_by_item = {row["forItem"]: row for row in materials if row["type"] == "讲解" and row.get("forItem")}
    material_by_kp: dict[str, dict[str, Any]] = {}
    for material in materials:
        if material["type"] == "练习" and material.get("kp") in scope.kp_ids:
            material_by_kp.setdefault(material["kp"], material)
    suggestions: dict[str, dict[str, dict[str, Any]]] = {}
    recommended: dict[str, list[str]] = {}
    for sid in scope.student_ids:
        suggestions[sid] = {}
        for kp_id in scope.kp_ids:
            stat = stats[sid][kp_id]
            suggestion = material_by_item.get(stat["topItem"]) or material_by_kp.get(kp_id)
            if suggestion is not None and suggestion.get("kp") == kp_id:
                # Results are detached from the source dataset, including material bodies.
                suggestions[sid][kp_id] = deepcopy(suggestion)
        best = focus_report(stats[sid], scope.kp_ids)
        if best["focus"]:
            material = suggestions[sid].get(best["focus"]["kp"])
            if material:
                recommended.setdefault(material["id"], []).append(sid)
    return {"stats": stats, "suggestions": suggestions, "recommended": recommended}


def student_report(
    data: Mapping[str, Any], student_id: str, *, scope: StudyScope | None = None,
    kp_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if scope is not None and student_id not in scope.student_ids:
        raise ScopeNotFoundError("范围中没有该学生")
    if scope is not None and kp_ids is not None and set(kp_ids) - set(scope.kp_ids):
        raise ScopeNotFoundError("范围中没有该知识点")
    selected = resolve_scope(
        data,
        topic=scope.topic if scope is not None else "all",
        student_ids=[student_id],
        kp_ids=kp_ids if kp_ids is not None else scope.kp_ids if scope is not None else None,
        date_from=scope.date_from if scope is not None else None,
        date_to=scope.date_to if scope is not None else None,
    )
    insights = build_insights(data, selected)
    return focus_report(insights["stats"][student_id], selected.kp_ids)
