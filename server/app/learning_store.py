"""Durable learning-domain SQLite store, isolated from platform and job databases.

All public reads take an actor ID and check task/goal ownership. The HTTP layer
must still revalidate current platform/class authorization before each read or
idempotent replay. A submitted attempt and its grading outbox row are committed
in the same learning.sqlite3 transaction; this module never writes JobQueue.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4


from .learning_schema import V2_TABLES, migrate_v2


class LearningStoreError(Exception):
    """Base exception for learning-store operations."""


class TaskNotFound(LearningStoreError, LookupError):
    """A task is absent or outside the actor's learning-store scope."""


class GoalNotFound(LearningStoreError, LookupError):
    """A goal is absent or outside the actor's scope."""


class VersionConflict(LearningStoreError):
    """The caller's optimistic version is stale."""


class LeaseLost(LearningStoreError):
    """The current draft lease has expired or was fenced by another device."""


class InvalidTransition(LearningStoreError):
    """The requested operation is invalid for the current state."""


class IdempotencyConflict(LearningStoreError):
    """A request ID was reused with different immutable request data."""


class _LearningStoreCore:
    """Transactional, file-backed repository for learning facts and answer keys."""

    SCHEMA_VERSION = 2
    _ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
    _REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
    _QUESTION_TYPES = {"single_choice", "multi_choice", "judge", "fill", "short_answer", "case", "text"}
    _QUESTION_STATUSES = {"candidate", "reviewing", "approved", "quarantined", "retired"}
    SCORE_SCALE = 100
    _EXPECTED_TABLES = {
        "learning_goals", "goal_revisions", "learning_rounds", "question_versions",
        "learning_tasks", "task_items", "draft_answers", "attempts", "attempt_items",
        "grading_outbox", "idempotency", "task_events", "deadline_extensions",
        "grading_corrections", "report_revisions", "learning_revision", "goal_commands",
        "round_policies", "task_policies", "teacher_judgments", "diagnostic_sessions", "diagnostic_items",
        "question_family_index", "question_metadata",
    }

    def __init__(
        self, db_path: str | Path, *, busy_timeout: float = 30.0,
        access_checker: Any | None = None,
        score_scale: int = 100,
    ) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("LearningStore requires a file-backed SQLite database")
        if isinstance(busy_timeout, bool) or not isinstance(busy_timeout, (int, float)):
            raise TypeError("busy_timeout must be a positive finite number")
        if not math.isfinite(float(busy_timeout)) or busy_timeout <= 0:
            raise ValueError("busy_timeout must be a positive finite number")
        self.db_path = Path(db_path)
        self.busy_timeout = float(busy_timeout)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._access_checker = access_checker
        if type(score_scale) is not int or score_scale != self.SCORE_SCALE:
            raise ValueError(f"score_scale must be {self.SCORE_SCALE} for persisted hundredth-point units")
        self.score_scale = self.SCORE_SCALE
        self._preflight_existing_database()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _preflight_existing_database(self) -> None:
        if not self.db_path.is_file() or self.db_path.stat().st_size == 0:
            return
        db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        except sqlite3.Error as error:
            raise RuntimeError("existing database is not a readable learning database") from error
        finally:
            db.close()
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {version} is newer than supported version {self.SCHEMA_VERSION}")
        if version == 0 and tables:
            raise RuntimeError("refusing to migrate an unversioned, non-empty database")
        expected = self._EXPECTED_TABLES - V2_TABLES if version == 1 else self._EXPECTED_TABLES | V2_TABLES
        if version > 0 and not expected.issubset(tables):
            raise RuntimeError("existing database does not match the learning schema")

    def _initialize(self) -> None:
        db = self._connect()
        try:
            mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            db.execute("BEGIN IMMEDIATE")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(f"database schema version {version} is newer than supported version {self.SCHEMA_VERSION}")
            if version == 0:
                self._migration_1(db)
                db.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                migrate_v2(db)
                db.execute("PRAGMA user_version=2")
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _migration_1(db: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE learning_goals (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL CHECK(length(trim(owner_id))>0),
                course_id TEXT, class_id TEXT, title TEXT NOT NULL CHECK(length(trim(title))>0),
                description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN ('draft','active','closed')),
                version INTEGER NOT NULL CHECK(version>0), created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE goal_revisions (
                goal_id TEXT NOT NULL REFERENCES learning_goals(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>0), snapshot_json TEXT NOT NULL,
                actor_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(goal_id,revision)
            )""",
            """CREATE TABLE learning_rounds (
                id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES learning_goals(id) ON DELETE CASCADE,
                round_no INTEGER NOT NULL CHECK(round_no>0), status TEXT NOT NULL CHECK(status IN ('open','closed')),
                deadline_at REAL, allow_late INTEGER NOT NULL CHECK(allow_late IN (0,1)),
                version INTEGER NOT NULL CHECK(version>0), created_at REAL NOT NULL,
                UNIQUE(goal_id,round_no), UNIQUE(goal_id,id)
            )""",
            """CREATE TABLE question_versions (
                question_id TEXT NOT NULL CHECK(length(trim(question_id))>0),
                version INTEGER NOT NULL CHECK(version>0), owner_id TEXT NOT NULL CHECK(length(trim(owner_id))>0),
                course_id TEXT, status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('candidate','reviewing','approved','quarantined','retired')),
                content_json TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(question_id,version)
            )""",
            """CREATE TABLE learning_tasks (
                id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES learning_goals(id) ON DELETE RESTRICT,
                round_id TEXT NOT NULL, student_id TEXT NOT NULL CHECK(length(trim(student_id))>0),
                status TEXT NOT NULL CHECK(status IN ('not_started','in_progress','submitted','grading','awaiting_manual','results_ready','closed')),
                version INTEGER NOT NULL CHECK(version>0), deadline_at REAL, allow_late INTEGER NOT NULL CHECK(allow_late IN (0,1)),
                allow_retries INTEGER NOT NULL CHECK(allow_retries IN (0,1)), first_attempt_id TEXT,
                lease_owner TEXT, lease_token TEXT, lease_until REAL, fence_token INTEGER NOT NULL DEFAULT 0 CHECK(fence_token>=0),
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(round_id,student_id), UNIQUE(goal_id,id),
                FOREIGN KEY(goal_id,round_id) REFERENCES learning_rounds(goal_id,id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE task_items (
                task_id TEXT NOT NULL REFERENCES learning_tasks(id) ON DELETE CASCADE,
                question_id TEXT NOT NULL, question_version INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
                frozen_json TEXT NOT NULL, PRIMARY KEY(task_id,question_id), UNIQUE(task_id,position),
                FOREIGN KEY(question_id,question_version) REFERENCES question_versions(question_id,version) ON DELETE RESTRICT
            )""",
            """CREATE TABLE draft_answers (
                task_id TEXT NOT NULL, question_id TEXT NOT NULL, answer_json TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(task_id,question_id),
                FOREIGN KEY(task_id,question_id) REFERENCES task_items(task_id,question_id) ON DELETE CASCADE
            )""",
            """CREATE TABLE attempts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES learning_tasks(id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL CHECK(attempt_no>0), kind TEXT NOT NULL CHECK(kind IN ('first','retry')),
                status TEXT NOT NULL CHECK(status IN ('submitted','grading','graded','review_required')),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0), answers_json TEXT NOT NULL,
                submitted_at REAL NOT NULL, graded_at REAL, total_score_units INTEGER CHECK(total_score_units>=0),
                max_score_units INTEGER NOT NULL CHECK(max_score_units>0),
                late INTEGER NOT NULL CHECK(late IN (0,1)), independent_evidence INTEGER NOT NULL CHECK(independent_evidence IN (0,1)),
                UNIQUE(task_id,attempt_no), UNIQUE(task_id,id)
            )""",
            """CREATE TABLE attempt_items (
                attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL, question_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','graded','manual_pending')),
                score_units INTEGER CHECK(score_units>=0), original_grading_json TEXT, grading_json TEXT, result_hash TEXT, updated_at REAL NOT NULL,
                PRIMARY KEY(attempt_id,question_id),
                FOREIGN KEY(task_id,question_id) REFERENCES task_items(task_id,question_id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE grading_outbox (
                job_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE CASCADE,
                attempt_revision INTEGER NOT NULL CHECK(attempt_revision>0),
                status TEXT NOT NULL CHECK(status IN ('pending','dispatched','completed','manual')),
                fence_token INTEGER NOT NULL DEFAULT 0 CHECK(fence_token>=0),
                queue_job_id TEXT, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            "CREATE INDEX grading_outbox_ready_idx ON grading_outbox(status,created_at)",
            """CREATE TABLE idempotency (
                actor_id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES learning_tasks(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100),
                request_hash TEXT NOT NULL CHECK(length(request_hash)=64), result_id TEXT, created_at REAL NOT NULL,
                PRIMARY KEY(actor_id,task_id,request_id)
            )""",
            """CREATE TABLE task_events (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES learning_tasks(id) ON DELETE CASCADE,
                actor_id TEXT NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('help','exposure','reading','teacher_evidence')),
                question_id TEXT, payload_json TEXT NOT NULL, occurred_at REAL NOT NULL
            )""",
            "CREATE INDEX task_events_scope_idx ON task_events(task_id,event_type,occurred_at)",
            """CREATE TABLE deadline_extensions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES learning_tasks(id) ON DELETE CASCADE,
                actor_id TEXT NOT NULL, prior_deadline REAL, new_deadline REAL NOT NULL,
                reason TEXT NOT NULL CHECK(length(trim(reason))>0), created_at REAL NOT NULL
            )""",
            """CREATE TABLE grading_corrections (
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, question_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, prior_score_units INTEGER NOT NULL CHECK(prior_score_units>=0),
                corrected_score_units INTEGER NOT NULL CHECK(corrected_score_units>=0),
                reason TEXT NOT NULL CHECK(length(trim(reason))>0), grading_revision INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(attempt_id,question_id) REFERENCES attempt_items(attempt_id,question_id) ON DELETE CASCADE
            )""",
            """CREATE TABLE report_revisions (
                goal_id TEXT NOT NULL REFERENCES learning_goals(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>0), actor_id TEXT NOT NULL,
                input_digest TEXT NOT NULL CHECK(length(input_digest)=64), report_json TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(goal_id,revision)
            )""",
        )
        for statement in statements:
            db.execute(statement)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and roll back unfinished transactions."""
        db = self._connect()
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def _using_connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        db = self._connect()
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("BEGIN")
            self._local.connection = db
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            self._local.connection = None
            db.close()

    @contextmanager
    def transaction(self) -> Iterator["LearningStore"]:
        """Atomically group repository calls; reject nested transactions."""
        if getattr(self._local, "connection", None) is not None:
            raise RuntimeError("nested transactions are not supported")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._local.connection = db
            yield self
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            self._local.connection = None
            db.close()

    @property
    def schema_version(self) -> int:
        with self._using_connection() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self._using_connection() as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @staticmethod
    def _text(value: Any, name: str, *, maximum: int = 500) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        result = value.strip()
        if not result or len(result) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return result

    def _require_access(self, actor_id: str, scope: Mapping[str, Any], *, write: bool = False) -> None:
        if self._access_checker is not None and not self._access_checker(actor_id, dict(scope), write=write):
            raise TaskNotFound("resource not found")

    @staticmethod
    def _score_units(value: Any, name: str = "score") -> int:
        if isinstance(value, bool):
            raise TypeError(f"{name} must be a finite decimal score")
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise TypeError(f"{name} must be a finite decimal score") from None
        if not decimal.is_finite():
            raise ValueError(f"{name} must be finite")
        return int((decimal * LearningStore.SCORE_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _score_value(units: int | None) -> float | None:
        return None if units is None else units / LearningStore.SCORE_SCALE

    @staticmethod
    def _id(value: Any, name: str = "id") -> str:
        value = LearningStore._text(value, name, maximum=160)
        if not LearningStore._ID.fullmatch(value):
            raise ValueError(f"{name} contains unsupported characters")
        return value

    @staticmethod
    def _version(value: Any, name: str = "expected_version") -> int:
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _timestamp(value: Any, name: str, *, nullable: bool = False) -> float | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite timestamp")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{name} must be a finite timestamp")
        return result

    @classmethod
    def _json_value(cls, value: Any, name: str = "value") -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError(f"{name} must not contain NaN or infinity")
            return
        if isinstance(value, list):
            for item in value:
                cls._json_value(item, name)
            return
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TypeError(f"{name} keys must be strings")
            for item in value.values():
                cls._json_value(item, name)
            return
        raise TypeError(f"{name} must contain JSON-compatible data")

    @classmethod
    def _json(cls, value: Any) -> str:
        cls._json_value(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(LearningStore._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _positive_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite positive number")
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        return result

    @staticmethod
    def _request_id(value: Any) -> str:
        if not isinstance(value, str) or not LearningStore._REQUEST_ID.fullmatch(value):
            raise ValueError("request_id must be 1-100 letters, digits, or _ . : -")
        return value

    @classmethod
    def _check_answer(cls, question: Mapping[str, Any], answer: Any) -> None:
        kind, options = question["type"], question["options"]
        if kind == "single_choice":
            valid = type(answer) is int and 0 <= answer < len(options)
        elif kind == "multi_choice":
            valid = isinstance(answer, list) and bool(answer) and all(
                type(item) is int and 0 <= item < len(options) for item in answer
            ) and len(set(answer)) == len(answer)
        elif kind == "judge":
            valid = type(answer) is bool
        elif kind == "fill":
            valid = (isinstance(answer, str) and bool(answer.strip())) or (
                isinstance(answer, list) and bool(answer)
                and all(isinstance(item, str) and bool(item.strip()) for item in answer)
            )
        else:
            valid = isinstance(answer, str) and bool(answer.strip())
        if not valid:
            raise ValueError(f"answer is invalid for question {question['id']}")
        cls._json_value(answer, f"answer for {question['id']}")

    @classmethod
    def _normalize_question(cls, question: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(question, Mapping):
            raise TypeError("question must be an object")
        question_id = cls._id(question.get("id", question.get("question_id")), "question_id")
        stem = cls._text(question.get("stem"), "question.stem", maximum=10_000)
        options = question.get("options", [])
        if not isinstance(options, list) or any(not isinstance(x, str) or not x.strip() for x in options):
            raise ValueError("question.options must be a list of non-empty strings")
        options = [x.strip() for x in options]
        kind = question.get("type", "single_choice" if options else "text")
        if kind not in cls._QUESTION_TYPES:
            raise ValueError("unsupported question type")
        if kind in {"single_choice", "multi_choice"} and len(options) < 2:
            raise ValueError("choice questions require at least two options")
        if "answer" not in question:
            raise ValueError("question version must include a protected answer key")
        explanation = question.get("explanation", "")
        if not isinstance(explanation, str) or len(explanation) > 10_000:
            raise ValueError("question.explanation must be a string of at most 10000 characters")
        points = cls._positive_number(question.get("points", 1), "question.points")
        points_units = cls._score_units(points, "question.points")
        if points_units <= 0:
            raise ValueError("question.points is below score unit precision")
        normalized = {
            "id": question_id, "type": kind, "stem": stem, "options": options,
            "answer": question["answer"], "explanation": explanation, "points": points_units / 100,
            "points_units": points_units, "rubric": question.get("rubric", []),
        }
        if not isinstance(normalized["rubric"], list):
            raise ValueError("question.rubric must be a list")
        cls._json(normalized)
        cls._check_answer(normalized, normalized["answer"])
        return normalized

    @staticmethod
    def _task_row(db: sqlite3.Connection, task_id: str, actor_id: str) -> sqlite3.Row:
        row = db.execute(
            """SELECT t.*,g.owner_id,g.course_id,g.class_id,g.title AS goal_title
               FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id WHERE t.id=?""",
            (task_id,),
        ).fetchone()
        if row is None or actor_id not in (row["student_id"], row["owner_id"]):
            raise TaskNotFound("task not found")
        return row

    @staticmethod
    def _goal_row(db: sqlite3.Connection, goal_id: str, actor_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM learning_goals WHERE id=? AND owner_id=?", (goal_id, actor_id)).fetchone()
        if row is None:
            raise GoalNotFound("goal not found")
        return row

    @staticmethod
    def _items(db: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        rows = db.execute("SELECT * FROM task_items WHERE task_id=? ORDER BY position", (task_id,)).fetchall()
        return [json.loads(row["frozen_json"]) for row in rows]

    @classmethod
    def _question_projection(cls, db: sqlite3.Connection, task_id: str, *, reveal: bool) -> list[dict[str, Any]]:
        questions = cls._items(db, task_id)
        result = []
        for question in questions:
            visible = {key: question[key] for key in ("id", "version", "type", "stem", "options", "points")}
            if reveal:
                visible.update(answer=question["answer"], explanation=question["explanation"], rubric=question["rubric"])
            result.append(visible)
        return result

    @classmethod
    def _task_view(cls, db: sqlite3.Connection, task: sqlite3.Row, *, teacher: bool = False) -> dict[str, Any]:
        task_id = task["id"]
        attempts = db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no", (task_id,)).fetchall()
        reveal = teacher or any(row["status"] == "graded" for row in attempts)
        drafts = db.execute("SELECT question_id,answer_json FROM draft_answers WHERE task_id=?", (task_id,)).fetchall()
        attempt_views = []
        for attempt in attempts:
            items = db.execute(
                "SELECT question_id,status,score_units,original_grading_json,grading_json FROM attempt_items WHERE attempt_id=? ORDER BY question_id",
                (attempt["id"],),
            ).fetchall()
            attempt_view = {
                "id": attempt["id"], "attempt_no": attempt["attempt_no"], "kind": attempt["kind"],
                "status": attempt["status"], "revision": attempt["revision"],
                "submitted_at": attempt["submitted_at"], "late": bool(attempt["late"]),
                "independent_evidence": bool(attempt["independent_evidence"]),
                "answers": json.loads(attempt["answers_json"]),
            }
            if attempt["status"] == "graded":
                attempt_view.update(
                    total_score=cls._score_value(attempt["total_score_units"]),
                    total_score_units=attempt["total_score_units"],
                    max_score=attempt["max_score_units"] / cls.SCORE_SCALE,
                )
                attempt_view["items"] = [
                    {"question_id": item["question_id"], "score": cls._score_value(item["score_units"]),
                     "score_units": item["score_units"], "result": json.loads(item["grading_json"] or "{}"),
                     "original_grading": json.loads(item["original_grading_json"] or "{}")}
                    for item in items
                ]
            attempt_views.append(attempt_view)
        return {
            "id": task_id, "goal_id": task["goal_id"], "round_id": task["round_id"],
            "student_id": task["student_id"], "status": task["status"], "version": task["version"],
            "deadline_at": task["deadline_at"], "allow_late": bool(task["allow_late"]),
            "allow_retries": bool(task["allow_retries"]), "first_attempt_id": task["first_attempt_id"],
            "questions": cls._question_projection(db, task_id, reveal=reveal),
            "draft_answers": {row["question_id"]: json.loads(row["answer_json"]) for row in drafts},
            "attempts": attempt_views, "answer_released": reveal,
            "created_at": task["created_at"], "updated_at": task["updated_at"],
        }

    @classmethod
    def _idempotent(
        cls, db: sqlite3.Connection, *, actor_id: str, task_id: str,
        request_id: str, operation: str, payload: Mapping[str, Any],
    ) -> bool:
        digest_payload = {key: value for key, value in payload.items() if key not in {"expected_version", "lease_token"}}
        digest = cls._digest({"operation": operation, "payload": digest_payload})
        row = db.execute(
            "SELECT request_hash FROM idempotency WHERE actor_id=? AND task_id=? AND request_id=?",
            (actor_id, task_id, request_id),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != digest:
                raise IdempotencyConflict("request_id was already used with different input")
            return True
        db.execute(
            "INSERT INTO idempotency(actor_id,task_id,request_id,request_hash,created_at) VALUES(?,?,?,?,?)",
            (actor_id, task_id, request_id, digest, time.time()),
        )
        return False

    @classmethod
    def _lease_check(cls, task: sqlite3.Row, actor_id: str, lease_token: str, fence_token: int, now: float) -> None:
        if actor_id != task["student_id"]:
            raise TaskNotFound("task not found")
        if (task["lease_token"] != lease_token
                or task["fence_token"] != fence_token or task["lease_until"] is None or task["lease_until"] <= now):
            raise LeaseLost("draft lease is no longer valid")

    def create_goal(
        self, actor_id: str, title: str, description: str = "", *, goal_id: str | None = None,
        course_id: str | None = None, class_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = self._id(actor_id, "actor_id")
        goal_id = self._id(goal_id or uuid4().hex, "goal_id")
        title = self._text(title, "title", maximum=160)
        if not isinstance(description, str) or len(description) > 10_000:
            raise ValueError("description must be a string of at most 10000 characters")
        course_id = self._id(course_id, "course_id") if course_id is not None else None
        class_id = self._id(class_id, "class_id") if class_id is not None else None
        now = time.time()
        snapshot = {"title": title, "description": description, "course_id": course_id, "class_id": class_id, "status": "draft"}
        with self._using_connection(write=True) as db:
            db.execute(
                "INSERT INTO learning_goals(id,owner_id,course_id,class_id,title,description,status,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'draft',1,?,?)",
                (goal_id, actor_id, course_id, class_id, title, description, now, now),
            )
            db.execute(
                "INSERT INTO goal_revisions(goal_id,revision,snapshot_json,actor_id,created_at) VALUES(?,1,?,?,?)",
                (goal_id, self._json(snapshot), actor_id, now),
            )
            return dict(db.execute("SELECT * FROM learning_goals WHERE id=?", (goal_id,)).fetchone())

    def update_goal(
        self, goal_id: str, actor_id: str, *, expected_version: int,
        title: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        expected_version = self._version(expected_version)
        if title is None and description is None:
            raise ValueError("at least one goal field must change")
        if title is not None:
            title = self._text(title, "title", maximum=160)
        if description is not None and (not isinstance(description, str) or len(description) > 10_000):
            raise ValueError("description must be a string of at most 10000 characters")
        now = time.time()
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            if goal["version"] != expected_version:
                raise VersionConflict("goal version is stale")
            next_title = title if title is not None else goal["title"]
            next_description = description if description is not None else goal["description"]
            version = expected_version + 1
            db.execute(
                "UPDATE learning_goals SET title=?,description=?,version=?,updated_at=? WHERE id=?",
                (next_title, next_description, version, now, goal_id),
            )
            snapshot = {"title": next_title, "description": next_description, "course_id": goal["course_id"],
                        "class_id": goal["class_id"], "status": goal["status"]}
            db.execute("INSERT INTO goal_revisions VALUES(?,?,?,?,?)",
                       (goal_id, version, self._json(snapshot), actor_id, now))
            return dict(db.execute("SELECT * FROM learning_goals WHERE id=?", (goal_id,)).fetchone())

    def get_goal_scope(self, goal_id: str, actor_id: str) -> dict[str, Any]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            goal = self._goal_row(db, goal_id, actor_id)
            return {"goal_id": goal_id, "owner_id": actor_id, "course_id": goal["course_id"],
                    "class_id": goal["class_id"], "version": goal["version"]}

    def create_round(
        self, actor_id: str, goal_id: str, round_no: int, *, round_id: str | None = None,
        deadline_at: float | None = None, allow_late: bool = True, status: str = "open",
    ) -> dict[str, Any]:
        actor_id, goal_id = self._id(actor_id, "actor_id"), self._id(goal_id, "goal_id")
        round_id = self._id(round_id or uuid4().hex, "round_id")
        if type(round_no) is not int or round_no < 1:
            raise ValueError("round_no must be a positive integer")
        deadline = self._timestamp(deadline_at, "deadline_at", nullable=True)
        if type(allow_late) is not bool or status not in {"open", "closed"}:
            raise ValueError("invalid round policy")
        now = time.time()
        with self._using_connection(write=True) as db:
            self._goal_row(db, goal_id, actor_id)
            db.execute(
                "INSERT INTO learning_rounds(id,goal_id,round_no,status,deadline_at,allow_late,version,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (round_id, goal_id, round_no, status, deadline, int(allow_late), now),
            )
            return dict(db.execute("SELECT * FROM learning_rounds WHERE id=?", (round_id,)).fetchone())

    def set_round_status(
        self, round_id: str, actor_id: str, status: str, *, expected_version: int,
    ) -> dict[str, Any]:
        round_id, actor_id = self._id(round_id, "round_id"), self._id(actor_id, "actor_id")
        expected_version = self._version(expected_version)
        if status not in {"open", "closed"}:
            raise ValueError("status must be open or closed")
        with self._using_connection(write=True) as db:
            row = db.execute(
                "SELECT r.*,g.owner_id FROM learning_rounds r JOIN learning_goals g ON g.id=r.goal_id WHERE r.id=?",
                (round_id,),
            ).fetchone()
            if row is None or row["owner_id"] != actor_id:
                raise GoalNotFound("round not found")
            if row["version"] != expected_version:
                raise VersionConflict("round version is stale")
            db.execute("UPDATE learning_rounds SET status=?,version=version+1 WHERE id=?", (status, round_id))
            return dict(db.execute("SELECT * FROM learning_rounds WHERE id=?", (round_id,)).fetchone())

    def create_question_version(
        self, actor_id: str, question_id: str, version: int, question: Mapping[str, Any], *,
        status: str = "candidate", course_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id, question_id = self._id(actor_id, "actor_id"), self._id(question_id, "question_id")
        if type(version) is not int or version < 1:
            raise ValueError("question version must be a positive integer")
        normalized = self._normalize_question({**dict(question), "id": question_id})
        if status not in self._QUESTION_STATUSES:
            raise ValueError("invalid question version status")
        course_id = self._id(course_id, "course_id") if course_id is not None else None
        now = time.time()
        with self._using_connection(write=True) as db:
            db.execute(
                "INSERT INTO question_versions(question_id,version,owner_id,course_id,status,content_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (question_id, version, actor_id, course_id, status, self._json(normalized), now),
            )
        return {"question_id": question_id, "version": version, "owner_id": actor_id,
                "course_id": course_id, "status": status, "created_at": now, "content": normalized}

    def set_question_version_status(
        self, actor_id: str, question_id: str, version: int, status: str,
    ) -> dict[str, Any]:
        actor_id, question_id = self._id(actor_id, "actor_id"), self._id(question_id, "question_id")
        if type(version) is not int or version < 1 or status not in self._QUESTION_STATUSES:
            raise ValueError("invalid question version or status")
        with self._using_connection(write=True) as db:
            row = db.execute("SELECT * FROM question_versions WHERE question_id=? AND version=? AND owner_id=?",
                             (question_id, version, actor_id)).fetchone()
            if row is None:
                raise LookupError("question version not found")
            if status == "approved":
                latest = db.execute("SELECT MAX(version) FROM question_versions WHERE question_id=?", (question_id,)).fetchone()[0]
                if version != latest:
                    raise InvalidTransition("only the latest question version can be approved")
            db.execute("UPDATE question_versions SET status=? WHERE question_id=? AND version=?",
                       (status, question_id, version))
            return {**dict(row), "status": status, "content": json.loads(row["content_json"])}

    def list_question_versions(
        self, actor_id: str, question_id: str, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        actor_id, question_id = self._id(actor_id, "actor_id"), self._id(question_id, "question_id")
        if status is not None and status not in self._QUESTION_STATUSES:
            raise ValueError("invalid question version status")
        with self._using_connection() as db:
            clauses, params = ["owner_id=?", "question_id=?"], [actor_id, question_id]
            if status is not None:
                clauses.append("status=?")
                params.append(status)
            rows = db.execute("SELECT * FROM question_versions WHERE " + " AND ".join(clauses) + " ORDER BY version", params).fetchall()
            return [{**dict(row), "content": json.loads(row["content_json"])} for row in rows]

    def create_task(
        self, actor_id: str, student_id: str, goal_id: str, round_id: str,
        question_versions: Sequence[Mapping[str, Any]], *, task_id: str | None = None,
        allow_retries: bool = True, deadline_at: float | None = None,
        allow_late: bool | None = None,
    ) -> dict[str, Any]:
        """Create a student's frozen paper from authoritative approved question versions."""
        actor_id, student_id = self._id(actor_id, "actor_id"), self._id(student_id, "student_id")
        goal_id, round_id = self._id(goal_id, "goal_id"), self._id(round_id, "round_id")
        task_id = self._id(task_id or uuid4().hex, "task_id")
        if type(allow_retries) is not bool or (allow_late is not None and type(allow_late) is not bool):
            raise TypeError("retry and late policies must be bool")
        if isinstance(question_versions, (str, bytes)) or not isinstance(question_versions, Sequence):
            raise TypeError("question_versions must be a sequence")
        if not 1 <= len(question_versions) <= 100:
            raise ValueError("a task must contain between 1 and 100 questions")
        refs, seen = [], set()
        for ref in question_versions:
            if not isinstance(ref, Mapping):
                raise TypeError("each question version reference must be an object")
            qid = self._id(ref.get("question_id", ref.get("id")), "question_id")
            qversion = ref.get("version")
            if type(qversion) is not int or qversion < 1 or qid in seen:
                raise ValueError("question version references must be unique and valid")
            refs.append((qid, qversion))
            seen.add(qid)
        explicit_deadline = self._timestamp(deadline_at, "deadline_at", nullable=True)
        now = time.time()
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            round_row = db.execute("SELECT * FROM learning_rounds WHERE id=? AND goal_id=?", (round_id, goal_id)).fetchone()
            if round_row is None:
                raise GoalNotFound("round not found")
            if goal["status"] == "closed" or round_row["status"] != "open":
                raise InvalidTransition("goal or round is closed")
            deadline = explicit_deadline if explicit_deadline is not None else round_row["deadline_at"]
            late = bool(round_row["allow_late"]) if allow_late is None else allow_late
            db.execute(
                """INSERT INTO learning_tasks
                   (id,goal_id,round_id,student_id,status,version,deadline_at,allow_late,allow_retries,created_at,updated_at)
                   VALUES(?,?,?,?,'not_started',1,?,?,?,?,?)""",
                (task_id, goal_id, round_id, student_id, deadline, int(late), int(allow_retries), now, now),
            )
            for position, (qid, qversion) in enumerate(refs):
                question = db.execute(
                    "SELECT * FROM question_versions WHERE question_id=? AND version=?", (qid, qversion),
                ).fetchone()
                if question is None or question["status"] != "approved":
                    raise ValueError(f"question version is not approved: {qid}@{qversion}")
                if question["course_id"] is not None and question["course_id"] != goal["course_id"]:
                    raise PermissionError("question version is outside this course")
                if question["course_id"] is None and question["owner_id"] != actor_id:
                    raise PermissionError("question version is outside actor's authoring scope")
                frozen = json.loads(question["content_json"])
                frozen["version"] = qversion
                db.execute(
                    "INSERT INTO task_items(task_id,question_id,question_version,position,frozen_json) VALUES(?,?,?,?,?)",
                    (task_id, qid, qversion, position, self._json(frozen)),
                )
            task = self._task_row(db, task_id, actor_id)
            return self._task_view(db, task, teacher=True)

    def get_question(
        self, question_id: str, version: int, actor_id: str, *, task_id: str | None = None,
    ) -> dict[str, Any]:
        """Teacher sees owned question keys; students need an assigned task and current answer gate."""
        question_id, actor_id = self._id(question_id, "question_id"), self._id(actor_id, "actor_id")
        if type(version) is not int or version < 1:
            raise ValueError("version must be a positive integer")
        with self._using_connection() as db:
            row = db.execute(
                "SELECT * FROM question_versions WHERE question_id=? AND version=?", (question_id, version),
            ).fetchone()
            if row is None:
                raise LookupError("question version not found")
            content = json.loads(row["content_json"])
            if row["owner_id"] == actor_id:
                return {"question_id": question_id, "version": version, **content}
            if task_id is None:
                raise TaskNotFound("question not assigned to actor")
            task = self._task_row(db, self._id(task_id, "task_id"), actor_id)
            item = db.execute(
                "SELECT 1 FROM task_items WHERE task_id=? AND question_id=? AND question_version=?",
                (task["id"], question_id, version),
            ).fetchone()
            if item is None:
                raise TaskNotFound("question not assigned to actor")
            view = self._task_view(db, task)
            return next(question for question in view["questions"] if question["id"] == question_id)

    def get_task(self, task_id: str, actor_id: str) -> dict[str, Any]:
        """Get the whole task through current ownership and server-side answer gating."""
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            task = self._task_row(db, task_id, actor_id)
            return self._task_view(db, task, teacher=actor_id == task["owner_id"])

    def get_task_scope(self, task_id: str, actor_id: str) -> dict[str, Any]:
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            task = self._task_row(db, task_id, actor_id)
            return {"task_id": task_id, "goal_id": task["goal_id"], "round_id": task["round_id"],
                    "student_id": task["student_id"], "owner_id": task["owner_id"],
                    "course_id": task["course_id"], "class_id": task["class_id"]}

    def acquire_draft_lease(
        self, task_id: str, actor_id: str, device_id: str, *, lease_seconds: float = 60.0,
        takeover: bool = False, now: float | None = None,
    ) -> dict[str, Any]:
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        device_id = self._id(device_id, "device_id")
        lease_seconds = self._positive_number(lease_seconds, "lease_seconds")
        current = time.time() if now is None else self._timestamp(now, "now")
        if type(takeover) is not bool:
            raise TypeError("takeover must be bool")
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            scope = {"kind": "task", "task_id": task_id, "goal_id": task["goal_id"], "round_id": task["round_id"],
                     "student_id": task["student_id"], "owner_id": task["owner_id"],
                     "course_id": task["course_id"], "class_id": task["class_id"]}
            self._require_access(actor_id, scope, write=False)
            if actor_id != task["student_id"]:
                raise TaskNotFound("task not found")
            if task["status"] not in {"not_started", "in_progress", "results_ready"}:
                raise InvalidTransition("task cannot be edited in its current state")
            active = task["lease_until"] is not None and task["lease_until"] > current
            if active and task["lease_owner"] != device_id and not takeover:
                raise LeaseLost("another device currently owns the draft lease")
            fence = task["fence_token"] + 1
            token = uuid4().hex
            expires = current + lease_seconds
            db.execute(
                "UPDATE learning_tasks SET lease_owner=?,lease_token=?,lease_until=?,fence_token=?,version=version+1,updated_at=? WHERE id=?",
                (device_id, token, expires, fence, current, task_id),
            )
            return {"task_id": task_id, "device_id": device_id, "lease_token": token,
                    "fence_token": fence, "lease_until": expires,
                    "task_version": task["version"] + 1}

    def renew_draft_lease(
        self, task_id: str, actor_id: str, lease_token: str, fence_token: int, *,
        lease_seconds: float = 60.0, now: float | None = None,
    ) -> dict[str, Any]:
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        lease_token = self._id(lease_token, "lease_token")
        if type(fence_token) is not int or fence_token < 1:
            raise ValueError("fence_token must be a positive integer")
        seconds = self._positive_number(lease_seconds, "lease_seconds")
        current = time.time() if now is None else self._timestamp(now, "now")
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            self._lease_check(task, actor_id, lease_token, fence_token, current)
            expires = current + seconds
            db.execute("UPDATE learning_tasks SET lease_until=?,updated_at=? WHERE id=?", (expires, current, task_id))
            return {"task_id": task_id, "lease_token": lease_token, "fence_token": fence_token, "lease_until": expires}

    def save_draft(
        self, task_id: str, actor_id: str, answers: Mapping[str, Any], *,
        expected_version: int, request_id: str, lease_token: str, fence_token: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Replace a partial draft. Caller passes current actor, version and lease fence."""
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        expected_version = self._version(expected_version)
        request_id = self._request_id(request_id)
        lease_token = self._id(lease_token, "lease_token")
        if type(fence_token) is not int or fence_token < 1:
            raise ValueError("fence_token must be a positive integer")
        if not isinstance(answers, Mapping) or any(not isinstance(key, str) for key in answers):
            raise TypeError("answers must map question IDs to answer values")
        payload = {"expected_version": expected_version, "answers": dict(answers), "fence_token": fence_token}
        self._json(payload)
        current = time.time() if now is None else self._timestamp(now, "now")
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            if self._idempotent(db, actor_id=actor_id, task_id=task_id, request_id=request_id,
                                operation="save_draft", payload=payload):
                return self._task_view(db, task)
            self._version_check(task, expected_version)
            self._lease_check(task, actor_id, lease_token, fence_token, current)
            if task["status"] not in {"not_started", "in_progress"}:
                raise InvalidTransition("drafts can only be saved before submitting")
            questions = {item["id"]: item for item in self._items(db, task_id)}
            if set(answers) - questions.keys():
                raise ValueError("draft contains a question outside this task")
            for question_id, answer in answers.items():
                self._check_answer(questions[question_id], answer)
            db.execute("DELETE FROM draft_answers WHERE task_id=?", (task_id,))
            db.executemany(
                "INSERT INTO draft_answers(task_id,question_id,answer_json,updated_at) VALUES(?,?,?,?)",
                [(task_id, key, self._json(value), current) for key, value in answers.items()],
            )
            db.execute("UPDATE learning_tasks SET status='in_progress',version=version+1,updated_at=? WHERE id=?",
                       (current, task_id))
            task = self._task_row(db, task_id, actor_id)
            return self._task_view(db, task)

    @staticmethod
    def _version_check(task: sqlite3.Row, expected_version: int) -> None:
        if task["version"] != expected_version:
            raise VersionConflict(f"expected task version {expected_version}, current version is {task['version']}")

    def submit_attempt(
        self, task_id: str, actor_id: str, *, expected_version: int, request_id: str,
        lease_token: str, fence_token: int, now: float | None = None,
    ) -> dict[str, Any]:
        """Freeze all draft answers and create the stable grading outbox job atomically."""
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        expected_version = self._version(expected_version)
        request_id = self._request_id(request_id)
        lease_token = self._id(lease_token, "lease_token")
        if type(fence_token) is not int or fence_token < 1:
            raise ValueError("fence_token must be a positive integer")
        current = time.time() if now is None else self._timestamp(now, "now")
        payload = {"expected_version": expected_version, "fence_token": fence_token}
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            if self._idempotent(db, actor_id=actor_id, task_id=task_id, request_id=request_id,
                                operation="submit_attempt", payload=payload):
                return self._task_view(db, task)
            self._version_check(task, expected_version)
            self._lease_check(task, actor_id, lease_token, fence_token, current)
            if task["status"] not in {"not_started", "in_progress"}:
                raise InvalidTransition("task is not open for submission")
            round_row = db.execute("SELECT status FROM learning_rounds WHERE id=?", (task["round_id"],)).fetchone()
            if round_row is None or round_row["status"] != "open":
                raise InvalidTransition("round is closed")
            if task["deadline_at"] is not None and current > task["deadline_at"] and not task["allow_late"]:
                raise InvalidTransition("task deadline has passed")
            questions = {item["id"]: item for item in self._items(db, task_id)}
            rows = db.execute("SELECT question_id,answer_json FROM draft_answers WHERE task_id=?", (task_id,)).fetchall()
            answers = {row["question_id"]: json.loads(row["answer_json"]) for row in rows}
            if answers.keys() != questions.keys():
                raise ValueError("all task questions must be answered before submission")
            for qid, answer in answers.items():
                self._check_answer(questions[qid], answer)
            attempt_no = int(db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE task_id=?",
                                        (task_id,)).fetchone()[0])
            kind = "first" if attempt_no == 1 else "retry"
            exposed = db.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND event_type IN ('help','exposure') AND occurred_at<=? LIMIT 1",
                (task_id, current),
            ).fetchone() is not None
            independent = kind == "first" and not exposed
            attempt_id, job_id = uuid4().hex, f"grade:{task_id}:{attempt_no}"
            max_score_units = sum(int(question["points_units"]) for question in questions.values())
            late = bool(task["deadline_at"] is not None and current > task["deadline_at"])
            db.execute(
                """INSERT INTO attempts
                   (id,task_id,attempt_no,kind,status,answers_json,submitted_at,max_score_units,late,independent_evidence)
                   VALUES(?,?,?,?,'submitted',?,?,?,?,?)""",
                (attempt_id, task_id, attempt_no, kind, self._json(answers), current, max_score_units, int(late), int(independent)),
            )
            db.executemany(
                "INSERT INTO attempt_items(attempt_id,task_id,question_id,status,updated_at) VALUES(?,?,?,'pending',?)",
                [(attempt_id, task_id, qid, current) for qid in questions],
            )
            db.execute(
                "INSERT INTO grading_outbox(job_id,attempt_id,attempt_revision,status,created_at,updated_at) "
                "VALUES(?,?,1,'pending',?,?)",
                (job_id, attempt_id, current, current),
            )
            db.execute(
                "UPDATE learning_tasks SET status='grading',version=version+1,first_attempt_id=COALESCE(first_attempt_id,?),updated_at=? WHERE id=?",
                (attempt_id if attempt_no == 1 else None, current, task_id),
            )
            db.execute("UPDATE idempotency SET result_id=? WHERE actor_id=? AND task_id=? AND request_id=?",
                       (attempt_id, actor_id, task_id, request_id))
            task = self._task_row(db, task_id, actor_id)
            result = self._task_view(db, task)
            result["grading_job_id"] = job_id
            return result

    def list_grading_outbox(
        self, *, status: str = "pending", limit: int = 100, before_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if status not in {"pending", "dispatched", "completed", "manual", "all"}:
            raise ValueError("invalid outbox status")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses, params = [], []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if before_id is not None:
            clauses.append("job_id<?")
            params.append(self._text(before_id, "before_id", maximum=200))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._using_connection() as db:
            rows = db.execute(
                "SELECT * FROM grading_outbox" + where + " ORDER BY created_at,job_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_grading_outbox(
        self, job_id: str, status: str = "dispatched", *, queue_job_id: str | None = None,
        fence_token: int | None = None, error: str | None = None,
    ) -> dict[str, Any]:
        """Record JobQueue delivery/claim metadata in this DB only; never enqueue here."""
        job_id = self._text(job_id, "job_id", maximum=200)
        if status not in {"pending", "dispatched", "completed", "manual"}:
            raise ValueError("invalid outbox status")
        if queue_job_id is not None:
            queue_job_id = self._text(queue_job_id, "queue_job_id", maximum=200)
        if fence_token is not None and (type(fence_token) is not int or fence_token < 1):
            raise ValueError("fence_token must be a positive integer")
        if error is not None and (not isinstance(error, str) or len(error) > 2000):
            raise ValueError("error must be a string of at most 2000 characters")
        with self._using_connection(write=True) as db:
            row = db.execute("SELECT * FROM grading_outbox WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("grading outbox job not found")
            if row["status"] in {"completed", "manual"} and row["status"] != status:
                raise InvalidTransition("grading outbox is terminal")
            if fence_token is not None and fence_token < row["fence_token"]:
                raise LeaseLost("stale grading fence token")
            db.execute(
                "UPDATE grading_outbox SET status=?,queue_job_id=COALESCE(?,queue_job_id),"
                "fence_token=COALESCE(?,fence_token),last_error=?,updated_at=? WHERE job_id=?",
                (status, queue_job_id, fence_token, error, time.time(), job_id),
            )
            return dict(db.execute("SELECT * FROM grading_outbox WHERE job_id=?", (job_id,)).fetchone())

    def get_grading_input(self, attempt_id: str) -> dict[str, Any]:
        """Return frozen answer key/rubric and submitted answers for a trusted worker."""
        attempt_id = self._id(attempt_id, "attempt_id")
        with self._using_connection() as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            outbox = db.execute("SELECT * FROM grading_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
            task = db.execute(
                """SELECT t.*,g.owner_id,g.course_id,g.class_id FROM learning_tasks t
                   JOIN learning_goals g ON g.id=t.goal_id WHERE t.id=?""", (attempt["task_id"],),
            ).fetchone()
            items = db.execute(
                "SELECT ai.question_id,ti.frozen_json FROM attempt_items ai JOIN task_items ti "
                "ON ti.task_id=ai.task_id AND ti.question_id=ai.question_id WHERE ai.attempt_id=? ORDER BY ti.position",
                (attempt_id,),
            ).fetchall()
            return {
                "attempt_id": attempt_id, "task_id": attempt["task_id"], "attempt_no": attempt["attempt_no"],
                "attempt_revision": attempt["revision"], "job_id": outbox["job_id"],
                "status": attempt["status"], "outbox_status": outbox["status"],
                "fence_token": outbox["fence_token"], "answers": json.loads(attempt["answers_json"]),
                "scope": {"student_id": task["student_id"], "owner_id": task["owner_id"],
                          "course_id": task["course_id"], "class_id": task["class_id"],
                          "goal_id": task["goal_id"], "round_id": task["round_id"]},
                "questions": [json.loads(row["frozen_json"]) for row in items],
            }

    def apply_grading_result(
        self, attempt_id: str, item_id: str, result: Mapping[str, Any], *,
        expected_revision: int, job_id: str, fence_token: int,
    ) -> dict[str, Any]:
        """Apply one item for the current attempt revision and worker fencing token."""
        attempt_id, item_id = self._id(attempt_id, "attempt_id"), self._id(item_id, "item_id")
        expected_revision = self._version(expected_revision, "expected_revision")
        job_id = self._text(job_id, "job_id", maximum=200)
        if type(fence_token) is not int or fence_token < 1:
            raise ValueError("fence_token must be a positive integer")
        if not isinstance(result, Mapping):
            raise TypeError("result must be an object")
        value = dict(result)
        self._json(value)
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            outbox = db.execute("SELECT * FROM grading_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
            if outbox["job_id"] != job_id or outbox["fence_token"] != fence_token:
                raise LeaseLost("grading job or fencing token is stale")
            if attempt["revision"] != expected_revision:
                raise VersionConflict("grading attempt revision is stale")
            task = db.execute("SELECT * FROM learning_tasks WHERE id=?", (attempt["task_id"],)).fetchone()
            question_row = db.execute(
                "SELECT frozen_json FROM task_items WHERE task_id=? AND question_id=?", (task["id"], item_id),
            ).fetchone()
            if question_row is None:
                raise LookupError("question is not in the frozen paper")
            question = json.loads(question_row["frozen_json"])
            score = value.get("score")
            score_units = self._score_units(score)
            max_units = int(question["points_units"])
            if score_units < 0 or score_units > max_units:
                raise ValueError("grading score outside question points")
            canonical = dict(value)
            canonical["score"] = self._score_value(score_units)
            canonical["score_units"] = score_units
            digest = self._digest(canonical)
            item = db.execute(
                "SELECT * FROM attempt_items WHERE attempt_id=? AND question_id=?", (attempt_id, item_id),
            ).fetchone()
            if item is None:
                raise LookupError("attempt item not found")
            # Check immutable-result replay before active outbox state: the final
            # item may have completed the outbox before the queue step was acked.
            if item["status"] == "graded":
                if item["result_hash"] == digest:
                    return self._task_view(db, self._task_row(db, task["id"], task["student_id"]))
                raise InvalidTransition("a different result was already applied to this item")
            if attempt["status"] == "graded":
                raise InvalidTransition("a graded attempt is immutable")
            if outbox["status"] not in {"dispatched", "pending"}:
                raise InvalidTransition("grading outbox is not active")
            now = time.time()
            db.execute(
                "UPDATE attempt_items SET status='graded',score_units=?,original_grading_json=?,grading_json=?,result_hash=?,updated_at=? "
                "WHERE attempt_id=? AND question_id=?",
                (score_units, self._json(canonical), self._json(canonical), digest, now, attempt_id, item_id),
            )
            remaining = int(db.execute(
                "SELECT COUNT(*) FROM attempt_items WHERE attempt_id=? AND status<>'graded'", (attempt_id,),
            ).fetchone()[0])
            if remaining == 0:
                total_units = int(db.execute(
                    "SELECT COALESCE(SUM(score_units),0) FROM attempt_items WHERE attempt_id=?", (attempt_id,),
                ).fetchone()[0])
                db.execute(
                    "UPDATE attempts SET status='graded',graded_at=?,total_score_units=? WHERE id=?",
                    (now, total_units, attempt_id),
                )
                db.execute("UPDATE learning_tasks SET status='results_ready',version=version+1,updated_at=? WHERE id=?",
                           (now, task["id"]))
                db.execute("UPDATE grading_outbox SET status='completed',updated_at=? WHERE job_id=?",
                           (now, job_id))
            refreshed = self._task_row(db, task["id"], task["student_id"])
            return self._task_view(db, refreshed)

    def fail_grading(self, attempt_id: str, *, job_id: str, fence_token: int, reason: str) -> dict[str, Any]:
        """Stop automated grading and move the whole attempt to manual review without releasing keys."""
        attempt_id, job_id = self._id(attempt_id, "attempt_id"), self._text(job_id, "job_id", maximum=200)
        reason = self._text(reason, "reason", maximum=2000)
        if type(fence_token) is not int or fence_token < 1:
            raise ValueError("fence_token must be a positive integer")
        now = time.time()
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            outbox = db.execute("SELECT * FROM grading_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
            if outbox["job_id"] != job_id or outbox["fence_token"] != fence_token:
                raise LeaseLost("grading job or fencing token is stale")
            if attempt["status"] == "graded":
                raise InvalidTransition("completed grading cannot be failed")
            db.execute("UPDATE attempt_items SET status='manual_pending',grading_json=?,updated_at=? "
                       "WHERE attempt_id=? AND status='pending'", (self._json({"reason": reason}), now, attempt_id))
            db.execute("UPDATE attempts SET status='review_required' WHERE id=?", (attempt_id,))
            db.execute("UPDATE learning_tasks SET status='awaiting_manual',version=version+1,updated_at=? WHERE id=?",
                       (now, attempt["task_id"]))
            db.execute("UPDATE grading_outbox SET status='manual',last_error=?,updated_at=? WHERE job_id=?",
                       (reason, now, job_id))
            task = self._task_row(db, attempt["task_id"],
                                  db.execute("SELECT student_id FROM learning_tasks WHERE id=?", (attempt["task_id"],)).fetchone()[0])
            return self._task_view(db, task)

    def apply_manual_grade(
        self, actor_id: str, attempt_id: str, item_id: str, *, score: float,
        reason: str, expected_revision: int,
    ) -> dict[str, Any]:
        actor_id, attempt_id, item_id = self._id(actor_id, "actor_id"), self._id(attempt_id, "attempt_id"), self._id(item_id, "item_id")
        expected_revision = self._version(expected_revision, "expected_revision")
        reason = self._text(reason, "reason", maximum=2000)
        score_units = self._score_units(score)
        now = time.time()
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            task = self._task_row(db, attempt["task_id"], actor_id)
            if actor_id != task["owner_id"]:
                raise TaskNotFound("task not found")
            self._require_access(actor_id, {"kind": "task", "task_id": task["id"], "goal_id": task["goal_id"],
                                             "student_id": task["student_id"], "owner_id": task["owner_id"],
                                             "course_id": task["course_id"], "class_id": task["class_id"]}, write=True)
            if attempt["revision"] != expected_revision or attempt["status"] != "review_required":
                raise VersionConflict("attempt is not at the expected manual-review revision")
            question = db.execute("SELECT frozen_json FROM task_items WHERE task_id=? AND question_id=?",
                                  (task["id"], item_id)).fetchone()
            item = db.execute("SELECT * FROM attempt_items WHERE attempt_id=? AND question_id=?",
                              (attempt_id, item_id)).fetchone()
            if question is None or item is None:
                raise LookupError("attempt question not found")
            frozen = json.loads(question["frozen_json"])
            if score_units < 0 or score_units > int(frozen["points_units"]):
                raise ValueError("score outside question points")
            payload = {"score": self._score_value(score_units), "score_units": score_units,
                       "feedback": reason, "source": "teacher"}
            db.execute(
                "UPDATE attempt_items SET status='graded',score_units=?,grading_json=?,result_hash=?,updated_at=? "
                "WHERE attempt_id=? AND question_id=?",
                (score_units, self._json(payload), self._digest(payload), now, attempt_id, item_id),
            )
            remaining = int(db.execute("SELECT COUNT(*) FROM attempt_items WHERE attempt_id=? AND status<>'graded'",
                                       (attempt_id,)).fetchone()[0])
            if remaining == 0:
                total_units = int(db.execute("SELECT COALESCE(SUM(score_units),0) FROM attempt_items WHERE attempt_id=?",
                                             (attempt_id,)).fetchone()[0])
                db.execute("UPDATE attempts SET status='graded',graded_at=?,total_score_units=?,revision=revision+1 WHERE id=?",
                           (now, total_units, attempt_id))
                db.execute("UPDATE learning_tasks SET status='results_ready',version=version+1,updated_at=? WHERE id=?",
                           (now, task["id"]))
                db.execute("UPDATE grading_outbox SET status='completed',updated_at=? WHERE attempt_id=?",
                           (now, attempt_id))
            return self._task_view(db, self._task_row(db, task["id"], task["student_id"]))

    def start_retry(
        self, task_id: str, actor_id: str, *, expected_version: int, request_id: str,
    ) -> dict[str, Any]:
        """Open the frozen paper for another attempt; preserve first score and exposure history."""
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        expected_version, request_id = self._version(expected_version), self._request_id(request_id)
        payload = {"expected_version": expected_version}
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            if self._idempotent(db, actor_id=actor_id, task_id=task_id, request_id=request_id,
                                operation="start_retry", payload=payload):
                return self._task_view(db, task)
            self._version_check(task, expected_version)
            if actor_id != task["student_id"]:
                raise TaskNotFound("task not found")
            if task["status"] != "results_ready" or not task["allow_retries"]:
                raise InvalidTransition("retry requires completed results and an enabled retry policy")
            now = time.time()
            db.execute("DELETE FROM draft_answers WHERE task_id=?", (task_id,))
            db.execute("UPDATE learning_tasks SET status='not_started',lease_owner=NULL,lease_token=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?",
                       (now, task_id))
            updated = self._task_row(db, task_id, actor_id)
            return self._task_view(db, updated)

    def extend_deadline(
        self, actor_id: str, task_id: str, new_deadline: float, reason: str, *, expected_version: int,
    ) -> dict[str, Any]:
        actor_id, task_id = self._id(actor_id, "actor_id"), self._id(task_id, "task_id")
        expected_version = self._version(expected_version)
        deadline = self._timestamp(new_deadline, "new_deadline")
        reason = self._text(reason, "reason", maximum=2000)
        now = time.time()
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            if actor_id != task["owner_id"]:
                raise TaskNotFound("task not found")
            self._version_check(task, expected_version)
            if task["status"] == "closed":
                raise InvalidTransition("closed task deadline cannot be extended")
            db.execute("INSERT INTO deadline_extensions VALUES(?,?,?,?,?,?,?)",
                       (uuid4().hex, task_id, actor_id, task["deadline_at"], deadline, reason, now))
            db.execute("UPDATE learning_tasks SET deadline_at=?,version=version+1,updated_at=? WHERE id=?",
                       (deadline, now, task_id))
            return self._task_view(db, self._task_row(db, task_id, actor_id), teacher=True)

    def record_help(
        self, task_id: str, actor_id: str, question_id: str, *, help_type: str = "hint",
        payload: Mapping[str, Any] | None = None, occurred_at: float | None = None,
    ) -> dict[str, Any]:
        return self._record_task_event(task_id, actor_id, "help", question_id, {"help_type": help_type, **dict(payload or {})}, occurred_at)

    def record_exposure(
        self, task_id: str, actor_id: str, question_id: str, *, source: str,
        occurred_at: float | None = None,
    ) -> dict[str, Any]:
        return self._record_task_event(task_id, actor_id, "exposure", question_id, {"source": self._text(source, "source")}, occurred_at)

    def record_reading(
        self, task_id: str, actor_id: str, material_id: str, *, occurred_at: float | None = None,
    ) -> dict[str, Any]:
        material_id = self._id(material_id, "material_id")
        return self._record_task_event(task_id, actor_id, "reading", None, {"material_id": material_id}, occurred_at)

    def _record_task_event(
        self, task_id: str, actor_id: str, event_type: str, question_id: str | None,
        payload: Mapping[str, Any], occurred_at: float | None,
    ) -> dict[str, Any]:
        task_id, actor_id = self._id(task_id, "task_id"), self._id(actor_id, "actor_id")
        if question_id is not None:
            question_id = self._id(question_id, "question_id")
        if event_type not in {"help", "exposure", "reading", "teacher_evidence"}:
            raise ValueError("unsupported task event type")
        self._json(dict(payload))
        when = time.time() if occurred_at is None else self._timestamp(occurred_at, "occurred_at")
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            if question_id is not None and db.execute(
                "SELECT 1 FROM task_items WHERE task_id=? AND question_id=?", (task_id, question_id),
            ).fetchone() is None:
                raise ValueError("event question is outside this task")
            db.execute("INSERT INTO task_events VALUES(?,?,?,?,?,?,?)",
                       (uuid4().hex, task_id, actor_id, event_type, question_id, self._json(dict(payload)), when))
            return {"task_id": task_id, "actor_id": actor_id, "event_type": event_type,
                    "question_id": question_id, "payload": dict(payload), "occurred_at": when}

    def list_evidence(self, goal_id: str, actor_id: str, *, student_id: str | None = None) -> list[dict[str, Any]]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        if student_id is not None:
            student_id = self._id(student_id, "student_id")
        with self._using_connection() as db:
            self._goal_row(db, goal_id, actor_id)
            clauses, params = ["t.goal_id=?"], [goal_id]
            if student_id is not None:
                clauses.append("t.student_id=?")
                params.append(student_id)
            rows = db.execute(
                "SELECT e.*,t.student_id FROM task_events e JOIN learning_tasks t ON t.id=e.task_id WHERE "
                + " AND ".join(clauses) + " ORDER BY e.occurred_at,e.id", params,
            ).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def report_input(self, goal_id: str, actor_id: str) -> dict[str, Any]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._require_access(actor_id, {"kind": "goal", "goal_id": goal_id, "owner_id": actor_id,
                                             "course_id": goal["course_id"], "class_id": goal["class_id"]}, write=False)
            task_rows = db.execute("SELECT * FROM learning_tasks WHERE goal_id=? ORDER BY round_id,student_id", (goal_id,)).fetchall()
            tasks = [self._task_view(db, self._task_row(db, row["id"], actor_id), teacher=True) for row in task_rows]
            evidence_rows = db.execute(
                "SELECT e.*,t.student_id FROM task_events e JOIN learning_tasks t ON t.id=e.task_id "
                "WHERE t.goal_id=? ORDER BY e.occurred_at,e.id", (goal_id,),
            ).fetchall()
            evidence = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in evidence_rows]
            digest = self._digest({"goal_version": goal["version"], "tasks": tasks, "evidence": evidence})
            latest = db.execute("SELECT revision FROM report_revisions WHERE goal_id=? ORDER BY revision DESC LIMIT 1", (goal_id,)).fetchone()
            return {"goal": dict(goal), "tasks": tasks, "evidence": evidence,
                    "input_digest": digest, "latest_report_revision": latest[0] if latest else 0}

    def save_report_revision(
        self, goal_id: str, actor_id: str, report: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(report, Mapping):
            raise TypeError("report must be an object")
        report_json = self._json(dict(report))
        now = time.time()
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._require_access(actor_id, {"kind": "goal", "goal_id": goal_id, "owner_id": actor_id,
                                             "course_id": goal["course_id"], "class_id": goal["class_id"]}, write=True)
            current = int(db.execute("SELECT COALESCE(MAX(revision),0) FROM report_revisions WHERE goal_id=?",
                                     (goal_id,)).fetchone()[0])
            if current != expected_revision:
                raise VersionConflict(f"expected report revision {expected_revision}, current revision is {current}")
            task_rows = db.execute("SELECT * FROM learning_tasks WHERE goal_id=? ORDER BY round_id,student_id", (goal_id,)).fetchall()
            tasks = [self._task_view(db, self._task_row(db, row["id"], actor_id), teacher=True) for row in task_rows]
            evidence_rows = db.execute(
                "SELECT e.*,t.student_id FROM task_events e JOIN learning_tasks t ON t.id=e.task_id "
                "WHERE t.goal_id=? ORDER BY e.occurred_at,e.id", (goal_id,),
            ).fetchall()
            evidence = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in evidence_rows]
            digest = self._digest({"goal_version": goal["version"], "tasks": tasks, "evidence": evidence})
            revision = current + 1
            db.execute("INSERT INTO report_revisions VALUES(?,?,?,?,?,?)",
                       (goal_id, revision, actor_id, digest, report_json, now))
            return {"goal_id": goal_id, "revision": revision, "input_digest": digest,
                    "report": json.loads(report_json), "created_at": now}

    def apply_correction(
        self, actor_id: str, attempt_id: str, item_id: str, *, score: float,
        reason: str, expected_revision: int,
    ) -> dict[str, Any]:
        actor_id, attempt_id, item_id = self._id(actor_id, "actor_id"), self._id(attempt_id, "attempt_id"), self._id(item_id, "item_id")
        expected_revision = self._version(expected_revision, "expected_revision")
        score = self._timestamp(score, "score")
        reason = self._text(reason, "reason", maximum=2000)
        now = time.time()
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            task = self._task_row(db, attempt["task_id"], actor_id)
            if actor_id != task["owner_id"]:
                raise TaskNotFound("task not found")
            if attempt["status"] != "graded" or attempt["revision"] != expected_revision:
                raise VersionConflict("attempt revision is stale or not graded")
            question_row = db.execute("SELECT frozen_json FROM task_items WHERE task_id=? AND question_id=?",
                                      (task["id"], item_id)).fetchone()
            item = db.execute("SELECT * FROM attempt_items WHERE attempt_id=? AND question_id=?",
                              (attempt_id, item_id)).fetchone()
            if question_row is None or item is None:
                raise LookupError("attempt question not found")
            question = json.loads(question_row["frozen_json"])
            corrected_units = self._score_units(score)
            if corrected_units < 0 or corrected_units > int(question["points_units"]):
                raise ValueError("corrected score outside question points")
            revision = expected_revision + 1
            db.execute("INSERT INTO grading_corrections VALUES(?,?,?,?,?,?,?,?,?)",
                       (uuid4().hex, attempt_id, item_id, actor_id, item["score_units"], corrected_units, reason, revision, now))
            prior_result = json.loads(item["grading_json"] or "{}")
            prior_snapshot = item["original_grading_json"] or item["grading_json"] or "{}"
            prior_result["correction"] = {"score": self._score_value(corrected_units), "score_units": corrected_units,
                                           "reason": reason, "revision": revision}
            db.execute("UPDATE attempt_items SET score_units=?,original_grading_json=?,grading_json=?,result_hash=?,updated_at=? "
                       "WHERE attempt_id=? AND question_id=?",
                       (corrected_units, prior_snapshot, self._json(prior_result), self._digest(prior_result), now, attempt_id, item_id))
            total_units = int(db.execute("SELECT COALESCE(SUM(score_units),0) FROM attempt_items WHERE attempt_id=?",
                                         (attempt_id,)).fetchone()[0])
            db.execute("UPDATE attempts SET revision=?,total_score_units=? WHERE id=?", (revision, total_units, attempt_id))
            return {"attempt_id": attempt_id, "question_id": item_id, "grading_revision": revision,
                    "prior_score": self._score_value(item["score_units"]), "score": self._score_value(corrected_units),
                    "score_units": corrected_units, "total_score": self._score_value(total_units), "reason": reason}

    def list_corrections(self, attempt_id: str, actor_id: str) -> list[dict[str, Any]]:
        attempt_id, actor_id = self._id(attempt_id, "attempt_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise LookupError("attempt not found")
            self._task_row(db, attempt["task_id"], actor_id)
            rows = db.execute("SELECT * FROM grading_corrections WHERE attempt_id=? ORDER BY grading_revision,id", (attempt_id,)).fetchall()
            return [dict(row) for row in rows]

    def list_report_revisions(self, goal_id: str, actor_id: str) -> list[dict[str, Any]]:
        goal_id, actor_id = self._id(goal_id, "goal_id"), self._id(actor_id, "actor_id")
        with self._using_connection() as db:
            self._goal_row(db, goal_id, actor_id)
            rows = db.execute("SELECT * FROM report_revisions WHERE goal_id=? ORDER BY revision", (goal_id,)).fetchall()
            return [{**dict(row), "report": json.loads(row["report_json"])} for row in rows]



from .learning_operations import LearningOperations
from .learning_lifecycle import LearningLifecycle
from .learning_planning import LearningPlanning


class LearningStore(LearningPlanning, LearningLifecycle, LearningOperations, _LearningStoreCore):
    """Public repository combining immutable facts and authenticated use cases."""
