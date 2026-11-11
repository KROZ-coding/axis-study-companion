"""SQLite AI queue, provider-call journal, and integer-unit budget ledger.

Worker protocol: persist ``start_call`` before sending, accept results only with
an active fencing lease, and reuse succeeded ``list_calls`` after a restart.
Unknown provider results require ``reconcile_call``. Missing billing data keeps
``cost_pending`` and its reservation. ``record_call_usage``/``settle_call_cost``
can settle a late invoice without a lease; neither adopts a business result.

Budget limits gate future sends, never the recording of an already incurred
bill. Necessary grading/summary work may reserve the configured teaching and
project emergency allowance. Unexpected bills beyond that allowance are still
recorded, flagged for notification, and prevent further unaffordable sends.
No provider, application settings, or other project modules are imported here.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

_PRIORITY = {"interaction": 0, "grading": 1, "batch": 2}
_TERMINAL = {"succeeded", "failed", "cancelled"}
_STATUSES = _TERMINAL | {"queued", "running", "awaiting_confirmation", "reconciling"}
_SCOPES = {"student_daily", "teaching", "project"}
_MAX_UNITS = 2**63 - 1
_APPLICATION_ID = 0x41495132  # AIQ2: this is a dedicated queue database.

# Only this complete, exact unversioned schema is eligible for migration. A
# familiar table name or a few matching columns do not identify a queue file.
_LEGACY_SCHEMA = (
    "CREATE TABLE queue_meta (id INTEGER PRIMARY KEY CHECK (id = 1), next_ticket INTEGER NOT NULL CHECK (next_ticket >= 0))",
    "CREATE TABLE budgets (account_id TEXT PRIMARY KEY, available_units INTEGER NOT NULL CHECK (available_units >= 0), reserved_units INTEGER NOT NULL CHECK (reserved_units >= 0))",
    """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, queue_order INTEGER NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('interaction','grading','batch')), payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued','running','awaiting_confirmation','reconciling','succeeded','failed','cancelled')),
        created_at REAL NOT NULL, updated_at REAL NOT NULL, available_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0), paid INTEGER NOT NULL CHECK (paid IN (0,1)),
        budget_account_id TEXT REFERENCES budgets(account_id), budget_limit INTEGER NOT NULL CHECK (budget_limit >= 0),
        budget_reserved INTEGER NOT NULL DEFAULT 0 CHECK (budget_reserved >= 0),
        budget_spent INTEGER NOT NULL DEFAULT 0 CHECK (budget_spent >= 0),
        reservation_baseline INTEGER NOT NULL DEFAULT 0 CHECK (reservation_baseline >= 0),
        reservation_active INTEGER NOT NULL DEFAULT 0 CHECK (reservation_active IN (0,1)),
        lease_owner TEXT, lease_until REAL, generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
        reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_required IN (0,1)), last_error TEXT
    )""",
    "CREATE INDEX jobs_ready_idx ON jobs(status, available_at, kind, queue_order)",
    "CREATE INDEX jobs_lease_idx ON jobs(status, lease_until)",
    """CREATE TABLE steps (
        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        name TEXT NOT NULL, position INTEGER NOT NULL CHECK (position >= 0),
        status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0), output_json TEXT, error TEXT,
        PRIMARY KEY (job_id,name), UNIQUE (job_id,position)
    )""",
    """CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL, created_at REAL NOT NULL, data_json TEXT NOT NULL
    )""",
    "CREATE INDEX events_job_idx ON events(job_id,event_id)",
    """CREATE TABLE upstream_calls (
        call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        step_name TEXT, status TEXT NOT NULL CHECK (status IN ('in_flight','unknown','succeeded','failed','not_executed')),
        generation INTEGER NOT NULL CHECK (generation > 0), result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        FOREIGN KEY (job_id,step_name) REFERENCES steps(job_id,name)
    )""",
    "CREATE INDEX upstream_calls_job_idx ON upstream_calls(job_id,status)",
    """CREATE TABLE call_charges (
        call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
        account_id TEXT REFERENCES budgets(account_id), cost_units INTEGER NOT NULL CHECK (cost_units >= 0), created_at REAL NOT NULL
    )""",
    "CREATE INDEX call_charges_job_idx ON call_charges(job_id)",
)


class QueueError(Exception):
    """Base queue error."""


class QueueSchemaError(QueueError):
    """The database is foreign, malformed, or uses an unsupported schema."""


class InvalidTransition(QueueError):
    """The requested state transition is invalid."""


class LeaseLost(QueueError):
    """The worker no longer owns the current lease generation."""


class InsufficientBudget(QueueError):
    """Future work cannot be reserved under the configured policy."""


class IdempotencyConflict(QueueError):
    """An idempotency key was reused with different immutable data."""


class ReconciliationRequired(QueueError):
    """An upstream result or its usage must be reconciled before a new send."""


@dataclass(frozen=True)
class Budget:
    account_id: str
    available_units: int
    reserved_units: int
    overdraft_units: int = 0


@dataclass(frozen=True)
class BudgetLimit:
    scope_type: str
    scope_id: str
    period: str | None
    limit_units: int
    emergency_units: int
    spent_units: int
    reserved_units: int
    overage_units: int
    emergency_exceeded_units: int
    notification_required: bool
    course_id: str | None = None
    class_id: str | None = None
    configured: bool = True

    @property
    def available_units(self) -> int:
        return max(0, self.limit_units - self.spent_units - self.reserved_units)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: int
    call_id: str
    scope_type: str
    scope_id: str
    period: str | None
    course_id: str | None
    class_id: str | None
    entry_type: str
    units: int
    created_at: float


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    payload: dict[str, Any]
    status: str
    created_at: float
    attempts: int
    max_attempts: int
    paid: bool
    budget_account_id: str | None
    budget_limit: int
    budget_reserved: int
    budget_spent: int
    lease_owner: str | None
    lease_until: float | None
    generation: int
    cancel_requested: bool
    reconciliation_required: bool
    purpose: str = ""
    student_id: str | None = None
    course_id: str | None = None
    class_id: str | None = None
    teaching_scope: str | None = None
    project_id: str | None = None
    budget_day: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    price_version: str | None = None
    input_version: str | None = None
    emergency_limit: int = 0
    budget_overrun_units: int = 0
    over_limit: bool = False
    notification_required: bool = False
    cost_pending: bool = False


@dataclass(frozen=True)
class Step:
    job_id: str
    name: str
    position: int
    status: str
    attempts: int
    output: Any
    error: str | None


@dataclass(frozen=True)
class Event:
    event_id: int
    job_id: str
    event_type: str
    created_at: float
    data: dict[str, Any]


@dataclass(frozen=True)
class CallIntent:
    call_id: str
    job_id: str
    step_name: str | None
    status: str
    should_send: bool
    result: Any = None


@dataclass(frozen=True)
class Call:
    call_id: str
    job_id: str
    step_name: str | None
    status: str
    generation: int
    model_name: str | None
    model_version: str | None
    price_version: str | None
    input_version: str | None
    usage: dict[str, Any] | None
    cost_units: int | None
    cost_pending: bool
    reserved_units: int
    overrun_units: int
    late_cost: bool
    result: Any
    created_at: float
    updated_at: float
    purpose: str = ""
    budget_day: str | None = None
    necessary: bool = False
    metadata: dict[str, Any] | None = None


class JobQueue:
    """Dedicated, file-backed queue; every mutation uses BEGIN IMMEDIATE."""

    SCHEMA_VERSION = 2
    APPLICATION_ID = _APPLICATION_ID
    _SCHEMA = (
        "CREATE TABLE queue_meta (id INTEGER PRIMARY KEY CHECK(id=1),next_ticket INTEGER NOT NULL CHECK(next_ticket>=0))",
        """CREATE TABLE budgets (
            account_id TEXT PRIMARY KEY, available_units INTEGER NOT NULL CHECK(available_units>=0),
            reserved_units INTEGER NOT NULL CHECK(reserved_units>=0), overdraft_units INTEGER NOT NULL DEFAULT 0 CHECK(overdraft_units>=0)
        )""",
        """CREATE TABLE funding_transactions (
            transaction_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES budgets(account_id),
            units INTEGER NOT NULL CHECK(units>=0), created_at REAL NOT NULL
        )""",
        """CREATE TABLE budget_limits (
            scope_type TEXT NOT NULL CHECK(scope_type IN ('student_daily','teaching','project')), scope_id TEXT NOT NULL,
            course_key TEXT NOT NULL DEFAULT '', class_key TEXT NOT NULL DEFAULT '', period_key TEXT NOT NULL DEFAULT '',
            configured INTEGER NOT NULL DEFAULT 0 CHECK(configured IN (0,1)), limit_units INTEGER NOT NULL DEFAULT 0 CHECK(limit_units>=0),
            emergency_units INTEGER NOT NULL DEFAULT 0 CHECK(emergency_units>=0), spent_units INTEGER NOT NULL DEFAULT 0 CHECK(spent_units>=0),
            reserved_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_units>=0), overage_units INTEGER NOT NULL DEFAULT 0 CHECK(overage_units>=0),
            emergency_exceeded_units INTEGER NOT NULL DEFAULT 0 CHECK(emergency_exceeded_units>=0),
            notification_required INTEGER NOT NULL DEFAULT 0 CHECK(notification_required IN (0,1)),
            PRIMARY KEY(scope_type,scope_id,course_key,class_key,period_key)
        )""",
        """CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, queue_order INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('interaction','grading','batch')),
            payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','awaiting_confirmation','reconciling','succeeded','failed','cancelled')),
            created_at REAL NOT NULL, updated_at REAL NOT NULL, available_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), max_attempts INTEGER NOT NULL CHECK(max_attempts>0), paid INTEGER NOT NULL CHECK(paid IN (0,1)),
            budget_account_id TEXT REFERENCES budgets(account_id), budget_limit INTEGER NOT NULL CHECK(budget_limit>=0),
            budget_reserved INTEGER NOT NULL DEFAULT 0 CHECK(budget_reserved>=0), budget_spent INTEGER NOT NULL DEFAULT 0 CHECK(budget_spent>=0),
            reservation_active INTEGER NOT NULL DEFAULT 0 CHECK(reservation_active IN (0,1)),
            lease_owner TEXT, lease_until REAL, generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
            reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_required IN (0,1)), last_error TEXT,
            purpose TEXT NOT NULL DEFAULT '', student_id TEXT, course_id TEXT, class_id TEXT, teaching_scope TEXT, project_id TEXT, budget_day TEXT,
            model_name TEXT, model_version TEXT, price_version TEXT, input_version TEXT,
            emergency_limit INTEGER NOT NULL DEFAULT 0 CHECK(emergency_limit>=0), budget_overrun_units INTEGER NOT NULL DEFAULT 0 CHECK(budget_overrun_units>=0),
            over_limit INTEGER NOT NULL DEFAULT 0 CHECK(over_limit IN (0,1)), notification_required INTEGER NOT NULL DEFAULT 0 CHECK(notification_required IN (0,1)),
            cost_pending INTEGER NOT NULL DEFAULT 0 CHECK(cost_pending IN (0,1))
        )""",
        "CREATE INDEX jobs_ready_idx ON jobs(status,available_at,kind,queue_order)",
        "CREATE INDEX jobs_lease_idx ON jobs(status,lease_until)",
        """CREATE TABLE steps (
            job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE, name TEXT NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
            status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')), attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
            output_json TEXT, error TEXT, PRIMARY KEY(job_id,name), UNIQUE(job_id,position)
        )""",
        """CREATE TABLE events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            event_type TEXT NOT NULL, created_at REAL NOT NULL, data_json TEXT NOT NULL
        )""",
        "CREATE INDEX events_job_idx ON events(job_id,event_id)",
        """CREATE TABLE upstream_calls (
            call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE, step_name TEXT,
            status TEXT NOT NULL CHECK(status IN ('in_flight','unknown','succeeded','failed','not_executed')),
            generation INTEGER NOT NULL CHECK(generation>0), result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            purpose TEXT NOT NULL DEFAULT '', necessary INTEGER NOT NULL DEFAULT 0 CHECK(necessary IN (0,1)), budget_day TEXT NOT NULL,
            model_name TEXT, model_version TEXT, price_version TEXT, input_version TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
            usage_json TEXT, cost_units INTEGER CHECK(cost_units IS NULL OR cost_units>=0), cost_pending INTEGER NOT NULL DEFAULT 1 CHECK(cost_pending IN (0,1)),
            reserved_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_units>=0), overrun_units INTEGER NOT NULL DEFAULT 0 CHECK(overrun_units>=0),
            late_cost INTEGER NOT NULL DEFAULT 0 CHECK(late_cost IN (0,1)),
            FOREIGN KEY(job_id,step_name) REFERENCES steps(job_id,name)
        )""",
        "CREATE INDEX upstream_calls_job_idx ON upstream_calls(job_id,status)",
        "CREATE INDEX upstream_calls_pending_idx ON upstream_calls(job_id,cost_pending)",
        """CREATE TABLE call_charges (
            call_id TEXT PRIMARY KEY REFERENCES upstream_calls(call_id) ON DELETE CASCADE,
            job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE, account_id TEXT REFERENCES budgets(account_id),
            cost_units INTEGER NOT NULL CHECK(cost_units>=0), created_at REAL NOT NULL,
            overrun_units INTEGER NOT NULL DEFAULT 0 CHECK(overrun_units>=0), late INTEGER NOT NULL DEFAULT 0 CHECK(late IN (0,1))
        )""",
        "CREATE INDEX call_charges_job_idx ON call_charges(job_id)",
        """CREATE TABLE call_budget_reservations (
            call_id TEXT NOT NULL REFERENCES upstream_calls(call_id) ON DELETE CASCADE,
            scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, course_key TEXT NOT NULL, class_key TEXT NOT NULL, period_key TEXT NOT NULL,
            allow_emergency INTEGER NOT NULL CHECK(allow_emergency IN (0,1)), reserved_units INTEGER NOT NULL CHECK(reserved_units>=0),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            PRIMARY KEY(call_id,scope_type,scope_id,course_key,class_key,period_key),
            FOREIGN KEY(scope_type,scope_id,course_key,class_key,period_key) REFERENCES budget_limits(scope_type,scope_id,course_key,class_key,period_key)
        )""",
        """CREATE TABLE budget_ledger_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL REFERENCES upstream_calls(call_id) ON DELETE CASCADE,
            scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, course_key TEXT NOT NULL, class_key TEXT NOT NULL, period_key TEXT NOT NULL,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('reserve','settle','release')), units INTEGER NOT NULL CHECK(units>=0), created_at REAL NOT NULL,
            FOREIGN KEY(scope_type,scope_id,course_key,class_key,period_key) REFERENCES budget_limits(scope_type,scope_id,course_key,class_key,period_key)
        )""",
        "CREATE INDEX budget_ledger_call_idx ON budget_ledger_entries(call_id,entry_id)",
    )

    def __init__(self, database: str | Path, *, busy_timeout: float = 30.0) -> None:
        if str(database) == ":memory:":
            raise ValueError("JobQueue requires a file-backed SQLite database")
        self.path = Path(database)
        self.database = str(self.path)
        self.busy_timeout = _positive_number(busy_timeout, "busy_timeout")
        self._preflight()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.budget_ledger = BudgetLedger(self)

    def _inspect_schema(self, db: sqlite3.Connection) -> str:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        app_id = int(db.execute("PRAGMA application_id").fetchone()[0])
        objects = {row[0]: (row[1], _canonical_sql(row[2])) for row in db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )}
        if not objects and version == 0 and app_id == 0:
            return "empty"
        if version == 0 and app_id == 0 and objects == _schema_signature(_LEGACY_SCHEMA):
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise QueueSchemaError("legacy queue contains invalid foreign-key references")
            return "legacy"
        if version == self.SCHEMA_VERSION and app_id == self.APPLICATION_ID and objects == _schema_signature(self._SCHEMA):
            return "current"
        raise QueueSchemaError(f"refusing to modify an unknown or foreign queue schema (version {version})")

    def _preflight(self) -> None:
        """Reject foreign files using read-only SQLite, before persistent pragmas."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=self.busy_timeout)) as db:
                self._inspect_schema(db)
        except sqlite3.Error as error:
            raise QueueSchemaError("existing file is not a readable job-queue database") from error

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=self.busy_timeout, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            # sqlite3.connect(timeout=...) already configures busy_timeout.
            # Call intent must survive a committed transaction before a send.
            db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise

    @classmethod
    def _create_schema(cls, db: sqlite3.Connection) -> None:
        for statement in cls._SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO queue_meta(id,next_ticket) VALUES(1,0)")

    def _initialize(self) -> None:
        with self._transaction() as db:
            # Recheck on the same locked connection, including concurrent opens.
            state = self._inspect_schema(db)
            if state == "empty":
                self._create_schema(db)
            elif state == "legacy":
                self._migrate_legacy(db)
            if state != "current":
                db.execute("PRAGMA user_version=2")
                db.execute("PRAGMA application_id=1095323954")
        with closing(self._connect()) as db:
            if str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise QueueSchemaError("SQLite WAL mode could not be enabled")

    @classmethod
    def _migrate_legacy(cls, db: sqlite3.Connection) -> None:
        # Fixed migration DDL never incorporates database or request values.
        db.execute("DROP INDEX jobs_ready_idx")
        db.execute("DROP INDEX jobs_lease_idx")
        db.execute("DROP INDEX events_job_idx")
        db.execute("DROP INDEX upstream_calls_job_idx")
        db.execute("DROP INDEX call_charges_job_idx")
        db.execute("ALTER TABLE queue_meta RENAME TO legacy_queue_meta")
        db.execute("ALTER TABLE budgets RENAME TO legacy_budgets")
        db.execute("ALTER TABLE jobs RENAME TO legacy_jobs")
        db.execute("ALTER TABLE steps RENAME TO legacy_steps")
        db.execute("ALTER TABLE events RENAME TO legacy_events")
        db.execute("ALTER TABLE upstream_calls RENAME TO legacy_upstream_calls")
        db.execute("ALTER TABLE call_charges RENAME TO legacy_call_charges")
        cls._create_schema(db)
        db.execute("UPDATE queue_meta SET next_ticket=(SELECT next_ticket FROM legacy_queue_meta WHERE id=1) WHERE id=1")
        db.execute("INSERT INTO budgets(account_id,available_units,reserved_units) SELECT account_id,available_units,0 FROM legacy_budgets")
        db.execute("""INSERT INTO jobs(
            job_id,queue_order,kind,payload_json,status,created_at,updated_at,available_at,attempts,max_attempts,paid,
            budget_account_id,budget_limit,budget_spent,reservation_active,lease_owner,lease_until,generation,
            cancel_requested,reconciliation_required,last_error)
            SELECT job_id,queue_order,kind,payload_json,status,created_at,updated_at,available_at,attempts,max_attempts,paid,
            budget_account_id,budget_limit,budget_spent,reservation_active,lease_owner,lease_until,generation,
            cancel_requested,reconciliation_required,last_error FROM legacy_jobs""")
        db.execute("""UPDATE jobs SET budget_reserved=(SELECT CASE WHEN l.reservation_active=1
            THEN MAX(0,l.budget_reserved-MAX(0,l.budget_spent-l.reservation_baseline)) ELSE 0 END
            FROM legacy_jobs l WHERE l.job_id=jobs.job_id),purpose=CASE WHEN kind='grading' THEN 'grading' ELSE '' END""")
        db.execute("UPDATE budgets SET reserved_units=COALESCE((SELECT SUM(j.budget_reserved) FROM jobs j WHERE j.budget_account_id=budgets.account_id),0)")
        db.execute("INSERT INTO steps SELECT * FROM legacy_steps")
        db.execute("INSERT INTO events SELECT * FROM legacy_events")
        db.execute("""INSERT INTO upstream_calls(call_id,job_id,step_name,status,generation,result_json,created_at,updated_at,purpose,necessary,budget_day)
            SELECT c.call_id,c.job_id,c.step_name,c.status,c.generation,c.result_json,c.created_at,c.updated_at,j.purpose,
            CASE WHEN j.kind='grading' THEN 1 ELSE 0 END,strftime('%Y-%m-%d',c.created_at,'unixepoch')
            FROM legacy_upstream_calls c JOIN jobs j ON j.job_id=c.job_id""")
        orphan = db.execute("""SELECT 1 FROM legacy_call_charges c LEFT JOIN upstream_calls u ON u.call_id=c.call_id
            WHERE u.call_id IS NULL OR u.job_id<>c.job_id LIMIT 1""").fetchone()
        if orphan:
            raise QueueSchemaError("legacy queue has a charge without a matching call")
        db.execute("INSERT INTO call_charges(call_id,job_id,account_id,cost_units,created_at) SELECT * FROM legacy_call_charges")
        db.execute("""UPDATE upstream_calls SET cost_units=(SELECT c.cost_units FROM call_charges c WHERE c.call_id=upstream_calls.call_id),
            cost_pending=CASE WHEN EXISTS(SELECT 1 FROM call_charges c WHERE c.call_id=upstream_calls.call_id) THEN 0 ELSE 1 END""")
        for row in db.execute("SELECT * FROM jobs").fetchall():
            pending_calls = db.execute("SELECT call_id FROM upstream_calls WHERE job_id=? AND cost_pending=1 ORDER BY created_at,rowid", (row["job_id"],)).fetchall()
            if pending_calls:
                db.execute("UPDATE upstream_calls SET reserved_units=? WHERE call_id=?", (row["budget_reserved"], pending_calls[0]["call_id"]))
            cls._refresh_pending(db, row["job_id"])
        for call in db.execute("SELECT * FROM upstream_calls ORDER BY created_at,rowid").fetchall():
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (call["job_id"],)).fetchone()
            cls._bind_scopes(db, job, call["call_id"], call["budget_day"], call["reserved_units"], call["created_at"], enforce=False)
            if call["cost_units"] is not None:
                cls._settle_scopes(db, call["call_id"], call["cost_units"], call["updated_at"])
        db.execute("DROP TABLE legacy_call_charges")
        db.execute("DROP TABLE legacy_upstream_calls")
        db.execute("DROP TABLE legacy_events")
        db.execute("DROP TABLE legacy_steps")
        db.execute("DROP TABLE legacy_jobs")
        db.execute("DROP TABLE legacy_budgets")
        db.execute("DROP TABLE legacy_queue_meta")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise QueueSchemaError("legacy migration produced invalid references")

    @staticmethod
    def _ticket(db: sqlite3.Connection) -> int:
        ticket = _sum_units(db.execute("SELECT next_ticket FROM queue_meta WHERE id=1").fetchone()[0], 1)
        db.execute("UPDATE queue_meta SET next_ticket=? WHERE id=1", (ticket,))
        return ticket

    @staticmethod
    def _event(db: sqlite3.Connection, job_id: str, kind: str, now: float, data: Mapping[str, Any] | None = None) -> Event:
        encoded = _json(dict(data or {}))
        cursor = db.execute("INSERT INTO events(job_id,event_type,created_at,data_json) VALUES(?,?,?,?)", (job_id, kind, now, encoded))
        return Event(int(cursor.lastrowid), job_id, kind, now, json.loads(encoded))

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        fields = {name: row[name] for name in Job.__dataclass_fields__ if name != "payload"}
        for name in ("paid", "cancel_requested", "reconciliation_required", "over_limit", "notification_required", "cost_pending"):
            fields[name] = bool(fields[name])
        return Job(payload=json.loads(row["payload_json"]), **fields)

    @staticmethod
    def _step(row: sqlite3.Row) -> Step:
        return Step(row["job_id"], row["name"], row["position"], row["status"], row["attempts"], _loads(row["output_json"]), row["error"])

    @staticmethod
    def _call(row: sqlite3.Row) -> Call:
        fields = {name: row[name] for name in Call.__dataclass_fields__ if name not in {"result", "usage", "metadata"}}
        for name in ("necessary", "cost_pending", "late_cost"):
            fields[name] = bool(fields[name])
        return Call(result=_loads(row["result_json"]), usage=_loads(row["usage_json"]), metadata=_loads(row["metadata_json"]), **fields)

    @staticmethod
    def _job_row(db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown job: {job_id}")
        return row

    @staticmethod
    def _call_row(db: sqlite3.Connection, call_id: str, job_id: str | None = None) -> sqlite3.Row:
        row = db.execute("SELECT * FROM upstream_calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown upstream call: {call_id}")
        if job_id is not None and row["job_id"] != job_id:
            raise IdempotencyConflict("call_id does not belong to job_id")
        return row

    @classmethod
    def _lease(cls, db: sqlite3.Connection, job_id: str, worker_id: str, generation: int, now: float) -> sqlite3.Row:
        worker_id = _text(worker_id, "worker_id")
        generation = _positive_integer(generation, "generation")
        row = cls._job_row(db, job_id)
        if row["status"] != "running" or row["lease_owner"] != worker_id or row["generation"] != generation or row["lease_until"] is None or row["lease_until"] <= now:
            raise LeaseLost(f"lease lost for job {job_id}")
        return row

    @staticmethod
    def _necessary(job: sqlite3.Row) -> bool:
        return job["purpose"] in {"grading", "summary"} or (not job["purpose"] and job["kind"] == "grading")

    @staticmethod
    def _automatic_recovery(job: sqlite3.Row) -> bool:
        return job["purpose"] == "grading" or (not job["purpose"] and job["kind"] == "grading")

    @staticmethod
    def _reserve_wallet(db: sqlite3.Connection, job: sqlite3.Row) -> None:
        if job["budget_account_id"] is None:
            return
        target = max(0, job["budget_limit"] - job["budget_spent"])
        additional = max(0, target - job["budget_reserved"])
        account = db.execute("SELECT * FROM budgets WHERE account_id=?", (job["budget_account_id"],)).fetchone()
        if account is None or account["available_units"] < additional or account["overdraft_units"]:
            raise InsufficientBudget("insufficient funded wallet balance")
        db.execute("UPDATE budgets SET available_units=?,reserved_units=? WHERE account_id=?", (account["available_units"] - additional, _sum_units(account["reserved_units"], additional), job["budget_account_id"]))
        db.execute("UPDATE jobs SET budget_reserved=?,reservation_active=1 WHERE job_id=?", (_sum_units(job["budget_reserved"], additional), job["job_id"]))

    @staticmethod
    def _release_wallet(db: sqlite3.Connection, job: sqlite3.Row) -> None:
        if not job["reservation_active"]:
            return
        # Preserve only money that an unsettled or unknown call can still owe.
        pending = db.execute("SELECT COALESCE(SUM(reserved_units),0) FROM upstream_calls WHERE job_id=? AND cost_pending=1", (job["job_id"],)).fetchone()[0]
        keep = min(job["budget_reserved"], pending)
        refund = job["budget_reserved"] - keep
        if job["budget_account_id"] is not None and refund:
            account = db.execute("SELECT * FROM budgets WHERE account_id=?", (job["budget_account_id"],)).fetchone()
            debt_paid = min(refund, account["overdraft_units"])
            db.execute("UPDATE budgets SET available_units=?,reserved_units=?,overdraft_units=? WHERE account_id=?", (_sum_units(account["available_units"], refund - debt_paid), account["reserved_units"] - refund, account["overdraft_units"] - debt_paid, job["budget_account_id"]))
        db.execute("UPDATE jobs SET budget_reserved=?,reservation_active=? WHERE job_id=?", (keep, int(bool(keep)), job["job_id"]))

    @staticmethod
    def _refresh_pending(db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        pending = db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND cost_pending=1 LIMIT 1", (job_id,)).fetchone() is not None
        db.execute("UPDATE jobs SET cost_pending=? WHERE job_id=?", (int(pending), job_id))
        return JobQueue._job_row(db, job_id)

    @staticmethod
    def _ledger_entry(db: sqlite3.Connection, call_id: str, key: tuple[str, str, str, str, str], kind: str, units: int, now: float) -> None:
        db.execute("INSERT INTO budget_ledger_entries(call_id,scope_type,scope_id,course_key,class_key,period_key,entry_type,units,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (call_id, *key, kind, units, now))

    @staticmethod
    def _scope_categories(job: sqlite3.Row, day: str) -> tuple[tuple[str, str, str, str, str], ...]:
        primary = ("student_daily", job["student_id"], job["course_id"] or "", job["class_id"] or "", day) if job["student_id"] and not JobQueue._necessary(job) else ("teaching", job["teaching_scope"] or job["course_id"] or "default", "", "", "")
        return primary, ("project", job["project_id"] or "default", "", "", "")

    @classmethod
    def _resolve_scope_limits(cls, db: sqlite3.Connection, category: tuple[str, str, str, str, str]) -> tuple[sqlite3.Row, ...]:
        scope_type, scope_id, course, classroom, period = category
        variants = [(course, classroom)]
        if scope_type == "student_daily":
            variants = list(dict.fromkeys((("", ""), (course, ""), (course, classroom))))
        keys: list[tuple[str, str, str, str, str]] = []
        for course_key, class_key in variants:
            key = (scope_type, scope_id, course_key, class_key, period)
            if scope_type == "student_daily":
                template = db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key='' AND configured=1", key[:4]).fetchone()
                exact = db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", key).fetchone()
                if exact is None and template is not None:
                    db.execute("INSERT INTO budget_limits(scope_type,scope_id,course_key,class_key,period_key,configured,limit_units,emergency_units) VALUES(?,?,?,?,?,1,?,?)", (*key, template["limit_units"], template["emergency_units"]))
            # Keep aggregate usage even before a policy is assigned. Creating a
            # tighter global/course policy later must not erase earlier spend.
            db.execute("INSERT OR IGNORE INTO budget_limits(scope_type,scope_id,course_key,class_key,period_key) VALUES(?,?,?,?,?)", key)
            keys.append(key)
        if category not in keys:
            db.execute("INSERT OR IGNORE INTO budget_limits(scope_type,scope_id,course_key,class_key,period_key) VALUES(?,?,?,?,?)", category)
            keys.append(category)
        return tuple(db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", key).fetchone() for key in keys)

    @classmethod
    def _bind_scopes(cls, db: sqlite3.Connection, job: sqlite3.Row, call_id: str, day: str, units: int, now: float, *, enforce: bool = True) -> None:
        billable = bool(job["paid"] or units)
        wallet_authorized = bool(job["budget_account_id"] is not None and job["budget_limit"])
        necessary = cls._necessary(job)
        for category in cls._scope_categories(job, day):
            limits = cls._resolve_scope_limits(db, category)
            if enforce and billable and not wallet_authorized and not any(row["configured"] for row in limits):
                raise InsufficientBudget(f"no configured {category[0]} budget for {category[1]}")
            for row in limits:
                allow_emergency = bool(necessary and row["scope_type"] != "student_daily")
                new_reserved = _sum_units(row["reserved_units"], units)
                if enforce and row["configured"]:
                    cap = _sum_units(row["limit_units"], row["emergency_units"] if allow_emergency else 0)
                    if _sum_units(row["spent_units"], new_reserved) > cap:
                        raise InsufficientBudget(f"{row['scope_type']} budget exhausted for {row['scope_id']}")
                key = _limit_key(row)
                db.execute("INSERT INTO call_budget_reservations(call_id,scope_type,scope_id,course_key,class_key,period_key,allow_emergency,reserved_units) VALUES(?,?,?,?,?,?,?,?)", (call_id, *key, int(allow_emergency), units))
                db.execute("UPDATE budget_limits SET reserved_units=? WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", (new_reserved, *key))
                cls._ledger_entry(db, call_id, key, "reserve", units, now)

    @classmethod
    def _settle_scopes(cls, db: sqlite3.Connection, call_id: str, cost: int, now: float) -> tuple[bool, bool]:
        overage_flag = False
        emergency_breach = False
        for binding in db.execute("SELECT * FROM call_budget_reservations WHERE call_id=? AND active=1", (call_id,)).fetchall():
            key = _limit_key(binding)
            limit = db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", key).fetchone()
            spent = _sum_units(limit["spent_units"], cost)
            overage = max(0, spent - limit["limit_units"]) if limit["configured"] else 0
            cap = _sum_units(limit["limit_units"], limit["emergency_units"] if binding["allow_emergency"] else 0)
            exceeded = max(0, spent - cap) if limit["configured"] else 0
            overage_flag |= bool(overage)
            emergency_breach |= bool(exceeded)
            db.execute("UPDATE budget_limits SET spent_units=?,reserved_units=?,overage_units=?,emergency_exceeded_units=?,notification_required=MAX(notification_required,?) WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", (spent, limit["reserved_units"] - binding["reserved_units"], overage, exceeded, int(bool(overage)), *key))
            db.execute("UPDATE call_budget_reservations SET active=0 WHERE call_id=? AND scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", (call_id, *key))
            cls._ledger_entry(db, call_id, key, "settle", cost, now)
            if binding["reserved_units"] > cost:
                cls._ledger_entry(db, call_id, key, "release", binding["reserved_units"] - cost, now)
        return overage_flag, emergency_breach

    @staticmethod
    def _merge_usage(old: str | None, usage: Mapping[str, Any] | None) -> str | None:
        if usage is None:
            return old
        incoming = json.loads(_json(dict(usage)))
        existing = _loads(old) or {}
        for key, value in incoming.items():
            if key in existing and existing[key] != value:
                raise IdempotencyConflict(f"usage was already recorded differently for {key}")
            existing[key] = value
        return _json(existing)

    @classmethod
    def _record_usage_tx(cls, db: sqlite3.Connection, call: sqlite3.Row, usage: Mapping[str, Any] | None, cost: int | None, cost_pending: bool, now: float) -> bool:
        encoded = cls._merge_usage(call["usage_json"], usage)
        if cost is None and call["cost_units"] is None and not cost_pending:
            raise ValueError("a final integer cost is required to clear cost_pending")
        if call["cost_units"] is not None and cost is not None and call["cost_units"] != cost:
            raise IdempotencyConflict(f"call_id already settled with a different cost: {call['call_id']}")
        db.execute("UPDATE upstream_calls SET usage_json=?,updated_at=? WHERE call_id=?", (encoded, now, call["call_id"]))
        if cost is None or call["cost_units"] is not None:
            return False
        job = cls._job_row(db, call["job_id"])
        new_spent = _sum_units(job["budget_spent"], cost)
        wallet_overage = max(0, new_spent - job["budget_limit"]) if job["budget_account_id"] is not None else 0
        estimate_overage = max(0, cost - call["reserved_units"])
        wallet_cap = _sum_units(job["budget_limit"], job["emergency_limit"] if call["necessary"] else 0)
        breached = bool(job["budget_account_id"] is not None and new_spent > wallet_cap)
        late = bool(job["status"] != "running" or job["cancel_requested"] or job["generation"] != call["generation"] or job["lease_until"] is None or job["lease_until"] <= now)
        if job["budget_account_id"] is not None:
            account = db.execute("SELECT * FROM budgets WHERE account_id=?", (job["budget_account_id"],)).fetchone()
            from_reserved = min(job["budget_reserved"], cost)
            remainder = cost - from_reserved
            from_available = min(account["available_units"], remainder)
            debt = remainder - from_available
            db.execute("UPDATE budgets SET reserved_units=?,available_units=?,overdraft_units=? WHERE account_id=?", (account["reserved_units"] - from_reserved, account["available_units"] - from_available, _sum_units(account["overdraft_units"], debt), job["budget_account_id"]))
            db.execute("UPDATE jobs SET budget_reserved=? WHERE job_id=?", (job["budget_reserved"] - from_reserved, job["job_id"]))
        scope_overage, scope_breach = cls._settle_scopes(db, call["call_id"], cost, now)
        notification = bool(job["notification_required"] or wallet_overage or estimate_overage or scope_overage or scope_breach)
        blocked = bool(job["over_limit"] or estimate_overage or wallet_overage or scope_breach or breached)
        db.execute("INSERT INTO call_charges(call_id,job_id,account_id,cost_units,created_at,overrun_units,late) VALUES(?,?,?,?,?,?,?)", (call["call_id"], call["job_id"], job["budget_account_id"], cost, now, estimate_overage, int(late)))
        db.execute("UPDATE upstream_calls SET cost_units=?,cost_pending=0,overrun_units=?,late_cost=?,updated_at=? WHERE call_id=?", (cost, estimate_overage, int(late), now, call["call_id"]))
        db.execute("UPDATE jobs SET budget_spent=?,budget_overrun_units=?,over_limit=?,notification_required=? WHERE job_id=?", (new_spent, wallet_overage, int(blocked), int(notification), call["job_id"]))
        cls._event(db, call["job_id"], "late_call_cost_settled" if late else "call_cost_settled", now, {"call_id": call["call_id"], "cost_units": cost, "overrun_units": estimate_overage, "notification_required": notification, "emergency_exceeded": bool(breached or scope_breach)})
        refreshed = cls._refresh_pending(db, call["job_id"])
        if refreshed["status"] in _TERMINAL:
            cls._release_wallet(db, refreshed)
        return True

    def fund_budget(self, account_id: str, units: int, *, transaction_id: str | None = None) -> Budget:
        """Credit a wallet once per transaction_id; repay recorded debt first."""
        account_id = _text(account_id, "account_id")
        units = _integer(units, "units")
        transaction_id = uuid4().hex if transaction_id is None else _text(transaction_id, "transaction_id")
        with self._transaction() as db:
            previous = db.execute("SELECT account_id,units FROM funding_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
            if previous is not None:
                if previous["account_id"] != account_id or previous["units"] != units:
                    raise IdempotencyConflict("funding transaction reused with different data")
            else:
                db.execute("INSERT OR IGNORE INTO budgets(account_id,available_units,reserved_units) VALUES(?,0,0)", (account_id,))
                row = db.execute("SELECT * FROM budgets WHERE account_id=?", (account_id,)).fetchone()
                debt_paid = min(units, row["overdraft_units"])
                db.execute("UPDATE budgets SET available_units=?,overdraft_units=? WHERE account_id=?", (_sum_units(row["available_units"], units - debt_paid), row["overdraft_units"] - debt_paid, account_id))
                db.execute("INSERT INTO funding_transactions VALUES(?,?,?,?)", (transaction_id, account_id, units, time.time()))
            row = db.execute("SELECT * FROM budgets WHERE account_id=?", (account_id,)).fetchone()
            return Budget(account_id, row["available_units"], row["reserved_units"], row["overdraft_units"])

    def budget(self, account_id: str) -> Budget:
        account_id = _text(account_id, "account_id")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM budgets WHERE account_id=?", (account_id,)).fetchone()
        return Budget(account_id, 0, 0, 0) if row is None else Budget(account_id, row["available_units"], row["reserved_units"], row["overdraft_units"])

    def enqueue(
        self, kind: str, payload: Mapping[str, Any] | None = None, *, steps: Sequence[str] = (), job_id: str | None = None,
        paid: bool = False, budget_account_id: str | None = None, budget_limit: int = 0, emergency_limit: int = 0,
        max_attempts: int = 3, available_at: float | None = None, purpose: str = "", student_id: str | None = None,
        course_id: str | None = None, class_id: str | None = None, teaching_scope: str | None = None,
        project_id: str | None = None, budget_day: str | None = None, model_name: str | None = None,
        model_version: str | None = None, price_version: str | None = None, input_version: str | None = None,
    ) -> Job:
        if kind not in _PRIORITY:
            raise ValueError(f"kind must be one of {tuple(_PRIORITY)}")
        _boolean(paid, "paid")
        budget_limit = _integer(budget_limit, "budget_limit")
        emergency_limit = _integer(emergency_limit, "emergency_limit")
        _sum_units(budget_limit, emergency_limit)
        max_attempts = _positive_integer(max_attempts, "max_attempts")
        if budget_limit and budget_account_id is None:
            raise ValueError("budget_account_id is required for a positive wallet budget_limit")
        budget_account_id = _optional_text(budget_account_id)
        options = {"student_id": student_id, "course_id": course_id, "class_id": class_id, "teaching_scope": teaching_scope,
                   "project_id": project_id, "model_name": model_name, "model_version": model_version, "price_version": price_version, "input_version": input_version}
        options = {key: _optional_text(value) for key, value in options.items()}
        if class_id is not None and course_id is None:
            raise ValueError("class_id requires course_id")
        purpose = _text(purpose, "purpose") if purpose else ""
        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        encoded = _json(dict(payload or {}))
        if isinstance(steps, (str, bytes)):
            raise TypeError("steps must be a sequence of names")
        step_names = tuple(_text(name, "step name") for name in steps)
        if len(set(step_names)) != len(step_names):
            raise ValueError("step names must be unique")
        job_id = uuid4().hex if job_id is None else _text(job_id, "job_id")
        day = None if budget_day is None else _date_key(budget_day)
        ready_at = None if available_at is None else _time(available_at)
        with self._transaction() as db:
            now = time.time()
            values = (job_id, self._ticket(db), kind, encoded, "queued", now, now, now if ready_at is None else ready_at,
                      max_attempts, int(paid), budget_account_id, budget_limit, emergency_limit, purpose, day,
                      options["student_id"], options["course_id"], options["class_id"], options["teaching_scope"],
                      options["project_id"], options["model_name"], options["model_version"], options["price_version"], options["input_version"])
            db.execute("""INSERT INTO jobs(
                job_id,queue_order,kind,payload_json,status,created_at,updated_at,available_at,
                max_attempts,paid,budget_account_id,budget_limit,emergency_limit,purpose,budget_day,
                student_id,course_id,class_id,teaching_scope,project_id,model_name,model_version,price_version,input_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
            for position, name in enumerate(step_names):
                db.execute("INSERT INTO steps(job_id,name,position,status) VALUES(?,?,?,'pending')", (job_id, name, position))
            if budget_account_id is not None:
                self._reserve_wallet(db, self._job_row(db, job_id))
            self._event(db, job_id, "enqueued", now, {"kind": kind, "paid": paid, "purpose": purpose})
            return self._job(self._job_row(db, job_id))

    def get_job(self, job_id: str) -> Job:
        with closing(self._connect()) as db:
            return self._job(self._job_row(db, job_id))

    def list_jobs(self, *, status: str | None = None, kind: str | None = None, limit: int = 100, offset: int = 0) -> tuple[Job, ...]:
        limit, offset = _positive_integer(limit, "limit"), _integer(offset, "offset")
        if status is not None and status not in _STATUSES:
            raise ValueError("invalid job status")
        if kind is not None and kind not in _PRIORITY:
            raise ValueError("invalid job kind")
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT * FROM jobs
                WHERE (? IS NULL OR status=?) AND (? IS NULL OR kind=?)
                ORDER BY created_at,job_id LIMIT ? OFFSET ?""", (status, status, kind, kind, limit, offset)).fetchall()
        return tuple(self._job(row) for row in rows)

    def list_steps(self, job_id: str) -> tuple[Step, ...]:
        with closing(self._connect()) as db:
            self._job_row(db, job_id)
            rows = db.execute("SELECT * FROM steps WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
        return tuple(self._step(row) for row in rows)

    def append_event(self, job_id: str, event_type: str, data: Mapping[str, Any] | None = None, *, worker_id: str | None = None, generation: int | None = None, now: float | None = None) -> Event:
        event_type = _text(event_type, "event_type")
        if data is not None and not isinstance(data, Mapping):
            raise TypeError("event data must be a mapping")
        if (worker_id is None) != (generation is None):
            raise ValueError("worker_id and generation must be provided together")
        with self._transaction() as db:
            current = _now(now)
            if worker_id is not None:
                self._lease(db, job_id, worker_id, generation, current)
            else:
                self._job_row(db, job_id)
            return self._event(db, job_id, event_type, current, data)

    def get_events(self, after_id: int = 0, *, job_id: str | None = None, limit: int = 1000) -> tuple[Event, ...]:
        after_id, limit = _integer(after_id, "after_id"), _positive_integer(limit, "limit")
        with closing(self._connect()) as db:
            if job_id is not None:
                self._job_row(db, job_id)
                rows = db.execute("SELECT * FROM events WHERE event_id>? AND job_id=? ORDER BY event_id LIMIT ?", (after_id, job_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE event_id>? ORDER BY event_id LIMIT ?", (after_id, limit)).fetchall()
        return tuple(Event(row["event_id"], row["job_id"], row["event_type"], row["created_at"], _loads(row["data_json"])) for row in rows)

    def list_events(self, job_id: str, *, after: int = 0) -> tuple[Event, ...]:
        return self.get_events(after, job_id=job_id, limit=_MAX_UNITS)

    def claim(self, worker_id: str, *, lease_seconds: float = 30.0, now: float | None = None) -> Job | None:
        worker_id = _text(worker_id, "worker_id")
        lease_seconds = _positive_number(lease_seconds, "lease_seconds")
        with self._transaction() as db:
            current = _now(now)
            self._recover_expired(db, current)
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 AND available_at<=? ORDER BY CASE kind WHEN 'interaction' THEN 0 WHEN 'grading' THEN 1 ELSE 2 END,queue_order LIMIT 1", (current,)).fetchone()
            if row is None:
                return None
            generation = _sum_units(row["generation"], 1)
            until = _time(current + lease_seconds)
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,generation=?,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?", (generation, worker_id, until, current, row["job_id"]))
            self._event(db, row["job_id"], "claimed", current, {"worker_id": worker_id, "generation": generation, "lease_until": until})
            return self._job(self._job_row(db, row["job_id"]))

    def heartbeat(self, job_id: str, worker_id: str, generation: int, *, lease_seconds: float = 30.0, now: float | None = None) -> Job:
        lease_seconds = _positive_number(lease_seconds, "lease_seconds")
        with self._transaction() as db:
            current = _now(now)
            row = self._lease(db, job_id, worker_id, generation, current)
            until = max(row["lease_until"], _time(current + lease_seconds))
            db.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?", (until, current, job_id))
            self._event(db, job_id, "lease_renewed", current, {"generation": generation, "lease_until": until})
            return self._job(self._job_row(db, job_id))

    def start_step(self, job_id: str, name: str, worker_id: str, generation: int, *, now: float | None = None) -> Step:
        name = _text(name, "step name")
        with self._transaction() as db:
            current = _now(now)
            job = self._lease(db, job_id, worker_id, generation, current)
            if job["cancel_requested"]:
                raise InvalidTransition("job cancellation requested")
            step = db.execute("SELECT * FROM steps WHERE job_id=? AND name=?", (job_id, name)).fetchone()
            if step is None:
                raise LookupError(f"unknown step: {name}")
            if step["status"] not in ("pending", "failed"):
                raise InvalidTransition(f"cannot start step in state {step['status']}")
            db.execute("UPDATE steps SET status='running',attempts=attempts+1,output_json=NULL,error=NULL WHERE job_id=? AND name=?", (job_id, name))
            self._event(db, job_id, "step_started", current, {"step": name, "attempt": step["attempts"] + 1, "generation": generation})
            return self._step(db.execute("SELECT * FROM steps WHERE job_id=? AND name=?", (job_id, name)).fetchone())

    def finish_step(self, job_id: str, name: str, worker_id: str, generation: int, *, success: bool, output: Any = None, error: str | None = None, now: float | None = None) -> Step:
        _boolean(success, "success")
        name = _text(name, "step name")
        encoded = _json(output) if success else None
        with self._transaction() as db:
            current = _now(now)
            job = self._lease(db, job_id, worker_id, generation, current)
            if job["cancel_requested"]:
                raise InvalidTransition("cancelled work cannot adopt a business result")
            step = db.execute("SELECT * FROM steps WHERE job_id=? AND name=?", (job_id, name)).fetchone()
            if step is None:
                raise LookupError(f"unknown step: {name}")
            if step["status"] != "running":
                raise InvalidTransition(f"cannot finish step in state {step['status']}")
            if db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND status IN ('in_flight','unknown') LIMIT 1", (job_id,)).fetchone():
                raise ReconciliationRequired("upstream result must be reconciled before finishing a step")
            status = "succeeded" if success else "failed"
            db.execute("UPDATE steps SET status=?,output_json=?,error=? WHERE job_id=? AND name=?", (status, encoded, None if success else error, job_id, name))
            self._event(db, job_id, "step_finished", current, {"step": name, "status": status, "generation": generation})
            return self._step(db.execute("SELECT * FROM steps WHERE job_id=? AND name=?", (job_id, name)).fetchone())

    def start_call(
        self, job_id: str, call_id: str, worker_id: str, generation: int, *, step_name: str | None = None,
        reserve_units: int | None = None, model_name: str | None = None, model_version: str | None = None,
        price_version: str | None = None, input_version: str | None = None, metadata: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> CallIntent:
        """Commit intent and conservative reservations before making one send.

        Omitted reserve_units uses the legacy job wallet's remaining allowance,
        otherwise zero. Paid scoped work requires a positive explicit estimate.
        Reusing a completed call_id returns should_send=False and its result.
        """
        call_id, step_name = _text(call_id, "call_id"), _optional_text(step_name)
        if reserve_units is not None:
            reserve_units = _integer(reserve_units, "reserve_units")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        encoded_metadata = _json(dict(metadata or {}))
        supplied = tuple(_optional_text(value) for value in (model_name, model_version, price_version, input_version))
        with self._transaction() as db:
            current = _now(now)
            job = self._lease(db, job_id, worker_id, generation, current)
            if job["cancel_requested"]:
                raise InvalidTransition("job cancellation requested")
            actual = tuple(value if value is not None else job[column] for value, column in zip(supplied, ("model_name", "model_version", "price_version", "input_version")))
            old = db.execute("SELECT * FROM upstream_calls WHERE call_id=?", (call_id,)).fetchone()
            if old is not None:
                if old["job_id"] != job_id or old["step_name"] != step_name:
                    raise IdempotencyConflict("call_id belongs to another request")
                for value, column in zip(supplied, ("model_name", "model_version", "price_version", "input_version")):
                    if value is not None and old[column] != value:
                        raise IdempotencyConflict(f"call_id reused with a different {column}")
                if reserve_units is not None and reserve_units != old["reserved_units"]:
                    raise IdempotencyConflict("call_id reused with a different reservation")
                if metadata is not None and _loads(old["metadata_json"]) != _loads(encoded_metadata):
                    raise IdempotencyConflict("call_id reused with different metadata")
                if old["status"] in ("in_flight", "unknown"):
                    raise ReconciliationRequired("unknown or in-flight call cannot be resent")
                return CallIntent(call_id, job_id, step_name, old["status"], False, _loads(old["result_json"]))
            if db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND (status IN ('in_flight','unknown') OR cost_pending=1) LIMIT 1", (job_id,)).fetchone():
                raise ReconciliationRequired("previous result or billing usage must be reconciled first")
            necessary = self._necessary(job)
            if job["over_limit"] and not necessary:
                raise InsufficientBudget("optional work stopped after an over-budget call")
            if job["attempts"] > job["max_attempts"]:
                raise InvalidTransition("maximum attempts exhausted; only local recovery is allowed")
            if step_name is not None:
                step = db.execute("SELECT status FROM steps WHERE job_id=? AND name=?", (job_id, step_name)).fetchone()
                if step is None:
                    raise LookupError(f"unknown step: {step_name}")
                if step["status"] != "running":
                    raise InvalidTransition("upstream call requires a running step")
            units = reserve_units
            if units is None:
                units = max(0, job["budget_limit"] - job["budget_spent"]) if job["budget_account_id"] is not None else 0
            if job["paid"] and units == 0:
                raise InsufficientBudget("paid work requires a positive conservative reservation")
            if job["budget_account_id"] is not None:
                cap = _sum_units(job["budget_limit"], job["emergency_limit"] if necessary else 0)
                if _sum_units(job["budget_spent"], units) > cap:
                    raise InsufficientBudget("job wallet and emergency cap exhausted")
                extra = max(0, units - job["budget_reserved"])
                if extra:
                    account = db.execute("SELECT * FROM budgets WHERE account_id=?", (job["budget_account_id"],)).fetchone()
                    if account["available_units"] < extra or account["overdraft_units"]:
                        raise InsufficientBudget("wallet needs funding before another call")
                    db.execute("UPDATE budgets SET available_units=?,reserved_units=? WHERE account_id=?", (account["available_units"] - extra, _sum_units(account["reserved_units"], extra), job["budget_account_id"]))
                    db.execute("UPDATE jobs SET budget_reserved=?,reservation_active=1 WHERE job_id=?", (_sum_units(job["budget_reserved"], extra), job_id))
            day = job["budget_day"] or _utc_day(current)
            db.execute("""INSERT INTO upstream_calls(call_id,job_id,step_name,status,generation,created_at,updated_at,purpose,necessary,budget_day,
                model_name,model_version,price_version,input_version,metadata_json,reserved_units)
                VALUES(?,?,?,'in_flight',?,?,?,?,?,?,?,?,?,?,?,?)""", (call_id, job_id, step_name, generation, current, current, job["purpose"], int(necessary), day, *actual, encoded_metadata, units))
            self._bind_scopes(db, job, call_id, day, units, current)
            self._refresh_pending(db, job_id)
            self._event(db, job_id, "upstream_call_started", current, {"call_id": call_id, "step": step_name, "generation": generation, "reserved_units": units})
            return CallIntent(call_id, job_id, step_name, "in_flight", True)

    def get_call(self, call_id: str) -> Call:
        with closing(self._connect()) as db:
            return self._call(self._call_row(db, call_id))

    def list_calls(self, job_id: str, *, step_name: str | None = None) -> tuple[Call, ...]:
        with closing(self._connect()) as db:
            self._job_row(db, job_id)
            if step_name is None:
                rows = db.execute("SELECT * FROM upstream_calls WHERE job_id=? ORDER BY created_at,rowid", (job_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM upstream_calls WHERE job_id=? AND step_name=? ORDER BY created_at,rowid", (job_id, _text(step_name, "step_name"))).fetchall()
        return tuple(self._call(row) for row in rows)

    def finish_call(
        self, job_id: str, call_id: str, worker_id: str, generation: int, *, success: bool, result: Any = None,
        usage: Mapping[str, Any] | None = None, cost_units: int | None = None, cost_pending: bool = True, now: float | None = None,
    ) -> CallIntent:
        """Record a current provider result; finishing the step is separate."""
        _boolean(success, "success")
        _boolean(cost_pending, "cost_pending")
        _validate_usage(usage)
        cost = None if cost_units is None else _integer(cost_units, "cost_units")
        encoded = _json(result) if success else None
        with self._transaction() as db:
            current = _now(now)
            job = self._lease(db, job_id, worker_id, generation, current)
            if job["cancel_requested"]:
                raise InvalidTransition("cancelled work cannot adopt a provider result; record late usage separately")
            call = self._call_row(db, call_id, job_id)
            if call["status"] != "in_flight" or call["generation"] != generation:
                raise InvalidTransition(f"cannot finish call in state {call['status']}; reconcile unknown results")
            self._record_usage_tx(db, call, usage, cost, cost_pending, current)
            status = "succeeded" if success else "failed"
            db.execute("UPDATE upstream_calls SET status=?,result_json=?,updated_at=? WHERE call_id=?", (status, encoded, current, call_id))
            refreshed = self._refresh_pending(db, job_id)
            self._event(db, job_id, "upstream_call_finished", current, {"call_id": call_id, "status": status, "cost_pending": bool(self._call_row(db, call_id)["cost_pending"])})
            return CallIntent(call_id, job_id, call["step_name"], status, False, _loads(encoded))

    def mark_call_unknown(self, job_id: str, call_id: str, worker_id: str, generation: int, *, now: float | None = None) -> Job:
        with self._transaction() as db:
            current = _now(now)
            self._lease(db, job_id, worker_id, generation, current)
            call = self._call_row(db, call_id, job_id)
            if call["status"] != "in_flight":
                raise InvalidTransition(f"cannot mark call unknown in state {call['status']}")
            self._mark_unknown(db, job_id, current, "upstream result unknown")
            return self._job(self._job_row(db, job_id))

    @classmethod
    def _mark_unknown(cls, db: sqlite3.Connection, job_id: str, now: float, reason: str) -> None:
        db.execute("UPDATE upstream_calls SET status='unknown',updated_at=? WHERE job_id=? AND status='in_flight'", (now, job_id))
        db.execute("UPDATE jobs SET status='reconciling',reconciliation_required=1,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (now, job_id))
        cls._refresh_pending(db, job_id)
        cls._event(db, job_id, "reconciliation_required", now, {"reason": reason})

    def reconcile_call(
        self, job_id: str, call_id: str, outcome: str, *, result: Any = None, usage: Mapping[str, Any] | None = None,
        cost_units: int | None = None, cost_pending: bool = True, now: float | None = None,
    ) -> Job:
        """Apply a verified provider outcome; caller supplies reconciliation evidence.

        not_executed is affirmative proof of no send/charge and defaults to zero.
        A succeeded call does not finish its step: recovery reuses that call's
        final result and then commits the local step with its current lease.
        """
        if outcome not in {"succeeded", "not_executed", "failed"}:
            raise ValueError("outcome must be succeeded, not_executed, or failed")
        _boolean(cost_pending, "cost_pending")
        _validate_usage(usage)
        cost = None if cost_units is None else _integer(cost_units, "cost_units")
        encoded = _json(result) if outcome == "succeeded" else None
        if outcome == "not_executed":
            if cost not in (None, 0):
                raise ValueError("not_executed cannot have a nonzero final cost")
            cost, cost_pending = 0, False
        with self._transaction() as db:
            current = _now(now)
            job = self._job_row(db, job_id)
            if job["status"] != "reconciling":
                raise InvalidTransition("job is not awaiting result reconciliation")
            call = self._call_row(db, call_id, job_id)
            if call["status"] != "unknown":
                raise InvalidTransition(f"call is not unknown: {call['status']}")
            self._record_usage_tx(db, call, usage, cost, cost_pending, current)
            db.execute("UPDATE upstream_calls SET status=?,result_json=?,updated_at=? WHERE call_id=?", (outcome, encoded, current, call_id))
            self._event(db, job_id, "upstream_call_reconciled", current, {"call_id": call_id, "outcome": outcome})
            remaining = db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND status IN ('in_flight','unknown') LIMIT 1", (job_id,)).fetchone()
            if remaining is None:
                db.execute("UPDATE steps SET status='pending',output_json=NULL,error=NULL WHERE job_id=? AND status='running'", (job_id,))
                if job["cancel_requested"]:
                    status, event = "cancelled", "cancelled"
                elif job["paid"] and not self._automatic_recovery(job):
                    status, event = "awaiting_confirmation", "recovery_confirmation_required"
                else:
                    status, event = "queued", "requeued_after_reconciliation"
                ticket = self._ticket(db) if status == "queued" else job["queue_order"]
                db.execute("UPDATE jobs SET status=?,queue_order=?,reconciliation_required=0,updated_at=?,last_error=NULL WHERE job_id=?", (status, ticket, current, job_id))
                self._event(db, job_id, event, current)
            refreshed = self._refresh_pending(db, job_id)
            if refreshed["status"] in _TERMINAL:
                self._release_wallet(db, refreshed)
            return self._job(self._job_row(db, job_id))

    def record_call_usage(
        self, job_id: str, call_id: str, *, usage: Mapping[str, Any] | None = None, cost_units: int | None = None,
        cost_pending: bool = True, now: float | None = None,
    ) -> Call:
        """Persist late usage/cost with no lease, without accepting any result."""
        _boolean(cost_pending, "cost_pending")
        _validate_usage(usage)
        cost = None if cost_units is None else _integer(cost_units, "cost_units")
        with self._transaction() as db:
            current = _now(now)
            call = self._call_row(db, call_id, job_id)
            self._record_usage_tx(db, call, usage, cost, cost_pending, current)
            self._refresh_pending(db, job_id)
            return self._call(self._call_row(db, call_id))

    def settle_call_cost(self, call_id: str, cost_units: int, *, usage: Mapping[str, Any] | None = None, now: float | None = None) -> bool:
        """Settle once by call_id; True means a new charge, False an exact replay."""
        cost = _integer(cost_units, "cost_units")
        _validate_usage(usage)
        with self._transaction() as db:
            call = self._call_row(db, call_id)
            return self._record_usage_tx(db, call, usage, cost, False, _now(now))

    def record_call_cost(
        self, job_id: str, call_id: str, cost_units: int, worker_id: str | None = None, generation: int | None = None,
        *, usage: Mapping[str, Any] | None = None, now: float | None = None,
    ) -> bool:
        """Compatibility alias: a stale worker may record cost but never results."""
        cost = _integer(cost_units, "cost_units")
        _validate_usage(usage)
        with self._transaction() as db:
            call = self._call_row(db, call_id, job_id)
            return self._record_usage_tx(db, call, usage, cost, False, _now(now))

    def call_costs(self, job_id: str) -> dict[str, int]:
        with closing(self._connect()) as db:
            self._job_row(db, job_id)
            rows = db.execute("SELECT call_id,cost_units FROM call_charges WHERE job_id=? ORDER BY created_at,call_id", (job_id,)).fetchall()
        return {row["call_id"]: row["cost_units"] for row in rows}

    def finish(self, job_id: str, worker_id: str, generation: int, *, success: bool, error: str | None = None, retryable: bool = False, now: float | None = None) -> Job:
        _boolean(success, "success")
        _boolean(retryable, "retryable")
        if success and retryable:
            raise ValueError("successful work cannot be retryable")
        with self._transaction() as db:
            current = _now(now)
            row = self._lease(db, job_id, worker_id, generation, current)
            if db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND status IN ('in_flight','unknown') LIMIT 1", (job_id,)).fetchone():
                raise ReconciliationRequired("job has an unresolved upstream result")
            if success and not row["cancel_requested"] and db.execute("SELECT 1 FROM steps WHERE job_id=? AND status<>'succeeded' LIMIT 1", (job_id,)).fetchone():
                raise InvalidTransition("job still has incomplete steps")
            if row["cancel_requested"]:
                status, event = "cancelled", "cancelled"
            elif success:
                status, event = "succeeded", "succeeded"
            elif retryable and row["attempts"] < row["max_attempts"]:
                status = "awaiting_confirmation" if row["paid"] and not self._automatic_recovery(row) else "queued"
                event = "retry_confirmation_required" if status == "awaiting_confirmation" else "retry_scheduled"
            else:
                status, event = "failed", "failed"
            ticket = self._ticket(db) if status == "queued" else row["queue_order"]
            db.execute("UPDATE jobs SET status=?,queue_order=?,lease_owner=NULL,lease_until=NULL,updated_at=?,last_error=?,reconciliation_required=0 WHERE job_id=?", (status, ticket, current, error, job_id))
            if status in {"queued", "awaiting_confirmation"}:
                db.execute("UPDATE steps SET status='pending',output_json=NULL,error=NULL WHERE job_id=? AND status<>'succeeded'", (job_id,))
            self._event(db, job_id, event, current, {"attempt": row["attempts"], "retryable": retryable})
            refreshed = self._refresh_pending(db, job_id)
            if status in _TERMINAL:
                self._release_wallet(db, refreshed)
            return self._job(self._job_row(db, job_id))

    def cancel(self, job_id: str, *, now: float | None = None) -> Job:
        with self._transaction() as db:
            current = _now(now)
            row = self._job_row(db, job_id)
            if row["status"] in _TERMINAL:
                return self._job(row)
            if row["status"] in {"running", "reconciling"}:
                db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=?", (current, job_id))
                self._event(db, job_id, "cancel_requested", current)
            else:
                db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE job_id=?", (current, job_id))
                self._release_wallet(db, self._refresh_pending(db, job_id))
                self._event(db, job_id, "cancelled", current)
            return self._job(self._job_row(db, job_id))

    def retry(self, job_id: str, *, confirm_paid: bool = False, now: float | None = None) -> Job:
        _boolean(confirm_paid, "confirm_paid")
        with self._transaction() as db:
            current = _now(now)
            row = self._job_row(db, job_id)
            if row["status"] not in {"failed", "cancelled"}:
                raise InvalidTransition(f"cannot retry job in state {row['status']}")
            if row["reconciliation_required"] or row["cost_pending"]:
                raise ReconciliationRequired("reconcile results and settle usage before retry")
            if row["paid"] and not self._automatic_recovery(row) and not confirm_paid:
                db.execute("UPDATE jobs SET status='awaiting_confirmation',attempts=0,updated_at=? WHERE job_id=?", (current, job_id))
                self._event(db, job_id, "retry_confirmation_required", current)
                return self._job(self._job_row(db, job_id))
            self._reserve_wallet(db, row)
            db.execute("UPDATE jobs SET status='queued',queue_order=?,attempts=0,cancel_requested=0,lease_owner=NULL,lease_until=NULL,updated_at=?,last_error=NULL WHERE job_id=?", (self._ticket(db), current, job_id))
            db.execute("UPDATE steps SET status='pending',output_json=NULL,error=NULL WHERE job_id=? AND status<>'succeeded'", (job_id,))
            self._event(db, job_id, "retried", current, {"confirmed_paid": confirm_paid})
            return self._job(self._job_row(db, job_id))

    def confirm_recovery(self, job_id: str, *, approved: bool, now: float | None = None) -> Job:
        _boolean(approved, "approved")
        with self._transaction() as db:
            current = _now(now)
            row = self._job_row(db, job_id)
            if row["status"] != "awaiting_confirmation":
                raise InvalidTransition("job is not awaiting confirmation")
            if not approved:
                db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE job_id=?", (current, job_id))
                self._release_wallet(db, self._refresh_pending(db, job_id))
                self._event(db, job_id, "recovery_declined", current)
            else:
                if not row["reservation_active"] and not row["cost_pending"]:
                    self._reserve_wallet(db, row)
                db.execute("UPDATE jobs SET status='queued',queue_order=?,cancel_requested=0,updated_at=?,last_error=NULL WHERE job_id=?", (self._ticket(db), current, job_id))
                self._event(db, job_id, "recovery_confirmed", current)
            return self._job(self._job_row(db, job_id))

    def recover_expired(self, *, now: float | None = None) -> tuple[str, ...]:
        with self._transaction() as db:
            return self._recover_expired(db, _now(now))

    def _recover_expired(self, db: sqlite3.Connection, now: float) -> tuple[str, ...]:
        rows = db.execute("SELECT * FROM jobs WHERE status='running' AND lease_until<=? ORDER BY queue_order", (now,)).fetchall()
        recovered: list[str] = []
        for job in rows:
            job_id = job["job_id"]
            if db.execute("SELECT 1 FROM upstream_calls WHERE job_id=? AND status IN ('in_flight','unknown') LIMIT 1", (job_id,)).fetchone():
                self._mark_unknown(db, job_id, now, "lease expired with an unresolved upstream result")
            else:
                if job["cancel_requested"]:
                    status, event = "cancelled", "cancelled"
                elif job["attempts"] >= job["max_attempts"]:
                    status, event = "failed", "failed"
                elif self._automatic_recovery(job):
                    status, event = "queued", "grading_recovered"
                elif job["paid"]:
                    status, event = "awaiting_confirmation", "recovery_confirmation_required"
                else:
                    status, event = "queued", "job_recovered"
                ticket = self._ticket(db) if status == "queued" else job["queue_order"]
                db.execute("UPDATE jobs SET status=?,queue_order=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (status, ticket, now, job_id))
                db.execute("UPDATE steps SET status='pending',output_json=NULL,error=NULL WHERE job_id=? AND status='running'", (job_id,))
                if status in _TERMINAL:
                    self._release_wallet(db, self._refresh_pending(db, job_id))
                self._event(db, job_id, event, now, {"previous_generation": job["generation"], "attempt": job["attempts"]})
            recovered.append(job_id)
        return tuple(recovered)


class BudgetLedger:
    """Limits, frozen call reservations, and billable usage in the queue file.

    student_daily supports course/class scope and ISO dates. period=None in
    assign/set_limit creates a recurring daily template; query defaults to the
    current UTC day. Applicable global, course, and class policies all apply,
    so a teacher policy cannot override a tighter administrator policy.
    Authorization of teachers/admins belongs to the caller, not this module.
    """

    def __init__(self, queue_or_database: JobQueue | str | Path, *, busy_timeout: float = 30.0) -> None:
        self.queue = queue_or_database if isinstance(queue_or_database, JobQueue) else JobQueue(queue_or_database, busy_timeout=busy_timeout)

    @staticmethod
    def _key(scope_type: str, scope_id: str, period: str | None, course_id: str | None, class_id: str | None) -> tuple[str, str, str, str, str]:
        if scope_type not in _SCOPES:
            raise ValueError(f"scope_type must be one of {tuple(sorted(_SCOPES))}")
        scope_id = _text(scope_id, "scope_id")
        course, classroom = _optional_text(course_id) or "", _optional_text(class_id) or ""
        if classroom and not course:
            raise ValueError("class_id requires course_id")
        if scope_type != "student_daily" and (period is not None or course or classroom):
            raise ValueError("only student_daily limits take a period/course/class scope")
        return scope_type, scope_id, course, classroom, "" if period is None else _date_key(period)

    @staticmethod
    def _limit(row: sqlite3.Row) -> BudgetLimit:
        return BudgetLimit(row["scope_type"], row["scope_id"], row["period_key"] or None, row["limit_units"], row["emergency_units"], row["spent_units"], row["reserved_units"], row["overage_units"], row["emergency_exceeded_units"], bool(row["notification_required"]), row["course_key"] or None, row["class_key"] or None, bool(row["configured"]))

    def assign(
        self, scope_type: str, scope_id: str, limit_units: int, *, emergency_units: int = 0,
        period: str | None = None, course_id: str | None = None, class_id: str | None = None,
    ) -> BudgetLimit:
        key = self._key(scope_type, scope_id, period, course_id, class_id)
        limit_units, emergency_units = _integer(limit_units, "limit_units"), _integer(emergency_units, "emergency_units")
        _sum_units(limit_units, emergency_units)
        if scope_type == "student_daily" and emergency_units:
            raise ValueError("student daily budgets cannot use teaching emergency funds")
        with self.queue._transaction() as db:
            db.execute("INSERT INTO budget_limits(scope_type,scope_id,course_key,class_key,period_key,configured,limit_units,emergency_units) VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(scope_type,scope_id,course_key,class_key,period_key) DO UPDATE SET configured=1,limit_units=excluded.limit_units,emergency_units=excluded.emergency_units", (*key, limit_units, emergency_units))
            if scope_type == "student_daily" and period is None:
                db.execute("UPDATE budget_limits SET configured=1,limit_units=?,emergency_units=0 WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=?", (limit_units, *key[:4]))
            rows = db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=?", key[:4]).fetchall()
            for row in rows:
                overage = max(0, row["spent_units"] - row["limit_units"])
                exceeded = max(0, row["spent_units"] - row["limit_units"] - row["emergency_units"])
                db.execute("UPDATE budget_limits SET overage_units=?,emergency_exceeded_units=?,notification_required=MAX(notification_required,?) WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", (overage, exceeded, int(bool(overage)), *_limit_key(row)))
            return self._limit(db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", key).fetchone())

    def set_limit(self, scope_type: str, scope_id: str, limit_units: int, *, emergency_units: int = 0, period: str | None = None, course_id: str | None = None, class_id: str | None = None) -> BudgetLimit:
        return self.assign(scope_type, scope_id, limit_units, emergency_units=emergency_units, period=period, course_id=course_id, class_id=class_id)

    def get_limit(self, scope_type: str, scope_id: str, *, period: str | None = None, course_id: str | None = None, class_id: str | None = None) -> BudgetLimit | None:
        """Read the exact stored policy (None period reads a daily template)."""
        key = self._key(scope_type, scope_id, period, course_id, class_id)
        with closing(self.queue._connect()) as db:
            row = db.execute("SELECT * FROM budget_limits WHERE scope_type=? AND scope_id=? AND course_key=? AND class_key=? AND period_key=?", key).fetchone()
        return None if row is None else self._limit(row)

    def query(self, scope_type: str, scope_id: str, *, period: str | None = None, course_id: str | None = None, class_id: str | None = None) -> BudgetLimit | None:
        """Read a bucket's current usage; an unused daily template has zero use."""
        day = _utc_day(time.time()) if scope_type == "student_daily" and period is None else period
        result = self.get_limit(scope_type, scope_id, period=day, course_id=course_id, class_id=class_id)
        if result is None and scope_type == "student_daily":
            template = self.get_limit(scope_type, scope_id, course_id=course_id, class_id=class_id)
            if template is not None:
                result = replace(template, period=day, spent_units=0, reserved_units=0, overage_units=0, emergency_exceeded_units=0, notification_required=False)
        return result

    def snapshot(self, *, scope_type: str | None = None, scope_id: str | None = None) -> tuple[BudgetLimit, ...]:
        if scope_type is not None and scope_type not in _SCOPES:
            raise ValueError("invalid scope_type")
        scope_id = None if scope_id is None else _text(scope_id, "scope_id")
        with closing(self.queue._connect()) as db:
            rows = db.execute("""SELECT * FROM budget_limits
                WHERE (? IS NULL OR scope_type=?) AND (? IS NULL OR scope_id=?)
                ORDER BY scope_type,scope_id,course_key,class_key,period_key""", (scope_type, scope_type, scope_id, scope_id)).fetchall()
        return tuple(self._limit(row) for row in rows)

    def list_limits(self) -> tuple[BudgetLimit, ...]:
        return self.snapshot()

    def entries(self, *, call_id: str | None = None, after_id: int = 0, limit: int = 1000) -> tuple[LedgerEntry, ...]:
        after_id, limit = _integer(after_id, "after_id"), _positive_integer(limit, "limit")
        with closing(self.queue._connect()) as db:
            if call_id is None:
                rows = db.execute("SELECT * FROM budget_ledger_entries WHERE entry_id>? ORDER BY entry_id LIMIT ?", (after_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM budget_ledger_entries WHERE entry_id>? AND call_id=? ORDER BY entry_id LIMIT ?", (after_id, call_id, limit)).fetchall()
        return tuple(LedgerEntry(row["entry_id"], row["call_id"], row["scope_type"], row["scope_id"], row["period_key"] or None, row["course_key"] or None, row["class_key"] or None, row["entry_type"], row["units"], row["created_at"]) for row in rows)


def _canonical_sql(sql: str | None) -> str:
    # Ignore formatting only outside SQL string literals. Literal constraints,
    # defaults, columns, keys, indexes, triggers and views remain significant.
    chunks = re.split(r"('(?:''|[^'])*')", sql or "")
    return "".join(chunk if index % 2 else re.sub(r"\s+", "", chunk).lower().replace("ifnotexists", "") for index, chunk in enumerate(chunks))


def _schema_signature(statements: Sequence[str]) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for statement in statements:
        match = re.match(r"CREATE (TABLE|INDEX) (\w+)", statement)
        if match is None:
            raise RuntimeError("invalid built-in schema declaration")
        result[match.group(2)] = (match.group(1).lower(), _canonical_sql(statement))
    return result


def _limit_key(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    return row["scope_type"], row["scope_id"], row["course_key"], row["class_key"], row["period_key"]


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: str | None) -> str | None:
    return None if value is None else _text(value, "optional text")


def _boolean(value: bool, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")


def _integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_UNITS:
        raise ValueError(f"{name} must be a non-negative signed 64-bit integer")
    return value


def _sum_units(*values: int) -> int:
    return _integer(sum(values), "aggregate units")


def _positive_integer(value: int, name: str) -> int:
    _integer(value, name)
    if value == 0:
        raise ValueError(f"{name} must be positive")
    return value


def _time(value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("timestamp must be finite")
    return float(value)


def _now(value: float | None) -> float:
    return time.time() if value is None else _time(value)


def _positive_number(value: float, name: str) -> float:
    result = _time(value)
    if result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _date_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("period must be an ISO date")
    try:
        day = date.fromisoformat(value).isoformat()
        if day != value:
            raise ValueError("period must use YYYY-MM-DD")
        return day
    except ValueError as error:
        raise ValueError("period must be an ISO date in YYYY-MM-DD format") from error


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def _validate_usage(usage: Mapping[str, Any] | None) -> None:
    if usage is not None and not isinstance(usage, Mapping):
        raise TypeError("usage must be a mapping or None")
