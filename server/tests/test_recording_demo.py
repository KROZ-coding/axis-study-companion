"""Isolated real-store HTTP checks for the recording application, with no upstream."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.update(SKIP_ENV_FILE="1", APP_ENV="test", RECORDING_DEMO_ALLOW_LIVE="0")
SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi.testclient import TestClient
from app.recording_demo import create_recording_app
from app.demo_fixture import QUESTIONS, DIAGNOSTICS, FORMAL_IDS


class RecordingDemoTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory(prefix="recording-demo-test-"))
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request"):
            self.enterContext(patch(target, side_effect=AssertionError("recording demo must not use the network")))
        self.app = create_recording_app(Path(self.root) / "run")
        self.client = self.enterContext(TestClient(self.app, headers={"Origin": "http://testserver"}))
        self.state = self.get("/api/demo/status")

    def get(self, path):
        result = self.client.get(path)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def post(self, path, body=None, expected=200):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.content else None

    def login(self, persona):
        return self.post("/api/demo/login", {"persona": persona})["user"]

    def diagnose(self, task_id):
        task = self.get("/api/v1/tasks/" + task_id)
        diagnostic = self.post(f"/api/v1/tasks/{task_id}/diagnostics", {"request_id": "start-" + task_id, "expected_version": task["version"]}, 201)
        self.assertEqual(len(diagnostic["questions"]), 2)
        self.assertTrue(all("answer" not in q for q in diagnostic["questions"]))
        result = self.post(f"/api/v1/diagnostics/{diagnostic['id']}/submit", {
            "request_id": "diagnose-" + task_id, "answers": {q["id"]: q["answer"] for q in DIAGNOSTICS}, "unknown_question_ids": []})
        self.assertEqual(result["feedback"]["correct"], 2)
        return result

    def save(self, task_id, answers, request_id="save"):
        lease = self.post(f"/api/v1/tasks/{task_id}/edit-leases", {"device_id": "recording-test", "lease_seconds": 300})
        result = self.client.put(f"/api/v1/tasks/{task_id}/draft", json={
            "request_id": request_id, "expected_version": lease["task_version"], "answers": answers,
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]})
        self.assertEqual(result.status_code, 200, result.text)
        return lease, result.json()

    def test_complete_flow_and_new_round_preserve_saved_facts(self):
        self.assertEqual(self.get("/health")["mode"], "platform")
        self.assertNotIn("password", str(self.state).lower())
        self.assertFalse(self.app.state.settings.llm_configured)
        teacher = self.login("teacher")
        library = self.get(f"/api/v1/courses/{self.state['course_id']}/questions")
        self.assertEqual(len(library["items"]), 14)
        self.assertTrue(all(q["status"] == "approved" for q in library["items"]))
        self.assertEqual({q["content"]["type"] for q in library["items"]}, {"single_choice", "judge", "fill", "text"})
        lin = self.login("lin")
        task_id = self.state["task_ids"]["lin"]
        qid = FORMAL_IDS[0]
        self.assertEqual(self.get("/api/v1/auth/me")["id"], lin["id"])
        task = self.get("/api/v1/tasks/" + task_id)
        self.assertEqual(task["questions"], [])
        self.assertEqual(task["status"], "not_started")
        self.post("/api/demo/hint", {"task_id": task_id, "question_id": qid}, 409)
        chat = self.post("/api/demo/chat", {"message": "帮我解方程"})
        self.assertEqual(chat["source"], "simulation")
        self.assertIn("首诊", chat["text"])
        self.diagnose(task_id)
        task = self.get("/api/v1/tasks/" + task_id)
        self.assertEqual(len(task["questions"]), 4)
        self.assertEqual([q["id"] for q in task["questions"]], list(FORMAL_IDS))
        self.assertTrue(all("answer" not in q and "rubric" not in q for q in task["questions"]))
        self.post("/api/demo/hint", {"task_id": task_id, "question_id": qid}, 409)
        answers = {q["id"]: q["answer"] for q in QUESTIONS if q["id"] in FORMAL_IDS}
        lease, draft = self.save(task_id, answers)
        hint = self.post("/api/demo/hint", {"task_id": task_id, "question_id": qid, "message": "直接告诉我答案"})
        self.assertEqual(hint["source"], "simulation")
        self.assertNotIn("答案是", hint["text"])
        self.assertIn("本机模拟", hint["text"])
        self.assertEqual(hint["step"], 1)
        texts = [hint["text"]]
        for step in (2, 3, 4):
            following = self.post("/api/demo/hint", {"task_id": task_id, "question_id": qid, "message": "继续"})
            self.assertEqual(following["step"], step)
            self.assertNotIn("x = 4", following["text"])
            texts.append(following["text"])
        self.assertEqual(len(set(texts)), 4)
        self.assertIn("复习提示", texts[-1])
        self.post(f"/api/v1/tasks/{task_id}/submit", {"request_id": "submit", "expected_version": draft["version"],
                  "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}, 202)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            task = self.get("/api/v1/tasks/" + task_id)
            if task["status"] in {"results_ready", "awaiting_manual"}:
                break
            time.sleep(0.05)
        self.assertEqual(task["status"], "results_ready", task)
        attempt = task["attempts"][0]
        self.assertEqual(attempt["total_score"], 8)
        self.assertFalse(attempt["independent_evidence"])
        subjective = [item for item in attempt["items"] if "text-" in item["question_id"]]
        self.assertEqual(len(subjective), 1)
        self.assertTrue(all("模拟" in item["result"]["why"] for item in subjective))
        for job in self.app.state.runtime.queue.list_jobs(limit=100):
            self.assertFalse(self.app.state.runtime.queue.list_calls(job.job_id))
        facts = self.get("/api/demo/evidence")
        self.assertEqual(facts["summary"]["help_event_count"], 4)
        self.assertEqual(facts["summary"]["submitted_attempt_count"], 1)
        self.assertEqual(facts["summary"]["graded_attempt_count"], 1)
        self.assertEqual(facts["source"], "persisted_records")
        self.login("zhou")
        self.post("/api/demo/hint", {"task_id": task_id, "question_id": qid}, 404)
        self.assertEqual(self.client.get("/api/demo/evidence", params={"student_id": lin["id"]}).status_code, 403)
        self.post("/api/demo/new-round", expected=403)
        self.login("teacher")
        self.assertEqual(self.get("/api/v1/auth/me")["id"], teacher["id"])
        second = self.post("/api/demo/new-round")
        self.assertNotEqual(second["goal_id"], self.state["goal_id"])
        self.assertEqual(second["round_number"], 2)
        self.assertEqual(self.get("/api/v1/tasks/" + task_id)["attempts"][0]["total_score"], 8)
        # Zhou's old unstarted diagnostic must not block a new completed one.
        self.login("zhou")
        fresh = second["task_ids"]["zhou"]
        self.diagnose(fresh)
        self.save(fresh, {qid: 1}, request_id="new-round-save")
        self.post("/api/demo/hint", {"task_id": fresh, "question_id": FORMAL_IDS[1]}, 409)
        self.post("/api/demo/hint", {"task_id": fresh, "question_id": qid})
        self.assertEqual(self.get("/api/demo/evidence")["summary"]["task_count"], 2)

    def test_four_report_scopes_five_exports_and_teacher_student_discussion(self):
        lin = self.login("lin")
        task_id = self.state["task_ids"]["lin"]
        self.diagnose(task_id)
        answers = {q["id"]: q["answer"] for q in QUESTIONS if q["id"] in FORMAL_IDS}
        lease, draft = self.save(task_id, answers)
        self.post(f"/api/v1/tasks/{task_id}/submit", {
            "request_id": "report-submit", "expected_version": draft["version"],
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}, 202)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            task = self.get("/api/v1/tasks/" + task_id)
            if task["status"] in {"results_ready", "awaiting_manual"}:
                break
            time.sleep(0.05)
        self.assertEqual(task["status"], "results_ready", task)
        attempt = task["attempts"][0]
        self.login("teacher")
        rules = self.get("/api/v1/reports/rules")
        scopes = [
            {"kind": "class", "course_id": self.state["course_id"], "class_id": self.state["class_id"]},
            {"kind": "student", "course_id": self.state["course_id"], "class_id": self.state["class_id"], "student_id": lin["id"]},
            {"kind": "goal", "course_id": self.state["course_id"], "class_id": self.state["class_id"], "goal_id": self.state["goal_id"]},
            {"kind": "question_bank", "course_id": self.state["course_id"], "question_bank_id": self.state["course_id"]},
        ]
        reports = {}
        for scope in scopes:
            with self.subTest(scope=scope["kind"]):
                source_response = self.client.get("/api/v1/reports/source-revision", params=scope)
                self.assertEqual(source_response.status_code, 200, source_response.text)
                created = self.client.post("/api/v1/reports/generate", json={
                    "scope": scope, "schema_version": rules["schema_version"],
                    "rule_version": rules["default_rule_version"],
                    "expected_source_revision": source_response.json()["source_revision"],
                }, headers={"Idempotency-Key": "recording-report-" + scope["kind"]})
                self.assertEqual(created.status_code, 201, created.text)
                report = created.json()
                reports[scope["kind"]] = report
                self.assertEqual(self.get("/api/v1/reports/" + report["report_id"])["id"], report["id"])
                for format in ("html", "pdf", "xlsx", "json", "csv"):
                    with self.subTest(scope=scope["kind"], format=format):
                        exported = self.client.get(f"/api/v1/reports/{report['report_id']}/export/{format}",
                                                   params={"revision_id": report["id"]})
                        self.assertEqual(exported.status_code, 200, exported.text[:300] if exported.status_code != 200 else "")
                        self.assertGreater(len(exported.content), 100)
                        self.assertEqual(exported.headers["X-Report-Snapshot-SHA256"], report["snapshot_sha256"])
                        self.assertEqual(exported.headers["X-Report-Revision"], report["id"])
                        if format == "pdf":
                            self.assertTrue(exported.content.startswith(b"%PDF"))
                        elif format == "xlsx":
                            self.assertTrue(exported.content.startswith(b"PK"))
                        elif format == "json":
                            self.assertEqual(exported.json()["snapshot"], report["snapshot"])
        thread = self.post("/api/v1/companion/discussion-threads", {
            "request_id": "recording-thread", "task_id": task_id, "attempt_id": attempt["id"],
            "question_version_id": FORMAL_IDS[-1] + ":1", "grading_revision": attempt["revision"],
            "title": "解方程的检查步骤"}, 201)
        path = "/api/v1/companion/discussion-threads/" + thread["id"]
        comment = "已经写出等式变形，请在下一轮继续保留代入检验过程。"
        self.post(path + "/messages", {"request_id": "recording-comment", "text": comment}, 201)
        self.login("lin")
        self.assertEqual(self.get(path)["messages"][0]["text"], comment)
        reply = "我会把检验单独列出来，并检查左右两边是否相等。"
        self.post(path + "/messages", {"request_id": "recording-reply", "text": reply}, 201)
        self.assertEqual(len(self.get(path)["messages"]), 2)
        self.assertEqual(self.get("/api/v1/reports/" + reports["student"]["report_id"])["id"], reports["student"]["id"])
        self.assertEqual(self.client.get("/api/v1/reports/" + reports["class"]["report_id"]).status_code, 404)
        self.login("zhou")
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/reports/" + reports["student"]["report_id"]).status_code, 404)
        self.login("teacher")
        messages = self.get(path)["messages"]
        self.assertEqual([row["text"] for row in messages], [comment, reply])

    def test_reject_nonempty_directory_and_cross_origin(self):
        with self.assertRaises(ValueError):
            create_recording_app(Path(self.root) / "run")
        for headers in ({"Origin": "http://evil.example"}, {"Origin": ""}):
            result = self.client.post("/api/demo/login", json={"persona": "teacher"}, headers=headers)
            self.assertEqual(result.status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/demo/new-round").status_code, 401)
        self.assertEqual(self.client.get("/api/demo/evidence").status_code, 401)
        from app.main import create_app
        from app.config import Settings
        config = Settings(app_env="test", data_dir=str(Path(self.root) / "platform"), worker_enabled=False,
                          demo_enabled=False, llm_providers=(), llm_base_url="", llm_api_key="", llm_model="")
        with TestClient(create_app(config)) as platform:
            self.assertEqual(platform.get("/api/demo/status").status_code, 404)
            self.assertEqual(platform.post("/api/demo/login", headers={"Origin": "http://testserver"}, json={"persona": "teacher"}).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
