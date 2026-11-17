"""Phase-four execution/recovery integration: real Runtime and six temporary DBs.

Run with run_tests.py's isolated bootstrap and this file's discovery pattern.
Only the gateway and explicit crash boundaries are replaced; domain validation,
queue leases, billing, HTTP authorization, projections and dispatch stay real.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# The runner sets disposable DATA_DIR/STUDY_DATA_DIR before application import.
os.environ["SKIP_ENV_FILE"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.companion_store import InvalidTransition as HintTransition
from app.config import Settings
from app.dispatcher import Dispatcher
from app.routers.v1_companion import router as companion_router
from app.routers.v1_jobs import router as jobs_router
from app.routers.v1_teaching_insights import router as insights_router
from app.runtime import Runtime
from app.services.companion_access import answer_policy_provider, response_access, response_event_access
from app.services.model_gateway import GatewayError, GatewayResult
from app.services.report_exports import build_report_snapshot
from app.services.restricted_hints import ACTIONS, commit_hint, prepare_hint, validate_hint
from app.services.teaching_insights import TeachingInsights
from app.worker import Worker


USAGE = {"prompt_tokens": 7, "completion_tokens": 5}
RAW_MARKER = "PRIVATE-MODEL-RAW-DO-NOT-PUBLISH"
PRICES = json.dumps([
    {"provider": "fixture", "model": model, "version": "fixture-price-v1", "currency": "fixture-unit",
     "input_units_per_million": 1_000_000, "output_units_per_million": 1_000_000}
    for model in ("gpt-5.6-sol", "gpt-5.6-terra")
])


class SimulatedCrash(BaseException):
    """A process interruption must bypass Worker's ordinary failure finalizer."""


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.handler = None
        self.forbid = False

    @staticmethod
    def model(purpose):
        return "gpt-5.6-terra" if purpose == "summary" else "gpt-5.6-sol"

    def describe(self, purpose):
        if self.forbid:
            raise AssertionError("recovery must not require provider configuration")
        return {"provider": SimpleNamespace(name="fixture"), "model": self.model(purpose),
                "reasoning_effort": "high", "max_tokens": 128, "timeout_seconds": 1}

    @staticmethod
    def selection(purpose, messages):
        source = json.loads(messages[-1]["content"])
        if purpose == "hint":
            return {"action_id": source["allowed_action_ids"][0]}
        if purpose != "summary":
            raise AssertionError(f"unexpected model purpose: {purpose}")
        explanations = {"mastered": "maintain_and_check", "developing": "consolidate_goal",
                        "needs_practice": "practice_goal", "insufficient_evidence": "collect_independent_evidence"}
        facts = source["facts"]["students"]
        states = {item["status"] for item in facts}
        summary = ("needs_more_evidence" if states == {"insufficient_evidence"}
                   else "mixed_goal_evidence" if len(states) > 1 else "frozen_goal_evidence")
        return {"schema_version": "teaching.insight.selection.v1", "summary_code": summary,
                "evidence_refs": ["report:scope", "report:rule", "report:students"],
                "limitations": source["limitations"],
                "findings": [{**{key: fact[key] for key in ("student_code", "status", "percent", "independent_families")},
                              "evidence_refs": [f"mastery:{fact['student_code']}"],
                              "explanation_code": explanations[fact["status"]]} for fact in facts]}

    @classmethod
    def result(cls, purpose, messages, *, text=None, usage=USAGE):
        selection = cls.selection(purpose, messages)
        return GatewayResult(text if text is not None else json.dumps(selection), selection, usage,
                             "fixture-request", cls.model(purpose), "high", "fixture", True, usage is None)

    async def complete(self, purpose, messages, **options):
        self.calls.append({"purpose": purpose, "messages": deepcopy(messages), **options})
        if self.forbid:
            raise AssertionError("durable result recovery must not send another model call")
        if self.handler is not None:
            result = self.handler(purpose, messages, **options)
            return await result if inspect.isawaitable(result) else result
        return self.result(purpose, messages)


class TeachingWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="teaching-worker-six-stores-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
                       "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("external network is forbidden"))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        self.settings = Settings(app_env="test", data_dir=str(self.root), worker_enabled=False,
                                 llm_providers=(), llm_base_url="", llm_api_key="", llm_model="",
                                 llm_prices_json=PRICES, budget_currency="fixture-unit", worker_lease_seconds=60)
        self.gateway = FakeGateway()
        self.sequence = 0
        self.worker_errors = []
        self.reopen()
        for actor, role in (("teacher", "teacher"), ("reader", "teacher"), ("successor", "teacher"),
                            ("student", "student"), ("admin", "admin")):
            self.runtime.platform.create_user(actor, actor.title(), "unused-fixture-hash", role, user_id=actor)
        self.course = self.runtime.platform.create_course("Worker course", "2026", "teacher")
        self.classroom = self.runtime.platform.create_class(self.course["id"], "Worker class", owner_teacher_id="teacher")
        self.runtime.platform.create_course_membership(self.course["id"], "student")
        self.runtime.platform.create_class_membership(self.classroom["id"], "student")
        self.runtime.platform.set_class_collaborator(self.classroom["id"], "reader", "read", granted_by="teacher")
        self.runtime.platform.set_course_collaborator(self.course["id"], "reader", "read", granted_by="teacher")
        ledger = self.runtime.queue.budget_ledger
        ledger.assign("teaching", self.classroom["id"], 1_000_000)
        ledger.assign("project", "platform", 1_000_000)
        ledger.assign("student_daily", "student", 1_000_000)
        self.runtime.learning.create_question_version("teacher", "worker-choice", 1, {
            "type": "single_choice", "stem": "Choose the statement matching the stated relationship.",
            "options": ["first option", "second option"], "answer": 1, "points": 1,
            "explanation": "PRIVATE-EXPLANATION", "reference_answer": "PRIVATE-REFERENCE",
            "rubric": [], "external_processing_permission": True,
        }, status="approved", course_id=self.course["id"])
        self.goal = self.runtime.learning.create_goal("teacher", "Worker goal", course_id=self.course["id"],
                                                     class_id=self.classroom["id"], request_id=self.key())
        published = self.runtime.learning.publish_round(
            "teacher", self.goal["id"], expected_version=self.goal["version"], request_id=self.key(), round_no=1,
            student_ids=["student"], question_versions=[{"question_id": "worker-choice", "version": 1}], allow_retries=True,
        )
        self.task_id = published["task_ids"][0]
        self.save_attempt()
        self.conversation = self.runtime.companion.create_conversation(
            "student", "Worker study", course_id=self.course["id"], request_id=self.key())
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.include_router(jobs_router)
        self.app.include_router(companion_router)
        self.app.include_router(insights_router)
        self.client = self.enterContext(TestClient(self.app))
        self.as_user("teacher")

    def key(self):
        self.sequence += 1
        return f"teaching-worker-{self.sequence}"

    def reopen(self, *, forbid_model=False):
        self.gateway.forbid = forbid_model
        settings = replace(self.settings, llm_prices_json="") if forbid_model else self.settings
        self.runtime = Runtime(settings)
        self.runtime.initialize()
        self.runtime.install("gateway", self.gateway)
        self.worker = Worker(self.runtime, worker_id=self.key())
        self.dispatcher = Dispatcher(self.runtime)
        if hasattr(self, "app"):
            self.app.state.runtime = self.runtime

    def as_user(self, actor):
        token = self.key()
        self.runtime.platform.create_session(token, actor, time.time() + 600)
        self.client.cookies.clear()
        self.client.cookies.set("sid", token)

    def save_attempt(self, answer=0):
        lease = self.runtime.learning.acquire_draft_lease(self.task_id, "student", "fixture-device")
        return self.runtime.learning.save_draft(
            self.task_id, "student", {"worker-choice": answer}, expected_version=lease["task_version"],
            request_id=self.key(), lease_token=lease["lease_token"], fence_token=lease["fence_token"])

    def summary(self, *, dispatch=True):
        scope = {"kind": "goal", "goal_id": self.goal["id"], "class_id": self.classroom["id"], "course_id": self.course["id"]}
        canonical = self.runtime.learning.report_input(scope=scope, actor_id="teacher")
        snapshot = self.runtime.reports.create_snapshot(
            scope, canonical["source_revision"], build_report_snapshot(canonical, scope), created_by="teacher")
        revision = self.runtime.reports.create_revision(snapshot["id"])
        insight = TeachingInsights(self.runtime).create(
            "teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id=self.key())
        if dispatch:
            self.assertEqual(self.dispatcher._insights(), 1)
        return insight

    def hint(self, *, level=1):
        self.as_user("student")
        posted = self.client.post(f"/api/v1/companion/conversations/{self.conversation['id']}/messages", json={
            "text": "Help with my saved attempt", "request_id": self.key(), "intent": "hint", "hint_level": level,
            "task_id": self.task_id, "question_version_id": "worker-choice:1",
        })
        self.assertEqual(posted.status_code, 202, posted.text)
        self.assertEqual(self.dispatcher._companion(), 1)
        return posted.json()

    def run_worker(self):
        # run_once intentionally records only an exception class. Preserve its
        # original traceback in assertion diagnostics without changing handling.
        original = self.worker._fail
        def observe_failure(job, reason):
            self.worker_errors.append(traceback.format_exc())
            return original(job, reason)
        with patch.object(self.worker, "_fail", side_effect=observe_failure):
            self.assertTrue(self.worker.run_once(dispatch=False))

    def assert_job(self, job_id, status):
        job = self.runtime.queue.get_job(job_id)
        self.assertEqual(job.status, status, "\n".join(self.worker_errors) or repr(job))
        return job

    def assert_one_charge(self, job_id, *, pending=False):
        calls = self.runtime.queue.list_calls(job_id)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.cost_pending, pending)
        self.assertEqual(call.cost_units, None if pending else 12)
        self.assertEqual(self.runtime.queue.get_job(job_id).budget_spent, 0 if pending else 12)
        self.assertEqual(self.runtime.queue.call_costs(job_id), {} if pending else {call.call_id: 12})
        return call

    def recover_expired(self, job_id):
        job = self.runtime.queue.get_job(job_id)
        self.assertEqual(job.status, "running")
        self.assertIn(job_id, self.runtime.queue.recover_expired(now=job.lease_until + 1))
        self.assert_job(job_id, "awaiting_confirmation")

    def resume(self, job_id, actor, *, status=200):
        self.as_user(actor)
        result = self.client.post(f"/api/v1/jobs/{job_id}/resume", json={"confirm_paid": True})
        self.assertEqual(result.status_code, status, result.text)
        return result

    def hint_states(self, response_id):
        with self.runtime.learning.connect() as db:
            payloads = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM task_events WHERE event_type='help'")]
        return [row["status"] for row in payloads if row.get("response_id") == response_id]

    def assert_hint_delivered(self, posted):
        self.assert_job(posted["job_id"], "succeeded")
        response = response_access(self.runtime, "student", posted["response_id"])
        self.assertEqual((response["status"], response["content_type"], response["redacted"]),
                         ("completed", "restricted_hint", False))
        self.assertEqual(response["text"], ACTIONS[response["hint_metadata"]["action_id"]][2])
        self.assertEqual(self.hint_states(posted["response_id"]).count("validated_delivered"), 1)
        with self.runtime.companion.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE response_id=? AND role='assistant'",
                                        (posted["response_id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE response_id=? AND event_type='end'",
                                        (posted["response_id"],)).fetchone()[0], 1)
        return response

    def assert_hint_reads(self, posted, raw, *, hidden):
        """Exercise both policy providers and every existing hint reference read."""
        response_id = posted["response_id"]
        provider = answer_policy_provider(self.runtime, "student")

        def assert_projection(row):
            self.assertEqual(row["redacted"], hidden)
            self.assertEqual(row["text"], "" if hidden else raw["text"])
            if "content" in row:
                self.assertEqual(row["content"], "" if hidden else raw["text"])

        def read(path, *, headers=None):
            result = self.client.get(path, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            return result

        assert_projection(read(f"/api/v1/companion/responses/{response_id}").json())
        history = read(f"/api/v1/companion/conversations/{posted['conversation_id']}/messages").json()
        linked = next(row["response"] for row in history if row.get("response", {}).get("response_id") == response_id)
        assistant = next(row for row in history if row["id"] == raw["assistant_message_id"])
        self.assertEqual(linked["status"], "completed")
        assert_projection(linked)
        assert_projection(assistant)
        assert_projection(self.runtime.companion.get_message(raw["assistant_message_id"], "student", answer_policy=provider))

        events = read(f"/api/v1/companion/responses/{response_id}/events").json()
        delta, = [event for event in events if event["event_type"] == "delta"]
        self.assertEqual(delta["data"]["text"], "" if hidden else raw["text"])
        current, replay = response_event_access(self.runtime, "student", response_id, delta["event_id"])
        assert_projection(current)
        self.assertEqual(replay["data"]["text"], "" if hidden else raw["text"])
        # Reconnection at the tail must still replace the old visible prefix.
        stream = read(f"/api/v1/companion/responses/{response_id}/stream?once=true",
                      headers={"Last-Event-ID": str(raw["last_event_id"])})
        frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["last_event_id"], raw["last_event_id"])
        assert_projection(frames[0])

        item = read(f"/api/v1/companion/collections/{posted['collection_id']}/items").json()[0]
        self.assertEqual(item["redacted"], hidden)
        assert_projection(item["message"])
        memory = read("/api/v1/companion/memories").json()[0]
        self.assertEqual(memory["redacted"], hidden)
        assert_projection(memory["message"])
        context = self.runtime.companion.build_request_context(posted["follow_response_id"], "student", answer_policy=provider)
        self.assertEqual(raw["text"] in [item["content"] for item in context["messages"]], not hidden)
        metadata = read(f"/api/v1/jobs/{posted['job_id']}/events?once=true")
        self.assertNotIn(raw["text"], metadata.text)

    def hint_read_references(self, posted):
        raw = self.runtime.companion.get_response(posted["response_id"])
        self.assertEqual((raw["status"], raw["content_type"]), ("completed", "restricted_hint"))
        self.assertEqual(self.hint_states(posted["response_id"]), ["requested", "validated", "validated_delivered"])
        collection = self.runtime.companion.create_collection("student", "Hint references", request_id=self.key())
        self.runtime.companion.add_collection_item(collection["id"], "student", "message", request_id=self.key(),
                                                  message_id=raw["assistant_message_id"])
        self.runtime.companion.create_memory("student", request_id=self.key(), message_id=raw["assistant_message_id"])
        follow = self.runtime.companion.post_message(posted["conversation_id"], "student", "next saved attempt",
                                                     task_id=self.task_id, request_id=self.key())
        posted.update(collection_id=collection["id"], follow_response_id=follow["response_id"])
        self.assert_hint_reads(posted, raw, hidden=False)
        return raw

    def assert_cancelled_hint_keeps_facts(self, posted, raw, *, status):
        job = self.assert_job(posted["job_id"], status)
        self.assertTrue(job.cancel_requested)
        self.assert_hint_reads(posted, raw, hidden=True)
        self.assertEqual(self.runtime.companion.get_response(posted["response_id"]), raw)
        states = self.hint_states(posted["response_id"])
        checkpoint = self.runtime.companion.get_hint_checkpoint(posted["response_id"])
        prepared = prepare_hint(self.runtime, raw)
        with self.assertRaises(HintTransition):
            commit_hint(self.runtime, raw, prepared, checkpoint["validated"],
                        call_id=raw["call_id"], generation=raw["generation"])
        self.assertEqual(self.hint_states(posted["response_id"]), states)
        self.assertEqual(states, ["requested", "validated", "validated_delivered"])
        rejected = self.resume(posted["job_id"], "student", status=409)
        self.assertEqual(rejected.json()["detail"]["code"], "cancelled_job")
        self.gateway.forbid = True
        self.assertFalse(self.worker.run_once(dispatch=False))
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_summary_success_uses_dispatch_scope_and_charges_teaching_once(self):
        insight = self.summary()
        self.run_worker()
        job = self.assert_job(insight["job_id"], "succeeded")
        row = self.runtime.insights.get(insight["id"])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result"]["summary_code"], "needs_more_evidence")
        self.assertEqual((job.purpose, job.class_id, job.course_id, job.student_id),
                         ("summary", self.classroom["id"], self.course["id"], None))
        self.assert_one_charge(job.job_id)
        self.assertEqual(self.runtime.queue.budget_ledger.query("teaching", self.classroom["id"]).spent_units, 12)
        self.assertEqual(self.runtime.queue.budget_ledger.query("project", "platform").spent_units, 12)
        call, = self.gateway.calls
        self.assertEqual((call["purpose"], call["json_mode"], call["on_delta"]), ("summary", True, None))
        self.assertEqual({path.name for path in self.root.glob("*.sqlite3")},
                         {name + ".sqlite3" for name in ("platform", "jobs", "learning", "content", "reports", "companion")})

    def test_summary_invalid_raw_output_fails_queue_and_keeps_real_charge(self):
        insight = self.summary()
        self.gateway.handler = lambda purpose, messages, **kw: FakeGateway.result(purpose, messages, text=RAW_MARKER)
        self.run_worker()
        self.assert_job(insight["job_id"], "failed")
        row = self.runtime.insights.get(insight["id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"]["code"], "invalid_model_output")
        self.assertIsNone(row["result"])
        self.assertNotIn(RAW_MARKER, json.dumps(row["error"]))
        self.assertEqual(self.assert_one_charge(insight["job_id"]).status, "succeeded")
        self.assertEqual(self.resume(insight["job_id"], "teacher", status=409).json()["detail"]["code"], "new_insight_required")

    def test_summary_domain_commit_before_queue_ack_recovers_without_model(self):
        insight = self.summary()
        with patch.object(self.runtime.queue, "finish_step", side_effect=SimulatedCrash("domain committed before queue ack")):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        completed = self.runtime.insights.get(insight["id"])
        self.assertEqual(completed["status"], "completed")
        self.reopen(forbid_model=True)
        self.recover_expired(insight["job_id"])
        self.resume(insight["job_id"], "teacher")
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assertEqual(self.runtime.insights.get(insight["id"]), completed)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(insight["job_id"])

    def test_summary_durable_call_before_domain_commit_replays_without_model(self):
        insight = self.summary()
        with patch.object(TeachingInsights, "apply", side_effect=SimulatedCrash("call committed before insight")):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        self.assertIsNone(self.runtime.insights.get(insight["id"])["result"])
        self.assertEqual(self.assert_one_charge(insight["job_id"]).status, "succeeded")
        self.reopen(forbid_model=True)
        self.recover_expired(insight["job_id"])
        self.resume(insight["job_id"], "teacher")
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assertEqual(self.runtime.insights.get(insight["id"])["status"], "completed")
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(insight["job_id"])

    def test_summary_unknown_requires_settlement_and_confirmation_before_replay(self):
        insight = self.summary()
        def unknown(*args, **kwargs):
            raise GatewayError("disconnected", request_sent=True, unknown_outcome=True)
        self.gateway.handler = unknown
        self.run_worker()
        self.assert_job(insight["job_id"], "reconciling")
        call = self.assert_one_charge(insight["job_id"], pending=True)
        self.assertIsNone(self.runtime.insights.get(insight["id"])["result"])
        self.assertFalse(self.worker.run_once(dispatch=False))
        saved = asdict(FakeGateway.result("summary", self.gateway.calls[0]["messages"]))
        self.runtime.queue.reconcile_call(insight["job_id"], call.call_id, "succeeded", result=saved)
        self.assert_job(insight["job_id"], "awaiting_confirmation")
        self.resume(insight["job_id"], "teacher", status=409)
        self.assertTrue(self.runtime.queue.settle_call_cost(call.call_id, 12, usage=USAGE))
        self.assertFalse(self.runtime.queue.settle_call_cost(call.call_id, 12, usage=USAGE))
        self.as_user("teacher")
        unconfirmed = self.client.post(f"/api/v1/jobs/{insight['job_id']}/resume", json={"confirm_paid": False})
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
        self.reopen(forbid_model=True)
        self.resume(insight["job_id"], "teacher")
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assertEqual(self.runtime.insights.get(insight["id"])["status"], "completed")
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(insight["job_id"])

    def test_summary_resume_queue_commit_before_domain_resume_repairs_on_worker(self):
        insight = self.summary()
        def unsent(*args, **kwargs):
            raise GatewayError("not_sent", request_sent=False)
        self.gateway.handler = unsent
        self.run_worker()
        self.assert_job(insight["job_id"], "failed")
        self.assertEqual(self.runtime.insights.get(insight["id"])["status"], "failed")
        # A normal exception at this HTTP boundary leaves the same committed
        # queue/domain gap without terminating TestClient's lifespan portal.
        with patch.object(self.runtime.insights, "resume", side_effect=RuntimeError("queue resume committed first")):
            with self.assertRaisesRegex(RuntimeError, "queue resume committed first"):
                self.resume(insight["job_id"], "teacher")
        self.assert_job(insight["job_id"], "queued")
        self.reopen()
        self.gateway.handler = None
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assertEqual(self.runtime.insights.get(insight["id"])["status"], "completed")
        self.assertEqual([call.cost_units for call in self.runtime.queue.list_calls(insight["job_id"])], [0, 12])

    def test_acceptance_waits_through_held_unsent_cancel_window(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        import verify_teaching_loop as acceptance

        insight = self.summary()
        output = self.root / "acceptance"
        output.mkdir()
        verification = acceptance.Verification(output, SimpleNamespace(timeout=10))
        verification.data_dir = self.root
        verification.clients["teacher"] = self.client
        verification.control("summary", "hold")
        self.gateway = acceptance.fixture.FakeGateway(output, forbidden_values=())
        self.runtime.install("gateway", self.gateway)
        self.runtime.settings = replace(self.settings, worker_lease_seconds=3, llm_prices_json=json.dumps([{
            "provider": acceptance.fixture.PROVIDER, "model": acceptance.fixture.MODELS["summary"],
            "version": acceptance.fixture.PRICE_VERSION, "currency": "fixture-unit",
            "input_units_per_million": 1_000_000, "output_units_per_million": 2_000_000,
        }]))
        private = "DO-NOT-SAVE-EVENT-PAYLOAD"
        self.runtime.queue.append_event(insight["job_id"], "fixture_metadata", {"text": private})
        window, observed, reconciled = Event(), Event(), Event()
        original_reconcile, original_api = self.runtime.queue.reconcile_call, verification.api

        def wait_for_observer(*args, **kwargs):
            # _park_call has already committed unknown/reconciling. Hold its
            # second transaction until the HTTP reader has obtained that state.
            window.set()
            if not observed.wait(10):
                raise AssertionError("acceptance reader did not observe the committed window")
            try:
                return original_reconcile(*args, **kwargs)
            finally:
                reconciled.set()

        def observe_api(actor, method, path, **kwargs):
            row = original_api(actor, method, path, **kwargs)
            if method == "GET" and path == "/api/v1/jobs/" + insight["job_id"] and row["status"] == "reconciling":
                self.assertTrue(window.wait(10))
                observed.set()
                self.assertTrue(reconciled.wait(10))
            return row

        with patch.object(self.runtime.queue, "reconcile_call", side_effect=wait_for_observer), \
                patch.object(verification, "api", side_effect=observe_api), ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(self.worker.run_once, dispatch=False)
            try:
                verification.cancel_held_insight(insight, calls_before=0)
            finally:
                observed.set()
                self.assertTrue(worker.result(timeout=15))
        evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
        audit = evidence["hold_cancel"]
        statuses = [row["status"] for row in audit["observations"] if row["source"] == "poll"]
        self.assertIn("reconciling", statuses)
        self.assertEqual(statuses[-1], "cancelled")
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["calls"][0]["status"], "not_executed")
        self.assertEqual(audit["calls"][0]["cost_units"], 0)
        self.assertFalse(audit["calls"][0]["cost_pending"])
        self.assertEqual(audit["domain"], {"status": "cancelled", "result_is_null": True})
        self.assertEqual(audit["resume"], {"http_status": 409, "error_code": "cancelled_job"})
        self.assertEqual(audit["gateway_calls_after"], 1)
        self.assertNotIn(private, json.dumps(evidence))
        self.assertNotIn("selection", audit["gateway"])
        self.assertTrue(all(set(row) == {"event_id", "event_type", "created_at"} for row in audit["queue_events"]))

    def test_summary_cancel_after_send_retains_usage_without_adopting_result(self):
        insight = self.summary()
        def cancel(purpose, messages, **kwargs):
            self.runtime.queue.cancel(insight["job_id"])
            return FakeGateway.result(purpose, messages)
        self.gateway.handler = cancel
        self.run_worker()
        job = self.assert_job(insight["job_id"], "reconciling")
        self.assertTrue(job.cancel_requested)
        call = self.assert_one_charge(job.job_id)
        self.assertEqual(call.status, "unknown")
        self.assertIsNone(call.result)
        row = self.runtime.insights.get(insight["id"])
        self.assertEqual((row["status"], row["result"]), ("cancelled", None))
        self.runtime.queue.reconcile_call(job.job_id, call.call_id, "failed", usage=USAGE, cost_units=12, cost_pending=False)
        self.assert_job(job.job_id, "cancelled")
        self.resume(job.job_id, "teacher", status=409)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_summary_revoked_class_before_send_blocks_model_and_charge(self):
        insight = self.summary()
        self.runtime.platform.transfer_class_owner(self.classroom["id"], "successor", transferred_by="teacher")
        self.run_worker()
        self.assert_job(insight["job_id"], "failed")
        self.assertEqual(self.runtime.queue.list_calls(insight["job_id"]), ())
        self.assertEqual(self.gateway.calls, [])
        self.assertIsNone(self.runtime.insights.get(insight["id"])["result"])

    def test_summary_revoked_class_after_call_rejects_paid_result(self):
        insight = self.summary()
        def revoke(purpose, messages, **kwargs):
            self.runtime.platform.transfer_class_owner(self.classroom["id"], "successor", transferred_by="teacher")
            return FakeGateway.result(purpose, messages)
        self.gateway.handler = revoke
        self.run_worker()
        self.assert_job(insight["job_id"], "failed")
        self.assertIsNone(self.runtime.insights.get(insight["id"])["result"])
        self.assertEqual(self.assert_one_charge(insight["job_id"]).status, "succeeded")
        self.assertEqual(len(self.gateway.calls), 1)

    def test_readonly_teacher_can_get_summary_job_but_cannot_cancel(self):
        insight = self.summary()
        self.as_user("reader")
        visible = self.client.get(f"/api/v1/jobs/{insight['job_id']}")
        self.assertEqual(visible.status_code, 200, visible.text)
        denied = self.client.post(f"/api/v1/jobs/{insight['job_id']}/cancel")
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assert_job(insight["job_id"], "queued")
        self.assertEqual(self.runtime.insights.get(insight["id"])["status"], "pending")

    def test_admin_can_reconcile_summary_metadata_without_insight_body(self):
        insight = self.summary()
        def unknown(*args, **kwargs):
            raise GatewayError("disconnected", request_sent=True, unknown_outcome=True)
        self.gateway.handler = unknown
        self.run_worker()
        call, = self.runtime.queue.list_calls(insight["job_id"])
        self.as_user("admin")
        reconciled = self.client.post(f"/api/v1/admin/jobs/{insight['job_id']}/calls/{call.call_id}/reconcile", json={
            "outcome": "succeeded", "result": asdict(FakeGateway.result("summary", self.gateway.calls[0]["messages"])),
            "usage": USAGE, "cost_units": 12, "reason": "verified fixture provider result",
        })
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        metadata = self.client.get(f"/api/v1/jobs/{insight['job_id']}/calls")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertFalse({"result", "text", "messages", "input"} & set(metadata.json()["items"][0]))
        self.assertNotIn("result", reconciled.json())
        self.assertEqual(self.client.get(f"/api/v1/teaching-insights/{insight['id']}").status_code, 403)
        self.reopen(forbid_model=True)
        self.resume(insight["job_id"], "teacher")
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assert_one_charge(insight["job_id"])

    def test_dispatcher_queue_insert_before_outbox_ack_replays_one_job(self):
        insight = self.summary(dispatch=False)
        with patch.object(self.runtime.insights, "mark_dispatched", side_effect=SimulatedCrash("queued before outbox ack")):
            with self.assertRaises(SimulatedCrash):
                self.dispatcher._insights()
        self.assert_job(insight["job_id"], "queued")
        self.reopen()
        self.assertEqual(self.dispatcher._insights(), 1)
        self.assertEqual(self.dispatcher._insights(), 0)
        self.assertEqual([job.job_id for job in self.runtime.queue.list_jobs()], [insight["job_id"]])
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        self.assert_one_charge(insight["job_id"])

    def test_dispatcher_notification_commit_before_ack_replays_without_duplicates(self):
        insight = self.summary()
        self.run_worker()
        self.assert_job(insight["job_id"], "succeeded")
        with patch.object(self.runtime.insights, "acknowledge_notification", side_effect=SimulatedCrash("notification committed before ack")):
            with self.assertRaises(SimulatedCrash):
                self.dispatcher.deliver()
        self.reopen()
        self.dispatcher.deliver()
        self.dispatcher.deliver()
        notifications = [row for row in self.runtime.companion.list_notifications("teacher")
                         if row["resource_id"] == insight["id"]]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["event_type"], "teaching_insight_ready")
        self.assertEqual(self.runtime.insights.pending_notifications(), [])
        self.assertEqual(len(self.gateway.calls), 1)

    def test_hint_success_is_buffered_json_and_sse_contains_only_server_template(self):
        posted = self.hint()
        self.run_worker()
        response = self.assert_hint_delivered(posted)
        call, = self.gateway.calls
        self.assertEqual((call["purpose"], call["json_mode"], call["on_delta"]), ("hint", True, None))
        outbound = json.loads(call["messages"][-1]["content"])
        self.assertEqual(outbound["saved_attempt"], 0)
        self.assertNotIn("answer", outbound["question"])
        self.assertNotIn("PRIVATE-", json.dumps(call["messages"]))
        stream = self.client.get(f"/api/v1/companion/responses/{posted['response_id']}/stream?once=true")
        self.assertEqual(stream.status_code, 200, stream.text)
        frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(frames[-1]["text"], response["text"])
        self.assertEqual(frames[-1]["hint_metadata"], response["hint_metadata"])
        self.assertEqual(frames[-1]["intent"], "hint")
        self.assert_one_charge(posted["job_id"])

    def test_hint_stream_waits_for_delivery_projection_after_companion_completion(self):
        posted = self.hint()
        self.run_worker()
        delivered = self.assert_hint_delivered(posted)
        between_commits = deepcopy(delivered)
        between_commits.update(text="", redacted=True, notice="交付记录尚未确认")
        between_commits["job"]["status"] = "running"
        # The preflight and first frame can observe companion completion before
        # learning evidence commits. The same cursor must later expose delivery.
        with patch("app.routers.v1_jobs.response_access", side_effect=[between_commits, between_commits, delivered]):
            stream = self.client.get(f"/api/v1/companion/responses/{posted['response_id']}/stream")
        self.assertEqual(stream.status_code, 200, stream.text)
        frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["status"], "completed")
        self.assertTrue(frames[0]["redacted"])
        self.assertEqual(frames[0]["text"], "")
        self.assertEqual(frames[-1]["last_event_id"], frames[0]["last_event_id"])
        self.assertFalse(frames[-1]["redacted"])
        self.assertEqual(frames[-1]["text"], delivered["text"])
        self.assertEqual(frames[-1]["job"]["status"], "succeeded")
        self.assertEqual(len(self.gateway.calls), 1)

    def test_hint_raw_output_never_enters_companion_or_sse_and_fallback_is_not_delivered(self):
        posted = self.hint()
        self.gateway.handler = lambda purpose, messages, **kw: FakeGateway.result(
            purpose, messages, text=json.dumps({"action_id": "recall_choice", "answer": RAW_MARKER}))
        self.run_worker()
        self.assert_job(posted["job_id"], "succeeded")
        response = response_access(self.runtime, "student", posted["response_id"])
        self.assertEqual(response["content_type"], "safe_guidance")
        self.assertIn("fallback", self.hint_states(posted["response_id"]))
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        with self.runtime.companion.connect() as db:
            self.assertNotIn(RAW_MARKER, "\n".join(db.iterdump()))
        for path in (f"/api/v1/companion/responses/{posted['response_id']}",
                     f"/api/v1/companion/responses/{posted['response_id']}/events",
                     f"/api/v1/companion/responses/{posted['response_id']}/stream?once=true",
                     f"/api/v1/jobs/{posted['job_id']}/events?once=true",
                     f"/api/v1/jobs/{posted['job_id']}/calls"):
            visible = self.client.get(path)
            self.assertEqual(visible.status_code, 200, visible.text)
            self.assertNotIn(RAW_MARKER, visible.text)
        self.assertIn(RAW_MARKER, self.assert_one_charge(posted["job_id"]).result["text"])

    def test_hint_known_unsent_failure_completes_safe_fallback_without_delivery_or_cost(self):
        posted = self.hint()
        def unavailable(*args, **kwargs):
            raise GatewayError("unavailable", request_sent=False)
        self.gateway.handler = unavailable
        self.run_worker()
        self.assert_job(posted["job_id"], "succeeded")
        response = response_access(self.runtime, "student", posted["response_id"])
        self.assertEqual(response["content_type"], "safe_guidance")
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        call, = self.runtime.queue.list_calls(posted["job_id"])
        self.assertEqual((call.status, call.cost_units, call.cost_pending), ("failed", 0, False))
        self.assertEqual(self.runtime.queue.budget_ledger.query("project", "platform").reserved_units, 0)

    def test_hint_durable_call_before_checkpoint_recovers_without_second_send(self):
        posted = self.hint()
        with patch("app.services.restricted_hints.commit_hint", side_effect=SimulatedCrash("durable call before hint checkpoint")):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        self.assertIsNone(self.runtime.companion.get_hint_checkpoint(posted["response_id"]))
        self.assertEqual(self.assert_one_charge(posted["job_id"]).status, "succeeded")
        self.reopen(forbid_model=True)
        self.recover_expired(posted["job_id"])
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_storage_error_after_paid_call_recovers_without_second_send(self):
        posted = self.hint()
        with patch("app.services.restricted_hints.commit_hint", side_effect=sqlite3.OperationalError("fixture busy")):
            self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        self.assertIsNone(self.runtime.companion.get_hint_checkpoint(posted["response_id"]))
        self.assertEqual(self.runtime.companion.get_response(posted["response_id"])["status"], "streaming")
        self.assert_one_charge(posted["job_id"])
        self.reopen(forbid_model=True)
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_learning_storage_error_keeps_safe_checkpoint_until_confirmed_recovery(self):
        posted = self.hint()
        with patch.object(self.runtime.learning, "record_restricted_hint", side_effect=sqlite3.OperationalError("fixture busy")):
            self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        self.assertIsNotNone(self.runtime.companion.get_hint_checkpoint(posted["response_id"]))
        self.assertEqual(self.runtime.companion.get_response(posted["response_id"])["status"], "streaming")
        self.assertEqual(self.hint_states(posted["response_id"]), ["requested"])
        self.assertEqual(response_access(self.runtime, "student", posted["response_id"])["text"], "")
        self.reopen(forbid_model=True)
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_delivery_storage_error_repairs_completed_projection_without_rebegin(self):
        posted = self.hint()
        original = self.runtime.learning.record_restricted_hint
        def storage_error(*args, **kwargs):
            if kwargs["status"] == "validated_delivered":
                raise sqlite3.OperationalError("fixture busy")
            return original(*args, **kwargs)
        with patch.object(self.runtime.learning, "record_restricted_hint", side_effect=storage_error):
            self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        before = self.runtime.companion.get_response(posted["response_id"])
        self.assertEqual(before["status"], "completed")
        self.assertEqual(response_access(self.runtime, "student", posted["response_id"])["text"], "")
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        self.reopen(forbid_model=True)
        self.resume(posted["job_id"], "student")
        with patch.object(self.runtime.companion, "begin_response", side_effect=AssertionError("completed hint must retain its binding")):
            self.run_worker()
        after = self.assert_hint_delivered(posted)
        self.assertEqual(after["generation"], before["generation"])
        self.assertEqual(after["assistant_message_id"], before["assistant_message_id"])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_cancel_completed_delivered_hint_after_queue_ack_failure_hides_all_reads(self):
        posted = self.hint()
        with patch.object(self.runtime.queue, "finish_step", side_effect=sqlite3.OperationalError("fixture ack unavailable")):
            self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        raw = self.hint_read_references(posted)
        cancelled = self.client.post(f"/api/v1/jobs/{posted['job_id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assert_cancelled_hint_keeps_facts(posted, raw, status="cancelled")
        self.reopen(forbid_model=True)
        self.assert_cancelled_hint_keeps_facts(posted, raw, status="cancelled")

    def test_cancel_completed_delivered_hint_awaiting_confirmation_hides_all_reads(self):
        posted = self.hint()
        with patch.object(self.runtime.queue, "finish_step", side_effect=SimulatedCrash("delivered before queue ack")):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        self.recover_expired(posted["job_id"])
        raw = self.hint_read_references(posted)
        cancelled = self.client.post(f"/api/v1/jobs/{posted['job_id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assert_cancelled_hint_keeps_facts(posted, raw, status="cancelled")

    def test_cancel_requested_reconciling_delivered_hint_hides_before_queue_terminal(self):
        posted = self.hint()
        queue, store = self.runtime.queue, self.runtime.companion
        job = queue.claim(self.worker.worker_id, lease_seconds=60)
        self.assertEqual(job.job_id, posted["job_id"])
        step = queue.start_step(job.job_id, "answer", self.worker.worker_id, job.generation)
        call_id = f"{job.job_id}:answer:{step.attempts}"
        store.begin_response(posted["response_id"], call_id=call_id, generation=job.generation)
        response = store.get_response(posted["response_id"])
        prepared = prepare_hint(self.runtime, response)
        queue.start_call(job.job_id, call_id, self.worker.worker_id, job.generation,
                         step_name=step.name, reserve_units=12)
        result = asyncio.run(self.gateway.complete("hint", prepared["messages"], json_mode=True))
        queue.record_call_usage(job.job_id, call_id, usage=USAGE, cost_units=12, cost_pending=False)
        commit_hint(self.runtime, response, prepared, validate_hint(prepared, result.text),
                    call_id=call_id, generation=job.generation)
        # Usage and domain delivery can survive without a final call/step ack.
        queue.mark_call_unknown(job.job_id, call_id, self.worker.worker_id, job.generation)
        self.assert_job(job.job_id, "reconciling")
        raw = self.hint_read_references(posted)
        cancelled = self.client.post(f"/api/v1/jobs/{job.job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assert_cancelled_hint_keeps_facts(posted, raw, status="reconciling")
        queue.reconcile_call(job.job_id, call_id, "failed", usage=USAGE, cost_units=12, cost_pending=False)
        self.assert_cancelled_hint_keeps_facts(posted, raw, status="cancelled")

    def test_completed_chat_remains_readable_after_failed_job_cancel(self):
        task = self.runtime.learning.get_task(self.task_id, "teacher")
        self.runtime.learning.close_task(self.task_id, "teacher", expected_version=task["version"],
                                         request_id=self.key(), reason="fixture allows ordinary chat")
        self.as_user("student")
        posted = self.runtime.companion.post_message(self.conversation["id"], "student", "ordinary chat", request_id=self.key())
        self.dispatcher._companion()
        job = self.runtime.queue.claim(self.worker.worker_id, lease_seconds=60)
        self.runtime.companion.begin_response(posted["response_id"], call_id="chat-fixture", generation=job.generation)
        self.runtime.companion.complete_response(posted["response_id"], "completed chat", call_id="chat-fixture", generation=job.generation)
        self.runtime.queue.finish(job.job_id, self.worker.worker_id, job.generation, success=False, error="fixture ack unavailable")
        cancelled = self.client.post(f"/api/v1/jobs/{job.job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        visible = self.client.get(f"/api/v1/companion/responses/{posted['response_id']}")
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual((visible.json()["status"], visible.json()["text"], visible.json()["redacted"]),
                         ("completed", "completed chat", False))
        self.assertEqual(visible.json()["job"]["status"], "cancelled")
        self.assertEqual(self.gateway.calls, [])

    def test_cancel_after_hint_storage_error_stays_terminal_and_keeps_charge(self):
        posted = self.hint()
        with patch.object(self.runtime.learning, "record_restricted_hint", side_effect=sqlite3.OperationalError("fixture busy")):
            self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        self.as_user("student")
        cancelled = self.client.post(f"/api/v1/jobs/{posted['job_id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.runtime.companion.get_response(posted["response_id"])["status"], "cancelled")
        self.resume(posted["job_id"], "student", status=409)
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_checkpoint_before_learning_write_recovers_across_real_databases(self):
        posted = self.hint()
        with patch.object(self.runtime.learning, "record_restricted_hint", side_effect=SimulatedCrash("checkpoint before evidence")):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        self.assertIsNotNone(self.runtime.companion.get_hint_checkpoint(posted["response_id"]))
        self.assertEqual(self.hint_states(posted["response_id"]), ["requested"])
        self.assertEqual(response_access(self.runtime, "student", posted["response_id"])["text"], "")
        self.reopen(forbid_model=True)
        self.recover_expired(posted["job_id"])
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_completed_companion_without_delivered_evidence_is_hidden_then_repaired(self):
        posted = self.hint()
        original = self.runtime.learning.record_restricted_hint
        def crash_delivery(*args, **kwargs):
            if kwargs["status"] == "validated_delivered":
                raise SimulatedCrash("companion committed before learning delivery")
            return original(*args, **kwargs)
        with patch.object(self.runtime.learning, "record_restricted_hint", side_effect=crash_delivery):
            with self.assertRaises(SimulatedCrash):
                self.run_worker()
        raw = self.runtime.companion.get_response(posted["response_id"])
        self.assertEqual(raw["status"], "completed")
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        public = response_access(self.runtime, "student", posted["response_id"])
        self.assertTrue(public["redacted"])
        self.assertEqual(public["text"], "")
        self.reopen(forbid_model=True)
        self.recover_expired(posted["job_id"])
        self.resume(posted["job_id"], "student")
        with patch.object(self.runtime.companion, "begin_response", side_effect=AssertionError("completed hint must retain its binding")):
            self.run_worker()
        completed = self.assert_hint_delivered(posted)
        self.assertEqual(completed["assistant_message_id"], raw["assistant_message_id"])
        self.assertEqual(completed["generation"], raw["generation"])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_unknown_reconciliation_and_confirmed_replay_never_duplicate_delivery(self):
        posted = self.hint()
        def unknown(*args, **kwargs):
            raise GatewayError("disconnected", request_sent=True, unknown_outcome=True)
        self.gateway.handler = unknown
        self.run_worker()
        self.assert_job(posted["job_id"], "reconciling")
        call = self.assert_one_charge(posted["job_id"], pending=True)
        self.assertEqual(response_access(self.runtime, "student", posted["response_id"])["text"], "")
        self.resume(posted["job_id"], "student", status=409)
        self.as_user("admin")
        reconciled = self.client.post(f"/api/v1/admin/jobs/{posted['job_id']}/calls/{call.call_id}/reconcile", json={
            "outcome": "succeeded", "result": asdict(FakeGateway.result("hint", self.gateway.calls[0]["messages"])),
            "cost_units": 12, "usage": USAGE, "reason": "verified fixture buffered result",
        })
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        self.assert_job(posted["job_id"], "awaiting_confirmation")
        self.assertFalse(self.worker.run_once(dispatch=False))
        self.reopen(forbid_model=True)
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_missing_usage_stays_hidden_and_replays_after_cost_settlement(self):
        posted = self.hint()
        self.gateway.handler = lambda purpose, messages, **kw: FakeGateway.result(purpose, messages, usage=None)
        self.run_worker()
        call = self.assert_one_charge(posted["job_id"], pending=True)
        self.assertEqual(call.status, "succeeded")
        self.assertGreater(self.runtime.queue.budget_ledger.query("project", "platform").reserved_units, 0)
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        self.assertEqual(response_access(self.runtime, "student", posted["response_id"])["text"], "")
        self.resume(posted["job_id"], "student", status=409)
        self.assertTrue(self.runtime.queue.settle_call_cost(call.call_id, 12, usage=USAGE))
        self.reopen(forbid_model=True)
        self.resume(posted["job_id"], "student")
        self.run_worker()
        self.assert_hint_delivered(posted)
        self.assertEqual(len(self.gateway.calls), 1)
        self.assert_one_charge(posted["job_id"])

    def test_hint_cancelled_after_send_never_adopts_template_or_delivered_evidence(self):
        posted = self.hint()
        def cancel(purpose, messages, **kwargs):
            self.runtime.queue.cancel(posted["job_id"])
            return FakeGateway.result(purpose, messages)
        self.gateway.handler = cancel
        self.run_worker()
        job = self.assert_job(posted["job_id"], "reconciling")
        self.assertTrue(job.cancel_requested)
        call = self.assert_one_charge(posted["job_id"])
        self.assertIsNone(call.result)
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        response = response_access(self.runtime, "student", posted["response_id"])
        self.assertEqual((response["status"], response["text"]), ("cancelled", ""))
        self.runtime.queue.reconcile_call(job.job_id, call.call_id, "failed", cost_units=12, usage=USAGE, cost_pending=False)
        self.assert_job(job.job_id, "cancelled")
        self.resume(job.job_id, "student", status=409)

    def test_hint_membership_revoked_after_paid_call_rejects_adoption(self):
        posted = self.hint()
        def revoke(purpose, messages, **kwargs):
            self.runtime.platform.delete_class_membership(self.classroom["id"], "student")
            return FakeGateway.result(purpose, messages)
        self.gateway.handler = revoke
        self.run_worker()
        self.assert_job(posted["job_id"], "failed")
        self.assert_one_charge(posted["job_id"])
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        raw = self.runtime.companion.get_response(posted["response_id"])
        self.assertEqual(raw["text"], "")
        self.assertEqual(self.client.get(f"/api/v1/companion/responses/{posted['response_id']}/stream?once=true").status_code, 404)

    def test_hint_saved_attempt_changed_during_call_falls_back_without_delivery(self):
        posted = self.hint()
        def change_attempt(purpose, messages, **kwargs):
            self.save_attempt(answer=1)
            return FakeGateway.result(purpose, messages)
        self.gateway.handler = change_attempt
        self.run_worker()
        self.assert_job(posted["job_id"], "succeeded")
        response = response_access(self.runtime, "student", posted["response_id"])
        self.assertEqual(response["content_type"], "safe_guidance")
        self.assertNotIn("validated_delivered", self.hint_states(posted["response_id"]))
        self.assert_one_charge(posted["job_id"])


if __name__ == "__main__":
    unittest.main()
