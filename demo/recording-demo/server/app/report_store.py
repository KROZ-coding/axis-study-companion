"""Frozen report snapshots and immutable revisions in a dedicated SQLite database.

No learning tables are read here.  Callers supply an already-authorized, safe
snapshot.  The current pointer is advanced only for the latest observed source
revision of the same report scope and mastery rule.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

from .services.mastery import DEFAULT_RULE_VERSION, get_rule


SNAPSHOT_SCHEMA_VERSION = "report.snapshot.v1"
REPORT_KINDS = frozenset({"class", "student", "goal", "question_bank"})
_SCOPE_FIELDS = frozenset({"kind", "course_id", "class_id", "student_id", "goal_id", "question_bank_id"})
_SCOPE_REQUIRED = {"class": "class_id", "student": "student_id", "goal": "goal_id", "question_bank": "question_bank_id"}


class ReportConflict(ValueError):
    """An idempotency key or source revision was reused with different content."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) - _SCOPE_FIELDS:
        raise ValueError("report scope contains unsupported fields")
    kind = scope.get("kind")
    if kind not in REPORT_KINDS:
        raise ValueError("report scope kind must be class, student, goal, or question_bank")
    result = {"kind": kind}
    for name in sorted(_SCOPE_FIELDS - {"kind"}):
        value = scope.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{name} must be a non-empty identifier")
        result[name] = value.strip()
    if _SCOPE_REQUIRED[kind] not in result:
        raise ValueError(f"{kind} report requires {_SCOPE_REQUIRED[kind]}")
    if kind == "class" and any(field in result for field in ("student_id", "goal_id", "question_bank_id")):
        raise ValueError("class reports cannot contain narrower scope identifiers")
    if kind == "student" and any(field in result for field in ("goal_id", "question_bank_id")):
        raise ValueError("student reports cannot contain a goal or question bank identifier")
    if kind == "question_bank" and any(field in result for field in ("student_id", "goal_id")):
        raise ValueError("question bank reports cannot contain a student or goal identifier")
    return result


def report_identifier(scope: Mapping[str, Any], rule_version: str = DEFAULT_RULE_VERSION) -> str:
    get_rule(rule_version)
    return "report_" + content_hash({"scope": normalize_scope(scope), "rule_version": rule_version})[:32]


def _source_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("source_revision must be a non-negative integer")
    return value


def _text(value: Any, field: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty text up to {maximum} characters")
    return value.strip()


class ReportStore:
    """A lazy-runtime friendly, file-backed repository with explicit migrations."""

    SCHEMA_VERSION = 1
    _TABLES = frozenset({"reports", "report_snapshots", "report_revisions", "report_idempotency"})

    def __init__(self, db_path: str | Path, *, timeout: float = 30.0) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("ReportStore requires a file-backed database")
        path = Path(db_path)
        self.db_path = path / "reports.sqlite3" if path.is_dir() or not path.suffix else path
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._preflight()
        self._initialize()

    def _preflight(self) -> None:
        if not self.db_path.is_file() or self.db_path.stat().st_size == 0:
            return
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        except sqlite3.Error as error:
            raise RuntimeError("existing database is not a readable report database") from error
        finally:
            connection.close()
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("report database schema is newer than supported")
        if version == 0 and tables:
            raise RuntimeError("refusing to migrate an unversioned, non-empty report database")
        if version > 0 and not self._TABLES.issubset(tables):
            raise RuntimeError("existing database does not match the report schema")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        with self.connect() as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("report database WAL could not be enabled")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > self.SCHEMA_VERSION:
                    raise RuntimeError("report database schema is newer than supported")
                if version == 0:
                    self._migration_1(connection)
                    connection.execute("PRAGMA user_version=1")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE reports (
                id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, rule_version TEXT NOT NULL,
                latest_source_revision INTEGER NOT NULL CHECK(latest_source_revision >= 0),
                latest_snapshot_id TEXT REFERENCES report_snapshots(id) ON DELETE RESTRICT,
                current_revision_id TEXT REFERENCES report_revisions(id) ON DELETE RESTRICT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(scope_json, rule_version)
            )""",
            """CREATE TABLE report_snapshots (
                id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,
                schema_version TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
                rule_version TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
                created_by TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(report_id, source_revision)
            )""",
            "CREATE INDEX idx_report_snapshots_report ON report_snapshots(report_id, source_revision DESC)",
            """CREATE TABLE report_revisions (
                id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,
                snapshot_id TEXT NOT NULL UNIQUE REFERENCES report_snapshots(id) ON DELETE RESTRICT,
                revision_number INTEGER NOT NULL CHECK(revision_number > 0),
                source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
                rule_version TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
                created_at REAL NOT NULL, UNIQUE(report_id, revision_number)
            )""",
            "CREATE INDEX idx_report_revisions_report ON report_revisions(report_id, revision_number DESC)",
            """CREATE TABLE report_idempotency (
                owner_id TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                snapshot_id TEXT NOT NULL REFERENCES report_snapshots(id) ON DELETE RESTRICT,
                created_at REAL NOT NULL, PRIMARY KEY(owner_id, key)
            )""",
            """CREATE TRIGGER report_snapshots_no_update BEFORE UPDATE ON report_snapshots
                BEGIN SELECT RAISE(ABORT, 'report snapshots are immutable'); END""",
            """CREATE TRIGGER report_snapshots_no_delete BEFORE DELETE ON report_snapshots
                BEGIN SELECT RAISE(ABORT, 'report snapshots are immutable'); END""",
            """CREATE TRIGGER report_revisions_no_update BEFORE UPDATE ON report_revisions
                BEGIN SELECT RAISE(ABORT, 'report revisions are immutable'); END""",
            """CREATE TRIGGER report_revisions_no_delete BEFORE DELETE ON report_revisions
                BEGIN SELECT RAISE(ABORT, 'report revisions are immutable'); END""",
            """CREATE TRIGGER report_idempotency_no_update BEFORE UPDATE ON report_idempotency
                BEGIN SELECT RAISE(ABORT, 'report idempotency records are immutable'); END""",
            """CREATE TRIGGER report_idempotency_no_delete BEFORE DELETE ON report_idempotency
                BEGIN SELECT RAISE(ABORT, 'report idempotency records are immutable'); END""",
            """CREATE TRIGGER report_current_matches_scope BEFORE UPDATE OF current_revision_id ON reports
                WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM report_revisions r WHERE r.id=NEW.current_revision_id AND r.report_id=NEW.id
                ) BEGIN SELECT RAISE(ABORT, 'report revision scope mismatch'); END""",
        )
        for statement in statements:
            connection.execute(statement)

    @property
    def schema_version(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @staticmethod
    def _snapshot_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    @staticmethod
    def _report_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["scope"] = json.loads(result.pop("scope_json"))
        return result

    def get_idempotent_snapshot(
        self, owner_id: str, key: str, *, request_fingerprint: Any,
    ) -> dict[str, Any] | None:
        owner_id = _text(owner_id, "owner_id")
        key = _text(key, "idempotency_key", maximum=200)
        fingerprint = content_hash(request_fingerprint)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_idempotency WHERE owner_id=? AND key=?", (owner_id, key)
            ).fetchone()
            if row is None:
                return None
            if row["request_sha256"] != fingerprint:
                raise ReportConflict("idempotency key was already used for a different request")
            return self._snapshot_row(connection.execute(
                "SELECT * FROM report_snapshots WHERE id=?", (row["snapshot_id"],)
            ).fetchone())

    def create_snapshot(
        self, scope: Mapping[str, Any], source_revision: int, snapshot: Mapping[str, Any], *,
        created_by: str, rule_version: str = DEFAULT_RULE_VERSION,
        idempotency_key: str | None = None, request_fingerprint: Any = None,
    ) -> dict[str, Any]:
        """Freeze JSON and atomically observe the source revision.

        An idempotency replay returns its original snapshot even if learning data
        has changed. Equal source revisions with different data are a conflict.
        """
        scope = normalize_scope(scope)
        get_rule(rule_version)
        source_revision = _source_revision(source_revision)
        created_by = _text(created_by, "created_by")
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key")
        if not isinstance(snapshot, Mapping):
            raise ValueError("snapshot must be a mapping")
        snapshot_json = canonical_json(dict(snapshot))
        frozen = json.loads(snapshot_json)
        if frozen.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported report snapshot schema version")
        if frozen.get("scope") != scope or frozen.get("source_revision") != source_revision:
            raise ValueError("snapshot scope/source revision does not match the request")
        if frozen.get("rule", {}).get("version") != rule_version:
            raise ValueError("snapshot mastery rule does not match the request")
        fingerprint = content_hash(request_fingerprint if request_fingerprint is not None else {
            "scope": scope, "rule_version": rule_version,
        })
        sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        report_id = report_identifier(scope, rule_version)
        now = time.time()
        with self._write() as connection:
            if idempotency_key is not None:
                previous = connection.execute(
                    "SELECT * FROM report_idempotency WHERE owner_id=? AND key=?", (created_by, idempotency_key)
                ).fetchone()
                if previous is not None:
                    if previous["request_sha256"] != fingerprint:
                        raise ReportConflict("idempotency key was already used for a different request")
                    return self._snapshot_row(connection.execute(
                        "SELECT * FROM report_snapshots WHERE id=?", (previous["snapshot_id"],)
                    ).fetchone())
            connection.execute(
                """INSERT INTO reports(id,scope_json,rule_version,latest_source_revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (report_id, canonical_json(scope), rule_version, source_revision, now, now),
            )
            row = connection.execute(
                "SELECT * FROM report_snapshots WHERE report_id=? AND source_revision=?", (report_id, source_revision)
            ).fetchone()
            if row is not None:
                if row["snapshot_sha256"] != sha256:
                    raise ReportConflict("source revision was already frozen with different report data")
                snapshot_id = row["id"]
            else:
                snapshot_id = "snapshot_" + uuid4().hex
                connection.execute(
                    """INSERT INTO report_snapshots(id,report_id,schema_version,source_revision,rule_version,
                            snapshot_json,snapshot_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, report_id, SNAPSHOT_SCHEMA_VERSION, source_revision, rule_version,
                     snapshot_json, sha256, created_by, now),
                )
                row = connection.execute("SELECT * FROM report_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            connection.execute(
                """UPDATE reports SET latest_source_revision=?,latest_snapshot_id=?,updated_at=?
                   WHERE id=? AND latest_source_revision<=?""",
                (source_revision, snapshot_id, now, report_id, source_revision),
            )
            if idempotency_key is not None:
                connection.execute(
                    "INSERT INTO report_idempotency(owner_id,key,request_sha256,snapshot_id,created_at) VALUES(?,?,?,?,?)",
                    (created_by, idempotency_key, fingerprint, snapshot_id, now),
                )
            return self._snapshot_row(row)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._snapshot_row(connection.execute(
                "SELECT * FROM report_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone())

    @staticmethod
    def _promote(connection: sqlite3.Connection, revision: Mapping[str, Any]) -> bool:
        cursor = connection.execute(
            """UPDATE reports SET current_revision_id=?,updated_at=?
               WHERE id=? AND latest_source_revision=? AND latest_snapshot_id=?
                 AND (current_revision_id IS NULL OR current_revision_id=? OR EXISTS (
                     SELECT 1 FROM report_revisions old WHERE old.id=reports.current_revision_id
                       AND old.source_revision<?
                 ))""",
            (revision["id"], time.time(), revision["report_id"], revision["source_revision"],
             revision["snapshot_id"], revision["id"], revision["source_revision"]),
        )
        return cursor.rowcount > 0

    def create_revision(self, snapshot_id: str, *, promote: bool = True) -> dict[str, Any]:
        """Materialize a frozen snapshot; out-of-order completions cannot regress current."""
        with self._write() as connection:
            snapshot = connection.execute("SELECT * FROM report_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if snapshot is None:
                raise KeyError("report snapshot not found")
            row = connection.execute("SELECT * FROM report_revisions WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if row is None:
                number = connection.execute(
                    "SELECT COALESCE(MAX(revision_number),0)+1 FROM report_revisions WHERE report_id=?",
                    (snapshot["report_id"],),
                ).fetchone()[0]
                revision_id = "revision_" + uuid4().hex
                connection.execute(
                    """INSERT INTO report_revisions(id,report_id,snapshot_id,revision_number,source_revision,
                            rule_version,snapshot_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (revision_id, snapshot["report_id"], snapshot_id, number, snapshot["source_revision"],
                     snapshot["rule_version"], snapshot["snapshot_sha256"], time.time()),
                )
                row = connection.execute("SELECT * FROM report_revisions WHERE id=?", (revision_id,)).fetchone()
            if promote:
                self._promote(connection, row)
            result = dict(row)
            result["is_current"] = connection.execute(
                "SELECT current_revision_id FROM reports WHERE id=?", (row["report_id"],)
            ).fetchone()[0] == row["id"]
            result["snapshot"] = json.loads(snapshot["snapshot_json"])
            return result

    def promote_revision(self, revision_id: str) -> bool:
        with self._write() as connection:
            row = connection.execute("SELECT * FROM report_revisions WHERE id=?", (revision_id,)).fetchone()
            return False if row is None else self._promote(connection, row)

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.*,s.snapshot_json,(p.current_revision_id=r.id) is_current FROM report_revisions r
                   JOIN report_snapshots s ON s.id=r.snapshot_id JOIN reports p ON p.id=r.report_id WHERE r.id=?""",
                (revision_id,),
            ).fetchone()
            result = self._snapshot_row(row)
            if result is not None:
                result["is_current"] = bool(result["is_current"])
            return result

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._report_row(connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())

    def get_current(self, scope: Mapping[str, Any], *, rule_version: str = DEFAULT_RULE_VERSION) -> dict[str, Any] | None:
        report = self.get_report(report_identifier(scope, rule_version))
        return None if report is None or report["current_revision_id"] is None else self.get_revision(report["current_revision_id"])

    def list_reports(self, *, rule_version: str | None = None) -> list[dict[str, Any]]:
        if rule_version is not None:
            get_rule(rule_version)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE (? IS NULL OR rule_version=?) ORDER BY updated_at DESC,id",
                (rule_version, rule_version),
            )
            return [self._report_row(row) for row in rows]

    def list_revisions(self, report_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT r.*,(p.current_revision_id=r.id) is_current FROM report_revisions r
                   JOIN reports p ON p.id=r.report_id WHERE r.report_id=? ORDER BY revision_number DESC""", (report_id,)
            )]
