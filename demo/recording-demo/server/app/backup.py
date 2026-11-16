"""Verified six-database backup bundles with isolated, non-activating restore."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from typing import Any, Mapping
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
from io import BytesIO


BUNDLE_FORMAT = "learning-platform-backup"
BUNDLE_VERSION = 1
REQUIRED_DATABASES = ("platform", "learning", "jobs", "content", "reports", "companion")
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_BYTES = 700 * 1024 * 1024


class BackupError(ValueError):
    """Backup is incomplete, corrupt, or incompatible with the configured schemas."""


@dataclass(frozen=True)
class DatabaseContract:
    """Exact schema trust contract supplied by each store owner."""

    user_version: int
    application_id: int
    tables: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.user_version) is not int or self.user_version < 1:
            raise ValueError("database contract requires a positive user_version")
        if type(self.application_id) is not int or not 0 <= self.application_id <= 0x7FFFFFFF:
            raise ValueError("database contract requires an explicit SQLite application_id")
        if not self.tables or any(not isinstance(item, str) or not item for item in self.tables):
            raise ValueError("database contract requires the exact non-empty table set")


DEFAULT_DATABASE_CONTRACTS = {
    "platform": DatabaseContract(2, 0, frozenset({
        "users", "sessions", "activation_codes", "courses", "course_collaborators", "classes",
        "course_memberships", "class_memberships", "audit_events", "idempotency",
        "class_collaborators", "course_applications", "auth_rate_limits",
    })),
    "learning": DatabaseContract(2, 0, frozenset({
        "learning_goals", "goal_revisions", "learning_rounds", "question_versions",
        "learning_tasks", "task_items", "draft_answers", "attempts", "attempt_items",
        "grading_outbox", "idempotency", "task_events", "deadline_extensions",
        "grading_corrections", "report_revisions", "learning_revision", "goal_commands",
        "round_policies", "task_policies", "teacher_judgments", "diagnostic_sessions",
        "diagnostic_items", "question_family_index", "question_metadata", "learning_outbox", "appeals", "goal_plans",
    })),
    "jobs": DatabaseContract(2, 0x41495132, frozenset({
        "queue_meta", "budgets", "jobs", "steps", "events", "upstream_calls", "call_charges",
        "funding_transactions", "budget_limits", "call_budget_reservations", "budget_ledger_entries",
    })),
    "content": DatabaseContract(1, 0, frozenset({
        "import_batches", "material_files", "source_blocks", "material_issues", "confirmations",
        "confirmation_blocks", "course_requirements", "requirement_blocks", "knowledge_points",
        "knowledge_sources", "knowledge_prerequisites", "content_jobs",
    })),
    "reports": DatabaseContract(1, 0, frozenset({"reports", "report_snapshots", "report_revisions", "report_idempotency"})),
    "companion": DatabaseContract(1, 0x41584350, frozenset({
        "conversations", "messages", "responses", "response_outbox", "events", "collections",
        "collection_items", "notes", "memories", "discussion_threads", "discussion_messages",
        "notifications", "retention_policies", "legal_holds", "audit_events",
    })),
}


def application_id_from_text(value: str) -> int:
    """Convert a four-character SQLite application id such as AXCP to its integer form."""
    if not isinstance(value, str) or len(value) != 4 or not value.isascii():
        raise ValueError("application id text must be exactly four ASCII characters")
    result = int.from_bytes(value.encode("ascii"), "big", signed=False)
    if result > 0x7FFFFFFF:
        raise ValueError("application id exceeds SQLite's signed 32-bit range")
    return result


def _normal_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=True)


def _connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    # immutable is reserved for detached, complete SQLite backup copies. Live
    # databases must keep normal WAL visibility during backup/read validation.
    options = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    connection = sqlite3.connect(path.as_uri() + options, uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _schema(path: Path, *, immutable: bool = False) -> dict[str, Any]:
    try:
        with closing(_connect_readonly(path, immutable=immutable)) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            tables = sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign_key_errors = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    except sqlite3.Error as error:
        raise BackupError(f"cannot read SQLite database {path.name}") from error
    if integrity != ["ok"] or foreign_key_errors:
        raise BackupError(f"SQLite integrity validation failed for {path.name}")
    return {"user_version": version, "application_id": application_id, "tables": tables}


def _validate_contract(name: str, schema: Mapping[str, Any], contract: DatabaseContract) -> None:
    if schema.get("user_version") != contract.user_version:
        raise BackupError(f"unsupported {name} schema version {schema.get('user_version')!r}")
    if schema.get("application_id") != contract.application_id:
        raise BackupError(f"unexpected {name} SQLite application_id")
    if set(schema.get("tables", ())) != set(contract.tables):
        raise BackupError(f"unexpected {name} database table set")


def _validate_contracts(contracts: Mapping[str, DatabaseContract]) -> None:
    if set(contracts) != set(REQUIRED_DATABASES):
        raise BackupError("exact schema contracts for all six databases are required")
    if any(not isinstance(contracts[name], DatabaseContract) for name in REQUIRED_DATABASES):
        raise BackupError("every database requires a DatabaseContract")


def _attachment_files(
    root: Path,
    database_paths: Mapping[str, Path],
    excluded_roots: tuple[Path, ...] = (),
) -> list[tuple[Path, str]]:
    if not root.is_dir():
        raise BackupError("attachment directory is missing")
    excluded: set[Path] = set()
    for database in database_paths.values():
        resolved = database.expanduser().resolve(strict=False)
        excluded.add(resolved)
        excluded.update(Path(str(resolved) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    excluded_roots = tuple(
        other.expanduser().resolve(strict=False)
        for other in excluded_roots
        if other.expanduser().resolve(strict=False) != root.expanduser().resolve(strict=False)
        and other.expanduser().resolve(strict=False).is_relative_to(root.expanduser().resolve(strict=False))
    )
    excluded.clear()
    for database in database_paths.values():
        resolved = database.expanduser().resolve(strict=False)
        excluded.add(resolved)
        excluded.update(Path(str(resolved) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    files: list[tuple[Path, str]] = []
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise BackupError("symbolic links are not allowed in attachments")
        if not candidate.is_file():
            continue
        if candidate == root:
            continue
        resolved = candidate.resolve(strict=False)
        if resolved in excluded or any(resolved == other or resolved.is_relative_to(other) for other in excluded_roots):
            continue
        relative = candidate.relative_to(root).as_posix()
        if not _safe_relative(relative):
            raise BackupError("attachment path is unsafe")
        files.append((candidate, relative))
    return files


def _safe_relative(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    if value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    if path.parts and ":" in path.parts[0]:
        return False
    return True


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


class BackupManager:
    """Create or verify complete bundles; restores are staged away from live DBs."""

    def __init__(
        self,
        database_paths: Mapping[str, str | Path],
        attachment_dir: str | Path,
        contracts: Mapping[str, DatabaseContract] | None = None,
        *,
        additional_attachment_dirs: Mapping[str, str | Path] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if set(database_paths) != set(REQUIRED_DATABASES):
            raise BackupError("database_paths must include exactly the six required domains")
        selected_contracts = DEFAULT_DATABASE_CONTRACTS if contracts is None else contracts
        _validate_contracts(selected_contracts)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.database_paths = {name: Path(database_paths[name]).expanduser() for name in REQUIRED_DATABASES}
        self.attachment_dir = Path(attachment_dir).expanduser()
        additional = dict(additional_attachment_dirs or {})
        if any(not isinstance(name, str) or not name.isidentifier() or name in {"shared", "databases"} for name in additional):
            raise ValueError("additional attachment roots require safe unique names")
        self.attachment_dirs = {"shared": self.attachment_dir,
                                **{name: Path(path).expanduser() for name, path in additional.items()}}
        self.contracts = dict(selected_contracts)
        self.max_bytes = max_bytes

    def _validate_live_databases(self) -> dict[str, dict[str, Any]]:
        schemas: dict[str, dict[str, Any]] = {}
        for name in REQUIRED_DATABASES:
            path = _normal_path(self.database_paths[name])
            schema = _schema(path)
            _validate_contract(name, schema, self.contracts[name])
            schemas[name] = schema
        return schemas

    def export_bytes(self) -> bytes:
        """Snapshot each live DB through SQLite's backup API, then build a checked bundle."""
        schemas = self._validate_live_databases()
        attachment_roots = {
            name: path.expanduser().resolve(strict=False)
            for name, path in self.attachment_dirs.items()
        }
        attachments = {}
        for name, root in attachment_roots.items():
            if not root.exists():
                if name == "shared":
                    raise BackupError("attachment directory is missing")
                attachments[name] = []
                continue
            if not root.is_dir():
                raise BackupError("attachment root must be a directory")
            attachments[name] = _attachment_files(
                root, self.database_paths,
                tuple(other for other_name, other in attachment_roots.items() if other_name != name),
            )
        with tempfile.TemporaryDirectory(prefix="verified-backup-") as directory:
            temp_root = Path(directory)
            database_entries: dict[str, dict[str, Any]] = {}
            snapshot_paths: dict[str, Path] = {}
            for name in REQUIRED_DATABASES:
                source = _normal_path(self.database_paths[name])
                snapshot = temp_root / f"{name}.sqlite3"
                try:
                    with closing(_connect_readonly(source)) as source_connection, closing(sqlite3.connect(snapshot)) as target:
                        source_connection.backup(target)
                except sqlite3.Error as error:
                    raise BackupError(f"could not snapshot {name} database") from error
                snapshot_paths[name] = snapshot
                schema = _schema(snapshot, immutable=True)
                _validate_contract(name, schema, self.contracts[name])
                database_entries[name] = {
                    "path": f"databases/{name}.sqlite3", **schema,
                    "sha256": _hash_file(snapshot)[0], "size": snapshot.stat().st_size,
                }
            attachment_entries: list[dict[str, Any]] = []
            total = 0
            for root_name, files in attachments.items():
                for source, relative in files:
                    digest, size = _hash_file(source)
                    total += size
                    if total > self.max_bytes:
                        raise BackupError("attachment data exceeds the configured size limit")
                    attachment_entries.append({"path": f"attachments/{root_name}/{relative}",
                                               "sha256": digest, "size": size})
            manifest = {
                "format": BUNDLE_FORMAT, "bundle_version": BUNDLE_VERSION,
                "databases": database_entries, "attachments": attachment_entries,
            }
            output = BytesIO()
            try:
                with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                    archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    for name in REQUIRED_DATABASES:
                        archive.write(snapshot_paths[name], database_entries[name]["path"])
                    for root_name, files in attachments.items():
                        for source, relative in files:
                            archive.write(source, f"attachments/{root_name}/{relative}")
            except OSError as error:
                raise BackupError("could not create backup archive") from error
            content = output.getvalue()
            if len(content) > self.max_bytes:
                raise BackupError("backup bundle exceeds the configured size limit")
            return content

    def write_backup(self, destination: str | Path) -> Path:
        """Write a complete bundle to a new file without replacing an existing backup."""
        target = Path(destination).expanduser().resolve()
        live_files = {path.expanduser().resolve(strict=False) for path in self.database_paths.values()}
        live_files.update(Path(str(path) + suffix).resolve(strict=False)
                          for path in self.database_paths.values() for suffix in ("-wal", "-shm", "-journal"))
        if target in live_files:
            raise BackupError("backup destination must not be a live database or SQLite sidecar")
        target.parent.mkdir(parents=True, exist_ok=True)
        data_root = self.attachment_dir.expanduser().resolve(strict=False)
        if target == data_root or target.is_relative_to(data_root):
            raise BackupError("backup archive must be written outside the live data directory")
        if target.exists():
            raise FileExistsError(target)
        content = self.export_bytes()
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def inspect(self, content: bytes | bytearray | str | Path) -> dict[str, Any]:
        """Validate a bundle completely without extracting or modifying any files."""
        data = _read_archive_bytes(content, self.max_bytes)
        manifest, entries = _read_and_validate(data, self.contracts, self.max_bytes)
        return {"format": manifest["format"], "bundle_version": manifest["bundle_version"],
                "databases": manifest["databases"], "attachments": manifest["attachments"],
                "member_count": len(entries), "verified": True}

    def restore_to_staging(
        self,
        content: bytes | bytearray | str | Path,
        staging_parent: str | Path,
    ) -> Path:
        """Restore into a new isolated directory; never targets a configured live path."""
        data = _read_archive_bytes(content, self.max_bytes)
        manifest, members = _read_and_validate(data, self.contracts, self.max_bytes)
        parent = Path(staging_parent).expanduser().resolve()
        if not parent.is_dir():
            raise BackupError("staging_parent must already exist")
        live_roots = {path.expanduser().resolve() for path in self.attachment_dirs.values()}
        live_roots.update(path.expanduser().resolve().parent for path in self.database_paths.values())
        for live in live_roots:
            if parent == live or parent.is_relative_to(live):
                raise BackupError("restore staging must be outside every live data directory")
        destination = parent / f"restore-{uuid4().hex}"
        work = parent / f".restore-{uuid4().hex}.tmp"
        work.mkdir(mode=0o700)
        try:
            with ZipFile(BytesIO(data)) as archive:
                for name, member in members.items():
                    if name == MANIFEST_NAME:
                        continue
                    if name.startswith("databases/"):
                        path = work / name
                    else:
                        path = work / "attachments" / name.split("/", 2)[1] / name.split("/", 2)[2]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, path.open("xb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            for name, entry in manifest["databases"].items():
                database = work / entry["path"]
                schema = _schema(database, immutable=True)
                _validate_contract(name, schema, self.contracts[name])
                if _hash_file(database)[0] != entry["sha256"]:
                    raise BackupError(f"restored database hash mismatch for {name}")
            for entry in manifest["attachments"]:
                attachment = work / entry["path"]
                digest, size = _hash_file(attachment)
                if digest != entry["sha256"] or size != entry["size"]:
                    raise BackupError("restored attachment validation failed")
            os.replace(work, destination)
            return destination
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise


def _read_archive_bytes(content: bytes | bytearray | str | Path, max_bytes: int) -> bytes:
    if isinstance(content, (str, Path)):
        path = Path(content).expanduser().resolve(strict=True)
        if path.stat().st_size > max_bytes:
            raise BackupError("backup bundle exceeds the configured size limit")
        data = path.read_bytes()
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    else:
        raise TypeError("content must be bytes or a backup path")
    if len(data) > max_bytes:
        raise BackupError("backup bundle exceeds the configured size limit")
    return data


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise BackupError(f"non-finite manifest number is not allowed: {value}")


def _read_and_validate(
    content: bytes,
    contracts: Mapping[str, DatabaseContract],
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_contracts(contracts)
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            files = [item for item in infos if not item.is_dir()]
            names = [item.filename for item in files]
            if len(names) != len(set(names)) or MANIFEST_NAME not in names:
                raise BackupError("backup archive has duplicate entries or no manifest")
            if any(not _safe_relative(name) for name in names):
                raise BackupError("backup archive has an unsafe path")
            manifest_raw = archive.read(MANIFEST_NAME)
            manifest = json.loads(manifest_raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys,
                                  parse_constant=_reject_json_constant)
            if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
                raise BackupError("unknown backup bundle format")
            if type(manifest.get("bundle_version")) is not int or manifest["bundle_version"] != BUNDLE_VERSION:
                raise BackupError("unsupported backup bundle version")
            databases = manifest.get("databases")
            attachments = manifest.get("attachments")
            if not isinstance(databases, dict) or set(databases) != set(REQUIRED_DATABASES) or not isinstance(attachments, list):
                raise BackupError("backup manifest must describe all six databases and an attachment list")
            expected_paths = {MANIFEST_NAME}
            for name in REQUIRED_DATABASES:
                entry = databases[name]
                if not isinstance(entry, dict) or entry.get("path") != f"databases/{name}.sqlite3":
                    raise BackupError(f"invalid manifest entry for {name}")
                if (type(entry.get("user_version")) is not int
                        or type(entry.get("application_id")) is not int
                        or type(entry.get("size")) is not int or entry["size"] < 1
                        or not isinstance(entry.get("sha256"), str)
                        or len(entry["sha256"]) != 64
                        or not isinstance(entry.get("tables"), list)
                        or any(not isinstance(table, str) or not table for table in entry["tables"])):
                    raise BackupError(f"invalid schema or hash fields for {name}")
                expected_paths.add(entry["path"])
            seen_attachments: set[str] = set()
            for entry in attachments:
                if not isinstance(entry, dict):
                    raise BackupError("invalid attachment manifest entry")
                path = entry.get("path")
                if (not isinstance(path, str) or not path.startswith("attachments/") or not _safe_relative(path)
                        or len(PurePosixPath(path).parts) < 3
                        or type(entry.get("size")) is not int or entry["size"] < 0
                        or not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64):
                    raise BackupError("invalid attachment manifest path or hash fields")
                root_name = PurePosixPath(path).parts[1]
                if not root_name.isidentifier() or root_name == "databases":
                    raise BackupError("invalid attachment root name")
                if path in seen_attachments:
                    raise BackupError("duplicate attachment manifest path")
                seen_attachments.add(path)
                expected_paths.add(path)
            if set(names) != expected_paths:
                raise BackupError("archive entries do not exactly match the manifest")
            total = sum(item.file_size for item in files)
            if total > max_bytes:
                raise BackupError("backup archive expands beyond the configured size limit")
            by_name = {item.filename: item for item in files}
            for name in REQUIRED_DATABASES:
                entry = databases[name]
                info = by_name[entry["path"]]
                payload = archive.read(info)
                if len(payload) != entry.get("size") or hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
                    raise BackupError(f"database checksum mismatch for {name}")
                if entry.get("user_version") != contracts[name].user_version:
                    raise BackupError(f"unsupported {name} schema version {entry.get('user_version')!r}")
                if entry.get("application_id") != contracts[name].application_id:
                    raise BackupError(f"unexpected {name} application_id")
                if set(entry.get("tables", ())) != set(contracts[name].tables):
                    raise BackupError(f"unexpected {name} table set")
                with tempfile.TemporaryDirectory(prefix="verify-db-") as directory:
                    temporary_path = Path(directory) / "database.sqlite3"
                    temporary_path.write_bytes(payload)
                    schema = _schema(temporary_path, immutable=True)
                _validate_contract(name, schema, contracts[name])
                if schema["user_version"] != entry["user_version"] or schema["application_id"] != entry["application_id"] or schema["tables"] != entry["tables"]:
                    raise BackupError(f"database schema manifest mismatch for {name}")
            for entry in attachments:
                payload = archive.read(by_name[entry["path"]])
                if len(payload) != entry.get("size") or hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
                    raise BackupError("attachment checksum mismatch")
            return manifest, by_name
    except BackupError:
        raise
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, OSError, RuntimeError, EOFError, TypeError, ValueError, sqlite3.Error) as error:
        raise BackupError("invalid backup archive") from error
