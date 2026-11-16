"""Management contracts against real repositories, with no upstream transport."""

import atexit
from dataclasses import asdict, replace
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="management-import-")
atexit.register(_IMPORT_DATA.cleanup)
for _key in tuple(os.environ):
    if _key.startswith(("LLM_", "MAXKB_")):
        os.environ.pop(_key)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name,
    STUDY_DATA_DIR=_IMPORT_DATA.name, PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false",
    STUDY_DEMO_ENABLED="false", STUDY_WORKER_ENABLED="false",
    LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
    MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
)

from fastapi.testclient import TestClient

with patch("sqlite3.connect", side_effect=AssertionError("application import must not open SQLite")):
    from app.config import Settings
    from app.job_queue import InsufficientBudget
    from app.main import create_app
    from app.platform_store import PlatformStore
    from app.routers import v1_auth, v1_jobs


ACCOUNT_FIELDS = {"id", "username", "display_name", "role", "status"}
COURSE_FIELDS = {"id", "name", "term", "teacher_id", "can_manage_materials", "can_create_classes", "can_manage_collaborators"}
CLASS_FIELDS = {"id", "course_id", "name", "owner_teacher_id", "can_manage", "can_manage_permissions", "can_set_student_budgets"}
LIMIT_FIELDS = {
    "scope_type", "scope_id", "period", "limit_units", "emergency_units", "spent_units", "reserved_units",
    "overage_units", "emergency_exceeded_units", "notification_required", "course_id", "class_id", "configured",
}


class ManagementApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="management-api-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.network_mocks = []
        for target in (
            "socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
            "httpx.AsyncHTTPTransport.handle_async_request",
        ):
            self.network_mocks.append(self.enterContext(patch(target, side_effect=AssertionError("external network is forbidden"))))
        self.settings = Settings(
            app_env="test", data_dir=str(self.root), persistence_enabled=False,
            demo_enabled=False, worker_enabled=False, cookie_secure=False, allowed_hosts=("testserver",),
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="", llm_prices_json="",
            maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(), maxkb_course_dataset_ids=(),
            budget_currency="site-credit",
        )
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(
            self.app, headers={"Origin": "http://testserver"}, raise_server_exceptions=False,
        ))
        self.runtime = self.app.state.runtime
        self.store = self.runtime.platform
        self.ledger = self.runtime.queue.budget_ledger
        self.assertEqual(self.runtime.worker_status()["running"], 0)
        self.admin = self.user("admin", "admin")
        self.material_owner = self.user("materials", "teacher")
        self.owner = self.user("class-owner", "teacher")
        self.reader = self.user("class-reader", "teacher")
        self.manager = self.user("class-manager", "teacher")
        self.course_reader = self.user("course-reader", "teacher")
        self.course_manager = self.user("course-manager", "teacher")
        self.other = self.user("other-owner", "teacher")
        self.course = self.store.create_course("数学", "2026", self.material_owner["id"])
        self.classroom = self.store.create_class(self.course["id"], "一班", owner_teacher_id=self.owner["id"])
        self.other_class = self.store.create_class(self.course["id"], "二班", owner_teacher_id=self.other["id"])
        self.student = self.member("student", self.classroom)
        self.pending = self.member("pending-student", self.classroom, status="pending")
        self.outsider = self.member("outside-student", self.other_class)
        for teacher, level in ((self.reader, "read"), (self.manager, "manage")):
            self.store.set_class_collaborator(self.classroom["id"], teacher["id"], level, granted_by=self.owner["id"])
        for teacher, level in ((self.course_reader, "read"), (self.course_manager, "manage")):
            self.store.set_course_collaborator(self.course["id"], teacher["id"], level, granted_by=self.material_owner["id"])

    def tearDown(self):
        for transport in self.network_mocks:
            transport.assert_not_called()

    def user(self, username, role="student", status="active", display_name=None):
        return self.store.create_user(
            username, display_name or username, "fixture-password-hash-not-for-exposure", role, status=status,
            phone=f"private-phone-{username}", student_number=f"private-student-number-{username}",
        )

    def member(self, username, classroom, status="active"):
        user = self.user(username, status=status)
        self.store.create_course_membership(classroom["course_id"], user["id"])
        self.store.create_class_membership(classroom["id"], user["id"])
        return user

    def as_user(self, user):
        token = secrets.token_urlsafe(24)
        self.store.create_session(token, user["id"], 9_999_999_999)
        self.client.cookies.clear()
        self.client.cookies.set("sid", token)
        return token

    def expect(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json() if response.content else None

    def budget_payload(self, **changes):
        return {"scope_type": "student_daily", "scope_id": self.student["id"],
                "class_id": self.classroom["id"], "limit_units": 120, **changes}

    def seed_limits(self):
        self.ledger.set_limit("project", "platform", 1000, emergency_units=30)
        self.ledger.set_limit("teaching", self.classroom["id"], 200, emergency_units=10)
        self.ledger.set_limit("student_daily", self.student["id"], 80)
        self.ledger.set_limit("student_daily", self.student["id"], 70, course_id=self.course["id"])
        for student, classroom, units in (
            (self.student, self.classroom, 60), (self.pending, self.classroom, 0),
            (self.outsider, self.other_class, 50),
        ):
            self.ledger.set_limit("student_daily", student["id"], units,
                                  course_id=classroom["course_id"], class_id=classroom["id"])
        self.ledger.set_limit("student_daily", self.student["id"], 40, period="2026-11-16",
                              course_id=self.course["id"], class_id=self.classroom["id"])

    def test_accounts_are_admin_only_and_disabled_sessions_are_rejected(self):
        self.expect(self.client.get("/api/v1/auth/accounts"), 401)
        for actor in (self.owner, self.reader, self.manager, self.material_owner, self.student):
            with self.subTest(role=actor["username"]):
                self.as_user(actor)
                self.expect(self.client.get("/api/v1/auth/accounts"), 403)
        self.as_user(self.admin)
        self.store.update_user(self.admin["id"], status="disabled")
        self.expect(self.client.get("/api/v1/auth/accounts"), 401)

    def test_account_pagination_filters_count_and_five_field_projection(self):
        expected = [self.user("page-a", "teacher", display_name="分页甲"),
                    self.user("PAGE-b", "teacher", display_name="分页乙"),
                    self.user("page-c", "teacher", display_name="分页丙")]
        self.user("page-disabled", "teacher", status="disabled")
        self.user("page-student")
        self.as_user(self.admin)
        params = {"query": "  PAGE- ", "role": "teacher", "status": "active", "limit": 2, "offset": 1}
        result = self.expect(self.client.get("/api/v1/auth/accounts", params=params))
        self.assertEqual(set(result), {"items", "total", "limit", "offset"})
        self.assertEqual((result["total"], result["limit"], result["offset"]), (3, 2, 1))
        self.assertEqual([row["id"] for row in result["items"]], [row["id"] for row in expected[1:]])
        for row in result["items"]:
            self.assertEqual(set(row), ACCOUNT_FIELDS)
        empty_page = self.expect(self.client.get("/api/v1/auth/accounts", params={**params, "offset": 50}))
        self.assertEqual((empty_page["items"], empty_page["total"]), ([], 3))
        all_rows = self.expect(self.client.get("/api/v1/auth/accounts", params={"role": "", "status": ""}))
        self.assertEqual(all_rows["total"], len(self.store.list_users()))
        self.assertEqual((all_rows["limit"], all_rows["offset"]), (50, 0))
        by_name = self.expect(self.client.get("/api/v1/auth/accounts", params={"query": "分页乙"}))
        self.assertEqual([row["id"] for row in by_name["items"]], [expected[1]["id"]])
        by_id = self.expect(self.client.get("/api/v1/auth/accounts", params={"query": expected[0]["id"]}))
        self.assertEqual([row["id"] for row in by_id["items"]], [expected[0]["id"]])

    def test_account_search_is_literal_and_does_not_search_private_fields(self):
        special = self.user("literal_name", display_name="Literal 100% ! unique")
        self.as_user(self.admin)
        for query in ("_", "%", "!", "100% !"):
            with self.subTest(query=query):
                result = self.expect(self.client.get("/api/v1/auth/accounts", params={"query": query}))
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["id"], special["id"])
        for query in ("' OR 1=1 --", "private-phone", "private-student-number", "fixture-password-hash-not-for-exposure"):
            result = self.expect(self.client.get("/api/v1/auth/accounts", params={"query": query}))
            self.assertEqual(result["items"], [])
            self.assertEqual(result["total"], 0)

    def test_account_query_validation_is_bounded(self):
        self.as_user(self.admin)
        for params in ({"query": "x" * 201}, {"role": "owner"}, {"status": "approved"},
                       {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 2**63}):
            with self.subTest(params=params):
                self.expect(self.client.get("/api/v1/auth/accounts", params=params), 422)

    def test_last_admin_protection_and_status_revokes_existing_session(self):
        self.as_user(self.admin)
        denied = self.expect(self.client.patch(f"/api/v1/auth/users/{self.admin['id']}/status", json={"status": "disabled"}), 409)
        self.assertEqual(denied["detail"]["code"], "last_admin")
        self.assertEqual(self.store.get_user(self.admin["id"])["status"], "active")
        second = self.user("second-admin", "admin")
        old_token = self.as_user(second)
        self.as_user(self.admin)
        changed = self.expect(self.client.patch(f"/api/v1/auth/users/{second['id']}/status", json={"status": "disabled"}))
        self.assertEqual(set(changed), ACCOUNT_FIELDS)
        self.assertIsNone(self.store.get_session(old_token))
        self.expect(self.client.patch(f"/api/v1/auth/users/{self.admin['id']}/status", json={"status": "disabled"}), 409)
        self.expect(self.client.patch(f"/api/v1/auth/users/{self.pending['id']}/status", json={"status": "active"}), 409)

    def test_teacher_review_note_is_persisted_and_body_remains_optional(self):
        self.as_user(self.admin)
        for index, (action, body, expected_note, status) in enumerate((
            ("approve", None, "", "active"), ("reject", None, "", "disabled"),
            ("reject", {"note": "  请补充教师身份材料  "}, "请补充教师身份材料", "disabled"),
            ("approve", {"note": "身份已核验"}, "身份已核验", "active"),
        )):
            applicant = self.user(f"applicant-{index}", "teacher", status="pending")
            kwargs = {} if body is None else {"json": body}
            result = self.expect(self.client.post(f"/api/v1/auth/teacher-applications/{applicant['id']}/{action}", **kwargs))
            self.assertEqual(result, {"user_id": applicant["id"], "status": status, "review_note": expected_note})
            reopened = PlatformStore(self.store.db_path)
            event = next(row for row in reopened.list_audit_events() if row["resource_id"] == applicant["id"])
            self.assertEqual(event["payload"], {"note": expected_note})
            self.assertEqual(event["actor_user_id"], self.admin["id"])
            self.assertEqual(reopened.get_user(applicant["id"])["status"], status)
            self.expect(self.client.post(f"/api/v1/auth/teacher-applications/{applicant['id']}/{action}", **kwargs), 404)

    def test_teacher_review_validation_permissions_and_atomic_audit_failure(self):
        applicant = self.user("review-validation", "teacher", status="pending")
        path = f"/api/v1/auth/teacher-applications/{applicant['id']}/reject"
        self.as_user(self.owner)
        self.expect(self.client.post(path, json={"note": "拒绝"}), 403)
        self.as_user(self.admin)
        for payload in ({"note": "x" * 501}, {"note": 123}, {"note": None}, {"note": "ok", "password": "no"}):
            self.expect(self.client.post(path, json=payload), 422)
        before = self.store.get_user(applicant["id"])
        original = self.store.record_audit_event

        def fail_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("disposable audit failure")

        with patch.object(self.store, "record_audit_event", side_effect=fail_after_insert):
            self.expect(self.client.post(path, json={"note": "必须同时保存的理由"}), 503)
        self.assertEqual(self.store.get_user(applicant["id"]), before)
        self.assertFalse(any(row["resource_id"] == applicant["id"] for row in self.store.list_audit_events()))
        self.expect(self.client.post(path, json={"note": "x" * 500}))

    def test_course_capabilities_do_not_imply_class_authority(self):
        cases = ((self.material_owner, True, True), (self.course_manager, True, False),
                 (self.course_reader, False, False), (self.student, False, False), (self.admin, True, True))
        for actor, manage, permissions in cases:
            with self.subTest(actor=actor["username"]):
                self.as_user(actor)
                courses = self.expect(self.client.get("/api/v1/courses"))
                row = next(row for row in courses if row["id"] == self.course["id"])
                self.assertEqual(set(row), COURSE_FIELDS)
                self.assertEqual((row["can_manage_materials"], row["can_create_classes"], row["can_manage_collaborators"]),
                                 (manage, manage, permissions))
                self.assertEqual(self.expect(self.client.get(f"/api/v1/courses/{self.course['id']}")), row)
        for actor in (self.material_owner, self.course_manager, self.course_reader):
            self.as_user(actor)
            self.assertEqual(self.expect(self.client.get("/api/v1/classes")), [])
            self.expect(self.client.get(f"/api/v1/classes/{self.classroom['id']}/members"), 403)
        self.as_user(self.owner)
        self.assertEqual(self.expect(self.client.get("/api/v1/courses")), [])

    def test_class_capabilities_distinguish_reader_manager_owner_and_student(self):
        for actor, manage, permissions in ((self.reader, False, False), (self.manager, True, False),
                                           (self.owner, True, True), (self.admin, True, True), (self.student, False, False)):
            with self.subTest(actor=actor["username"]):
                self.as_user(actor)
                row = self.expect(self.client.get(f"/api/v1/classes/{self.classroom['id']}"))
                self.assertEqual(set(row), CLASS_FIELDS)
                self.assertEqual((row["can_manage"], row["can_manage_permissions"], row["can_set_student_budgets"]),
                                 (manage, permissions, permissions))
                for path in ("/api/v1/classes", f"/api/v1/courses/{self.course['id']}/classes"):
                    listed = self.expect(self.client.get(path))
                    self.assertEqual(next(item for item in listed if item["id"] == row["id"]), row)
                if actor["role"] != "student":
                    members = self.expect(self.client.get(f"/api/v1/classes/{row['id']}/members"))
                    self.assertEqual({item["user_id"] for item in members}, {self.student["id"], self.pending["id"]})
                    for member in members:
                        self.assertEqual(set(member), {"user_id", "username", "display_name", "status"})
                self.assertEqual(set(self.expect(self.client.get("/api/v1/auth/me"))), ACCOUNT_FIELDS)

    def test_create_class_requires_real_course_management_and_explicit_active_owner(self):
        path = f"/api/v1/courses/{self.course['id']}/classes"
        payload = {"name": "新班", "owner_teacher_id": self.owner["id"]}
        for actor in (self.owner, self.reader, self.course_reader):
            self.as_user(actor)
            self.expect(self.client.post(path, json=payload), 403)
        self.as_user(self.material_owner)
        self.expect(self.client.post(path, json={"name": "缺少负责人"}), 422)
        for owner_id in (self.student["id"], self.admin["id"], "missing"):
            self.expect(self.client.post(path, json={**payload, "owner_teacher_id": owner_id}), 409)
        created = self.expect(self.client.post(path, json=payload), 201)
        self.assertEqual(set(created), CLASS_FIELDS)
        self.assertFalse(created["can_manage"])
        self.assertFalse(created["can_set_student_budgets"])
        self.expect(self.client.get(f"/api/v1/classes/{created['id']}"), 403)
        self.as_user(self.owner)
        self.assertTrue(self.expect(self.client.get(f"/api/v1/classes/{created['id']}"))["can_manage"])
        self.as_user(self.course_manager)
        own_class = self.expect(self.client.post(path, json={"name": "共同管理者新班", "owner_teacher_id": self.course_manager["id"]}), 201)
        self.assertTrue(own_class["can_set_student_budgets"])

    def test_read_collaborator_cannot_create_students_issue_codes_or_change_class(self):
        before_users = self.store.count_users()
        self.as_user(self.reader)
        class_id = self.classroom["id"]
        self.expect(self.client.post("/api/v1/auth/students", json={
            "username": "reader-created", "display_name": "拒绝创建", "course_id": self.course["id"], "class_id": class_id,
        }), 403)
        for endpoint, target in (("password-reset-codes", self.student), ("activation-codes", self.pending)):
            self.expect(self.client.post(f"/api/v1/auth/{endpoint}", json={"user_id": target["id"], "class_id": class_id}), 403)
        self.expect(self.client.patch(f"/api/v1/classes/{class_id}", json={"name": "拒绝改名"}), 403)
        self.expect(self.client.delete(f"/api/v1/classes/{class_id}/members/{self.student['id']}"), 403)
        self.assertEqual(self.store.count_users(), before_users)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM activation_codes").fetchone()[0], 0)
        self.assertEqual(self.store.get_class(class_id)["name"], "一班")

    def test_class_manager_can_create_students_but_cannot_grant_permissions(self):
        self.as_user(self.manager)
        created = self.expect(self.client.post("/api/v1/auth/students", json={
            "username": "manager-created", "display_name": "新学生", "course_id": self.course["id"], "class_id": self.classroom["id"],
        }), 201)
        self.assertEqual(set(created), {"student_id", "activation_code", "expires_in_seconds"})
        self.assertIsNotNone(self.store.get_class_membership(self.classroom["id"], created["student_id"]))
        self.expect(self.client.put(f"/api/v1/classes/{self.classroom['id']}/collaborators", json={
            "teacher_id": self.other["id"], "access_level": "manage",
        }), 403)
        self.expect(self.client.post(f"/api/v1/classes/{self.classroom['id']}/owner", json={"owner_teacher_id": self.manager["id"]}), 403)
        self.store.remove_class_collaborator(self.classroom["id"], self.manager["id"], removed_by=self.owner["id"])
        self.expect(self.client.post("/api/v1/auth/activation-codes", json={
            "user_id": created["student_id"], "class_id": self.classroom["id"],
        }), 403)
        self.expect(self.client.get(f"/api/v1/classes/{self.classroom['id']}"), 403)

    def test_budget_metadata_uses_configured_units_and_reads_real_limits_without_writes(self):
        self.seed_limits()
        self.as_user(self.admin)
        before = self.ledger.list_limits()
        result = self.expect(self.client.get("/api/v1/budgets/limits"))
        self.assertEqual(set(result), {"items", "currency", "unit", "unit_scale", "unit_description"})
        self.assertEqual((result["currency"], result["unit"], result["unit_scale"]), ("site-credit", "billing_unit", 1))
        self.assertIn("不提供法币兑换比例", result["unit_description"])
        self.assertEqual(result["items"], [asdict(item) for item in before])
        for item in result["items"]:
            self.assertEqual(set(item), LIMIT_FIELDS)
        self.assertTrue(any(row["scope_type"] == "project" and row["scope_id"] == "platform" for row in result["items"]))
        self.assertTrue(any(row["scope_type"] == "student_daily" and row["class_id"] is None and row["course_id"] is None for row in result["items"]))
        self.assertEqual(self.ledger.list_limits(), before)
        self.runtime.settings = replace(self.runtime.settings, budget_currency="configured-billing-unit")
        self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["currency"], "configured-billing-unit")

    def test_budget_read_is_owner_only_and_class_filter_excludes_global_and_other_classes(self):
        self.seed_limits()
        self.store.create_class_membership(self.other_class["id"], self.student["id"])
        self.ledger.set_limit("student_daily", self.student["id"], 33,
                              course_id=self.course["id"], class_id=self.other_class["id"])
        expected = [asdict(item) for item in self.ledger.list_limits() if item.class_id == self.classroom["id"]]
        for actor in (self.admin, self.owner):
            self.as_user(actor)
            self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": self.classroom["id"]}))["items"], expected)
        self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["items"], expected)
        self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": self.other_class["id"]}), 403)
        for actor in (self.reader, self.manager, self.material_owner, self.course_manager):
            self.as_user(actor)
            self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["items"], [])
            self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": self.classroom["id"]}), 403)

    def test_budget_read_uses_membership_of_each_policy_class(self):
        second_owned = self.store.create_class(self.course["id"], "本人另一班", owner_teacher_id=self.owner["id"])
        self.ledger.set_limit("student_daily", self.student["id"], 15,
                              course_id=self.course["id"], class_id=second_owned["id"])
        self.as_user(self.owner)
        self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["items"], [])
        self.store.create_class_membership(second_owned["id"], self.student["id"])
        self.assertEqual(len(self.expect(self.client.get("/api/v1/budgets/limits"))["items"]), 1)
        self.store.delete_class_membership(second_owned["id"], self.student["id"])
        self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["items"], [])

    def test_student_budget_read_stays_personal_and_cannot_write(self):
        self.seed_limits()
        self.as_user(self.student)
        rows = self.expect(self.client.get("/api/v1/budgets/limits"))["items"]
        self.assertTrue(rows)
        self.assertTrue(all(row["scope_id"] == self.student["id"] and row["scope_type"] == "student_daily" for row in rows))
        filtered = self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": self.classroom["id"]}))["items"]
        self.assertTrue(all(row["class_id"] == self.classroom["id"] for row in filtered))
        self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": self.other_class["id"]}), 403)
        self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload()), 403)

    def test_budget_writes_keep_owner_global_and_collaborator_boundaries(self):
        self.as_user(self.owner)
        accepted = self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload(limit_units=0)))
        self.assertEqual((accepted["limit_units"], accepted["class_id"], accepted["course_id"]),
                         (0, self.classroom["id"], self.course["id"]))
        before = self.ledger.list_limits()
        for payload, status in ((self.budget_payload(class_id=None), 403),
                                ({"scope_type": "project", "scope_id": "platform", "limit_units": 50}, 403),
                                (self.budget_payload(class_id=self.other_class["id"]), 403),
                                (self.budget_payload(scope_id=self.outsider["id"]), 404),
                                (self.budget_payload(scope_id=self.owner["id"]), 404)):
            self.expect(self.client.put("/api/v1/budgets/limits", json=payload), status)
        for actor in (self.reader, self.manager, self.material_owner, self.course_manager):
            self.as_user(actor)
            self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload()), 403)
        self.assertEqual(self.ledger.list_limits(), before)
        self.as_user(self.admin)
        self.expect(self.client.put("/api/v1/budgets/limits", json={"scope_type": "project", "scope_id": "platform", "limit_units": 500}))
        global_limit = self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload(class_id=None)))
        self.assertIsNone(global_limit["class_id"])
        self.assertIsNone(global_limit["course_id"])
        self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload(class_id=self.other_class["id"])), 404)

    def test_budget_write_rechecks_current_owner_before_committing(self):
        self.as_user(self.owner)
        original = v1_jobs.get_runtime

        def handoff(request):
            self.store.transfer_class_owner(self.classroom["id"], self.other["id"], transferred_by=self.admin["id"])
            return original(request)

        with patch.object(v1_jobs, "get_runtime", side_effect=handoff):
            self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload()), 403)
        self.assertEqual(self.ledger.list_limits(), ())
        self.as_user(self.other)
        self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload()))

    def test_budget_validation_has_no_partial_writes(self):
        self.as_user(self.admin)
        for payload in (self.budget_payload(limit_units=-1), self.budget_payload(limit_units=True),
                        self.budget_payload(limit_units=1.5), self.budget_payload(limit_units=2**63),
                        self.budget_payload(emergency_units=1), self.budget_payload(period="2026-02-30"),
                        self.budget_payload(class_id=""),
                        {"scope_type": "project", "scope_id": "platform", "limit_units": 10, "period": "2026-11-16"},
                        {"scope_type": "project", "scope_id": "platform", "limit_units": 2**63 - 1, "emergency_units": 1},
                        {"scope_type": "teaching", "scope_id": "teacher", "limit_units": 10, "class_id": self.classroom["id"]}):
            with self.subTest(payload=payload):
                self.expect(self.client.put("/api/v1/budgets/limits", json=payload), 422)
                self.assertEqual(self.ledger.list_limits(), ())
        self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": ""}), 422)
        self.expect(self.client.get("/api/v1/budgets/limits", params={"class_id": "missing"}), 404)
        self.expect(self.client.put("/api/v1/budgets/limits", json=self.budget_payload(class_id="missing")), 404)

    def test_budget_get_does_not_enable_unconfigured_paid_work(self):
        self.as_user(self.admin)
        self.assertEqual(self.expect(self.client.get("/api/v1/budgets/limits"))["items"], [])
        self.runtime.queue.enqueue("interaction", job_id="unfunded", paid=True, purpose="student",
                                   student_id=self.student["id"], project_id="platform", available_at=1)
        job = self.runtime.queue.claim("test-worker", now=2)
        with self.assertRaises(InsufficientBudget):
            self.runtime.queue.start_call(job.job_id, "must-not-send", "test-worker", job.generation, reserve_units=1, now=2)
        self.assertEqual(self.runtime.queue.list_calls(job.job_id), ())
        self.assertEqual(self.ledger.list_limits(), ())

    def test_admin_management_queries_do_not_read_learning_repositories(self):
        class NoLearningAccess:
            def __getattr__(self, name):
                raise AssertionError(f"management attempted to read learning content: {name}")

        for name in ("learning", "companion", "reports", "content"):
            self.runtime.install(name, NoLearningAccess())
        self.as_user(self.admin)
        for path in ("/api/v1/auth/accounts", "/api/v1/auth/teacher-applications", "/api/v1/courses",
                     "/api/v1/classes", f"/api/v1/classes/{self.classroom['id']}/members", "/api/v1/budgets/limits"):
            with self.subTest(path=path):
                result = self.expect(self.client.get(path))
                self.assertNotIn("fixture-password-hash-not-for-exposure", str(result))
                self.assertNotIn("private-phone", str(result))
                self.assertNotIn("private-student-number", str(result))


if __name__ == "__main__":
    unittest.main()
