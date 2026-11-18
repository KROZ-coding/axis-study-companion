"""CLI recovery checks against six real temporary Runtime databases, offline only."""

from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

os.environ["SKIP_ENV_FILE"] = "1"

from app import backup_cli
from app.backup import (
    BackupError, BackupManager, DEFAULT_DATABASE_CONTRACTS, LEGACY_CONTENT_CONTRACT,
    REQUIRED_DATABASES,
)
from app.content_store import ContentStore
from app.services.competition_runs import CompetitionRunStore
from app.job_queue import InvalidTransition, ReconciliationRequired
from app.runtime import Runtime
from app.services.report_exports import build_report_snapshot


class NoNetworkGateway:
    def describe(self, *args, **kwargs):
        raise AssertionError("backup/recovery must not select a model")

    async def complete(self, *args, **kwargs):
        raise AssertionError("backup/recovery must not call a model")


class BackupCLITests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.getaddrinfo", "socket.create_connection", "socket.socket.connect",
                       "httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory(prefix="backup-cli-regression-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live = self.root / "live"
        self.runtime = self._runtime(self.live)
        self.parent = self.root / "restores"
        self.parent.mkdir()
        self.bundle = self.root / "backup.zip"
        self.runtime.platform.create_user("teacher", "Teacher", "fixture-hash", "teacher", user_id="teacher")
        self.runtime.platform.create_user("student", "Student", "fixture-hash", "student", user_id="student")
        self.runtime.platform.create_course("Course", "2026", "teacher", course_id="course")
        self.runtime.platform.create_class("course", "Class", owner_teacher_id="teacher", class_id="class")
        self.runtime.platform.create_course_membership("course", "student")
        self.runtime.platform.create_class_membership("class", "student")
        self.goal = self.runtime.learning.create_goal("teacher", "Goal", course_id="course", class_id="class")
        self.material = self.runtime.content.import_material(
            course_id="course", owner_id="teacher", filename="note.txt", content=b"lesson-bytes",
            parsed={"status": "parsed", "format": "txt", "blocks": [
                {"anchor": "line:1", "kind": "text", "text": "lesson"}], "issues": []},
        )
        scope = {"kind": "goal", "goal_id": self.goal["id"], "class_id": "class", "course_id": "course"}
        canonical = self.runtime.learning.report_input(scope=scope, actor_id="teacher")
        self.report = self.runtime.reports.create_snapshot(
            scope, canonical["source_revision"], build_report_snapshot(canonical, scope), created_by="teacher")
        self.conversation = self.runtime.companion.create_conversation("student", "Study", request_id="conversation")
        self._write("documents/handout.txt", b"document")
        self._write("reports/export.json", b"{\"report\":true}")
        self._write("attachments/course/image.bin", b"image")
        # Sentinels stay strictly inside the disposable test directory.
        self._write(".env", b"SECRET_SENTINEL_DO_NOT_COPY")
        self._write("worker.pid", b"process")
        self._write("logs/server.log", b"process-log")
        self._write("reports/.env", b"SECRET_SENTINEL_DO_NOT_COPY")
        self._write("reports/worker.pid", b"process")
        self._write("reports/logs/runtime.log", b"process-log")
        self._write("reports/service-info.json", b"SECRET_SENTINEL_DO_NOT_COPY")
        self._write("reports/extra.sqlite3", b"not-an-attachment")

    def _runtime(self, root):
        runtime = Runtime(SimpleNamespace(data_dir=str(root), worker_enabled=False))
        runtime.install("gateway", NoNetworkGateway())
        runtime.initialize()
        self.addCleanup(runtime.stop)
        return runtime

    def _write(self, relative, content):
        path = self.live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _backup(self):
        self.runtime.stop()
        return backup_cli.create_backup(self.live, self.bundle, stopped=True)

    def _rewrite_bundle(self, *, tamper_attachment=False, extra=None):
        content = BytesIO()
        with ZipFile(self.bundle) as source, ZipFile(content, "w", ZIP_DEFLATED) as target:
            manifest = json.loads(source.read("manifest.json"))
            if extra is not None:
                name, payload = extra
                manifest["attachments"].append({"path": name, "size": len(payload),
                                                "sha256": hashlib.sha256(payload).hexdigest()})
                target.writestr(name, payload)
            for member in source.infolist():
                payload = source.read(member.filename)
                if member.filename == "manifest.json":
                    payload = json.dumps(manifest).encode()
                elif tamper_attachment and member.filename == "attachments/shared/documents/handout.txt":
                    payload = b"tampered"
                target.writestr(member.filename, payload)
        self.bundle.write_bytes(content.getvalue())

    def test_real_six_store_roundtrip_assembles_runtime_layout_and_filters_process_data(self):
        before = {name: (self.live / f"{name}.sqlite3").read_bytes() for name in REQUIRED_DATABASES}
        created = self._backup()
        self.assertTrue(created["verified"])
        self.assertEqual(set(created["databases"]), set(REQUIRED_DATABASES))
        self.assertFalse(created["stopped_verified_by_tool"])
        verified = backup_cli.verify_bundle(self.bundle, expected_sha256=created["bundle_sha256"])
        self.assertEqual(verified["bundle_sha256"], created["bundle_sha256"])
        with ZipFile(self.bundle) as archive:
            names = archive.namelist()
            self.assertIn("attachments/shared/documents/handout.txt", names)
            self.assertIn("attachments/shared/reports/export.json", names)
            self.assertTrue(any(name.startswith("attachments/material_originals/upload_") for name in names))
            self.assertFalse(any(".env" in name or ".pid" in name or "/logs/" in name
                                 or "service-info" in name or "extra.sqlite3" in name for name in names))
            self.assertNotIn("attachments/shared/platform.sqlite3", names)
            self.assertFalse(any(b"SECRET_SENTINEL" in archive.read(name) for name in names))
        restored = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        data = Path(restored["data_dir"])
        self.assertEqual(data.parent.parent, self.parent.resolve())
        self.assertFalse(restored["ready_for_production"])
        self.assertTrue(restored["recovery_review"]["workers_must_remain_disabled"])
        self.assertTrue(restored["recovery_review"]["provider_access_must_remain_disabled"])
        self.assertEqual((data / "documents/handout.txt").read_bytes(), b"document")
        self.assertEqual((data / "reports/export.json").read_bytes(), b'{"report":true}')
        self.assertEqual((data / "attachments/course/image.bin").read_bytes(), b"image")
        self.assertFalse(any(path.name.endswith(("-wal", "-shm", "-journal")) for path in data.rglob("*")))
        for name in REQUIRED_DATABASES:
            target = data / f"{name}.sqlite3"
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), created["databases"][name]["sha256"])
        recovered = self._runtime(data)
        recovered.start()
        self.assertIsNone(recovered._executor)
        self.assertEqual(recovered.platform.get_user("student"), self.runtime.platform.get_user("student"))
        self.assertEqual(recovered.learning.get_goal(self.goal["id"], "teacher"),
                         self.runtime.learning.get_goal(self.goal["id"], "teacher"))
        self.assertEqual(recovered.content.original_bytes(self.material["id"]), b"lesson-bytes")
        self.assertEqual(recovered.reports.get_snapshot(self.report["id"]), self.report)
        self.assertEqual(recovered.companion.get_conversation(self.conversation["id"], "student"), self.conversation)
        self.assertEqual({name: (self.live / f"{name}.sqlite3").read_bytes() for name in REQUIRED_DATABASES}, before)

    def test_legacy_v1_six_database_bundle_restores_before_explicit_content_migration(self):
        self.runtime.stop()
        legacy_content = self.root / "content-v1.sqlite3"
        with closing(sqlite3.connect(legacy_content)) as db:
            ContentStore._migration_1(db)
            db.execute("PRAGMA user_version=1")
            db.commit()
        shared = self.root / "legacy-shared"
        shared.mkdir()
        paths = {name: self.live / f"{name}.sqlite3" for name in REQUIRED_DATABASES}
        paths["content"] = legacy_content
        manager = BackupManager(paths, shared, {
            **DEFAULT_DATABASE_CONTRACTS, "content": LEGACY_CONTENT_CONTRACT,
        })
        # Use the original manifest layout/version with a genuine pre-migration
        # content schema, rather than only relabelling a v2 database.
        output = BytesIO()
        with ZipFile(BytesIO(manager.export_bytes())) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
            for member in source.infolist():
                payload = source.read(member.filename)
                if member.filename == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["bundle_version"] = 1
                    payload = json.dumps(manifest).encode()
                target.writestr(member.filename, payload)
        self.bundle.write_bytes(output.getvalue())
        with patch.object(Runtime, "initialize", side_effect=AssertionError("restore must not start Runtime")), \
                patch.object(ContentStore, "_initialize", side_effect=AssertionError("restore must not migrate")):
            verified = backup_cli.verify_bundle(self.bundle)
            restored = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        self.assertEqual(verified["bundle_version"], 1)
        self.assertEqual(set(verified["databases"]), set(REQUIRED_DATABASES))
        data = Path(restored["data_dir"])
        content = data / "content.sqlite3"
        self.assertEqual(backup_cli._sha256(content), verified["databases"]["content"]["sha256"])
        self.assertFalse((data / "competition.sqlite3").exists())
        with closing(sqlite3.connect(content.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='material_commands'").fetchone())
        # Migration only occurs on the operator's later, explicit store startup.
        reopened = ContentStore(content)
        self.assertEqual(reopened.schema_version, 2)
        with reopened.connect() as db:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='material_commands'").fetchone())
        with closing(sqlite3.connect(legacy_content)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_content_v2_and_optional_competition_roundtrip_preserve_rows_and_hashes(self):
        competition = self.live / "competition.sqlite3"
        CompetitionRunStore(competition)
        source_hash = hashlib.sha256(b"competition-input").hexdigest()
        inputs = json.dumps({"source_sha256": source_hash, "source_record": "fixture.csv:2"})
        with closing(sqlite3.connect(competition)) as db:
            db.execute("INSERT INTO competition_runs(id,course_id,created_by,title,status,revision,preview_json,inputs_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       ("fixture-run", "course", "teacher", "Fixture", "preview", 1, "{}", inputs, "2026-11-17"))
            db.commit()
        with self.runtime.content.connect() as db:
            db.execute("INSERT INTO material_commands VALUES(?,?,?,?,?,?,?)",
                       ("teacher", "fixture-command", self.material["id"], "lifecycle", "{}", "{}", 1.0))
            db.commit()
        created = self._backup()
        self.assertEqual(created["bundle_version"], 2)
        self.assertEqual(set(created["databases"]), {*REQUIRED_DATABASES, "competition"})
        self.assertEqual(created["databases"]["content"]["user_version"], 2)
        self.assertEqual(created["databases"]["competition"]["user_version"], 1)
        # Offline verification/restore do not depend on any live competition file.
        protected = self.root / "nonexistent-live"
        with patch.object(Runtime, "initialize", side_effect=AssertionError("no Runtime startup")):
            verified = backup_cli.verify_bundle(self.bundle)
            restored = backup_cli.restore_bundle(self.bundle, self.parent, protected, stopped=True)
        self.assertEqual(verified["databases"], created["databases"])
        data = Path(restored["data_dir"])
        for name, entry in created["databases"].items():
            self.assertEqual(backup_cli._sha256(data / f"{name}.sqlite3"), entry["sha256"])
        with closing(sqlite3.connect((data / "competition.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            self.assertEqual(db.execute("SELECT inputs_json FROM competition_runs WHERE id='fixture-run'").fetchone()[0], inputs)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        with closing(sqlite3.connect((data / "content.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            self.assertEqual(db.execute("SELECT actor_id,request_id,file_id FROM material_commands").fetchall(),
                             [("teacher", "fixture-command", self.material["id"])])
        self.assertFalse(any(path.name.endswith(("-wal", "-shm", "-journal")) for path in data.rglob("*")))

    def test_optional_competition_rejects_wrong_version_application_id_and_table_set(self):
        competition = self.live / "competition.sqlite3"
        for mutation in ("version", "application_id", "extra_table", "missing_table"):
            with self.subTest(mutation=mutation):
                CompetitionRunStore(competition)
                with closing(sqlite3.connect(competition)) as db:
                    if mutation == "version":
                        db.execute("PRAGMA user_version=2")
                    elif mutation == "application_id":
                        db.execute("PRAGMA application_id=1")
                    elif mutation == "extra_table":
                        db.execute("CREATE TABLE unexpected(value TEXT)")
                    else:
                        db.execute("DROP TABLE competition_runs")
                    db.commit()
                before = competition.read_bytes()
                with self.assertRaises(BackupError):
                    self._backup()
                self.assertFalse(self.bundle.exists())
                self.assertEqual(competition.read_bytes(), before)
                competition.unlink()

    def test_stopped_confirmation_is_required_before_any_backup_or_restore_io(self):
        with patch.object(BackupManager, "write_backup", side_effect=AssertionError("must not open stores")):
            with self.assertRaisesRegex(backup_cli.CLIError, "--stopped"):
                backup_cli.create_backup(self.live, self.bundle, stopped=False)
        with self.assertRaisesRegex(backup_cli.CLIError, "--stopped"):
            backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=False)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_existing_or_racing_backup_target_is_never_overwritten(self):
        self.bundle.write_bytes(b"existing-backup")
        with self.assertRaisesRegex(backup_cli.CLIError, "already exists"):
            self._backup()
        self.assertEqual(self.bundle.read_bytes(), b"existing-backup")
        self.bundle.unlink()
        real_link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b"concurrent-backup")
            return real_link(source, destination)

        with patch.object(backup_cli.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            self._backup()
        self.assertEqual(self.bundle.read_bytes(), b"concurrent-backup")
        self.assertFalse(any(path.name.startswith(".backup-cli-") for path in self.root.iterdir()))

    def test_verify_and_restore_do_not_open_live_stores(self):
        self._backup()
        source_live = self.live
        backup_cli_root = self.root
        real_connect = sqlite3.connect

        def detached_connect(database, *args, **kwargs):
            location = str(database)
            self.assertNotIn(source_live.as_uri(), location)
            self.assertNotIn(str(source_live), location)
            self.assertNotIn("backup-cli-unused-live-path", location)
            self.assertIn("immutable=1", location)
            self.assertTrue(location.startswith("file:"))
            return real_connect(database, *args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=detached_connect), \
                patch.object(Runtime, "initialize", side_effect=AssertionError("no live Runtime")):
            self.assertTrue(backup_cli.verify_bundle(self.bundle)["verified"])
            # Protected live directory need not exist; it is never read.
            result = backup_cli.restore_bundle(self.bundle, self.parent,
                                               backup_cli_root / "nonexistent-live", stopped=True)
        self.assertTrue(Path(result["data_dir"]).is_dir())

    def test_restore_rejects_live_subdirectories_and_repeated_restores_use_new_directories(self):
        self._backup()
        inside = self.live / "nested"
        inside.mkdir()
        for parent in (self.live, inside):
            with self.subTest(parent=parent), self.assertRaises(backup_cli.CLIError):
                backup_cli.restore_bundle(self.bundle, parent, self.live, stopped=True)
        one = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        two = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        self.assertNotEqual(one["data_dir"], two["data_dir"])
        self.assertTrue(Path(one["data_dir"]).is_dir())
        with self.assertRaises(backup_cli.CLIError):
            backup_cli.create_backup(self.live, self.live / "bad.zip", stopped=True)

    def test_corruption_hash_and_unsupported_layout_fail_without_publishing_restore(self):
        self._backup()
        with self.assertRaisesRegex(backup_cli.CLIError, "SHA256"):
            backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True, expected_sha256="0" * 64)
        self._rewrite_bundle(tamper_attachment=True)
        for operation in (lambda: backup_cli.verify_bundle(self.bundle),
                          lambda: backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(list(self.parent.iterdir()), [])
        self.bundle.unlink()
        self._backup()
        self._rewrite_bundle(extra=("attachments/shared/.env", b"SECRET_SENTINEL_DO_NOT_COPY"))
        with self.assertRaises(backup_cli.CLIError):
            backup_cli.verify_bundle(self.bundle)
        with self.assertRaises(backup_cli.CLIError):
            backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_paid_unknown_call_cost_and_confirmation_gates_survive_recovery(self):
        queue = self.runtime.queue
        queue.fund_budget("fixture-wallet", 100)
        queue.enqueue("interaction", {"fixture": True}, steps=("answer",), job_id="unknown-paid",
                      paid=True, budget_account_id="fixture-wallet", budget_limit=20, available_at=1)
        claimed = queue.claim("old-worker", lease_seconds=5, now=2)
        queue.start_step(claimed.job_id, "answer", "old-worker", claimed.generation, now=2)
        queue.start_call(claimed.job_id, "original-call", "old-worker", claimed.generation,
                         step_name="answer", reserve_units=10, now=3)
        queue.mark_call_unknown(claimed.job_id, "original-call", "old-worker", claimed.generation, now=4)
        original_job, original_call = queue.get_job(claimed.job_id), queue.get_call("original-call")
        original_wallet = queue.budget("fixture-wallet")
        self._backup()
        restored = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
        review = restored["recovery_review"]
        self.assertEqual(review["jobs_by_status"], {"reconciling": 1})
        self.assertEqual(review["calls_by_status"], {"unknown": 1})
        self.assertEqual((review["jobs_reconciliation_required"], review["jobs_cost_pending"],
                          review["calls_cost_pending"]), (1, 1, 1))
        recovered = self._runtime(Path(restored["data_dir"]))
        new_queue = recovered.queue
        self.assertEqual(new_queue.get_job(claimed.job_id), original_job)
        self.assertEqual(new_queue.get_call("original-call"), original_call)
        self.assertEqual(new_queue.budget("fixture-wallet"), original_wallet)
        events = new_queue.list_events(claimed.job_id)
        self.assertIsNone(new_queue.claim("new-worker", now=10))
        with self.assertRaises(InvalidTransition):
            new_queue.confirm_recovery(claimed.job_id, approved=True, now=10)
        with self.assertRaises(InvalidTransition):
            new_queue.retry(claimed.job_id, confirm_paid=True, now=10)
        self.assertEqual(new_queue.list_events(claimed.job_id), events)
        reconciled = new_queue.reconcile_call(claimed.job_id, "original-call", "succeeded",
                                              result={"answer": "persisted"}, cost_pending=True, now=11)
        self.assertEqual(reconciled.status, "awaiting_confirmation")
        with self.assertRaises(ReconciliationRequired):
            new_queue.confirm_recovery(claimed.job_id, approved=True, now=12)
        new_queue.settle_call_cost("original-call", 3, now=13)
        self.assertEqual(new_queue.get_job(claimed.job_id).status, "awaiting_confirmation")
        self.assertIsNone(new_queue.claim("new-worker", now=14))
        new_queue.confirm_recovery(claimed.job_id, approved=True, now=15)
        new_claim = new_queue.claim("new-worker", now=16)
        new_queue.start_step(claimed.job_id, "answer", "new-worker", new_claim.generation, now=16)
        intent = new_queue.start_call(claimed.job_id, "original-call", "new-worker", new_claim.generation,
                                      step_name="answer", reserve_units=10, now=17)
        self.assertFalse(intent.should_send)
        self.assertEqual(intent.result, {"answer": "persisted"})
        self.assertEqual(len(new_queue.list_calls(claimed.job_id)), 1)
        self.assertEqual(new_queue.call_costs(claimed.job_id), {"original-call": 3})
        # Reconciliation was performed only in the isolated copy.
        self.assertEqual(queue.get_job(claimed.job_id), original_job)
        self.assertEqual(queue.get_call("original-call"), original_call)

    def test_wal_commits_survive_cli_and_restored_files_have_no_sidecars(self):
        with ExitStack() as connections:
            db = connections.enter_context(closing(sqlite3.connect(self.live / "platform.sqlite3")))
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = (self.live / "platform.sqlite3").read_bytes()
            db.execute("UPDATE users SET display_name='WAL-only Student' WHERE id='student'")
            db.commit()
            self.assertEqual((self.live / "platform.sqlite3").read_bytes(), before)
            self.assertGreater((self.live / "platform.sqlite3-wal").stat().st_size, 32)
            self._backup()
            restored = backup_cli.restore_bundle(self.bundle, self.parent, self.live, stopped=True)
            data = Path(restored["data_dir"])
            with closing(sqlite3.connect((data / "platform.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
                self.assertEqual(copy.execute("SELECT display_name FROM users WHERE id='student'").fetchone()[0],
                                 "WAL-only Student")
            self.assertFalse(any(path.name.endswith(("-wal", "-shm", "-journal")) for path in data.rglob("*")))
            self.assertGreater((self.live / "platform.sqlite3-wal").stat().st_size, 32)

    def test_module_entrypoint_and_help_do_not_import_config_or_load_dotenv(self):
        # A fresh isolated interpreter installs an import blocker, then invokes
        # the actual module entrypoint with malformed inherited model config.
        bootstrap = """
import importlib.abc, runpy, sys
sys.path.insert(0, sys.argv.pop(1))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'app.config', 'app.runtime', 'app.main'}:
            raise AssertionError('CLI imported application configuration')
sys.meta_path.insert(0, Block())
sys.argv = ['app.backup_cli', *sys.argv[1:]]
runpy.run_module('app.backup_cli', run_name='__main__')
"""
        self._backup()
        env = dict(os.environ, LLM_PROVIDERS_JSON="invalid-never-load", DATA_DIR="never-use-this")
        env.pop("SKIP_ENV_FILE", None)
        for arguments in (("--help",), ("verify", "--bundle", str(self.bundle))):
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", bootstrap,
                 str(Path(backup_cli.__file__).resolve().parents[1]), *arguments],
                cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["command"], "verify")

    def test_cli_errors_are_sanitized_and_return_nonzero(self):
        output, errors = StringIO(), StringIO()
        with patch.object(backup_cli, "verify_bundle", side_effect=ValueError("SECRET_SENTINEL")), \
                redirect_stdout(output), redirect_stderr(errors):
            code = backup_cli.main(["verify", "--bundle", str(self.bundle)])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("SECRET_SENTINEL", errors.getvalue())
        self.assertFalse(json.loads(errors.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
