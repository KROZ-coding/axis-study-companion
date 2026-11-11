"""ReportStore isolation, concurrency and immutable-source behavior."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.report_store import ReportConflict, ReportStore, content_hash
from app.services.report_exports import build_report_snapshot


SCOPE = {"kind": "student", "student_id": "student", "class_id": "class"}


def snapshot(source_revision=1):
    return build_report_snapshot({"source_revision": source_revision, "students": [{"id": "student", "name": "学生"}],
                                  "goals": [{"id": "goal", "title": "学习目标", "class_id": "class"}],
                                  "evidence": []}, SCOPE)


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="report-store-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "reports.sqlite3"
        self.store = ReportStore(self.path)

    def freeze(self, revision=1, **kwargs):
        return self.store.create_snapshot(SCOPE, revision, snapshot(revision), created_by="teacher", **kwargs)

    def test_version_wal_and_reopen(self):
        self.assertEqual(self.store.schema_version, 1)
        self.assertEqual(self.store.journal_mode, "wal")
        frozen = self.freeze()
        revision = self.store.create_revision(frozen["id"])
        reopened = ReportStore(self.path)
        self.assertEqual(reopened.get_revision(revision["id"])["snapshot"], snapshot())
        self.assertEqual(reopened.get_current(SCOPE)["id"], revision["id"])
        self.assertEqual(ReportStore(Path(self.directory.name)).db_path, self.path)

    def test_snapshot_is_frozen_and_returned_mappings_are_detached(self):
        source = snapshot()
        before = deepcopy(source)
        frozen = self.store.create_snapshot(SCOPE, 1, source, created_by="teacher")
        source["tables"]["students"]["rows"][0]["name"] = "mutated source"
        frozen["snapshot"]["tables"]["students"]["rows"][0]["name"] = "mutated return"
        stored = self.store.get_snapshot(frozen["id"])
        self.assertEqual(stored["snapshot"], before)
        self.assertEqual(stored["snapshot_sha256"], content_hash(before))
        with self.store.connect() as connection:
            for statement in ("UPDATE report_snapshots SET created_by='changed'", "DELETE FROM report_snapshots"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(statement)

    def test_revisions_are_immutable_and_new_source_does_not_change_history(self):
        first = self.store.create_revision(self.freeze()["id"])
        newer = self.store.create_revision(self.freeze(2)["id"])
        self.assertEqual(self.store.get_revision(first["id"])["snapshot"], snapshot(1))
        self.assertFalse(self.store.get_revision(first["id"])["is_current"])
        self.assertTrue(newer["is_current"])
        self.assertEqual(newer["revision_number"], 2)
        with self.store.connect() as connection:
            for statement in ("UPDATE report_revisions SET source_revision=999", "DELETE FROM report_revisions"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(statement)

    def test_late_old_completion_cannot_replace_current(self):
        old = self.freeze(1)
        newest = self.store.create_revision(self.freeze(3)["id"])
        late = self.store.create_revision(old["id"])
        self.assertFalse(late["is_current"])
        self.assertEqual(late["revision_number"], 2)
        self.assertEqual(self.store.get_current(SCOPE)["id"], newest["id"])
        self.assertFalse(self.store.promote_revision(late["id"]))

    def test_observed_newer_source_blocks_old_completion_even_before_new_result(self):
        old = self.freeze(1)
        self.freeze(2)
        late = self.store.create_revision(old["id"])
        self.assertFalse(late["is_current"])
        self.assertIsNone(self.store.get_current(SCOPE))
        self.assertEqual(len(self.store.list_revisions(late["report_id"])), 1)

    def test_equal_source_revision_with_changed_content_is_a_conflict(self):
        frozen = self.freeze(1)
        again = self.freeze(1)
        self.assertEqual(frozen["id"], again["id"])
        changed = snapshot(1)
        changed["tables"]["students"]["rows"][0]["name"] = "different data"
        with self.assertRaises(ReportConflict):
            self.store.create_snapshot(SCOPE, 1, changed, created_by="teacher")

    def test_idempotency_replays_original_after_evidence_changes_and_rejects_conflict(self):
        request = {"scope": SCOPE, "expected_source_revision": 1, "rule_version": "mastery-v1"}
        first = self.freeze(1, idempotency_key="request-key-1", request_fingerprint=request)
        self.store.create_revision(self.freeze(2)["id"])
        replay = self.freeze(3, idempotency_key="request-key-1", request_fingerprint=request)
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(self.store.get_idempotent_snapshot("teacher", "request-key-1", request_fingerprint=request)["id"], first["id"])
        with self.assertRaises(ReportConflict):
            self.store.get_idempotent_snapshot("teacher", "request-key-1", request_fingerprint={**request, "expected_source_revision": 2})
        self.assertIsNone(self.store.get_idempotent_snapshot("other-teacher", "request-key-1", request_fingerprint=request))

    def test_concurrent_idempotent_freeze_and_revision_make_one_record(self):
        def create(_):
            local = ReportStore(self.path)
            frozen = local.create_snapshot(SCOPE, 1, snapshot(), created_by="teacher", idempotency_key="parallel-request")
            return frozen["id"], local.create_revision(frozen["id"])["id"]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(create, range(12)))
        self.assertEqual(len(set(results)), 1)
        report = self.store.list_reports()[0]
        self.assertEqual(len(self.store.list_revisions(report["id"])), 1)

    def test_foreign_or_newer_database_is_refused_without_migration(self):
        path = Path(self.directory.name) / "unrelated.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE private_data(value TEXT)")
        connection.close()
        original = path.read_bytes()
        with patch("app.report_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
            with self.assertRaisesRegex(RuntimeError, "unversioned"):
                ReportStore(path)
            self.assertEqual(connect.call_count, 1)
            self.assertIn("mode=ro", connect.call_args.args[0])
        self.assertEqual(path.read_bytes(), original)
        path = Path(self.directory.name) / "future.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "newer"):
            ReportStore(path)

    def test_scope_and_snapshot_concurrency_tokens_must_match(self):
        for changes in ({"source_revision": 2}, {"scope": {"kind": "student", "student_id": "other"}}, {"schema_version": "unknown"}):
            value = {**snapshot(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.create_snapshot(SCOPE, 1, value, created_by="teacher")
        self.assertIsNone(self.store.get_snapshot("missing"))
        with self.assertRaises(KeyError):
            self.store.create_revision("missing")


if __name__ == "__main__":
    unittest.main()
