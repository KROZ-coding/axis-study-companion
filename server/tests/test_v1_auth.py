import atexit
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="v1-auth-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name,
    STUDY_DATA_DIR=_IMPORT_DATA.name, PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false",
    STUDY_DEMO_ENABLED="false", STUDY_WORKER_ENABLED="false",
    LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
)

from fastapi.testclient import TestClient

with patch("sqlite3.connect", side_effect=AssertionError("module import must not open SQLite")):
    from app.config import Settings
    from app.main import create_app
    from app.platform_store import PlatformStore
    from app.routers import v1_auth
    from app.utils.security import hash_password, verify_password
    from app.v1_deps import get_platform_store


class V1AuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="v1-auth-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = Settings(app_env="test", persistence_enabled=False, data_dir=str(self.root), cookie_secure=False,
                               demo_enabled=False, worker_enabled=False,
                               llm_providers=(), llm_base_url="", llm_api_key="", llm_model="")
        self.app = create_app(self.config)
        self.client = self.client_for(self.app)
        self.client.get("/api/v1/auth/me")
        self.store = self.app.state.platform_store
        self.password = secrets.token_urlsafe(20)
        self.password_hash = hash_password(self.password)
        self.admin = self.user("admin", "admin")
        self.owner = self.user("owner", "teacher")
        self.material_owner = self.user("materials", "teacher")
        self.course = self.store.create_course("Math", "2026", self.material_owner["id"])
        self.classroom = self.store.create_class(self.course["id"], "Section A", owner_teacher_id=self.owner["id"])

    def client_for(self, app, *, secure=False):
        origin = "https://testserver" if secure else "http://testserver"
        client = TestClient(app, base_url=origin, headers={"Origin": origin}, raise_server_exceptions=False)
        return self.enterContext(client)

    def user(self, username, role="student", status="active"):
        return self.store.create_user(username, username, self.password_hash, role, status=status)

    def as_user(self, user, client=None):
        client = client or self.client
        token = secrets.token_urlsafe(24)
        self.store.create_session(token, user["id"], 9_999_999_999)
        client.cookies.clear()
        client.cookies.set("sid", token)
        return token

    def member(self, username="student", *, status="active"):
        student = self.user(username, status=status)
        self.store.create_course_membership(self.course["id"], student["id"])
        self.store.create_class_membership(self.classroom["id"], student["id"])
        return student

    def assert_hash_outside_transaction(self, password):
        self.assertIsNone(getattr(self.store._local, "connection", None))
        return hash_password(password)

    def account_state(self, store, user_id):
        result = {"user": store.get_user(user_id)}
        with store.connect() as connection:
            for table, query in (
                ("sessions", "SELECT * FROM sessions WHERE user_id = ? ORDER BY token_hash"),
                ("activation_codes", "SELECT * FROM activation_codes WHERE user_id = ? ORDER BY code_hash"),
                ("course_memberships", "SELECT * FROM course_memberships WHERE user_id = ? ORDER BY course_id"),
                ("class_memberships", "SELECT * FROM class_memberships WHERE user_id = ? ORDER BY class_id"),
            ):
                result[table] = [dict(row) for row in connection.execute(query, (user_id,))]
        return result

    def audit_events(self, store, event_type):
        return [row for row in store.list_audit_events() if row["event_type"] == event_type]

    @contextmanager
    def audit_unavailable(self, target_event):
        original = self.store.record_audit_event
        inserted = []
        def unavailable(event_type, *args, **kwargs):
            event = original(event_type, *args, **kwargs)
            if event_type == target_event:
                inserted.append(event)
                raise sqlite3.OperationalError("audit unavailable")
            return event
        with patch.object(self.store, "record_audit_event", side_effect=unavailable):
            yield inserted
        self.assertEqual(len(inserted), 1, "fault must occur after the target audit row is inserted")

    def assert_storage_unavailable(self, response):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": {
            "code": "storage_unavailable", "message": "记录暂未可靠保存，请保留输入并重试", "retryable": True,
        }})

    def assert_code_attempts(self, store, operation, code, count):
        # The rate gate commits separately before code redemption starts.
        attempts = self.audit_events(store, "auth_attempt")
        self.assertEqual(len(attempts), count)
        self.assertTrue(all(row["payload"] == {"operation": operation} for row in attempts))
        with store.connect() as connection:
            for scope, subject in ((operation + ":subject", code), (operation + ":ip", "testclient")):
                row = connection.execute(
                    "SELECT attempts FROM auth_rate_limits WHERE scope = ? AND subject_hash = ?",
                    (scope, store._token_hash(subject)),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["attempts"], count)

    def test_teacher_application_admin_approval_and_white_list_cookie_login(self):
        with patch.object(v1_auth, "hash_password", side_effect=self.assert_hash_outside_transaction):
            result = self.client.post("/api/v1/auth/teacher-applications", json={
                "username": "applicant", "display_name": "申请教师", "password": self.password,
            })
        self.assertEqual(result.status_code, 202, result.text)
        user_id = result.json()["id"]
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "applicant", "password": self.password}).status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.client.post(f"/api/v1/auth/teacher-applications/{user_id}/approve").status_code, 200)
        self.client.cookies.clear()
        login = self.client.post("/api/v1/auth/login", json={"username": "applicant", "password": self.password})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(set(login.json()["user"]), {"id", "username", "display_name", "role", "status"})
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertNotIn("Secure;", login.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)

    def test_student_activates_and_course_scoped_reset_works_without_consumption_scope(self):
        self.as_user(self.owner)
        with patch.object(v1_auth, "hash_password", side_effect=self.assert_hash_outside_transaction):
            created = self.client.post("/api/v1/auth/students", json={
                "username": "student", "display_name": "Student", "course_id": self.course["id"], "class_id": self.classroom["id"],
            })
        self.assertEqual(created.status_code, 201, created.text)
        data = created.json()
        self.client.cookies.clear()
        with patch.object(v1_auth, "hash_password", side_effect=self.assert_hash_outside_transaction):
            response = self.client.post("/api/v1/auth/activate", json={"code": data["activation_code"], "password": self.password})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.client.post("/api/v1/auth/activate", json={"code": data["activation_code"], "password": self.password}).status_code, 400)
        student = self.store.get_user(data["student_id"])
        old_token = self.as_user(student)
        self.as_user(self.owner)
        issued = self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": student["id"], "course_id": self.course["id"]})
        self.assertEqual(issued.status_code, 201, issued.text)
        replacement = secrets.token_urlsafe(20)
        self.client.cookies.clear()
        with patch.object(v1_auth, "hash_password", side_effect=self.assert_hash_outside_transaction):
            reset = self.client.post("/api/v1/auth/password-reset", json={"code": issued.json()["reset_code"], "password": replacement})
        self.assertEqual(reset.status_code, 204, reset.text)
        self.assertIsNone(self.store.get_session(old_token))
        self.assertTrue(verify_password(replacement, self.store.get_user(student["id"])["password_hash"]))

    def test_admin_can_reset_all_roles_teacher_cannot_and_disabled_cannot_activate(self):
        student = self.member()
        self.as_user(self.admin)
        for target in (student, self.owner, self.admin):
            with self.subTest(role=target["role"]):
                issued = self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": target["id"]})
                self.assertEqual(issued.status_code, 201, issued.text)
                self.assertEqual(self.client.post("/api/v1/auth/password-reset", json={
                    "code": issued.json()["reset_code"], "password": self.password,
                }).status_code, 204)
                self.as_user(self.admin)
        self.as_user(self.owner)
        self.assertEqual(self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": self.material_owner["id"]}).status_code, 403)
        pending = self.member("pending", status="pending")
        code = secrets.token_urlsafe(32)
        self.store.create_activation_code(code, pending["id"], self.owner["id"])
        self.store.update_user(pending["id"], status="disabled")
        self.client.cookies.clear()
        result = self.client.post("/api/v1/auth/activate", json={"code": code, "password": self.password})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.store.get_user(pending["id"])["status"], "disabled")

    def test_course_material_owner_has_no_class_or_student_creation_permission(self):
        self.as_user(self.material_owner)
        response = self.client.post("/api/v1/auth/students", json={
            "username": "denied-student", "display_name": "Denied", "course_id": self.course["id"], "class_id": self.classroom["id"],
        })
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.store.get_user_by_username("denied-student"))
        self.as_user(self.owner)
        # No teacher-provided password or profile edits are accepted.
        response = self.client.post("/api/v1/auth/students", json={
            "username": "denied-student", "display_name": "Denied", "password": "teacher-selected-password",
            "course_id": self.course["id"], "class_id": self.classroom["id"],
        })
        self.assertEqual(response.status_code, 422)

    def test_revocation_after_code_issue_prevents_redemption(self):
        manager = self.user("manager", "teacher")
        student = self.member()
        self.store.set_class_collaborator(self.classroom["id"], manager["id"], "manage", granted_by=self.owner["id"])
        self.as_user(manager)
        issued = self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": student["id"]})
        self.assertEqual(issued.status_code, 201, issued.text)
        self.store.remove_class_collaborator(self.classroom["id"], manager["id"], removed_by=self.owner["id"])
        self.client.cookies.clear()
        result = self.client.post("/api/v1/auth/password-reset", json={"code": issued.json()["reset_code"], "password": self.password})
        self.assertEqual(result.status_code, 400)

    def test_audit_failure_rolls_back_student_creation_and_retry_persists_once(self):
        owner_token = self.as_user(self.owner)
        payload = {
            "username": "rollback-student", "display_name": "Rollback",
            "course_id": self.course["id"], "class_id": self.classroom["id"],
        }
        with self.audit_unavailable("student_created") as inserted:
            response = self.client.post("/api/v1/auth/students", json=payload)
        self.assert_storage_unavailable(response)
        reopened = PlatformStore(self.store.db_path)
        failed_student_id = inserted[0]["resource_id"]
        self.assertIsNone(reopened.get_user_by_username(payload["username"]))
        self.assertEqual(self.account_state(reopened, failed_student_id), {
            "user": None, "sessions": [], "activation_codes": [],
            "course_memberships": [], "class_memberships": [],
        })
        self.assertEqual(self.audit_events(reopened, "student_created"), [])
        self.assertIsNotNone(reopened.get_session(owner_token))
        retried = self.client.post("/api/v1/auth/students", json=payload)
        self.assertEqual(retried.status_code, 201, retried.text)
        created = retried.json()
        state = self.account_state(PlatformStore(self.store.db_path), created["student_id"])
        self.assertEqual(state["user"]["username"], payload["username"])
        self.assertEqual(state["user"]["status"], "pending")
        self.assertEqual(state["user"]["authorization_version"], 1)
        self.assertNotEqual(state["user"]["password_hash"], self.password_hash)
        self.assertEqual(state["sessions"], [])
        for table, field, expected in (
            ("course_memberships", "course_id", self.course["id"]),
            ("class_memberships", "class_id", self.classroom["id"]),
        ):
            self.assertEqual(len(state[table]), 1)
            self.assertEqual(state[table][0][field], expected)
            self.assertEqual(state[table][0]["user_id"], created["student_id"])
        self.assertEqual(len(state["activation_codes"]), 1)
        code = state["activation_codes"][0]
        self.assertEqual(code["code_hash"], self.store._token_hash(created["activation_code"]))
        self.assertEqual(code["purpose"], "activation")
        self.assertEqual(code["class_id"], self.classroom["id"])
        self.assertEqual(code["class_authorization_version"], self.classroom["authorization_version"])
        self.assertIsNone(code["consumed_at"])
        self.assertIsNone(code["revoked_at"])
        success = self.audit_events(PlatformStore(self.store.db_path), "student_created")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["actor_user_id"], self.owner["id"])
        self.assertEqual(success[0]["resource_id"], created["student_id"])
        self.assertEqual(self.client.post("/api/v1/auth/students", json=payload).status_code, 409)
        self.assertEqual(self.account_state(PlatformStore(self.store.db_path), created["student_id"]), state)
        self.assertEqual(self.audit_events(PlatformStore(self.store.db_path), "student_created"), success)

    def test_audit_failure_rolls_back_activation_and_code_retries_once(self):
        student = self.member("pending", status="pending")
        token, spare_code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for code in (token, spare_code):
            self.store.create_activation_code(code, student["id"], self.owner["id"])
        for _ in range(2):
            self.store.create_session(secrets.token_urlsafe(24), student["id"], 9_999_999_999)
        before = self.account_state(self.store, student["id"])
        replacement = secrets.token_urlsafe(20)
        payload = {"code": token, "password": replacement}
        with self.audit_unavailable("account_activated"):
            response = self.client.post("/api/v1/auth/activate", json=payload)
        self.assert_storage_unavailable(response)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(self.account_state(reopened, student["id"]), before)
        self.assertTrue(verify_password(self.password, reopened.get_user(student["id"])["password_hash"]))
        self.assertEqual(self.audit_events(reopened, "account_activated"), [])
        self.assert_code_attempts(reopened, "activation", token, 1)
        retried = self.client.post("/api/v1/auth/activate", json=payload)
        self.assertEqual(retried.status_code, 204, retried.text)
        reopened = PlatformStore(self.store.db_path)
        after = self.account_state(reopened, student["id"])
        self.assertEqual(after["user"]["status"], "active")
        self.assertEqual(after["user"]["authorization_version"], before["user"]["authorization_version"] + 1)
        self.assertTrue(verify_password(replacement, after["user"]["password_hash"]))
        self.assertFalse(verify_password(self.password, after["user"]["password_hash"]))
        self.assertEqual(after["sessions"], [])
        for table in ("course_memberships", "class_memberships"):
            self.assertEqual(after[table], before[table])
        codes = {row["code_hash"]: row for row in after["activation_codes"]}
        self.assertEqual(len(codes), 2)
        self.assertIsNotNone(codes[self.store._token_hash(token)]["consumed_at"])
        self.assertIsNone(codes[self.store._token_hash(token)]["revoked_at"])
        self.assertIsNone(codes[self.store._token_hash(spare_code)]["consumed_at"])
        self.assertIsNotNone(codes[self.store._token_hash(spare_code)]["revoked_at"])
        success = self.audit_events(reopened, "account_activated")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["resource_id"], student["id"])
        self.assert_code_attempts(reopened, "activation", token, 2)
        replay = self.client.post("/api/v1/auth/activate", json={"code": token, "password": self.password})
        self.assertEqual(replay.status_code, 400, replay.text)
        self.assertEqual(replay.json()["detail"]["code"], "invalid_activation_code")
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(self.account_state(reopened, student["id"]), after)
        self.assertEqual(self.audit_events(reopened, "account_activated"), success)
        self.assert_code_attempts(reopened, "activation", token, 3)

    def test_audit_failure_rolls_back_password_reset_and_code_retries_once(self):
        student = self.member()
        self.as_user(self.owner)
        issued = self.client.post("/api/v1/auth/password-reset-codes", json={
            "user_id": student["id"], "class_id": self.classroom["id"],
        })
        self.assertEqual(issued.status_code, 201, issued.text)
        token = issued.json()["reset_code"]
        spare_code = secrets.token_urlsafe(32)
        self.store.create_activation_code(spare_code, student["id"], self.owner["id"], purpose="reset")
        old_token = self.as_user(student)
        self.store.create_session(secrets.token_urlsafe(24), student["id"], 9_999_999_999)
        before = self.account_state(self.store, student["id"])
        replacement = secrets.token_urlsafe(20)
        payload = {"code": token, "password": replacement}
        self.client.cookies.clear()
        with self.audit_unavailable("password_reset_completed"):
            response = self.client.post("/api/v1/auth/password-reset", json=payload)
        self.assert_storage_unavailable(response)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(self.account_state(reopened, student["id"]), before)
        self.assertTrue(verify_password(self.password, reopened.get_user(student["id"])["password_hash"]))
        self.assertIsNotNone(reopened.get_session(old_token))
        self.assertEqual(self.audit_events(reopened, "password_reset_completed"), [])
        self.assert_code_attempts(reopened, "reset", token, 1)
        retried = self.client.post("/api/v1/auth/password-reset", json=payload)
        self.assertEqual(retried.status_code, 204, retried.text)
        reopened = PlatformStore(self.store.db_path)
        after = self.account_state(reopened, student["id"])
        self.assertEqual(after["user"]["status"], "active")
        self.assertEqual(after["user"]["authorization_version"], before["user"]["authorization_version"] + 1)
        self.assertTrue(verify_password(replacement, after["user"]["password_hash"]))
        self.assertFalse(verify_password(self.password, after["user"]["password_hash"]))
        self.assertEqual(after["sessions"], [])
        self.assertIsNone(reopened.get_session(old_token))
        for table in ("course_memberships", "class_memberships"):
            self.assertEqual(after[table], before[table])
        codes = {row["code_hash"]: row for row in after["activation_codes"]}
        self.assertEqual(len(codes), 2)
        self.assertIsNotNone(codes[self.store._token_hash(token)]["consumed_at"])
        self.assertIsNone(codes[self.store._token_hash(token)]["revoked_at"])
        self.assertIsNone(codes[self.store._token_hash(spare_code)]["consumed_at"])
        self.assertIsNotNone(codes[self.store._token_hash(spare_code)]["revoked_at"])
        success = self.audit_events(reopened, "password_reset_completed")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["resource_id"], student["id"])
        self.assert_code_attempts(reopened, "reset", token, 2)
        replay = self.client.post("/api/v1/auth/password-reset", json={"code": token, "password": self.password})
        self.assertEqual(replay.status_code, 400, replay.text)
        self.assertEqual(replay.json()["detail"]["code"], "invalid_reset_code")
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(self.account_state(reopened, student["id"]), after)
        self.assertEqual(self.audit_events(reopened, "password_reset_completed"), success)
        self.assert_code_attempts(reopened, "reset", token, 3)

    def test_login_application_and_activation_rate_limits_survive_application_restart(self):
        config = replace(self.config, login_failure_limit=2, registration_rate_limit=2)
        client = self.client_for(create_app(config))
        for _ in range(2):
            self.assertEqual(client.post("/api/v1/auth/login", json={"username": "missing", "password": self.password}).status_code, 401)
            self.assertEqual(client.post("/api/v1/auth/activate", json={"code": "invalid-activation-token", "password": self.password}).status_code, 400)
        for index in range(2):
            self.assertEqual(client.post("/api/v1/auth/teacher-applications", json={
                "username": "application" + str(index), "display_name": "Teacher", "password": self.password,
            }).status_code, 202)
        reopened = self.client_for(create_app(config))
        for url, data in (
            ("login", {"username": "missing", "password": self.password}),
            ("activate", {"code": "invalid-activation-token", "password": self.password}),
            ("teacher-applications", {"username": "application2", "display_name": "Teacher", "password": self.password}),
        ):
            result = reopened.post("/api/v1/auth/" + url, json=data)
            self.assertEqual(result.status_code, 429, result.text)
            self.assertIn("retry-after", result.headers)

    def test_cookie_configuration_comes_from_request_not_store_mutation(self):
        secure_app = create_app(replace(self.config, cookie_secure=True, session_ttl_seconds=77))
        secure_app.dependency_overrides[get_platform_store] = lambda: self.store
        client = self.client_for(secure_app, secure=True)
        result = client.post("/api/v1/auth/login", json={"username": "owner", "password": self.password})
        self.assertEqual(result.status_code, 200, result.text)
        cookie = result.headers["set-cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("Max-Age=77", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
        self.assertEqual(client.post("/api/v1/auth/logout").status_code, 204)
        self.assertFalse(hasattr(self.store, "cookie_secure"))

    def test_change_password_hashes_outside_transaction_and_revokes_other_sessions(self):
        student = self.member()
        token = self.as_user(student)
        new_password = secrets.token_urlsafe(20)
        original_verify = v1_auth.verify_password
        def verify_unlocked(*args):
            self.assertIsNone(getattr(self.store._local, "connection", None))
            return original_verify(*args)
        with patch.object(v1_auth, "hash_password", side_effect=self.assert_hash_outside_transaction), patch.object(v1_auth, "verify_password", side_effect=verify_unlocked):
            response = self.client.post("/api/v1/auth/change-password", json={"current_password": self.password, "new_password": new_password})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertIsNone(self.store.get_session(token))
        self.assertTrue(verify_password(new_password, self.store.get_user(student["id"])["password_hash"]))

    def test_default_demo_routes_are_disabled(self):
        self.assertEqual(self.client.get("/api/study/sample").status_code, 404)


if __name__ == "__main__":
    unittest.main()
