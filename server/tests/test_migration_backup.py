from contextlib import ExitStack, closing
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

from app.backup import (
    BackupError,
    BackupManager,
    DatabaseContract,
    DEFAULT_DATABASE_CONTRACTS,
    REQUIRED_DATABASES,
    application_id_from_text,
)
from app.migration import (
    MigrationError,
    decode_legacy_archive,
    decode_legacy_json,
    decode_legacy_sqlite,
    dry_run_migration,
    require_explicit_mappings,
)
from app.learning_store import LearningStore
from app.runtime import Runtime


class FakeGateway:
    """Backup and restore never need an upstream call."""

    def describe(self, *args, **kwargs):
        raise AssertionError("backup tests must not select an upstream model")

    async def complete(self, *args, **kwargs):
        raise AssertionError("backup tests must not call an upstream model")


PLANNING_TABLES = frozenset({"teacher_plan_drafts", "teacher_plan_previews", "teacher_next_round_drafts"})


_USER_VERSION_PRAGMAS = {
    1: "PRAGMA user_version=1",
    2: "PRAGMA user_version=2",
}
_APPLICATION_ID_PRAGMAS = {
    0: "PRAGMA application_id=0",
    application_id_from_text("AIQ2"): "PRAGMA application_id=0x41495132",
    application_id_from_text("AXCP"): "PRAGMA application_id=0x41584350",
    application_id_from_text("ZZZZ"): "PRAGMA application_id=0x5A5A5A5A",
}


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="migration-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _legacy_state(self) -> dict:
        return {
            "version": 2,
            "users": {"u1": {"id": "u1", "role": "student"}},
            "courses": {"c1": {"id": "c1"}},
            "enrollments": [["c1", "u1", "A"], ["missing", "ghost", "B"]],
            "questions": {"q1": {"id": "q1", "stem": "old"}},
            "quiz_sessions": {
                "s1": {"user_id": "u1", "course_id": "c1", "questions": [{"id": "q1"}]},
                "s2": {"user_id": "ghost", "course_id": "c1", "questions": [{"id": "absent"}]},
            },
            "results": {"r1": {"session_id": "s1"}, "orphan-result": {"session_id": "missing-session"}},
            "mastery": [["u1", "c1", "topic", {"mastery": 0.8}]],
            "documents": {"doc1": {"file_key": "documents/doc1.pdf"}},
        }

    def _write_legacy_sqlite(self, state: dict) -> Path:
        path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE store (id INTEGER PRIMARY KEY CHECK(id=1), blob BLOB NOT NULL)")
            connection.execute("INSERT INTO store VALUES (1, ?)", (zlib.compress(json.dumps(state).encode("utf-8")),))
            connection.commit()
        finally:
            connection.close()
        return path

    def test_readonly_sqlite_decodes_old_zlib_json_and_preserves_source_bytes(self) -> None:
        path = self._write_legacy_sqlite(self._legacy_state())
        before = path.read_bytes()
        snapshot = decode_legacy_sqlite(path)
        self.assertEqual(snapshot.source_format, "sqlite-zlib-json")
        self.assertEqual(snapshot.state["users"]["u1"]["id"], "u1")
        with closing(sqlite3.connect(path)) as connection:
            compressed = connection.execute("SELECT blob FROM store WHERE id=1").fetchone()[0]
        self.assertEqual(snapshot.source_sha256, hashlib.sha256(compressed).hexdigest())
        self.assertEqual(path.read_bytes(), before)

    def test_sqlite_unknown_version_or_nonlegacy_schema_fails_closed_without_writes(self) -> None:
        path = self._write_legacy_sqlite(self._legacy_state())
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=7")
        altered = path.read_bytes()
        with self.assertRaises(MigrationError):
            decode_legacy_sqlite(path)
        self.assertEqual(path.read_bytes(), altered)

    def test_legacy_json_and_archive_decode_without_store_instantiation(self) -> None:
        raw = json.dumps(self._legacy_state()).encode("utf-8")
        snapshot = decode_legacy_json(raw)
        self.assertEqual(snapshot.source_format, "json")
        self.assertEqual(snapshot.source_sha256, hashlib.sha256(raw).hexdigest())
        json_path = self.root / "store.json"
        json_path.write_bytes(raw)
        self.assertEqual(decode_legacy_json(json_path).state["version"], 2)
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("store.json", raw)
            archive.writestr("files/documents/doc1.pdf", b"pdf-bytes")
        archive_snapshot = decode_legacy_archive(output.getvalue())
        self.assertEqual(archive_snapshot.attachments, {"documents/doc1.pdf": b"pdf-bytes"})

    def test_legacy_archive_rejects_traversal_and_duplicate_files(self) -> None:
        state = json.dumps(self._legacy_state()).encode()
        malicious = BytesIO()
        with ZipFile(malicious, "w", ZIP_DEFLATED) as archive:
            archive.writestr("store.json", state)
            archive.writestr("files/../../escape", b"bad")
        with self.assertRaises(MigrationError):
            decode_legacy_archive(malicious.getvalue())

        duplicate = BytesIO()
        with ZipFile(duplicate, "w", ZIP_DEFLATED) as archive:
            archive.writestr("store.json", state)
            archive.writestr("files/a", b"one")
            archive.writestr("files/a", b"two")
        with self.assertRaises(MigrationError):
            decode_legacy_archive(duplicate.getvalue())

        drive_path = BytesIO()
        with ZipFile(drive_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("store.json", state)
            archive.writestr("files/C:/escape", b"bad")
        with self.assertRaises(MigrationError):
            decode_legacy_archive(drive_path.getvalue())

    def test_dry_run_reports_mappings_orphans_and_raw_quarantine(self) -> None:
        snapshot = decode_legacy_sqlite(self._write_legacy_sqlite(self._legacy_state()))
        report = dry_run_migration(snapshot)
        self.assertFalse(report["import_enabled"])
        self.assertEqual(report["statistics"]["orphan_count"], 5)
        self.assertTrue(report["missing_mappings"])
        self.assertTrue(any(entry["reason"] == "explicit_target_mapping_required" for entry in report["quarantine"]))
        self.assertTrue(any(entry["collection"] == "documents" and entry["raw"] == self._legacy_state()["documents"]
                            for entry in report["quarantine"]))
        with self.assertRaises(MigrationError):
            require_explicit_mappings(report, platform={}, learning={})

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            decode_legacy_json(b'{"version":2,"version":3}')
        with self.assertRaises(MigrationError):
            decode_legacy_json(b'{"version":2,"value":NaN}')

    def test_unknown_snapshot_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(MigrationError, "unsupported legacy snapshot version"):
            from app.migration import _decode_state
            _decode_state(b'{"version":999}', "test", "hash")


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
                       "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("external network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "live"
        self.live.mkdir()
        self.attachments = self.live / "attachments"
        self.attachments.mkdir()
        self.contracts = {
            name: DatabaseContract(
                user_version=DEFAULT_DATABASE_CONTRACTS[name].user_version,
                application_id=(
                    application_id_from_text("AXCP") if name == "companion"
                    else application_id_from_text("AIQ2") if name == "jobs"
                    else 0
                ),
                tables=frozenset({"marker"}),
            ) for name in REQUIRED_DATABASES
        }
        self.database_paths = {}
        for name, contract in self.contracts.items():
            path = self.live / f"{name}.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO marker(value) VALUES (?)", (f"{name}-data",))
                connection.execute(_USER_VERSION_PRAGMAS[contract.user_version])
                connection.execute(_APPLICATION_ID_PRAGMAS[contract.application_id])
                connection.commit()
            finally:
                connection.close()
            self.database_paths[name] = path
        (self.attachments / "course" / "handout.bin").parent.mkdir(parents=True)
        (self.attachments / "course" / "handout.bin").write_bytes(b"attachment-payload")
        self.additional = self.live / "material_originals"
        self.additional.mkdir()
        (self.additional / "upload_x").mkdir()
        (self.additional / "upload_x" / "original.bin").write_bytes(b"private-original")
        # Shared-root traversal must exclude, not flatten, the private sub-tree.
        (self.live / "unrelated.txt").write_bytes(b"shared-data-dir-file")
        self.manager = BackupManager(self.database_paths, self.live, self.contracts,
                                    additional_attachment_dirs={"material_originals": self.additional})

    def test_default_bundle_contracts_match_all_six_runtime_domains(self) -> None:
        self.assertEqual(set(DEFAULT_DATABASE_CONTRACTS), set(REQUIRED_DATABASES))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["platform"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["learning"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["learning"].tables,
                         frozenset({"learning_goals", "goal_revisions", "learning_rounds", "question_versions",
                                    "learning_tasks", "task_items", "draft_answers", "attempts", "attempt_items",
                                    "grading_outbox", "idempotency", "task_events", "deadline_extensions",
                                    "grading_corrections", "report_revisions", "learning_revision", "goal_commands",
                                    "round_policies", "task_policies", "teacher_judgments", "diagnostic_sessions",
                                    "diagnostic_items", "question_family_index", "question_metadata", "learning_outbox", "appeals", "goal_plans",
                                    "teacher_plan_drafts", "teacher_plan_previews", "teacher_next_round_drafts"}))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].application_id, application_id_from_text("AIQ2"))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].tables,
                         frozenset({"queue_meta", "budgets", "jobs", "steps", "events", "upstream_calls",
                                    "call_charges", "funding_transactions", "budget_limits",
                                    "call_budget_reservations", "budget_ledger_entries"}))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["content"].user_version, 2)
        self.assertIn("material_commands", DEFAULT_DATABASE_CONTRACTS["content"].tables)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["reports"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["reports"].tables,
                         frozenset({"reports", "report_snapshots", "report_revisions", "report_idempotency",
                                    "teaching_insights", "teaching_insight_outbox"}))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["companion"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["companion"].application_id, application_id_from_text("AXCP"))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["companion"].tables,
                         frozenset({"conversations", "messages", "responses", "response_outbox", "events", "collections",
                                    "collection_items", "notes", "memories", "discussion_threads", "discussion_messages",
                                    "notifications", "retention_policies", "legal_holds", "audit_events", "hint_checkpoints"}))

    def _runtime(self, root):
        settings = type("RuntimeSettings", (), {"data_dir": str(root), "worker_enabled": False})()
        runtime = Runtime(settings)
        runtime.install("gateway", FakeGateway())
        self.addCleanup(runtime.stop)
        runtime.initialize()
        return runtime

    def _assert_runtime_contracts(self, root):
        for name, contract in DEFAULT_DATABASE_CONTRACTS.items():
            with self.subTest(database=name), closing(sqlite3.connect(root / f"{name}.sqlite3")) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], contract.user_version)
                self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], contract.application_id)
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                self.assertEqual(tables, contract.tables)

    def test_runtime_initializes_exact_contract_before_first_planning_request(self):
        from app.services.teacher_planning import TeacherPlanning

        root = self.root / "fresh-runtime"
        runtime = self._runtime(root)
        self._assert_runtime_contracts(root)
        manager = BackupManager({name: root / f"{name}.sqlite3" for name in REQUIRED_DATABASES}, root)
        initial = manager.inspect(manager.export_bytes())
        TeacherPlanning(runtime.learning, runtime.platform, runtime.content)
        self._assert_runtime_contracts(root)
        after = manager.inspect(manager.export_bytes())
        self.assertEqual(initial["databases"]["learning"]["tables"], after["databases"]["learning"]["tables"])

    def test_existing_v2_without_lazy_planning_tables_is_stabilized_on_reopen(self):
        path = self.root / "older-learning.sqlite3"
        store = LearningStore(path)
        goal = store.create_goal("teacher", "Existing goal")
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE IF EXISTS teacher_next_round_drafts")
            db.execute("DROP TABLE IF EXISTS teacher_plan_previews")
            db.execute("DROP TABLE IF EXISTS teacher_plan_drafts")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        reopened = LearningStore(path)
        with reopened.connect() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, DEFAULT_DATABASE_CONTRACTS["learning"].tables)
            self.assertEqual(db.execute("SELECT title FROM learning_goals WHERE id=?", (goal["id"],)).fetchone()[0],
                             "Existing goal")
        LearningStore(path)

    def test_runtime_contract_rejects_missing_and_extra_tables_without_mutating_databases(self):
        root = self.root / "contract-runtime"
        self._runtime(root)
        paths = {name: root / f"{name}.sqlite3" for name in REQUIRED_DATABASES}
        for domain, table in (("learning", "teacher_plan_previews"), ("reports", "teaching_insight_outbox"),
                              ("companion", "hint_checkpoints")):
            for mutation in ("missing", "extra"):
                with self.subTest(domain=domain, mutation=mutation):
                    target = self.root / f"{domain}-{mutation}.sqlite3"
                    with closing(sqlite3.connect(paths[domain])) as source, closing(sqlite3.connect(target)) as db:
                        source.backup(db)
                        if mutation == "missing":
                            db.execute(f'DROP TABLE "{table}"')
                        else:
                            db.execute("CREATE TABLE unexpected_stage_four_data(value TEXT)")
                    before = target.read_bytes()
                    manager = BackupManager({**paths, domain: target}, root)
                    with self.assertRaisesRegex(BackupError, f"unexpected {domain} database table set"):
                        manager.export_bytes()
                    self.assertEqual(target.read_bytes(), before)

    def test_bundle_round_trip_to_isolated_staging_and_manifest_hashes(self) -> None:
        before = {name: path.read_bytes() for name, path in self.database_paths.items()}
        content = self.manager.export_bytes()
        inspection = self.manager.inspect(content)
        self.assertTrue(inspection["verified"])
        self.assertEqual(set(inspection["databases"]), set(REQUIRED_DATABASES))
        attachment_paths = {row["path"] for row in inspection["attachments"]}
        self.assertIn("attachments/material_originals/upload_x/original.bin", attachment_paths)
        self.assertIn("attachments/shared/unrelated.txt", attachment_paths)
        self.assertNotIn("attachments/shared/material_originals/upload_x/original.bin", attachment_paths)
        staging_parent = self.root / "restores"
        staging_parent.mkdir()
        restored = self.manager.restore_to_staging(content, staging_parent)
        self.assertTrue(restored.is_dir())
        self.assertEqual(sorted(path.name for path in (restored / "databases").iterdir()),
                         sorted(f"{name}.sqlite3" for name in REQUIRED_DATABASES))
        self.assertEqual((restored / "attachments/shared/attachments/course/handout.bin").read_bytes(), b"attachment-payload")
        self.assertEqual((restored / "attachments/shared/unrelated.txt").read_bytes(), b"shared-data-dir-file")
        self.assertEqual((restored / "attachments/material_originals/upload_x/original.bin").read_bytes(), b"private-original")
        for name, contract in self.contracts.items():
            path = restored / "databases" / f"{name}.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], contract.user_version)
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], f"{name}-data")
        self.assertEqual({name: path.read_bytes() for name, path in self.database_paths.items()}, before)

        root_manager = BackupManager(
            self.database_paths, self.live, self.contracts,
            additional_attachment_dirs={"material_originals": self.additional},
        )
        root_manifest = root_manager.inspect(root_manager.export_bytes())
        root_paths = {row["path"] for row in root_manifest["attachments"]}
        self.assertNotIn("attachments/shared/platform.sqlite3", root_paths)
        self.assertIn("attachments/shared/attachments/course/handout.bin", root_paths)
        self.assertIn("attachments/shared/unrelated.txt", root_paths)
        self.assertNotIn("attachments/shared/material_originals/upload_x/original.bin", root_paths)
        self.assertIn("attachments/material_originals/upload_x/original.bin", root_paths)

    def test_uncheckpointed_wal_is_backed_up_and_restored_without_sidecars(self) -> None:
        with ExitStack() as connections:
            source_bytes = {}
            for name, path in self.database_paths.items():
                connection = connections.enter_context(closing(sqlite3.connect(path)))
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                self.assertEqual(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
                source_bytes[name] = path.read_bytes()
                connection.execute("INSERT INTO marker(value) VALUES (?)", (f"{name}-wal-only",))
                # Live validation must also observe schema metadata committed only to WAL.
                connection.execute("PRAGMA user_version=3")
                connection.commit()
                self.contracts[name] = DatabaseContract(3, self.contracts[name].application_id,
                                                         self.contracts[name].tables)
                self.assertEqual(path.read_bytes(), source_bytes[name])
                self.assertGreater(Path(str(path) + "-wal").stat().st_size, 32)
                self.assertTrue(Path(str(path) + "-shm").is_file())

            manager = BackupManager(self.database_paths, self.live, self.contracts,
                                    additional_attachment_dirs={"material_originals": self.additional})
            bundle = manager.export_bytes()
            inspection = manager.inspect(bundle)
            self.assertTrue(inspection["verified"])
            attachment_paths = {entry["path"] for entry in inspection["attachments"]}
            for name in REQUIRED_DATABASES:
                self.assertEqual(inspection["databases"][name]["user_version"], 3)
                for suffix in ("", "-wal", "-shm", "-journal"):
                    self.assertNotIn(f"attachments/shared/{name}.sqlite3{suffix}", attachment_paths)
            staging_parent = self.root / "wal-restores"
            staging_parent.mkdir()
            restored = manager.restore_to_staging(bundle, staging_parent)
            # Check immediately: opening a restored WAL-mode file normally could itself create sidecars.
            self.assertEqual(sorted(path.name for path in (restored / "databases").iterdir()),
                             sorted(f"{name}.sqlite3" for name in REQUIRED_DATABASES))
            for name, source in self.database_paths.items():
                path = restored / "databases" / f"{name}.sqlite3"
                entry = inspection["databases"][name]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual(path.stat().st_size, entry["size"])
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                    self.assertEqual(connection.execute("SELECT value FROM marker ORDER BY id").fetchall(),
                                     [(f"{name}-data",), (f"{name}-wal-only",)])
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(source.read_bytes(), source_bytes[name])
                self.assertGreater(Path(str(source) + "-wal").stat().st_size, 32)
                self.assertTrue(Path(str(source) + "-shm").is_file())
            self.assertEqual(sorted(path.name for path in (restored / "databases").iterdir()),
                             sorted(f"{name}.sqlite3" for name in REQUIRED_DATABASES))

    def test_runtime_six_real_databases_export_and_restore(self) -> None:
        from app.services.report_exports import build_report_snapshot
        from app.services.teacher_planning import TeacherPlanning
        from app.services.teaching_insights import TeachingInsights

        source_root = self.root / "runtime-source"
        runtime = self._runtime(source_root)
        try:
            self._assert_runtime_contracts(source_root)
            teacher = runtime.platform.create_user("teacher", "Teacher", "hash", "teacher", user_id="teacher")
            student = runtime.platform.create_user("student", "Student", "hash", "student", user_id="student")
            course = runtime.platform.create_course("Course", "2026", teacher["id"], course_id="course")
            runtime.platform.create_class("course", "Class", owner_teacher_id="teacher", class_id="class")
            runtime.platform.create_course_membership(course["id"], student["id"])
            runtime.platform.create_class_membership("class", "student")

            learning = runtime.learning
            goal = learning.create_goal("teacher", "Goal", course_id="course", class_id="class")
            round_row = learning.create_round("teacher", goal["id"], 1)
            learning.create_question_version("teacher", "question", 1,
                {"stem": "2+2", "options": [], "type": "fill", "answer": "4", "points": 1,
                 "family_id": "addition"}, status="approved", course_id="course")
            task = learning.create_task("teacher", "student", goal["id"], round_row["id"],
                [{"question_id": "question", "version": 1}])
            lease = learning.acquire_draft_lease(task["id"], "student", "device-1")
            learning.save_draft(task["id"], "student", {"question": "4"},
                expected_version=lease["task_version"], request_id="draft-1",
                lease_token=lease["lease_token"], fence_token=lease["fence_token"])

            task_with_draft = learning.get_task(task["id"], "student")
            job = runtime.queue.enqueue("interaction", {"purpose": "runtime-backup-test"})
            material = runtime.content.import_material(
                course_id="course", owner_id="teacher", filename="note.txt", content=b"lesson",
                parsed={"status": "parsed", "format": "txt", "blocks": [{"anchor": "line:1", "kind": "text", "text": "lesson"}], "issues": []},
            )
            scope = {"kind": "goal", "goal_id": goal["id"], "class_id": "class", "course_id": "course"}
            canonical = learning.report_input(scope=scope, actor_id="teacher")
            frozen = build_report_snapshot(canonical, scope)
            report = runtime.reports.create_snapshot(scope, canonical["source_revision"], frozen, created_by="teacher")
            revision = runtime.reports.create_revision(report["id"])
            conversation = runtime.companion.create_conversation("student", "Study", request_id="conversation-1")

            # Exercise the real planning APIs: all three formerly lazy tables
            # must already exist, and their persisted rows must survive restore.
            planning = TeacherPlanning(learning, runtime.platform, runtime.content)
            config = {"course_id": "course", "class_id": "class", "title": "Next learning round",
                      "student_ids": ["student"], "question_count": 1,
                      "question_types": {"single_choice": 0, "fill": 1, "judge": 0, "text": 0}}
            draft = planning.save_draft("teacher", config, request_id="backup-plan")
            preview = planning.preview("teacher", config)
            self.assertTrue(preview["ready"], preview)
            insight_service = TeachingInsights(runtime)
            insight = insight_service.create("teacher", report_id=revision["report_id"],
                                             source_report_revision=revision["id"], request_id="backup-insight")
            runtime.insights.bind_generation(insight["id"], 1)
            output = {"schema_version": "teaching.insight.selection.v1", "summary_code": "needs_more_evidence",
                      "evidence_refs": ["report:scope", "report:rule", "report:students"],
                      "findings": [{"student_code": "S001", "status": "insufficient_evidence", "percent": None,
                                    "independent_families": 0, "evidence_refs": ["mastery:S001"],
                                    "explanation_code": "collect_independent_evidence"}],
                      "limitations": ["goal_level_only", "uncalibrated_rule", "insufficient_evidence"]}
            completed = insight_service.apply(insight["id"], output, generation=1)
            self.assertEqual(completed["status"], "completed")
            next_draft = insight_service.create_next_round("teacher", insight["id"],
                                                          request_id="backup-next-round", config=config)
            insight_row = runtime.insights.get(insight["id"])
            with learning.connect() as db:
                planning_rows = {table: [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
                                 for table in sorted(PLANNING_TABLES)}
            self.assertTrue(all(planning_rows.values()))
            self.assertEqual(planning_rows["teacher_plan_drafts"][0]["id"], draft["id"])
            with runtime.reports.connect() as db:
                insight_outbox = dict(db.execute("SELECT * FROM teaching_insight_outbox WHERE insight_id=?",
                                                (insight["id"],)).fetchone())
            self._assert_runtime_contracts(source_root)

            data_root = source_root
            backup = BackupManager(
                {name: data_root / f"{name}.sqlite3" for name in REQUIRED_DATABASES}, data_root,
                additional_attachment_dirs={"material_originals": data_root / "material_originals"},
            )
            bundle = backup.export_bytes()
            inspection = backup.inspect(bundle)
            self.assertTrue(inspection["verified"])
            staging_parent = self.root / "runtime-staging"
            staging_parent.mkdir()
            restored = backup.restore_to_staging(bundle, staging_parent)
            # Restore must return sealed main files before the normal Runtime can create sidecars.
            self.assertEqual(sorted(path.name for path in (restored / "databases").iterdir()),
                             sorted(f"{name}.sqlite3" for name in REQUIRED_DATABASES))
            for name in REQUIRED_DATABASES:
                path = restored / "databases" / f"{name}.sqlite3"
                entry = inspection["databases"][name]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual(path.stat().st_size, entry["size"])
            self.assertTrue((restored / "attachments/material_originals").is_dir())
            self.assertTrue((data_root / "platform.sqlite3").is_file())

            restored_settings = type("RuntimeSettings", (), {
                "data_dir": str(restored / "databases"), "worker_enabled": False,
            })()
            recovered = Runtime(restored_settings)
            recovered.install("gateway", FakeGateway())
            try:
                recovered.initialize()
                self._assert_runtime_contracts(restored / "databases")
                self.assertEqual(recovered.platform.get_user("teacher"), teacher)
                self.assertEqual(recovered.platform.get_user("student"), student)
                self.assertEqual(recovered.platform.get_course("course"), course)
                membership = recovered.platform.get_course_membership("course", "student")
                self.assertEqual((membership["course_id"], membership["user_id"]), ("course", "student"))
                self.assertEqual(recovered.learning.get_task(task["id"], "student"), task_with_draft)
                self.assertEqual(recovered.learning.get_task(task["id"], "student")["draft_answers"], {"question": "4"})
                self.assertEqual(recovered.queue.get_job(job.job_id), job)
                self.assertEqual(recovered.content.get_material(material["id"]), material)
                blocks = recovered.content.list_blocks(material["id"])
                self.assertEqual([(block["anchor"], block["text"]) for block in blocks], [("line:1", "lesson")])
                # Bundle attachment roots remain explicit; point the reopened store at its restored originals.
                recovered.content.originals_dir = restored / "attachments/material_originals"
                self.assertEqual(recovered.content.original_bytes(material["id"]), b"lesson")
                self.assertEqual(recovered.reports.get_snapshot(report["id"]), report)
                self.assertEqual(recovered.reports.get_snapshot(report["id"])["snapshot"], frozen)
                self.assertEqual(recovered.reports.get_revision(revision["id"]), revision)
                self.assertEqual(recovered.insights.get(insight["id"]), insight_row)
                with recovered.reports.connect() as db:
                    self.assertEqual(dict(db.execute("SELECT * FROM teaching_insight_outbox WHERE insight_id=?",
                                                     (insight["id"],)).fetchone()), insight_outbox)
                recovered_planning = TeacherPlanning(recovered.learning, recovered.platform, recovered.content)
                self.assertEqual(recovered_planning.get_next_round_draft("teacher", next_draft["id"]), next_draft)
                with recovered.learning.connect() as db:
                    restored_rows = {table: [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
                                     for table in sorted(PLANNING_TABLES)}
                self.assertEqual(restored_rows, planning_rows)
                self.assertEqual(recovered.companion.get_conversation(conversation["id"], "student"), conversation)
            finally:
                recovered.stop()
        finally:
            runtime.stop()

    def test_bundle_rejects_missing_contract_or_unknown_database_version(self) -> None:
        with self.assertRaises(BackupError):
            BackupManager({"platform": self.database_paths["platform"]}, self.attachments, {})
        with closing(sqlite3.connect(self.database_paths["jobs"])) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(BackupError, "schema version"):
            self.manager.export_bytes()

    def test_bundle_detects_attachment_tamper_and_missing_database(self) -> None:
        content = self.manager.export_bytes()
        tampered = BytesIO()
        with ZipFile(BytesIO(content), "r") as source, ZipFile(tampered, "w", ZIP_DEFLATED) as target:
            for entry in source.infolist():
                payload = source.read(entry.filename)
                if entry.filename == "attachments/shared/attachments/course/handout.bin":
                    payload = b"tampered"
                target.writestr(entry.filename, payload)
        with self.assertRaises(BackupError):
            self.manager.inspect(tampered.getvalue())

        missing = BytesIO()
        with ZipFile(BytesIO(content), "r") as source, ZipFile(missing, "w", ZIP_DEFLATED) as target:
            for entry in source.infolist():
                if entry.filename != "databases/learning.sqlite3":
                    target.writestr(entry.filename, source.read(entry.filename))
        with self.assertRaises(BackupError):
            self.manager.inspect(missing.getvalue())

    def test_empty_private_attachment_root_is_treated_as_empty(self) -> None:
        manager = BackupManager(
            self.database_paths, self.live, self.contracts,
            additional_attachment_dirs={"material_originals": self.root / "not-created-yet"},
        )
        inspected = manager.inspect(manager.export_bytes())
        self.assertTrue(inspected["verified"])
        self.assertFalse(any(row["path"].startswith("attachments/material_originals/")
                             for row in inspected["attachments"]))

    def test_restore_refuses_staging_inside_live_directory(self) -> None:
        content = self.manager.export_bytes()
        with self.assertRaisesRegex(BackupError, "outside"):
            self.manager.restore_to_staging(content, self.live)
        self.assertEqual(sorted(item.name for item in self.live.iterdir()),
                         sorted(["attachments", "material_originals", "unrelated.txt",
                                 *(f"{name}.sqlite3" for name in REQUIRED_DATABASES)]))

    def test_bad_application_id_and_unversioned_nonempty_database_fail_closed(self) -> None:
        wrong = application_id_from_text("ZZZZ")
        with closing(sqlite3.connect(self.database_paths["content"])) as connection:
            connection.execute(_APPLICATION_ID_PRAGMAS[wrong])
        with self.assertRaisesRegex(BackupError, "application_id"):
            self.manager.export_bytes()


if __name__ == "__main__":
    unittest.main()
