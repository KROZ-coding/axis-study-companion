"""Isolated regression coverage for account data, migrations and class scopes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.platform_store import PlatformStore, resolve_platform_db_path


class PlatformStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="platform-store-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "platform.sqlite3"
        self.store = PlatformStore(self.path)

    def user(self, username, role="teacher", **fields):
        return self.store.create_user(username, username, "test-hash", role, **fields)

    def classroom(self, owner, *, name="Section", course_owner=None):
        course = self.store.create_course("Course " + name, "2026", (course_owner or owner)["id"])
        classroom = self.store.create_class(course["id"], name, owner_teacher_id=owner["id"])
        return course, classroom

    def member(self, course, classroom, *, username="student", status="active"):
        student = self.user(username, "student", status=status)
        self.store.create_course_membership(course["id"], student["id"])
        self.store.create_class_membership(classroom["id"], student["id"])
        return student

    def test_schema_wal_foreign_keys_uniqueness_and_reopen(self):
        self.assertEqual(self.store.schema_version, PlatformStore.SCHEMA_VERSION)
        self.assertEqual(self.store.journal_mode, "wal")
        teacher = self.user("Teacher")
        course, classroom = self.classroom(teacher)
        student = self.member(course, classroom)
        with self.assertRaises(sqlite3.IntegrityError):
            self.user("teacher")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_class_membership(classroom["id"], student["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_course_membership("missing-course", student["id"])
        with self.assertRaises(ValueError):
            self.store.create_class(course["id"], "No owner")
        self.store.create_session("private-session", student["id"], 9_999_999_999)
        reopened = PlatformStore(self.path)
        self.assertEqual(reopened.get_user_by_username("TEACHER")["id"], teacher["id"])
        self.assertTrue(reopened.can_access_class(student["id"], classroom["id"]))
        self.assertEqual(reopened.get_session("private-session")["user_id"], student["id"])
        with reopened.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(b"private-session").hexdigest())
        self.assertIsNone(reopened.get_session("private-session", now=9_999_999_999))

    def test_class_scopes_are_independent_of_course_material_rights(self):
        material_owner = self.user("material-owner")
        class_owner = self.user("class-owner")
        reader, manager = self.user("reader"), self.user("manager")
        course, classroom = self.classroom(class_owner, course_owner=material_owner)
        student = self.member(course, classroom)
        self.store.set_course_collaborator(course["id"], manager["id"], "manage", granted_by=material_owner["id"])
        self.assertTrue(self.store.can_manage_course_materials(material_owner["id"], course["id"]))
        self.assertFalse(self.store.can_access_class(material_owner["id"], classroom["id"]))
        self.assertFalse(self.store.can_access_class(manager["id"], classroom["id"]))
        self.assertEqual(self.store.list_accessible_classes(material_owner["id"]), [])
        self.assertFalse(self.store.can_manage_course_materials(class_owner["id"], course["id"]))
        self.store.set_class_collaborator(classroom["id"], reader["id"], "read", granted_by=class_owner["id"])
        self.store.set_class_collaborator(classroom["id"], manager["id"], "manage", granted_by=class_owner["id"])
        self.assertTrue(self.store.can_access_class(reader["id"], classroom["id"]))
        self.assertFalse(self.store.can_access_class(reader["id"], classroom["id"], teaching=True))
        self.assertFalse(self.store.can_reset_user(reader["id"], student["id"]))
        self.assertTrue(self.store.can_reset_user(manager["id"], student["id"]))
        with self.assertRaises(PermissionError):
            self.store.set_class_collaborator(classroom["id"], material_owner["id"], "manage", granted_by=manager["id"])

    def test_scoped_reset_derives_saved_scope_and_admin_resets_all_roles(self):
        owner, admin = self.user("owner"), self.user("admin", "admin")
        course, classroom = self.classroom(owner)
        student = self.member(course, classroom)
        code = "class-scoped-reset-token"
        record = self.store.create_activation_code(code, student["id"], owner["id"], purpose="reset", course_id=course["id"])
        self.assertEqual(record["class_id"], classroom["id"])
        self.assertNotIn(code, record.values())
        self.assertIsNone(self.store.consume_activation_code(code, purpose="activation"))
        self.assertIsNone(self.store.consume_activation_code(code, purpose="reset", course_id="wrong"))
        self.assertEqual(self.store.consume_activation_code(code, purpose="reset")["id"], student["id"])
        self.assertIsNone(self.store.consume_activation_code(code, purpose="reset"))
        for target in (owner, admin, student):
            with self.subTest(role=target["role"]):
                token = "admin-reset-" + target["id"]
                self.assertTrue(self.store.can_reset_user(admin["id"], target["id"]))
                self.store.create_activation_code(token, target["id"], admin["id"], purpose="reset")
                self.assertEqual(self.store.consume_activation_code(token, purpose="reset")["id"], target["id"])
        with self.assertRaises(PermissionError):
            self.store.create_activation_code("teacher-cannot-reset-teacher", owner["id"], owner["id"], purpose="reset")

    def test_revoked_class_grants_and_owner_transfer_invalidate_codes(self):
        owner, manager, successor = self.user("owner"), self.user("manager"), self.user("successor")
        course, classroom = self.classroom(owner)
        student = self.member(course, classroom)
        self.store.set_class_collaborator(classroom["id"], manager["id"], "manage", granted_by=owner["id"])
        self.store.create_activation_code("manager-issued-code", student["id"], manager["id"], purpose="reset")
        self.store.remove_class_collaborator(classroom["id"], manager["id"], removed_by=owner["id"])
        self.store.set_class_collaborator(classroom["id"], manager["id"], "manage", granted_by=owner["id"])
        self.assertIsNone(self.store.consume_activation_code("manager-issued-code", purpose="reset"))
        self.store.create_activation_code("owner-issued-code", student["id"], owner["id"], purpose="reset")
        self.store.transfer_class_owner(classroom["id"], successor["id"], transferred_by=owner["id"])
        self.assertFalse(self.store.can_access_class(owner["id"], classroom["id"]))
        self.assertTrue(self.store.can_access_class(successor["id"], classroom["id"], teaching=True))
        self.assertIsNone(self.store.consume_activation_code("owner-issued-code", purpose="reset"))

    def test_disabled_target_or_issuer_and_member_removal_invalidate_activation(self):
        owner = self.user("owner")
        course, classroom = self.classroom(owner)
        student = self.member(course, classroom, status="pending")
        self.store.create_activation_code("pending-activation", student["id"], owner["id"])
        self.store.update_user(student["id"], status="disabled")
        self.assertIsNone(self.store.consume_activation_code("pending-activation"))
        self.store.update_user(student["id"], status="pending")
        self.assertIsNone(self.store.consume_activation_code("pending-activation"))
        self.store.create_activation_code("removed-activation", student["id"], owner["id"])
        self.store.delete_class_membership(classroom["id"], student["id"])
        self.store.create_class_membership(classroom["id"], student["id"])
        self.assertIsNone(self.store.consume_activation_code("removed-activation"))
        self.store.create_activation_code("course-removed-activation", student["id"], owner["id"])
        self.store.delete_course_membership(course["id"], student["id"])
        self.store.create_course_membership(course["id"], student["id"])
        self.store.create_class_membership(classroom["id"], student["id"])
        self.assertIsNone(self.store.consume_activation_code("course-removed-activation"))
        self.store.create_activation_code("issuer-activation", student["id"], owner["id"])
        self.store.update_user(owner["id"], status="disabled")
        self.assertIsNone(self.store.consume_activation_code("issuer-activation"))

    def test_atomic_transaction_audit_idempotency_and_rollback(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction():
                owner = self.user("owner")
                self.store.record_audit_event("account_created", owner["id"])
                self.store.create_idempotency_record("create", "request", owner_id=owner["id"], response={"id": owner["id"]})
                with self.store.connect() as connection:
                    self.assertTrue(connection.in_transaction)
                    self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)
                raise RuntimeError("abort")
        self.assertEqual(self.store.list_users(), [])
        self.assertEqual(self.store.list_audit_events(), [])
        self.assertIsNone(self.store.get_idempotency_record("create", "request"))
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.user("nested")
                with self.store.transaction():
                    pass
        self.assertIsNone(self.store.get_user_by_username("nested"))

    def test_atomic_single_use_under_concurrent_consumption(self):
        admin, student = self.user("admin", "admin"), self.user("student", "student", status="pending")
        self.store.create_activation_code("race-activation", student["id"], admin["id"])
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.store.consume_activation_code("race-activation"), range(3)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_rate_limit_survives_restart_and_expires(self):
        self.assertEqual(self.store.consume_rate_limit("login", "alice", limit=2, window_seconds=60, now=100), (True, 0))
        self.assertEqual(self.store.consume_rate_limit("login", "alice", limit=2, window_seconds=60, now=101), (True, 0))
        reopened = PlatformStore(self.path)
        self.assertEqual(reopened.consume_rate_limit("login", "alice", limit=2, window_seconds=60, now=102), (False, 58))
        self.assertEqual(reopened.consume_rate_limit("login", "alice", limit=2, window_seconds=60, now=160), (True, 0))
        with reopened.connect() as connection:
            value = connection.execute("SELECT subject_hash FROM auth_rate_limits").fetchone()[0]
        self.assertNotEqual(value, "alice")

    def test_v1_migration_keeps_course_materials_without_granting_classes(self):
        path = self.root / "old-platform.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("BEGIN")
            PlatformStore._migration_1(connection)
            connection.execute("INSERT INTO users VALUES ('owner', 'owner', 'Owner', 'hash', 'teacher', 'active', '', '', 1, 1)")
            connection.execute("INSERT INTO users VALUES ('student', 'student', 'Student', 'hash', 'student', 'pending', '', '', 1, 1)")
            connection.execute("INSERT INTO courses VALUES ('course', 'Course', '2026', 'owner', '', 1, 1)")
            connection.execute("INSERT INTO classes VALUES ('class', 'course', 'Class', 1)")
            connection.execute("INSERT INTO activation_codes VALUES (?, 'student', 'owner', 'activation', NULL, 1, 9999999999, NULL)",
                               (self.store._token_hash("old-activation"),))
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        upgraded = PlatformStore(path)
        self.assertEqual(upgraded.schema_version, PlatformStore.SCHEMA_VERSION)
        self.assertTrue(upgraded.can_manage_course_materials("owner", "course"))
        self.assertFalse(upgraded.can_access_class("owner", "class"))
        self.assertIsNone(upgraded.get_class("class")["owner_teacher_id"])
        self.assertIsNone(upgraded.consume_activation_code("old-activation"))
        self.assertEqual(PlatformStore(path).get_course("course")["name"], "Course")

    def test_failed_schema_migration_rolls_back_ddl_and_version(self):
        path = self.root / "failed.sqlite3"
        original = PlatformStore._migration_2
        def fail(connection):
            original(connection)
            raise OSError("migration failed")
        with patch.object(PlatformStore, "_migration_2", side_effect=fail):
            with self.assertRaises(OSError):
                PlatformStore(path)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
        self.assertEqual(PlatformStore(path).schema_version, PlatformStore.SCHEMA_VERSION)

    def test_legacy_and_newer_databases_are_refused_without_writes(self):
        for name, version in (("store.sqlite3", 0), ("future.sqlite3", 999)):
            path = self.root / name
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE store(id INTEGER PRIMARY KEY, blob BLOB)")
                connection.execute(f"PRAGMA user_version = {version}")
            original = path.read_bytes()
            with patch("app.platform_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises(RuntimeError):
                    PlatformStore(path)
                self.assertEqual(connect.call_count, 1)
                self.assertIn("mode=ro", connect.call_args.args[0])
            self.assertEqual(path.read_bytes(), original)

    def test_database_path_uses_one_server_relative_root(self):
        from types import SimpleNamespace
        self.assertEqual(resolve_platform_db_path(SimpleNamespace(data_dir=str(self.root))), self.path)
        expected = Path(__file__).resolve().parents[1] / "relative-test-data" / "platform.sqlite3"
        self.assertEqual(resolve_platform_db_path(SimpleNamespace(data_dir="relative-test-data")), expected)


if __name__ == "__main__":
    unittest.main()
