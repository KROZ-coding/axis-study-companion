"""Durable companion facts and read-time answer protection.

This repository never opens another domain's database, enqueues a job, or calls
an upstream model. The worker validates its queue lease before every write;
response generations fence previous workers inside this database. User-facing
reads must supply the actor and a current learning answer policy. Omitting the
actor from a response read is a Python-only trusted worker operation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4


APPLICATION_ID = 0x41584350
SCHEMA_VERSION = 1
SAFE_GUIDANCE = {
    "independent_attempt": "请独立完成这次诊断；暂时不会可以选择‘暂时不会’，结束后再一起回顾。",
    "try_first": "先写下你的想法或试着作答，保存后再继续获取提示。",
    "await_results": "先检查题目要求、已知条件和当前步骤，保留你的尝试；本轮交卷并完成评分后可查看完整解析。",
    "response_failed": "本次回复未完成，请稍后重试。",
}
_POLICY_NOTICE = "为保护独立作答，相关讲解暂时收起，结果开放后可查看。"
_UNKNOWN_POLICY_NOTICE = "当前学习状态暂不可核验，相关讲解已暂时收起。"
_TERMINAL = {"completed", "failed", "cancelled"}
_POLICY_RESOURCES = {"conversations", "messages", "responses", "events", "collections", "notes", "memories", "discussion_threads", "notifications"}
AnswerPolicy = Callable[..., Mapping[str, Any] | bool]


class CompanionStoreError(Exception):
    """Base exception for companion operations."""


class NotFound(CompanionStoreError, LookupError):
    """A resource is absent or not owned by the caller."""


class Forbidden(CompanionStoreError, PermissionError):
    """The caller cannot perform this operation."""


class InvalidTransition(CompanionStoreError):
    """The resource is not in a state that permits this operation."""


class IdempotencyConflict(CompanionStoreError):
    """A stable request or business key was reused with different data."""


class StaleGeneration(CompanionStoreError):
    """A write belongs to a fenced response generation or call."""


class CompanionStore:
    """Version-one file-backed SQLite repository, with no implicit external I/O."""

    SCHEMA_VERSION = SCHEMA_VERSION
    APPLICATION_ID = APPLICATION_ID
    SAFE_GUIDANCE = SAFE_GUIDANCE
    _EXPECTED_TABLES = {
        "conversations", "messages", "responses", "response_outbox", "events", "collections",
        "collection_items", "notes", "memories", "discussion_threads", "discussion_messages",
        "notifications", "retention_policies", "legal_holds", "audit_events",
    }

    def __init__(self, path: str | Path, *, answer_policy: AnswerPolicy | None = None,
                 busy_timeout: float = 30.0) -> None:
        if str(path) == ":memory:":
            raise ValueError("CompanionStore requires a file-backed SQLite database")
        if isinstance(busy_timeout, bool) or not isinstance(busy_timeout, (int, float)) or not math.isfinite(busy_timeout) or busy_timeout <= 0:
            raise ValueError("busy_timeout must be a positive finite number")
        if answer_policy is not None and not callable(answer_policy):
            raise TypeError("answer_policy must be callable")
        self.db_path = Path(path)
        self.database = str(path)
        self.busy_timeout = float(busy_timeout)
        self.answer_policy = answer_policy
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._preflight()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A configured connection; unfinished transactions are always rolled back."""
        db = self._connect()
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _preflight(self) -> None:
        if not self.db_path.is_file() or self.db_path.stat().st_size == 0:
            return
        db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self._check_database(db)
        except sqlite3.Error as error:
            raise RuntimeError("existing database is not a readable companion database") from error
        finally:
            db.close()

    @classmethod
    def _check_database(cls, db: sqlite3.Connection) -> int:
        app_id = int(db.execute("PRAGMA application_id").fetchone()[0])
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if app_id not in (0, APPLICATION_ID) or version > SCHEMA_VERSION:
            raise RuntimeError("unsupported companion database identity or schema version")
        if version == 0:
            if tables or app_id not in (0, APPLICATION_ID):
                raise RuntimeError("refusing an unversioned non-empty database")
        elif app_id != APPLICATION_ID or not cls._EXPECTED_TABLES.issubset(tables):
            raise RuntimeError("existing database does not match the companion schema")
        return version

    def _initialize(self) -> None:
        with self.connect() as db:
            if str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            db.execute("BEGIN IMMEDIATE")
            try:
                version = self._check_database(db)
                if version == 0:
                    self._migration_1(db)
                    db.execute("PRAGMA application_id=0x41584350")
                    db.execute("PRAGMA user_version=1")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _migration_1(db: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE conversations (
                id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL CHECK(length(trim(user_id))>0),
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200), course_id TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, archived_at REAL,
                UNIQUE(user_id,request_id), UNIQUE(id,user_id),
                CHECK((status='active' AND archived_at IS NULL) OR (status='archived' AND archived_at IS NOT NULL))
            )""",
            "CREATE INDEX conversations_owner_idx ON conversations(user_id,status,updated_at DESC)",
            """CREATE TABLE messages (
                id TEXT PRIMARY KEY NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 1000000), task_id TEXT, question_version_id TEXT,
                response_id TEXT UNIQUE, content_type TEXT NOT NULL DEFAULT 'freeform' CHECK(content_type IN ('freeform','safe_guidance')),
                guidance_key TEXT, request_id TEXT, request_hash TEXT, created_at REAL NOT NULL,
                FOREIGN KEY(conversation_id,user_id) REFERENCES conversations(id,user_id) ON DELETE RESTRICT,
                FOREIGN KEY(response_id,conversation_id,user_id) REFERENCES responses(id,conversation_id,user_id) DEFERRABLE INITIALLY DEFERRED,
                UNIQUE(conversation_id,request_id), UNIQUE(id,conversation_id,user_id),
                CHECK((role='user' AND response_id IS NULL AND request_id IS NOT NULL AND length(request_hash)=64)
                   OR (role='assistant' AND response_id IS NOT NULL AND request_id IS NULL)),
                CHECK((content_type='freeform' AND guidance_key IS NULL) OR (content_type='safe_guidance' AND guidance_key IS NOT NULL))
            )""",
            "CREATE INDEX messages_conversation_idx ON messages(conversation_id,created_at,id)",
            "CREATE INDEX messages_question_idx ON messages(user_id,task_id,question_version_id)",
            """CREATE TABLE responses (
                id TEXT PRIMARY KEY NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','streaming','completed','failed','cancelled')),
                generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0), call_id TEXT,
                text TEXT NOT NULL DEFAULT '' CHECK(length(text)<=1000000), last_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_seq>=0),
                content_type TEXT NOT NULL DEFAULT 'freeform' CHECK(content_type IN ('freeform','safe_guidance')), guidance_key TEXT,
                error TEXT, assistant_message_id TEXT UNIQUE REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, terminal_at REAL,
                FOREIGN KEY(conversation_id,user_id) REFERENCES conversations(id,user_id) ON DELETE RESTRICT,
                FOREIGN KEY(message_id,conversation_id,user_id) REFERENCES messages(id,conversation_id,user_id) ON DELETE RESTRICT,
                UNIQUE(id,conversation_id,user_id), UNIQUE(id,job_id),
                CHECK((generation=0 AND call_id IS NULL) OR (generation>0 AND length(trim(call_id))>0)),
                CHECK((status IN ('pending','streaming') AND terminal_at IS NULL AND assistant_message_id IS NULL)
                   OR (status IN ('completed','failed','cancelled') AND terminal_at IS NOT NULL AND assistant_message_id IS NOT NULL)),
                CHECK((content_type='freeform' AND guidance_key IS NULL) OR (content_type='safe_guidance' AND guidance_key IS NOT NULL))
            )""",
            """CREATE TABLE response_outbox (
                id TEXT PRIMARY KEY NOT NULL, response_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('pending','dispatched')), payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                created_at REAL NOT NULL, dispatched_at REAL,
                FOREIGN KEY(response_id,job_id) REFERENCES responses(id,job_id) ON DELETE RESTRICT,
                CHECK((status='pending' AND dispatched_at IS NULL) OR (status='dispatched' AND dispatched_at IS NOT NULL))
            )""",
            "CREATE INDEX response_outbox_pending_idx ON response_outbox(status,created_at,id)",
            """CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL CHECK(event_type IN ('start','reset','delta','end')), seq INTEGER,
                generation INTEGER NOT NULL CHECK(generation>0), data_json TEXT NOT NULL CHECK(json_valid(data_json)), created_at REAL NOT NULL,
                CHECK((event_type='delta' AND seq>0) OR (event_type<>'delta' AND seq IS NULL))
            )""",
            "CREATE INDEX events_response_idx ON events(response_id,event_id)",
            "CREATE UNIQUE INDEX events_delta_unique ON events(response_id,generation,seq) WHERE event_type='delta'",
            "CREATE UNIQUE INDEX events_end_unique ON events(response_id) WHERE event_type='end'",
            """CREATE TABLE collections (
                id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(user_id,request_id), UNIQUE(id,user_id)
            )""",
            """CREATE TABLE collection_items (
                id TEXT PRIMARY KEY NOT NULL, collection_id TEXT NOT NULL, user_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('question','message','explanation')), message_id TEXT REFERENCES messages(id) ON DELETE RESTRICT,
                task_id TEXT, question_version_id TEXT, text TEXT NOT NULL DEFAULT '' CHECK(length(text)<=20000),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, UNIQUE(collection_id,request_id), UNIQUE(id,user_id),
                FOREIGN KEY(collection_id,user_id) REFERENCES collections(id,user_id) ON DELETE RESTRICT,
                CHECK((kind='question' AND task_id IS NOT NULL AND question_version_id IS NOT NULL AND message_id IS NULL AND text='')
                   OR (kind='message' AND message_id IS NOT NULL AND task_id IS NULL AND question_version_id IS NULL AND text='')
                   OR (kind='explanation' AND message_id IS NULL AND task_id IS NULL AND question_version_id IS NULL AND length(trim(text))>0))
            )""",
            "CREATE INDEX collection_items_owner_idx ON collection_items(collection_id,created_at,id)",
            """CREATE TABLE notes (
                id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, collection_item_id TEXT NOT NULL,
                text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 20000), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(user_id,request_id),
                FOREIGN KEY(collection_item_id,user_id) REFERENCES collection_items(id,user_id) ON DELETE RESTRICT
            )""",
            "CREATE INDEX notes_item_idx ON notes(collection_item_id,created_at,id)",
            """CREATE TABLE memories (
                id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, text TEXT NOT NULL CHECK(length(text)<=20000),
                message_id TEXT REFERENCES messages(id) ON DELETE RESTRICT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(user_id,request_id),
                CHECK(message_id IS NOT NULL OR length(trim(text))>0)
            )""",
            "CREATE INDEX memories_owner_idx ON memories(user_id,status,created_at,id)",
            """CREATE TABLE discussion_threads (
                id TEXT PRIMARY KEY NOT NULL, student_id TEXT NOT NULL, course_id TEXT NOT NULL, class_id TEXT NOT NULL,
                task_id TEXT NOT NULL, attempt_id TEXT NOT NULL, question_version_id TEXT NOT NULL,
                grading_revision INTEGER NOT NULL CHECK(grading_revision>0),
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200), created_by TEXT NOT NULL,
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(created_by,request_id), UNIQUE(student_id,task_id,attempt_id,question_version_id,grading_revision)
            )""",
            "CREATE INDEX discussion_threads_class_idx ON discussion_threads(class_id,updated_at DESC,id)",
            "CREATE INDEX discussion_threads_student_idx ON discussion_threads(student_id,updated_at DESC,id)",
            """CREATE TABLE discussion_messages (
                id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL REFERENCES discussion_threads(id) ON DELETE RESTRICT,
                author_id TEXT NOT NULL, author_role TEXT NOT NULL CHECK(author_role IN ('teacher','student')),
                text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 20000), preset_key TEXT,
                delivery TEXT NOT NULL CHECK(delivery IN ('delivered','deferred')),
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, UNIQUE(thread_id,author_id,request_id),
                CHECK(author_role='teacher' OR (delivery='delivered' AND preset_key IS NULL))
            )""",
            "CREATE INDEX discussion_messages_thread_idx ON discussion_messages(thread_id,created_at,id)",
            """CREATE TABLE notifications (
                id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL, event_type TEXT NOT NULL CHECK(length(trim(event_type))>0),
                resource_type TEXT NOT NULL CHECK(length(trim(resource_type))>0), resource_id TEXT NOT NULL CHECK(length(trim(resource_id))>0),
                business_key TEXT NOT NULL UNIQUE CHECK(length(trim(business_key)) BETWEEN 1 AND 300),
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, read_at REAL
            )""",
            "CREATE INDEX notifications_owner_idx ON notifications(user_id,created_at DESC,id)",
            """CREATE TABLE retention_policies (
                resource_type TEXT PRIMARY KEY NOT NULL CHECK(resource_type IN ('conversations','messages','responses','events','collections','notes','memories','discussion_threads','notifications')),
                retention_days INTEGER NOT NULL CHECK(retention_days BETWEEN 1 AND 36500),
                version INTEGER NOT NULL CHECK(version>0), updated_by TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(length(trim(reason))>0), updated_at REAL NOT NULL
            )""",
            """CREATE TABLE legal_holds (
                id TEXT PRIMARY KEY NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('all','user','conversation','collection','discussion_thread')),
                resource_id TEXT, reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000), created_by TEXT NOT NULL,
                request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 100), request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
                created_at REAL NOT NULL, expires_at REAL, released_at REAL, released_by TEXT, release_reason TEXT,
                UNIQUE(created_by,request_id), CHECK((scope='all' AND resource_id IS NULL) OR (scope<>'all' AND length(trim(resource_id))>0)),
                CHECK(expires_at IS NULL OR expires_at>created_at),
                CHECK((released_at IS NULL AND released_by IS NULL AND release_reason IS NULL)
                   OR (released_at IS NOT NULL AND length(trim(released_by))>0 AND length(trim(release_reason))>0))
            )""",
            "CREATE INDEX legal_holds_scope_idx ON legal_holds(scope,resource_id,released_at,expires_at)",
            """CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, event_type TEXT NOT NULL,
                resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, reason TEXT NOT NULL CHECK(length(trim(reason))>0),
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at REAL NOT NULL
            )""",
        )
        for statement in statements:
            db.execute(statement)

    @property
    def schema_version(self) -> int:
        with self.connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @staticmethod
    def _text(value: Any, name: str, maximum: int = 20000, *, strip: bool = True) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        result = value.strip() if strip else value
        if not result.strip() or len(result) > maximum or "\x00" in result:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return result

    @classmethod
    def _id(cls, value: Any, name: str = "id") -> str:
        return cls._text(value, name, 200)

    @classmethod
    def _request(cls, value: Any) -> str:
        return cls._text(value, "request_id", 100)

    @staticmethod
    def _integer(value: Any, name: str, minimum: int = 0, maximum: int = 2147483647) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        return value

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return float(value)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _owned(db: sqlite3.Connection, table: str, resource_id: str, user_id: str) -> sqlite3.Row:
        queries = {
            "conversations": "SELECT * FROM conversations WHERE id=? AND user_id=?",
            "messages": "SELECT * FROM messages WHERE id=? AND user_id=?",
            "collections": "SELECT * FROM collections WHERE id=? AND user_id=?",
            "collection_items": "SELECT * FROM collection_items WHERE id=? AND user_id=?",
            "notes": "SELECT * FROM notes WHERE id=? AND user_id=?",
            "memories": "SELECT * FROM memories WHERE id=? AND user_id=?",
            "notifications": "SELECT * FROM notifications WHERE id=? AND user_id=?",
        }
        row = db.execute(queries[table], (resource_id, user_id)).fetchone()
        if row is None:
            raise NotFound("resource not found")
        return row

    @staticmethod
    def _same(row: sqlite3.Row, digest: str) -> None:
        if row["request_hash"] != digest:
            raise IdempotencyConflict("request key was already used for different data")

    @staticmethod
    def _public(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key not in {"request_hash", "request_id", "payload_json"}}

    def _policy(self, user_id: str, task_id: str | None, question_version_id: str | None,
                provider: AnswerPolicy | Mapping[str, Any] | None) -> dict[str, Any]:
        source = provider if provider is not None else self.answer_policy
        try:
            value = source(user_id, task_id=task_id, question_version_id=question_version_id) if callable(source) else source
            if type(value) is bool:
                return {"allow_assistant_content": value, "allow_generation": value, "known": True}
            if not isinstance(value, Mapping) or type(value.get("allow_assistant_content")) is not bool:
                return {"allow_assistant_content": False, "allow_generation": False, "known": False}
            result = dict(value)
            result["known"] = True
            result["allow_generation"] = value.get("allow_generation", value.get("allow_freeform_generation", value["allow_assistant_content"])) is True
            return result
        except Exception:
            # Missing or temporarily unavailable learning APIs cannot open the answer gate.
            return {"allow_assistant_content": False, "allow_generation": False, "known": False}

    @staticmethod
    def _notice(policy: Mapping[str, Any]) -> str:
        return _POLICY_NOTICE if policy.get("known") else _UNKNOWN_POLICY_NOTICE

    def _project_message(self, row: sqlite3.Row | Mapping[str, Any], user_id: str,
                         provider: AnswerPolicy | Mapping[str, Any] | None) -> dict[str, Any]:
        result = self._public(row)
        safe = row["content_type"] == "safe_guidance" and SAFE_GUIDANCE.get(row["guidance_key"]) == row["text"]
        if row["role"] == "assistant" and not safe:
            policy = self._policy(user_id, row["task_id"], row["question_version_id"], provider)
            if not policy["allow_assistant_content"]:
                result.update(text="", content="", redacted=True, notice=self._notice(policy))
                return result
        result.update(content=row["text"], redacted=False)
        return result

    def create_conversation(self, user_id: str, title: str, *, course_id: str | None = None,
                            request_id: str) -> dict[str, Any]:
        user_id, title, request_id = self._id(user_id, "user_id"), self._text(title, "title", 200), self._request(request_id)
        course_id = self._id(course_id, "course_id") if course_id is not None else None
        digest = self._hash([title, course_id])
        with self._transaction() as db:
            old = db.execute("SELECT * FROM conversations WHERE user_id=? AND request_id=?", (user_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            record_id, now = uuid4().hex, time.time()
            db.execute("INSERT INTO conversations(id,user_id,title,course_id,request_id,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (record_id, user_id, title, course_id, request_id, digest, now, now))
            return self._public(self._owned(db, "conversations", record_id, user_id))

    def list_conversations(self, user_id: str, *, archived: bool = False, limit: int = 100,
                           offset: int = 0) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        self._integer(offset, "offset")
        if type(archived) is not bool:
            raise ValueError("archived must be bool")
        with self.connect() as db:
            return [self._public(row) for row in db.execute(
                "SELECT * FROM conversations WHERE user_id=? AND status=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                (user_id, "archived" if archived else "active", limit, offset))]

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as db:
            return self._public(self._owned(db, "conversations", conversation_id, user_id))

    def rename_conversation(self, conversation_id: str, user_id: str, title: str) -> dict[str, Any]:
        title = self._text(title, "title", 200)
        with self._transaction() as db:
            self._owned(db, "conversations", conversation_id, user_id)
            db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title, time.time(), conversation_id))
            return self._public(self._owned(db, "conversations", conversation_id, user_id))

    def _archive_conversation(self, conversation_id: str, user_id: str, archived: bool) -> dict[str, Any]:
        with self._transaction() as db:
            self._owned(db, "conversations", conversation_id, user_id)
            now = time.time()
            db.execute("UPDATE conversations SET status=?,archived_at=?,updated_at=? WHERE id=?",
                       ("archived" if archived else "active", now if archived else None, now, conversation_id))
            return self._public(self._owned(db, "conversations", conversation_id, user_id))

    def archive_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        return self._archive_conversation(conversation_id, user_id, True)

    def restore_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        return self._archive_conversation(conversation_id, user_id, False)

    @staticmethod
    def _posted(db: sqlite3.Connection, message: sqlite3.Row) -> dict[str, Any]:
        response = db.execute("SELECT id,job_id,status FROM responses WHERE message_id=?", (message["id"],)).fetchone()
        return {"message_id": message["id"], "conversation_id": message["conversation_id"], "response_id": response["id"],
                "job_id": response["job_id"], "status": response["status"], "message": CompanionStore._public(message)}

    def post_message(self, conversation_id: str, user_id: str, text: str, *, request_id: str,
                     task_id: str | None = None, question_version_id: str | None = None) -> dict[str, Any]:
        text, request_id = self._text(text, "text", 20000, strip=False), self._request(request_id)
        task_id = self._id(task_id, "task_id") if task_id is not None else None
        question_version_id = self._id(question_version_id, "question_version_id") if question_version_id is not None else None
        if question_version_id is not None and task_id is None:
            raise ValueError("question_version_id requires task_id")
        digest = self._hash([text, task_id, question_version_id])
        with self._transaction() as db:
            conversation = self._owned(db, "conversations", conversation_id, user_id)
            old = db.execute("SELECT * FROM messages WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._posted(db, old)
            if conversation["status"] != "active":
                raise InvalidTransition("restore the archived conversation before posting")
            message_id, response_id, job_id, outbox_id, now = uuid4().hex, uuid4().hex, uuid4().hex, uuid4().hex, time.time()
            db.execute("""INSERT INTO messages(id,conversation_id,user_id,role,text,task_id,question_version_id,request_id,request_hash,created_at)
                          VALUES(?,?,?,'user',?,?,?,?,?,?)""",
                       (message_id, conversation_id, user_id, text, task_id, question_version_id, request_id, digest, now))
            db.execute("INSERT INTO responses(id,conversation_id,user_id,message_id,job_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (response_id, conversation_id, user_id, message_id, job_id, now, now))
            payload = {"purpose": "student", "resource_id": response_id, "response_id": response_id,
                       "conversation_id": conversation_id, "user_id": user_id}
            db.execute("INSERT INTO response_outbox(id,response_id,job_id,status,payload_json,created_at) VALUES(?,?,?,'pending',?,?)",
                       (outbox_id, response_id, job_id, self._json(payload), now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            return self._posted(db, db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def list_messages(self, conversation_id: str, user_id: str, *, after: str | None = None,
                      limit: int = 100, answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        with self.connect() as db:
            self._owned(db, "conversations", conversation_id, user_id)
            after_rowid = 0
            if after is not None:
                cursor = db.execute("SELECT rowid FROM messages WHERE id=? AND conversation_id=? AND user_id=?", (after, conversation_id, user_id)).fetchone()
                if cursor is None:
                    raise NotFound("message cursor not found")
                after_rowid = cursor[0]
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? AND user_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                              (conversation_id, user_id, after_rowid, limit)).fetchall()
        return [self._project_message(row, user_id, answer_policy) for row in rows]

    def get_message(self, message_id: str, user_id: str, *, answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.connect() as db:
            row = self._owned(db, "messages", message_id, user_id)
        return self._project_message(row, user_id, answer_policy)

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 1000)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM response_outbox WHERE status='pending' ORDER BY created_at,id LIMIT ?", (limit,)).fetchall()
        return [dict(self._public(row), outbox_id=row["id"], kind="interaction", payload=json.loads(row["payload_json"])) for row in rows]

    def mark_outbox(self, outbox_id: str, *, job_id: str | None = None) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM response_outbox WHERE id=?", (outbox_id,)).fetchone()
            if row is None:
                raise NotFound("outbox item not found")
            if job_id is not None and row["job_id"] != job_id:
                raise IdempotencyConflict("queue job must use the stable outbox job_id")
            db.execute("UPDATE response_outbox SET status='dispatched',dispatched_at=COALESCE(dispatched_at,?) WHERE id=?", (time.time(), outbox_id))
            return self._public(db.execute("SELECT * FROM response_outbox WHERE id=?", (outbox_id,)).fetchone())

    @staticmethod
    def _response_row(db: sqlite3.Connection, response_id: str, user_id: str | None = None) -> sqlite3.Row:
        row = db.execute(
            "SELECT r.*,m.task_id,m.question_version_id,c.course_id,'student' purpose FROM responses r "
            "JOIN messages m ON m.id=r.message_id JOIN conversations c ON c.id=r.conversation_id "
            "WHERE r.id=? AND (? IS NULL OR r.user_id=?)",
            (response_id, user_id, user_id)).fetchone()
        if row is None:
            raise NotFound("response not found")
        return row

    def get_response(self, response_id: str, user_id: str | None = None, *,
                     answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.connect() as db:
            row = self._response_row(db, response_id, user_id)
        result = self._public(row)
        result["response_id"] = row["id"]
        if user_id is None:
            return result
        safe = row["content_type"] == "safe_guidance" and SAFE_GUIDANCE.get(row["guidance_key"]) == row["text"]
        policy = self._policy(user_id, row["task_id"], row["question_version_id"], answer_policy)
        if not safe and not policy["allow_assistant_content"]:
            result.update(text="", content="", error=None, redacted=True, notice=self._notice(policy))
        elif row["status"] in {"failed", "cancelled"}:
            result.update(text="", content="", error="response_failed", redacted=False, notice=SAFE_GUIDANCE["response_failed"])
        else:
            result.update(content=row["text"], error=None, redacted=False)
        return result

    @staticmethod
    def _event(db: sqlite3.Connection, response_id: str, kind: str, generation: int,
               data: Mapping[str, Any], *, seq: int | None = None) -> dict[str, Any]:
        now = time.time()
        cursor = db.execute("INSERT INTO events(response_id,event_type,seq,generation,data_json,created_at) VALUES(?,?,?,?,?,?)",
                            (response_id, kind, seq, generation, CompanionStore._json(dict(data)), now))
        return {"event_id": cursor.lastrowid, "response_id": response_id, "event_type": kind, "seq": seq,
                "generation": generation, "data": dict(data), "created_at": now}

    def _generation(self, generation: int | None, expected_generation: int | None) -> int:
        if generation is not None:
            self._integer(generation, "generation", 1)
        if expected_generation is not None:
            self._integer(expected_generation, "expected_generation", 1)
        if generation is None and expected_generation is None:
            raise ValueError("generation or expected_generation is required")
        if generation is not None and expected_generation is not None and generation != expected_generation:
            raise StaleGeneration("generation does not match expected_generation")
        return generation if generation is not None else expected_generation

    def _bind(self, db: sqlite3.Connection, row: sqlite3.Row, call_id: str, generation: int) -> sqlite3.Row:
        if row["generation"] == 0:
            if row["status"] != "pending":
                raise InvalidTransition("response cannot be started")
            db.execute("UPDATE responses SET generation=?,call_id=?,status='streaming',updated_at=? WHERE id=?",
                       (generation, call_id, time.time(), row["id"]))
            self._event(db, row["id"], "start", generation, {"status": "streaming"})
            return self._response_row(db, row["id"])
        if row["generation"] != generation or row["call_id"] != call_id:
            raise StaleGeneration("response belongs to another generation or call")
        return row

    def begin_response(self, response_id: str, *, call_id: str, generation: int,
                       expected_generation: int | None = None) -> dict[str, Any]:
        call_id = self._id(call_id, "call_id")
        self._integer(generation, "generation", 1)
        if expected_generation is not None:
            self._integer(expected_generation, "expected_generation")
        with self._transaction() as db:
            row = self._response_row(db, response_id)
            if row["generation"] == generation and row["call_id"] == call_id:
                return self._public(row)
            if row["status"] in _TERMINAL:
                raise InvalidTransition("response is terminal")
            if row["generation"] == 0:
                if expected_generation not in (None, 0):
                    raise StaleGeneration("response generation changed")
                return self._public(self._bind(db, row, call_id, generation))
            if expected_generation != row["generation"] or generation <= row["generation"]:
                raise StaleGeneration("takeover requires the current generation and a higher lease generation")
            db.execute("UPDATE responses SET generation=?,call_id=?,text='',last_seq=0,status='streaming',updated_at=? WHERE id=?",
                       (generation, call_id, time.time(), response_id))
            self._event(db, response_id, "reset", generation, {"status": "streaming", "previous_generation": row["generation"]})
            return self._public(self._response_row(db, response_id))

    def append_delta(self, response_id: str, seq: int, text: str, *, call_id: str,
                     generation: int | None = None, expected_generation: int | None = None) -> dict[str, Any]:
        seq = self._integer(seq, "seq", 1)
        if not isinstance(text, str) or not text or len(text) > 1000000 or "\x00" in text:
            raise ValueError("a delta must contain 1 to 1000000 characters")
        call_id, generation = self._id(call_id, "call_id"), self._generation(generation, expected_generation)
        with self._transaction() as db:
            row = self._bind(db, self._response_row(db, response_id), call_id, generation)
            old = db.execute("SELECT * FROM events WHERE response_id=? AND generation=? AND seq=? AND event_type='delta'", (response_id, generation, seq)).fetchone()
            if old is not None:
                if json.loads(old["data_json"])["text"] != text:
                    raise IdempotencyConflict("delta sequence was reused with different text")
                return self._event_view(old)
            if row["status"] in _TERMINAL:
                raise InvalidTransition("response is terminal")
            if seq != row["last_seq"] + 1:
                raise InvalidTransition("delta sequence must be contiguous and start at one")
            if len(row["text"]) + len(text) > 1000000:
                raise ValueError("response exceeds the maximum length")
            db.execute("UPDATE responses SET text=text||?,last_seq=?,updated_at=? WHERE id=?", (text, seq, time.time(), response_id))
            return self._event(db, response_id, "delta", generation, {"text": text}, seq=seq)

    def complete_response(self, response_id: str, text: str | None = None, *, call_id: str,
                          generation: int | None = None, expected_generation: int | None = None,
                          guidance_key: str | None = None) -> dict[str, Any]:
        call_id, generation = self._id(call_id, "call_id"), self._generation(generation, expected_generation)
        if guidance_key is not None:
            if guidance_key not in SAFE_GUIDANCE:
                raise ValueError("unknown preset guidance")
            if text is not None and text != SAFE_GUIDANCE[guidance_key]:
                raise ValueError("preset guidance text cannot be replaced")
            text = SAFE_GUIDANCE[guidance_key]
        if text is not None:
            text = self._text(text, "text", 1000000, strip=False)
        with self._transaction() as db:
            row = self._bind(db, self._response_row(db, response_id), call_id, generation)
            if row["status"] in {"failed", "cancelled"}:
                raise InvalidTransition("response is terminal")
            final = row["text"] if text is None else text
            final = self._text(final, "text", 1000000, strip=False)
            if row["status"] == "completed":
                if row["text"] != final or (guidance_key is not None and row["guidance_key"] != guidance_key):
                    raise IdempotencyConflict("completed response has different content")
                return self._public(row)
            if row["status"] in _TERMINAL:
                raise InvalidTransition("response is terminal")
            if row["text"] and row["text"] != final:
                raise IdempotencyConflict("final content must match committed deltas")
            # Only byte-for-byte presets can bypass a closed answer projection.
            key = guidance_key or next((key for key, value in SAFE_GUIDANCE.items() if value == final), None)
            content_type = "safe_guidance" if key else "freeform"
            if not row["text"]:
                self._event(db, response_id, "delta", generation, {"text": final}, seq=1)
            assistant_id, now = uuid4().hex, time.time()
            db.execute("""INSERT INTO messages(id,conversation_id,user_id,role,text,task_id,question_version_id,response_id,content_type,guidance_key,created_at)
                          VALUES(?,?,?,'assistant',?,?,?,?,?,?,?)""",
                       (assistant_id, row["conversation_id"], row["user_id"], final, row["task_id"], row["question_version_id"], response_id, content_type, key, now))
            db.execute("""UPDATE responses SET status='completed',text=?,content_type=?,guidance_key=?,last_seq=?,assistant_message_id=?,updated_at=?,terminal_at=? WHERE id=?""",
                       (final, content_type, key, max(1, row["last_seq"]), assistant_id, now, now, response_id))
            self._event(db, response_id, "end", generation, {"status": "completed", "message_id": assistant_id})
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, row["conversation_id"]))
            return self._public(self._response_row(db, response_id))

    def fail_response(self, response_id: str, error: str, *, call_id: str,
                      generation: int | None = None, expected_generation: int | None = None) -> dict[str, Any]:
        call_id, generation = self._id(call_id, "call_id"), self._generation(generation, expected_generation)
        error = self._text(error, "error", 2000)
        with self._transaction() as db:
            row = self._bind(db, self._response_row(db, response_id), call_id, generation)
            if row["status"] == "failed":
                if row["error"] != error:
                    raise IdempotencyConflict("response already failed with a different error")
                return self._public(row)
            if row["status"] in _TERMINAL:
                raise InvalidTransition("response is terminal")
            assistant_id, now = uuid4().hex, time.time()
            db.execute("""INSERT INTO messages(id,conversation_id,user_id,role,text,task_id,question_version_id,response_id,content_type,guidance_key,created_at)
                          VALUES(?,?,?,'assistant',?,?,?,?,'safe_guidance','response_failed',?)""",
                       (assistant_id, row["conversation_id"], row["user_id"], SAFE_GUIDANCE["response_failed"], row["task_id"], row["question_version_id"], response_id, now))
            db.execute("UPDATE responses SET status='failed',error=?,assistant_message_id=?,updated_at=?,terminal_at=? WHERE id=?",
                       (error, assistant_id, now, now, response_id))
            self._event(db, response_id, "end", generation, {"status": "failed", "error": "response_failed", "message_id": assistant_id})
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, row["conversation_id"]))
            return self._public(self._response_row(db, response_id))

    @staticmethod
    def _event_view(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in row.keys() if key != "data_json"}
        result["data"] = json.loads(row["data_json"])
        return result

    def list_response_events(self, response_id: str, user_id: str | None = None, *, after: int = 0,
                             limit: int = 1000, answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self._integer(after, "after")
        self._integer(limit, "limit", 1, 1000)
        with self.connect() as db:
            response = self._response_row(db, response_id, user_id)
            # Events from a fenced generation must not reappear during replay.
            rows = db.execute("SELECT * FROM events WHERE response_id=? AND generation=? AND event_id>? ORDER BY event_id LIMIT ?",
                              (response_id, response["generation"], after, limit)).fetchall()
        result = [self._event_view(row) for row in rows]
        if user_id is None:
            return result
        safe = response["content_type"] == "safe_guidance" and SAFE_GUIDANCE.get(response["guidance_key"]) == response["text"]
        policy = self._policy(user_id, response["task_id"], response["question_version_id"], answer_policy)
        blocked = not safe and not policy["allow_assistant_content"]
        for item in result:
            if blocked or response["status"] == "failed":
                if item["event_type"] == "delta":
                    item["data"] = {"text": "", "redacted": True, "notice": self._notice(policy) if blocked else SAFE_GUIDANCE["response_failed"]}
                elif item["event_type"] == "end":
                    item["data"] = {"status": response["status"], "redacted": blocked}
        return result

    response_events = list_response_events

    def build_request_context(self, response_id: str, user_id: str | None = None, *,
                              answer_policy: AnswerPolicy | Mapping[str, Any] | None = None,
                              recent_limit: int = 6) -> dict[str, Any]:
        """Return only the current input and a few user-authored turns, never profile or IDs."""
        self._integer(recent_limit, "recent_limit", 0, 6)
        with self.connect() as db:
            row = self._response_row(db, response_id, user_id)
            current = db.execute("SELECT rowid,text FROM messages WHERE id=?", (row["message_id"],)).fetchone()
            recent = db.execute("""SELECT text FROM messages WHERE conversation_id=? AND user_id=? AND role='user' AND rowid<?
                                   AND (task_id IS NULL OR task_id=?) ORDER BY rowid DESC LIMIT ?""",
                                (row["conversation_id"], row["user_id"], current["rowid"], row["task_id"], recent_limit)).fetchall()
        policy = self._policy(row["user_id"], row["task_id"], row["question_version_id"], answer_policy)
        global_policy = self._policy(row["user_id"], None, None, answer_policy)
        allowed = policy["allow_generation"] and global_policy["allow_generation"]
        diagnostic = policy.get("diagnostic") is True or policy.get("phase") in {"diagnostic", "first_diagnostic"}
        key = "independent_attempt" if diagnostic else ("try_first" if policy.get("attempted") is False else "await_results")
        return {"current_question": current["text"][:4000],
                "messages": [{"role": "user", "content": item["text"][:2000]} for item in reversed(recent)] + [{"role": "user", "content": current["text"][:4000]}],
                "allow_generation": bool(allowed), "safe_guidance_key": key,
                "notice": "" if allowed else self._notice(policy if not policy["allow_generation"] else global_policy)}

    def create_collection(self, user_id: str, title: str, *, request_id: str) -> dict[str, Any]:
        user_id, title, request_id = self._id(user_id, "user_id"), self._text(title, "title", 200), self._request(request_id)
        digest = self._hash(title)
        with self._transaction() as db:
            old = db.execute("SELECT * FROM collections WHERE user_id=? AND request_id=?", (user_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            record_id, now = uuid4().hex, time.time()
            db.execute("INSERT INTO collections(id,user_id,title,request_id,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (record_id, user_id, title, request_id, digest, now, now))
            return self._public(self._owned(db, "collections", record_id, user_id))

    def list_collections(self, user_id: str, *, archived: bool = False) -> list[dict[str, Any]]:
        if type(archived) is not bool:
            raise ValueError("archived must be bool")
        with self.connect() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM collections WHERE user_id=? AND status=? ORDER BY updated_at DESC,id",
                                                         (user_id, "archived" if archived else "active"))]

    def get_collection(self, collection_id: str, user_id: str) -> dict[str, Any]:
        with self.connect() as db:
            return self._public(self._owned(db, "collections", collection_id, user_id))

    def update_collection(self, collection_id: str, user_id: str, *, title: str | None = None,
                          archived: bool | None = None) -> dict[str, Any]:
        if title is not None:
            title = self._text(title, "title", 200)
        if archived is not None and type(archived) is not bool:
            raise ValueError("archived must be bool")
        with self._transaction() as db:
            row = self._owned(db, "collections", collection_id, user_id)
            db.execute("UPDATE collections SET title=?,status=?,updated_at=? WHERE id=?",
                       (title if title is not None else row["title"], ("archived" if archived else "active") if archived is not None else row["status"], time.time(), collection_id))
            return self._public(self._owned(db, "collections", collection_id, user_id))

    def add_collection_item(self, collection_id: str, user_id: str, kind: str, *, request_id: str,
                            message_id: str | None = None, task_id: str | None = None,
                            question_version_id: str | None = None, text: str = "",
                            answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._request(request_id)
        if kind not in {"question", "message", "explanation"}:
            raise ValueError("unsupported collection item kind")
        if kind == "question":
            task_id, question_version_id = self._id(task_id, "task_id"), self._id(question_version_id, "question_version_id")
            if message_id is not None or text:
                raise ValueError("a question collection stores references only")
        elif kind == "message":
            message_id = self._id(message_id, "message_id")
            if task_id is not None or question_version_id is not None or text:
                raise ValueError("a message collection stores references only")
        else:
            text = self._text(text, "text", strip=False)
            if message_id is not None or task_id is not None or question_version_id is not None:
                raise ValueError("an explanation must be user-authored text")
        digest = self._hash([kind, message_id, task_id, question_version_id, text])
        with self._transaction() as db:
            collection = self._owned(db, "collections", collection_id, user_id)
            if message_id is not None:
                self._owned(db, "messages", message_id, user_id)
            old = db.execute("SELECT * FROM collection_items WHERE collection_id=? AND request_id=?", (collection_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                row = dict(old)
            else:
                if collection["status"] != "active":
                    raise InvalidTransition("restore the collection before adding items")
                record_id, now = uuid4().hex, time.time()
                db.execute("INSERT INTO collection_items(id,collection_id,user_id,kind,message_id,task_id,question_version_id,text,request_id,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (record_id, collection_id, user_id, kind, message_id, task_id, question_version_id, text, request_id, digest, now))
                db.execute("UPDATE collections SET updated_at=? WHERE id=?", (now, collection_id))
                row = dict(self._owned(db, "collection_items", record_id, user_id))
        return self._project_collection_item(row, user_id, answer_policy)

    def _project_collection_item(self, row: Mapping[str, Any], user_id: str,
                                 provider: AnswerPolicy | Mapping[str, Any] | None) -> dict[str, Any]:
        result = self._public(row)
        if row["kind"] == "message":
            result["message"] = self.get_message(row["message_id"], user_id, answer_policy=provider)
            result["redacted"] = result["message"]["redacted"]
            if result["redacted"]:
                result["notice"] = result["message"]["notice"]
        elif row["kind"] == "question":
            policy = self._policy(user_id, row["task_id"], row["question_version_id"], provider)
            result["redacted"] = not policy["allow_assistant_content"]
            result["notice"] = "" if policy["allow_assistant_content"] else self._notice(policy)
        else:
            result["redacted"] = False
        # Question stems/keys are obtained from learning's *current* public projection by the route.
        return result

    def list_collection_items(self, collection_id: str, user_id: str, *, limit: int = 100,
                              offset: int = 0, answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        self._integer(offset, "offset")
        with self.connect() as db:
            self._owned(db, "collections", collection_id, user_id)
            rows = db.execute("SELECT * FROM collection_items WHERE collection_id=? AND user_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                              (collection_id, user_id, limit, offset)).fetchall()
        return [self._project_collection_item(row, user_id, answer_policy) for row in rows]

    def create_note(self, collection_item_id: str, user_id: str, text: str, *, request_id: str) -> dict[str, Any]:
        text, request_id = self._text(text, "text", strip=False), self._request(request_id)
        digest = self._hash([collection_item_id, text])
        with self._transaction() as db:
            self._owned(db, "collection_items", collection_item_id, user_id)
            old = db.execute("SELECT * FROM notes WHERE user_id=? AND request_id=?", (user_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            record_id, now = uuid4().hex, time.time()
            db.execute("INSERT INTO notes(id,user_id,collection_item_id,text,request_id,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (record_id, user_id, collection_item_id, text, request_id, digest, now, now))
            return self._public(self._owned(db, "notes", record_id, user_id))

    def list_notes(self, collection_item_id: str, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            self._owned(db, "collection_items", collection_item_id, user_id)
            return [self._public(row) for row in db.execute("SELECT * FROM notes WHERE collection_item_id=? AND user_id=? ORDER BY created_at,id", (collection_item_id, user_id))]

    def update_note(self, note_id: str, user_id: str, text: str, *, expected_version: int) -> dict[str, Any]:
        text = self._text(text, "text", strip=False)
        self._integer(expected_version, "expected_version", 1)
        with self._transaction() as db:
            row = self._owned(db, "notes", note_id, user_id)
            if row["version"] != expected_version:
                raise InvalidTransition("note version changed")
            db.execute("UPDATE notes SET text=?,version=version+1,updated_at=? WHERE id=?", (text, time.time(), note_id))
            return self._public(self._owned(db, "notes", note_id, user_id))

    def create_memory(self, user_id: str, text: str = "", *, request_id: str,
                      message_id: str | None = None, answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> dict[str, Any]:
        user_id, request_id = self._id(user_id, "user_id"), self._request(request_id)
        if text:
            text = self._text(text, "text", strip=False)
        elif message_id is None:
            raise ValueError("a memory needs user-authored text or a message reference")
        if message_id is not None:
            message_id = self._id(message_id, "message_id")
        digest = self._hash([text, message_id])
        with self._transaction() as db:
            if message_id is not None:
                self._owned(db, "messages", message_id, user_id)
            old = db.execute("SELECT * FROM memories WHERE user_id=? AND request_id=?", (user_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                row = dict(old)
            else:
                record_id, now = uuid4().hex, time.time()
                db.execute("INSERT INTO memories(id,user_id,text,message_id,request_id,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (record_id, user_id, text, message_id, request_id, digest, now, now))
                row = dict(self._owned(db, "memories", record_id, user_id))
        return self._project_memory(row, user_id, answer_policy)

    def _project_memory(self, row: Mapping[str, Any], user_id: str,
                        provider: AnswerPolicy | Mapping[str, Any] | None) -> dict[str, Any]:
        result = self._public(row)
        if row["message_id"] is not None:
            result["message"] = self.get_message(row["message_id"], user_id, answer_policy=provider)
            if result["message"]["redacted"]:
                result.update(text="", redacted=True, notice=result["message"]["notice"])
                return result
        result["redacted"] = False
        return result

    def list_memories(self, user_id: str, *, archived: bool = False,
                      answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if type(archived) is not bool:
            raise ValueError("archived must be bool")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE user_id=? AND status=? ORDER BY created_at DESC,id LIMIT 500",
                              (user_id, "archived" if archived else "active")).fetchall()
        return [self._project_memory(row, user_id, answer_policy) for row in rows]

    def archive_memory(self, memory_id: str, user_id: str, *, archived: bool = True) -> dict[str, Any]:
        if type(archived) is not bool:
            raise ValueError("archived must be bool")
        with self._transaction() as db:
            self._owned(db, "memories", memory_id, user_id)
            db.execute("UPDATE memories SET status=?,updated_at=? WHERE id=?", ("archived" if archived else "active", time.time(), memory_id))
            # Returning metadata prevents a write endpoint becoming an unprojected read.
            row = self._public(self._owned(db, "memories", memory_id, user_id))
            row.pop("text", None)
            return row

    def create_discussion_thread(self, student_id: str, course_id: str, class_id: str, task_id: str,
                                 attempt_id: str, question_version_id: str, grading_revision: int, *,
                                 created_by: str, request_id: str, title: str = "教师短评") -> dict[str, Any]:
        values = [self._id(value, name) for value, name in ((student_id, "student_id"), (course_id, "course_id"),
                  (class_id, "class_id"), (task_id, "task_id"), (attempt_id, "attempt_id"),
                  (question_version_id, "question_version_id"), (created_by, "created_by"))]
        student_id, course_id, class_id, task_id, attempt_id, question_version_id, created_by = values
        self._integer(grading_revision, "grading_revision", 1)
        request_id, title = self._request(request_id), self._text(title, "title", 200)
        digest = self._hash([student_id, course_id, class_id, task_id, attempt_id, question_version_id, grading_revision, title])
        with self._transaction() as db:
            old = db.execute("SELECT * FROM discussion_threads WHERE created_by=? AND request_id=?", (created_by, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            existing = db.execute("SELECT * FROM discussion_threads WHERE student_id=? AND task_id=? AND attempt_id=? AND question_version_id=? AND grading_revision=?",
                                  (student_id, task_id, attempt_id, question_version_id, grading_revision)).fetchone()
            if existing is not None:
                if existing["course_id"] != course_id or existing["class_id"] != class_id:
                    raise IdempotencyConflict("thread scope is immutable")
                return self._public(existing)
            record_id, now = uuid4().hex, time.time()
            db.execute("""INSERT INTO discussion_threads(id,student_id,course_id,class_id,task_id,attempt_id,question_version_id,grading_revision,title,created_by,request_id,request_hash,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (record_id, student_id, course_id, class_id, task_id, attempt_id, question_version_id, grading_revision, title, created_by, request_id, digest, now, now))
            return self._public(db.execute("SELECT * FROM discussion_threads WHERE id=?", (record_id,)).fetchone())

    def get_discussion_thread(self, thread_id: str) -> dict[str, Any]:
        """Internal scope lookup; routes must check current learning and class access."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM discussion_threads WHERE id=?", (thread_id,)).fetchone()
            if row is None:
                raise NotFound("thread not found")
            return self._public(row)

    def list_discussion_threads(self, *, student_id: str | None = None, class_id: str | None = None,
                                task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        if student_id is None and class_id is None:
            raise ValueError("a student or class scope is required")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM discussion_threads WHERE (? IS NULL OR student_id=?) "
                "AND (? IS NULL OR class_id=?) AND (? IS NULL OR task_id=?) "
                "ORDER BY updated_at DESC,id LIMIT ?",
                (student_id, student_id, class_id, class_id, task_id, task_id, limit)).fetchall()
            return [self._public(row) for row in rows]

    @staticmethod
    def _thread_access(thread: Mapping[str, Any], user_id: str, role: str, *, can_read: bool, write: bool = False,
                       can_manage: bool = False) -> None:
        if role == "student" and user_id == thread["student_id"]:
            return
        if role == "teacher" and (can_manage if write else can_read):
            return
        raise NotFound("thread not found")

    def post_discussion_message(self, thread_id: str, author_id: str, text: str = "", *, author_role: str,
                                request_id: str, results_released: bool = False, preset_key: str | None = None,
                                can_manage: bool = False) -> dict[str, Any]:
        request_id = self._request(request_id)
        if preset_key is not None:
            if author_role != "teacher" or preset_key not in SAFE_GUIDANCE or preset_key == "response_failed":
                raise ValueError("unknown teacher preset guidance")
            if text and text != SAFE_GUIDANCE[preset_key]:
                raise ValueError("teacher preset text cannot be replaced")
            text = SAFE_GUIDANCE[preset_key]
        text = self._text(text, "text", strip=False)
        digest = self._hash([text, preset_key, author_role])
        with self._transaction() as db:
            thread = db.execute("SELECT * FROM discussion_threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None:
                raise NotFound("thread not found")
            self._thread_access(thread, author_id, author_role, can_read=can_manage, write=True, can_manage=can_manage)
            old = db.execute("SELECT * FROM discussion_messages WHERE thread_id=? AND author_id=? AND request_id=?", (thread_id, author_id, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            delivery = "deferred" if author_role == "teacher" and not results_released and preset_key is None else "delivered"
            record_id, now = uuid4().hex, time.time()
            db.execute("INSERT INTO discussion_messages(id,thread_id,author_id,author_role,text,preset_key,delivery,request_id,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (record_id, thread_id, author_id, author_role, text, preset_key, delivery, request_id, digest, now))
            db.execute("UPDATE discussion_threads SET updated_at=? WHERE id=?", (now, thread_id))
            return self._public(db.execute("SELECT * FROM discussion_messages WHERE id=?", (record_id,)).fetchone())

    def list_discussion_messages(self, thread_id: str, user_id: str, *, role: str, can_read: bool = False,
                                 results_released: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        with self.connect() as db:
            thread = db.execute("SELECT * FROM discussion_threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None:
                raise NotFound("thread not found")
            self._thread_access(thread, user_id, role, can_read=can_read)
            rows = db.execute("SELECT * FROM discussion_messages WHERE thread_id=? ORDER BY rowid LIMIT ?", (thread_id, limit)).fetchall()
        result = []
        for row in rows:
            item = self._public(row)
            safe = row["preset_key"] in SAFE_GUIDANCE and SAFE_GUIDANCE.get(row["preset_key"]) == row["text"]
            if role == "student" and row["author_role"] == "teacher" and not results_released and not safe:
                item.update(text="", redacted=True, delivery="deferred", notice="教师短评将在本题结果开放后显示。")
            else:
                item.update(redacted=False)
                if results_released:
                    item["delivery"] = "delivered"
            result.append(item)
        return result

    def create_notification(self, user_id: str, event_type: str, resource_type: str, resource_id: str, *,
                            business_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        user_id, event_type = self._id(user_id, "user_id"), self._text(event_type, "event_type", 100)
        resource_type, resource_id = self._text(resource_type, "resource_type", 100), self._id(resource_id, "resource_id")
        business_key = self._text(business_key, "business_key", 300)
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        encoded = self._json(dict(payload))
        if len(encoded) > 20000:
            raise ValueError("notification payload is too large")
        digest = self._hash([user_id, event_type, resource_type, resource_id, dict(payload)])
        with self._transaction() as db:
            old = db.execute("SELECT * FROM notifications WHERE business_key=?", (business_key,)).fetchone()
            if old is not None:
                self._same(old, digest)
                return dict(self._public(old), payload=json.loads(old["payload_json"]))
            record_id, now = uuid4().hex, time.time()
            db.execute("INSERT INTO notifications(id,user_id,event_type,resource_type,resource_id,business_key,payload_json,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (record_id, user_id, event_type, resource_type, resource_id, business_key, encoded, digest, now))
            return dict(self._public(self._owned(db, "notifications", record_id, user_id)), payload=dict(payload))

    def _project_notification(self, row: sqlite3.Row, user_id: str,
                              provider: AnswerPolicy | Mapping[str, Any] | None) -> dict[str, Any]:
        result, payload = self._public(row), json.loads(row["payload_json"])
        task_id = payload.get("task_id") or (row["resource_id"] if row["resource_type"] == "task" else None)
        policy = self._policy(user_id, task_id, payload.get("question_version_id"), provider)
        if not policy["allow_assistant_content"]:
            result.update(payload={}, redacted=True, notice=self._notice(policy))
        else:
            result.update(payload=payload, redacted=False)
        return result

    def list_notifications(self, user_id: str, *, unread_only: bool = False, limit: int = 100, offset: int = 0,
                           answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        self._integer(limit, "limit", 1, 500)
        self._integer(offset, "offset")
        if type(unread_only) is not bool:
            raise ValueError("unread_only must be bool")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM notifications WHERE user_id=? AND (?=0 OR read_at IS NULL) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                              (user_id, unread_only, limit, offset)).fetchall()
        return [self._project_notification(row, user_id, answer_policy) for row in rows]

    def mark_notification_read(self, notification_id: str, user_id: str, *,
                               answer_policy: AnswerPolicy | Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._transaction() as db:
            self._owned(db, "notifications", notification_id, user_id)
            db.execute("UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND user_id=?", (time.time(), notification_id, user_id))
            row = self._owned(db, "notifications", notification_id, user_id)
        return self._project_notification(row, user_id, answer_policy)

    @staticmethod
    def _audit(db: sqlite3.Connection, actor_id: str, event_type: str, resource_type: str, resource_id: str,
               reason: str, payload: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO audit_events(actor_id,event_type,resource_type,resource_id,reason,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                   (actor_id, event_type, resource_type, resource_id, reason, CompanionStore._json(dict(payload)), time.time()))

    def set_retention_policy(self, resource_type: str, retention_days: int, *, actor_id: str, reason: str) -> dict[str, Any]:
        if resource_type not in _POLICY_RESOURCES:
            raise ValueError("unsupported retention resource type")
        self._integer(retention_days, "retention_days", 1, 36500)
        actor_id, reason = self._id(actor_id, "actor_id"), self._text(reason, "reason", 2000)
        with self._transaction() as db:
            previous = db.execute("SELECT * FROM retention_policies WHERE resource_type=?", (resource_type,)).fetchone()
            db.execute("""INSERT INTO retention_policies(resource_type,retention_days,version,updated_by,reason,updated_at) VALUES(?,?,1,?,?,?)
                          ON CONFLICT(resource_type) DO UPDATE SET retention_days=excluded.retention_days,version=version+1,updated_by=excluded.updated_by,reason=excluded.reason,updated_at=excluded.updated_at""",
                       (resource_type, retention_days, actor_id, reason, time.time()))
            self._audit(db, actor_id, "retention_policy_changed", "retention_policy", resource_type, reason,
                        {"previous_days": previous["retention_days"] if previous else None, "retention_days": retention_days})
            return dict(db.execute("SELECT * FROM retention_policies WHERE resource_type=?", (resource_type,)).fetchone())

    def list_retention_policies(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM retention_policies ORDER BY resource_type")]

    def create_legal_hold(self, scope: str, resource_id: str | None = None, *, created_by: str, reason: str,
                          request_id: str, expires_at: float | None = None) -> dict[str, Any]:
        if scope not in {"all", "user", "conversation", "collection", "discussion_thread"}:
            raise ValueError("unsupported hold scope")
        if scope == "all":
            if resource_id is not None:
                raise ValueError("global hold cannot have a resource_id")
        else:
            resource_id = self._id(resource_id, "resource_id")
        created_by, reason, request_id = self._id(created_by, "created_by"), self._text(reason, "reason", 2000), self._request(request_id)
        if expires_at is not None:
            expires_at = self._timestamp(expires_at)
        digest = self._hash([scope, resource_id, reason, expires_at])
        with self._transaction() as db:
            old = db.execute("SELECT * FROM legal_holds WHERE created_by=? AND request_id=?", (created_by, request_id)).fetchone()
            if old is not None:
                self._same(old, digest)
                return self._public(old)
            now = time.time()
            if expires_at is not None and expires_at <= now:
                raise ValueError("hold expiry must be in the future")
            queries = {
                "conversation": "SELECT 1 FROM conversations WHERE id=?",
                "collection": "SELECT 1 FROM collections WHERE id=?",
                "discussion_thread": "SELECT 1 FROM discussion_threads WHERE id=?",
            }
            if scope in queries and db.execute(queries[scope], (resource_id,)).fetchone() is None:
                raise NotFound("hold resource not found")
            record_id = uuid4().hex
            db.execute("INSERT INTO legal_holds(id,scope,resource_id,reason,created_by,request_id,request_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (record_id, scope, resource_id, reason, created_by, request_id, digest, now, expires_at))
            self._audit(db, created_by, "legal_hold_created", "legal_hold", record_id, reason, {"scope": scope, "resource_id": resource_id, "expires_at": expires_at})
            return self._public(db.execute("SELECT * FROM legal_holds WHERE id=?", (record_id,)).fetchone())

    def list_legal_holds(self, *, active_only: bool = True, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else self._timestamp(now)
        if type(active_only) is not bool:
            raise ValueError("active_only must be bool")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM legal_holds WHERE (?=0 OR (released_at IS NULL AND (expires_at IS NULL OR expires_at>?))) ORDER BY created_at DESC,id",
                              (active_only, current)).fetchall()
            return [self._public(row) for row in rows]

    def release_legal_hold(self, hold_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
        actor_id, reason = self._id(actor_id, "actor_id"), self._text(reason, "reason", 2000)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone()
            if row is None:
                raise NotFound("legal hold not found")
            if row["released_at"] is None:
                db.execute("UPDATE legal_holds SET released_at=?,released_by=?,release_reason=? WHERE id=?", (time.time(), actor_id, reason, hold_id))
                self._audit(db, actor_id, "legal_hold_released", "legal_hold", hold_id, reason, {})
            return self._public(db.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone())

    def preview_cleanup(self, *, now: float | None = None, limit: int = 100) -> dict[str, Any]:
        """Metadata-only preview. No facts, events, or pending work are deleted."""
        current = time.time() if now is None else self._timestamp(now)
        self._integer(limit, "limit", 1, 1000)
        with self.connect() as db:
            db.execute("BEGIN")
            policies = db.execute("SELECT * FROM retention_policies ORDER BY resource_type").fetchall()
            holds = db.execute("SELECT * FROM legal_holds WHERE released_at IS NULL AND (expires_at IS NULL OR expires_at>?)", (current,)).fetchall()
            all_held = any(hold["scope"] == "all" for hold in holds)
            held = {(hold["scope"], hold["resource_id"]) for hold in holds}
            entries = []
            queries = {
                "conversations": "SELECT c.id,c.user_id,c.id conversation_id,NULL collection_id,NULL discussion_thread_id FROM conversations c WHERE c.status='archived' AND c.updated_at<? AND NOT EXISTS(SELECT 1 FROM responses r WHERE r.conversation_id=c.id AND r.status IN ('pending','streaming')) ORDER BY id",
                "messages": "SELECT m.id,m.user_id,m.conversation_id,NULL collection_id,NULL discussion_thread_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.status='archived' AND m.created_at<? ORDER BY id",
                "responses": "SELECT r.id,r.user_id,r.conversation_id,NULL collection_id,NULL discussion_thread_id FROM responses r JOIN conversations c ON c.id=r.conversation_id WHERE c.status='archived' AND r.status IN ('completed','failed','cancelled') AND r.updated_at<? ORDER BY id",
                "events": "SELECT CAST(e.event_id AS TEXT) id,r.user_id,r.conversation_id,NULL collection_id,NULL discussion_thread_id FROM events e JOIN responses r ON r.id=e.response_id JOIN conversations c ON c.id=r.conversation_id WHERE c.status='archived' AND r.status IN ('completed','failed','cancelled') AND e.created_at<? ORDER BY id",
                "collections": "SELECT id,user_id,NULL conversation_id,id collection_id,NULL discussion_thread_id FROM collections WHERE status='archived' AND updated_at<? ORDER BY id",
                "notes": "SELECT n.id,n.user_id,m.conversation_id,i.collection_id,NULL discussion_thread_id FROM notes n JOIN collection_items i ON i.id=n.collection_item_id JOIN collections c ON c.id=i.collection_id LEFT JOIN messages m ON m.id=i.message_id WHERE c.status='archived' AND n.updated_at<? ORDER BY id",
                "memories": "SELECT me.id,me.user_id,m.conversation_id,NULL collection_id,NULL discussion_thread_id FROM memories me LEFT JOIN messages m ON m.id=me.message_id WHERE me.status='archived' AND me.updated_at<? ORDER BY id",
                "discussion_threads": "SELECT id,student_id user_id,NULL conversation_id,NULL collection_id,id discussion_thread_id FROM discussion_threads WHERE updated_at<? ORDER BY id",
                "notifications": "SELECT id,user_id,NULL conversation_id,NULL collection_id,NULL discussion_thread_id FROM notifications WHERE read_at IS NOT NULL AND created_at<? ORDER BY id",
            }
            for policy in policies:
                rows = db.execute(queries[policy["resource_type"]], (current - policy["retention_days"] * 86400,)).fetchall()
                eligible, blocked = [], 0
                for row in rows:
                    is_held = all_held or any((scope, row[field]) in held for scope, field in
                        (("user", "user_id"), ("conversation", "conversation_id"), ("collection", "collection_id"), ("discussion_thread", "discussion_thread_id")))
                    if policy["resource_type"] == "conversations" and not is_held:
                        # A hold on a collection that references a conversation protects the source too.
                        references = db.execute("SELECT DISTINCT i.collection_id FROM collection_items i JOIN messages m ON m.id=i.message_id WHERE m.conversation_id=?", (row["id"],)).fetchall()
                        is_held = any(("collection", ref[0]) in held for ref in references)
                    if is_held:
                        blocked += 1
                    else:
                        eligible.append(row["id"])
                entries.append({"resource_type": policy["resource_type"], "retention_days": policy["retention_days"],
                                "eligible_count": len(eligible), "held_count": blocked, "resource_ids": eligible[:limit], "truncated": len(eligible) > limit})
            db.rollback()
        return {"dry_run": True, "as_of": current, "policies": entries, "active_hold_count": len(holds), "deleted_count": 0}


__all__ = ["CompanionStore", "CompanionStoreError", "NotFound", "Forbidden", "InvalidTransition",
           "IdempotencyConflict", "StaleGeneration", "SAFE_GUIDANCE", "APPLICATION_ID", "SCHEMA_VERSION"]
