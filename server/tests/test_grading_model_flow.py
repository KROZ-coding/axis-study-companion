"""Real Runtime/SQLite/Worker grading; only provider HTTP uses local fixtures."""

from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import unittest
from unittest.mock import patch

# Set these before config imports; inherited model credentials/config are unused.
os.environ["SKIP_ENV_FILE"] = "1"
for _key in tuple(os.environ):
    if _key.startswith(("LLM_", "MAXKB_")):
        os.environ.pop(_key)

import httpx

from app.config import LLMProvider, Settings
from app.runtime import Runtime
from app.services.math_grading import grade_objective
from app.services.model_gateway import ModelGateway, build_minimal_context
from app.worker import Worker


USAGE = {"prompt_tokens": 7, "completion_tokens": 5}
PRICES = json.dumps([{
    "provider": "grading-fixture", "model": "gpt-5.6-terra", "version": "grading-price-v1",
    "currency": "fixture-unit", "input_units_per_million": 2_000_000,
    "output_units_per_million": 3_000_000,
}])
RUBRIC = [{"point": "common denominator", "score_units": 200},
          {"point": "exact arithmetic", "score_units": 300}]
GRADE = {
    "points": [{"point": "common denominator", "score_units": 100, "why": "Partly justified."},
               {"point": "exact arithmetic", "score_units": 300, "why": "Correct calculation."}],
    "score_units": 400, "why": "Partial credit for the incomplete denominator explanation.",
}


class GradingModelFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="grading-model-flow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Windows asyncio creates its socketpair via a loopback TCP connect.
        # Permit that local mechanism only; provider HTTP still has no socket path.
        original_connect = socket.socket.connect

        def guarded_connect(sock, address):
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original_connect(sock, address)
            raise AssertionError("external socket connections are forbidden")

        connect_guard = patch("socket.socket.connect", new=guarded_connect)
        connect_guard.start()
        self.addCleanup(connect_guard.stop)
        # MockTransport is the only HTTP route; fixed DNS never resolves outside.
        for target in ("socket.create_connection", "socket.socket.connect_ex",
                       "httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("external network is forbidden"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)

        def local_dns(host, port, *args, **kwargs):
            self.assertEqual(host, "grading.example")
            self.assertEqual(port, 443)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        dns = patch("socket.getaddrinfo", side_effect=local_dns)
        dns.start()
        self.addCleanup(dns.stop)
        provider = LLMProvider(
            name="grading-fixture", channel="grading", base_url="https://grading.example/v1",
            api_key=secrets.token_urlsafe(24), model="gpt-5.6-terra", max_concurrency=1,
            queue_size=4, priority=10, quota_group="grading-fixture",
        )
        config = Settings(
            app_env="test", data_dir=str(self.root), worker_enabled=False,
            llm_providers=(provider,), llm_base_url="", llm_api_key="", llm_model="",
            llm_prices_json=PRICES, budget_currency="fixture-unit", worker_lease_seconds=60,
            llm_batch_max_tokens=128, maxkb_url="", maxkb_api_key="",
            maxkb_dataset_ids=(), maxkb_course_dataset_ids=(),
        )
        self.runtime = Runtime(config)
        self.addCleanup(self.runtime.stop)
        self.runtime.initialize()
        self.assertEqual(self.runtime.data_dir, self.root.resolve())
        self.requests = []
        self.reply = deepcopy(GRADE)

        def handler(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers["host"], "grading.example")
            self.assertEqual(request.extensions["sni_hostname"], "grading.example")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "gpt-5.6-terra")
            self.assertEqual(body["reasoning_effort"], "high")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertFalse(body["stream"])
            text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
            return httpx.Response(200, json={
                "id": "grading-fixture-response", "model": body["model"], "usage": USAGE,
                "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            }, headers={"x-request-id": "grading-fixture-request"})

        self.runtime.install("gateway", ModelGateway(config, transport=httpx.MockTransport(handler)))
        self.worker = Worker(self.runtime, worker_id="grading-flow-worker")
        platform = self.runtime.platform
        platform.create_user("fixture-teacher", "PRIVATE-TEACHER", "unused-fixture-hash", "teacher", user_id="teacher")
        platform.create_user("fixture-student", "PRIVATE-STUDENT", "unused-fixture-hash", "student", user_id="student")
        self.course = platform.create_course("Grading fixture", "2026", "teacher")
        self.classroom = platform.create_class(self.course["id"], "Fixture class", owner_teacher_id="teacher")
        platform.create_course_membership(self.course["id"], "student")
        platform.create_class_membership(self.classroom["id"], "student")
        self.ledger = self.runtime.queue.budget_ledger
        self.ledger.assign("teaching", self.classroom["id"], 1_000_000)
        self.ledger.assign("project", "platform", 1_000_000)
        self.ledger.assign("student_daily", "student", 1_000_000)

    def submit(self, question=None, answer="Use a common denominator, then add the numerators."):
        if question is None:
            question = {
                "type": "short_answer", "stem": "Explain how to add 1/3 and 1/6.",
                "answer": "Use denominator six; 2/6 + 1/6 = 1/2.", "points": 5,
                "rubric": deepcopy(RUBRIC), "answer_format": "explain the steps",
            }
        self.question = deepcopy(question)
        self.question.update(
            external_processing_permission=True,
            source_anchors=[{"path": "PRIVATE-SOURCE-PATH", "raw_text": "PRIVATE-RAW-MATERIAL"}],
            provenance={"student_name": "PRIVATE-STUDENT", "metadata": "PRIVATE-METADATA"},
            student_id="PRIVATE-STUDENT-ID", metadata={"secret": "PRIVATE-ARBITRARY-METADATA"},
        )
        learning = self.runtime.learning
        learning.create_question_version("teacher", "grading-question", 1, self.question,
                                         status="approved", course_id=self.course["id"])
        goal = learning.create_goal("teacher", "Grading goal", course_id=self.course["id"],
                                    class_id=self.classroom["id"], request_id="grading-goal")
        published = learning.publish_round(
            "teacher", goal["id"], expected_version=goal["version"], request_id="grading-publish", round_no=1,
            student_ids=["student"], question_versions=[{"question_id": "grading-question", "version": 1}],
        )
        self.task_id = published["task_ids"][0]
        lease = learning.acquire_draft_lease(self.task_id, "student", "fixture-device")
        saved = learning.save_draft(
            self.task_id, "student", {"grading-question": answer}, expected_version=lease["task_version"],
            request_id="grading-draft", lease_token=lease["lease_token"], fence_token=lease["fence_token"],
        )
        submitted = learning.submit_attempt(
            self.task_id, "student", expected_version=saved["version"], request_id="grading-submit",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"],
        )
        self.job_id = submitted["grading_job_id"]
        self.attempt_id = submitted["attempts"][0]["id"]
        return learning.get_grading_input(self.attempt_id)["questions"][0]

    def run_worker(self):
        self.assertTrue(self.worker.run_once())
        job = self.runtime.queue.get_job(self.job_id)
        self.assertEqual(job.status, "succeeded", repr(job))
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(step.status == "succeeded" for step in self.runtime.queue.list_steps(self.job_id)))
        return job

    def context(self):
        payload = json.loads(self.requests[0].content)
        context = json.loads(payload["messages"][1]["content"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in ("PRIVATE-", self.task_id, self.attempt_id, self.course["id"], self.classroom["id"]):
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(context), {"question", "answer"})
        self.assertNotIn("source_anchors", context["question"])
        self.assertNotIn("provenance", context["question"])
        self.assertNotIn("metadata", context["question"])
        return context

    def test_subjective_partial_credit_reaches_sqlite_through_real_worker_and_gateway(self):
        self.submit()
        self.run_worker()
        task = self.runtime.learning.get_task(self.task_id, "student")
        attempt = task["attempts"][0]
        self.assertEqual((task["status"], attempt["status"], attempt["total_score_units"]),
                         ("results_ready", "graded", 400))
        self.assertEqual([point["score_units"] for point in attempt["items"][0]["result"]["per_point"]], [100, 300])
        self.assertEqual(attempt["items"][0]["original_grading"]["score_units"], 400)
        context = self.context()
        self.assertEqual(context["question"]["rubric"], RUBRIC)
        self.assertEqual(context["question"]["score_units"], 500)
        self.assertEqual(context["question"]["answer_format"], "explain the steps")
        self.assertEqual(self.runtime.learning.get_grading_input(self.attempt_id)["outbox_status"], "completed")

    def test_upstream_usage_drives_actual_price_and_budget_settlement_once(self):
        self.submit()
        observed = []
        actual_transport = self.runtime.gateway._transport

        def observe_reservation(request):
            observed.append(self.ledger.query("teaching", self.classroom["id"]).reserved_units)
            return actual_transport.handle_request(request)

        self.runtime.install("gateway", ModelGateway(self.runtime.settings, transport=httpx.MockTransport(observe_reservation)))
        job = self.run_worker()
        call, = self.runtime.queue.list_calls(self.job_id)
        self.assertEqual(call.usage, USAGE)
        self.assertEqual((call.model_name, call.price_version), ("gpt-5.6-terra", "grading-price-v1"))
        self.assertEqual(call.metadata["purpose"], "grading")
        self.assertGreater(observed[0], 29)
        self.assertEqual((call.cost_units, call.cost_pending, job.budget_spent, job.budget_reserved), (29, False, 29, 0))
        self.assertFalse(job.cost_pending)
        for kind, identity in (("teaching", self.classroom["id"]), ("project", "platform")):
            budget = self.ledger.query(kind, identity)
            self.assertEqual((budget.spent_units, budget.reserved_units), (29, 0))
        self.assertEqual(self.ledger.query("student_daily", "student").spent_units, 0)
        self.assertEqual(self.runtime.queue.call_costs(self.job_id), {call.call_id: 29})
        self.assertFalse(self.worker.run_once())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.runtime.queue.call_costs(self.job_id), {call.call_id: 29})

    def test_invalid_provider_json_preserves_submission_and_routes_to_teacher(self):
        self.reply = '```json\n{"score_units":500}\n```'
        self.submit()
        self.run_worker()
        data = self.runtime.learning.get_grading_input(self.attempt_id)
        self.assertEqual((data["status"], data["outbox_status"]), ("review_required", "manual"))
        student = self.runtime.learning.get_task(self.task_id, "student")
        self.assertEqual(student["status"], "awaiting_manual")
        self.assertFalse(student["answer_released"])
        self.assertNotIn("total_score_units", student["attempts"][0])
        self.assertNotIn("answer", student["questions"][0])
        review = self.runtime.learning.get_teacher_review(self.attempt_id, "teacher")
        self.assertEqual(review["items"][0]["status"], "manual_pending")
        self.assertTrue(review["items"][0]["can_manual_grade"])
        self.assertEqual(review["items"][0]["answer"], data["answers"]["grading-question"])
        call, = self.runtime.queue.list_calls(self.job_id)
        self.assertEqual((call.status, call.usage, call.cost_units, call.cost_pending), ("failed", USAGE, 29, False))
        self.context()

    def test_math_restrictions_survive_question_freezing_and_reach_grading_upstream(self):
        question = {
            "type": "fill", "stem": "Give an expression equal to x for the stated domain.",
            "answer": "x", "points": 2.5, "rubric": [], "answer_format": "math",
            "domain": {"x": [0, 1]}, "variables": {"x": {"integer": True}},
            "assumptions": ["x is an integer"], "constraints": {"x": {"min": 0, "max": 1}},
        }
        frozen = self.submit(question, answer="x^2")
        self.assertEqual(grade_objective(frozen, "x^2")["status"], "uncertain")
        # New question versions cannot change the requirements of an assigned task.
        changed = deepcopy(self.question)
        changed["domain"] = "real"
        self.runtime.learning.create_question_version("teacher", "grading-question", 2, changed,
                                                     status="approved", course_id=self.course["id"])
        self.reply = {"correct": True, "why": "For x in {0,1}, x squared equals x."}
        self.run_worker()
        context = self.context()
        for key in ("answer_format", "domain", "variables", "assumptions", "constraints"):
            self.assertEqual(frozen[key], question[key])
            self.assertEqual(context["question"][key], question[key])
        self.assertEqual(context["question"]["score_units"], 250)
        independent = build_minimal_context(question=frozen)["question"]
        self.assertEqual(independent["domain"], question["domain"])
        self.assertNotIn("answer", independent)
        self.assertNotIn("rubric", independent)
        task = self.runtime.learning.get_task(self.task_id, "student")
        self.assertEqual(task["attempts"][0]["total_score_units"], 250)
        self.assertEqual(task["attempts"][0]["items"][0]["result"]["method"], "ai_math")


if __name__ == "__main__":
    unittest.main()
