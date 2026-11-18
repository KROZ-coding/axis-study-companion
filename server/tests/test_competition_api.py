from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from app.platform_store import PlatformStore
from app.routers.v1_competition import router
from app.services.competition_inputs import TABLES
from app.services.competition_runs import CompetitionRunStore
from app.runtime import Runtime
from app.config import Settings
from test_competition_inputs import actual_csv_inputs, workbook_input


class CompetitionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="competition-api-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.platform = PlatformStore(root / "platform.sqlite3")
        self.owner = self.platform.create_user("teacher", "教师", "local-test-password-hash", "teacher")
        self.other = self.platform.create_user("other", "无关教师", "local-test-password-hash", "teacher")
        self.student = self.platform.create_user("student", "学生", "local-test-password-hash", "student")
        self.course = self.platform.create_course("数学", "2026", self.owner["id"], course_id="course-a")
        self.second_course = self.platform.create_course("物理", "2026", self.owner["id"], course_id="course-b")
        self.platform.create_session("sid-owner", self.owner["id"], 9_999_999_999)
        self.platform.create_session("sid-other", self.other["id"], 9_999_999_999)
        self.platform.create_session("sid-student", self.student["id"], 9_999_999_999)
        self.runtime = Runtime(Settings(data_dir=str(root), app_env="test", worker_enabled=False))
        self.runtime.install("platform", self.platform)
        app = FastAPI()
        app.state.runtime = self.runtime
        app.include_router(router)
        self.client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        self._login("owner")
        try:
            self.files = workbook_input()
        except unittest.SkipTest:
            self.files = actual_csv_inputs()

    def _login(self, key):
        self.client.cookies.clear()
        if key:
            self.client.cookies.set("sid", "sid-" + key)

    def _upload(self, course="course-a"):
        return self.client.post("/api/v1/competition/runs/upload", data={"course_id": course, "title": "Unit test"},
                                files=[("files", (name, data, "application/octet-stream")) for name, data in self.files.items()])

    def _confirm(self, run_id, scope=None, revision=1):
        return self.client.post(f"/api/v1/competition/runs/{run_id}/confirm", json={"expected_revision": revision, "scope": scope or {}})

    def test_auth_course_isolation_input_projection_and_frozen_export_digest_headers(self):
        self.assertEqual(self.client.get("/api/v1/competition/schema").status_code, 200)
        anonymous = self.client.get("/api/v1/competition/runs", params={"course_id": "course-a"})
        self._login(None)
        self.assertEqual(self.client.get("/api/v1/competition/runs", params={"course_id": "course-a"}).status_code, 401)
        self._login("student")
        self.assertEqual(self.client.get("/api/v1/competition/schema").status_code, 403)
        self._login("other")
        self.assertEqual(self._upload().status_code, 404)
        self._login("owner")
        response = self._upload()
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["run"]["status"], "preview")
        self.assertEqual(len(data["preview"]["options"]["auto_suggestions"]), 3)
        self.assertIsNone(data["result"])
        self.assertNotIn("tables", data["preview"])
        self.assertNotIn("学生最终作答", json.dumps(data["preview"]["options"], ensure_ascii=False))
        run_id = data["run"]["id"]
        self.assertEqual(self.client.get(f"/api/v1/competition/runs/{run_id}/export/json").status_code, 409)
        self.assertEqual(self._confirm(run_id, {"node_ids": ["N02"], "class_ids": ["DC02"]}).status_code, 200)
        fetched = self.client.get(f"/api/v1/competition/runs/{run_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["result"]["summary"]["scope"]["class_ids"], ["DC02"])
        listed = self.client.get("/api/v1/competition/runs", params={"course_id": "course-a"})
        self.assertEqual([row["id"] for row in listed.json()["items"]], [run_id])
        self.assertEqual(self.client.get("/api/v1/competition/runs", params={"course_id": "course-b"}).json()["items"], [])
        self._login("other")
        self.assertEqual(self.client.get(f"/api/v1/competition/runs/{run_id}").status_code, 404)
        self._login("owner")
        for fmt in ("json", "csv", "zip", "html"):
            exported = self.client.get(f"/api/v1/competition/runs/{run_id}/export/{fmt}")
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertEqual(exported.headers["x-content-sha256"], hashlib.sha256(exported.content).hexdigest())
            self.assertEqual(exported.headers["x-competition-run-id"], run_id)
            self.assertEqual(exported.headers["x-competition-input-sha256"], data["preview"]["input_sha256"])
            self.assertEqual(exported.headers["x-content-type-options"], "nosniff")
        for scope, expected_status in (({"node_ids": ["guessed"]}, 422), ({"class_ids": ["DC01"]}, 409)):
            conflict = self._confirm(run_id, scope)
            self.assertEqual(conflict.status_code, expected_status)
        self.assertEqual(self.client.post("/api/v1/competition/runs/not-a-run/confirm", json={"expected_revision": 1}).status_code, 404)

    def test_request_recovery_returns_original_run_and_checks_current_actor_and_course(self):
        texts = {name: data.decode("utf-8-sig") for name, data in actual_csv_inputs().items()}
        payload = {"course_id": "course-a", "title": "Recoverable", "csv_texts": texts, "request_id": "original-request"}
        created = self.client.post("/api/v1/competition/runs", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        same = self.client.post("/api/v1/competition/runs", json=payload)
        self.assertEqual(same.json()["run"]["id"], created.json()["run"]["id"])
        url = "/api/v1/competition/runs/by-request/original-request"
        recovered = self.client.get(url, params={"course_id": "course-a"})
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json(), created.json())
        self.assertEqual(recovered.headers["cache-control"], "no-store")
        changed = self.client.post("/api/v1/competition/runs", json={**payload, "title": "Changed"})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.client.get(url, params={"course_id": "course-b"}).status_code, 404)
        self._login("other")
        self.assertEqual(self.client.get(url, params={"course_id": "course-a"}).status_code, 404)

    def test_pasted_csv_exact_schema_path(self):
        texts = {name: data.decode("utf-8-sig") for name, data in actual_csv_inputs().items()}
        response = self.client.post("/api/v1/competition/runs", json={"course_id": "course-a", "title": "Pasted",
                                                                      "csv_texts": texts})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["preview"]["summary"]["table_rows"]["作答记录"], 806)
        bad = self.client.post("/api/v1/competition/runs", json={"course_id": "course-a", "title": "Bad",
                                                                 "csv_texts": {"mystery.csv": "x\n1"}})
        self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":
    unittest.main()
