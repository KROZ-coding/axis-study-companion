"""Read-only inspection and dry-run mapping for legacy PlatformStore snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from io import BytesIO
from pathlib import Path, PurePosixPath
import sqlite3
from typing import Any, Mapping
import zlib
from zipfile import BadZipFile, ZipFile


MAX_SNAPSHOT_BYTES = 700 * 1024 * 1024
SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1, 2, 3})


class MigrationError(ValueError):
    """A legacy snapshot is unreadable, ambiguous, or outside the known format."""


@dataclass(frozen=True)
class LegacySnapshot:
    state: dict[str, Any]
    source_format: str
    source_sha256: str
    attachments: Mapping[str, bytes]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise MigrationError(f"non-finite JSON number is not allowed: {value}")


def _decode_state(raw: bytes, source_format: str, source_sha256: str) -> LegacySnapshot:
    try:
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys,
                           parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError("legacy snapshot JSON is invalid") from error
    if not isinstance(state, dict):
        raise MigrationError("legacy snapshot root must be an object")
    version = state.get("version")
    if type(version) is not int or version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise MigrationError(f"unsupported legacy snapshot version: {version!r}")
    expected_shapes = {
        "users": dict, "courses": dict, "enrollments": list, "questions": dict,
        "quiz_sessions": dict, "practices": dict, "assignments": dict,
        "results": dict, "mastery": list,
    }
    for key, expected_type in expected_shapes.items():
        if key in state and not isinstance(state[key], expected_type):
            raise MigrationError(f"legacy collection {key!r} has an invalid shape")
    return LegacySnapshot(state=state, source_format=source_format, source_sha256=source_sha256, attachments={})


def decode_legacy_sqlite(path: str | Path, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> LegacySnapshot:
    """Decode the known zlib(JSON) row without opening the source database for writes."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    source = Path(path).expanduser().resolve(strict=True)
    if source.stat().st_size > max_bytes:
        raise MigrationError("legacy SQLite file exceeds the configured size limit")
    uri = source.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only=ON")
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if user_version != 0 or application_id != 0 or tables != {"store"}:
                raise MigrationError("unrecognized legacy SQLite schema")
            columns = [row[1] for row in connection.execute("PRAGMA table_info(store)")]
            if set(columns) != {"id", "blob"}:
                raise MigrationError("legacy store table has an unknown shape")
            rows = connection.execute("SELECT id, blob FROM store").fetchall()
            if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], (bytes, bytearray, memoryview)):
                raise MigrationError("legacy store snapshot row is missing or invalid")
            compressed = bytes(rows[0][1])
        finally:
            connection.close()
    except MigrationError:
        raise
    except sqlite3.Error as error:
        raise MigrationError("legacy SQLite snapshot is unreadable") from error

    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, max_bytes + 1)
        if len(raw) > max_bytes or decoder.unconsumed_tail:
            raise MigrationError("decompressed legacy snapshot exceeds the configured size limit")
        raw += decoder.flush(max_bytes + 1 - len(raw))
        if len(raw) > max_bytes or not decoder.eof or decoder.unused_data:
            raise MigrationError("legacy snapshot compressed stream is invalid or too large")
    except zlib.error as error:
        raise MigrationError("legacy snapshot zlib data is invalid") from error
    return _decode_state(raw, "sqlite-zlib-json", hashlib.sha256(compressed).hexdigest())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_legacy_json(content: bytes | bytearray | str | Path, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> LegacySnapshot:
    """Decode legacy store.json without importing it into any application store."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(content, (str, Path)):
        path = Path(content).expanduser().resolve(strict=True)
        if path.stat().st_size > max_bytes:
            raise MigrationError("legacy JSON exceeds the configured size limit")
        raw = path.read_bytes()
    elif isinstance(content, (bytes, bytearray)):
        raw = bytes(content)
    else:
        raise TypeError("content must be JSON bytes or a path")
    if len(raw) > max_bytes:
        raise MigrationError("legacy JSON exceeds the configured size limit")
    return _decode_state(raw, "json", hashlib.sha256(raw).hexdigest())


def _safe_archive_name(name: str) -> bool:
    if "\\" in name or "\x00" in name or ":" in name or not name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if name != PurePosixPath(name).as_posix() or any(part in {"", ".", ".."} for part in parts):
        return False
    return name == "store.json" or (len(parts) >= 2 and parts[0] == "files")


def decode_legacy_archive(content: bytes | bytearray | str | Path, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> LegacySnapshot:
    """Read a legacy Store ZIP in memory, validating every path before reading files."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(content, (str, Path)):
        raw_archive = Path(content).read_bytes()
    elif isinstance(content, (bytes, bytearray)):
        raw_archive = bytes(content)
    else:
        raise TypeError("content must be ZIP bytes or a path")
    if len(raw_archive) > max_bytes:
        raise MigrationError("legacy archive exceeds the configured size limit")
    try:
        with ZipFile(BytesIO(raw_archive)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos if not info.is_dir()]
            if len(names) != len(set(names)) or "store.json" not in names:
                raise MigrationError("legacy archive has duplicate paths or no store.json")
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if not _safe_archive_name(info.filename) or info.filename.lower().endswith(".pkl"):
                    raise MigrationError("legacy archive contains an unsafe path")
                total += info.file_size
                if total > max_bytes:
                    raise MigrationError("legacy archive expands beyond the configured size limit")
            state_bytes = archive.read("store.json")
            snapshot = _decode_state(state_bytes, "zip-store-json", hashlib.sha256(raw_archive).hexdigest())
            attachments = {
                info.filename[6:]: archive.read(info)
                for info in infos
                if not info.is_dir() and info.filename.startswith("files/")
            }
            return LegacySnapshot(snapshot.state, snapshot.source_format, snapshot.source_sha256, attachments)
    except MigrationError:
        raise
    except (BadZipFile, KeyError, OSError, RuntimeError, EOFError, ValueError) as error:
        raise MigrationError("legacy archive is unreadable") from error


def _records(collection: Any) -> list[tuple[str, Any]]:
    if isinstance(collection, dict):
        return [(str(key), value) for key, value in collection.items()]
    if isinstance(collection, list):
        return [(str(index), value) for index, value in enumerate(collection)]
    return []


def _mapping_for(
    mappings: Mapping[str, Mapping[str, Mapping[str, str]]], domain: str, collection: str
) -> Mapping[str, str]:
    selected = mappings.get(domain, {})
    if not isinstance(selected, Mapping):
        return {}
    entries = selected.get(collection, {})
    return entries if isinstance(entries, Mapping) else {}


def dry_run_migration(
    snapshot: LegacySnapshot,
    *,
    mappings: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> dict[str, Any]:
    """Report exact source counts, explicit ID mappings, orphans, and quarantined facts.

    No target store is opened or modified. Each record intended for the new platform or
    learning schema requires an explicit mapping at mappings[domain][collection][source_id].
    """
    if not isinstance(snapshot, LegacySnapshot):
        raise TypeError("snapshot must come from a legacy decoder")
    maps = mappings or {}
    if not isinstance(maps, Mapping):
        raise ValueError("mappings must be an object")
    state = snapshot.state
    platform_names = ("users", "courses", "enrollments")
    learning_names = ("questions", "quiz_sessions", "practices", "assignments", "results", "mastery")
    collection_counts: dict[str, int] = {}
    mapped_counts: dict[str, int] = {}
    missing_mappings: list[dict[str, str]] = []
    mapping_summary: list[dict[str, str]] = []
    quarantine: list[dict[str, Any]] = []

    for domain, names in (("platform", platform_names), ("learning", learning_names)):
        for name in names:
            records = _records(state.get(name, {} if name != "enrollments" else []))
            collection_counts[name] = len(records)
            explicit = _mapping_for(maps, domain, name)
            mapped = 0
            actual_keys = {key for key, _ in records}
            for source_id, record in records:
                target_id = explicit.get(source_id)
                if isinstance(target_id, str) and target_id.strip():
                    mapped += 1
                    mapping_summary.append({"domain": domain, "collection": name,
                                            "source_id": source_id, "target_id": target_id.strip()})
                else:
                    missing_mappings.append({"domain": domain, "collection": name, "source_id": source_id})
                    quarantine.append({"domain": domain, "collection": name, "source_id": source_id,
                                       "reason": "explicit_target_mapping_required", "raw": record})
            for unused in set(explicit) - actual_keys:
                quarantine.append({"domain": domain, "collection": name, "source_id": str(unused),
                                   "reason": "mapping_source_not_found", "raw": None})
            mapped_counts[name] = mapped

    users = state.get("users", {})
    courses = state.get("courses", {})
    questions = state.get("questions", {})
    sessions = state.get("quiz_sessions", {})
    orphans: list[dict[str, str]] = []
    for index, enrollment in enumerate(state.get("enrollments", [])):
        if not isinstance(enrollment, (list, tuple)) or len(enrollment) != 3:
            orphans.append({"collection": "enrollments", "source_id": str(index), "reason": "invalid_enrollment_shape"})
            continue
        course_id, user_id, _class_name = enrollment
        if not isinstance(courses, dict) or course_id not in courses:
            orphans.append({"collection": "enrollments", "source_id": str(index), "reason": f"missing_course:{course_id}"})
        if not isinstance(users, dict) or user_id not in users:
            orphans.append({"collection": "enrollments", "source_id": str(index), "reason": f"missing_user:{user_id}"})
    for session_id, session in _records(sessions):
        if not isinstance(session, dict):
            orphans.append({"collection": "quiz_sessions", "source_id": session_id, "reason": "invalid_session_shape"})
            continue
        user_id, course_id = session.get("user_id"), session.get("course_id")
        if user_id is not None and (not isinstance(users, dict) or user_id not in users):
            orphans.append({"collection": "quiz_sessions", "source_id": session_id, "reason": f"missing_user:{user_id}"})
        if course_id is not None and (not isinstance(courses, dict) or course_id not in courses):
            orphans.append({"collection": "quiz_sessions", "source_id": session_id, "reason": f"missing_course:{course_id}"})
        source_questions = session.get("questions", [])
        if isinstance(source_questions, list):
            for question in source_questions:
                if isinstance(question, dict) and question.get("id") is not None:
                    question_id = question["id"]
                    if isinstance(questions, dict) and question_id not in questions:
                        orphans.append({"collection": "quiz_sessions", "source_id": session_id,
                                        "reason": f"missing_question:{question_id}"})
    for practice_id, practice in _records(state.get("practices", {})):
        if not isinstance(practice, dict):
            orphans.append({"collection": "practices", "source_id": practice_id, "reason": "invalid_practice_shape"})
            continue
        user_id, course_id = practice.get("user_id"), practice.get("course_id")
        if user_id is not None and (not isinstance(users, dict) or user_id not in users):
            orphans.append({"collection": "practices", "source_id": practice_id, "reason": f"missing_user:{user_id}"})
        if course_id is not None and (not isinstance(courses, dict) or course_id not in courses):
            orphans.append({"collection": "practices", "source_id": practice_id, "reason": f"missing_course:{course_id}"})
        source_question_ids = practice.get("source_questions", [])
        if not isinstance(source_question_ids, list):
            orphans.append({"collection": "practices", "source_id": practice_id,
                            "reason": "invalid_source_questions_shape"})
        else:
            for question_id in source_question_ids:
                if isinstance(questions, dict) and question_id not in questions:
                    orphans.append({"collection": "practices", "source_id": practice_id,
                                    "reason": f"missing_source_question:{question_id}"})
    assignments = state.get("assignments", {})
    for assignment_id, assignment in _records(assignments):
        if not isinstance(assignment, dict):
            orphans.append({"collection": "assignments", "source_id": assignment_id, "reason": "invalid_assignment_shape"})
            continue
        course_id = assignment.get("course_id")
        if course_id is not None and (not isinstance(courses, dict) or course_id not in courses):
            orphans.append({"collection": "assignments", "source_id": assignment_id, "reason": f"missing_course:{course_id}"})
        for field in ("student_ids", "session_ids"):
            references = assignment.get(field, []) or []
            if not isinstance(references, list):
                orphans.append({"collection": "assignments", "source_id": assignment_id,
                                "reason": f"invalid_{field}_shape"})
                continue
            referenced = users if field == "student_ids" else sessions
            label = "student" if field == "student_ids" else "session"
            for reference in references:
                if isinstance(referenced, dict) and reference not in referenced:
                    orphans.append({"collection": "assignments", "source_id": assignment_id,
                                    "reason": f"missing_{label}:{reference}"})
    results = state.get("results", {})
    for result_id, result in _records(results):
        if not isinstance(result, dict):
            orphans.append({"collection": "results", "source_id": result_id, "reason": "invalid_result_shape"})
            continue
        session_id = result.get("session_id")
        if session_id is not None and (not isinstance(sessions, dict) or session_id not in sessions):
            orphans.append({"collection": "results", "source_id": result_id,
                            "reason": f"missing_quiz_session:{session_id}"})

    understood = {"version", *platform_names, *learning_names}
    for name, value in state.items():
        if name not in understood:
            quarantine.append({"domain": "unknown", "collection": name, "source_id": name,
                               "reason": "no_confirmed_target_schema", "raw": value})
    if "mastery" in state:
        # Legacy mastery is derived and its evidence/algorithm is not a target fact mapping.
        for source_id, record in _records(state.get("mastery")):
            quarantine.append({"domain": "learning", "collection": "mastery", "source_id": source_id,
                               "reason": "derived_legacy_fact_requires_review", "raw": record})

    attachments = sorted(snapshot.attachments)
    return {
        "source": {"format": snapshot.source_format, "sha256": snapshot.source_sha256,
                   "snapshot_version": state["version"]},
        "statistics": {"collections": collection_counts, "mapped": mapped_counts,
                       "orphan_count": len(orphans), "missing_mapping_count": len(missing_mappings),
                       "quarantined_count": len(quarantine), "attachment_count": len(attachments)},
        "explicit_mappings": mapping_summary,
        "missing_mappings": missing_mappings,
        "orphans": orphans,
        "quarantine": quarantine,
        "attachments": attachments,
        "import_enabled": False,
        "status": "requires_review" if (orphans or missing_mappings or quarantine) else "awaiting_schema_adapter",
    }


def require_explicit_mappings(
    dry_run: Mapping[str, Any], *, platform: Mapping[str, Mapping[str, str]],
    learning: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail clearly when any source record lacks an explicit destination identity."""
    if dry_run.get("orphans"):
        raise MigrationError("legacy source contains orphan references")
    if dry_run.get("quarantine"):
        raise MigrationError("legacy source contains records requiring quarantine review")
    required: dict[str, Mapping[str, Mapping[str, str]]] = {"platform": platform, "learning": learning}
    missing = []
    for item in dry_run.get("missing_mappings", []):
        table = required.get(item["domain"], {}).get(item["collection"], {})
        if item["source_id"] not in table or not str(table[item["source_id"]]).strip():
            missing.append(item)
    if missing:
        raise MigrationError("explicit source-to-target mappings are required for every imported record")
    raise MigrationError("import is disabled until the learning and platform schema adapters are confirmed")
