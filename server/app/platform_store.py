"""Relational SQLite storage for platform accounts, courses, and memberships."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4


def resolve_platform_db_path(settings: Any) -> Path:
    """Resolve the one platform database below the configured server data root."""
    root = Path(settings.data_dir).expanduser()
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[1] / root
    return root.resolve() / "platform.sqlite3"


class PlatformStore:
    """SQLite repository; the caller explicitly supplies its database path."""

    SCHEMA_VERSION = 2
    _MIGRATIONS = {1: "_migration_1", 2: "_migration_2"}
    _SCHEMA_VERSION_SQL = {1: "PRAGMA user_version = 1", 2: "PRAGMA user_version = 2"}
    _USER_FIELDS = {
        "username", "display_name", "password_hash", "role", "status",
        "phone", "student_number",
    }
    _COURSE_FIELDS = {"name", "term", "teacher_id", "class_code"}

    def __init__(self, db_path: str | Path, *, timeout: float = 30.0) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("PlatformStore requires a file-backed database for WAL")
        self.db_path = Path(db_path)
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._preflight_existing_database()
        self._initialize()

    def _preflight_existing_database(self) -> None:
        if not self.db_path.is_file() or self.db_path.stat().st_size == 0:
            return
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        except sqlite3.Error as error:
            raise RuntimeError("existing database is not a readable platform database") from error
        finally:
            connection.close()
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
            )
        expected = {
            "users", "sessions", "activation_codes", "courses", "course_collaborators", "classes", "course_memberships",
            "class_memberships", "audit_events", "idempotency",
        }
        if version == 0 and tables:
            raise RuntimeError("refusing to migrate an unversioned, non-empty database")
        if version > 0 and not expected.issubset(tables):
            raise RuntimeError("existing database does not match the platform schema")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=self.timeout, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Reuse this thread's transaction, otherwise close a fresh connection on exit."""
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than supported version {self.SCHEMA_VERSION}"
                )
            while version < self.SCHEMA_VERSION:
                target = version + 1
                migration = self._MIGRATIONS.get(target)
                if migration is None:
                    raise RuntimeError(f"no migration registered for schema version {target}")
                getattr(self, migration)(connection)
                connection.execute(self._SCHEMA_VERSION_SQL[target])
                version = target
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(username)) > 0),
                display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
                password_hash TEXT NOT NULL CHECK (length(password_hash) > 0),
                role TEXT NOT NULL CHECK (role IN ('admin', 'teacher', 'student')),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'disabled')),
                phone TEXT NOT NULL DEFAULT '', student_number TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            "CREATE UNIQUE INDEX uq_users_student_number ON users(student_number COLLATE NOCASE) WHERE student_number <> ''",
            "CREATE INDEX idx_users_role_status ON users(role, status)",
            """CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL, created_at REAL NOT NULL
            )""",
            "CREATE INDEX idx_sessions_user_id ON sessions(user_id)",
            "CREATE INDEX idx_sessions_expires_at ON sessions(expires_at)",
            """CREATE TABLE courses (
                id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                term TEXT NOT NULL DEFAULT '',
                teacher_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                class_code TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
            """CREATE TABLE activation_codes (
                code_hash TEXT PRIMARY KEY CHECK (length(code_hash) = 64),
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                purpose TEXT NOT NULL CHECK (purpose IN ('activation', 'reset')),
                course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
                created_at REAL NOT NULL, expires_at REAL, consumed_at REAL
            )""",
            "CREATE INDEX idx_activation_codes_user_id ON activation_codes(user_id)",
            "CREATE INDEX idx_activation_codes_expires_at ON activation_codes(expires_at)",
            "CREATE INDEX idx_courses_teacher_id ON courses(teacher_id)",
            "CREATE UNIQUE INDEX uq_courses_class_code ON courses(class_code COLLATE NOCASE) WHERE class_code <> ''",
            """CREATE TABLE course_collaborators (
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                teacher_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                access_level TEXT NOT NULL CHECK (access_level IN ('read', 'manage')),
                granted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(course_id, teacher_id)
            )""",
            "CREATE INDEX idx_course_collaborators_teacher_id ON course_collaborators(teacher_id)",
            """CREATE TABLE classes (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0), created_at REAL NOT NULL,
                UNIQUE(course_id, name), UNIQUE(course_id, id)
            )""",
            "CREATE INDEX idx_classes_course_id ON classes(course_id)",
            """CREATE TABLE course_memberships (
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at REAL NOT NULL, PRIMARY KEY(course_id, user_id)
            )""",
            "CREATE INDEX idx_course_memberships_user_id ON course_memberships(user_id)",
            """CREATE TABLE class_memberships (
                course_id TEXT NOT NULL, class_id TEXT NOT NULL, user_id TEXT NOT NULL,
                joined_at REAL NOT NULL, PRIMARY KEY(class_id, user_id),
                FOREIGN KEY(course_id, class_id) REFERENCES classes(course_id, id) ON DELETE CASCADE,
                FOREIGN KEY(course_id, user_id) REFERENCES course_memberships(course_id, user_id) ON DELETE CASCADE
            )""",
            "CREATE INDEX idx_class_memberships_user_id ON class_memberships(user_id)",
            "CREATE INDEX idx_class_memberships_course_id ON class_memberships(course_id)",
            """CREATE TABLE audit_events (
                id TEXT PRIMARY KEY, actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
                resource_type TEXT, resource_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
                ip TEXT, created_at REAL NOT NULL
            )""",
            "CREATE INDEX idx_audit_events_created_at ON audit_events(created_at DESC)",
            "CREATE INDEX idx_audit_events_actor_user_id ON audit_events(actor_user_id)",
            "CREATE INDEX idx_audit_events_resource ON audit_events(resource_type, resource_id)",
            """CREATE TABLE idempotency (
                scope TEXT NOT NULL CHECK (length(trim(scope)) > 0),
                key TEXT NOT NULL CHECK (length(trim(key)) > 0),
                owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                request_hash TEXT, status_code INTEGER, response_json TEXT NOT NULL DEFAULT 'null',
                created_at REAL NOT NULL, expires_at REAL, PRIMARY KEY(scope, key)
            )""",
            "CREATE INDEX idx_idempotency_owner_id ON idempotency(owner_id)",
            "CREATE INDEX idx_idempotency_expires_at ON idempotency(expires_at)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migration_2(connection: sqlite3.Connection) -> None:
        # Historical course ownership never grants access to historical classes.
        statements = (
            "ALTER TABLE classes ADD COLUMN owner_teacher_id TEXT REFERENCES users(id) ON DELETE RESTRICT",
            "ALTER TABLE classes ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 1",
            "CREATE INDEX idx_classes_owner ON classes(owner_teacher_id)",
            """CREATE TABLE class_collaborators (
                class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                teacher_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                access_level TEXT NOT NULL CHECK (access_level IN ('read', 'manage')),
                granted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at REAL NOT NULL, PRIMARY KEY(class_id, teacher_id)
            )""",
            "CREATE INDEX idx_class_collaborators_teacher ON class_collaborators(teacher_id, class_id)",
            "ALTER TABLE activation_codes ADD COLUMN class_id TEXT REFERENCES classes(id) ON DELETE CASCADE",
            "ALTER TABLE activation_codes ADD COLUMN class_authorization_version INTEGER",
            "ALTER TABLE activation_codes ADD COLUMN revoked_at REAL",
            "CREATE INDEX idx_activation_codes_class ON activation_codes(class_id)",
            # Codes issued under the previous, broader course rules must be reissued.
            "UPDATE activation_codes SET revoked_at = 0 WHERE consumed_at IS NULL",
            "ALTER TABLE users ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 1",
            """CREATE TABLE course_applications (
                id TEXT PRIMARY KEY NOT NULL,
                applicant_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0), term TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                course_id TEXT REFERENCES courses(id) ON DELETE SET NULL,
                reviewed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                review_note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, reviewed_at REAL
            )""",
            "CREATE INDEX idx_course_applications_status ON course_applications(status, created_at)",
            "CREATE INDEX idx_course_applications_applicant ON course_applications(applicant_id, created_at)",
            """CREATE UNIQUE INDEX uq_course_applications_pending
               ON course_applications(applicant_id, name, term) WHERE status = 'pending'""",
            """CREATE TABLE auth_rate_limits (
                scope TEXT NOT NULL, subject_hash TEXT NOT NULL,
                window_start REAL NOT NULL, attempts INTEGER NOT NULL CHECK(attempts >= 0),
                PRIMARY KEY(scope, subject_hash)
            )""",
            "CREATE INDEX idx_auth_rate_limits_window ON auth_rate_limits(window_start)",
        )
        for statement in statements:
            connection.execute(statement)

    @contextmanager
    def _using_connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._local.connection = None
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator["PlatformStore"]:
        """Atomically group repository calls; any raised exception rolls back."""
        if getattr(self._local, "connection", None) is not None:
            raise RuntimeError("nested transactions are not supported")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            yield self
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._local.connection = None
            connection.close()

    @property
    def schema_version(self) -> int:
        with self._using_connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self._using_connection() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _timestamp(value: float | int | datetime) -> float:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise TypeError("timestamp must be a number or datetime")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        return result

    @staticmethod
    def _id(value: str | None) -> str:
        result = value or str(uuid4())
        if not isinstance(result, str) or not result.strip():
            raise ValueError("id must be a non-empty string")
        return result

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _token_hash(token: str) -> str:
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    _activation_code_hash = _token_hash

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def create_user(
        self, username: str, display_name: str, password_hash: str, role: str, *,
        status: str = "active", phone: str = "", student_number: str = "", user_id: str | None = None,
    ) -> dict[str, Any]:
        username, display_name = username.strip(), display_name.strip()
        record_id, now = self._id(user_id), self._now()
        with self._using_connection(write=True) as connection:
            connection.execute(
                """INSERT INTO users
                   (id, username, display_name, password_hash, role, status, phone, student_number, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, username, display_name, password_hash, role, status, phone, student_number, now, now),
            )
            return dict(connection.execute("SELECT * FROM users WHERE id = ?", (record_id,)).fetchone())

    create_account = create_user

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    get_account = get_user

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone())

    def list_users(self, *, role: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            rows = connection.execute(
                """SELECT * FROM users WHERE (? IS NULL OR role = ?) AND (? IS NULL OR status = ?)
                   ORDER BY username COLLATE NOCASE, id""", (role, role, status, status),
            )
            return [dict(row) for row in rows]

    def update_user(self, user_id: str, **changes: Any) -> dict[str, Any] | None:
        unknown = changes.keys() - self._USER_FIELDS
        if unknown:
            raise ValueError(f"unsupported user fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_user(user_id)
        values = dict(changes)
        for field in ("username", "display_name"):
            if field in values and isinstance(values[field], str):
                values[field] = values[field].strip()
        values["updated_at"] = self._now()
        # Presence flags preserve explicit NULL writes and their constraint checks.
        with self._using_connection(write=True) as connection:
            cursor = connection.execute(
                """UPDATE users SET
                   username = CASE WHEN ? THEN ? ELSE username END,
                   display_name = CASE WHEN ? THEN ? ELSE display_name END,
                   password_hash = CASE WHEN ? THEN ? ELSE password_hash END,
                   role = CASE WHEN ? THEN ? ELSE role END,
                   status = CASE WHEN ? THEN ? ELSE status END,
                   phone = CASE WHEN ? THEN ? ELSE phone END,
                   student_number = CASE WHEN ? THEN ? ELSE student_number END,
                   updated_at = ? WHERE id = ?""",
                ("username" in values, values.get("username"),
                 "display_name" in values, values.get("display_name"),
                 "password_hash" in values, values.get("password_hash"),
                 "role" in values, values.get("role"),
                 "status" in values, values.get("status"),
                 "phone" in values, values.get("phone"),
                 "student_number" in values, values.get("student_number"),
                 values["updated_at"], user_id),
            )
            if cursor.rowcount == 0:
                return None
            if {"password_hash", "status", "role"}.intersection(changes):
                connection.execute("UPDATE users SET authorization_version = authorization_version + 1 WHERE id = ?", (user_id,))
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                connection.execute(
                    """UPDATE activation_codes SET revoked_at = ?
                       WHERE (user_id = ? OR created_by = ?) AND consumed_at IS NULL""",
                    (self._now(), user_id, user_id),
                )
            return self._dict(connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    update_account = update_user

    def delete_user(self, user_id: str) -> bool:
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    delete_account = delete_user

    def create_session(
        self, token: str, user_id: str, expires_at: float | int | datetime, *,
        created_at: float | int | datetime | None = None,
    ) -> dict[str, Any]:
        token_hash = self._token_hash(token)
        expiry = self._timestamp(expires_at)
        created = self._now() if created_at is None else self._timestamp(created_at)
        with self._using_connection(write=True) as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, expiry, created),
            )
            return dict(connection.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone())

    def get_session(self, token: str, *, now: float | int | datetime | None = None) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        current = self._now() if now is None else self._timestamp(now)
        with self._using_connection() as connection:
            return self._dict(connection.execute(
                """SELECT s.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active'""", (token_hash, current),
            ).fetchone())

    def delete_session(self, token: str) -> bool:
        token_hash = self._token_hash(token)
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,)).rowcount > 0

    def delete_user_sessions(self, user_id: str) -> int:
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount

    def create_activation_code(
        self, code: str, user_id: str, created_by: str | None = None, *,
        issued_by: str | None = None, purpose: str = "activation",
        course_id: str | None = None, class_id: str | None = None,
        expires_at: float | int | datetime | None = None,
        created_at: float | int | datetime | None = None,
    ) -> dict[str, Any]:
        """Issue a hash-only code, binding teacher authority to a single class."""
        if issued_by is not None:
            if created_by is not None and created_by != issued_by:
                raise ValueError("created_by and issued_by disagree")
            created_by = issued_by
        if purpose not in {"activation", "reset"}:
            raise ValueError("invalid code purpose")
        code_hash = self._token_hash(code)
        created = self._now() if created_at is None else self._timestamp(created_at)
        expiry = created + (86400 if purpose == "activation" else 3600) if expires_at is None else self._timestamp(expires_at)
        if expiry <= created:
            raise ValueError("code expiry must follow creation")
        with self._using_connection(write=True) as connection:
            issuer = connection.execute("SELECT * FROM users WHERE id = ?", (created_by,)).fetchone()
            target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if issuer is None or issuer["status"] != "active" or issuer["role"] not in {"admin", "teacher"}:
                raise PermissionError("an active administrator or class manager is required")
            if target is None or target["status"] == "disabled":
                raise PermissionError("target account is unavailable")
            if purpose == "activation" and (target["role"] != "student" or target["status"] != "pending"):
                raise ValueError("activation requires a pending student account")
            if purpose == "reset" and target["status"] != "active":
                raise ValueError("reset requires an active account")
            if issuer["role"] != "admin":
                if target["role"] != "student":
                    raise PermissionError("teachers may only issue codes for their students")
                candidates = connection.execute(
                    """SELECT cl.* FROM classes cl JOIN class_memberships m ON m.class_id = cl.id
                       WHERE m.user_id = ? AND (? IS NULL OR cl.course_id = ?)
                         AND (? IS NULL OR cl.id = ?) ORDER BY cl.id""",
                    (user_id, course_id, course_id, class_id, class_id),
                ).fetchall()
                allowed = [cl for cl in candidates if self._class_access(connection, created_by, cl["id"], manage=True)]
                if not allowed:
                    raise PermissionError("issuer has no class management authority over this student")
                class_id = allowed[0]["id"]
                course_id = allowed[0]["course_id"]
            classroom = None
            if class_id is not None:
                classroom = connection.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
                if classroom is None or (course_id is not None and classroom["course_id"] != course_id):
                    raise ValueError("class does not belong to course")
                if not connection.execute("SELECT 1 FROM class_memberships WHERE class_id = ? AND user_id = ?",
                                          (class_id, user_id)).fetchone():
                    raise PermissionError("target is not a member of this class")
                course_id = classroom["course_id"]
            elif course_id is not None:
                if not connection.execute("SELECT 1 FROM courses WHERE id = ?", (course_id,)).fetchone():
                    raise ValueError("course not found")
            connection.execute(
                """INSERT INTO activation_codes
                   (code_hash, user_id, created_by, purpose, course_id, class_id,
                    class_authorization_version, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code_hash, user_id, created_by, purpose, course_id, class_id,
                 classroom["authorization_version"] if classroom else None, created, expiry),
            )
            return dict(connection.execute("SELECT * FROM activation_codes WHERE code_hash = ?", (code_hash,)).fetchone())

    def consume_activation_code(
        self, code: str, *, purpose: str = "activation", course_id: str | None = None,
        class_id: str | None = None, now: float | int | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Resolve scope from the saved code and recheck authority before consuming."""
        if purpose not in {"activation", "reset"}:
            raise ValueError("invalid code purpose")
        if not code:
            return None
        current = self._now() if now is None else self._timestamp(now)
        code_hash = self._token_hash(code)
        with self._using_connection(write=True) as connection:
            record = connection.execute("SELECT * FROM activation_codes WHERE code_hash = ?", (code_hash,)).fetchone()
            if record is None or record["purpose"] != purpose or record["consumed_at"] is not None or record["revoked_at"] is not None:
                return None
            if record["expires_at"] is not None and record["expires_at"] <= current:
                return None
            if (course_id is not None and course_id != record["course_id"]) or (class_id is not None and class_id != record["class_id"]):
                return None
            target = connection.execute("SELECT * FROM users WHERE id = ?", (record["user_id"],)).fetchone()
            issuer = connection.execute("SELECT * FROM users WHERE id = ?", (record["created_by"],)).fetchone()
            if target is None or issuer is None or issuer["status"] != "active":
                return None
            required_status = "pending" if purpose == "activation" else "active"
            if target["status"] != required_status or (purpose == "activation" and target["role"] != "student"):
                return None
            if record["class_id"] is not None:
                classroom = connection.execute("SELECT * FROM classes WHERE id = ?", (record["class_id"],)).fetchone()
                if classroom is None or classroom["authorization_version"] != record["class_authorization_version"]:
                    return None
                if not connection.execute("SELECT 1 FROM class_memberships WHERE class_id = ? AND user_id = ?",
                                          (record["class_id"], target["id"])).fetchone():
                    return None
            if issuer["role"] != "admin":
                if issuer["role"] != "teacher" or target["role"] != "student" or record["class_id"] is None:
                    return None
                if not self._class_access(connection, issuer["id"], record["class_id"], manage=True):
                    return None
            connection.execute("UPDATE activation_codes SET consumed_at = ? WHERE code_hash = ?", (current, code_hash))
            return dict(target)

    def create_course(
        self, name: str, term: str, teacher_id: str, *, course_id: str | None = None, class_code: str = "",
    ) -> dict[str, Any]:
        record_id, now = self._id(course_id), self._now()
        with self._using_connection(write=True) as connection:
            connection.execute(
                """INSERT INTO courses(id, name, term, teacher_id, class_code, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record_id, name.strip(), term, teacher_id, class_code.strip(), now, now),
            )
            return dict(connection.execute("SELECT * FROM courses WHERE id = ?", (record_id,)).fetchone())

    def get_course(self, course_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone())

    def list_courses(self, *, teacher_id: str | None = None) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT c.* FROM courses c WHERE ? IS NULL OR c.teacher_id = ? OR EXISTS (
                       SELECT 1 FROM course_collaborators cc WHERE cc.course_id = c.id AND cc.teacher_id = ?
                   ) ORDER BY c.name COLLATE NOCASE, c.id""", (teacher_id, teacher_id, teacher_id),
            )]

    def update_course(self, course_id: str, **changes: Any) -> dict[str, Any] | None:
        unknown = changes.keys() - self._COURSE_FIELDS
        if unknown:
            raise ValueError(f"unsupported course fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_course(course_id)
        values = dict(changes)
        for field in ("name", "class_code"):
            if field in values and isinstance(values[field], str):
                values[field] = values[field].strip()
        values["updated_at"] = self._now()
        with self._using_connection(write=True) as connection:
            cursor = connection.execute(
                """UPDATE courses SET
                   name = CASE WHEN ? THEN ? ELSE name END,
                   term = CASE WHEN ? THEN ? ELSE term END,
                   teacher_id = CASE WHEN ? THEN ? ELSE teacher_id END,
                   class_code = CASE WHEN ? THEN ? ELSE class_code END,
                   updated_at = ? WHERE id = ?""",
                ("name" in values, values.get("name"),
                 "term" in values, values.get("term"),
                 "teacher_id" in values, values.get("teacher_id"),
                 "class_code" in values, values.get("class_code"),
                 values["updated_at"], course_id),
            )
            if cursor.rowcount == 0:
                return None
            return self._dict(connection.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone())

    def delete_course(self, course_id: str) -> bool:
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM courses WHERE id = ?", (course_id,)).rowcount > 0

    def create_class(
        self, course_id: str, name: str, *, owner_teacher_id: str | None = None,
        class_id: str | None = None,
    ) -> dict[str, Any]:
        if not owner_teacher_id:
            raise ValueError("class owner_teacher_id must be explicit")
        record_id = self._id(class_id)
        with self._using_connection(write=True) as connection:
            owner = connection.execute("SELECT role, status FROM users WHERE id = ?", (owner_teacher_id,)).fetchone()
            if owner is None or owner["role"] != "teacher" or owner["status"] != "active":
                raise ValueError("class owner must be an active teacher")
            connection.execute(
                "INSERT INTO classes(id, course_id, name, created_at, owner_teacher_id) VALUES (?, ?, ?, ?, ?)",
                (record_id, course_id, name.strip(), self._now(), owner_teacher_id),
            )
            return dict(connection.execute("SELECT * FROM classes WHERE id = ?", (record_id,)).fetchone())

    def get_class(self, class_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone())

    def list_classes(self, course_id: str) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM classes WHERE course_id = ? ORDER BY name COLLATE NOCASE, id", (course_id,)
            )]

    def update_class(self, class_id: str, *, name: str) -> dict[str, Any] | None:
        with self._using_connection(write=True) as connection:
            cursor = connection.execute("UPDATE classes SET name = ? WHERE id = ?", (name.strip(), class_id))
            if not cursor.rowcount:
                return None
            return self._dict(connection.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone())

    def delete_class(self, class_id: str) -> bool:
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM classes WHERE id = ?", (class_id,)).rowcount > 0

    def create_course_membership(self, course_id: str, user_id: str, *,
                                 joined_at: float | int | datetime | None = None) -> dict[str, Any]:
        joined = self._now() if joined_at is None else self._timestamp(joined_at)
        with self._using_connection(write=True) as connection:
            connection.execute("INSERT INTO course_memberships(course_id, user_id, joined_at) VALUES (?, ?, ?)",
                               (course_id, user_id, joined))
            return dict(connection.execute("SELECT * FROM course_memberships WHERE course_id = ? AND user_id = ?",
                                           (course_id, user_id)).fetchone())

    add_course_member = create_course_membership

    def get_course_membership(self, course_id: str, user_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM course_memberships WHERE course_id = ? AND user_id = ?",
                                                 (course_id, user_id)).fetchone())

    def list_course_memberships(self, course_id: str) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM course_memberships WHERE course_id = ? ORDER BY joined_at, user_id", (course_id,)
            )]

    def delete_course_membership(self, course_id: str, user_id: str) -> bool:
        with self._using_connection(write=True) as connection:
            changed = connection.execute("DELETE FROM course_memberships WHERE course_id = ? AND user_id = ?",
                                         (course_id, user_id)).rowcount > 0
            if changed:
                connection.execute(
                    """UPDATE activation_codes SET revoked_at = ?
                       WHERE course_id = ? AND user_id = ? AND consumed_at IS NULL""",
                    (self._now(), course_id, user_id),
                )
            return changed

    remove_course_member = delete_course_membership

    def create_class_membership(self, class_id: str, user_id: str, *,
                                joined_at: float | int | datetime | None = None) -> dict[str, Any]:
        joined = self._now() if joined_at is None else self._timestamp(joined_at)
        with self._using_connection(write=True) as connection:
            row = connection.execute("SELECT course_id FROM classes WHERE id = ?", (class_id,)).fetchone()
            if row is None:
                raise KeyError(f"class not found: {class_id}")
            course_id = row["course_id"]
            connection.execute("""INSERT INTO class_memberships(course_id, class_id, user_id, joined_at)
                                  VALUES (?, ?, ?, ?)""", (course_id, class_id, user_id, joined))
            return dict(connection.execute("SELECT * FROM class_memberships WHERE class_id = ? AND user_id = ?",
                                           (class_id, user_id)).fetchone())

    add_class_member = create_class_membership

    def get_class_membership(self, class_id: str, user_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM class_memberships WHERE class_id = ? AND user_id = ?",
                                                 (class_id, user_id)).fetchone())

    def list_class_memberships(self, class_id: str) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM class_memberships WHERE class_id = ? ORDER BY joined_at, user_id", (class_id,)
            )]

    def delete_class_membership(self, class_id: str, user_id: str) -> bool:
        with self._using_connection(write=True) as connection:
            changed = connection.execute("DELETE FROM class_memberships WHERE class_id = ? AND user_id = ?",
                                         (class_id, user_id)).rowcount > 0
            if changed:
                connection.execute(
                    "UPDATE activation_codes SET revoked_at = ? WHERE class_id = ? AND user_id = ? AND consumed_at IS NULL",
                    (self._now(), class_id, user_id),
                )
            return changed

    remove_class_member = delete_class_membership

    def transfer_class_member(self, source_class_id: str, target_class_id: str, user_id: str, *, actor_id: str) -> dict[str, Any]:
        """Move membership without rewriting historical learning facts or user profiles."""
        with self._using_connection(write=True) as connection:
            if source_class_id == target_class_id:
                raise ValueError("source and destination classes must differ")
            if not self._class_access(connection, actor_id, source_class_id, manage=True) or not self._class_access(
                connection, actor_id, target_class_id, manage=True,
            ):
                raise PermissionError("both source and destination require class management permission")
            if self.get_class_membership(source_class_id, user_id) is None:
                raise KeyError("source class membership not found")
            target_class = self.get_class(target_class_id)
            target = self.get_user(user_id)
            if target is None or target["role"] != "student" or target["status"] == "disabled":
                raise ValueError("only available student accounts may be moved")
            if self.get_course_membership(target_class["course_id"], user_id) is None:
                self.create_course_membership(target_class["course_id"], user_id)
            result = self.create_class_membership(target_class_id, user_id)
            self.delete_class_membership(source_class_id, user_id)
            return result

    def create_course_application(self, applicant_id: str, name: str, term: str) -> dict[str, Any]:
        with self._using_connection(write=True) as connection:
            applicant = self.get_user(applicant_id)
            if applicant is None or applicant["role"] != "teacher" or applicant["status"] != "active":
                raise PermissionError("course applications require an active teacher")
            record_id = self._id(None)
            connection.execute(
                """INSERT INTO course_applications(id, applicant_id, name, term, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""", (record_id, applicant_id, name.strip(), term.strip(), self._now()),
            )
            return dict(connection.execute("SELECT * FROM course_applications WHERE id = ?", (record_id,)).fetchone())

    def list_course_applications(self, *, applicant_id: str | None = None) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM course_applications WHERE (? IS NULL OR applicant_id = ?) ORDER BY created_at, id",
                (applicant_id, applicant_id),
            )]

    def review_course_application(self, application_id: str, *, reviewer_id: str, approve: bool, note: str = "") -> dict[str, Any]:
        with self._using_connection(write=True) as connection:
            reviewer = self.get_user(reviewer_id)
            if reviewer is None or reviewer["role"] != "admin" or reviewer["status"] != "active":
                raise PermissionError("course confirmation requires an administrator")
            application = connection.execute("SELECT * FROM course_applications WHERE id = ?", (application_id,)).fetchone()
            if application is None:
                raise KeyError("course application not found")
            if application["status"] != "pending":
                raise ValueError("course application already reviewed")
            course_id = None
            if approve:
                applicant = self.get_user(application["applicant_id"])
                if applicant is None or applicant["status"] != "active" or applicant["role"] != "teacher":
                    raise ValueError("applicant is no longer an active teacher")
                course_id = self.create_course(application["name"], application["term"], applicant["id"])["id"]
            connection.execute(
                """UPDATE course_applications SET status = ?, course_id = ?, reviewed_by = ?,
                   reviewed_at = ?, review_note = ? WHERE id = ?""",
                ("approved" if approve else "rejected", course_id, reviewer_id, self._now(), note, application_id),
            )
            return dict(connection.execute("SELECT * FROM course_applications WHERE id = ?", (application_id,)).fetchone())

    def consume_rate_limit(
        self, scope: str, subject: str, *, limit: int, window_seconds: int,
        now: float | int | datetime | None = None,
    ) -> tuple[bool, int]:
        """Persist an attempt before expensive work; return allowed/retry-after."""
        if not scope or type(limit) is not int or limit < 1 or type(window_seconds) is not int or window_seconds < 1:
            raise ValueError("positive rate limit and window are required")
        current = self._now() if now is None else self._timestamp(now)
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        with self._using_connection(write=True) as connection:
            row = connection.execute("SELECT * FROM auth_rate_limits WHERE scope = ? AND subject_hash = ?",
                                     (scope, subject_hash)).fetchone()
            if row is None or current >= row["window_start"] + window_seconds:
                connection.execute(
                    """INSERT INTO auth_rate_limits(scope, subject_hash, window_start, attempts)
                       VALUES (?, ?, ?, 1) ON CONFLICT(scope, subject_hash) DO UPDATE SET
                       window_start = excluded.window_start, attempts = 1""", (scope, subject_hash, current),
                )
                return True, 0
            if row["attempts"] >= limit:
                return False, max(1, math.ceil(row["window_start"] + window_seconds - current))
            connection.execute("UPDATE auth_rate_limits SET attempts = attempts + 1 WHERE scope = ? AND subject_hash = ?",
                               (scope, subject_hash))
            return True, 0

    def clear_rate_limit(self, scope: str, subject: str) -> None:
        with self._using_connection(write=True) as connection:
            connection.execute("DELETE FROM auth_rate_limits WHERE scope = ? AND subject_hash = ?",
                               (scope, hashlib.sha256(subject.encode("utf-8")).hexdigest()))

    def set_course_collaborator(
        self, course_id: str, teacher_id: str, access_level: str, *, granted_by: str,
    ) -> dict[str, Any]:
        if access_level not in {"read", "manage"}:
            raise ValueError("access_level must be 'read' or 'manage'")
        with self._using_connection(write=True) as connection:
            course = connection.execute("SELECT teacher_id FROM courses WHERE id = ?", (course_id,)).fetchone()
            if course is None:
                raise KeyError(f"course not found: {course_id}")
            issuer = connection.execute("SELECT role, status FROM users WHERE id = ?", (granted_by,)).fetchone()
            target = connection.execute("SELECT role, status FROM users WHERE id = ?", (teacher_id,)).fetchone()
            if issuer is None or issuer["status"] != "active" or not (
                issuer["role"] == "admin" or course["teacher_id"] == granted_by
            ):
                raise PermissionError("only an admin or the course owner may manage collaborators")
            if target is None or target["role"] != "teacher" or target["status"] != "active":
                raise ValueError("collaborator must be an active teacher")
            connection.execute(
                """INSERT INTO course_collaborators(course_id, teacher_id, access_level, granted_by, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(course_id, teacher_id) DO UPDATE SET
                       access_level = excluded.access_level, granted_by = excluded.granted_by,
                       created_at = excluded.created_at""",
                (course_id, teacher_id, access_level, granted_by, self._now()),
            )
            return dict(connection.execute(
                "SELECT * FROM course_collaborators WHERE course_id = ? AND teacher_id = ?",
                (course_id, teacher_id),
            ).fetchone())

    def get_course_collaborator(self, course_id: str, teacher_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute(
                "SELECT * FROM course_collaborators WHERE course_id = ? AND teacher_id = ?",
                (course_id, teacher_id),
            ).fetchone())

    def list_course_collaborators(self, course_id: str) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM course_collaborators WHERE course_id = ? ORDER BY teacher_id", (course_id,)
            )]

    def remove_course_collaborator(self, course_id: str, teacher_id: str, *, removed_by: str) -> bool:
        with self._using_connection(write=True) as connection:
            course = connection.execute("SELECT teacher_id FROM courses WHERE id = ?", (course_id,)).fetchone()
            issuer = connection.execute("SELECT role, status FROM users WHERE id = ?", (removed_by,)).fetchone()
            if course is None:
                return False
            if issuer is None or issuer["status"] != "active" or not (
                issuer["role"] == "admin" or course["teacher_id"] == removed_by
            ):
                raise PermissionError("only an admin or the course owner may manage collaborators")
            return connection.execute(
                "DELETE FROM course_collaborators WHERE course_id = ? AND teacher_id = ?",
                (course_id, teacher_id),
            ).rowcount > 0

    def can_access_course(self, user_id: str, course_id: str, *, teaching: bool = False) -> bool:
        with self._using_connection() as connection:
            row = connection.execute("""SELECT u.role, c.teacher_id,
                    EXISTS(SELECT 1 FROM course_memberships m WHERE m.course_id = c.id AND m.user_id = u.id) enrolled,
                    (SELECT cc.access_level FROM course_collaborators cc
                     WHERE cc.course_id = c.id AND cc.teacher_id = u.id) collaborator_access
                FROM users u JOIN courses c ON c.id = ? WHERE u.id = ? AND u.status = 'active'""",
                                     (course_id, user_id)).fetchone()
            if row is None:
                return False
            if row["role"] == "admin":
                return True
            if row["role"] == "teacher":
                if row["teacher_id"] == user_id:
                    return True
                return row["collaborator_access"] == "manage" or (
                    not teaching and row["collaborator_access"] == "read"
                )
            return not teaching and row["role"] == "student" and bool(row["enrolled"])

    @staticmethod
    def _class_access(connection: sqlite3.Connection, user_id: str, class_id: str, *, manage: bool = False) -> bool:
        row = connection.execute(
            """SELECT u.role, cl.owner_teacher_id,
                      (SELECT access_level FROM class_collaborators cc
                       WHERE cc.class_id = cl.id AND cc.teacher_id = u.id) collaborator_access,
                      EXISTS(SELECT 1 FROM class_memberships m
                             WHERE m.class_id = cl.id AND m.user_id = u.id) member
               FROM users u JOIN classes cl ON cl.id = ?
               WHERE u.id = ? AND u.status = 'active'""", (class_id, user_id),
        ).fetchone()
        if row is None:
            return False
        if row["role"] == "admin":
            return True
        if row["role"] == "teacher":
            return row["owner_teacher_id"] == user_id or row["collaborator_access"] == "manage" or (
                not manage and row["collaborator_access"] == "read"
            )
        return not manage and row["role"] == "student" and bool(row["member"])

    def can_access_class(self, user_id: str, class_id: str, *, teaching: bool = False) -> bool:
        """Check class authority only, independently of course material ownership."""
        with self._using_connection() as connection:
            return self._class_access(connection, user_id, class_id, manage=teaching)

    def can_manage_course_materials(self, user_id: str, course_id: str) -> bool:
        return self.can_access_course(user_id, course_id, teaching=True)

    def can_reset_user(
        self, issuer_user_id: str, user_id: str, *, course_id: str | None = None, class_id: str | None = None,
    ) -> bool:
        with self._using_connection() as connection:
            issuer = connection.execute("SELECT * FROM users WHERE id = ?", (issuer_user_id,)).fetchone()
            target = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if issuer is None or issuer["status"] != "active" or target is None or target["status"] != "active":
                return False
            if issuer["role"] == "admin":
                return True
            if issuer["role"] != "teacher" or target["role"] != "student":
                return False
            candidates = connection.execute(
                """SELECT cl.id FROM classes cl JOIN class_memberships m ON m.class_id = cl.id
                   WHERE m.user_id = ? AND (? IS NULL OR cl.course_id = ?)
                     AND (? IS NULL OR cl.id = ?)""", (user_id, course_id, course_id, class_id, class_id),
            ).fetchall()
            return any(self._class_access(connection, issuer_user_id, row["id"], manage=True) for row in candidates)

    can_reset_student = can_reset_user

    def list_accessible_classes(self, user_id: str, *, course_id: str | None = None) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM classes WHERE (? IS NULL OR course_id = ?) ORDER BY name, id", (course_id, course_id),
            ).fetchall()
            return [dict(row) for row in rows if self._class_access(connection, user_id, row["id"])]

    def can_manage_class_permissions(self, user_id: str, class_id: str) -> bool:
        with self._using_connection() as connection:
            row = connection.execute(
                """SELECT u.role, cl.owner_teacher_id FROM users u JOIN classes cl ON cl.id = ?
                   WHERE u.id = ? AND u.status = 'active'""", (class_id, user_id),
            ).fetchone()
            return bool(row and (row["role"] == "admin" or (row["role"] == "teacher" and row["owner_teacher_id"] == user_id)))

    def set_class_collaborator(
        self, class_id: str, teacher_id: str, access_level: str, *, granted_by: str,
    ) -> dict[str, Any]:
        if access_level not in {"read", "manage"}:
            raise ValueError("access_level must be read or manage")
        with self._using_connection(write=True) as connection:
            if not self.can_manage_class_permissions(granted_by, class_id):
                raise PermissionError("only the class owner or an administrator may grant class access")
            target = self.get_user(teacher_id)
            if target is None or target["role"] != "teacher" or target["status"] != "active":
                raise ValueError("collaborator must be an active teacher")
            if self.get_class(class_id)["owner_teacher_id"] == teacher_id:
                raise ValueError("class owner already has management rights")
            connection.execute(
                """INSERT INTO class_collaborators(class_id, teacher_id, access_level, granted_by, created_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(class_id, teacher_id) DO UPDATE SET
                   access_level = excluded.access_level, granted_by = excluded.granted_by,
                   created_at = excluded.created_at""", (class_id, teacher_id, access_level, granted_by, self._now()),
            )
            self._invalidate_class_codes(connection, class_id)
            return dict(connection.execute("SELECT * FROM class_collaborators WHERE class_id = ? AND teacher_id = ?",
                                           (class_id, teacher_id)).fetchone())

    def get_class_collaborator(self, class_id: str, teacher_id: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            return self._dict(connection.execute("SELECT * FROM class_collaborators WHERE class_id = ? AND teacher_id = ?",
                                                 (class_id, teacher_id)).fetchone())

    def list_class_collaborators(self, class_id: str) -> list[dict[str, Any]]:
        with self._using_connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM class_collaborators WHERE class_id = ? ORDER BY teacher_id", (class_id,),
            )]

    def remove_class_collaborator(self, class_id: str, teacher_id: str, *, removed_by: str) -> bool:
        with self._using_connection(write=True) as connection:
            if not self.can_manage_class_permissions(removed_by, class_id):
                raise PermissionError("only the class owner or an administrator may revoke class access")
            changed = connection.execute("DELETE FROM class_collaborators WHERE class_id = ? AND teacher_id = ?",
                                         (class_id, teacher_id)).rowcount > 0
            if changed:
                self._invalidate_class_codes(connection, class_id)
            return changed

    def _invalidate_class_codes(self, connection: sqlite3.Connection, class_id: str) -> None:
        connection.execute("UPDATE classes SET authorization_version = authorization_version + 1 WHERE id = ?", (class_id,))
        connection.execute("UPDATE activation_codes SET revoked_at = ? WHERE class_id = ? AND consumed_at IS NULL",
                           (self._now(), class_id))

    def transfer_class_owner(self, class_id: str, owner_teacher_id: str, *, transferred_by: str) -> dict[str, Any]:
        with self._using_connection(write=True) as connection:
            if not self.can_manage_class_permissions(transferred_by, class_id):
                raise PermissionError("only the class owner or an administrator may transfer ownership")
            target = self.get_user(owner_teacher_id)
            if target is None or target["role"] != "teacher" or target["status"] != "active":
                raise ValueError("class owner must be an active teacher")
            connection.execute("UPDATE classes SET owner_teacher_id = ? WHERE id = ?", (owner_teacher_id, class_id))
            # Old owners keep no implicit access; regranting is an explicit action.
            connection.execute("DELETE FROM class_collaborators WHERE class_id = ? AND teacher_id = ?", (class_id, owner_teacher_id))
            self._invalidate_class_codes(connection, class_id)
            return self.get_class(class_id)

    def record_audit_event(self, event_type: str, actor_user_id: str | None = None, *,
                           resource_type: str | None = None, resource_id: str | None = None,
                           payload: Mapping[str, Any] | None = None, ip: str | None = None,
                           event_id: str | None = None,
                           created_at: float | int | datetime | None = None) -> dict[str, Any]:
        record_id, data = self._id(event_id), self._json(dict(payload or {}))
        created = self._now() if created_at is None else self._timestamp(created_at)
        with self._using_connection(write=True) as connection:
            connection.execute("""INSERT INTO audit_events
                (id, actor_user_id, event_type, resource_type, resource_id, payload_json, ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                               (record_id, actor_user_id, event_type.strip(), resource_type, resource_id, data, ip, created))
            result = dict(connection.execute("SELECT * FROM audit_events WHERE id = ?", (record_id,)).fetchone())
            result["payload"] = json.loads(result["payload_json"])
            return result

    def list_audit_events(self, *, limit: int = 100, actor_user_id: str | None = None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._using_connection() as connection:
            result = [dict(row) for row in connection.execute(
                """SELECT * FROM audit_events WHERE (? IS NULL OR actor_user_id = ?)
                   ORDER BY created_at DESC, id DESC LIMIT ?""", (actor_user_id, actor_user_id, limit),
            )]
        for row in result:
            row["payload"] = json.loads(row["payload_json"])
        return result

    def create_idempotency_record(self, scope: str, key: str, *, owner_id: str | None = None,
                                  request_hash: str | None = None, status_code: int | None = None,
                                  response: Any = None,
                                  expires_at: float | int | datetime | None = None,
                                  created_at: float | int | datetime | None = None) -> dict[str, Any]:
        expiry = None if expires_at is None else self._timestamp(expires_at)
        created = self._now() if created_at is None else self._timestamp(created_at)
        response_json = self._json(response)
        scope, key = scope.strip(), key.strip()
        with self._using_connection(write=True) as connection:
            connection.execute("""INSERT INTO idempotency
                (scope, key, owner_id, request_hash, status_code, response_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                               (scope, key, owner_id, request_hash, status_code, response_json, created, expiry))
            result = dict(connection.execute("SELECT * FROM idempotency WHERE scope = ? AND key = ?",
                                             (scope, key)).fetchone())
            result["response"] = json.loads(result["response_json"])
            return result

    def get_idempotency_record(self, scope: str, key: str) -> dict[str, Any] | None:
        with self._using_connection() as connection:
            result = self._dict(connection.execute("SELECT * FROM idempotency WHERE scope = ? AND key = ?",
                                                   (scope, key)).fetchone())
        if result is not None:
            result["response"] = json.loads(result["response_json"])
        return result

    def delete_idempotency_record(self, scope: str, key: str) -> bool:
        with self._using_connection(write=True) as connection:
            return connection.execute("DELETE FROM idempotency WHERE scope = ? AND key = ?",
                                      (scope, key)).rowcount > 0


__all__ = ["PlatformStore", "resolve_platform_db_path"]
