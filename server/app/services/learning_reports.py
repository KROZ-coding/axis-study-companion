"""Safe canonical report input, implemented inside the learning repository boundary.

This module reads only LearningStore's own facts. It never projects the task DTO
(which contains protected work and answer keys), never reads platform tables,
and does not render files. Every task contributing metadata or statistics passes
through the store's current authorization gate using the original actor ID.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re
import sqlite3
from typing import Any


_SCOPE_FIELDS = frozenset({"kind", "course_id", "class_id", "student_id", "goal_id", "question_bank_id"})
_REQUIRED = {"class": "class_id", "student": "student_id", "goal": "goal_id", "question_bank": "question_bank_id"}
_JUDGMENTS = frozenset({"mastered", "developing", "needs_practice", "insufficient_evidence", "needs_review"})
_QUESTION_TYPES = frozenset({"single_choice", "multi_choice", "judge", "fill", "short_answer", "case", "text"})
_IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_HASH = re.compile(r"^[a-fA-F0-9]{64}$")
_ANCHOR = re.compile(r"^[A-Za-z0-9_ .:!#/@,;()\[\]+\-]{1,500}$")


def _scope(store: Any, value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) - _SCOPE_FIELDS:
        raise ValueError("unsupported learning report scope fields")
    kind = value.get("kind")
    if kind not in _REQUIRED:
        raise ValueError("unsupported learning report scope kind")
    result = {"kind": kind}
    for key in sorted(_SCOPE_FIELDS - {"kind"}):
        if value.get(key) is not None:
            result[key] = store._id(value[key], key)
    if _REQUIRED[kind] not in result:
        raise ValueError(f"{kind} reports require {_REQUIRED[kind]}")
    forbidden = {
        "class": {"student_id", "goal_id", "question_bank_id"},
        "student": {"goal_id", "question_bank_id"},
        "goal": {"question_bank_id"},
        "question_bank": {"student_id", "goal_id"},
    }
    if forbidden[kind].intersection(result):
        raise ValueError("conflicting learning report scope identifiers")
    if kind == "question_bank" and "course_id" not in result:
        raise ValueError("question bank reports require course_id")
    return result


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENT.fullmatch(value) else None


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    return value if type(value) is int and value >= minimum else None


def _timestamp(value: Any) -> float | None:
    return float(value) if type(value) in {int, float} and math.isfinite(value) else None


def _permission(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value in {"approved", "allowed"})


def _source_revision(db: sqlite3.Connection) -> int:
    try:
        row = db.execute("SELECT revision FROM learning_revision WHERE id=1").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            raise NotImplementedError("versioned learning report input requires learning schema v2") from error
        raise
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise RuntimeError("learning source revision is missing or invalid")
    return row[0]


def _require_scope(store: Any, db: sqlite3.Connection, scope: Mapping[str, Any], actor_id: str) -> None:
    # Course material ownership does not imply any class access. Remove class_id
    # from this authorization probe; aggregate task usage is checked separately.
    probe = dict(scope)
    if scope["kind"] == "question_bank":
        probe.pop("class_id", None)
    elif scope["kind"] == "goal":
        goal = db.execute(
            "SELECT owner_id,class_id,course_id FROM learning_goals WHERE id=?", (scope["goal_id"],),
        ).fetchone()
        if goal is None or any(scope.get(key) is not None and scope[key] != goal[key] for key in ("class_id", "course_id")):
            raise LookupError("learning report goal is absent or inaccessible")
        probe.update(owner_id=goal["owner_id"], class_id=goal["class_id"], course_id=goal["course_id"])
    store._require_access(actor_id, probe, write=False)


def _question_meta(
    row: Mapping[str, Any], frozen: Mapping[str, Any] | None = None, *, question_bank_id: str | None = None,
) -> dict[str, Any]:
    content = _object(row.get("content_json"))
    frozen = dict(frozen or {})
    values = (frozen, row, content)
    family_id = next((_identifier(value.get("family_id")) for value in values if _identifier(value.get("family_id"))), None)
    family_sha = next((value["family_sha"].lower() for value in values
                       if isinstance(value.get("family_sha"), str) and _HASH.fullmatch(value["family_sha"])), None)
    family_id = family_id or family_sha
    maximum = next((_integer(value.get(key), minimum=1) for value in values for key in ("points_units", "max_score_units", "score_units")
                    if _integer(value.get(key), minimum=1) is not None), None)
    result = {"id": row["question_id"], "version": int(row["version"]), "family_id": family_id,
              "family_sha": family_sha, "status": row["status"], "goal_ids": [],
              "course_id": row.get("course_id"), "points_units": maximum}
    kind = next((value["type"] for value in values if value.get("type") in _QUESTION_TYPES), None)
    if kind is not None:
        result["type"] = kind
    bank_id = question_bank_id or next((_identifier(value.get("question_bank_id", value.get("bank_id"))) for value in values
                                      if _identifier(value.get("question_bank_id", value.get("bank_id")))), None)
    if bank_id is not None:
        result["question_bank_id"] = bank_id
    return result


def _sources(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project structural attribution only; license notes and source text stay private."""
    result = []
    rows = question.get("source_anchors", question.get("sources", []))
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_id = _identifier(row.get("block_id", row.get("source_id", row.get("id"))))
        locator = row.get("anchor", row.get("locator"))
        if source_id is None or not isinstance(locator, str) or not _ANCHOR.fullmatch(locator):
            continue
        safe = {"id": source_id, "locator": locator}
        file_id = _identifier(row.get("file_id"))
        if file_id is not None:
            safe["file_id"] = file_id
        digest = row.get("sha256")
        if isinstance(digest, str) and _HASH.fullmatch(digest):
            safe["sha256"] = digest.lower()
        permission = row.get("export_allowed", row.get("permission"))
        if _permission(permission):
            safe["export_allowed"] = True
        # An omitted or denied permission never becomes an invented approval.
        result.append(safe)
    return result


def _events(db: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT event_type,question_id,occurred_at FROM task_events "
        "WHERE task_id=? AND event_type IN ('help','exposure') ORDER BY occurred_at,id", (task_id,),
    ).fetchall()


def _evidence(
    task: Mapping[str, Any], item: Mapping[str, Any], question: Mapping[str, Any], events: list[sqlite3.Row],
) -> dict[str, Any] | None:
    maximum = question["points_units"]
    grading = _object(item.get("grading_json"))
    earned = _integer(item.get("score_units"))
    if earned is None:
        earned = _integer(grading.get("score_units"))
    occurred = _timestamp(item.get("submitted_at"))
    if maximum is None or earned is None or earned > maximum or occurred is None:
        return None
    prior = [event for event in events if event["occurred_at"] <= occurred
             and event["question_id"] in (None, item["question_id"])]
    assisted = any(event["event_type"] == "help" for event in prior)
    retry = item["kind"] == "retry" or item["attempt_no"] > 1
    exposed = retry or any(event["event_type"] == "exposure" for event in prior)
    independent = item["attempt_no"] == 1 and not (assisted or exposed or retry)
    exposure = "answer_exposed" if exposed else "hinted" if assisted else "independent" if independent else "unknown"
    evidence_id = "evidence_" + hashlib.sha256(
        json.dumps([item["attempt_id"], item["question_id"]], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "id": evidence_id, "revision": int(item["attempt_revision"]),
        "student_id": task["student_id"], "goal_id": task["goal_id"],
        "question_id": item["question_id"], "question_version": int(item["question_version"]),
        "family_id": question["family_id"], "family_sha": question["family_sha"],
        "attempt_id": item["attempt_id"], "attempt_number": int(item["attempt_no"]),
        "score_units": earned, "max_score_units": maximum,
        "assisted": assisted, "exposed": exposed, "retry": retry,
        "independent_evidence": independent, "exposure": exposure,
        "status": "graded", "occurred_at": occurred,
    }


def _task_items(db: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT q.*,ti.question_version,ti.frozen_json
           FROM task_items ti JOIN question_versions q
             ON q.question_id=ti.question_id AND q.version=ti.question_version
           WHERE ti.task_id=? AND q.status NOT IN ('quarantined','retired') ORDER BY ti.position,ti.question_id""", (task_id,),
    ).fetchall()


def _graded_items(db: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT ai.question_id,ai.score_units,ai.grading_json,a.id AS attempt_id,a.revision AS attempt_revision,
                  a.attempt_no,a.kind,a.submitted_at,a.independent_evidence,ti.question_version
           FROM attempts a JOIN attempt_items ai ON ai.attempt_id=a.id AND ai.task_id=a.task_id
           JOIN task_items ti ON ti.task_id=ai.task_id AND ti.question_id=ai.question_id
           WHERE a.task_id=? AND a.status='graded' AND ai.status='graded'
           ORDER BY a.submitted_at,a.attempt_no,a.id,ti.position""", (task_id,),
    ).fetchall()


def _authorized_task(store: Any, db: sqlite3.Connection, task_id: str, actor_id: str) -> dict[str, Any] | None:
    try:
        task = dict(store._task_row(db, task_id, actor_id))
        store._require_access(actor_id, {"kind": "task", "task_id": task_id, "goal_id": task["goal_id"],
                                       "student_id": task["student_id"], "class_id": task.get("class_id"),
                                       "course_id": task.get("course_id"), "owner_id": task.get("owner_id")}, write=False)
        return task
    except (LookupError, PermissionError):
        return None


def _goals_and_tasks(store: Any, db: sqlite3.Connection, scope: Mapping[str, Any], actor_id: str):
    candidates = db.execute(
        """SELECT g.* FROM learning_goals g WHERE (? = 0 OR g.id=?)
           AND (? = 0 OR g.class_id=?) AND (? = 0 OR g.course_id=?) ORDER BY g.id""",
        ("goal_id" in scope, scope.get("goal_id"), "class_id" in scope, scope.get("class_id"),
         "course_id" in scope, scope.get("course_id")),
    ).fetchall()
    goals, tasks = {}, []
    for raw in candidates:
        goal = dict(raw)
        goal_authorized = False
        try:
            store._goal_row(db, goal["id"], actor_id)
            store._require_access(actor_id, {"kind": "goal", "goal_id": goal["id"], "owner_id": goal["owner_id"],
                                           "class_id": goal["class_id"], "course_id": goal["course_id"]}, write=False)
            goal_authorized = True
        except (LookupError, PermissionError):
            pass
        task_rows = db.execute(
            """SELECT id FROM learning_tasks WHERE goal_id=? AND (? = 0 OR student_id=?) ORDER BY id""",
            (goal["id"], bool(scope.get("student_id")), scope.get("student_id")),
        ).fetchall()
        accepted = []
        for row in task_rows:
            task = _authorized_task(store, db, row["id"], actor_id)
            if task is not None:
                accepted.append(task)
        # Empty personal reports do not acquire another student's goal metadata
        # merely because a teacher can access the surrounding class.
        if accepted or (goal_authorized and scope["kind"] in {"class", "goal"} and "student_id" not in scope):
            goals[goal["id"]] = {"id": goal["id"], "title": goal["title"], "version": int(goal["version"]),
                                   "class_id": goal["class_id"], "course_id": goal["course_id"]}
            tasks.extend(accepted)
    if scope["kind"] == "goal" and not goals:
        raise LookupError("learning report goal is absent or inaccessible")
    return goals, tasks


def _teacher_judgments(db: sqlite3.Connection, pairs: set[tuple[str, str]]) -> list[dict[str, Any]]:
    result = []
    for goal_id in sorted({goal for _, goal in pairs}):
        try:
            rows = db.execute(
                "SELECT id,student_id,goal_id,knowledge_point_id,judgment_json,created_at "
                "FROM teacher_judgments WHERE goal_id=? ORDER BY created_at,id", (goal_id,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return []
            raise
        for row in rows:
            if (row["student_id"], row["goal_id"]) not in pairs:
                continue
            raw = _object(row["judgment_json"])
            judgment = raw.get("judgment", raw.get("status"))
            if judgment not in _JUDGMENTS or _timestamp(row["created_at"]) is None:
                continue
            marker = {"id": row["id"], "student_id": row["student_id"], "goal_id": row["goal_id"],
                      "judgment": judgment, "revision": _integer(raw.get("revision"), minimum=1) or 1,
                      "created_at": float(row["created_at"])}
            kp = _identifier(row["knowledge_point_id"])
            if kp is not None:
                marker["knowledge_point_id"] = kp
            evidence_revision = raw.get("evidence_revision", raw.get("based_on_evidence_revision"))
            if isinstance(evidence_revision, str) and _HASH.fullmatch(evidence_revision):
                marker["evidence_revision"] = evidence_revision.lower()
            source_revision = _integer(raw.get("source_revision", raw.get("based_on_source_revision")))
            if source_revision is not None:
                marker["source_revision"] = source_revision
            result.append(marker)
    return sorted(result, key=lambda row: (row["created_at"], row["id"]))


def _question_bank(store: Any, db: sqlite3.Connection, scope: Mapping[str, Any], actor_id: str) -> dict[str, Any]:
    # Without a platform checker a standalone LearningStore may only expose the
    # actor's own authoring records, never every author in an arbitrary course.
    versions = db.execute(
        """SELECT * FROM question_versions WHERE course_id=? AND status NOT IN ('quarantined','retired')
           AND (? = 0 OR owner_id=?) ORDER BY question_id,version""",
        (scope["course_id"], getattr(store, "_access_checker", None) is None, actor_id),
    ).fetchall()
    questions, source_rows, usage = [], {}, {}
    for raw in versions:
        row = dict(raw)
        meta = _question_meta(row, question_bank_id=scope["question_bank_id"])
        key = (row["question_id"], row["version"])
        questions.append(meta)
        usage[key] = {"question_id": row["question_id"], "version": row["version"], "usage_count": 0,
                      "independent_count": 0, "assisted_count": 0, "retry_count": 0, "exposed_count": 0}
        for source in _sources(_object(row["content_json"])):
            source_rows[(source["id"], source["locator"])] = source
    # Scope all usage by actual task class grants before counting. No per-person
    # keys, scores or class/student counts are returned from this branch.
    candidates = db.execute(
        "SELECT t.id FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id WHERE g.course_id=? ORDER BY t.id",
        (scope["course_id"],),
    ).fetchall()
    for row in candidates:
        raw_task = db.execute("SELECT t.*,g.course_id,g.class_id,g.owner_id FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id WHERE t.id=?", (row["id"],)).fetchone()
        task = dict(raw_task) if raw_task else None
        if task is None:
            continue
        # Course-authorized quality reports expose aggregate counts only, never
        # individual identities, answers or scores from another class.
        events = _events(db, task["id"])
        frozen_by_version = {(item["question_id"], item["question_version"]): dict(item) for item in _task_items(db, task["id"])}
        for item in _graded_items(db, task["id"]):
            key = (item["question_id"], item["question_version"])
            if key not in usage or key not in frozen_by_version:
                continue
            question_row = frozen_by_version[key]
            meta = _question_meta(question_row, _object(question_row["frozen_json"]), question_bank_id=scope["question_bank_id"])
            evidence = _evidence(task, dict(item), meta, events)
            if evidence is None:
                continue
            counter = usage[key]
            counter["usage_count"] += 1
            for flag, field in (("independent_evidence", "independent_count"), ("assisted", "assisted_count"),
                                ("retry", "retry_count"), ("exposed", "exposed_count")):
                counter[field] += int(evidence[flag])
    return {"students": [], "goals": [], "questions": questions, "evidence": [], "teacher_judgments": [],
            "sources": [source_rows[key] for key in sorted(source_rows)],
            "question_usage": [usage[key] for key in sorted(usage)]}


def build_learning_report(store: Any, scope: Mapping[str, Any], actor_id: str) -> dict[str, Any]:
    """Return canonical safe report input for one authorized, frozen source read.

    ``LearningStore.report_input(scope=..., actor_id=...)`` delegates here. Its
    legacy goal-id overload remains the repository's responsibility. Missing v2
    source-version support raises NotImplementedError instead of inventing a
    digest or timestamp as a concurrency token.
    """
    scope = _scope(store, scope)
    actor_id = store._id(actor_id, "actor_id")
    with store._using_connection() as db:
        _require_scope(store, db, scope, actor_id)
        revision = _source_revision(db)
        if scope["kind"] == "question_bank":
            return {"scope": scope, "source_revision": revision, **_question_bank(store, db, scope, actor_id)}
        goals, tasks = _goals_and_tasks(store, db, scope, actor_id)
        students = {task["student_id"] for task in tasks}
        if scope["kind"] == "student" and not students and scope["student_id"] == actor_id:
            # Only the actor's own ID can be known without an authorized task.
            students.add(actor_id)
        questions, evidence, source_rows = {}, [], {}
        for task in tasks:
            events = _events(db, task["id"])
            allowed_items = {}
            for raw in _task_items(db, task["id"]):
                row = dict(raw)
                frozen = _object(row["frozen_json"])
                meta = _question_meta(row, frozen)
                key = (row["question_id"], row["question_version"])
                allowed_items[key] = meta
                if key not in questions:
                    questions[key] = meta
                elif (questions[key]["family_id"], questions[key]["points_units"]) != (meta["family_id"], meta["points_units"]):
                    raise ValueError("conflicting frozen question version metadata")
                questions[key]["goal_ids"] = sorted(set(questions[key]["goal_ids"]) | {task["goal_id"]})
                for source in _sources(frozen):
                    source_rows[(source["id"], source["locator"])] = source
            for raw in _graded_items(db, task["id"]):
                item = dict(raw)
                question = allowed_items.get((item["question_id"], item["question_version"]))
                if question is None:
                    continue
                value = _evidence(task, item, question, events)
                if value is not None:
                    evidence.append(value)
        pairs = {(task["student_id"], task["goal_id"]) for task in tasks}
        return {"scope": scope, "source_revision": revision,
                "students": [{"id": student_id} for student_id in sorted(students)],
                "goals": [goals[key] for key in sorted(goals)],
                "questions": [questions[key] for key in sorted(questions)],
                "evidence": sorted(evidence, key=lambda row: (row["occurred_at"], row["attempt_number"], row["id"])),
                "teacher_judgments": _teacher_judgments(db, pairs),
                "sources": [source_rows[key] for key in sorted(source_rows)]}
