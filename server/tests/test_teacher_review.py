"""Teacher review, appeal resolution and submission closure through real HTTP routes.

Every repository lives in a temporary directory. Environment-file loading and
external transports are disabled before importing the application.
"""

import atexit
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="teacher-review-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name,
    STUDY_DATA_DIR=_IMPORT_DATA.name, PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false",
    STUDY_DEMO_ENABLED="false", STUDY_WORKER_ENABLED="false",
    LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
    MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
)

from fastapi.testclient import TestClient

with patch("sqlite3.connect", side_effect=AssertionError("import must not open SQLite")):
    from app.config import Settings
    from app.main import create_app
    from app.learning_store import LearningStore, TaskNotFound
    from app.utils.security import hash_password
    from app.dispatcher import Dispatcher
    from app.worker import Worker


class TeacherReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(24)
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="teacher-review-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.network_mocks = []
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
                       "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("network is forbidden in teacher review tests"))
            self.network_mocks.append(guard.start())
            self.addCleanup(guard.stop)
        self.app = create_app(Settings(
            app_env="test", data_dir=str(self.root), persistence_enabled=True, demo_enabled=False,
            worker_enabled=False, cookie_secure=False, allowed_hosts=("testserver",),
            llm_providers=(), llm_base_url="", llm_api_key="", llm_model="",
            maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(), maxkb_course_dataset_ids=(),
        ))
        self.runtime = self.app.state.runtime
        self.gateway = SimpleNamespace(describe=Mock(side_effect=AssertionError("model calls forbidden")),
                                       complete=AsyncMock(side_effect=AssertionError("model calls forbidden")))
        self.runtime.install("gateway", self.gateway)
        self.client = self.enterContext(TestClient(self.app, headers={"Origin": "http://testserver"}))
        self.platform, self.learning = self.runtime.platform, self.runtime.learning
        self.users, self.cookies = {}, {}
        for name, role in (("teacher", "teacher"), ("manager", "teacher"), ("reader", "teacher"),
                           ("materials", "teacher"), ("foreign", "teacher"), ("student", "student"),
                           ("peer", "student"), ("outsider", "student"), ("admin", "admin")):
            self.users[name] = self.platform.create_user(name, name, self.password_hash, role, user_id=name)
        self.course = self.platform.create_course("Review course", "2026", "materials")
        self.classroom = self.platform.create_class(self.course["id"], "Review A", owner_teacher_id="teacher")
        self.other_class = self.platform.create_class(self.course["id"], "Review B", owner_teacher_id="foreign")
        for name in ("student", "peer", "outsider"):
            self.platform.create_course_membership(self.course["id"], name)
            self.platform.create_class_membership(self.other_class["id"] if name == "outsider" else self.classroom["id"], name)
        for name in ("teacher", "manager", "foreign"):
            self.platform.set_course_collaborator(self.course["id"], name, "manage", granted_by="materials")
        self.platform.set_class_collaborator(self.classroom["id"], "reader", "read", granted_by="teacher")
        self.platform.set_class_collaborator(self.classroom["id"], "manager", "manage", granted_by="teacher")
        self.refs = []
        for qid, kind, answer, points in (("choice", "single_choice", 1, 2), ("text", "text", "frozen reference", 3),
                                          ("diagnostic", "judge", True, 1)):
            self.learning.create_question_version("teacher", qid, 1, {
                "type": kind, "stem": "Frozen " + qid, "options": ["A", "B"] if kind == "single_choice" else [],
                "answer": answer, "points": points, "explanation": "PRIVATE-FROZEN-EXPLANATION-" + qid,
                "rubric": [{"point": "PRIVATE-FROZEN-RUBRIC-" + qid, "score_units": points * 100}],
            }, course_id=self.course["id"], status="approved")
            if qid != "diagnostic":
                self.refs.append({"question_id": qid, "version": 1})
        self.answers = {"choice": 1, "text": "Submitted reasoning"}
        self.sequence = 0

    def tearDown(self):
        self.gateway.describe.assert_not_called()
        self.gateway.complete.assert_not_called()
        for guard in self.network_mocks:
            guard.assert_not_called()

    def expect(self, response, status=200):
        self.assertEqual(response.status_code, status, f"{response.request.method} {response.request.url}: {response.text}")
        return response.json()

    def as_user(self, name):
        self.client.cookies.clear()
        if name not in self.cookies:
            self.expect(self.client.post("/api/v1/auth/login", json={
                "username": name, "password": self.password, "role": self.users[name]["role"],
            }))
            self.cookies[name] = self.client.cookies.get("sid")
            self.assertTrue(self.cookies[name])
        else:
            self.client.cookies.set("sid", self.cookies[name])

    def publish(self, *, teacher="teacher", students=None, diagnostic=False, refs=None):
        self.sequence += 1
        other = teacher == "foreign"
        students = students or (["outsider"] if other else ["student"])
        self.as_user(teacher)
        goal = self.expect(self.client.post("/api/v1/goals", json={
            "request_id": f"goal-{self.sequence}", "title": f"Goal {self.sequence}",
            "course_id": self.course["id"], "class_id": self.other_class["id"] if other else self.classroom["id"],
        }), 201)
        publication = self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json={
            "request_id": f"round-{self.sequence}", "expected_version": goal["version"], "round_no": 1,
            "student_ids": students, "question_versions": self.refs if refs is None else refs,
            "deadline_at": time.time() + 3600, "allow_late": False, "allow_retries": True,
            "diagnostic_question_versions": [{"question_id": "diagnostic", "version": 1}] if diagnostic else [],
        }), 201)
        return goal, self.expect(self.client.get(f"/api/v1/tasks/{publication['task_ids'][0]}"))

    def save(self, task):
        self.as_user("student")
        lease = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "browser", "lease_seconds": 300}))
        body = {"request_id": f"save-{lease['task_version']}", "expected_version": lease["task_version"],
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"], "answers": self.answers}
        saved = self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=body))
        return lease, body, saved

    def submit(self, task):
        lease, _, saved = self.save(task)
        body = {"request_id": f"submit-{saved['version']}", "expected_version": saved["version"],
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}
        submitted = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json=body), 202)
        return submitted["attempts"][-1]["id"]

    def grade(self, attempt_id, *, manual=False):
        event = next(row for row in self.learning.list_grading_outbox(status="all") if row["attempt_id"] == attempt_id)
        self.learning.mark_grading_outbox(event["job_id"], fence_token=1)
        self.learning.apply_grading_result(attempt_id, "choice", {"score_units": 200, "why": "PRIVATE-GRADING"},
                                           expected_revision=1, job_id=event["job_id"], fence_token=1)
        if manual:
            self.learning.fail_grading(attempt_id, job_id=event["job_id"], fence_token=1, reason="PRIVATE-MANUAL-REASON")
        else:
            self.learning.apply_grading_result(attempt_id, "text", {"score_units": 250, "why": "PRIVATE-GRADING"},
                                               expected_revision=1, job_id=event["job_id"], fence_token=1)
        return event

    def review(self, attempt_id, teacher="teacher"):
        self.as_user(teacher)
        return self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/teacher-review"))

    def appeal(self, attempt_id, question="choice", request_id="appeal"):
        self.as_user("student")
        return self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/appeals", json={
            "request_id": request_id, "question_id": question, "grading_revision": 1, "reason": "Please check my reasoning",
        }), 202)["id"]

    def close(self, task, *, teacher="teacher", request_id="close", reason="Stop further submissions", version=None, status=200):
        self.as_user(teacher)
        body = {"request_id": request_id, "expected_version": task["version"] if version is None else version, "reason": reason}
        return self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/close", json=body), status)

    def assert_keys_closed(self, task):
        self.assertFalse(task["answer_released"])
        self.assertNotIn("PRIVATE-", json.dumps(task))
        for question in task["questions"]:
            for key in ("answer", "explanation", "rubric", "reference_answer"):
                self.assertNotIn(key, question)
        for attempt in task["attempts"]:
            self.assertNotIn("items", attempt)
            if attempt["status"] != "graded":
                self.assertNotIn("total_score", attempt)

    def test_teacher_review_partial_results_frozen_question_and_no_worker_fields(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        pending = self.review(attempt_id)
        self.assertEqual([item["status"] for item in pending["items"]], ["pending", "pending"])
        self.assertTrue(pending["can_grade"])
        self.assertTrue(all(not item["can_manual_grade"] and not item["can_correct"] for item in pending["items"]))
        self.grade(attempt_id, manual=True)
        self.learning.create_question_version("teacher", "choice", 2, {
            "type": "single_choice", "stem": "NEW UNASSIGNED STEM", "options": ["C", "D"], "answer": 0, "points": 8,
        }, course_id=self.course["id"], status="approved")
        with patch.object(self.learning, "get_grading_input", side_effect=AssertionError("review must not call worker input")):
            review = self.review(attempt_id)
        self.assertEqual(review["attempt"]["status"], "review_required")
        self.assertEqual(review["attempt"]["answers"], self.answers)
        self.assertEqual(review["attempt"]["max_score"], 5)
        self.assertNotIn("total_score", review["attempt"])
        self.assertFalse(review["results_released"])
        choice, text = review["items"]
        self.assertEqual((choice["question_version_id"], choice["question"]["stem"], choice["question"]["points"]), ("choice:1", "Frozen choice", 2))
        self.assertEqual(choice["original_grading"]["score_units"], 200)
        self.assertEqual((choice["can_manual_grade"], choice["can_correct"]), (False, False))
        self.assertEqual((text["status"], text["score_units"], text["score"], text["can_manual_grade"]), ("manual_pending", None, None, True))
        for field in ("fence_token", "job_id", "outbox_status", "lease_token"):
            self.assertNotIn(field, json.dumps(review))
        self.as_user("student")
        self.assert_keys_closed(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}")))

    def test_review_permissions_reader_manager_cross_class_and_revocation(self):
        _, task = self.publish(teacher="manager")
        attempt_id = self.submit(task)
        self.grade(attempt_id, manual=True)
        self.assertFalse(self.review(attempt_id, "reader")["can_grade"])
        self.assertTrue(self.review(attempt_id, "manager")["can_grade"])
        for name in ("foreign", "materials"):
            self.as_user(name)
            self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/teacher-review"), 404)
        for name in ("student", "peer", "outsider", "admin"):
            self.as_user(name)
            self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/teacher-review"), 403)
        self.client.cookies.clear()
        self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/teacher-review"), 401)
        self.platform.remove_class_collaborator(self.classroom["id"], "manager", removed_by="teacher")
        self.as_user("manager")
        self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/teacher-review"), 404)
        self.as_user("teacher")
        self.expect(self.client.get("/api/v1/attempts/missing/teacher-review"), 404)

    def test_manual_only_pending_corrections_revision_replay_and_immutable_history(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id, manual=True)
        body = {"request_id": "manual", "expected_revision": 1, "score_units": 225, "reason": "PRIVATE-TEACHER-REASON"}
        self.as_user("reader")
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json=body), 404)
        self.as_user("manager")
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/choice/manual-grade", json=body), 409)
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/choice/corrections", json=body), 409)
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json={**body, "expected_revision": 2}), 409)
        for units in (True, 1.5, -1, 301):
            self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json={**body, "score_units": units}), 422)
        graded = self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json=body))
        self.assertEqual((graded["grading_revision"], graded["total_score"]), (2, 4.25))
        self.assertEqual(self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json=body)), graded)
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json={**body, "score_units": 226}), 409)
        original = self.review(attempt_id)["attempt"]
        correction = {"request_id": "correction", "expected_revision": 2, "score_units": 150, "reason": "Reviewed rubric"}
        corrected = self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/choice/corrections", json=correction))
        self.assertEqual(corrected["total_score"], 3.75)
        self.assertEqual(self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/choice/corrections", json=correction)), corrected)
        review = self.review(attempt_id)
        self.assertEqual(review["attempt"]["answers"], original["answers"])
        self.assertEqual(review["attempt"]["submitted_at"], original["submitted_at"])
        self.assertEqual(review["items"][0]["original_grading"]["score_units"], 200)
        self.assertTrue(all(item["can_correct"] and not item["can_manual_grade"] for item in review["items"]))
        history = self.expect(self.client.get(f"/api/v1/attempts/{attempt_id}/corrections"))["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["prior_score_units"], history[0]["corrected_score_units"]), (200, 150))
        self.platform.remove_class_collaborator(self.classroom["id"], "manager", removed_by="teacher")
        self.as_user("manager")
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json=body), 404)

    def test_pending_worker_item_cannot_be_manually_overwritten(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        with self.learning._using_connection(write=True) as db:
            db.execute("UPDATE attempts SET status='review_required' WHERE id=?", (attempt_id,))
        review = self.review(attempt_id)
        self.assertFalse(any(item["can_manual_grade"] for item in review["items"]))
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json={
            "request_id": "pending-race", "expected_revision": 1, "score_units": 100, "reason": "Do not race worker",
        }), 409)

    def test_appeal_detail_resolution_replay_revision_and_no_score_mutation(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id)
        appeal_id = self.appeal(attempt_id)
        body = {"request_id": "resolve", "expected_revision": 1, "reason": "PRIVATE-RESOLUTION"}
        self.as_user("teacher")
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body), 409)
        review_evidence = {"score_units": 200, "why": "PRIVATE-APPEAL-REVIEW"}
        self.learning.finish_appeal_review(appeal_id, review_evidence)
        self.as_user("reader")
        detail = self.expect(self.client.get(f"/api/v1/appeals/{appeal_id}"))
        self.assertEqual(detail["reason"], "Please check my reasoning")
        self.assertEqual(detail["result"]["review"], review_evidence)
        self.assertFalse(detail["can_resolve"])
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body), 404)
        for name in ("peer", "outsider", "foreign", "materials"):
            self.as_user(name)
            self.expect(self.client.get(f"/api/v1/appeals/{appeal_id}"), 404)
        before = self.review(attempt_id)
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/choice/corrections", json={
            "request_id": "appeal-correction", "expected_revision": 1, "score_units": 175, "reason": "Separately audited score correction",
        }))
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body), 409)
        body["expected_revision"] = 2
        before_resolve = self.review(attempt_id)
        resolved = self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body))
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["result"]["review"], review_evidence)
        self.assertEqual(resolved["result"]["resolution"]["grading_revision"], 2)
        self.assertEqual(self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body)), resolved)
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json={**body, "reason": "changed"}), 409)
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json={**body, "request_id": "resolve-again"}), 409)
        after = self.review(attempt_id)
        self.assertEqual(after, before_resolve)
        self.assertEqual(after["attempt"]["answers"], before["attempt"]["answers"])
        self.as_user("student")
        listing = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/appeals"))["items"]
        self.assertEqual(listing[0]["result"], resolved["result"])
        self.expect(self.client.post(f"/api/v1/appeals/{appeal_id}/resolve", json=body), 403)
        current = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={"request_id": "retry", "expected_version": current["version"]}))
        redacted = self.expect(self.client.get(f"/api/v1/appeals/{appeal_id}"))
        self.assertEqual(redacted["result"], {})
        self.assertNotIn("PRIVATE-", json.dumps(redacted))
        self.assertEqual(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/appeals"))["items"][0]["result"], {})
        self.as_user("teacher")
        self.assertEqual(self.expect(self.client.get(f"/api/v1/appeals/{appeal_id}"))["result"], resolved["result"])

    def test_multiple_appeals_use_distinct_durable_events_and_replays_deduplicate(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id)
        first = self.appeal(attempt_id)
        second = self.appeal(attempt_id, "text", "appeal-text")
        self.assertEqual(self.appeal(attempt_id), first)
        self.assertNotEqual(first, second)
        events = [event for event in self.learning.pending_notifications() if event["event_type"] == "appeal_received"]
        self.assertEqual(len(events), 2)
        self.assertEqual({json.loads(event["payload_json"])["appeal_id"] for event in events}, {first, second})
        for event in events:
            payload = json.loads(event["payload_json"])
            self.assertEqual((payload["task_id"], payload["attempt_id"]), (task["id"], attempt_id))
            self.assertIn(payload["appeal_id"], event["id"])
        again = LearningStore(self.learning.db_path, access_checker=self.runtime.check_learning_access)
        self.assertEqual([event for event in again.pending_notifications() if event["event_type"] == "appeal_received"], events)

    def test_close_preserves_draft_fences_lease_and_blocks_all_submission_paths(self):
        _, task = self.publish()
        lease, draft_body, saved = self.save(task)
        self.close(saved, teacher="reader", status=404)
        self.close(saved, version=saved["version"] - 1, status=409)
        closed = self.close(saved, teacher="manager")
        self.assertTrue(closed["submission_closed"])
        self.assertIsInstance(closed["submission_closed_at"], float)
        self.assertEqual(closed["status"], saved["status"])
        self.assertEqual(closed["draft_answers"], self.answers)
        self.assertEqual(closed["version"], saved["version"] + 1)
        self.assertEqual(self.close(saved, teacher="manager"), closed)
        self.close(saved, teacher="manager", reason="changed", status=409)
        with self.learning._using_connection() as db:
            row = db.execute("SELECT * FROM learning_tasks WHERE id=?", (task["id"],)).fetchone()
            self.assertIsNone(row["lease_token"])
            self.assertGreater(row["fence_token"], lease["fence_token"])
        self.as_user("student")
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "new"}), 409)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases/renew", json={
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"], "lease_seconds": 300,
        }), 409)
        self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=draft_body), 409)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json={
            "request_id": "closed-submit", "expected_version": closed["version"],
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        }), 409)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={"request_id": "closed-retry", "expected_version": closed["version"]}), 409)
        self.assert_keys_closed(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}")))
        self.assertTrue(self.learning.get_answer_policy("student")["allow_generation"])
        self.assertFalse(self.learning.get_answer_policy("student", task_id=task["id"])["allow_generation"])
        self.assertFalse(self.learning.get_answer_policy("student", task_id=task["id"])["results_released"])
        self.as_user("teacher")
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/deadline-extensions", json={
            "request_id": "closed-extend", "expected_version": closed["version"], "new_deadline": time.time() + 7200, "reason": "too late",
        }), 409)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])
        self.assertEqual(len([event for event in self.learning.pending_notifications() if event["event_type"] == "task_closed"]), 1)
        self.platform.remove_class_collaborator(self.classroom["id"], "manager", removed_by="teacher")
        self.close(saved, teacher="manager", status=404)

    def test_close_rejects_inflight_and_preserves_released_history_and_answer_policy(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        inflight = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.close(inflight, status=409)
        self.grade(attempt_id)
        self.as_user("student")
        ready = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        closed = self.close(ready)
        self.assertEqual(closed["status"], "results_ready")
        self.as_user("student")
        history = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertEqual(history["attempts"], ready["attempts"])
        self.assertEqual(history["questions"], ready["questions"])
        self.assertTrue(history["answer_released"])
        self.assertTrue(self.learning.get_answer_policy("student", task_id=task["id"])["results_released"])
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={"request_id": "closed-retry", "expected_version": closed["version"]}), 409)
        self.assertTrue(self.review(attempt_id)["items"][0]["can_correct"])

    def assert_close_unsubmitted_retry(self, retry_status):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id)
        self.as_user("teacher")
        thread = self.expect(self.client.post("/api/v1/companion/discussion-threads", json={
            "request_id": "retry-thread", "task_id": task["id"], "attempt_id": attempt_id,
            "question_version_id": "text:1", "grading_revision": 1, "title": "Historical feedback",
        }), 201)
        messages_path = f"/api/v1/companion/discussion-threads/{thread['id']}/messages"
        self.expect(self.client.post(messages_path, json={
            "request_id": "retry-comment", "text": "PRIVATE-HISTORICAL-TEACHER-FEEDBACK",
        }), 201)
        self.as_user("student")
        ready = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        messages = self.expect(self.client.get(messages_path))
        self.assertFalse(messages[0]["redacted"])
        retry_body = {"request_id": "start-retry", "expected_version": ready["version"]}
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json=retry_body))
        lease = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "retry-browser"}))
        draft_body = {
            "request_id": "retry-draft", "expected_version": lease["task_version"],
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
            "answers": {"choice": 0, "text": "Unsubmitted retry reasoning"},
        }
        if retry_status == "in_progress":
            self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=draft_body))
        retry = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertEqual(retry["status"], retry_status)
        self.assert_keys_closed(retry)
        self.assertTrue(self.expect(self.client.get(messages_path))[0]["redacted"])
        self.assertFalse(self.expect(self.client.get("/api/v1/companion/answer-policy"))["allow_generation"])

        closed = self.close(retry)
        self.assertEqual(closed["status"], "results_ready")
        self.assertTrue(closed["submission_closed"])
        self.assertEqual(closed["version"], retry["version"] + 1)
        self.assertEqual(self.close(retry), closed)
        self.as_user("student")
        history = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertTrue(history["answer_released"])
        self.assertEqual(history["first_attempt_id"], attempt_id)
        self.assertEqual(history["attempts"], ready["attempts"])
        self.assertEqual(history["questions"], ready["questions"])
        self.assertEqual(history["draft_answers"], retry["draft_answers"])
        self.assertEqual(history["draft_answers"], draft_body["answers"] if retry_status == "in_progress" else {})
        self.assertEqual(self.expect(self.client.get(messages_path)), messages)
        scoped = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"))
        self.assertTrue(scoped["results_released"])
        self.assertTrue(scoped["allow_assistant_content"])
        for params in ({}, {"task_id": task["id"], "question_version_id": "text:1"}):
            policy = self.expect(self.client.get("/api/v1/companion/answer-policy", params=params))
            self.assertTrue(policy["allow_generation"])
            self.assertTrue(policy["allow_assistant_content"])
            self.assertEqual(policy["active_count"], 0)

        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "closed-retry"}), 409)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases/renew", json={
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"], "lease_seconds": 300,
        }), 409)
        for body in (draft_body, {**draft_body, "request_id": "closed-draft", "expected_version": history["version"]}):
            self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=body), 409)
        for body in (retry_body, {"request_id": "retry-after-close", "expected_version": history["version"]}):
            self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json=body), 409)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json={
            "request_id": "closed-submit", "expected_version": history["version"],
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        }), 409)
        self.assertEqual(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}")), history)

    def test_close_not_started_retry_restores_history_discussion_and_companion_policy(self):
        self.assert_close_unsubmitted_retry("not_started")

    def test_close_in_progress_retry_restores_history_discussion_and_companion_policy(self):
        self.assert_close_unsubmitted_retry("in_progress")

    def test_close_unsubmitted_retry_keeps_other_active_task_blocking_global_reveal(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id)
        ready = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        retry = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={
            "request_id": "retry", "expected_version": ready["version"],
        }))
        _, other = self.publish()
        self.close(retry)
        self.as_user("student")
        history = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertTrue(history["answer_released"])
        self.assertEqual(history["attempts"], ready["attempts"])
        self.assert_keys_closed(self.expect(self.client.get(f"/api/v1/tasks/{other['id']}")))
        for params in ({}, {"task_id": task["id"], "question_version_id": "choice:1"}):
            policy = self.expect(self.client.get("/api/v1/companion/answer-policy", params=params))
            self.assertFalse(policy["allow_generation"])
            self.assertFalse(policy["allow_assistant_content"])
            self.assertEqual(policy["active_count"], 1)
        self.close(other)
        self.as_user("student")
        unsubmitted = self.expect(self.client.get(f"/api/v1/tasks/{other['id']}"))
        self.assertEqual(unsubmitted["status"], "not_started")
        self.assertEqual(unsubmitted["attempts"], [])
        self.assertIsNone(unsubmitted["first_attempt_id"])
        self.assert_keys_closed(unsubmitted)
        policy = self.expect(self.client.get("/api/v1/companion/answer-policy"))
        self.assertTrue(policy["allow_generation"])
        self.assertTrue(policy["allow_assistant_content"])
        self.assertEqual(policy["active_count"], 0)

    def test_close_submitted_manual_retry_does_not_release_from_graded_first_attempt(self):
        _, task = self.publish()
        first_id = self.submit(task)
        self.grade(first_id)
        ready = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={
            "request_id": "retry", "expected_version": ready["version"],
        }))
        retry_id = self.submit(task)
        self.grade(retry_id, manual=True)
        pending = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.close(pending)
        self.as_user("student")
        closed = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertTrue(closed["submission_closed"])
        self.assertEqual(closed["status"], "awaiting_manual")
        self.assertEqual(closed["attempts"], pending["attempts"])
        self.assertEqual(closed["attempts"][0]["total_score"], ready["attempts"][0]["total_score"])
        self.assert_keys_closed(closed)
        policy = self.expect(self.client.get("/api/v1/companion/answer-policy"))
        self.assertFalse(policy["allow_generation"])
        self.assertFalse(policy["allow_assistant_content"])
        self.assertEqual(policy["active_count"], 1)

    def test_closed_manual_review_can_finish_without_reopening_submissions(self):
        _, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id, manual=True)
        self.as_user("teacher")
        task = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.close(task)
        self.assertFalse(self.learning.get_answer_policy("student", task_id=task["id"])["results_released"])
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/items/text/manual-grade", json={
            "request_id": "closed-manual", "expected_revision": 1, "score_units": 200, "reason": "Finish existing review",
        }))
        self.as_user("student")
        final = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))
        self.assertTrue(final["submission_closed"])
        self.assertEqual(final["status"], "results_ready")
        self.assertTrue(final["answer_released"])
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/retry", json={"request_id": "retry-closed", "expected_version": final["version"]}), 409)

    def test_close_diagnostic_blocks_start_and_submit_without_deleting_history(self):
        _, task = self.publish(diagnostic=True)
        self.as_user("student")
        body = {"request_id": "diagnostic", "expected_version": task["version"]}
        diagnostic = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/diagnostics", json=body), 201)
        self.close(task)
        self.as_user("student")
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/diagnostics", json=body), 409)
        self.expect(self.client.post(f"/api/v1/diagnostics/{diagnostic['id']}/submit", json={
            "request_id": "diagnostic-submit", "answers": {"diagnostic": True}, "unknown_question_ids": [],
        }), 409)
        self.assertEqual(self.expect(self.client.get(f"/api/v1/diagnostics/{diagnostic['id']}"))["status"], "active")
        self.assert_keys_closed(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}")))

    def test_goal_close_stops_future_rounds_and_keeps_published_task_open(self):
        goal, task = self.publish()
        current = self.expect(self.client.get(f"/api/v1/goals/{goal['id']}"))
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/plan", json={
            "request_id": "goal-close", "expected_version": current["version"], "action": "close", "reason": "No further rounds",
        }))
        self.assertFalse(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"))["submission_closed"])
        self.assertTrue(self.submit(task))

    def test_current_membership_is_required_for_task_appeal_and_replayed_writes(self):
        goal, task = self.publish()
        attempt_id = self.submit(task)
        self.grade(attempt_id)
        appeal_id = self.appeal(attempt_id)
        self.platform.delete_class_membership(self.classroom["id"], "student")
        self.platform.create_class_membership(self.other_class["id"], "student")
        for route in (f"tasks/{task['id']}", f"tasks/{task['id']}/appeals", f"appeals/{appeal_id}",
                      f"tasks/{task['id']}/answer-policy", f"attempts/{attempt_id}/corrections", f"goals/{goal['id']}"):
            self.expect(self.client.get("/api/v1/" + route), 404)
        self.expect(self.client.post(f"/api/v1/attempts/{attempt_id}/appeals", json={
            "request_id": "appeal", "question_id": "choice", "grading_revision": 1, "reason": "Please check my reasoning",
        }), 404)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "revoked"}), 404)
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks"))["items"], [])
        self.assertEqual(self.expect(self.client.get("/api/v1/goals"))["items"], [])
        with self.assertRaises(TaskNotFound):
            self.learning.get_teacher_review(attempt_id, "student")

    def test_goals_tasks_offsets_count_only_authorized_rows_and_are_stable(self):
        allowed_goals, allowed_tasks = [], []
        for _ in range(3):
            goal, task = self.publish()
            allowed_goals.append(goal["id"]); allowed_tasks.append(task["id"])
            self.publish(teacher="foreign")
        with self.learning._using_connection(write=True) as db:
            db.execute("UPDATE learning_goals SET created_at=100")
            db.execute("UPDATE learning_tasks SET created_at=100")
        for actor in ("reader", "student"):
            self.as_user(actor)
            for route, ids in (("goals", allowed_goals), ("tasks", allowed_tasks)):
                all_rows = self.expect(self.client.get(f"/api/v1/{route}", params={"limit": 100}))["items"]
                self.assertEqual([row["id"] for row in all_rows], sorted(ids))
                pages = [self.expect(self.client.get(f"/api/v1/{route}", params={"limit": 1, "offset": n}))["items"] for n in range(4)]
                self.assertEqual([page[0]["id"] for page in pages[:3]], sorted(ids))
                self.assertEqual(pages[3], [])
                for params in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
                    self.expect(self.client.get(f"/api/v1/{route}", params=params), 422)
        for limit, offset in ((0, 0), (101, 0), (1, -1), (True, 0)):
            with self.assertRaises(ValueError):
                self.learning.list_tasks("teacher", limit=limit, offset=offset)

    def test_grade_outbox_atomic_rollback_restart_replay_and_legacy_delivery(self):
        _, task = self.publish(refs=[self.refs[0]])
        self.answers = {"choice": 1}
        attempt_id = self.submit(task)
        event = self.learning.list_grading_outbox()[0]
        self.learning.mark_grading_outbox(event["job_id"], fence_token=1)
        original_notify = self.learning._notify
        def fail_notification(db, user, event_type, *args, **kwargs):
            if event_type == "results_ready":
                raise RuntimeError("outbox write unavailable")
            return original_notify(db, user, event_type, *args, **kwargs)
        with patch.object(self.learning, "_notify", side_effect=fail_notification), self.assertRaises(RuntimeError):
            self.learning.apply_grading_result(attempt_id, "choice", {"score_units": 200}, expected_revision=1,
                                               job_id=event["job_id"], fence_token=1)
        self.assertEqual(self.review(attempt_id)["items"][0]["status"], "pending")
        self.assertFalse(any(item["event_type"] == "results_ready" for item in self.learning.pending_notifications()))
        self.learning.apply_grading_result(attempt_id, "choice", {"score_units": 200}, expected_revision=1,
                                           job_id=event["job_id"], fence_token=1)
        again = LearningStore(self.learning.db_path, access_checker=self.runtime.check_learning_access)
        again.apply_grading_result(attempt_id, "choice", {"score_units": 200}, expected_revision=1,
                                   job_id=event["job_id"], fence_token=1)
        events = [item for item in again.pending_notifications() if item["event_type"] == "results_ready"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], f"grading:{attempt_id}:1")
        self.runtime.companion.create_notification("student", "results_ready", "task", task["id"],
                                                  business_key=events[0]["id"], payload={"state": "results_ready"})
        Dispatcher(self.runtime).deliver()
        self.assertFalse(any(item["event_type"] == "results_ready" for item in self.learning.pending_notifications()))
        notifications = self.runtime.companion.list_notifications("student")
        self.assertEqual(len([row for row in notifications if row["event_type"] == "results_ready"]), 1)

    def test_worker_objective_result_and_manual_fallback_have_durable_outbox(self):
        _, task = self.publish(refs=[self.refs[0]])
        self.answers = {"choice": 1}
        attempt_id = self.submit(task)
        self.assertTrue(Worker(self.runtime, worker_id="review-test-worker").run_once())
        with self.learning._using_connection() as db:
            event = db.execute("SELECT * FROM learning_outbox WHERE id=?", (f"grading:{attempt_id}:1",)).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["event_type"], "results_ready")
        _, manual_task = self.publish()
        self.answers = {"choice": 1, "text": "Submitted reasoning"}
        manual_id = self.submit(manual_task)
        self.grade(manual_id, manual=True)
        events = [row for row in self.learning.pending_notifications() if row["id"] == f"grading:{manual_id}:1"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "grading_manual_required")


if __name__ == "__main__":
    unittest.main()
