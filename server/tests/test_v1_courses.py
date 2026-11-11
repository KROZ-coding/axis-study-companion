import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="v1-courses-import-")
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


class V1CourseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="v1-courses-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = Settings(
            app_env="test", data_dir=str(self.root), persistence_enabled=False,
            demo_enabled=False, worker_enabled=False, cookie_secure=False, allowed_hosts=("testserver",),
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="",
        )
        self.app = create_app(self.config)
        self.client = self.enterContext(TestClient(
            self.app, headers={"Origin": "http://testserver"}, raise_server_exceptions=False,
        ))
        self.store = self.app.state.platform_store
        self.assertIs(self.store, self.app.state.runtime.platform)
        self.assertEqual(self.app.state.runtime.worker_status()["running"], 0)
        self.admin = self.user("admin", "admin")
        self.material_owner = self.user("materials", "teacher")
        self.owner = self.user("class-owner", "teacher")
        self.reader = self.user("reader", "teacher")
        self.manager = self.user("manager", "teacher")
        self.other = self.user("other", "teacher")
        self.course = self.store.create_course("Math", "2026", self.material_owner["id"])
        self.classroom = self.store.create_class(self.course["id"], "Class A", owner_teacher_id=self.owner["id"])
        self.other_class = self.store.create_class(self.course["id"], "Class B", owner_teacher_id=self.other["id"])
        self.student = self.user("student", "student")
        self.store.create_course_membership(self.course["id"], self.student["id"])
        self.store.create_class_membership(self.classroom["id"], self.student["id"])

    def user(self, username, role):
        return self.store.create_user(username, username, "test-hash", role)

    def as_user(self, user):
        token = "token-" + user["id"]
        self.store.delete_session(token)
        self.store.create_session(token, user["id"], 9_999_999_999)
        self.client.cookies.clear()
        self.client.cookies.set("sid", token)

    def audit_events(self, store, event_type):
        return [row for row in store.list_audit_events() if row["event_type"] == event_type]

    def code_rows(self, store, user_id):
        with store.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM activation_codes WHERE user_id = ? ORDER BY code_hash", (user_id,),
            )]

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

    def test_course_application_requires_admin_confirmation_and_returns_white_list(self):
        self.as_user(self.owner)
        application = self.client.post("/api/v1/course-applications", json={"name": "Physics", "term": "2027"})
        self.assertEqual(application.status_code, 202, application.text)
        self.assertEqual(application.json()["status"], "pending")
        self.assertNotIn("password_hash", application.text)
        self.assertEqual(len(self.store.list_courses()), 1)
        application_id = application.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/course-applications/{application_id}/review", json={"approve": True}).status_code, 403)
        self.as_user(self.admin)
        approved = self.client.post(f"/api/v1/course-applications/{application_id}/review", json={"approve": True, "note": "Confirmed"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "approved")
        course = self.store.get_course(approved.json()["course_id"])
        self.assertEqual(course["teacher_id"], self.owner["id"])
        self.assertEqual(self.client.post(f"/api/v1/course-applications/{application_id}/review", json={"approve": True}).status_code, 409)
        self.as_user(self.owner)
        courses = self.client.get("/api/v1/courses").json()
        self.assertEqual(set(courses[0]), {"id", "name", "term", "teacher_id"})
        self.assertEqual(self.client.get(f"/api/v1/courses/{course['id']}/classes").json(), [])

    def test_material_owner_does_not_see_all_classes_or_rosters(self):
        self.as_user(self.material_owner)
        self.assertEqual(self.client.get(f"/api/v1/courses/{self.course['id']}/classes").json(), [])
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.classroom['id']}/members").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/courses/{self.course['id']}", json={"name": "Updated Math", "term": "2027"}).status_code, 200)
        created = self.client.post(f"/api/v1/courses/{self.course['id']}/classes", json={"name": "Class C", "owner_teacher_id": self.owner["id"]})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(set(created.json()), {"id", "course_id", "name", "owner_teacher_id"})
        self.assertEqual(self.client.get(f"/api/v1/classes/{created.json()['id']}").status_code, 403)
        self.as_user(self.owner)
        self.assertEqual(self.client.patch(f"/api/v1/courses/{self.course['id']}", json={"name": "Denied", "term": "2027"}).status_code, 403)
        classes = self.client.get(f"/api/v1/courses/{self.course['id']}/classes").json()
        self.assertEqual({row["name"] for row in classes}, {"Class A", "Class C"})
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.other_class['id']}/members").status_code, 403)

    def test_read_manage_collaborators_have_separate_class_permissions(self):
        self.as_user(self.owner)
        class_id = self.classroom["id"]
        for teacher, level in ((self.reader, "read"), (self.manager, "manage")):
            response = self.client.put(f"/api/v1/classes/{class_id}/collaborators", json={"teacher_id": teacher["id"], "access_level": level})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(set(response.json()), {"teacher_id", "access_level"})
        self.as_user(self.reader)
        members = self.client.get(f"/api/v1/classes/{class_id}/members")
        self.assertEqual(members.status_code, 200, members.text)
        self.assertEqual(set(members.json()[0]), {"user_id", "username", "display_name", "status"})
        self.assertEqual(self.client.patch(f"/api/v1/classes/{class_id}", json={"name": "Denied"}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": self.student["id"]}).status_code, 403)
        self.as_user(self.manager)
        self.assertEqual(self.client.patch(f"/api/v1/classes/{class_id}", json={"name": "Renamed"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/classes/{class_id}/owner", json={"owner_teacher_id": self.manager["id"]}).status_code, 403)
        self.assertEqual(self.client.put(f"/api/v1/classes/{class_id}/collaborators", json={"teacher_id": self.other["id"], "access_level": "manage"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.other_class['id']}/members").status_code, 403)

    def test_membership_transfer_requires_both_scopes_and_preserves_account(self):
        self.as_user(self.owner)
        path = f"/api/v1/classes/{self.classroom['id']}/members/{self.student['id']}/transfer"
        before = self.store.get_user(self.student["id"])
        denied = self.client.post(path, json={"target_class_id": self.other_class["id"]})
        self.assertEqual(denied.status_code, 403)
        self.assertIsNotNone(self.store.get_class_membership(self.classroom["id"], self.student["id"]))
        self.as_user(self.admin)
        moved = self.client.post(path, json={"target_class_id": self.other_class["id"]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(set(moved.json()), {"course_id", "class_id", "user_id"})
        self.assertIsNone(self.store.get_class_membership(self.classroom["id"], self.student["id"]))
        self.assertIsNotNone(self.store.get_class_membership(self.other_class["id"], self.student["id"]))
        self.assertEqual(self.store.get_user(self.student["id"]), before)
        self.as_user(self.owner)
        self.assertEqual(self.client.patch(f"/api/v1/classes/{self.classroom['id']}/members/{self.student['id']}", json={"password": secrets.token_urlsafe(24)}).status_code, 405)

    def test_class_owner_handoff_removes_old_owner_even_if_material_owner(self):
        self.as_user(self.owner)
        response = self.client.post(f"/api/v1/classes/{self.classroom['id']}/owner", json={"owner_teacher_id": self.material_owner["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.classroom['id']}/members").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/auth/password-reset-codes", json={"user_id": self.student["id"]}).status_code, 403)
        self.as_user(self.material_owner)
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.classroom['id']}/members").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.other_class['id']}/members").status_code, 403)

    def test_material_collaboration_never_grants_class_learning_access(self):
        self.as_user(self.material_owner)
        granted = self.client.put(f"/api/v1/courses/{self.course['id']}/collaborators", json={"teacher_id": self.manager["id"], "access_level": "manage"})
        self.assertEqual(granted.status_code, 200, granted.text)
        self.as_user(self.manager)
        self.assertEqual(self.client.patch(f"/api/v1/courses/{self.course['id']}", json={"name": "New Math", "term": "2027"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/classes/{self.classroom['id']}/members").status_code, 403)

    def test_audit_failure_rolls_back_course_review_and_retry_creates_one_course(self):
        self.as_user(self.owner)
        response = self.client.post("/api/v1/course-applications", json={"name": "Biology", "term": "2027"})
        self.assertEqual(response.status_code, 202, response.text)
        application = response.json()
        before = self.store.list_course_applications()
        before_courses = self.store.list_courses()
        self.assertEqual(application["status"], "pending")
        self.assertIsNone(application["course_id"])
        self.assertIsNone(application["reviewed_by"])
        self.assertIsNone(application["reviewed_at"])
        self.assertEqual(application["review_note"], "")
        self.as_user(self.admin)
        path = f"/api/v1/course-applications/{application['id']}/review"
        payload = {"approve": True, "note": "Confirmed after recovery"}
        with self.audit_unavailable("course_application_approved") as inserted:
            reviewed = self.client.post(path, json=payload)
        self.assert_storage_unavailable(reviewed)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.list_courses(), before_courses)
        self.assertIsNone(reopened.get_course(inserted[0]["payload"]["course_id"]))
        self.assertEqual(reopened.list_course_applications(), before)
        self.assertEqual(self.audit_events(reopened, "course_application_approved"), [])
        retried = self.client.post(path, json=payload)
        self.assertEqual(retried.status_code, 200, retried.text)
        reviewed = retried.json()
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(reviewed["reviewed_by"], self.admin["id"])
        self.assertEqual(reviewed["review_note"], payload["note"])
        self.assertIsNotNone(reviewed["reviewed_at"])
        self.assertGreaterEqual(reviewed["reviewed_at"], application["created_at"])
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.list_course_applications(), [reviewed])
        course = reopened.get_course(reviewed["course_id"])
        self.assertIsNotNone(course)
        self.assertEqual((course["name"], course["term"], course["teacher_id"]), (
            application["name"], application["term"], self.owner["id"],
        ))
        self.assertEqual({row["id"] for row in reopened.list_courses()}, {self.course["id"], course["id"]})
        success = self.audit_events(reopened, "course_application_approved")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["actor_user_id"], self.admin["id"])
        self.assertEqual(success[0]["resource_id"], application["id"])
        self.assertEqual(success[0]["payload"], {"course_id": course["id"]})
        self.assertEqual(self.client.post(path, json=payload).status_code, 409)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.list_course_applications(), [reviewed])
        self.assertEqual(len(reopened.list_courses()), 2)
        self.assertEqual(self.audit_events(reopened, "course_application_approved"), success)

    def test_audit_failure_rolls_back_owner_authority_and_code_revocation_then_retries(self):
        class_id = self.classroom["id"]
        for teacher, level in ((self.material_owner, "manage"), (self.reader, "read")):
            self.store.set_class_collaborator(class_id, teacher["id"], level, granted_by=self.owner["id"])
        code = secrets.token_urlsafe(32)
        self.store.create_activation_code(code, self.student["id"], self.owner["id"], purpose="reset")
        before_class = self.store.get_class(class_id)
        before_collaborators = self.store.list_class_collaborators(class_id)
        before_codes = self.code_rows(self.store, self.student["id"])
        before_student = self.store.get_user(self.student["id"])
        self.as_user(self.owner)
        path = f"/api/v1/classes/{class_id}/owner"
        payload = {"owner_teacher_id": self.material_owner["id"]}
        with self.audit_unavailable("class_owner_transferred"):
            response = self.client.post(path, json=payload)
        self.assert_storage_unavailable(response)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.get_class(class_id), before_class)
        self.assertEqual(reopened.list_class_collaborators(class_id), before_collaborators)
        self.assertEqual(self.code_rows(reopened, self.student["id"]), before_codes)
        self.assertEqual(reopened.get_user(self.student["id"]), before_student)
        self.assertTrue(reopened.can_manage_class_permissions(self.owner["id"], class_id))
        self.assertFalse(reopened.can_manage_class_permissions(self.material_owner["id"], class_id))
        self.assertEqual(self.audit_events(reopened, "class_owner_transferred"), [])
        retried = self.client.post(path, json=payload)
        self.assertEqual(retried.status_code, 200, retried.text)
        reopened = PlatformStore(self.store.db_path)
        after_class = reopened.get_class(class_id)
        self.assertEqual(after_class["owner_teacher_id"], self.material_owner["id"])
        self.assertEqual(after_class["authorization_version"], before_class["authorization_version"] + 1)
        self.assertIsNone(reopened.get_class_collaborator(class_id, self.material_owner["id"]))
        self.assertIsNone(reopened.get_class_collaborator(class_id, self.owner["id"]))
        self.assertEqual(reopened.list_class_collaborators(class_id), [
            row for row in before_collaborators if row["teacher_id"] != self.material_owner["id"]
        ])
        self.assertFalse(reopened.can_access_class(self.owner["id"], class_id))
        self.assertTrue(reopened.can_manage_class_permissions(self.material_owner["id"], class_id))
        after_codes = self.code_rows(reopened, self.student["id"])
        self.assertEqual(len(after_codes), 1)
        self.assertIsNone(after_codes[0]["consumed_at"])
        self.assertIsNotNone(after_codes[0]["revoked_at"])
        self.assertEqual({key: value for key, value in after_codes[0].items() if key != "revoked_at"}, {
            key: value for key, value in before_codes[0].items() if key != "revoked_at"
        })
        self.assertEqual(reopened.get_user(self.student["id"]), before_student)
        success = self.audit_events(reopened, "class_owner_transferred")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["actor_user_id"], self.owner["id"])
        self.assertEqual(success[0]["resource_id"], class_id)
        self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/classes/{class_id}/members").status_code, 403)
        self.as_user(self.material_owner)
        self.assertEqual(self.client.get(f"/api/v1/classes/{class_id}/members").status_code, 200)
        self.client.cookies.clear()
        denied = self.client.post("/api/v1/auth/password-reset", json={
            "code": code, "password": secrets.token_urlsafe(20),
        })
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertEqual(denied.json()["detail"]["code"], "invalid_reset_code")
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.get_class(class_id), after_class)
        self.assertEqual(self.code_rows(reopened, self.student["id"]), after_codes)
        self.assertEqual(reopened.get_user(self.student["id"]), before_student)
        self.assertEqual(self.audit_events(reopened, "class_owner_transferred"), success)

    def test_audit_failure_rolls_back_cross_course_membership_and_codes_then_retries(self):
        target_course = self.store.create_course("Chemistry", "2027", self.other["id"])
        target_class = self.store.create_class(target_course["id"], "Class C", owner_teacher_id=self.other["id"])
        code = secrets.token_urlsafe(32)
        self.store.create_activation_code(code, self.student["id"], self.owner["id"], purpose="reset")
        before_user = self.store.get_user(self.student["id"])
        before_course_member = self.store.get_course_membership(self.course["id"], self.student["id"])
        before_class_member = self.store.get_class_membership(self.classroom["id"], self.student["id"])
        before_codes = self.code_rows(self.store, self.student["id"])
        self.as_user(self.admin)
        path = f"/api/v1/classes/{self.classroom['id']}/members/{self.student['id']}/transfer"
        payload = {"target_class_id": target_class["id"]}
        with self.audit_unavailable("class_member_transferred"):
            response = self.client.post(path, json=payload)
        self.assert_storage_unavailable(response)
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.get_user(self.student["id"]), before_user)
        self.assertEqual(reopened.get_course_membership(self.course["id"], self.student["id"]), before_course_member)
        self.assertEqual(reopened.get_class_membership(self.classroom["id"], self.student["id"]), before_class_member)
        self.assertIsNone(reopened.get_course_membership(target_course["id"], self.student["id"]))
        self.assertIsNone(reopened.get_class_membership(target_class["id"], self.student["id"]))
        self.assertEqual(self.code_rows(reopened, self.student["id"]), before_codes)
        self.assertEqual(self.audit_events(reopened, "class_member_transferred"), [])
        retried = self.client.post(path, json=payload)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json(), {
            "course_id": target_course["id"], "class_id": target_class["id"], "user_id": self.student["id"],
        })
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.get_user(self.student["id"]), before_user)
        self.assertEqual(reopened.get_course_membership(self.course["id"], self.student["id"]), before_course_member)
        self.assertIsNone(reopened.get_class_membership(self.classroom["id"], self.student["id"]))
        target_course_member = reopened.get_course_membership(target_course["id"], self.student["id"])
        target_class_member = reopened.get_class_membership(target_class["id"], self.student["id"])
        self.assertIsNotNone(target_course_member)
        self.assertIsNotNone(target_class_member)
        self.assertEqual(target_class_member["course_id"], target_course["id"])
        self.assertEqual(target_class_member["user_id"], self.student["id"])
        after_codes = self.code_rows(reopened, self.student["id"])
        self.assertEqual(len(after_codes), 1)
        self.assertIsNone(after_codes[0]["consumed_at"])
        self.assertIsNotNone(after_codes[0]["revoked_at"])
        self.assertEqual({key: value for key, value in after_codes[0].items() if key != "revoked_at"}, {
            key: value for key, value in before_codes[0].items() if key != "revoked_at"
        })
        success = self.audit_events(reopened, "class_member_transferred")
        self.assertEqual(len(success), 1)
        self.assertEqual(success[0]["actor_user_id"], self.admin["id"])
        self.assertEqual(success[0]["resource_id"], self.student["id"])
        self.assertEqual(self.client.post(path, json=payload).status_code, 404)
        self.client.cookies.clear()
        denied = self.client.post("/api/v1/auth/password-reset", json={
            "code": code, "password": secrets.token_urlsafe(20),
        })
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertEqual(denied.json()["detail"]["code"], "invalid_reset_code")
        reopened = PlatformStore(self.store.db_path)
        self.assertEqual(reopened.get_user(self.student["id"]), before_user)
        self.assertEqual(reopened.get_course_membership(target_course["id"], self.student["id"]), target_course_member)
        self.assertEqual(reopened.get_class_membership(target_class["id"], self.student["id"]), target_class_member)
        self.assertEqual(self.code_rows(reopened, self.student["id"]), after_codes)
        self.assertEqual(self.audit_events(reopened, "class_member_transferred"), success)

    def test_members_admin_add_and_manager_remove_do_not_edit_student_profile(self):
        unassigned = self.user("unassigned", "student")
        self.as_user(self.owner)
        self.assertEqual(self.client.post(f"/api/v1/classes/{self.classroom['id']}/members", json={"user_id": unassigned["id"]}).status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.client.post(f"/api/v1/classes/{self.classroom['id']}/members", json={"user_id": unassigned["id"]}).status_code, 201)
        self.as_user(self.owner)
        self.assertEqual(self.client.delete(f"/api/v1/classes/{self.classroom['id']}/members/{unassigned['id']}").status_code, 204)
        self.assertEqual(self.store.get_user(unassigned["id"]), unassigned)
        self.assertEqual(self.client.patch(f"/api/v1/classes/{self.classroom['id']}", json={"name": "x", "owner_teacher_id": self.other["id"]}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
