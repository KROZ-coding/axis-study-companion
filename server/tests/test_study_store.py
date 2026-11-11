import atexit
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import zlib

# Do this before importing any app module (main has a module-level application).
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="study-store-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ["SKIP_ENV_FILE"] = "1"
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = _IMPORT_DATA.name
os.environ["STUDY_DATA_DIR"] = _IMPORT_DATA.name
os.environ["PERSISTENCE_ENABLED"] = "false"
os.environ["STUDY_PERSISTENCE_ENABLED"] = "false"

from app.config import Settings
from app.study_store import StudyStore, TeachingPersistenceError


class StudyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="study-store-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = Settings(
            app_env="test", persistence_enabled=True, data_dir=str(self.root),
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="",
        )

    def _legacy_state(self, version: int = 2) -> dict:
        return {
            "version": version,
            "reports": {
                "study-practice": {"S07": {"P-501": 1, "P-502": 0}, "S08": {"P-501": 2}},
                "cached-report": {"summary": "not a teaching fact"},
            },
        }

    def _write_sqlite(self, state: dict) -> None:
        with closing(sqlite3.connect(self.root / "store.sqlite3")) as connection:
            with connection:
                connection.execute("CREATE TABLE store (id INTEGER PRIMARY KEY, blob BLOB NOT NULL)")
                connection.execute("INSERT INTO store VALUES (1, ?)", (zlib.compress(json.dumps(state).encode("utf-8")),))

    def _disk_state(self) -> dict:
        with closing(sqlite3.connect(self.root / "store.sqlite3")) as connection:
            row = connection.execute("SELECT blob FROM store WHERE id = 1").fetchone()
        return json.loads(zlib.decompress(row[0]))

    def _new_store(self) -> StudyStore:
        return StudyStore(self.config)

    def test_new_schema_and_practice_survive_report_clearing_and_restart(self) -> None:
        store = self._new_store()
        state = store.teaching_snapshot()
        self.assertEqual(state["schema_version"], 1)
        for key in ("reviews", "followups", "tasks", "attempts", "feedback", "idempotency"):
            self.assertEqual(state[key], {})
        with store.teaching_transaction() as state:
            state["tasks"]["task-1"] = {"status": "not_started", "frozen": {"options": ["甲", "乙"]}}
        self.assertTrue(store.save_practice("S07", "P-501", 1))
        store.reports["cache"] = {"x": 1}
        store.rebuild_mastery()
        self.assertEqual(store.reports, {})
        self.assertTrue(store.save())
        restarted = self._new_store()
        self.assertEqual(restarted.teaching_snapshot(), store.teaching_snapshot())
        self.assertEqual(restarted.practice_answers("S07"), {"P-501": 1})
        self.assertEqual(restarted.mastery, {})
        self.assertEqual(restarted.quiz_sessions, {})

    def test_old_sqlite_is_migrated_once_and_original_choices_can_be_restored(self) -> None:
        legacy = self._legacy_state()
        self._write_sqlite(legacy)
        store = self._new_store()
        state = store.teaching_snapshot()
        self.assertEqual(state["known_last_choices"], legacy["reports"]["study-practice"])
        self.assertEqual(state["attempts"], {})
        self.assertEqual(state["tasks"], {})
        self.assertEqual(state["feedback"], {})
        self.assertEqual(state["migrations"]["study-practice"], {
            "source": "reports.study-practice", "original": legacy["reports"]["study-practice"],
        })
        self.assertNotIn("study-practice", store.reports)
        self.assertIn("cached-report", store.reports)
        self.assertIn("teaching_state", self._disk_state())
        with patch.object(StudyStore, "save", side_effect=AssertionError("migration ran twice")):
            restarted = self._new_store()
        self.assertEqual(restarted.teaching_snapshot(), state)

        # Rebuild an old-format backup using only the preserved source, then import it.
        recovery = {"version": 2, "reports": {"study-practice": state["migrations"]["study-practice"]["original"]}}
        restarted.restore_bytes(json.dumps(recovery).encode("utf-8"))
        self.assertEqual(restarted.practice_answers("S07"), {"P-501": 1, "P-502": 0})

    def test_version_one_migration_captures_choices_before_upstream_clears_reports(self) -> None:
        self._write_sqlite(self._legacy_state(version=1))
        store = self._new_store()
        self.assertEqual(store.reports, {})
        self.assertEqual(store.practice_answers("S07"), {"P-501": 1, "P-502": 0})
        self.assertEqual(self._new_store().teaching_snapshot(), store.teaching_snapshot())

    def test_legacy_json_import_preserves_original_file_and_teaching_facts(self) -> None:
        original = json.dumps(self._legacy_state()).encode("utf-8")
        path = self.root / "store.json"
        path.write_bytes(original)
        store = self._new_store()
        self.assertFalse(path.exists())
        self.assertEqual(path.with_suffix(".json.migrated").read_bytes(), original)
        self.assertEqual(store.practice_answers("S08"), {"P-501": 2})
        self.assertEqual(self._new_store().teaching_snapshot(), store.teaching_snapshot())

    def test_migration_marker_ignores_reintroduced_stale_cache(self) -> None:
        self._write_sqlite(self._legacy_state())
        store = self._new_store()
        self.assertTrue(store.save_practice("S07", "P-501", 3))
        backup = json.loads(store.export_json())
        backup["reports"]["study-practice"] = {"S07": {"P-501": 0, "old-only": 1}}
        store.restore_bytes(json.dumps(backup).encode("utf-8"))
        self.assertEqual(store.practice_answers("S07"), {"P-501": 3, "P-502": 0})
        self.assertNotIn("study-practice", store.reports)
        self.assertEqual(store.teaching_snapshot()["migrations"]["study-practice"]["original"]["S07"]["P-501"], 1)

    def test_mixed_snapshot_keeps_newer_independent_choice_on_first_migration(self) -> None:
        store = self._new_store()
        legacy = self._legacy_state()
        legacy["teaching_state"] = store.teaching_snapshot()
        legacy["teaching_state"]["known_last_choices"] = {"S07": {"P-501": 3}}
        store.restore_bytes(json.dumps(legacy).encode("utf-8"))
        self.assertEqual(store.practice_answers("S07"), {"P-501": 3, "P-502": 0})
        self.assertEqual(store.practice_answers("S08"), {"P-501": 2})

    def test_all_snapshot_and_backup_paths_round_trip_teaching_state(self) -> None:
        store = self._new_store()
        with store.teaching_transaction() as state:
            for collection in ("reviews", "followups", "tasks", "attempts", "feedback", "idempotency"):
                state[collection]["record-1"] = {"version": 2, "items": [{"text": "教师批注"}]}
        self.assertTrue(store.save_practice("S07", "P-501", 1))
        expected = store.teaching_snapshot()
        snapshots = (store._snapshot(), store._state(), store._state(include_runtime=False))
        for snapshot in snapshots:
            self.assertEqual(snapshot["teaching_state"], expected)
            snapshot["teaching_state"]["tasks"].clear()
            self.assertEqual(store.teaching_snapshot(), expected)

        json_backup = store.export_json().encode("utf-8")
        (self.root / "lesson.txt").write_text("附件", encoding="utf-8")
        archive = store.export_archive()
        with ZipFile(BytesIO(archive)) as package:
            self.assertEqual(json.loads(package.read("store.json"))["teaching_state"], expected)
        for kind, content in (("json", json_backup), ("zip", archive)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="study-restore-") as directory:
                restored = StudyStore(Settings(app_env="test", persistence_enabled=True, data_dir=directory, llm_providers=()))
                if kind == "json":
                    restored.restore_bytes(content)
                else:
                    restored.restore_archive(content)
                    self.assertEqual(Path(directory, "lesson.txt").read_text(encoding="utf-8"), "附件")
                self.assertEqual(restored.teaching_snapshot(), expected)
                self.assertEqual(StudyStore(restored.settings).teaching_snapshot(), expected)

    def test_business_exception_rolls_back_nested_changes_without_saving(self) -> None:
        store = self._new_store()
        with store.teaching_transaction() as state:
            state["tasks"]["task-1"] = {"status": "draft", "choices": [0]}
        before = store.teaching_snapshot()
        error = ValueError("version conflict")
        with patch.object(store, "save") as save:
            with self.assertRaises(ValueError) as caught:
                with store.teaching_transaction() as state:
                    state["tasks"]["task-1"]["choices"].append(1)
                    state["tasks"]["task-2"] = {}
                    raise error
            save.assert_not_called()
        self.assertIs(caught.exception, error)
        self.assertEqual(store.teaching_snapshot(), before)
        self.assertEqual(self._new_store().teaching_snapshot(), before)

    def test_save_false_and_real_sqlite_failure_restore_memory_disk_and_dirty(self) -> None:
        store = self._new_store()
        self.assertTrue(store.save_practice("S07", "P-501", 1))
        before = store.teaching_snapshot()
        store.mark_dirty()
        for target, kwargs in (("save", {"return_value": False}), ("_connect", {"side_effect": sqlite3.OperationalError("write failed")})):
            with self.subTest(target=target), patch.object(store, target, **kwargs):
                with self.assertRaises(TeachingPersistenceError):
                    with store.teaching_transaction() as state:
                        state["known_last_choices"]["S07"]["P-501"] = 3
                        state["tasks"]["new"] = {"state": "published"}
            self.assertEqual(store.teaching_snapshot(), before)
            self.assertEqual(store._dirty, 1)
            self.assertEqual(self._new_store().teaching_snapshot(), before)

    def test_save_exception_becomes_teaching_persistence_error_and_rolls_back(self) -> None:
        store = self._new_store()
        before = store.teaching_snapshot()
        error = OSError("disk unavailable")
        with patch.object(store, "save", side_effect=error), self.assertRaises(TeachingPersistenceError) as caught:
            with store.teaching_transaction() as state:
                state["tasks"]["new"] = {}
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(store.teaching_snapshot(), before)
        with patch.object(store, "save", return_value=False):
            self.assertFalse(store.save_practice("S07", "P-501", 2))
        self.assertEqual(store.practice_answers("S07"), {})

    def test_non_json_transaction_is_rejected_even_with_persistence_disabled(self) -> None:
        store = StudyStore(Settings(app_env="test", persistence_enabled=False, data_dir=str(self.root), llm_providers=()))
        before = store.teaching_snapshot()
        for value in (object(), float("nan")):
            with self.subTest(value=value), self.assertRaises(TeachingPersistenceError):
                with store.teaching_transaction() as state:
                    state["tasks"]["bad"] = value
            self.assertEqual(store.teaching_snapshot(), before)

    def test_nested_transaction_cannot_persist_part_of_failed_outer_change(self) -> None:
        store = self._new_store()
        before = store.teaching_snapshot()
        with patch.object(store, "save") as save, self.assertRaises(RuntimeError):
            with store.teaching_transaction() as state:
                state["tasks"]["outer"] = {}
                with store.teaching_transaction() as nested:
                    nested["tasks"]["inner"] = {}
        save.assert_not_called()
        self.assertEqual(store.teaching_snapshot(), before)

    def test_snapshots_and_practice_reads_do_not_alias_internal_state(self) -> None:
        store = self._new_store()
        self.assertTrue(store.save_practice("S07", "P-501", 1))
        with store.teaching_transaction() as state:
            state["tasks"]["task-1"] = {"history": [{"answer": 1}]}
        snapshot = store.teaching_snapshot()
        snapshot["tasks"]["task-1"]["history"][0]["answer"] = 3
        store.practice_answers("S07")["P-501"] = 2
        self.assertEqual(store.practice_answers("S07"), {"P-501": 1})
        self.assertEqual(store.teaching_snapshot()["tasks"]["task-1"]["history"], [{"answer": 1}])

    def test_failed_backup_restore_rolls_back_teaching_and_reports(self) -> None:
        store = self._new_store()
        self.assertTrue(store.save_practice("S07", "P-501", 3))
        store.reports["live"] = {"version": 1}
        self.assertTrue(store.save())
        before = store.teaching_snapshot()
        reports = deepcopy(store.reports)
        store.mark_dirty()
        with patch.object(store, "save", return_value=False), self.assertRaises(OSError):
            store.restore_bytes(json.dumps(self._legacy_state()).encode("utf-8"))
        self.assertEqual(store.teaching_snapshot(), before)
        self.assertEqual(store.reports, reports)
        self.assertEqual(store._dirty, 1)
        self.assertEqual(self._new_store().teaching_snapshot(), before)

    def test_invalid_teaching_backup_is_rejected_before_changing_memory(self) -> None:
        store = self._new_store()
        before = store.teaching_snapshot()
        for field, value in (("schema_version", 999), ("schema_version", True), ("tasks", [])):
            backup = json.loads(store.export_json())
            backup["teaching_state"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                store.restore_bytes(json.dumps(backup).encode("utf-8"))
            self.assertEqual(store.teaching_snapshot(), before)

    def test_failed_initial_migration_leaves_old_snapshot_recoverable(self) -> None:
        legacy = self._legacy_state()
        self._write_sqlite(legacy)
        with patch.object(StudyStore, "save", return_value=False), self.assertRaises(OSError):
            self._new_store()
        self.assertEqual(self._disk_state(), legacy)
        self.assertEqual(self._new_store().practice_answers("S07"), {"P-501": 1, "P-502": 0})

    def test_threads_serialize_transactions_and_keep_every_increment(self) -> None:
        store = self._new_store()
        with store.teaching_transaction() as state:
            state["tasks"]["counter"] = {"value": 0}
        workers = 4
        rounds = 8
        barrier = Barrier(workers)

        def increment() -> None:
            barrier.wait(timeout=5)
            for _ in range(rounds):
                with store.teaching_transaction() as state:
                    state["tasks"]["counter"]["value"] += 1

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(increment) for _ in range(workers)]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(store.teaching_snapshot()["tasks"]["counter"]["value"], workers * rounds)
        self.assertEqual(self._new_store().teaching_snapshot(), store.teaching_snapshot())

    def test_reader_waits_until_failed_transaction_has_rolled_back(self) -> None:
        store = self._new_store()
        self.assertTrue(store.save_practice("S07", "P-501", 1))
        save_started, release_save, read_started, read_done = Event(), Event(), Event(), Event()

        def failing_save() -> bool:
            save_started.set()
            if not release_save.wait(timeout=5):
                raise AssertionError("test did not release save")
            return False

        def write() -> bool:
            return store.save_practice("S07", "P-501", 3)

        def read() -> dict:
            read_started.set()
            try:
                return store.teaching_snapshot()
            finally:
                read_done.set()

        with patch.object(store, "save", side_effect=failing_save), ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(write)
            try:
                self.assertTrue(save_started.wait(timeout=5))
                reader = pool.submit(read)
                self.assertTrue(read_started.wait(timeout=5))
                self.assertFalse(read_done.wait(timeout=0.1))
            finally:
                release_save.set()
            self.assertFalse(writer.result(timeout=5))
            self.assertEqual(reader.result(timeout=5)["known_last_choices"]["S07"], {"P-501": 1})


if __name__ == "__main__":
    unittest.main()
