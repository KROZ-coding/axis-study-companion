"""Restricted hints: real six temporary stores, fake buffered model, no network."""

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["SKIP_ENV_FILE"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backup import BackupManager
from app.companion_store import APPLICATION_ID, CompanionStore, IdempotencyConflict, InvalidTransition, SAFE_GUIDANCE, StaleGeneration
from app.content_store import ContentStore
from app.job_queue import JobQueue
from app.learning_store import LearningStore, InvalidTransition as LearningTransition
from app.models import User
from app.platform_store import PlatformStore
from app.report_store import ReportStore
from app.routers.v1_companion import router
from app.services.companion_access import answer_policy, answer_policy_provider, response_access, response_events
from app.services.math_grading import grade_objective
from app.services.restricted_hints import ACTIONS, commit_hint, hint_evidence_id, prepare_hint, validate_hint
from app.v1_deps import get_platform_store, get_platform_user


class BufferedGateway:
    def __init__(self):
        self.calls = []
        self.output = None

    async def complete(self, purpose, messages, **options):
        if purpose != "hint" or options != {"json_mode": True}:
            raise AssertionError("hints must use one buffered JSON call without on_delta")
        self.calls.append({"purpose": purpose, "messages": messages, "options": options})
        choice = json.loads(messages[-1]["content"])["allowed_action_ids"][0]
        return SimpleNamespace(text=self.output if self.output is not None else json.dumps({"action_id": choice}))


class RestrictedHintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="restricted-hints-six-stores-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.guards = []
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("restricted hint tests forbid external network"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.platform = PlatformStore(self.root / "platform.sqlite3")
        self.queue = JobQueue(self.root / "jobs.sqlite3")
        self.content = ContentStore(self.root / "content.sqlite3")
        self.reports = ReportStore(self.root / "reports.sqlite3")
        self.learning = LearningStore(self.root / "learning.sqlite3", access_checker=self.learning_access)
        self.companion = CompanionStore(self.root / "companion.sqlite3", answer_policy=self.learning.get_answer_policy)
        self.gateway = BufferedGateway()
        self.runtime = SimpleNamespace(platform=self.platform, queue=self.queue, content=self.content, reports=self.reports,
                                       learning=self.learning, companion=self.companion, gateway=self.gateway)
        self.sequence = 0
        self.teacher = self.platform.create_user("hint-teacher", "Teacher", "fixture-hash", "teacher")
        self.reader = self.platform.create_user("hint-reader", "Read-only teacher", "fixture-hash", "teacher")
        self.student = self.platform.create_user("hint-student", "Student", "fixture-hash", "student")
        self.peer = self.platform.create_user("hint-peer", "Peer", "fixture-hash", "student")
        self.course = self.platform.create_course("Hints", "2026", self.teacher["id"])
        self.classroom = self.platform.create_class(self.course["id"], "Class A", owner_teacher_id=self.teacher["id"])
        self.other_class = self.platform.create_class(self.course["id"], "Class B", owner_teacher_id=self.teacher["id"])
        for person, classroom in ((self.student, self.classroom), (self.peer, self.other_class)):
            self.platform.create_course_membership(self.course["id"], person["id"])
            self.platform.create_class_membership(classroom["id"], person["id"])
        self.platform.set_class_collaborator(self.classroom["id"], self.reader["id"], "read", granted_by=self.teacher["id"])
        self.refs = [self.question("q-choice", "single_choice"), self.question("q-text", "text")]
        self.goal, self.task = self.new_task()
        self.conversation = self.companion.create_conversation(self.student["id"], "Hint study", course_id=self.course["id"], request_id=self.key())
        self.actor = self.student
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.include_router(router)
        self.app.dependency_overrides[get_platform_store] = lambda: self.platform
        self.app.dependency_overrides[get_platform_user] = lambda: User(**{key: self.actor[key] for key in ("id", "username", "display_name", "password_hash", "role", "status")})
        self.client = self.enterContext(TestClient(self.app))

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()

    def key(self):
        self.sequence += 1
        return f"hint-test-{self.sequence}"

    def learning_access(self, actor_id, scope, *, write=False):
        user = self.platform.get_user(actor_id)
        if not user or user["status"] != "active":
            return False
        if user["role"] == "student":
            membership = self.platform.get_class_membership(scope.get("class_id"), actor_id) if scope.get("class_id") else None
            return scope.get("student_id") == actor_id and (not scope.get("class_id") or membership is not None)
        if scope.get("class_id"):
            return self.platform.can_access_class(actor_id, scope["class_id"], teaching=write)
        return self.platform.can_access_course(actor_id, scope["course_id"], teaching=write) if scope.get("course_id") else scope.get("owner_id") == actor_id

    def question(self, qid, kind, *, permission=True, actions=None):
        value = {"id": qid, "type": kind, "stem": "Explain the relationship between the given quantities.",
                 "options": ["first option", "second option"] if kind == "single_choice" else [],
                 "answer": 1 if kind == "single_choice" else "PRIVATE-STANDARD-ANSWER",
                 "points": 2, "explanation": "PRIVATE-EXPLANATION", "reference_answer": "PRIVATE-REFERENCE",
                 "rubric": [{"point": "PRIVATE-RUBRIC", "score": 2}], "external_processing_permission": permission}
        if actions is not None:
            value["hint_action_ids"] = actions
        self.learning.create_question_version(self.teacher["id"], qid, 1, value, status="approved", course_id=self.course["id"])
        return {"question_id": qid, "version": 1}

    def new_task(self, *, refs=None, student=None, classroom=None, diagnostic=None):
        student = student or self.student
        classroom = classroom or self.classroom
        goal = self.learning.create_goal(self.teacher["id"], "Hints goal", course_id=self.course["id"], class_id=classroom["id"], request_id=self.key())
        published = self.learning.publish_round(self.teacher["id"], goal["id"], expected_version=goal["version"], request_id=self.key(),
                                               round_no=1, student_ids=[student["id"]], question_versions=refs or self.refs,
                                               allow_retries=True, diagnostic_question_versions=diagnostic)
        return goal, self.learning.get_task(published["task_ids"][0], student["id"])

    def save(self, answers=None, *, task=None, student=None):
        task, student = task or self.task, student or self.student
        lease = self.learning.acquire_draft_lease(task["id"], student["id"], "fixture-device")
        current = self.learning.get_task(task["id"], student["id"])
        return self.learning.save_draft(task["id"], student["id"], answers if answers is not None else {"q-choice": 0, "q-text": "I tried a relation"},
                                       expected_version=current["version"], request_id=self.key(),
                                       lease_token=lease["lease_token"], fence_token=lease["fence_token"])

    def post(self, *, level=1, question="q-choice:1", task=None, status=202, request_id=None, **extra):
        response = self.client.post(f"/api/v1/companion/conversations/{self.conversation['id']}/messages", json={
            "request_id": request_id or self.key(), "text": "A hint for my saved attempt", "task_id": (task or self.task)["id"],
            "question_version_id": question, "intent": "hint", "hint_level": level, **extra,
        })
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def prepared(self, posted, *, generation=1, call_id="fixture-call"):
        self.companion.begin_response(posted["response_id"], call_id=call_id, generation=generation,
                                      expected_generation=self.companion.get_response(posted["response_id"])["generation"])
        response = self.companion.get_response(posted["response_id"])
        return response, prepare_hint(self.runtime, response)

    def complete(self, posted, *, output=None):
        response, prepared = self.prepared(posted)
        self.gateway.output = output
        raw = asyncio.run(self.gateway.complete("hint", prepared["messages"], json_mode=True)).text if prepared["allowed"] else None
        validated = validate_hint(prepared, raw)
        result = commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)
        return result, response, prepared, validated

    def events(self, response_id):
        with self.learning.connect() as db:
            return [dict(row, payload=json.loads(row["payload_json"])) for row in db.execute("SELECT * FROM task_events WHERE event_type='help'")
                    if json.loads(row["payload_json"]).get("response_id") == response_id]

    def assert_hidden_everywhere(self, posted, *, inaccessible=False):
        provider = answer_policy_provider(self.runtime, self.student["id"])
        raw = self.companion.get_response(posted["response_id"])
        visible = self.companion.get_response(posted["response_id"], self.student["id"], answer_policy=provider)
        self.assertTrue(visible["redacted"])
        self.assertEqual(visible["text"], "")
        messages = self.companion.list_messages(self.conversation["id"], self.student["id"], answer_policy=provider)
        for message in messages:
            if message.get("response_id") == posted["response_id"]:
                self.assertTrue(message["redacted"])
            if (message.get("response") or {}).get("response_id") == posted["response_id"]:
                self.assertTrue(message["response"]["redacted"])
        for event in self.companion.list_response_events(posted["response_id"], self.student["id"], answer_policy=provider):
            if event["event_type"] == "delta":
                self.assertEqual(event["data"]["text"], "")
        follow = self.companion.post_message(self.conversation["id"], self.student["id"], "follow up", request_id=self.key(), task_id=self.task["id"])
        context = self.companion.build_request_context(follow["response_id"], self.student["id"], answer_policy=provider)
        self.assertNotIn(raw["text"], [item["content"] for item in context["messages"]])
        if inaccessible:
            with self.assertRaises((PermissionError, LookupError)):
                response_access(self.runtime, self.student["id"], posted["response_id"])

    def submit_and_grade(self):
        task = self.learning.get_task(self.task["id"], self.student["id"])
        lease = self.learning.acquire_draft_lease(task["id"], self.student["id"], "fixture-device")
        task = self.learning.submit_attempt(task["id"], self.student["id"], expected_version=lease["task_version"], request_id=self.key(),
                                            lease_token=lease["lease_token"], fence_token=lease["fence_token"])
        attempt = task["attempts"][-1]
        data = self.learning.get_grading_input(attempt["id"])
        self.learning.mark_grading_outbox(data["job_id"], fence_token=1)
        for question in data["questions"]:
            self.learning.apply_grading_result(attempt["id"], question["id"], {"status": "graded", "score_units": 0, "why": "fixture grade"},
                                               expected_revision=data["attempt_revision"], job_id=data["job_id"], fence_token=1)
        return self.learning.get_task(task["id"], self.student["id"])

    def test_hint_requires_exact_saved_question_and_keeps_freeform_closed(self):
        self.post(status=409)
        self.save({"q-choice": 0})
        self.post(question="q-text:1", status=409)
        self.post(question="q-choice:2", status=403)
        with self.learning.transaction():
            with self.learning._using_connection(write=True) as db:
                db.execute("INSERT INTO draft_answers VALUES(?,?,?,0)", (self.task["id"], "q-text", json.dumps("   ")))
        self.post(question="q-text:1", status=409)
        self.save()
        policy = answer_policy(self.runtime, self.student["id"], task_id=self.task["id"], question_version_id="q-choice:1")
        self.assertTrue(policy["allow_hint_generation"])
        self.assertTrue(policy["allow_hint_content"])
        self.assertEqual(policy["allowed_hint_levels"], [1, 2])
        self.assertFalse(policy["allow_generation"])
        self.assertFalse(policy["allow_assistant_content"])
        self.assertIsNotNone(policy["hint_binding"]["draft_hash"])
        self.assertEqual(self.gateway.calls, [])

    def test_global_diagnostic_cross_task_class_and_readonly_denials(self):
        self.save()
        _, other = self.new_task()
        self.post(task=other, status=409)
        _, peer_task = self.new_task(student=self.peer, classroom=self.other_class)
        self.post(task=peer_task, status=404)
        diagnosis = self.question("q-diagnostic", "single_choice")
        _, diagnostic_task = self.new_task(diagnostic=[diagnosis])
        self.learning.start_diagnostic(diagnostic_task["id"], self.student["id"], request_id=self.key())
        policy = answer_policy(self.runtime, self.student["id"], task_id=self.task["id"], question_version_id="q-choice:1")
        self.assertTrue(policy["diagnostic"])
        self.assertFalse(policy["allow_hint_content"])
        self.assertFalse(policy["allow_generation"])
        self.post(status=409)
        self.actor = self.reader
        self.post(status=403)
        with self.assertRaises((PermissionError, LookupError)):
            self.learning.get_hint_context(self.reader["id"], task_id=self.task["id"], question_version_id="q-choice:1")
        self.assertEqual(self.gateway.calls, [])

    def test_hint_intent_level_binding_digest_and_outbox_are_immutable(self):
        self.save()
        key = self.key()
        posted = self.post(request_id=key)
        self.assertEqual(self.post(request_id=key)["response_id"], posted["response_id"])
        self.post(request_id=key, level=2, status=409)
        raw = self.companion.get_response(posted["response_id"])
        self.assertEqual(raw["intent"], "hint")
        self.assertEqual(raw["hint_level"], 1)
        self.assertEqual(raw["hint_binding"]["attempt_cycle"], 1)
        outbox = next(item for item in self.companion.pending_outbox() if item["response_id"] == posted["response_id"])
        self.assertEqual(outbox["payload"]["purpose"], "hint")
        self.assertEqual(outbox["payload"]["hint_binding"], raw["hint_binding"])
        self.assertEqual([event["payload"]["status"] for event in self.events(posted["response_id"])], ["requested"])
        self.save({"q-choice": 1, "q-text": "a changed attempt"})
        self.post(request_id=key, status=409)
        self.post(level=True, status=422)
        self.post(intent="chat", level=1, status=422)

    def test_two_levels_use_templates_minimal_payload_and_server_evidence(self):
        self.save()
        outputs = []
        for level in (1, 2):
            posted = self.post(level=level)
            result, response, prepared, validated = self.complete(posted)
            outputs.append(result["text"])
            self.assertEqual(result["content_type"], "restricted_hint")
            self.assertFalse(result["redacted"])
            self.assertEqual(result["text"], ACTIONS[validated["action_id"]][2])
            self.assertEqual(result["hint_metadata"]["hint_level"], level)
            states = [event["payload"]["status"] for event in self.events(posted["response_id"])]
            self.assertEqual(set(states), {"requested", "validated", "validated_delivered"})
            replay = commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)
            self.assertEqual(replay["assistant_message_id"], result["assistant_message_id"])
            self.assertEqual(len(self.events(posted["response_id"])), 3)
            prompt = json.dumps(prepared["messages"], ensure_ascii=False)
            for private in ("PRIVATE-STANDARD-ANSWER", "PRIVATE-REFERENCE", "PRIVATE-EXPLANATION", "PRIVATE-RUBRIC", self.student["id"], self.classroom["id"], self.task["id"]):
                self.assertNotIn(private, prompt)
            payload = json.loads(prepared["messages"][-1]["content"])
            self.assertEqual(payload["saved_attempt"], 0)
            self.assertEqual(set(payload["question"]), {"type", "stem", "options"})
            self.assertEqual(len([event for event in response_events(self.runtime, self.student["id"], posted["response_id"]) if event["event_type"] == "delta"]), 1)
        self.assertNotEqual(*outputs)
        self.assertIn("知识回忆", outputs[0])
        self.assertIn("下一步方向", outputs[1])
        self.assertEqual(len(self.gateway.calls), 2)

    def test_invalid_json_answer_leak_and_forged_completion_never_enter_public_storage(self):
        self.save()
        bad_values = ["the answer is PRIVATE-MODEL-RAW", '{"action_id":"recall_choice","answer":"PRIVATE-MODEL-RAW"}',
                      '{"action_id":"recall_choice","action_id":"recall_choice"}',
                      '{"action_id":"next_choice_reason"}', '```json\n{"action_id":"recall_choice"}\n```', '{"action_id":[]}']
        for value in bad_values:
            posted = self.post()
            result, _, _, _ = self.complete(posted, output=value)
            self.assertEqual(result["content_type"], "safe_guidance")
            self.assertEqual(result["text"], SAFE_GUIDANCE["await_results"])
            self.assertNotIn("validated_delivered", [event["payload"]["status"] for event in self.events(posted["response_id"])])
        posted = self.post()
        response, prepared = self.prepared(posted)
        with self.assertRaises(InvalidTransition):
            self.companion.append_delta(posted["response_id"], 1, "PRIVATE-MODEL-RAW", call_id="fixture-call", generation=1)
        with self.assertRaises(InvalidTransition):
            self.companion.complete_response(posted["response_id"], "PRIVATE-MODEL-RAW", call_id="fixture-call", generation=1)
        validated = validate_hint(prepared, '{"action_id":"recall_choice"}')
        with self.assertRaises(InvalidTransition):
            commit_hint(self.runtime, response, prepared, {**validated, "text": "PRIVATE-MODEL-RAW"}, call_id="fixture-call", generation=1)
        with self.companion.connect() as db:
            serialized = "\n".join(db.iterdump())
        with self.learning.connect() as db:
            serialized += "\n".join(str(tuple(row)) for row in db.execute("SELECT * FROM task_events"))
            serialized += "\n".join(str(tuple(row)) for row in db.execute("SELECT * FROM learning_outbox"))
        self.assertNotIn("PRIVATE-MODEL-RAW", serialized)

    def test_external_permission_and_teacher_action_allowlist(self):
        denied = self.question("q-denied", "text", permission=False)
        _, task = self.new_task(refs=[denied])
        self.save({"q-denied": "my saved attempt"}, task=task)
        posted = self.post(task=task, question="q-denied:1")
        result, _, prepared, _ = self.complete(posted)
        self.assertFalse(prepared["allowed"])
        self.assertEqual(prepared["messages"], [])
        self.assertEqual(result["content_type"], "safe_guidance")
        self.assertEqual(self.gateway.calls, [])
        narrowed = self.question("q-narrow", "text", actions=["recall_expression"])
        _, task = self.new_task(refs=[narrowed])
        self.save({"q-narrow": "my saved attempt"}, task=task)
        self.post(task=task, question="q-narrow:1", level=2, status=409)
        result, _, _, _ = self.complete(self.post(task=task, question="q-narrow:1"))
        self.assertEqual(result["hint_metadata"]["action_id"], "recall_expression")

    def test_dynamic_history_events_context_hide_after_draft_change_and_revocation(self):
        self.save()
        posted = self.post()
        result, _, _, _ = self.complete(posted)
        provider = answer_policy_provider(self.runtime, self.student["id"])
        follow = self.companion.post_message(self.conversation["id"], self.student["id"], "next", request_id=self.key(), task_id=self.task["id"])
        self.assertIn(result["text"], [item["content"] for item in self.companion.build_request_context(follow["response_id"], self.student["id"], answer_policy=provider)["messages"]])
        self.save({"q-choice": 1, "q-text": "changed"})
        self.assert_hidden_everywhere(posted)
        posted = self.post()
        self.complete(posted)
        self.platform.delete_class_membership(self.classroom["id"], self.student["id"])
        self.assert_hidden_everywhere(posted, inaccessible=True)

    def test_retry_closed_task_and_global_diagnostic_hide_completed_hints(self):
        self.save()
        posted = self.post()
        self.complete(posted)
        task = self.submit_and_grade()
        task = self.learning.start_retry(task["id"], self.student["id"], expected_version=task["version"], request_id=self.key())
        self.post(status=409)
        self.save()
        self.assert_hidden_everywhere(posted)
        new_post = self.post()
        self.assertEqual(self.companion.get_response(new_post["response_id"])["hint_binding"]["attempt_cycle"], 2)
        self.complete(new_post)
        diagnostic = self.question("q-new-diagnostic", "single_choice")
        self.new_task(diagnostic=[diagnostic])
        self.assert_hidden_everywhere(new_post)
        task = self.learning.get_task(self.task["id"], self.teacher["id"])
        self.learning.close_task(task["id"], self.teacher["id"], expected_version=task["version"], request_id=self.key(), reason="fixture close")
        self.post(status=409)

    def test_pending_hint_draft_change_falls_back_and_stale_generation_is_fenced(self):
        self.save()
        posted = self.post()
        response, prepared = self.prepared(posted)
        validated = validate_hint(prepared, '{"action_id":"recall_choice"}')
        self.companion.begin_response(posted["response_id"], call_id="new-call", generation=2, expected_generation=1)
        with self.assertRaises(StaleGeneration):
            commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)
        self.save({"q-choice": 1, "q-text": "changed"})
        result = commit_hint(self.runtime, self.companion.get_response(posted["response_id"]), prepared, validated, call_id="new-call", generation=2)
        self.assertEqual(result["content_type"], "safe_guidance")
        self.assertNotIn("validated_delivered", [event["payload"]["status"] for event in self.events(posted["response_id"])])

    def test_checkpoint_crash_before_and_after_companion_completion_recovers_once(self):
        self.save()
        posted = self.post()
        response, prepared = self.prepared(posted)
        raw = asyncio.run(self.gateway.complete("hint", prepared["messages"], json_mode=True)).text
        validated = validate_hint(prepared, raw)
        original = self.learning.record_restricted_hint
        def fail_validated(*args, **kwargs):
            if kwargs["status"] == "validated":
                raise sqlite3.OperationalError("fixture learning disk error")
            return original(*args, **kwargs)
        with patch.object(self.learning, "record_restricted_hint", side_effect=fail_validated):
            with self.assertRaises(sqlite3.OperationalError):
                commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)
        self.assertEqual(self.companion.get_response(posted["response_id"])["text"], "")
        restored = prepare_hint(self.runtime, response)
        self.assertEqual(restored["checkpoint"]["action_id"], "recall_choice")
        def fail_delivered(*args, **kwargs):
            if kwargs["status"] == "validated_delivered":
                raise sqlite3.OperationalError("fixture crash after companion commit")
            return original(*args, **kwargs)
        with patch.object(self.learning, "record_restricted_hint", side_effect=fail_delivered):
            with self.assertRaises(sqlite3.OperationalError):
                commit_hint(self.runtime, response, restored, restored["checkpoint"], call_id="fixture-call", generation=1)
        raw_response = self.companion.get_response(posted["response_id"])
        self.assertEqual(raw_response["status"], "completed")
        self.assert_hidden_everywhere(posted)
        recovered = prepare_hint(self.runtime, raw_response)
        result = commit_hint(self.runtime, raw_response, recovered, recovered["checkpoint"], call_id=raw_response["call_id"], generation=raw_response["generation"])
        self.assertFalse(result["redacted"])
        commit_hint(self.runtime, raw_response, recovered, recovered["checkpoint"], call_id="fixture-call", generation=1)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(len(self.events(posted["response_id"])), 3)
        with self.companion.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE response_id=?", (posted["response_id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE response_id=? AND event_type='end'", (posted["response_id"],)).fetchone()[0], 1)

    def test_cancel_and_revoked_membership_cannot_confirm_hidden_checkpoint(self):
        self.save()
        posted = self.post()
        response, prepared = self.prepared(posted)
        validated = validate_hint(prepared, '{"action_id":"recall_choice"}')
        self.queue.enqueue("interaction", {"purpose": "hint", "resource_type": "response", "resource_id": posted["response_id"]}, job_id=posted["job_id"], steps=("answer",))
        self.queue.cancel(posted["job_id"])
        with self.assertRaises(InvalidTransition):
            commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)
        self.assertEqual(self.companion.get_response(posted["response_id"])["text"], "")
        posted = self.post()
        response, prepared = self.prepared(posted)
        validated = validate_hint(prepared, '{"action_id":"recall_choice"}')
        self.platform.delete_class_membership(self.classroom["id"], self.student["id"])
        with self.assertRaises((LookupError, PermissionError)):
            commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=1)

    def test_cost_pending_keeps_domain_recoverable_and_uses_one_durable_call_after_settlement(self):
        self.save()
        posted = self.post()
        self.queue.enqueue("interaction", {"purpose": "hint", "resource_type": "response", "resource_id": posted["response_id"]},
                           job_id=posted["job_id"], steps=("answer",))
        job = self.queue.claim("hint-fixture", lease_seconds=60)
        step = self.queue.start_step(job.job_id, "answer", "hint-fixture", job.generation)
        response, prepared = self.prepared(posted, generation=job.generation)
        self.queue.start_call(job.job_id, "fixture-call", "hint-fixture", job.generation, step_name=step.name, reserve_units=0)
        raw = asyncio.run(self.gateway.complete("hint", prepared["messages"], json_mode=True)).text
        self.queue.finish_call(job.job_id, "fixture-call", "hint-fixture", job.generation,
                               success=True, result={"text": raw}, cost_units=None, cost_pending=True)
        validated = validate_hint(prepared, raw)
        with self.assertRaises(InvalidTransition):
            commit_hint(self.runtime, response, prepared, validated, call_id="fixture-call", generation=job.generation)
        self.assertEqual(self.companion.get_response(posted["response_id"])["status"], "streaming")
        self.assertEqual(self.companion.get_response(posted["response_id"])["text"], "")
        self.queue.finish(job.job_id, "hint-fixture", job.generation, success=False, error="awaiting_cost")
        self.assertTrue(self.queue.get_job(job.job_id).cost_pending)
        self.queue.settle_call_cost("fixture-call", 0)
        self.queue.retry(job.job_id)
        recovered_job = self.queue.claim("hint-fixture", lease_seconds=60)
        self.assertGreater(recovered_job.generation, job.generation)
        self.queue.start_step(job.job_id, "answer", "hint-fixture", recovered_job.generation)
        recovered, prepared = self.prepared(posted, generation=recovered_job.generation, call_id="recovered-call")
        durable = self.queue.list_calls(job.job_id, step_name="answer")[0].result
        validated = validate_hint(prepared, durable["text"])
        result = commit_hint(self.runtime, recovered, prepared, validated, call_id="recovered-call", generation=recovered_job.generation)
        self.assertFalse(result["redacted"])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(len(self.queue.list_calls(job.job_id)), 1)
        self.assertEqual(len([event for event in self.events(posted["response_id"]) if event["payload"]["status"] == "validated_delivered"]), 1)

    def test_requested_and_fallback_do_not_mark_attempt_assisted_and_client_cannot_forge(self):
        self.save()
        requested = self.learning.record_help(self.task["id"], self.student["id"], "q-choice", payload={"hint_level": 1}, request_id=self.key())
        self.assertEqual(requested["payload"]["status"], "requested")
        legacy_payload = {"help_type": "hint", "source": "companion", "request_semantics": "requested"}
        legacy_key = self.key()
        legacy = self.learning.record_help(self.task["id"], self.student["id"], "q-choice", payload=legacy_payload, request_id=legacy_key)
        self.assertEqual(legacy["payload"]["source"], "client")
        self.assertEqual(legacy["payload"]["status"], "requested")
        self.assertEqual(self.learning.record_help(self.task["id"], self.student["id"], "q-choice", payload=legacy_payload, request_id=legacy_key), legacy)
        for payload in ({"status": "validated_delivered"}, {"source": "restricted_hint"}, {"response_id": "forged"}, {"text": "answer"},
                        {"request_semantics": "delivered"}, {"source": {"claim": "client"}}):
            with self.assertRaises(ValueError):
                self.learning.record_help(self.task["id"], self.student["id"], "q-choice", payload=payload, request_id=self.key())
        self.complete(self.post(), output="invalid")
        task = self.submit_and_grade()
        self.assertTrue(task["attempts"][0]["independent_evidence"])

    def test_delivered_hint_marks_first_attempt_assisted(self):
        self.save()
        self.complete(self.post())
        task = self.submit_and_grade()
        self.assertFalse(task["attempts"][0]["independent_evidence"])

    def test_six_database_backup_preserves_safe_checkpoint_and_metadata(self):
        self.save()
        posted = self.post(level=2)
        result, _, _, _ = self.complete(posted)
        attachments = self.root / "attachments"
        attachments.mkdir()
        manager = BackupManager({name: self.root / f"{name}.sqlite3" for name in ("platform", "learning", "jobs", "content", "reports", "companion")}, attachments)
        bundle = manager.export_bytes()
        manifest = manager.inspect(bundle)
        self.assertEqual(manifest["databases"]["companion"]["user_version"], 2)
        self.assertIn("hint_checkpoints", manifest["databases"]["companion"]["tables"])
        destination = tempfile.TemporaryDirectory(prefix="restricted-hint-restored-")
        self.addCleanup(destination.cleanup)
        restored = manager.restore_to_staging(bundle, destination.name)
        copy = CompanionStore(restored / "databases" / "companion.sqlite3")
        self.assertEqual(copy.get_response(posted["response_id"])["hint_metadata"], result["hint_metadata"])
        self.assertEqual(copy.get_hint_checkpoint(posted["response_id"])["state"], "completed")
        with self.learning.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"teacher_plan_drafts", "teacher_plan_previews", "teacher_next_round_drafts"}.issubset(tables))
        self.assertEqual(self.learning.schema_version, 2)


class RestrictedHintMigrationTests(unittest.TestCase):
    def make_v1(self, path):
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN")
            CompanionStore._migration_1(db)
            db.execute("PRAGMA application_id=0x41584350")
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO conversations(id,user_id,title,request_id,request_hash,created_at,updated_at) VALUES('c','s','old','create',?,1,1)", ("a" * 64,))
            digest = CompanionStore._hash(["old question", None, None])
            db.execute("INSERT INTO messages(id,conversation_id,user_id,role,text,request_id,request_hash,created_at) VALUES('u','c','s','user','old question','request',?,1)", (digest,))
            db.execute("INSERT INTO responses(id,conversation_id,user_id,message_id,job_id,status,generation,call_id,text,last_seq,assistant_message_id,created_at,updated_at,terminal_at) VALUES('r','c','s','u','j','completed',1,'call','OLD-FREEFORM-ANSWER',1,'a',1,2,2)")
            db.execute("INSERT INTO messages(id,conversation_id,user_id,role,text,response_id,created_at) VALUES('a','c','s','assistant','OLD-FREEFORM-ANSWER','r',2)")
            db.execute("INSERT INTO response_outbox VALUES('o','r','j','pending','{}',1,NULL)")
            db.execute("INSERT INTO events(response_id,event_type,seq,generation,data_json,created_at) VALUES('r','delta',1,1,?,2)", (json.dumps({"text": "OLD-FREEFORM-ANSWER"}),))
            db.execute("INSERT INTO events(response_id,event_type,generation,data_json,created_at) VALUES('r','end',1,'{}',2)")
            db.commit()
            return {"indexes": [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")],
                    "rowids": [tuple(row) for row in db.execute("SELECT rowid,id FROM messages ORDER BY rowid")]}

    def test_actual_v1_migration_preserves_cycles_events_indexes_and_freeform_identity(self):
        with tempfile.TemporaryDirectory(prefix="hint-v1-migration-") as directory:
            path = Path(directory) / "companion.sqlite3"
            original = self.make_v1(path)
            store = CompanionStore(path)
            self.assertEqual(store.schema_version, 2)
            self.assertEqual(store.get_response("r")["content_type"], "freeform")
            self.assertEqual(store.get_response("r")["intent"], "chat")
            self.assertEqual(store.post_message("c", "s", "old question", request_id="request")["response_id"], "r")
            with store.connect() as db:
                self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual([tuple(row) for row in db.execute("SELECT rowid,id FROM messages ORDER BY rowid")], original["rowids"])
                current = {tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
                self.assertTrue(set(original["indexes"]).issubset(current))
                self.assertIn("responses", {row[2] for row in db.execute("PRAGMA foreign_key_list(messages)")})
                self.assertIn("messages", {row[2] for row in db.execute("PRAGMA foreign_key_list(responses)")})
                self.assertEqual({row[2] for row in db.execute("PRAGMA foreign_key_list(events)")}, {"responses"})
            policy = {"allow_assistant_content": False, "allow_generation": False, "allow_hint_content": True, "allowed_hint_levels": [1, 2]}
            self.assertEqual(store.get_response("r", "s", answer_policy=policy)["text"], "")
            self.assertEqual(store.get_message("a", "s", answer_policy=policy)["text"], "")
            self.assertEqual(store.list_response_events("r", "s", answer_policy=policy)[0]["data"]["text"], "")
            with self.assertRaises(sqlite3.IntegrityError):
                with store._transaction() as db:
                    db.execute("UPDATE messages SET content_type='restricted_hint' WHERE id='a'")
            reopened = CompanionStore(path)
            self.assertEqual(reopened.get_response("r")["text"], "OLD-FREEFORM-ANSWER")

    def test_failed_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory(prefix="hint-v1-rollback-") as directory:
            path = Path(directory) / "companion.sqlite3"
            self.make_v1(path)
            migrate = CompanionStore._migration_2
            def fail_after_rebuild(db):
                migrate(db)
                raise sqlite3.OperationalError("fixture interrupted migration")
            with patch.object(CompanionStore, "_migration_2", side_effect=fail_after_rebuild):
                with self.assertRaises(sqlite3.OperationalError):
                    CompanionStore(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT text FROM responses WHERE id='r'").fetchone()[0], "OLD-FREEFORM-ANSWER")
                self.assertNotIn("hint_level", [row[1] for row in db.execute("PRAGMA table_info(responses)")])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
