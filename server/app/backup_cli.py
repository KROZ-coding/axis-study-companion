"""Stopped-write six-store backup and offline, non-activating recovery.

This module deliberately imports neither Settings nor Runtime: importing the CLI,
verifying a bundle, and restoring it must not load .env or open a live store.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Sequence

from .backup import (
    BackupError,
    BackupManager,
    DEFAULT_DATABASE_CONTRACTS,
    DEFAULT_MAX_BYTES,
    REQUIRED_DATABASES,
)


SERVER_DIR = Path(__file__).resolve().parents[1]
# Originals have a dedicated bundle root. Shared paths retain their DATA_DIR
# prefix, including optional documents/reports left by supported local workflows.
SHARED_ATTACHMENT_DIRS = (
    "attachments", "documents", "reports", "exports", "report_exports", "knowledge",
)
_EXCLUDED_DIRS = frozenset({
    "logs", "run", "tmp", "temp", "cache", "__pycache__", "node_modules",
    "secrets", "credentials",
})
_EXCLUDED_FILES = frozenset({
    "credentials.json", "secrets.json", "service-info.json", "environ", "environment",
})
_EXCLUDED_SUFFIXES = frozenset({
    ".pid", ".lock", ".log", ".sock", ".socket", ".tmp", ".pyc", ".pyo",
    ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx",
})


class CLIError(ValueError):
    """A fixed, non-sensitive operator-facing failure."""


def _data_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    # Match Runtime's server-relative DATA_DIR, without importing app.config.
    return (path if path.is_absolute() else SERVER_DIR / path).resolve()


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _excluded(part: str, *, directory: bool) -> bool:
    name = part.casefold()
    return (
        name.startswith(".")
        or name in _EXCLUDED_DIRS
        or name in _EXCLUDED_FILES
        or (not directory and (
            PurePosixPath(name).suffix in _EXCLUDED_SUFFIXES
            or name.endswith(("-wal", "-shm", "-journal"))
            or ".log." in name
        ))
    )


def _attachment_allowed(parts: tuple[str, ...]) -> bool:
    return all(not _excluded(part, directory=index < len(parts) - 1)
               for index, part in enumerate(parts))


def _copy_attachments(data: Path, shared: Path, originals: Path, max_bytes: int) -> None:
    """Copy only named attachment trees, never the whole DATA_DIR or server tree."""
    total = 0

    def copy_tree(source: Path, target: Path) -> None:
        nonlocal total
        if not source.exists() and not source.is_symlink():
            return
        if _linked(source) or not source.is_dir():
            raise CLIError("Attachment roots must be ordinary directories without links.")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in sorted(source.iterdir()):
            # Hidden configuration is excluded before reading its contents.
            if _excluded(child.name, directory=False):
                continue
            if _linked(child):
                raise CLIError("Links and reparse points are not allowed in attachment trees.")
            if child.is_dir():
                copy_tree(child, target / child.name)
            elif child.is_file():
                total += child.stat().st_size
                if total > max_bytes:
                    raise CLIError("Attachment data exceeds the configured size limit.")
                with child.open("rb") as source_stream, (target / child.name).open("xb") as output:
                    shutil.copyfileobj(source_stream, output, length=1024 * 1024)
                (target / child.name).chmod(0o600)
            else:
                raise CLIError("Only ordinary files are allowed in attachment trees.")

    shared.mkdir(mode=0o700)
    originals.mkdir(mode=0o700)
    for name in SHARED_ATTACHMENT_DIRS:
        copy_tree(data / name, shared / name)
    copy_tree(data / "material_originals", originals)


def _manager(data: Path, shared: Path, originals: Path, max_bytes: int) -> BackupManager:
    return BackupManager(
        {name: data / f"{name}.sqlite3" for name in REQUIRED_DATABASES}, shared,
        DEFAULT_DATABASE_CONTRACTS,
        additional_attachment_dirs={"material_originals": originals}, max_bytes=max_bytes,
    )


def _offline_manager(data: Path, max_bytes: int) -> BackupManager:
    # These are path guards only. inspect/restore never open these source paths.
    return _manager(data, data, data / "material_originals", max_bytes)


def _runtime_attachments(inspection: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
    """Reject bundle paths which cannot safely be assembled for this Runtime."""
    files: list[tuple[dict[str, Any], Path]] = []
    destinations: set[str] = set()
    for entry in inspection["attachments"]:
        parts = PurePosixPath(entry["path"]).parts
        root, relative = parts[1], parts[2:]
        if root == "material_originals":
            mapped = ("material_originals", *relative)
        elif root == "shared" and len(relative) >= 2 and relative[0] in SHARED_ATTACHMENT_DIRS:
            mapped = relative
        else:
            raise CLIError("Bundle attachment roots do not match the supported Runtime layout.")
        if not _attachment_allowed(mapped):
            raise CLIError("Bundle contains configuration, process data, or unsupported attachment files.")
        # Always check case-insensitively so a bundle verified on Linux also
        # assembles without overwrites on Windows.
        key = "/".join(mapped).casefold()
        if key in destinations or any(key.startswith(other + "/") or other.startswith(key + "/")
                                      for other in destinations):
            raise CLIError("Bundle has colliding Runtime attachment paths.")
        destinations.add(key)
        files.append((entry, Path(*mapped)))
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_bundle(path: Path, max_bytes: int) -> bytes:
    if path.stat().st_size > max_bytes:
        raise CLIError("The bundle exceeds the configured size limit.")
    with path.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise CLIError("The bundle exceeds the configured size limit.")
    return content


def _bundle_summary(inspection: dict[str, Any], bundle: bytes | Path) -> dict[str, Any]:
    return {
        "verified": True,
        "bundle_sha256": hashlib.sha256(bundle).hexdigest() if isinstance(bundle, bytes) else _sha256(bundle),
        "bundle_version": inspection["bundle_version"],
        "databases": {
            name: {field: inspection["databases"][name][field]
                   for field in ("user_version", "application_id", "sha256", "size")}
            for name in REQUIRED_DATABASES
        },
        "attachment_count": len(inspection["attachments"]),
        "member_count": inspection["member_count"],
    }


def create_backup(
    data_dir: str | Path, output: str | Path, *, stopped: bool,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if not stopped:
        raise CLIError("Explicit --stopped confirmation is required; it cannot prove writes have stopped.")
    data = _data_path(data_dir)
    if not data.is_dir():
        raise CLIError("The explicit DATA_DIR must already exist.")
    for name in REQUIRED_DATABASES:
        path = data / f"{name}.sqlite3"
        if not path.is_file() or _linked(path):
            raise CLIError("All six existing, ordinary SQLite database files are required.")
    raw_output = Path(output).expanduser()
    target = raw_output.parent.resolve() / raw_output.name
    if target == data or target.is_relative_to(data):
        raise CLIError("The backup destination must be outside DATA_DIR.")
    if target.exists() or target.is_symlink():
        raise CLIError("The backup destination already exists; choose a new file.")
    if not target.parent.is_dir():
        raise CLIError("The backup destination parent must already exist.")
    # Same-filesystem private workspace lets the final hard link atomically
    # publish a verified bundle without os.replace's overwrite race.
    with tempfile.TemporaryDirectory(prefix=".backup-cli-", dir=target.parent) as temporary:
        work = Path(temporary)
        shared, originals = work / "shared", work / "material_originals"
        _copy_attachments(data, shared, originals, max_bytes)
        manager = _manager(data, shared, originals, max_bytes)
        bundle = manager.write_backup(work / "bundle.zip")
        bundle.chmod(0o600)
        inspection = manager.inspect(bundle)
        _runtime_attachments(inspection)
        result = _bundle_summary(inspection, bundle)
        # FileExistsError is intentional: another process may have created the
        # target after our preflight. Never fall back to replacing that file.
        os.link(bundle, target)
    return {
        **result, "command": "backup", "bundle": str(target),
        "stopped_confirmed_by_caller": True, "stopped_verified_by_tool": False,
    }


def verify_bundle(
    bundle: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    path = Path(bundle).expanduser().resolve(strict=True)
    # No Runtime, config, or live database is opened; BackupManager only checks
    # detached temporary copies of the six archived SQLite main files.
    guard = Path(tempfile.gettempdir()) / "backup-cli-unused-live-path"
    content = _read_bundle(path, max_bytes)
    inspection = _offline_manager(guard, max_bytes).inspect(content)
    _runtime_attachments(inspection)
    result = _bundle_summary(inspection, content)
    if expected_sha256 is not None and result["bundle_sha256"] != expected_sha256.lower():
        raise CLIError("The bundle does not match the supplied SHA256.")
    return {**result, "command": "verify"}


def _recovery_review(data: Path) -> dict[str, Any]:
    """Count persisted gates without exposing payloads or changing restored facts."""
    with closing(sqlite3.connect((data / "jobs.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        jobs = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        calls = dict(db.execute("SELECT status,COUNT(*) FROM upstream_calls GROUP BY status"))
        counts = db.execute("""SELECT
            COALESCE(SUM(reconciliation_required),0), COALESCE(SUM(cost_pending),0),
            COALESCE(SUM(cancel_requested),0),
            COALESCE(SUM(lease_owner IS NOT NULL OR lease_until IS NOT NULL),0)
            FROM jobs""").fetchone()
        pending = db.execute("SELECT COUNT(*) FROM upstream_calls WHERE cost_pending=1").fetchone()[0]
    return {
        "workers_must_remain_disabled": True, "provider_access_must_remain_disabled": True,
        "jobs_by_status": jobs, "calls_by_status": calls,
        "jobs_reconciliation_required": counts[0], "jobs_cost_pending": counts[1],
        "jobs_cancel_requested": counts[2], "jobs_with_persisted_leases": counts[3],
        "calls_cost_pending": pending,
        "required_reviews": [
            "sessions_and_draft_leases", "outbox_and_hint_checkpoints",
            "upstream_results_and_costs", "explicit_confirmation_before_paid_recovery",
        ],
    }


def restore_bundle(
    bundle: str | Path, staging_parent: str | Path, data_dir: str | Path, *, stopped: bool,
    max_bytes: int = DEFAULT_MAX_BYTES, expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not stopped:
        raise CLIError("Explicit --stopped confirmation is required; it cannot prove writes have stopped.")
    live = _data_path(data_dir)
    parent = Path(staging_parent).expanduser().resolve(strict=True)
    if not parent.is_dir() or parent == live or parent.is_relative_to(live):
        raise CLIError("The existing staging parent must be outside the live DATA_DIR.")
    path = Path(bundle).expanduser().resolve(strict=True)
    manager = _offline_manager(live, max_bytes)
    content = _read_bundle(path, max_bytes)
    inspection = manager.inspect(content)
    attachments = _runtime_attachments(inspection)
    result = _bundle_summary(inspection, content)
    if expected_sha256 is not None and result["bundle_sha256"] != expected_sha256.lower():
        raise CLIError("The bundle does not match the supplied SHA256.")
    restored = manager.restore_to_staging(content, parent)
    try:
        runtime_data = restored / "data"
        (restored / "databases").rename(runtime_data)
        for entry, relative in attachments:
            source, target = restored / entry["path"], runtime_data / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Every destination is new; inspection already rejected path aliases.
            with source.open("rb") as input_stream, target.open("xb") as output:
                shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            target.chmod(0o600)
            if target.stat().st_size != entry["size"] or _sha256(target) != entry["sha256"]:
                raise CLIError("Runtime attachment assembly failed checksum validation.")
        (runtime_data / "material_originals").mkdir(exist_ok=True, mode=0o700)
        for name in REQUIRED_DATABASES:
            target = runtime_data / f"{name}.sqlite3"
            target.chmod(0o600)
            if _sha256(target) != inspection["databases"][name]["sha256"]:
                raise CLIError("Runtime database assembly failed checksum validation.")
        review = _recovery_review(runtime_data)
        if (restored / "attachments").exists():
            shutil.rmtree(restored / "attachments")
        return {
            **result, "command": "restore", "restore_dir": str(restored),
            "data_dir": str(runtime_data), "ready_for_production": False,
            "stopped_confirmed_by_caller": True, "stopped_verified_by_tool": False,
            "recovery_review": review,
        }
    except BaseException:
        shutil.rmtree(restored, ignore_errors=True)
        raise


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _digest_argument(value: str) -> str:
    if len(value) != 64 or any(letter not in "0123456789abcdefABCDEF" for letter in value):
        raise argparse.ArgumentTypeError("must be a 64-character SHA256 hex digest")
    return value.lower()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Six-store stopped-write backup, offline verification, and isolated recovery.",
        epilog="--stopped is the caller's attestation, not proof. Stop API, workers and all other writers first. "
               "No command loads .env, activates restored data, or calls a provider.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Snapshot six databases and allowlisted attachments to a NEW bundle")
    backup.add_argument("--data-dir", required=True, type=Path, help="Explicit Runtime DATA_DIR; relative paths use server/")
    backup.add_argument("--output", required=True, type=Path, help="New backup file outside DATA_DIR; parent must exist")
    backup.add_argument("--stopped", required=True, action="store_true", help="Attest all writers have stopped; cannot prove it")
    verify = commands.add_parser("verify", help="Verify only the bundle, without opening any live database")
    verify.add_argument("--bundle", required=True, type=Path)
    restore = commands.add_parser("restore", help="Recover only into a NEW restore-*/data directory; never activate")
    restore.add_argument("--bundle", required=True, type=Path)
    restore.add_argument("--data-dir", required=True, type=Path, help="Live DATA_DIR to protect; source files are not opened")
    restore.add_argument("--staging-parent", required=True, type=Path, help="Existing directory outside the live DATA_DIR")
    restore.add_argument("--stopped", required=True, action="store_true", help="Attest the live writers have stopped")
    for command in (backup, verify, restore):
        command.add_argument("--max-bytes", type=_positive_integer, default=DEFAULT_MAX_BYTES,
                             help=f"Compressed AND expanded bundle size ceiling (default {DEFAULT_MAX_BYTES})")
    for command in (verify, restore):
        command.add_argument("--sha256", type=_digest_argument, help="Optional trusted whole-bundle SHA256 to compare")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.data_dir, args.output, stopped=args.stopped, max_bytes=args.max_bytes)
        elif args.command == "verify":
            result = verify_bundle(args.bundle, max_bytes=args.max_bytes, expected_sha256=args.sha256)
        else:
            result = restore_bundle(args.bundle, args.staging_parent, args.data_dir, stopped=args.stopped,
                                    max_bytes=args.max_bytes, expected_sha256=args.sha256)
    except CLIError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except FileExistsError:
        print(json.dumps({"ok": False, "error": "A destination already exists; choose a new path."}), file=sys.stderr)
        return 2
    except (BackupError, OSError, ValueError, RuntimeError, sqlite3.Error):
        # Core errors can contain untrusted manifest keys or filesystem details.
        # Never echo them or print a traceback containing credentials/payloads.
        print(json.dumps({"ok": False, "error": "Backup operation failed; check paths, bundle integrity, schema contracts and size limits."}),
              file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
