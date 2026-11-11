"""Reports routes with real cookie auth and class/course authorization stores."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform_store import PlatformStore
from app.report_store import ReportStore
from app.routers.v1_reports import router
from app.services.report_exports import build_report_snapshot, render_export
from test_report_exports import PRIVATE_MARKER, report_input


class EvidenceAPI:
    """A mapping-boundary double, never a fake SQLite learning schema."""

    def __init__(self):
        self.source_revision = 7
        self.calls = []
        self.after_read = None
        self.data = report_input()

    def get_goal_scope(self, goal_id, actor_id):
        if goal_id != "goal":
            raise LookupError("missing goal")
        return {"goal_id": goal_id, "class_id": "class", "course_id": "course", "version": 1}

    def report_input(self, scope, actor_id):
        self.calls.append((dict(scope), actor_id))
        result = deepcopy(self.data)
        result["scope"] = dict(scope)
        result["source_revision"] = self.source_revision
        if self.after_read:
            self.after_read()
        return result


class V1ReportsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="v1-reports-test-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.platform = PlatformStore(root / "platform.sqlite3")
        self.reports = ReportStore(root / "reports.sqlite3")
        for user_id, role in (("teacher", "teacher"), ("other-teacher", "teacher"), ("course-owner", "teacher"),
                              ("student", "student"), ("other-student", "student"), ("no-evidence", "student"), ("admin", "admin")):
            self.platform.create_user(user_id, user_id, "argon2$test-only-hash", role, user_id=user_id)
            self.platform.create_session(user_id + "-token", user_id, 9_999_999_999)
        self.platform.create_course("Course", "2026", "course-owner", course_id="course")
        self.platform.create_class("course", "Class", owner_teacher_id="teacher", class_id="class")
        self.platform.create_class("course", "Other class", owner_teacher_id="other-teacher", class_id="other-class")
        for user_id in ("student", "other-student", "no-evidence"):
            self.platform.create_course_membership("course", user_id)
            self.platform.create_class_membership("class", user_id)
        self.learning = EvidenceAPI()
        self.runtime = SimpleNamespace(platform=self.platform, reports=self.reports, learning=self.learning)
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.include_router(router)
        self.client = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        self.login("teacher")

    def login(self, user_id):
        self.client.cookies.clear()
        if user_id:
            self.client.cookies.set("sid", user_id + "-token")

    def scope(self, kind="class", **changes):
        value = {"kind": kind, "class_id": "class", "course_id": "course"}
        if kind == "student":
            value["student_id"] = "student"
        elif kind == "goal":
            value["goal_id"] = "goal"
        elif kind == "question_bank":
            value["question_bank_id"] = "bank"
        value.update(changes)
        return value

    def generate(self, scope=None, *, key="request-key-1", version=7, **changes):
        value = {"scope": scope or self.scope(), "schema_version": "report.snapshot.v1", "rule_version": "mastery-v1",
                 "expected_source_revision": version, **changes}
        return self.client.post("/api/v1/reports", json=value, headers={"Idempotency-Key": key})

    def assert_created(self, scope=None, **kwargs):
        response = self.generate(scope, **kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_cookie_auth_required_and_rules_explicit(self):
        self.login(None)
        for path in ("/api/v1/reports", "/api/v1/reports/rules", "/api/v1/reports/missing/export/json"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.generate().status_code, 401)
        self.login("student")
        result = self.client.get("/api/v1/reports/rules")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["rules"][0]["calibrated"])

    def test_generate_version_idempotency_and_old_revision_export(self):
        first = self.assert_created()
        self.assertEqual(first["snapshot"]["source_revision"], 7)
        self.assertTrue(first["is_current"])
        self.learning.source_revision = 8
        replay = self.generate()
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], first["id"])
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(len(self.learning.calls), 1)
        self.assertEqual(self.generate(version=8).status_code, 409)
        newer = self.assert_created(key="request-key-2", version=8)
        self.assertNotEqual(newer["id"], first["id"])
        current = self.client.get("/api/v1/reports/" + first["report_id"])
        self.assertEqual(current.json()["id"], newer["id"])
        old = self.client.get(f"/api/v1/reports/{first['report_id']}/export/json", params={"revision_id": first["id"]})
        self.assertEqual(old.status_code, 200, old.text)
        self.assertEqual(old.json()["snapshot"], first["snapshot"])
        self.assertEqual(old.headers["X-Report-Snapshot-SHA256"], first["snapshot_sha256"])
        self.assertNotIn(PRIVATE_MARKER, old.text)

    def test_scope_and_explicit_versions_are_required(self):
        cases = ({"schema_version": "future"}, {"rule_version": "future"}, {"expected_source_revision": -1},
                 {"expected_source_revision": "7"}, {"unexpected": True}, {"scope": {"kind": "class"}})
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.generate(**changes).status_code, 422)
        self.assertEqual(self.generate(version=6).status_code, 409)
        payload = {"scope": self.scope(), "schema_version": "report.snapshot.v1", "rule_version": "mastery-v1", "expected_source_revision": 7}
        self.assertEqual(self.client.post("/api/v1/reports", json=payload).status_code, 422)

    def test_student_only_personal_reports_and_teacher_class_scope(self):
        personal = self.assert_created(self.scope("student"))
        class_report = self.assert_created(key="class-request")
        self.login("student")
        self.assertEqual(self.client.get("/api/v1/reports/" + personal["report_id"]).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/reports/" + class_report["report_id"]).status_code, 404)
        self.assertEqual(self.generate(self.scope("student", student_id="other-student")).status_code, 403)
        self.assertEqual(self.generate(self.scope()).status_code, 403)
        listing = self.client.get("/api/v1/reports").json()["items"]
        self.assertEqual([row["id"] for row in listing], [personal["report_id"]])
        self.login("other-teacher")
        self.assertEqual(self.client.get("/api/v1/reports/" + personal["report_id"]).status_code, 404)
        self.assertEqual(self.generate().status_code, 403)
        self.login("course-owner")
        self.assertEqual(self.client.get("/api/v1/reports/" + class_report["report_id"]).status_code, 404)

    def test_permission_revocation_blocks_every_read_export_and_idempotency_replay(self):
        self.platform.set_class_collaborator("class", "other-teacher", "manage", granted_by="teacher")
        self.login("other-teacher")
        report = self.assert_created()
        self.platform.remove_class_collaborator("class", "other-teacher", removed_by="teacher")
        paths = [f"/api/v1/reports/{report['report_id']}", f"/api/v1/reports/{report['report_id']}/revisions",
                 f"/api/v1/reports/{report['report_id']}/revisions/{report['id']}"]
        paths += [f"/api/v1/reports/{report['report_id']}/export/{format}" for format in ("html", "pdf", "xlsx", "json", "csv")]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.generate().status_code, 403)
        self.assertEqual(self.client.get("/api/v1/reports").json()["items"], [])

    def test_read_collaborator_can_read_but_not_generate(self):
        report = self.assert_created()
        self.platform.set_class_collaborator("class", "other-teacher", "read", granted_by="teacher")
        self.login("other-teacher")
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 200)
        self.assertEqual(self.generate(key="reader-new").status_code, 403)

    def test_student_history_remains_personal_after_class_transfer(self):
        report = self.assert_created(self.scope("student"))
        self.platform.transfer_class_member("class", "other-class", "student", actor_id="admin")
        self.login("student")
        self.assertFalse(self.platform.can_access_class("student", "class"))
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/reports/{report['report_id']}/export/json").status_code, 200)
        self.login("other-teacher")
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 404)
        self.login("teacher")
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 200)

    def test_question_bank_course_authorization_has_no_implicit_class_data(self):
        self.login("course-owner")
        scope = {"kind": "question_bank", "question_bank_id": "bank", "course_id": "course"}
        report = self.assert_created(scope)
        self.assertNotIn("students", report["snapshot"]["tables"])
        self.assertNotIn("mastery", report["snapshot"]["tables"])
        self.assertNotIn("evidence", report["snapshot"]["tables"])
        self.assertNotIn("测试学生", str(report))
        self.login("teacher")
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 404)
        self.login("student")
        self.assertEqual(self.client.get("/api/v1/reports/" + report["report_id"]).status_code, 404)

    def test_slow_generation_and_render_recheck_revoked_sessions(self):
        self.learning.after_read = lambda: self.platform.delete_session("teacher-token")
        self.assertEqual(self.generate().status_code, 401)
        self.assertEqual(self.reports.list_reports(), [])
        self.platform.create_session("teacher-token", "teacher", 9_999_999_999)
        self.learning.after_read = None
        report = self.assert_created()
        def revoke(*args, **kwargs):
            artifact = render_export(*args, **kwargs)
            self.platform.delete_session("teacher-token")
            return artifact
        with patch("app.routers.v1_reports.render_export", side_effect=revoke):
            response = self.client.get(f"/api/v1/reports/{report['report_id']}/export/json")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("snapshot", response.json())

    def test_cross_report_revision_cannot_be_selected(self):
        first = self.assert_created()
        second = self.assert_created(self.scope("student"), key="student-request")
        response = self.client.get(f"/api/v1/reports/{first['report_id']}/export/json", params={"revision_id": second["id"]})
        self.assertEqual(response.status_code, 404)

    def test_source_revision_discovery_returns_token_and_no_raw_evidence(self):
        result = self.client.get("/api/v1/reports/source-revision", params=self.scope())
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["source_revision"], 7)
        self.assertNotIn("evidence", result.json())
        self.assertNotIn(PRIVATE_MARKER, result.text)

    def test_legacy_learning_report_input_is_explicitly_pending(self):
        class Legacy:
            def report_input(self, goal_id, actor_id):
                raise AssertionError("unsafe legacy report method must not run")
        self.runtime.learning = Legacy()
        result = self.generate()
        self.assertEqual(result.status_code, 501)
        self.assertEqual(result.json()["detail"]["code"], "report_scope_pending")
        self.assertEqual(self.reports.list_reports(), [])

    def test_admin_exports_safe_snapshot_without_raw_privilege(self):
        self.login("admin")
        report = self.assert_created()
        response = self.client.get(f"/api/v1/reports/{report['report_id']}/export/json")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PRIVATE_MARKER, response.text)
        self.platform.update_user("admin", status="disabled")
        self.assertEqual(self.client.get(f"/api/v1/reports/{report['report_id']}").status_code, 401)


if __name__ == "__main__":
    unittest.main()
