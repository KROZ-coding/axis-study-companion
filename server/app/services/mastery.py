"""Deterministic, versioned mastery rules over revisioned learning evidence.

The defaults are explicitly uncalibrated.  A question family contributes at most
one scored, first, independent attempt; assisted work and later exposure remain
visible without increasing the independent sample.  Teacher judgments are a
separate annotation and never replace the calculation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any


DEFAULT_RULE_VERSION = "mastery-v1"
EVIDENCE_SCHEMA_VERSION = "mastery.evidence.v1"


@dataclass(frozen=True, slots=True)
class MasteryRule:
    version: str
    min_independent_families: int = 3
    mastery_threshold_percent: int = 80
    developing_threshold_percent: int = 50
    percent_decimal_places: int = 1
    calibrated: bool = False
    family_policy: str = "first_independent_attempt_per_family"
    notice: str = "默认规则参数未经校准；结果仅描述当前独立作答证据，不代表能力测量结论。"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_RULES = {DEFAULT_RULE_VERSION: MasteryRule(DEFAULT_RULE_VERSION)}
_EXPOSURES = frozenset({"independent", "hinted", "redo", "answer_exposed", "unknown"})
_JUDGMENTS = frozenset({"mastered", "developing", "needs_practice", "insufficient_evidence", "needs_review"})


def get_rule(version: str = DEFAULT_RULE_VERSION) -> MasteryRule:
    try:
        return _RULES[version]
    except KeyError as error:
        raise ValueError(f"unsupported mastery rule version: {version}") from error


def list_rules() -> list[dict[str, Any]]:
    return [rule.to_dict() for _, rule in sorted(_RULES.items())]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be a non-empty identifier")
    return value.strip()


def _revision(value: Any, name: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _time(value: Any) -> tuple[float, str]:
    if value is None or value == "":
        return (0.0, "")
    if isinstance(value, bool):
        raise ValueError("evidence timestamp must be finite or an ISO datetime")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("evidence timestamp must be finite")
        return (float(value), datetime.fromtimestamp(value, tz=timezone.utc).isoformat())
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("evidence timestamp must be an ISO datetime") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.timestamp(), parsed.astimezone(timezone.utc).isoformat())
    raise ValueError("evidence timestamp must be finite or an ISO datetime")


def _score(row: Mapping[str, Any]) -> float | None:
    value = row.get("score_ratio")
    if value is None and row.get("score_units") is not None:
        maximum = row.get("max_score_units")
        earned = row["score_units"]
        if isinstance(earned, bool) or isinstance(maximum, bool):
            raise ValueError("score units must be numeric")
        if not isinstance(earned, (int, float)) or not isinstance(maximum, (int, float)):
            raise ValueError("score units require max_score_units")
        if not math.isfinite(earned) or not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("score units must be finite with positive max_score_units")
        value = earned / maximum
    if value is None and row.get("score") is not None:
        maximum = row.get("max_score", row.get("possible_score"))
        earned = row["score"]
        if isinstance(earned, bool) or isinstance(maximum, bool):
            raise ValueError("evidence score must be numeric")
        if not isinstance(earned, (int, float)) or not isinstance(maximum, (int, float)):
            raise ValueError("score evidence requires a numeric max_score")
        if not math.isfinite(earned) or not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("score evidence requires finite scores and positive max_score")
        value = earned / maximum
    if value is None and isinstance(row.get("correct"), bool):
        value = 1.0 if row["correct"] else 0.0
    if value is None and isinstance(row.get("is_correct"), bool):
        value = 1.0 if row["is_correct"] else 0.0
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("score_ratio must be a finite number")
    if not 0 <= value <= 1:
        raise ValueError("score_ratio must be between zero and one")
    return float(value)


def _exposure(row: Mapping[str, Any]) -> str:
    """Explicit assistance always wins over a claimed independent label."""
    value = row.get("exposure", row.get("exposure_type", "unknown"))
    if value not in _EXPOSURES:
        raise ValueError("unsupported evidence exposure")
    for key in ("answer_exposed", "solution_viewed", "hint_used", "is_redo", "assisted", "exposed", "retry", "independent_evidence"):
        if key in row and not isinstance(row[key], bool):
            raise ValueError(f"{key} must be a boolean")
    count = row.get("hint_count", row.get("hints_used", 0))
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("hint_count must be a non-negative integer")
    attempt = row.get("attempt_number", row.get("attempt_index", 1))
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt_number must be a positive integer")
    if row.get("answer_exposed") is True or row.get("solution_viewed") is True or row.get("exposed") is True or value == "answer_exposed":
        return "answer_exposed"
    if row.get("hint_used") is True or row.get("assisted") is True or count > 0 or value == "hinted":
        return "hinted"
    if row.get("is_redo") is True or row.get("retry") is True or attempt > 1 or value == "redo":
        return "redo"
    if row.get("independent_evidence") is False:
        return "unknown"
    if row.get("independent_evidence") is True or (
        all(key in row for key in ("assisted", "exposed", "retry"))
        and all(row[key] is False for key in ("assisted", "exposed", "retry"))
    ):
        return "independent"
    return value


def normalize_evidence(evidence: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select the latest revision of each evidence ID without retaining raw work.

    Repeated IDs are evidence revisions, not independent attempts. Conflicting
    records with the same ID and revision are rejected rather than resolved by
    input order. A retracted latest revision removes the logical evidence.
    """
    latest: dict[str, dict[str, Any]] = {}
    for raw in evidence:
        if not isinstance(raw, Mapping):
            raise ValueError("evidence rows must be mappings")
        evidence_id = _id(raw.get("evidence_id", raw.get("id")), "evidence_id")
        revision = _revision(raw.get("evidence_revision", raw.get("revision", 1)))
        goals = raw.get("goal_ids")
        if goals is None:
            goal = raw.get("goal_id")
            goals = [] if goal is None else [goal]
        if not isinstance(goals, (list, tuple)):
            raise ValueError("goal_ids must be an array")
        goal_ids = sorted({_id(value, "goal_id") for value in goals})
        question_id = _id(raw.get("question_id"), "question_id", optional=True)
        family_id = _id(raw.get("family_id", raw.get("question_family_id")), "family_id", optional=True)
        observed_at = raw.get("observed_at", raw.get("occurred_at", raw.get("created_at")))
        timestamp, normalized_time = _time(observed_at)
        deleted = raw.get("retracted", False) is True or raw.get("status") in {"retracted", "deleted", "void"}
        if "retracted" in raw and not isinstance(raw["retracted"], bool):
            raise ValueError("retracted must be a boolean")
        status = raw.get("status", "graded")
        if status not in {"graded", "corrected", "completed", "valid", "retracted", "deleted", "void", "pending", "submitted", "grading", "failed", "manual"}:
            raise ValueError("unsupported evidence status")
        score_ratio = _score(raw) if status in {"graded", "corrected", "completed", "valid"} else None
        record = {
            "evidence_id": evidence_id,
            "revision": revision,
            "student_id": _id(raw.get("student_id"), "student_id"),
            "goal_ids": goal_ids,
            "question_id": question_id,
            "family_id": family_id,
            "attempt_id": _id(raw.get("attempt_id", evidence_id), "attempt_id"),
            "attempt_number": raw.get("attempt_number", raw.get("attempt_index", 1)),
            "observed_at": normalized_time,
            "exposure": _exposure(raw),
            "score_ratio": score_ratio,
            "status": status,
            "retracted": deleted,
            "_timestamp": timestamp,
        }
        previous = latest.get(evidence_id)
        if previous is not None and revision == previous["revision"] and previous != record:
            raise ValueError("conflicting evidence revisions")
        if previous is None or revision > previous["revision"]:
            latest[evidence_id] = record
    records = sorted(latest.values(), key=lambda row: (row["_timestamp"], row["attempt_number"], row["evidence_id"]))
    for row in records:
        row.pop("_timestamp")
    return [row for row in records if not row["retracted"]]


def _teacher_marker(
    judgments: Iterable[Mapping[str, Any]], *, student_id: str, goal_id: str,
    evidence_revision: str, source_revision: int | None,
) -> dict[str, Any] | None:
    matching = []
    for row in judgments:
        if row.get("student_id") != student_id or row.get("goal_id") != goal_id:
            continue
        value = row.get("judgment", row.get("status"))
        if value not in _JUDGMENTS:
            raise ValueError("unsupported teacher judgment")
        judgment_id = _id(row.get("judgment_id", row.get("id")), "judgment_id")
        revision = _revision(row.get("revision", 1))
        timestamp, _ = _time(row.get("created_at", row.get("observed_at")))
        based_on = row.get("evidence_revision", row.get("based_on_evidence_revision"))
        based_on_source = row.get("source_revision", row.get("based_on_source_revision"))
        if based_on_source is not None:
            based_on_source = _revision(based_on_source, "source_revision")
        if based_on is not None and not isinstance(based_on, str):
            raise ValueError("teacher judgment evidence revision must be text")
        matching.append((timestamp, revision, judgment_id, {
            "judgment_id": judgment_id,
            "revision": revision,
            "judgment": value,
            "based_on_evidence_revision": based_on,
            "based_on_source_revision": based_on_source,
            "evidence_changed": (based_on != evidence_revision) if based_on is not None else (
                based_on_source != source_revision if based_on_source is not None and source_revision is not None else None
            ),
            "separate_from_derived": True,
        }))
    if not matching:
        return None
    return max(matching, key=lambda item: item[:3])[3]


def _compute_normalized(
    records: list[dict[str, Any]], *, student_id: str, goal_id: str, rule: MasteryRule,
    teacher_judgments: Iterable[Mapping[str, Any]], source_revision: int | None,
) -> dict[str, Any]:
    relevant = [row for row in records if row["student_id"] == student_id and goal_id in row["goal_ids"]]
    evidence_revision = hashlib.sha256(_json({"schema_version": EVIDENCE_SCHEMA_VERSION, "evidence": relevant}).encode("utf-8")).hexdigest()
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in relevant:
        # Unknown family metadata must not manufacture an independent sample.
        key = row["family_id"] or f"unassigned:{row['question_id'] or row['evidence_id']}"
        by_family[key].append(row)
    families = []
    counts = {exposure: 0 for exposure in sorted(_EXPOSURES)}
    scores: list[Decimal] = []
    unique_attempts: set[tuple[str, str, str | None]] = set()
    for family_id, rows in sorted(by_family.items()):
        family_observations = []
        first = True
        eligible = None
        for row in rows:
            # Several evidence records can describe the same attempt. They do not
            # become new observations or create another independent sample.
            attempt_key = (family_id, row["attempt_id"], row["question_id"])
            if attempt_key in unique_attempts:
                continue
            unique_attempts.add(attempt_key)
            exposure = row["exposure"]
            if not first and exposure == "independent":
                exposure = "redo"
            if row["family_id"] is None and exposure == "independent":
                exposure = "unknown"
            counts[exposure] += 1
            if first and exposure == "independent" and row["score_ratio"] is not None:
                eligible = row
                scores.append(Decimal(str(row["score_ratio"])))
            family_observations.append({
                "evidence_id": row["evidence_id"], "revision": row["revision"],
                "attempt_id": row["attempt_id"], "exposure": exposure,
                "score_ratio": row["score_ratio"],
            })
            first = False
        families.append({
            "family_id": family_id, "family_assigned": rows[0]["family_id"] is not None,
            "independent_evidence_id": eligible["evidence_id"] if eligible else None,
            "independent_score_ratio": eligible["score_ratio"] if eligible else None,
            "observations": family_observations,
        })
    percent = None
    status = "insufficient_evidence"
    if len(scores) >= rule.min_independent_families:
        quantum = Decimal(1).scaleb(-rule.percent_decimal_places)
        percent = float((sum(scores) / Decimal(len(scores)) * 100).quantize(quantum, rounding=ROUND_HALF_UP))
        if percent >= rule.mastery_threshold_percent:
            status = "mastered"
        elif percent >= rule.developing_threshold_percent:
            status = "developing"
        else:
            status = "needs_practice"
    return {
        "student_id": student_id, "goal_id": goal_id,
        "rule_version": rule.version, "calibrated": rule.calibrated,
        "source_revision": source_revision, "evidence_revision": evidence_revision,
        "percent": percent, "status": status,
        "independent_families": len(scores), "observed_families": len(by_family),
        "required_independent_families": rule.min_independent_families,
        "observations": counts,
        "evidence_refs": [{"evidence_id": row["evidence_id"], "revision": row["revision"]} for row in relevant],
        "families": families,
        "teacher_judgment": _teacher_marker(
            teacher_judgments, student_id=student_id, goal_id=goal_id,
            evidence_revision=evidence_revision, source_revision=source_revision,
        ),
    }


def compute_mastery(
    evidence: Iterable[Mapping[str, Any]], *, student_id: str, goal_id: str,
    rule_version: str = DEFAULT_RULE_VERSION,
    teacher_judgments: Iterable[Mapping[str, Any]] = (), source_revision: int | None = None,
) -> dict[str, Any]:
    """Recompute a student/goal result from current evidence, without cached overrides."""
    student_id = _id(student_id, "student_id")
    goal_id = _id(goal_id, "goal_id")
    if source_revision is not None:
        source_revision = _revision(source_revision, "source_revision")
    return _compute_normalized(
        normalize_evidence(evidence), student_id=student_id, goal_id=goal_id,
        rule=get_rule(rule_version), teacher_judgments=teacher_judgments, source_revision=source_revision,
    )


def build_mastery(
    evidence: Iterable[Mapping[str, Any]], *, students: Iterable[str | Mapping[str, Any]] = (),
    goals: Iterable[str | Mapping[str, Any]] = (), teacher_judgments: Iterable[Mapping[str, Any]] = (),
    rule_version: str = DEFAULT_RULE_VERSION, source_revision: int | None = None,
) -> list[dict[str, Any]]:
    """Build a stable student × goal matrix, including genuinely missing evidence."""
    records = normalize_evidence(evidence)
    student_ids = {_id(row.get("id", row.get("student_id")) if isinstance(row, Mapping) else row, "student_id") for row in students}
    goal_ids = {_id(row.get("id", row.get("goal_id")) if isinstance(row, Mapping) else row, "goal_id") for row in goals}
    if not student_ids:
        student_ids = {row["student_id"] for row in records}
    if not goal_ids:
        goal_ids = {goal for row in records for goal in row["goal_ids"]}
    if source_revision is not None:
        source_revision = _revision(source_revision, "source_revision")
    rule = get_rule(rule_version)
    judgments = list(teacher_judgments)
    return [
        _compute_normalized(records, student_id=student, goal_id=goal, rule=rule,
                            teacher_judgments=judgments, source_revision=source_revision)
        for student in sorted(student_ids) for goal in sorted(goal_ids)
    ]
