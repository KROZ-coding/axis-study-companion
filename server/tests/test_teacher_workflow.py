"""Real-app teacher-to-student workflow; only model responses are local fixtures.

This integration test crosses HTTP authentication, confirmed sources, reviewed
question versions, planning, frozen student work, the production grading Worker
and teacher review. It never opens project data or sends a network/model call.
"""

import asyncio
import atexit
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="teacher-workflow-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name, STUDY_DATA_DIR=_IMPORT_DATA.name,
    PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false", STUDY_DEMO_ENABLED="false",
    STUDY_WORKER_ENABLED="false", LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
    MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
)

from fastapi.testclient import TestClient

with patch("sqlite3.connect", side_effect=AssertionError("application import must not open SQLite")):
    from app.config import Settings
    from app.main import create_app
    from app.runtime import Runtime
    from app.services.model_gateway import GatewayError, ModelGateway
    from app.services.question_bank import QuestionBank
    from app.utils.security import hash_password
    from app.worker import Worker


class TeacherWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(24)
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="teacher-workflow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.network_guards = []
        for target in (
            "socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
            "httpx.AsyncHTTPTransport.handle_async_request",
        ):
            guard = patch(target, side_effect=AssertionError("external network is forbidden in this workflow"))
            self.network_guards.append(guard.start())
            self.addCleanup(guard.stop)
        real_model_guard = patch.object(ModelGateway, "complete", new_callable=AsyncMock,
                                        side_effect=AssertionError("real models are forbidden in this workflow"))
        self.real_model_guard = real_model_guard.start()
        self.addCleanup(real_model_guard.stop)
        self.app = create_app(Settings(
            app_env="test", data_dir=str(self.root), persistence_enabled=True,
            demo_enabled=False, worker_enabled=False, cookie_secure=False, allowed_hosts=("testserver",),
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="", llm_prices_json="",
            maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(), maxkb_course_dataset_ids=(),
        ))
        self.runtime = self.app.state.runtime
        self.assertIsInstance(self.runtime, Runtime)
        self.assertEqual(self.runtime.data_dir, self.root.resolve())
        # The real Worker sees an unavailable model configuration before it can
        # reserve a paid call. Its normal fallback must put text into manual review.
        self.disabled_gateway = SimpleNamespace(
            describe=Mock(side_effect=GatewayError("local_test_model_disabled", request_sent=False)),
            complete=AsyncMock(side_effect=AssertionError("grading must not call any model")),
        )
        self.runtime.install("gateway", self.disabled_gateway)
        self.client = self.enterContext(TestClient(
            self.app, headers={"Origin": "http://testserver"}, raise_server_exceptions=True,
        ))
        self.assertFalse(self.runtime.worker_status()["enabled"])
        self.assertEqual(self.runtime.worker_status()["running"], 0)
        self.assertIs(self.app.state.platform_store, self.runtime.platform)
        self.platform, self.learning, self.content = self.runtime.platform, self.runtime.learning, self.runtime.content
        self.users, self.cookies = {}, {}
        for name, role in (("teacher", "teacher"), ("a", "student"), ("b", "student")):
            self.users[name] = self.platform.create_user("workflow_" + name, "Workflow " + name,
                                                        self.password_hash, role)
        self.course = self.platform.create_course("Four-type workflow", "2026", self.users["teacher"]["id"])
        self.classroom = self.platform.create_class(self.course["id"], "Workflow class",
                                                    owner_teacher_id=self.users["teacher"]["id"])
        for name in ("a", "b"):
            self.platform.create_course_membership(self.course["id"], self.users[name]["id"])
            self.platform.create_class_membership(self.classroom["id"], self.users[name]["id"])

    def tearDown(self):
        self.disabled_gateway.complete.assert_not_called()
        self.real_model_guard.assert_not_called()
        for guard in self.network_guards:
            guard.assert_not_called()

    def expect(self, response, status=200):
        self.assertEqual(response.status_code, status,
                         f"{response.request.method} {response.request.url.path}: {response.text}")
        return response.json()

    def as_user(self, name):
        self.client.cookies.clear()
        if name not in self.cookies:
            response = self.client.post("/api/v1/auth/login", json={
                "username": self.users[name]["username"], "password": self.password,
                "role": self.users[name]["role"],
            })
            self.expect(response)
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            token = self.client.cookies.get("sid")
            self.assertTrue(token)
            self.assertEqual(self.platform.get_session(token)["user_id"], self.users[name]["id"])
            self.cookies[name] = token
        else:
            self.client.cookies.set("sid", self.cookies[name])
        self.expect(self.client.get("/api/v1/auth/me"))

    def task(self, task_id):
        return self.expect(self.client.get(f"/api/v1/tasks/{task_id}"))

    def assert_answers_closed(self, task):
        self.assertFalse(task["answer_released"])
        self.assertEqual(Counter(q["type"] for q in task["questions"]), dict.fromkeys(
            ("single_choice", "fill", "judge", "text"), 1))
        for question in task["questions"]:
            for key in ("answer", "reference_answer", "explanation", "rubric"):
                self.assertNotIn(key, question)
        self.assertNotIn("PROTECTED-WORKFLOW-", json.dumps(task, ensure_ascii=False))
        for attempt in task["attempts"]:
            for key in ("items", "total_score", "total_score_units"):
                self.assertNotIn(key, attempt)
        policy = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"))
        self.assertFalse(policy["results_released"])
        self.assertFalse(policy["allow_assistant_content"])

    def complete_local_pipeline(self, job_id, candidates):
        """Script only the external model boundary; run real preparation/adoption."""
        scripted = {
            "generate": {"questions": deepcopy(candidates)},
            "review": {"solutions": [{"index": index, "answer": candidate["answer"],
                                      "solvable": True, "reasoning": "Local test fixture solution"}
                                     for index, candidate in enumerate(candidates)]},
            "compare": {"comparisons": [{"index": index, "agrees": True, "valid": True,
                                          "reason": "Local test fixture agrees with the selected source"}
                                         for index in range(len(candidates))]},
        }
        job = self.content.get_job(job_id)
        stages = ("generate", "review", "compare") if job["stage"] == "generate" else ("review", "compare")
        fake = SimpleNamespace(complete=AsyncMock())
        bank = QuestionBank(self.content, self.learning, gateway=fake)
        for stage in stages:
            self.assertEqual(job["stage"], stage, job)
            self.assertEqual(job["status"], "pending", job)
            fake.complete.return_value = {
                "data": scripted[stage], "text": "local structured test fixture", "model": "fixture-model",
                "provider": "local-fixture", "request_id": f"{job_id}:{stage}", "request_sent": False,
                "reasoning_effort": "high", "usage": None,
            }
            job = asyncio.run(bank.execute_stage(job))
        self.assertEqual(fake.complete.await_count, len(stages))
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["stage"], "done")
        self.assertEqual(len(job["state"]["published_versions"]), len(candidates))
        return job

    def reviewed_latest_questions(self):
        course_id = self.course["id"]
        uploaded = self.expect(self.client.post(f"/api/v1/courses/{course_id}/materials", files={
            "file": ("workflow.txt", (
                "零加零等于零。并非所有整数都为偶数，奇数是一类反例。"
                "偶数是能够被二整除的整数，可以从整除定义说明原因。"
            ).encode("utf-8"), "text/plain")}, data={
                "title": "Workflow confirmed source", "license_code": "own", "rights_confirmed": "true",
                "allow_generation": "true", "allow_retrieval": "true", "external_processing_allowed": "true",
            }), 201)
        material = uploaded["material"]
        source = uploaded["blocks"][0]
        confirmed = self.expect(self.client.post(f"/api/v1/materials/{material['id']}/confirm", json={
            "expected_revision": material["revision"], "block_ids": [source["id"]],
        }))
        self.assertIn(source["id"], confirmed["confirmed_block_ids"])
        point = self.expect(self.client.post(f"/api/v1/courses/{course_id}/knowledge-points", json={
            "code": "K-WORKFLOW", "title": "Integer definitions", "source_block_ids": [source["id"]],
        }), 201)
        self.expect(self.client.post(f"/api/v1/courses/{course_id}/curriculum/knowledge/{point['id']}/confirm"))
        common = {"difficulty": "easy", "knowledge_points": [point["id"]], "source_refs": ["B1"],
                  "explanation": "PROTECTED-WORKFLOW-EXPLANATION", "rubric": [], "options": []}
        candidates = [
            {**common, "type": "single_choice", "stem": "零加零的结果请选择正确选项。", "options": ["零", "一"],
             "answer": 0, "points": 1},
            {**common, "type": "judge", "stem": "所有整数都是偶数。", "answer": False, "points": 2},
            {**common, "type": "fill", "stem": "可以被二整除的整数称为____。", "answer": "偶数",
             "points": 3, "answer_format": "text"},
            {**common, "type": "text", "stem": "请解释为什么零属于偶数。", "answer": "零等于二乘零，所以能被二整除。",
             "points": 4, "rubric": [
                 {"point": "给出整除定义 PROTECTED-WORKFLOW-RUBRIC", "score_units": 200},
                 {"point": "将定义应用到零", "score_units": 200},
             ]},
        ]
        created = self.expect(self.client.post(f"/api/v1/courses/{course_id}/question-jobs", json={
            "class_id": self.classroom["id"], "source_block_ids": [source["id"]],
            "question_types": dict.fromkeys(("single_choice", "fill", "judge", "text"), 1),
            "difficulty": "easy", "knowledge_points": [point["id"]], "external_processing_allowed": True,
        }), 202)["job"]
        completed = self.complete_local_pipeline(created["id"], candidates)
        choice_id = completed["state"]["published_versions"][0]["question_id"]
        # A real teacher edit and independent review create v2. The composer and
        # student must receive it while the authoritative v1 remains readable.
        edited_choice = {**candidates[0], "stem": "零加零的结果请选择正确选项（已审定新版）。", "points": 2,
                         "explanation": "PROTECTED-WORKFLOW-LATEST-EXPLANATION"}
        edited = self.expect(self.client.post(f"/api/v1/courses/{course_id}/questions/{choice_id}/edit-jobs", json={
            "class_id": self.classroom["id"], "source_block_ids": [source["id"]],
            "expected_version": 1, "question": edited_choice, "external_processing_allowed": True,
        }), 202)["job"]
        self.complete_local_pipeline(edited["id"], [edited_choice])
        library = self.expect(self.client.get(f"/api/v1/courses/{course_id}/question-library", params={
            "class_id": self.classroom["id"], "status": "approved", "difficulty": "easy", "knowledge_point": point["id"],
        }))
        self.assertEqual(library["total"], 4)
        self.assertEqual(Counter(item["content"]["type"] for item in library["items"]),
                         dict.fromkeys(("single_choice", "fill", "judge", "text"), 1))
        by_type = {item["content"]["type"]: item for item in library["items"]}
        self.assertEqual(by_type["single_choice"]["question_id"], choice_id)
        self.assertEqual(by_type["single_choice"]["version"], 2)
        self.assertEqual(by_type["single_choice"]["content"]["points"], 2)
        for item in library["items"]:
            self.assertEqual(item["content"]["knowledge_points"], [point["id"]])
            self.assertEqual(item["content"]["source_anchors"][0]["block_id"], source["id"])
            self.assertEqual(item["content"]["review_evidence"]["calls"]["review"]["provider"], "local-fixture")
        old = self.expect(self.client.get(f"/api/v1/courses/{course_id}/questions/{choice_id}/versions/1"))
        self.assertEqual(old["question"]["content"]["points"], 1)
        self.assertEqual(old["question"]["content"]["stem"], candidates[0]["stem"])
        self.assertEqual(self.content.list_pending_jobs(), [])
        return point, by_type

    def test_reviewed_latest_bank_to_selected_student_submission_manual_grade_and_history(self):
        self.as_user("teacher")
        point, bank = self.reviewed_latest_questions()
        config = {
            "course_id": self.course["id"], "class_id": self.classroom["id"], "title": "Four-type selected workflow",
            "description": "Only student a receives this paper", "student_ids": [self.users["a"]["id"]],
            "question_count": 4, "question_types": dict.fromkeys(("single_choice", "fill", "judge", "text"), 1),
            "common_level": "balanced", "difficulty": "easy", "knowledge_points": [point["id"]],
            "deadline_at": time.time() + 3600, "scheduled_at": None,
            "allow_late": False, "allow_retries": True, "allow_retry_after_deadline": False,
        }
        preview = self.expect(self.client.post("/api/v1/teaching-plans/preview", json=config))
        self.assertTrue(preview["ready"], preview)
        self.assertEqual(preview["total_points"], 11)
        self.assertEqual(preview["students"][0]["question_types"], config["question_types"])
        self.assertEqual([s["student_id"] for s in preview["students"]], [self.users["a"]["id"]])
        expected_versions = {item["question_id"]: item["version"] for item in bank.values()}
        self.assertEqual({q["question_id"]: q["version"] for q in preview["questions"]}, expected_versions)
        publication = self.expect(self.client.post("/api/v1/teaching-plans/publish", json={
            "config": config, "preview_token": preview["preview_token"], "request_id": "workflow-publish",
        }), 201)
        self.assertEqual(len(publication["task_ids"]), 1)
        task_id = publication["task_ids"][0]
        teacher_tasks = self.expect(self.client.get("/api/v1/tasks", params={"goal_id": publication["goal_id"]}))["items"]
        self.assertEqual([(t["id"], t["student_id"]) for t in teacher_tasks], [(task_id, self.users["a"]["id"])])
        self.as_user("b")
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks"))["items"], [])
        self.assertEqual(self.expect(self.client.get("/api/v1/goals"))["items"], [])
        self.expect(self.client.get(f"/api/v1/tasks/{task_id}"), 404)
        self.as_user("a")
        listed = self.expect(self.client.get("/api/v1/tasks", params={"goal_id": publication["goal_id"]}))["items"]
        self.assertEqual([item["id"] for item in listed], [task_id])
        task = self.task(task_id)
        self.assert_answers_closed(task)
        self.assertEqual({q["id"]: q["version"] for q in task["questions"]}, expected_versions)
        self.assertEqual(sum(q["points"] for q in task["questions"]), 11)
        ids = {kind: item["question_id"] for kind, item in bank.items()}
        answers = {ids["single_choice"]: 0, ids["judge"]: False, ids["fill"]: "偶数",
                   ids["text"]: "零可以写成二乘以整数零，所以能被二整除。"}
        lease = self.expect(self.client.post(f"/api/v1/tasks/{task_id}/edit-leases", json={
            "device_id": "workflow-student-browser", "lease_seconds": 300,
        }))
        draft = self.expect(self.client.put(f"/api/v1/tasks/{task_id}/draft", json={
            "answers": answers, "expected_version": lease["task_version"], "request_id": "workflow-save",
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        }))
        self.assertEqual(draft["draft_answers"], answers)
        self.assertIs(type(draft["draft_answers"][ids["single_choice"]]), int)
        self.assertIs(draft["draft_answers"][ids["judge"]], False)
        self.assertIs(type(draft["draft_answers"][ids["fill"]]), str)
        self.assert_answers_closed(draft)
        submitted = self.expect(self.client.post(f"/api/v1/tasks/{task_id}/submit", json={
            "expected_version": draft["version"], "request_id": "workflow-submit",
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        }), 202)
        self.assertEqual(submitted["status"], "grading")
        self.assertEqual(len(submitted["attempts"]), 1)
        attempt_id = submitted["attempts"][0]["id"]
        self.assertEqual(submitted["attempts"][0]["answers"], answers)
        self.assertEqual(submitted["first_attempt_id"], attempt_id)
        self.assert_answers_closed(submitted)
        # No service or queue methods are patched: production dispatch, claim,
        # objective grading and the manual fallback all execute once explicitly.
        worker = Worker(self.runtime, worker_id="teacher-workflow-worker")
        self.assertTrue(worker.run_once())
        job_id = submitted["grading_job_id"]
        self.assertEqual(self.runtime.queue.get_job(job_id).status, "succeeded")
        self.assertEqual(self.runtime.queue.list_calls(job_id), ())
        self.disabled_gateway.describe.assert_called_once_with("grading")
        steps = {step.name: step for step in self.runtime.queue.list_steps(job_id)}
        for kind, units in (("single_choice", 200), ("judge", 200), ("fill", 300)):
            self.assertEqual(steps[f"item:{ids[kind]}"].output["score_units"], units)
        self.assertEqual(steps[f"item:{ids['text']}"].output["status"], "manual_pending")
        waiting = self.task(task_id)
        self.assertEqual(waiting["status"], "awaiting_manual")
        self.assertEqual(waiting["attempts"][0]["status"], "review_required")
        self.assert_answers_closed(waiting)
        self.assertEqual(self.learning.list_grading_outbox(status="all")[0]["status"], "manual")
        self.as_user("teacher")
        frozen_text = next(q for q in self.task(task_id)["questions"] if q["type"] == "text")
        self.assertEqual(frozen_text["rubric"], bank["text"]["content"]["rubric"])
        manual = self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/{ids['text']}/manual-grade", json={
            "expected_revision": waiting["attempts"][0]["revision"], "score_units": 300,
            "reason": "按冻结量表给出三分，定义表述尚欠完整。", "request_id": "workflow-manual",
        }))
        self.assertEqual(manual["total_score"], 10)
        self.as_user("a")
        completed = self.task(task_id)
        self.assertEqual(completed["status"], "results_ready")
        self.assertTrue(completed["answer_released"])
        self.assertTrue(self.expect(self.client.get(f"/api/v1/tasks/{task_id}/answer-policy"))["results_released"])
        attempt = completed["attempts"][0]
        self.assertEqual((attempt["id"], attempt["kind"], attempt["attempt_no"]), (attempt_id, "first", 1))
        self.assertEqual((attempt["total_score_units"], attempt["total_score"], attempt["max_score"]), (1000, 10, 11))
        self.assertEqual(attempt["answers"], answers)
        self.assertEqual({item["question_id"]: item["score_units"] for item in attempt["items"]}, {
            ids["single_choice"]: 200, ids["judge"]: 200, ids["fill"]: 300, ids["text"]: 300,
        })
        released = {q["type"]: q for q in completed["questions"]}
        self.assertIs(type(released["single_choice"]["answer"]), int)
        self.assertEqual(released["single_choice"]["answer"], 0)
        self.assertIs(released["judge"]["answer"], False)
        self.assertEqual(released["fill"]["answer"], "偶数")
        self.assertEqual(released["text"]["rubric"], frozen_text["rubric"])
        # A teacher correction must update totals while retaining the original
        # submission, frozen versions and an auditable prior score in history.
        self.as_user("teacher")
        corrected = self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/{ids['text']}/corrections", json={
            "expected_revision": attempt["revision"], "score_units": 350,
            "reason": "复核学生解释后补给半分，保留初评分记录。", "request_id": "workflow-correction",
        }))
        self.assertEqual(corrected["total_score"], 10.5)
        self.as_user("a")
        history = self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/corrections"))["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["question_id"], history[0]["prior_score_units"], history[0]["corrected_score_units"]),
                         (ids["text"], 300, 350))
        final = self.task(task_id)
        self.assertEqual(len(final["attempts"]), 1)
        self.assertEqual(final["first_attempt_id"], attempt_id)
        self.assertEqual(final["attempts"][0]["answers"], answers)
        self.assertEqual(final["attempts"][0]["submitted_at"], attempt["submitted_at"])
        self.assertEqual(final["attempts"][0]["total_score_units"], 1050)
        self.assertEqual(final["attempts"][0]["total_score"], 10.5)
        self.assertEqual(final["attempts"][0]["revision"], corrected["grading_revision"])
        self.assertEqual({q["id"]: q["version"] for q in final["questions"]}, expected_versions)
        self.assertEqual(self.learning.list_grading_outbox(status="all")[0]["status"], "completed")
        self.assertFalse(worker.run_once(), "the completed workflow must not enqueue more grading or model work")
        self.assertEqual([job.job_id for job in self.runtime.queue.list_jobs()], [job_id])
        self.as_user("b")
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks"))["items"], [])
        self.expect(self.client.get(f"/api/v1/tasks/{task_id}"), 404)
        self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/corrections"), 404)
        self.as_user("teacher")
        usage = self.expect(self.client.get(
            f"/api/v1/courses/{self.course['id']}/questions/{ids['text']}/versions/1"))["usage"]
        self.assertEqual((usage["task_count"], usage["student_count"], usage["first_graded_count"]), (1, 1, 1))
        self.assertEqual(usage["first_average_score"], 3.5)


if __name__ == "__main__":
    unittest.main()
