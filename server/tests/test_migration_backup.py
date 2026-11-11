from contextlib import ExitStack, closing
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
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
from app.report_store import SNAPSHOT_SCHEMA_VERSION
from app.report_store import SNAPSHOT_SCHEMA_VERSION
from app.runtime import Runtime


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
                                    "diagnostic_items", "question_family_index", "question_metadata", "learning_outbox", "appeals", "goal_plans"}))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].user_version, 2)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].application_id, application_id_from_text("AIQ2"))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["jobs"].tables,
                         frozenset({"queue_meta", "budgets", "jobs", "steps", "events", "upstream_calls",
                                    "call_charges", "funding_transactions", "budget_limits",
                                    "call_budget_reservations", "budget_ledger_entries"}))
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["content"].user_version, 1)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["reports"].user_version, 1)
        self.assertEqual(DEFAULT_DATABASE_CONTRACTS["companion"].application_id, application_id_from_text("AXCP"))

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

        source_root = self.root / "runtime-source"
        settings = type("RuntimeSettings", (), {"data_dir": str(source_root), "worker_enabled": False})()
        runtime = Runtime(settings)
        try:
            runtime.initialize()
            teacher = runtime.platform.create_user("teacher", "Teacher", "hash", "teacher", user_id="teacher")
            student = runtime.platform.create_user("student", "Student", "hash", "student", user_id="student")
            course = runtime.platform.create_course("Course", "2026", teacher["id"], course_id="course")
            runtime.platform.create_course_membership(course["id"], student["id"])

            learning = runtime.learning
            goal = learning.create_goal("teacher", "Goal", course_id="course")
            round_row = learning.create_round("teacher", goal["id"], 1)
            learning.create_question_version("teacher", "question", 1,
                {"stem": "2+2", "options": [], "type": "text", "answer": "4"}, status="approved")
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
            scope = {"kind": "student", "student_id": "student", "class_id": "class"}
            frozen = build_report_snapshot({"source_revision": 1, "students": [{"id": "student", "name": "Student"}],
                                            "goals": [{"id": "goal", "title": "Goal", "class_id": "class"}],
                                            "evidence": []}, scope)
            report = runtime.reports.create_snapshot(scope, 1, frozen, created_by="teacher")
            conversation = runtime.companion.create_conversation("student", "Study", request_id="conversation-1")

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
            try:
                recovered.initialize()
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
