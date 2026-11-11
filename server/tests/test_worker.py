"""Worker regressions with real disposable queues/domain stores and fake models.

LearningDouble documents the score_units worker contract while LearningStore's
integration is being revised independently. Queue, companion, content, platform,
price calculation, parser, and question-pipeline persistence are all real.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import inspect
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["SKIP_ENV_FILE"] = "1"

from app.companion_store import CompanionStore
from app.content_store import ContentStore
from app.dispatcher import Dispatcher
from app.job_queue import BudgetLedger, JobQueue, LeaseLost
from app.platform_store import PlatformStore
from app.services.model_gateway import GatewayError, GatewayResult, ModelGateway
from app.services.material_parser import parse_material
from app.services.question_bank import QuestionBank
from app.worker import Worker, finalize_domain_job


USAGE = {"prompt_tokens": 7, "completion_tokens": 5}
MESSAGES = [{"role": "user", "content": "approved mathematical question"}]
STEPS = ("generate", "review", "compare", "recheck", "compare_recheck")


def price_config():
    return json.dumps([{
        "provider": "fixture", "model": model, "version": "fixture-price-v1", "currency": "fixture-unit",
        "input_units_per_million": 1_000_000, "output_units_per_million": 1_000_000,
    } for model in ("gpt-5.6-sol", "gpt-5.6-terra")])


class FakeGateway:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []
        self.descriptions = []

    def describe(self, purpose):
        self.descriptions.append(purpose)
        model = "gpt-5.6-sol" if purpose in {"teacher", "suggest", "generate", "review"} else "gpt-5.6-terra"
        return {"provider": SimpleNamespace(name="fixture"), "model": model,
                "reasoning_effort": "high", "max_tokens": 64, "timeout_seconds": 1}

    @staticmethod
    def result(purpose="student", *, text="complete answer", data=None, usage=USAGE):
        model = "gpt-5.6-sol" if purpose in {"teacher", "suggest", "generate", "review"} else "gpt-5.6-terra"
        return GatewayResult(text, data, usage, "fixture-request", model, "high", "fixture", True, usage is None)

    async def complete(self, purpose, messages, **options):
        self.calls.append({"purpose": purpose, "messages": deepcopy(messages), **options})
        if self.handler is not None:
            value = self.handler(purpose, messages, **options)
            return await value if inspect.isawaitable(value) else value
        if options.get("on_delta") is not None:
            options["on_delta"]("complete answer")
        return self.result(purpose)


class LearningDouble:
    """Explicit worker contract, not a replacement LearningStore implementation."""

    def __init__(self):
        self.allowed = True
        self.versions = {}
        self.grading = None
        self.applied = []
        self.marked = []
        self.manual = []

    def get_answer_policy(self, user_id, *, task_id=None, question_version_id=None):
        return {"allow_generation": self.allowed, "allow_assistant_content": self.allowed, "attempted": True}

    def list_grading_outbox(self, *, limit=100):
        return []

    def get_grading_input(self, attempt_id):
        if self.grading is None or self.grading["attempt_id"] != attempt_id:
            raise LookupError("grading input missing")
        return deepcopy(self.grading)

    def mark_grading_outbox(self, job_id, **options):
        self.marked.append((job_id, options))

    def apply_grading_result(self, attempt_id, item_id, result, **options):
        if type(result.get("score_units")) is not int:
            raise AssertionError("worker contract requires integer score_units")
        self.applied.append((attempt_id, item_id, deepcopy(result), options))
        self.grading["status"] = "graded"
        self.grading["outbox_status"] = "completed"

    def fail_grading(self, attempt_id, **options):
        self.manual.append((attempt_id, options))
        self.grading["status"] = "review_required"
        self.grading["outbox_status"] = "manual"

    def get_question(self, question_id, version, actor_id, **options):
        try:
            return deepcopy(self.versions[(question_id, version)])
        except KeyError:
            raise LookupError("question version missing") from None

    def find_question_duplicate(self, course_id, content_sha, family_sha):
        for (question_id, version), question in self.versions.items():
            if question["course_id"] == course_id and (question["content_sha"] == content_sha or question["family_sha"] == family_sha):
                return {"question_id": question_id, "version": version}
        return None

    def create_question_version(self, actor_id, question_id, version, question, **options):
        key = (question_id, version)
        if key in self.versions:
            raise AssertionError("duplicate question publication")
        self.versions[key] = {**deepcopy(question), **deepcopy(options)}
        return {"question_id": question_id, "version": version}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="worker-regression-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.queue = JobQueue(self.root / "jobs.sqlite3")
        self.ledger = BudgetLedger(self.queue)
        self.ledger.assign("teaching", "default", 1_000_000)
        self.ledger.assign("project", "default", 1_000_000)
        self.companion = CompanionStore(self.root / "companion.sqlite3")
        self.content = ContentStore(self.root / "content.sqlite3")
        self.platform = PlatformStore(self.root / "platform.sqlite3")
        self.learning = LearningDouble()
        self.settings = SimpleNamespace(llm_prices_json=price_config(), worker_lease_seconds=60.0, worker_poll_seconds=0.01)
        self.gateway = FakeGateway()
        self.runtime = SimpleNamespace(queue=self.queue, companion=self.companion, content=self.content,
                                       platform=self.platform, learning=self.learning, settings=self.settings,
                                       gateway=self.gateway, last_worker_error=None)
        self.worker = Worker(self.runtime, worker_id="fixture-worker")
        self.teacher = self.platform.create_user("fixture-teacher", "Teacher", "fixture-hash", "teacher")
        self.student = self.platform.create_user("fixture-student", "Student", "fixture-hash", "student")
        self.course = self.platform.create_course("Fixture course", "2026", self.teacher["id"])
        self.classroom = self.platform.create_class(self.course["id"], "Fixture section", owner_teacher_id=self.teacher["id"])
        self.platform.create_course_membership(self.course["id"], self.student["id"])
        self.platform.create_class_membership(self.classroom["id"], self.student["id"])
        self.sequence = 0

    def key(self, prefix="fixture"):
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    def posted_job(self, *, wallet=False):
        conversation = self.companion.create_conversation(self.student["id"], "Study", course_id=self.course["id"], request_id=self.key("conversation"))
        posted = self.companion.post_message(conversation["id"], self.student["id"], "How can I understand one half?", request_id=self.key("message"))
        payload = {"purpose": "student", "resource_type": "response", "resource_id": posted["response_id"], "user_id": self.student["id"]}
        options = {}
        if wallet:
            self.queue.fund_budget("fixture-wallet", 50_000, transaction_id=self.key("fund"))
            options.update(budget_account_id="fixture-wallet", budget_limit=20_000)
        self.queue.enqueue("interaction", payload, job_id=posted["job_id"], steps=("answer",), paid=True, **options)
        event = next(event for event in self.companion.pending_outbox() if event["response_id"] == posted["response_id"])
        self.companion.mark_outbox(event["outbox_id"], job_id=posted["job_id"])
        return posted

    def claim(self, job_id):
        job = self.queue.claim(self.worker.worker_id, lease_seconds=self.settings.worker_lease_seconds)
        self.assertIsNotNone(job)
        self.assertEqual(job.job_id, job_id)
        return job

    def call_step(self, job):
        return self.queue.start_step(job.job_id, "answer", self.worker.worker_id, job.generation)

    def run_worker(self):
        self.assertTrue(self.worker.run_once(dispatch=False))

    def assert_response_ended(self, response_id):
        response = self.companion.get_response(response_id)
        self.assertIn(response["status"], {"completed", "failed", "cancelled"})
        self.assertEqual(self.companion.list_response_events(response_id)[-1]["event_type"], "end")
        return response

    def seed_successful_call(self, job, step, text="committed answer"):
        call_id = f"{job.job_id}:answer:{step.attempts}"
        self.queue.start_call(job.job_id, call_id, self.worker.worker_id, job.generation,
                              step_name=step.name, reserve_units=1000, price_version="fixture-price-v1")
        result = asdict(FakeGateway.result(text=text))
        self.queue.finish_call(job.job_id, call_id, self.worker.worker_id, job.generation,
                               success=True, result=result, usage=USAGE, cost_units=12, cost_pending=False)
        return call_id, result

    def recover(self, job):
        self.queue.recover_expired(now=job.lease_until + 1)
        current = self.queue.get_job(job.job_id)
        if current.status == "awaiting_confirmation":
            self.queue.confirm_recovery(job.job_id, approved=True)
        return self.queue.get_job(job.job_id)

    def make_content_job(self, *, class_id=None, purpose="generate", owner=None):
        owner = owner or self.teacher
        data = b"Two plus three equals five. Approved course material."
        material = self.content.import_material(
            course_id=self.course["id"], owner_id=owner["id"], filename="lesson.txt", content=data,
            parsed=parse_material("lesson.txt", data), license_code="own", rights_confirmed=True,
            allow_generation=True, allow_retrieval=True, external_processing_allowed=True,
        )
        block = self.content.list_blocks(material["id"])[0]
        self.content.confirm_blocks(material["id"], [block["id"]], actor_id=owner["id"], expected_revision=1)
        resource = QuestionBank(self.content, self.learning).create_generation(
            course_id=self.course["id"], owner_id=owner["id"], class_id=class_id,
            source_block_ids=[block["id"]], external_processing_allowed=True,
        )
        payload = {"purpose": purpose, "resource_type": "content_job", "resource_id": resource["id"],
                   "course_id": self.course["id"], "owner_id": owner["id"]}
        if class_id:
            payload["class_id"] = class_id
        job = self.queue.enqueue("batch", payload, job_id=f"content:{resource['id']}", paid=True, steps=STEPS)
        return resource, job

    @staticmethod
    def candidate():
        return {"type": "single_choice", "stem": "What is two plus three?", "options": ["Four", "Five"],
                "answer": 1, "explanation": "Add the two numbers.", "points": 1, "rubric": [], "source_refs": ["B1"]}

    def test_unsent_gateway_error_settles_zero_releases_wallet_and_ends_response(self):
        posted = self.posted_job(wallet=True)

        async def fail(*args, **options):
            raise GatewayError("unsafe_url", request_sent=False)

        self.gateway.handler = fail
        self.run_worker()
        job = self.queue.get_job(posted["job_id"])
        call, = self.queue.list_calls(job.job_id)
        self.assertEqual((job.status, job.reconciliation_required, job.cost_pending), ("failed", False, False))
        self.assertEqual((call.status, call.cost_units, call.cost_pending), ("failed", 0, False))
        self.assertIsNone(call.usage)
        self.assertEqual(self.queue.budget("fixture-wallet").reserved_units, 0)
        self.assertEqual(self.queue.budget("fixture-wallet").available_units, 50_000)
        self.assertEqual(self.assert_response_ended(posted["response_id"])["status"], "failed")
        self.assertEqual(len(self.gateway.calls), 1)

    def test_no_model_or_price_never_sends_and_domain_response_ends(self):
        for disabled in ("model", "price"):
            with self.subTest(disabled=disabled):
                posted = self.posted_job()
                if disabled == "model":
                    self.runtime.gateway = ModelGateway(SimpleNamespace(llm_providers=()))
                    self.settings.llm_prices_json = price_config()
                else:
                    self.runtime.gateway = self.gateway
                    self.settings.llm_prices_json = ""
                self.run_worker()
                self.assertEqual(self.queue.get_job(posted["job_id"]).status, "failed")
                self.assertEqual(self.queue.list_calls(posted["job_id"]), ())
                self.assert_response_ended(posted["response_id"])
        self.assertEqual(self.gateway.calls, [])

    def test_missing_usage_stays_pending_with_reservation_and_successful_response(self):
        posted = self.posted_job(wallet=True)
        self.gateway.handler = lambda purpose, messages, **options: FakeGateway.result(purpose, text="saved text", usage=None)
        self.run_worker()
        job = self.queue.get_job(posted["job_id"])
        call, = self.queue.list_calls(job.job_id)
        self.assertEqual(job.status, "succeeded")
        self.assertTrue(job.cost_pending)
        self.assertIsNone(call.usage)
        self.assertIsNone(call.cost_units)
        self.assertTrue(call.cost_pending)
        self.assertGreater(self.queue.budget("fixture-wallet").reserved_units, 0)
        self.assertEqual(self.assert_response_ended(posted["response_id"])["text"], "saved text")

    def test_heartbeat_cancellation_records_gateway_error_usage_without_adopting_result(self):
        posted = self.posted_job(wallet=True)
        self.settings.worker_lease_seconds = 0.15

        async def waiting(purpose, messages, **options):
            self.queue.cancel(posted["job_id"])
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise GatewayError("cancelled", request_sent=True, unknown_outcome=True, usage=USAGE)

        self.gateway.handler = waiting
        self.run_worker()
        call, = self.queue.list_calls(posted["job_id"])
        self.assertEqual((call.status, call.usage, call.cost_units, call.cost_pending), ("unknown", USAGE, 12, False))
        self.assertIsNone(call.result)
        self.assertTrue(call.late_cost)
        job = self.queue.get_job(posted["job_id"])
        self.assertEqual(job.status, "reconciling")
        self.assertTrue(job.cancel_requested)
        self.assertTrue(job.reconciliation_required)
        self.assertEqual(self.assert_response_ended(posted["response_id"])["status"], "failed")
        self.queue.reconcile_call(job.job_id, call.call_id, "failed", usage=USAGE, cost_units=12, cost_pending=False)
        self.assertEqual(self.queue.get_job(job.job_id).status, "cancelled")
        self.assertEqual(self.queue.budget("fixture-wallet").reserved_units, 0)

    def test_lease_loss_records_late_usage_and_keeps_unknown_persistent(self):
        posted = self.posted_job()
        self.settings.worker_lease_seconds = 0.15
        heartbeat = self.queue.heartbeat
        revoked = False

        async def waiting(*args, **options):
            nonlocal revoked
            revoked = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise GatewayError("cancelled", request_sent=True, unknown_outcome=True, usage=USAGE)

        def lost(job_id, worker_id, generation, **options):
            if revoked:
                self.queue.recover_expired(now=time.time() + 5)
                raise LeaseLost("fixture expiry")
            return heartbeat(job_id, worker_id, generation, **options)

        self.gateway.handler = waiting
        with patch.object(self.queue, "heartbeat", side_effect=lost):
            self.run_worker()
        reopened = JobQueue(self.root / "jobs.sqlite3")
        call, = reopened.list_calls(posted["job_id"])
        self.assertEqual((call.status, call.usage, call.cost_units), ("unknown", USAGE, 12))
        self.assertEqual(reopened.get_job(posted["job_id"]).status, "reconciling")
        self.assertIsNone(call.result)
        self.assert_response_ended(posted["response_id"])

    def test_operation_returns_after_heartbeat_loss_only_usage_is_recorded(self):
        posted = self.posted_job()
        self.settings.worker_lease_seconds = 0.15

        async def late_success(purpose, messages, **options):
            self.queue.cancel(posted["job_id"])
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return FakeGateway.result(purpose, text="must never be adopted")

        self.gateway.handler = late_success
        self.run_worker()
        call, = self.queue.list_calls(posted["job_id"])
        self.assertEqual((call.status, call.cost_units), ("unknown", 12))
        self.assertIsNone(call.result)
        response = self.assert_response_ended(posted["response_id"])
        self.assertNotIn("must never", response["text"])

    def test_cancelled_outer_operation_harvests_usage_and_persists_reconciliation(self):
        posted = self.posted_job()
        job = self.claim(posted["job_id"])
        step = self.call_step(job)

        async def scenario():
            started = asyncio.Event()

            async def waiting(*args, **options):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise GatewayError("cancelled", request_sent=True, unknown_outcome=True, usage=USAGE)

            self.gateway.handler = waiting
            task = asyncio.create_task(self.worker._call(job, step, "student", MESSAGES))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        call, = self.queue.list_calls(job.job_id)
        self.assertEqual((call.status, call.usage, call.cost_units), ("unknown", USAGE, 12))
        self.assertEqual(self.queue.get_job(job.job_id).status, "reconciling")

    def test_completed_response_crash_before_step_ack_recovers_without_begin_or_paid_call(self):
        posted = self.posted_job()
        old = self.claim(posted["job_id"])
        step = self.call_step(old)
        call_id, result = self.seed_successful_call(old, step)
        self.companion.begin_response(posted["response_id"], call_id=call_id, generation=old.generation)
        self.companion.complete_response(posted["response_id"], result["text"], call_id=call_id, generation=old.generation)
        self.recover(old)
        self.runtime.gateway = ModelGateway(SimpleNamespace(llm_providers=()))
        self.settings.llm_prices_json = ""
        with patch.object(self.companion, "begin_response", side_effect=AssertionError("terminal recovery must not begin")):
            self.run_worker()
        self.assertEqual(self.queue.get_job(old.job_id).status, "succeeded")
        self.assertEqual(self.queue.list_steps(old.job_id)[0].status, "succeeded")
        self.assertEqual(len(self.queue.list_calls(old.job_id)), 1)
        with self.companion.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event_type='end'").fetchone()[0], 1)

    def test_successful_call_replay_persists_delta_and_completes_exact_text_without_network(self):
        posted = self.posted_job()
        old = self.claim(posted["job_id"])
        step = self.call_step(old)
        call_id, result = self.seed_successful_call(old, step, "complete saved answer")
        self.companion.begin_response(posted["response_id"], call_id=call_id, generation=old.generation)
        self.companion.append_delta(posted["response_id"], 1, "complete ", call_id=call_id, generation=old.generation)
        self.recover(old)
        self.runtime.gateway = ModelGateway(SimpleNamespace(llm_providers=()))
        self.settings.llm_prices_json = ""
        self.run_worker()
        response = self.assert_response_ended(posted["response_id"])
        self.assertEqual(response["text"], result["text"])
        self.assertGreater(response["generation"], old.generation)
        events = self.companion.list_response_events(posted["response_id"])
        self.assertEqual("".join(event["data"]["text"] for event in events if event["event_type"] == "delta"), result["text"])
        self.assertEqual(self.queue.get_job(old.job_id).status, "succeeded")
        self.assertEqual(len(self.queue.list_calls(old.job_id)), 1)
        self.assertEqual(self.gateway.calls, [])

    def test_finalizer_cancel_pending_response_and_content_is_idempotent(self):
        posted = self.posted_job()
        job = self.queue.cancel(posted["job_id"])
        finalize_domain_job(self.runtime, job, "cancelled")
        finalize_domain_job(self.runtime, job, "cancelled")
        self.assertEqual(self.assert_response_ended(posted["response_id"])["status"], "failed")
        self.assertEqual(len([event for event in self.companion.list_response_events(posted["response_id"]) if event["event_type"] == "end"]), 1)
        resource, queued = self.make_content_job()
        job = self.queue.cancel(queued.job_id)
        finalize_domain_job(self.runtime, job, "cancelled")
        stopped = self.content.get_job(resource["id"])
        finalize_domain_job(self.runtime, job, "cancelled")
        self.assertEqual(self.content.get_job(resource["id"]), stopped)
        self.assertEqual(stopped["status"], "quarantined")
        self.assertEqual(stopped["state"]["worker_terminal"], {"reason": "cancelled", "cancelled": True})
        self.assertNotIn(resource["id"], [row["id"] for row in self.content.list_pending_jobs()])

    def test_failed_content_is_terminal_and_stopped_first_page_cannot_starve_new_jobs(self):
        failed_ids = []
        for index in range(101):
            resource = self.content.create_job("question_generate", course_id=self.course["id"], owner_id=self.teacher["id"], payload={})
            job = self.queue.enqueue("batch", {"purpose": "generate", "resource_type": "content_job", "resource_id": resource["id"]}, job_id=f"content:{resource['id']}")
            self.queue.cancel(job.job_id)
            failed_ids.append(resource["id"])
        fresh = self.content.create_job("question_generate", course_id=self.course["id"], owner_id=self.teacher["id"], payload={})
        self.assertNotIn(fresh["id"], [row["id"] for row in self.content.list_pending_jobs()])
        self.worker._finalize_stopped_content()
        self.assertEqual([row["id"] for row in self.content.list_pending_jobs()], [fresh["id"]])
        self.assertTrue(all(self.content.get_job(item)["status"] == "quarantined" for item in failed_ids))

    def test_content_failure_without_price_exits_pending_outbox(self):
        resource, job = self.make_content_job()
        self.settings.llm_prices_json = ""
        self.run_worker()
        self.assertEqual(self.queue.get_job(job.job_id).status, "failed")
        self.assertEqual(self.content.get_job(resource["id"])["status"], "quarantined")
        self.assertEqual(self.content.list_pending_jobs(), [])
        self.assertEqual(self.gateway.calls, [])

    def test_content_permission_rechecked_after_io_before_result_or_publication(self):
        resource, job = self.make_content_job(class_id=self.classroom["id"])
        successor = self.platform.create_user("fixture-successor", "Successor", "fixture-hash", "teacher")

        async def revoke_class(purpose, messages, **options):
            self.platform.transfer_class_owner(self.classroom["id"], successor["id"], transferred_by=self.teacher["id"])
            return FakeGateway.result(purpose, data={"questions": [self.candidate()]})

        self.gateway.handler = revoke_class
        self.run_worker()
        stopped = self.content.get_job(resource["id"])
        self.assertEqual(stopped["status"], "quarantined")
        self.assertNotIn("candidates", stopped["state"])
        self.assertEqual(self.learning.versions, {})
        self.assertEqual(self.queue.get_job(job.job_id).status, "failed")
        self.assertEqual(len(self.gateway.calls), 1)
        call, = self.queue.list_calls(job.job_id)
        self.assertEqual((call.status, call.cost_units), ("succeeded", 12))

    def test_content_course_permission_rechecked_before_each_send(self):
        manager = self.platform.create_user("fixture-manager", "Manager", "fixture-hash", "teacher")
        self.platform.set_course_collaborator(self.course["id"], manager["id"], "manage", granted_by=self.teacher["id"])
        resource, job = self.make_content_job(owner=manager)
        prepare = QuestionBank.prepare_input
        preparations = []

        def revoke_on_next_prepare(service, job_id, stage=None):
            prepared = prepare(service, job_id, stage=stage)
            preparations.append(stage)
            if stage == "review":
                self.platform.remove_course_collaborator(self.course["id"], manager["id"], removed_by=self.teacher["id"])
            return prepared

        self.gateway.handler = lambda purpose, messages, **options: FakeGateway.result(purpose, data={"questions": [self.candidate()]})
        with patch.object(QuestionBank, "prepare_input", new=revoke_on_next_prepare):
            self.run_worker()
        self.assertEqual(preparations, ["generate", "review"])
        self.assertEqual(len(self.gateway.calls), 1)
        stopped = self.content.get_job(resource["id"])
        self.assertEqual(stopped["status"], "quarantined")
        self.assertEqual(stopped["stage"], "review")
        self.assertEqual(self.learning.versions, {})

    def test_content_class_permission_required_before_first_send(self):
        resource, job = self.make_content_job(class_id=self.classroom["id"])
        successor = self.platform.create_user("fixture-successor", "Successor", "fixture-hash", "teacher")
        self.platform.transfer_class_owner(self.classroom["id"], successor["id"], transferred_by=self.teacher["id"])
        self.run_worker()
        self.assertEqual(self.queue.get_job(job.job_id).status, "failed")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.queue.list_calls(job.job_id), ())
        self.assertEqual(self.content.get_job(resource["id"])["status"], "quarantined")

    def test_content_successful_stages_route_only_from_job_payload(self):
        resource, job = self.make_content_job(purpose="review")
        outputs = iter((
            {"questions": [self.candidate()]},
            {"solutions": [{"index": 0, "answer": 1, "reasoning": "Independent sum equals five.", "solvable": True}]},
            {"comparisons": [{"index": 0, "agrees": True, "valid": True, "reason": "Both agree."}]},
        ))
        self.gateway.handler = lambda purpose, messages, **options: FakeGateway.result(purpose, data=next(outputs))
        self.run_worker()
        self.assertEqual(self.queue.get_job(job.job_id).status, "succeeded")
        completed = self.content.get_job(resource["id"])
        self.assertEqual(completed["status"], "completed", str(completed))
        self.assertEqual([call["purpose"] for call in self.gateway.calls], ["review"] * 3)
        self.assertEqual(len(self.learning.versions), 1)

    def test_objective_grading_smoke_reads_nested_scope_without_model_or_price(self):
        self.learning.grading = {
            "attempt_id": "fixture-attempt", "task_id": "fixture-task", "attempt_revision": 1,
            "status": "submitted", "outbox_status": "dispatched", "answers": {"q1": 1},
            "scope": {"owner_id": self.teacher["id"], "student_id": self.student["id"],
                      "course_id": self.course["id"], "class_id": self.classroom["id"]},
            "questions": [{"id": "q1", "type": "single_choice", "options": ["no", "yes"], "answer": 1, "score_units": 250}],
        }
        job = self.queue.enqueue("grading", {"purpose": "grading", "resource_id": "fixture-attempt", "input_revision": 1}, steps=("item:q1",))
        self.settings.llm_prices_json = ""
        self.runtime.gateway = ModelGateway(SimpleNamespace(llm_providers=()))
        self.run_worker()
        self.assertEqual(self.queue.get_job(job.job_id).status, "succeeded")
        self.assertEqual(self.learning.applied[0][2]["score_units"], 250)
        self.assertEqual(self.learning.applied[0][3]["expected_revision"], 1)
        self.assertEqual(self.queue.list_calls(job.job_id), ())
        self.assertEqual(self.learning.manual, [])


if __name__ == "__main__":
    unittest.main()
