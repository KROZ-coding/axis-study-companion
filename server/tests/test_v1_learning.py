"""Real application learning HTTP E2E; only model/network boundaries are mocked."""

import atexit
from copy import deepcopy
import os
from pathlib import Path
import secrets
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Follow the project bootstrap convention before importing config or main.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="v1-learning-import-")
atexit.register(_IMPORT_DATA.cleanup)
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
    from app.main import create_app
    from app.runtime import Runtime
    from app.utils.security import hash_password
    from app.worker import Worker


class V1LearningE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(24)
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="v1-learning-e2e-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.network_mocks = []
        # TestClient uses an in-process ASGI transport. These guards catch any
        # accidental external transport or DNS use without mocking HTTP routes.
        for target in (
            "socket.getaddrinfo", "socket.create_connection",
            "httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request",
        ):
            guard = patch(target, side_effect=AssertionError("network is forbidden in learning E2E"))
            self.network_mocks.append(guard.start())
            self.addCleanup(guard.stop)
        self.settings = Settings(
            app_env="test", data_dir=str(self.root), persistence_enabled=True,
            demo_enabled=False, worker_enabled=False, cookie_secure=False,
            allowed_hosts=("testserver",), llm_providers=(),
            llm_base_url="", llm_api_key="", llm_model="",
            maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(), maxkb_course_dataset_ids=(),
        )
        self.app = create_app(self.settings)
        self.runtime = self.app.state.runtime
        self.assertIsInstance(self.runtime, Runtime)
        self.gateway = SimpleNamespace(
            describe=Mock(side_effect=AssertionError("objective grading must not select a model")),
            complete=AsyncMock(side_effect=AssertionError("objective grading must not call a model")),
        )
        self.runtime.install("gateway", self.gateway)
        self.client = self.enterContext(TestClient(
            self.app, headers={"Origin": "http://testserver"}, raise_server_exceptions=True,
        ))
        self.assertIs(self.app.state.platform_store, self.runtime.platform)
        self.assertFalse(self.runtime.worker_status()["enabled"])
        self.assertEqual(self.runtime.worker_status()["running"], 0)
        self.platform = self.runtime.platform
        self.learning = self.runtime.learning
        self.users = {}
        self.cookies = {}
        roles = {
            "teacher": "teacher", "materials": "teacher", "manager": "teacher", "reader": "teacher",
            "other_teacher": "teacher", "student": "student", "peer": "student", "outsider": "student",
        }
        for name, role in roles.items():
            self.users[name] = self.platform.create_user(
                "e2e_" + name, "E2E " + name, self.password_hash, role,
            )
        self.course = self.platform.create_course("E2E Mathematics", "2026", self.users["materials"]["id"])
        self.other_course = self.platform.create_course("Other Mathematics", "2026", self.users["other_teacher"]["id"])
        self.classroom = self.platform.create_class(
            self.course["id"], "Class A", owner_teacher_id=self.users["teacher"]["id"],
        )
        self.other_class = self.platform.create_class(
            self.course["id"], "Class B", owner_teacher_id=self.users["other_teacher"]["id"],
        )
        for name in ("student", "peer", "outsider"):
            self.platform.create_course_membership(self.course["id"], self.users[name]["id"])
            self.platform.create_class_membership(
                self.other_class["id"] if name == "outsider" else self.classroom["id"], self.users[name]["id"],
            )
        # Material and class rights are explicitly separate grants.
        for name in ("teacher", "manager"):
            self.platform.set_course_collaborator(
                self.course["id"], self.users[name]["id"], "manage", granted_by=self.users["materials"]["id"],
            )
        self.platform.set_class_collaborator(
            self.classroom["id"], self.users["reader"]["id"], "read", granted_by=self.users["teacher"]["id"],
        )
        self.platform.set_class_collaborator(
            self.classroom["id"], self.users["manager"]["id"], "manage", granted_by=self.users["teacher"]["id"],
        )
        self.refs = self.seed_questions("teacher")
        self.answers = {self.refs[0]["question_id"]: 1, self.refs[1]["question_id"]: 0}

    def tearDown(self):
        self.gateway.describe.assert_not_called()
        self.gateway.complete.assert_not_called()
        for mock in self.network_mocks:
            mock.assert_not_called()

    def seed_questions(self, author):
        refs = []
        for suffix, answer, points in (("a", 1, 2), ("b", 0, 3)):
            question_id = f"choice-{author}-{suffix}"
            self.learning.create_question_version(
                self.users[author]["id"], question_id, 1,
                {"type": "single_choice", "stem": f"Frozen E2E choice {suffix}",
                 "options": ["Option A", "Option B", "Option C"], "answer": answer, "points": points,
                 "explanation": "PRIVATE-ANSWER-EXPLANATION-" + suffix,
                 "rubric": [{"point": "PRIVATE-FROZEN-RUBRIC-" + suffix, "score": points}]},
                status="approved", course_id=self.course["id"],
            )
            refs.append({"question_id": question_id, "version": 1})
        return refs

    def expect(self, response, status_code):
        self.assertEqual(
            response.status_code, status_code,
            f"{response.request.method} {response.request.url.path}: expected {status_code}; "
            f"got {response.status_code}: {response.text}",
        )
        return response.json() if response.content else None

    def as_user(self, name):
        self.client.cookies.clear()
        if name not in self.cookies:
            result = self.client.post("/api/v1/auth/login", json={
                "username": self.users[name]["username"], "password": self.password,
                "role": self.users[name]["role"],
            })
            self.expect(result, 200)
            self.assertIn("HttpOnly", result.headers["set-cookie"])
            token = self.client.cookies.get("sid")
            self.assertTrue(token, "login must return an actual session cookie")
            self.assertEqual(self.platform.get_session(token)["user_id"], self.users[name]["id"])
            self.cookies[name] = token
        else:
            self.client.cookies.set("sid", self.cookies[name])
        self.expect(self.client.get("/api/v1/auth/me"), 200)

    def goal_payload(self, request_id="goal-create-1", **changes):
        return {
            "request_id": request_id, "title": "Add and compare quantities", "description": "Two objective checks",
            "course_id": self.course["id"], "class_id": self.classroom["id"], **changes,
        }

    def create_goal(self, author="teacher", **changes):
        self.as_user(author)
        goal = self.expect(self.client.post("/api/v1/goals", json=self.goal_payload(**changes)), 201)
        self.assertTrue(goal["id"])
        self.assertGreaterEqual(goal["version"], 1)
        return goal

    def publish_payload(self, goal, *, refs=None, students=None, request_id="round-publish-1", **changes):
        return {
            "request_id": request_id, "expected_version": goal["version"], "round_no": 1,
            "student_ids": [self.users[name]["id"] for name in (students or ("student", "peer"))],
            "question_versions": deepcopy(self.refs if refs is None else refs),
            "deadline_at": time.time() + 600, "scheduled_at": None,
            "allow_late": False, "allow_retries": True, "allow_retry_after_deadline": False,
            "common_question_ratio": 100, **changes,
        }

    def publish(self, goal, *, author="teacher", **changes):
        self.as_user(author)
        payload = self.publish_payload(goal, **changes)
        result = self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=payload), 201)
        return payload, result

    def task_for(self, goal, student="student"):
        self.as_user(student)
        listing = self.expect(self.client.get("/api/v1/tasks", params={"goal_id": goal["id"]}), 200)
        self.assertEqual(len(listing["items"]), 1, listing)
        task = listing["items"][0]
        self.assertEqual(task["student_id"], self.users[student]["id"])
        return self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)

    def goal_and_task(self, *, author="teacher", refs=None):
        goal = self.create_goal(author)
        self.publish(goal, author=author, refs=refs)
        return goal, self.task_for(goal)

    def lease(self, task, *, device="browser-a", takeover=False):
        return self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={
            "device_id": device, "lease_seconds": 300, "takeover": takeover,
        }), 200)

    def draft_payload(self, lease, *, request_id="draft-save-1", answers=None, **changes):
        return {
            "expected_version": lease["task_version"], "request_id": request_id,
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
            "answers": deepcopy(self.answers if answers is None else answers), **changes,
        }

    def draft_and_submit(self, task, *, answers=None):
        lease = self.lease(task)
        draft_payload = self.draft_payload(lease, answers=answers)
        draft = self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=draft_payload), 200)
        submit_payload = {
            "expected_version": draft["version"], "request_id": "attempt-submit-1",
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        }
        submitted = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json=submit_payload), 202)
        return lease, draft_payload, draft, submit_payload, submitted

    def assert_answers_closed(self, task):
        self.assertFalse(task["answer_released"])
        for question in task["questions"]:
            for field in ("answer", "reference_answer", "explanation", "rubric"):
                self.assertNotIn(field, question)
        self.assertNotIn("PRIVATE-ANSWER-EXPLANATION", str(task))
        self.assertNotIn("PRIVATE-FROZEN-RUBRIC", str(task))
        for attempt in task["attempts"]:
            for field in ("total_score", "total_score_units", "items"):
                self.assertNotIn(field, attempt)

    def test_real_cookie_goal_publish_submit_worker_objective_grade_and_answer_gate(self):
        goal, task = self.goal_and_task()
        self.assertEqual({q["id"] for q in task["questions"]}, set(self.answers))
        self.assert_answers_closed(task)
        policy = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 200)
        self.assertFalse(policy["results_released"])
        self.assertFalse(policy["allow_assistant_content"])
        _, _, draft, _, submitted = self.draft_and_submit(task)
        self.assertEqual(draft["draft_answers"], self.answers)
        self.assertEqual(submitted["status"], "grading")
        self.assertEqual(len(submitted["attempts"]), 1)
        self.assert_answers_closed(submitted)
        self.assert_answers_closed(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200))
        policy = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 200)
        self.assertFalse(policy["results_released"])
        outbox = self.learning.list_grading_outbox(status="pending")
        self.assertEqual(len(outbox), 1)
        job_id = outbox[0]["job_id"]
        self.assertEqual(job_id, submitted["grading_job_id"])
        with self.assertRaises(LookupError):
            self.runtime.queue.get_job(job_id)
        worker = Worker(self.runtime, worker_id="objective-e2e-worker")
        self.assertTrue(worker.run_once())
        job = self.runtime.queue.get_job(job_id)
        self.assertEqual(job.status, "succeeded", job)
        completed = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)
        self.assertEqual(completed["status"], "results_ready")
        self.assertTrue(completed["answer_released"])
        attempt = completed["attempts"][0]
        self.assertEqual(attempt["status"], "graded")
        self.assertEqual(attempt["total_score_units"], 500)
        self.assertEqual(attempt["total_score"], 5)
        self.assertEqual({row["question_id"]: row["score_units"] for row in attempt["items"]}, {
            self.refs[0]["question_id"]: 200, self.refs[1]["question_id"]: 300,
        })
        self.assertEqual({q["id"]: q["answer"] for q in completed["questions"]}, self.answers)
        self.assertTrue(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 200)["results_released"])
        completed_outbox = self.learning.list_grading_outbox(status="all")
        self.assertEqual(len(completed_outbox), 1)
        self.assertEqual(completed_outbox[0]["status"], "completed")
        self.assertEqual(self.runtime.queue.list_calls(job_id), ())
        self.assertEqual([row.job_id for row in self.runtime.queue.list_jobs()], [job_id])
        self.assertFalse(worker.run_once(), "completed outbox must not enqueue a second grading job")
        self.assertEqual(self.task_for(goal)["attempts"], completed["attempts"])
        self.assertEqual(len(completed["attempts"]), 1)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), completed_outbox)
        self.assertEqual(self.runtime.queue.list_jobs(), (job,))
        self.assertEqual(self.runtime.queue.list_calls(job_id), ())

    def test_authenticated_empty_listings_and_anonymous_requests(self):
        self.expect(self.client.get("/api/v1/goals"), 401)
        self.expect(self.client.get("/api/v1/tasks"), 401)
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload()), 401)
        self.as_user("teacher")
        self.assertEqual(self.expect(self.client.get("/api/v1/goals"), 200)["items"], [])
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks"), 200)["items"], [])
        self.as_user("student")
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload()), 403)
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks"), 200)["items"], [])

    def test_non_owner_student_cannot_read_edit_submit_or_select_others_evidence(self):
        goal, task = self.goal_and_task()
        lease = self.lease(task)
        owner_payload = self.draft_payload(lease)
        self.as_user("peer")
        own = self.task_for(goal, "peer")
        self.assertNotEqual(own["id"], task["id"])
        self.assert_answers_closed(own)
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 404)
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 404)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "peer-browser"}), 404)
        self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=owner_payload), 404)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json={
            key: value for key, value in owner_payload.items() if key != "answers"
        }), 404)
        self.expect(self.client.get(f"/api/v1/goals/{goal['id']}/evidence", params={"student_id": self.users["student"]["id"]}), 403)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])
        self.as_user("student")
        self.assertEqual(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)["draft_answers"], {})

    def test_read_teacher_can_read_its_class_but_never_write_or_submit_for_student(self):
        goal, task = self.goal_and_task()
        lease = self.lease(task)
        self.as_user("reader")
        self.expect(self.client.get(f"/api/v1/goals/{goal['id']}"), 200)
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)
        listed = self.expect(self.client.get("/api/v1/goals", params={"class_id": self.classroom["id"]}), 200)
        self.assertIn(goal["id"], {row["id"] for row in listed["items"]})
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload("reader-create")), 404)
        self.expect(self.client.patch(f"/api/v1/goals/{goal['id']}", json={
            "request_id": "reader-update", "expected_version": goal["version"], "title": "Denied change",
        }), 404)
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=self.publish_payload(goal, request_id="reader-publish")), 404)
        for teacher in ("reader", "teacher"):
            with self.subTest(teacher=teacher):
                self.as_user(teacher)
                self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "teacher-browser"}), 403)
                self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=self.draft_payload(lease)), 403)
                self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json={
                    "request_id": "teacher-submit", "expected_version": lease["task_version"],
                    "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
                }), 403)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])

    def test_goal_and_round_idempotency_replays_without_duplicate_tasks_and_conflicts_on_change(self):
        self.as_user("teacher")
        payload = self.goal_payload()
        goal = self.expect(self.client.post("/api/v1/goals", json=payload), 201)
        replay = self.expect(self.client.post("/api/v1/goals", json=payload), 201)
        self.assertEqual(replay, goal)
        self.expect(self.client.post("/api/v1/goals", json={**payload, "title": "Changed request"}), 409)
        listing = self.expect(self.client.get("/api/v1/goals"), 200)["items"]
        self.assertEqual([row["id"] for row in listing], [goal["id"]])
        publish_payload = self.publish_payload(goal)
        published = self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=publish_payload), 201)
        replay = self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=publish_payload), 201)
        self.assertEqual(replay, published)
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json={**publish_payload, "allow_late": True}), 409)
        for student in ("student", "peer"):
            self.task_for(goal, student)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])

    def test_draft_submit_replay_and_conflict_do_not_duplicate_attempt_or_outbox(self):
        _, task = self.goal_and_task()
        lease = self.lease(task)
        payload = self.draft_payload(lease)
        draft = self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=payload), 200)
        replay = self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=payload), 200)
        self.assertEqual(replay["version"], draft["version"])
        self.assertEqual(replay["draft_answers"], draft["draft_answers"])
        self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json={
            **payload, "answers": {**self.answers, self.refs[0]["question_id"]: 0},
        }), 409)
        submit = {"request_id": "submit-idempotent", "expected_version": draft["version"],
                  "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}
        submitted = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json=submit), 202)
        replay = self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json=submit), 202)
        self.assertEqual([row["id"] for row in replay["attempts"]], [row["id"] for row in submitted["attempts"]])
        self.assertEqual(replay["version"], submitted["version"])
        self.assertEqual(len(self.learning.list_grading_outbox(status="all")), 1)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/submit", json={**submit, "fence_token": lease["fence_token"] + 1}), 409)
        self.assertEqual(len(self.learning.list_grading_outbox(status="all")), 1)
        self.assert_answers_closed(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200))

    def test_wrong_course_class_student_and_question_scope_fail_before_publishing(self):
        self.as_user("teacher")
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload("wrong-course", course_id=self.other_course["id"])), 404)
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload("wrong-class", class_id=self.other_class["id"])), 404)
        for actor in ("materials", "other_teacher"):
            self.as_user(actor)
            self.expect(self.client.post("/api/v1/goals", json=self.goal_payload("ungranted-" + actor)), 404)
        goal = self.create_goal()
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=self.publish_payload(
            goal, students=("student", "outsider"), request_id="wrong-student-class",
        )), 422)
        self.learning.create_question_version(
            self.users["teacher"]["id"], "candidate-choice", 1,
            {"type": "single_choice", "stem": "Not approved", "options": ["A", "B"], "answer": 0, "points": 1},
            status="candidate", course_id=self.course["id"],
        )
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=self.publish_payload(
            goal, refs=[{"question_id": "candidate-choice", "version": 1}], request_id="candidate-denied",
        )), 422)
        self.learning.create_question_version(
            self.users["other_teacher"]["id"], "other-course-choice", 1,
            {"type": "single_choice", "stem": "Other course", "options": ["A", "B"], "answer": 0, "points": 1},
            status="approved", course_id=self.other_course["id"],
        )
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=self.publish_payload(
            goal, refs=[{"question_id": "other-course-choice", "version": 1}], request_id="other-course-question",
        )), 404)
        self.as_user("student")
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks", params={"goal_id": goal["id"]}), 200)["items"], [])
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])

    def test_fenced_lease_and_foreign_draft_question_cannot_overwrite_student_work(self):
        _, task = self.goal_and_task()
        first = self.lease(task)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "browser-b"}), 409)
        second = self.lease(task, device="browser-b", takeover=True)
        self.assertGreater(second["fence_token"], first["fence_token"])
        self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=self.draft_payload(
            first, request_id="stale-fence", expected_version=second["task_version"],
        )), 409)
        for request_id, answers in (("foreign-question", {"not-assigned": 0}), ("invalid-choice", {self.refs[0]["question_id"]: True})):
            self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=self.draft_payload(
                second, request_id=request_id, answers=answers,
            )), 422)
        saved = self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=self.draft_payload(second)), 200)
        self.assertEqual(saved["draft_answers"], self.answers)
        self.expect(self.client.put(f"/api/v1/tasks/{task['id']}/draft", json=self.draft_payload(
            second, request_id="stale-version", answers={self.refs[0]["question_id"]: 0},
        )), 409)
        self.assertEqual(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)["draft_answers"], self.answers)

    def test_revoked_author_loses_reads_writes_and_idempotency_replays(self):
        refs = self.seed_questions("manager")
        goal = self.create_goal("manager")
        publish_payload, _ = self.publish(goal, author="manager", refs=refs)
        task = self.task_for(goal)
        self.platform.remove_class_collaborator(
            self.classroom["id"], self.users["manager"]["id"], removed_by=self.users["teacher"]["id"],
        )
        self.as_user("manager")
        self.expect(self.client.get(f"/api/v1/goals/{goal['id']}"), 404)
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 404)
        self.expect(self.client.get(f"/api/v1/goals/{goal['id']}/evidence"), 404)
        self.expect(self.client.post("/api/v1/goals", json=self.goal_payload()), 404)
        self.expect(self.client.post(f"/api/v1/goals/{goal['id']}/rounds", json=publish_payload), 404)
        self.expect(self.client.patch(f"/api/v1/goals/{goal['id']}", json={
            "request_id": "revoked-update", "expected_version": goal["version"], "title": "Revoked change",
        }), 404)
        self.assertEqual(self.expect(self.client.get("/api/v1/goals", params={"class_id": self.classroom["id"]}), 200)["items"], [])
        self.assertEqual(self.expect(self.client.get("/api/v1/tasks", params={"class_id": self.classroom["id"]}), 200)["items"], [])
        self.assert_answers_closed(self.task_for(goal))

    def test_revoked_grading_author_never_opens_student_result_gate(self):
        refs = self.seed_questions("manager")
        goal, task = self.goal_and_task(author="manager", refs=refs)
        answers = {refs[0]["question_id"]: 1, refs[1]["question_id"]: 0}
        _, _, _, _, submitted = self.draft_and_submit(task, answers=answers)
        self.platform.remove_class_collaborator(
            self.classroom["id"], self.users["manager"]["id"], removed_by=self.users["teacher"]["id"],
        )
        self.assertTrue(Worker(self.runtime, worker_id="revocation-e2e-worker").run_once())
        task = self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 200)
        self.assertEqual(self.runtime.queue.get_job(submitted["grading_job_id"]).status, "failed")
        self.assertEqual(task["status"], "awaiting_manual")
        self.assert_answers_closed(task)
        self.assertFalse(self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 200)["results_released"])

    def test_write_requires_same_origin_even_with_real_auth_cookie(self):
        self.as_user("teacher")
        request = self.client.build_request("POST", "/api/v1/goals", json=self.goal_payload("no-origin"))
        request.headers.pop("origin", None)
        response = self.client.send(request)
        self.expect(response, 403)
        self.assertEqual(response.json()["detail"]["code"], "same_origin_required")
        response = self.client.post("/api/v1/goals", json=self.goal_payload("foreign-origin"), headers={"Origin": "https://foreign.example"})
        self.expect(response, 403)
        self.assertEqual(response.json()["detail"]["code"], "same_origin_required")
        response = self.client.post("/api/v1/goals", json=self.goal_payload("wrong-scheme"), headers={"Origin": "https://testserver"})
        self.expect(response, 403)
        self.assertEqual(response.json()["detail"]["code"], "same_origin_required")
        request = self.client.build_request("GET", "/api/v1/auth/me")
        request.headers.pop("origin", None)
        self.expect(self.client.send(request), 200)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])

    def test_disabled_cookie_owner_cannot_access_learning_after_login(self):
        goal, task = self.goal_and_task()
        self.platform.update_user(self.users["student"]["id"], status="disabled")
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}"), 401)
        self.expect(self.client.get(f"/api/v1/tasks/{task['id']}/answer-policy"), 401)
        self.expect(self.client.post(f"/api/v1/tasks/{task['id']}/edit-leases", json={"device_id": "disabled-browser"}), 401)
        self.assertEqual(self.learning.list_grading_outbox(status="all"), [])


if __name__ == "__main__":
    unittest.main()
