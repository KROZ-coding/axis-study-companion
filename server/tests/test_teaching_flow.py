"""Teaching API contracts, using one process and disposable SQLite stores only."""

import atexit
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import io
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

# main creates an application at import time. Never let it read the developer DB
# or load server/.env, including when this module is run without a test runner.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="teaching-api-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name,
    STUDY_DATA_DIR=_IMPORT_DATA.name, PERSISTENCE_ENABLED="false",
    STUDY_PERSISTENCE_ENABLED="false",
)

from fastapi.testclient import TestClient

with patch("sqlite3.connect", side_effect=AssertionError("app import must not open SQLite")):
    from app.main import app as import_application, create_app

from app.config import Settings
from app.services.study_demo import SAMPLE


API = "/api/teaching"
TEACHER = {"X-Demo-Role": "teacher"}


def student_headers(student_id="S07"):
    return {"X-Demo-Role": "student", "X-Demo-Student-Id": student_id}


class TeachingFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="teaching-api-test-")
        self.addCleanup(self.directory.cleanup)
        self.config = Settings(
            app_env="test", demo_enabled=True, worker_enabled=False, data_dir=self.directory.name, persistence_enabled=True,
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="",
            maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(),
        )
        self.app = create_app(self.config)
        self.client = self.new_client(self.app)
        self.sequence = 0
        self.material_id = next(row["id"] for row in SAMPLE["materials"] if row.get("kp") == "K5")
        self.question_id = SAMPLE["practice"]["K5"]["id"]
        self.correct_answer = SAMPLE["practice"]["K5"]["answer"]

    def new_client(self, application):
        client = TestClient(application)
        self.addCleanup(client.close)
        return client

    def key(self):
        self.sequence += 1
        return f"api-test-{self.sequence}"

    def request(self, method, path, *, body=None, headers=None, status=200, client=None, **kwargs):
        response = (client or self.client).request(
            method, API + path, json=body, headers=TEACHER if headers is None else headers, **kwargs,
        )
        self.assertEqual(response.status_code, status, f"{method} {path}: {response.text}")
        return response

    def review(self, students=("S07", "S08"), **overrides):
        body = dict(
            request_id=self.key(), title="去括号学习回顾", topic="T2",
            student_ids=list(students), knowledge_point_ids=["K5"],
            date_from="2026-10-08", date_to="2026-11-03",
        )
        body.update(overrides)
        return self.request("POST", "/reviews", body=body, status=201).json()

    def followup_body(self, review, students=None):
        return dict(
            request_id=self.key(), review_id=review["id"], title="去括号跟进",
            goal="逐项乘入并解释符号变化", knowledge_point_id="K5",
            student_ids=list(review["student_ids"] if students is None else students),
            material_ids=[self.material_id], question_ids=[self.question_id],
            instructions="先阅读指定材料，再完成验证题。", completion_condition="提交后等待教师反馈。",
        )

    def followup(self, review=None, students=None):
        review = self.review() if review is None else review
        return self.request(
            "POST", "/followups", body=self.followup_body(review, students), status=201,
        ).json()

    def version_body(self, row, **extra):
        return {"request_id": self.key(), "expected_version": row["version"], **extra}

    def publish(self, followup=None):
        followup = self.followup() if followup is None else followup
        return self.request(
            "POST", f"/followups/{followup['id']}/publish", body=self.version_body(followup),
        ).json()

    def tasks(self, student="S07", client=None):
        return self.request("GET", "/tasks", headers=student_headers(student), client=client).json()["tasks"]

    def get_task(self, task, *, teacher=False, client=None):
        return self.request(
            "GET", f"/tasks/{task['id']}",
            headers=TEACHER if teacher else student_headers(task["student_id"]), client=client,
        ).json()

    def save_body(self, task, answer=None):
        return self.version_body(
            task, answers={self.question_id: self.correct_answer if answer is None else answer},
            read_material_ids=[self.material_id],
        )

    def save(self, task, answer=None):
        return self.request(
            "PUT", f"/tasks/{task['id']}/draft", body=self.save_body(task, answer),
            headers=student_headers(task["student_id"]),
        ).json()

    def submit(self, task):
        return self.request(
            "POST", f"/tasks/{task['id']}/submit", body=self.version_body(task),
            headers=student_headers(task["student_id"]),
        ).json()

    def feedback_body(self, task, decision="completed", comment="已核实本次作答"):
        return self.version_body(
            task, attempt_id=task["attempt_ids"][-1], decision=decision, comment=comment,
        )

    def feedback(self, task, decision="completed", comment="已核实本次作答"):
        return self.request(
            "POST", f"/tasks/{task['id']}/feedback", body=self.feedback_body(task, decision, comment),
        ).json()

    def assert_no_answers(self, task):
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["attempt_ids"], [])
        for question in task["content"]["questions"]:
            self.assertEqual(set(question), {"id", "stem", "options"})

    def assert_rejected_unchanged(self, method, path, body, *, status, headers=None):
        before = self.app.state.store.teaching_snapshot()
        response = self.request(method, path, body=body, status=status, headers=headers)
        self.assertEqual(self.app.state.store.teaching_snapshot(), before)
        return response

    def test_import_application_is_memory_only_and_opens_no_sqlite(self):
        self.assertIsNone(import_application.state.store)
        self.assertFalse(Path(_IMPORT_DATA.name, "store.sqlite3").exists())
        self.review(students=("S07",))
        self.assertTrue(Path(self.directory.name, "store.sqlite3").is_file())
        self.assertEqual(self.app.state.store.users, {})
        self.assertEqual(self.app.state.store.courses, {})

    def test_complete_review_publish_draft_submit_feedback_flow(self):
        context = self.request("GET", "/context").json()
        self.assertEqual(context["actor"]["role"], "teacher")
        review = self.review()
        self.assertEqual(set(review["student_ids"]), {"S07", "S08"})
        self.assertEqual(review["knowledge_point_ids"], ["K5"])
        self.assertEqual(self.request("GET", f"/reviews/{review['id']}").json(), review)
        followup = self.publish(self.followup(review))
        self.assertEqual(followup["status"], "published")
        task = self.tasks()[0]
        self.assertEqual(task["status"], "not_started")
        self.assertEqual(task["followup_id"], followup["id"])
        self.assert_no_answers(task)
        task = self.request(
            "POST", f"/tasks/{task['id']}/start", body=self.version_body(task), headers=student_headers(),
        ).json()
        self.assertEqual(task["status"], "in_progress")
        task = self.save(task)
        self.assert_no_answers(task)
        task = self.submit(task)
        self.assertEqual(task["status"], "submitted")
        self.assertEqual(len(task["attempts"]), 1)
        attempt = task["attempts"][0]
        self.assertEqual(attempt["answers"], {self.question_id: self.correct_answer})
        self.assertEqual(attempt["grading"]["total"], 1)
        self.assertEqual(attempt["grading"]["graded_by"], "rule")
        self.assertIn("不更新历史诊断", attempt["grading"]["boundary"])
        self.assertEqual(self.request("GET", "/dashboard").json()["pending_review_count"], 1)
        task = self.feedback(task)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["feedback"][0]["attempt_id"], attempt["id"])
        self.assertEqual(task["feedback"][0]["teacher_id"], "demo-teacher")
        self.assertEqual(self.get_task(task), task)
        dashboard = self.request("GET", "/dashboard").json()
        self.assertEqual(dashboard["pending_review_count"], 0)
        self.assertEqual(dashboard["published_count"], 1)
        progress = self.request("GET", f"/followups/{followup['id']}").json()["progress"]
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["not_started"], 1)
        for method, suffix, body in (
            ("POST", "start", self.version_body(task)),
            ("PUT", "draft", self.save_body(task)),
            ("POST", "submit", self.version_body(task)),
        ):
            self.assert_rejected_unchanged(
                method, f"/tasks/{task['id']}/{suffix}", body, status=409, headers=student_headers(),
            )

    def test_draft_followup_invisible_until_publish_and_draft_updates_are_versioned(self):
        followup = self.followup()
        self.assertEqual(followup["status"], "draft")
        self.assertEqual(self.tasks(), [])
        self.assertEqual(self.tasks("S08"), [])
        self.assertEqual(self.app.state.store.teaching_snapshot()["tasks"], {})
        self.request("GET", f"/followups/{followup['id']}", headers=student_headers(), status=403)
        self.request("GET", f"/followups/{followup['id']}/preview/S07", status=404)
        body = self.followup_body(self.request("GET", f"/reviews/{followup['review_id']}").json())
        body.pop("review_id")
        body.update(expected_version=followup["version"], title="修订后跟进", student_ids=["S07"])
        updated = self.request("PUT", f"/followups/{followup['id']}", body=body).json()
        self.assertEqual(updated["version"], followup["version"] + 1)
        self.assert_rejected_unchanged(
            "PUT", f"/followups/{followup['id']}", {**body, "request_id": self.key()}, status=409,
        )
        published = self.publish(updated)
        self.assertEqual(self.tasks("S08"), [])
        self.assertEqual(self.tasks()[0]["title"], "修订后跟进")
        self.assert_rejected_unchanged(
            "PUT", f"/followups/{followup['id']}",
            {**body, "request_id": self.key(), "expected_version": published["version"]}, status=409,
        )

    def test_multi_student_task_and_basis_ownership_isolation(self):
        followup = self.publish()
        first, second = self.tasks()[0], self.tasks("S08")[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.tasks("S09"), [])
        for task in (first, second):
            self.assertEqual({row["student_id"] for row in task["content"]["basis"]}, {task["student_id"]})
        for outsider in ("S08", "S09"):
            for method, suffix, body in (
                ("GET", "", None),
                ("POST", "/start", self.version_body(first)),
                ("PUT", "/draft", self.save_body(first)),
                ("POST", "/submit", self.version_body(first)),
            ):
                with self.subTest(outsider=outsider, method=method, suffix=suffix):
                    self.assert_rejected_unchanged(
                        method, f"/tasks/{first['id']}{suffix}", body,
                        status=404, headers=student_headers(outsider),
                    )
        saved = self.save(first)
        self.assertEqual(self.get_task(second), second)
        preview = self.request("GET", f"/followups/{followup['id']}/preview/S07").json()
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview["preview_student_id"], "S07")
        self.assertEqual(preview["task"], saved)
        self.request("GET", f"/followups/{followup['id']}/preview/S09", status=404)
        self.request("GET", "/insights?student_id=S08", headers=student_headers(), status=403)
        own = self.request("GET", "/insights", headers=student_headers()).json()
        self.assertEqual(own["context"]["scope"]["student_ids"], ["S07"])
        self.assertEqual(set(own["stats"]), {"S07"})

    def test_answers_and_explanations_only_appear_after_that_students_submission(self):
        followup = self.publish()
        first, second = self.tasks()[0], self.tasks("S08")[0]
        self.assert_no_answers(first)
        self.assert_no_answers(self.get_task(first, teacher=True))
        public_question = self.request("GET", f"/questions/{self.question_id}", headers=student_headers()).json()
        self.assertEqual(set(public_question), {"id", "stem", "options"})
        self.assertIn("answer", self.request("GET", f"/questions/{self.question_id}").json())
        saved = self.save(first)
        self.assert_no_answers(saved)
        self.assert_no_answers(self.get_task(saved))
        submitted = self.submit(saved)
        question = submitted["content"]["questions"][0]
        self.assertEqual(question["answer"], self.correct_answer)
        self.assertEqual(question["why"], SAMPLE["practice"]["K5"]["why"])
        self.assert_no_answers(self.get_task(second))
        self.assert_no_answers(self.request("GET", f"/followups/{followup['id']}/preview/S08").json()["task"])

    def test_published_material_questions_grading_and_review_are_frozen(self):
        followup = self.publish()
        task = self.tasks()[0]
        review_before = self.request("GET", f"/reviews/{followup['review_id']}").json()
        frozen = deepcopy(followup["frozen_content"])
        material = next(row for row in self.app.state.study_data["materials"] if row["id"] == self.material_id)
        material["title"] = "被后来编辑的材料"
        material["body"].append("后续新增的说明")
        practice = self.app.state.study_data["practice"]["K5"]
        practice.update(stem="后续替换题干", answer=0, why="后续答案解释")
        practice["options"][0] = "后续修改选项"
        self.app.state.study_data["records"][0]["correct"] = not self.app.state.study_data["records"][0]["correct"]
        self.assertEqual(self.request("GET", f"/materials/{self.material_id}").json()["title"], "被后来编辑的材料")
        self.assertEqual(self.get_task(task), task)
        submitted = self.submit(self.save(task))
        self.assertEqual(submitted["attempts"][0]["grading"]["total"], 1)
        self.assertEqual(submitted["content"]["materials"], frozen["materials"])
        self.assertEqual(submitted["content"]["questions"], [
            {key: question[key] for key in ("id", "stem", "options", "answer", "why")}
            for question in frozen["questions"]
        ])
        current = self.request("GET", f"/followups/{followup['id']}").json()
        self.assertEqual(current["frozen_content"], frozen)
        self.assertEqual(self.request("GET", f"/reviews/{followup['review_id']}").json(), review_before)

    def test_draft_refresh_and_fresh_app_restart_restore_facts_and_idempotency(self):
        followup = self.publish()
        task = self.tasks()[0]
        draft_body = self.save_body(task)
        saved = self.request("PUT", f"/tasks/{task['id']}/draft", body=draft_body, headers=student_headers()).json()
        refreshed = self.new_client(self.app)
        self.assertEqual(self.get_task(saved, client=refreshed), saved)
        before_restart = self.app.state.store.teaching_snapshot()
        self.client.close()
        refreshed.close()
        self.app = create_app(self.config)
        self.client = self.new_client(self.app)
        self.assertEqual(self.app.state.store.teaching_snapshot(), before_restart)
        self.assertEqual(self.tasks()[0], saved)
        replay = self.request("PUT", f"/tasks/{task['id']}/draft", body=draft_body, headers=student_headers()).json()
        self.assertEqual(replay, saved)
        submitted = self.submit(saved)
        completed = self.feedback(submitted)
        expected_state = self.app.state.store.teaching_snapshot()
        self.client.close()
        self.app = create_app(self.config)
        self.client = self.new_client(self.app)
        self.assertEqual(self.app.state.store.teaching_snapshot(), expected_state)
        self.assertEqual(self.get_task(completed), completed)
        self.assertEqual(self.request("GET", f"/followups/{followup['id']}").json()["progress"]["completed"], 1)
        self.assertEqual(self.tasks("S08")[0]["status"], "not_started")

    def test_retry_retains_original_attempt_and_feedback_and_rejects_stale_attempt(self):
        self.publish()
        first = self.submit(self.save(self.tasks()[0], answer=0))
        first_attempt = deepcopy(first["attempts"][0])
        self.assertEqual(first_attempt["grading"]["total"], 0)
        retry = self.feedback(first, "needs_retry", "请逐项乘入后再试")
        first_feedback = deepcopy(retry["feedback"][0])
        self.assertEqual(retry["status"], "needs_retry")
        self.assertEqual(retry["draft_answers"], {})
        self.assertEqual(retry["attempts"], [first_attempt])
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{retry['id']}/submit", self.version_body(retry),
            status=422, headers=student_headers(),
        )
        second = self.submit(self.save(retry))
        self.assertEqual([item["number"] for item in second["attempts"]], [1, 2])
        self.assertEqual(second["attempts"][0], first_attempt)
        self.assertEqual(second["attempts"][1]["grading"]["total"], 1)
        self.assertNotEqual(second["attempt_ids"][0], second["attempt_ids"][1])
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{second['id']}/feedback",
            {**self.feedback_body(second), "attempt_id": first_attempt["id"]}, status=409,
        )
        completed = self.feedback(second)
        self.assertEqual(completed["attempts"][0], first_attempt)
        self.assertEqual(completed["feedback"][0], first_feedback)
        self.assertEqual([row["decision"] for row in completed["feedback"]], ["needs_retry", "completed"])
        self.assertEqual(completed["feedback"][1]["attempt_id"], second["attempt_ids"][1])
        self.assertEqual(self.get_task(completed), completed)

    def test_closed_followup_rejects_start_save_submit_retry_but_allows_final_feedback(self):
        followup = self.publish()
        submitted = self.submit(self.save(self.tasks()[0]))
        untouched = self.tasks("S08")[0]
        closed = self.request("POST", f"/followups/{followup['id']}/close", body=self.version_body(followup)).json()
        self.assertEqual(closed["status"], "closed")
        for method, action, body in (
            ("POST", "start", self.version_body(untouched)),
            ("PUT", "draft", self.save_body(untouched)),
            ("POST", "submit", self.version_body(untouched)),
        ):
            self.assert_rejected_unchanged(
                method, f"/tasks/{untouched['id']}/{action}", body, status=409, headers=student_headers("S08"),
            )
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{submitted['id']}/feedback", self.feedback_body(submitted, "needs_retry"), status=409,
        )
        self.assertEqual(self.feedback(submitted)["status"], "completed")
        for action in ("publish", "close"):
            self.assert_rejected_unchanged(
                "POST", f"/followups/{followup['id']}/{action}", self.version_body(closed), status=409,
            )

    def test_replays_return_original_response_after_later_versions_and_key_reuse_conflicts(self):
        def replay(method, path, body, *, headers=None, status=200):
            first = self.request(method, path, body=body, headers=headers, status=status).json()
            second = self.request(method, path, body=body, headers=headers, status=status).json()
            self.assertEqual(second, first)
            return first

        review_body = dict(request_id=self.key(), title="幂等回顾", student_ids=["S07"], knowledge_point_ids=["K5"])
        review = replay("POST", "/reviews", review_body, status=201)
        self.assert_rejected_unchanged(
            "POST", "/reviews", {**review_body, "title": "参数变化"}, status=409,
        )
        followup_body = self.followup_body(review)
        followup = replay("POST", "/followups", followup_body, status=201)
        update_body = {key: value for key, value in followup_body.items() if key != "review_id"}
        update_body.update(self.version_body(followup), title="修订后发布")
        followup = replay("PUT", f"/followups/{followup['id']}", update_body)
        publish_body = self.version_body(followup)
        published = replay("POST", f"/followups/{followup['id']}/publish", publish_body)
        task = self.tasks()[0]
        start_body = self.version_body(task)
        started = replay("POST", f"/tasks/{task['id']}/start", start_body, headers=student_headers())
        save_body = self.save_body(started)
        saved = replay("PUT", f"/tasks/{task['id']}/draft", save_body, headers=student_headers())
        self.assert_rejected_unchanged(
            "PUT", f"/tasks/{task['id']}/draft", {**save_body, "answers": {self.question_id: 0}},
            status=409, headers=student_headers(),
        )
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{task['id']}/submit", {"request_id": save_body["request_id"], "expected_version": saved["version"]},
            status=409, headers=student_headers(),
        )
        submit_body = self.version_body(saved)
        submitted = replay("POST", f"/tasks/{task['id']}/submit", submit_body, headers=student_headers())
        feedback_body = self.feedback_body(submitted)
        completed = replay("POST", f"/tasks/{task['id']}/feedback", feedback_body)
        self.assertEqual(self.request(
            "POST", f"/tasks/{task['id']}/submit", body=submit_body, headers=student_headers(),
        ).json(), submitted)
        self.assertEqual(self.request(
            "PUT", f"/tasks/{task['id']}/draft", body=save_body, headers=student_headers(),
        ).json(), saved)
        self.assertEqual(self.get_task(completed), completed)
        close_body = self.version_body(published)
        replay("POST", f"/followups/{followup['id']}/close", close_body)
        self.assertEqual(self.request(
            "POST", f"/followups/{followup['id']}/publish", body=publish_body,
        ).json(), published)
        state = self.app.state.store.teaching_snapshot()
        for collection in ("reviews", "followups", "tasks", "attempts", "feedback"):
            self.assertEqual(len(state[collection]), 1, collection)

    def test_same_request_id_is_independent_for_different_student_actors(self):
        self.publish()
        for student in ("S07", "S08"):
            task = self.tasks(student)[0]
            body = {**self.save_body(task), "request_id": "shared-student-key"}
            saved = self.request("PUT", f"/tasks/{task['id']}/draft", body=body, headers=student_headers(student)).json()
            self.assertEqual(saved["student_id"], student)
            self.assertEqual(saved["version"], task["version"] + 1)

    def test_stale_versions_on_every_versioned_operation_leave_state_unchanged(self):
        draft = self.followup()
        followup = self.publish(draft)
        self.assert_rejected_unchanged(
            "POST", f"/followups/{followup['id']}/publish", self.version_body(draft), status=409,
        )
        self.assert_rejected_unchanged(
            "POST", f"/followups/{followup['id']}/close", self.version_body(draft), status=409,
        )
        original = self.tasks()[0]
        saved = self.save(original)
        for method, action, body in (
            ("POST", "start", self.version_body(original)),
            ("PUT", "draft", self.save_body(original)),
            ("POST", "submit", self.version_body(original)),
        ):
            self.assert_rejected_unchanged(
                method, f"/tasks/{original['id']}/{action}", body, status=409, headers=student_headers(),
            )
        submitted = self.submit(saved)
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{submitted['id']}/feedback",
            {**self.feedback_body(submitted), "expected_version": saved["version"]}, status=409,
        )
        self.assertEqual(self.feedback(submitted)["status"], "completed")

    def test_illegal_answers_materials_and_incomplete_submissions_are_422_and_atomic(self):
        self.publish()
        task = self.tasks()[0]
        for value in (True, False, "1", 1.0, None, -1, 4, [], {}):
            with self.subTest(answer=value):
                body = {**self.save_body(task), "answers": {self.question_id: value}}
                self.assert_rejected_unchanged(
                    "PUT", f"/tasks/{task['id']}/draft", body, status=422, headers=student_headers(),
                )
        for changes in (
            {"answers": {"P-not-assigned": 1}},
            {"read_material_ids": ["M-not-assigned"]},
            {"read_material_ids": [self.material_id, self.material_id]},
            {"unexpected": "must not be silently accepted"},
            {"expected_version": True}, {"expected_version": "1"}, {"expected_version": 0},
            {"request_id": "contains spaces"},
        ):
            with self.subTest(changes=changes):
                self.assert_rejected_unchanged(
                    "PUT", f"/tasks/{task['id']}/draft", {**self.save_body(task), **changes},
                    status=422, headers=student_headers(),
                )
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{task['id']}/submit", self.version_body(task), status=422, headers=student_headers(),
        )
        for answers, materials in (({}, [self.material_id]), ({self.question_id: self.correct_answer}, [])):
            task = self.request(
                "PUT", f"/tasks/{task['id']}/draft",
                body=self.version_body(task, answers=answers, read_material_ids=materials), headers=student_headers(),
            ).json()
            self.assert_rejected_unchanged(
                "POST", f"/tasks/{task['id']}/submit", self.version_body(task), status=422, headers=student_headers(),
            )
        submitted = self.submit(self.save(task))
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{task['id']}/feedback",
            {**self.feedback_body(submitted), "decision": "approved"}, status=422,
        )

    def test_unknown_scope_and_invalid_followup_references_are_errors_without_writes(self):
        for changes in ({"topic": "missing"}, {"student_ids": ["missing"]}, {"knowledge_point_ids": ["missing"]}):
            body = dict(request_id=self.key(), title="无效范围", **changes)
            with self.subTest(changes=changes):
                self.assert_rejected_unchanged("POST", "/reviews", body, status=404)
        self.assert_rejected_unchanged(
            "POST", "/reviews", dict(request_id=self.key(), title="日期颠倒", date_from="2026-11-03", date_to="2026-10-08"),
            status=422,
        )
        review = self.review(students=("S07",))
        for changes in (
            {"student_ids": ["S08"]}, {"student_ids": ["S07", "S07"]},
            {"knowledge_point_id": "K4"}, {"material_ids": ["M-01"]},
            {"question_ids": ["P-401"]}, {"question_ids": [self.question_id, self.question_id]},
            {"material_ids": [self.material_id, self.material_id]},
        ):
            with self.subTest(changes=changes):
                self.assert_rejected_unchanged(
                    "POST", "/followups", {**self.followup_body(review), **changes}, status=422,
                )

    def test_explicit_role_student_identity_and_class_headers_are_required(self):
        for headers, expected in (
            ({}, 401), ({"X-Demo-Role": "admin"}, 401),
            ({"X-Demo-Role": "student"}, 403), (student_headers("arbitrary-user"), 403),
            ({**TEACHER, "X-Demo-Class-Id": "another-class"}, 403),
            ({**student_headers(), "X-Demo-Class-Id": "another-class"}, 403),
            ({**TEACHER, "X-Demo-Student-Id": "S07"}, 422),
        ):
            with self.subTest(headers=headers):
                self.request("GET", "/context", headers=headers, status=expected)
        self.publish()
        task = self.tasks()[0]
        for headers in (student_headers("arbitrary-user"), {**student_headers(), "X-Demo-Class-Id": "another-class"}):
            self.assert_rejected_unchanged(
                "PUT", f"/tasks/{task['id']}/draft", self.save_body(task), status=403, headers=headers,
            )
        self.request("GET", "/tasks", headers=TEACHER, status=403)

    def test_students_cannot_use_any_teacher_endpoint_and_teacher_cannot_submit_for_student(self):
        followup = self.publish()
        task = self.tasks()[0]
        review = self.request("GET", f"/reviews/{followup['review_id']}").json()
        followup_body = self.followup_body(review)
        update_body = {key: value for key, value in followup_body.items() if key != "review_id"}
        update_body.update(self.version_body(followup))
        endpoints = (
            ("GET", "/dashboard", None), ("GET", "/reviews", None),
            ("GET", f"/reviews/{review['id']}", None),
            ("POST", "/reviews", dict(request_id=self.key(), title="越权回顾")),
            ("GET", "/followups", None), ("GET", f"/followups/{followup['id']}", None),
            ("POST", "/followups", followup_body), ("PUT", f"/followups/{followup['id']}", update_body),
            ("POST", f"/followups/{followup['id']}/publish", self.version_body(followup)),
            ("POST", f"/followups/{followup['id']}/close", self.version_body(followup)),
            ("GET", f"/followups/{followup['id']}/preview/S07", None),
            ("GET", f"/followups/{followup['id']}/report.json", None),
            ("GET", f"/followups/{followup['id']}/report.csv", None),
            ("POST", f"/tasks/{task['id']}/feedback", self.version_body(
                task, attempt_id="attempt-not-created", decision="completed", comment="越权反馈",
            )),
        )
        for method, path, body in endpoints:
            with self.subTest(method=method, path=path):
                self.assert_rejected_unchanged(method, path, body, status=403, headers=student_headers())
        for method, action, body in (
            ("POST", "start", self.version_body(task)), ("PUT", "draft", self.save_body(task)),
            ("POST", "submit", self.version_body(task)),
        ):
            self.assert_rejected_unchanged(method, f"/tasks/{task['id']}/{action}", body, status=403)
        # Feedback is checked on a genuinely submitted task, not only an invalid state.
        submitted = self.submit(self.save(task))
        self.assert_rejected_unchanged(
            "POST", f"/tasks/{task['id']}/feedback", self.feedback_body(submitted), status=403, headers=student_headers(),
        )

    def test_failed_saves_return_503_rollback_memory_disk_and_key_then_retry_succeeds(self):
        draft = self.followup()
        publish_body = self.version_body(draft)
        cases = [("POST", f"/followups/{draft['id']}/publish", publish_body, TEACHER)]
        for stage in ("publish", "save", "submit", "feedback"):
            if stage == "save":
                task = self.tasks()[0]
                cases = [("PUT", f"/tasks/{task['id']}/draft", self.save_body(task), student_headers())]
            elif stage == "submit":
                task = self.tasks()[0]
                cases = [("POST", f"/tasks/{task['id']}/submit", self.version_body(task), student_headers())]
            elif stage == "feedback":
                task = self.tasks()[0]
                cases = [("POST", f"/tasks/{task['id']}/feedback", self.feedback_body(task), TEACHER)]
            method, path, body, headers = cases[0]
            for failure in (False, OSError("simulated disk failure")):
                before = self.app.state.store.teaching_snapshot()
                disk_before = Path(self.directory.name, "store.sqlite3").read_bytes()
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with self.subTest(stage=stage, failure=repr(failure)), patch.object(self.app.state.store, "save", **kwargs):
                    response = self.request(method, path, body=body, headers=headers, status=503)
                self.assertIn("撤回", response.json()["detail"])
                self.assertEqual(self.app.state.store.teaching_snapshot(), before)
                self.assertEqual(Path(self.directory.name, "store.sqlite3").read_bytes(), disk_before)
                restarted = create_app(self.config)
                self.assertEqual(restarted.state.store.teaching_snapshot(), before)
            success = self.request(method, path, body=body, headers=headers).json()
            self.assertEqual(self.request(method, path, body=body, headers=headers).json(), success)
        state = self.app.state.store.teaching_snapshot()
        self.assertEqual(len(state["tasks"]), 2)
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(len(state["feedback"]), 1)

    def test_json_csv_reports_have_same_scope_and_never_rewrite_historical_diagnosis(self):
        insight_before = self.request("GET", "/insights?topic=T2").json()
        legacy_json_before = self.client.get("/api/study/export.json?topic=T2&student_id=S07").json()
        legacy_csv_before = self.client.get("/api/study/export.csv?topic=T2&student_id=S07").content
        records_before = deepcopy(self.app.state.study_data["records"])
        review = self.review(students=("S07", "S08", "S09"))
        followup = self.publish(self.followup(review, students=("S07", "S08")))
        self.feedback(self.submit(self.save(self.tasks()[0])), comment="=SUM(1,2)\n需要保留逗号与换行")
        self.submit(self.save(self.tasks("S08")[0], answer=0))
        json_response = self.request("GET", f"/followups/{followup['id']}/report.json")
        report = json_response.json()
        csv_response = self.request("GET", f"/followups/{followup['id']}/report.csv")
        self.assertIn("attachment", json_response.headers["content-disposition"])
        self.assertIn("text/csv", csv_response.headers["content-type"])
        self.assertTrue(csv_response.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        tasks = {row["id"]: row for row in report["followup"]["tasks"]}
        self.assertEqual(report["scope"], {"topic": "T2", "student_ids": ["S07", "S08"],
                                         "knowledge_point_ids": ["K5"],
                                         "date_from": "2026-10-08", "date_to": "2026-11-03"})
        self.assertEqual({row["student_id"] for row in rows}, {"S07", "S08"})
        self.assertEqual({row["task_id"] for row in rows}, set(tasks))
        self.assertEqual(len(rows), 2)
        for row in rows:
            task = tasks[row["task_id"]]
            latest = task["attempts"][-1]
            for key in ("class_id", "review_id", "data_version", "boundary"):
                self.assertEqual(row[key], report[key])
            self.assertEqual(row["review_version"], str(report["review_version"]))
            self.assertEqual(row["followup_id"], followup["id"])
            self.assertEqual(row["student_id"], task["student_id"])
            self.assertEqual(row["status"], task["status"])
            self.assertEqual(int(row["task_version"]), task["version"])
            self.assertEqual(int(row["attempt_count"]), len(task["attempts"]))
            self.assertEqual(row["latest_attempt_id"], latest["id"])
            self.assertEqual(float(row["rule_score"]), latest["grading"]["total"])
            comment = task["feedback"][-1]["comment"] if task["feedback"] else ""
            self.assertEqual(row["feedback"], "'" + comment if comment.startswith("=") else comment)
        self.assertEqual(report["followup"]["frozen_content"]["basis"], [
            row for row in review["snapshot"]["learning_analysis"] if row["student_id"] in {"S07", "S08"}
        ])
        self.assertEqual(self.request("GET", f"/reviews/{review['id']}").json(), review)
        self.assertEqual(self.request("GET", "/insights?topic=T2").json(), insight_before)
        self.assertEqual(self.client.get("/api/study/export.json?topic=T2&student_id=S07").json(), legacy_json_before)
        self.assertEqual(self.client.get("/api/study/export.csv?topic=T2&student_id=S07").content, legacy_csv_before)
        self.assertEqual(self.app.state.study_data["records"], records_before)
        self.assertEqual(self.app.state.store.mastery, {})
        self.assertEqual(self.app.state.store.practice_answers("S07"), {})

    def test_cached_success_replay_needs_no_new_save_even_when_storage_is_unavailable(self):
        review_body = dict(request_id=self.key(), title="幂等回顾", student_ids=["S07"], knowledge_point_ids=["K5"])
        review = self.request("POST", "/reviews", body=review_body, status=201).json()
        followup = self.publish(self.followup(review))
        task = self.save(self.tasks()[0])
        submit_body = self.version_body(task)
        submitted = self.request("POST", f"/tasks/{task['id']}/submit", body=submit_body, headers=student_headers()).json()
        self.feedback(submitted)
        before = self.app.state.store.teaching_snapshot()
        with patch.object(self.app.state.store, "save", return_value=False) as save:
            self.assertEqual(self.request("POST", "/reviews", body=review_body, status=201).json(), review)
            self.assertEqual(self.request("POST", f"/tasks/{task['id']}/submit", body=submit_body,
                                          headers=student_headers()).json(), submitted)
            save.assert_not_called()
        self.assertEqual(self.app.state.store.teaching_snapshot(), before)
        self.assertEqual(len(before["attempts"]), 1)
        self.assertEqual(self.request("GET", f"/followups/{followup['id']}").json()["progress"]["completed"], 1)

    def test_single_day_report_retains_frozen_scope_and_raw_evidence_after_source_change(self):
        review = self.review(students=("S07", "S08"), date_from="2026-10-15", date_to="2026-10-15")
        followup = self.publish(self.followup(review, students=("S07",)))
        expected_records = [row for row in review["snapshot"]["records"] if row["sid"] == "S07"]
        self.assertTrue(expected_records)
        self.app.state.study_data["records"].clear()
        report = self.request("GET", f"/followups/{followup['id']}/report.json").json()
        self.assertEqual(report["scope"], {"topic": "T2", "student_ids": ["S07"],
                                         "knowledge_point_ids": ["K5"],
                                         "date_from": "2026-10-15", "date_to": "2026-10-15"})
        history = report["historical_snapshot"]
        self.assertEqual(history["scope"], report["scope"])
        self.assertEqual(history["records"], expected_records)
        self.assertEqual({row["id"] for row in history["students"]}, {"S07"})
        evidence = {row["id"] for row in history["records"]}
        self.assertTrue(set(history["learning_analysis"][0]["evidence_ids"].split("|")).issubset(evidence))
        self.assertEqual(report["followup"]["frozen_content"]["historical_snapshot"], history)
        response = self.request("GET", f"/followups/{followup['id']}/report.csv")
        row = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(row["date_from"], "2026-10-15")
        self.assertEqual(row["date_to"], "2026-10-15")
        self.assertEqual(row["knowledge_point_id"], "K5")
        self.assertEqual(row["evidence_ids"], history["learning_analysis"][0]["evidence_ids"])
        self.assertEqual(row["data_version"], review["data_version"])

    def test_csv_latest_attempt_does_not_inherit_feedback_from_previous_attempt(self):
        followup = self.publish()
        first = self.submit(self.save(self.tasks()[0], answer=0))
        retry = self.feedback(first, "needs_retry", "上一轮错误，请再次尝试")
        submitted = self.submit(self.save(retry))

        def csv_row():
            response = self.request("GET", f"/followups/{followup['id']}/report.csv")
            return next(row for row in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
                        if row["student_id"] == "S07")

        row = csv_row()
        self.assertEqual(row["latest_attempt_id"], submitted["attempt_ids"][-1])
        self.assertEqual(float(row["rule_score"]), 1)
        self.assertEqual((row["feedback"], row["feedback_attempt_id"], row["feedback_decision"]), ("", "", ""))
        self.feedback(submitted, comment="本轮已确认完成")
        row = csv_row()
        self.assertEqual(row["feedback_attempt_id"], submitted["attempt_ids"][-1])
        self.assertEqual(row["feedback"], "本轮已确认完成")
        self.assertEqual(row["feedback_decision"], "completed")
        report = self.request("GET", f"/followups/{followup['id']}/report.json").json()
        task = next(task for task in report["followup"]["tasks"] if task["student_id"] == "S07")
        self.assertEqual(task["feedback"][0]["attempt_id"], first["attempt_ids"][0])
        self.assertEqual(len(task["attempts"]), 2)

    def test_two_threads_with_same_task_version_have_one_winner_and_no_lost_update(self):
        self.publish()
        task = self.tasks()[0]
        barrier = Barrier(2)
        bodies = [self.save_body(task, answer=answer) for answer in (0, self.correct_answer)]
        clients = [self.new_client(self.app) for _ in bodies]

        def save_concurrently(index):
            barrier.wait(timeout=5)
            response = clients[index].put(
                API + f"/tasks/{task['id']}/draft", headers=student_headers(), json=bodies[index],
            )
            return index, response

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save_concurrently, index) for index in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(response.status_code for _, response in results), [200, 409])
        winner = next(index for index, response in results if response.status_code == 200)
        saved = self.get_task(task)
        self.assertEqual(saved["version"], task["version"] + 1)
        self.assertEqual(saved["draft_answers"], bodies[winner]["answers"])
        state = self.app.state.store.teaching_snapshot()
        self.assertEqual(len(state["idempotency"]), 4)  # review, followup, publish, winning save
        self.assertEqual(create_app(self.config).state.store.teaching_snapshot(), state)

    def test_two_threads_replaying_identical_submit_create_one_attempt(self):
        self.publish()
        task = self.save(self.tasks()[0])
        body = self.version_body(task)
        barrier = Barrier(2)
        clients = [self.new_client(self.app) for _ in range(2)]

        def submit_concurrently(index):
            barrier.wait(timeout=5)
            return clients[index].post(API + f"/tasks/{task['id']}/submit", headers=student_headers(), json=body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit_concurrently, index) for index in range(2)]
            responses = [future.result(timeout=15) for future in futures]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(responses[0].json(), responses[1].json())
        submitted = self.get_task(task)
        self.assertEqual(submitted["version"], task["version"] + 1)
        self.assertEqual(len(submitted["attempts"]), 1)
        self.assertEqual(len(self.app.state.store.teaching_snapshot()["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
