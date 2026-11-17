"""Verifier regressions; run through run_tests.py's disposable bootstrap.

The live worker/HTTP interleaving is covered in test_teaching_worker. These
controlled observations cover timeout, terminal invariants and safe evidence
without running a server, opening application databases or sending a request.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import verify_teaching_loop as acceptance


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CancellationObservations:
    def __init__(self, verifier, *, stuck=False):
        self.verifier = verifier
        self.stuck = stuck
        self.cancel_requested = False
        self.reads = 0
        self.requests = []
        self.insight = {"id": "fixture-insight", "job_id": "teaching-insight:fixture-insight"}
        self.path = "/api/v1/jobs/" + self.insight["job_id"]
        self.call_id = self.insight["job_id"] + ":summary:1"
        self.job_override = {}
        self.call_override = {}
        self.domain_override = {}
        self.extra_call = False
        self.resume_code = "cancelled_job"
        self.gateway_status = "cancelled_before_result"
        self.marker = "PRIVATE-RESULT-REQUEST-CREDENTIAL-MARKER"

    def events(self):
        event = {"call_id": self.call_id, "purpose": "summary", "mode": "hold",
                 "status": self.gateway_status if self.cancel_requested else "running",
                 "started_at": "2026-11-17T00:00:00+00:00",
                 "finished_at": "2026-11-17T00:00:01+00:00" if self.cancel_requested else None,
                 "selection": {"private_text": self.marker}, "reconcile_result": self.marker}
        return [event, {**event, "call_id": "unexpected-send"}] if self.extra_call and self.cancel_requested else [event]

    def job(self):
        settled = not self.stuck and self.reads > 1
        return {"status": "cancelled" if settled else "reconciling", "cancel_requested": True,
                "reconciliation_required": not settled, "cost_pending": False,
                "private": self.marker, **self.job_override}

    def api(self, actor, method, path, **options):
        self.requests.append((actor, method, path))
        if method == "POST" and path == self.path + "/cancel":
            self.cancel_requested = True
            return {"status": "running", "cancel_requested": True, "reconciliation_required": False,
                    "cost_pending": True, "private": self.marker}
        if method == "GET" and path == self.path:
            self.reads += 1
            return self.job()
        if method == "GET" and path == self.path + "/calls":
            return {"items": [{"call_id": self.call_id, "status": "unknown" if self.stuck else "not_executed",
                               "cost_units": 0, "cost_pending": False, "result": self.marker,
                               "created_at": 1.0, "updated_at": 2.0, **self.call_override}]}
        if method == "GET" and path == acceptance.INSIGHTS + "/" + self.insight["id"]:
            return {"status": "cancelled", "result": None, "source": self.marker, **self.domain_override}
        if method == "POST" and path == self.path + "/resume":
            if options.get("expected") != 409:
                raise AssertionError("cancelled resume must require HTTP 409")
            return {"detail": {"code": self.resume_code, "message": self.marker}}
        raise AssertionError("unexpected request, including any automatic reconciliation")

    def sql(self, database, statement, values):
        if (database != "jobs" or "SELECT event_id,event_type,created_at FROM events" not in statement
                or values != (self.insight["job_id"],)):
            raise AssertionError("only this job's event metadata may be read")
        return [{"event_id": 1, "event_type": "reconciliation_required", "created_at": 1.0},
                *([] if self.stuck else [{"event_id": 2, "event_type": "cancelled", "created_at": 2.0}])]

    def install(self, case):
        case.enterContext(patch.object(self.verifier, "api", side_effect=self.api))
        case.enterContext(patch.object(self.verifier, "events", side_effect=self.events))
        case.enterContext(patch.object(self.verifier, "sql", side_effect=self.sql))


class TeachingLoopAcceptanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="teaching-loop-verifier-")
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.verifier = acceptance.Verification(self.output, SimpleNamespace(timeout=0.3))
        self.clock = Clock()
        self.enterContext(patch.object(acceptance, "time", self.clock))

    def saved(self):
        return json.loads((self.output / "evidence.json").read_text(encoding="utf-8"))

    def test_permanent_reconciling_times_out_and_saves_metadata_before_cleanup(self):
        observations = CancellationObservations(self.verifier, stuck=True)
        observations.install(self)
        with self.assertRaisesRegex(acceptance.VerificationFailure,
                                    "timed out waiting for held unsent summary cancellation; last status=reconciling"):
            self.verifier.cancel_held_insight(observations.insight, calls_before=0)
        self.assertGreaterEqual(self.clock.now, self.verifier.args.timeout)
        audit = self.saved()["hold_cancel"]
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["error_type"], "VerificationFailure")
        self.assertEqual(audit["final_job"]["status"], "reconciling")
        self.assertEqual(audit["calls"][0]["status"], "unknown")
        self.assertGreater(len(audit["observations"]), 1)
        self.assertTrue(audit["queue_events"])
        self.assertNotIn(observations.marker, json.dumps(self.saved()))
        self.assertFalse(any(path.endswith(("/resume", "/reconcile")) for _, _, path in observations.requests))

    def test_known_unsent_cancel_requires_all_terminal_invariants(self):
        cases = (
            ("job flags", {"job_override": {"reconciliation_required": True}}, "did not settle the job"),
            ("pending cost", {"job_override": {"cost_pending": True}}, "did not settle the job"),
            ("nonzero cost", {"call_override": {"cost_units": 1}}, "not settled once at zero cost"),
            ("unknown call", {"call_override": {"status": "unknown"}}, "not settled once at zero cost"),
            ("unsettled call", {"call_override": {"cost_pending": True}}, "not settled once at zero cost"),
            ("result escaped", {"domain_override": {"result": {"text": "unwanted"}}}, "published a result"),
            ("not cancelled", {"domain_override": {"status": "running"}}, "published a result"),
            ("resent call", {"extra_call": True}, "resent a gateway call"),
            ("unknown gateway", {"gateway_status": "fixture_unknown_outcome"}, "no-send cancellation"),
            ("wrong conflict", {"resume_code": "job_conflict"}, "resume was not rejected"),
        )
        for name, changes, message in cases:
            with self.subTest(name=name):
                observations = CancellationObservations(self.verifier)
                for key, value in changes.items():
                    setattr(observations, key, deepcopy(value))
                with patch.object(self.verifier, "api", side_effect=observations.api), \
                        patch.object(self.verifier, "events", side_effect=observations.events), \
                        patch.object(self.verifier, "sql", side_effect=observations.sql):
                    with self.assertRaisesRegex(acceptance.VerificationFailure, message):
                        self.verifier.cancel_held_insight(observations.insight, calls_before=0)
                self.assertEqual(self.saved()["hold_cancel"]["status"], "failed")
                self.assertNotIn(observations.marker, json.dumps(self.saved()))

    def test_other_job_waits_still_reject_unknown_outcomes_immediately(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"status": "reconciling"})
        with patch.object(self.verifier, "request", return_value=response):
            with self.assertRaisesRegex(acceptance.VerificationFailure,
                                        "background job ended in reconciling; expected cancelled"):
                self.verifier.job("teacher", "sent-call", ("cancelled",))
        self.assertEqual(self.clock.now, 0)

    def test_metadata_capture_failure_preserves_original_timeout_and_safe_evidence(self):
        observations = CancellationObservations(self.verifier, stuck=True)
        observations.install(self)
        with patch.object(self.verifier, "sql", side_effect=RuntimeError(observations.marker)):
            with self.assertRaisesRegex(acceptance.VerificationFailure, "timed out waiting"):
                self.verifier.cancel_held_insight(observations.insight, calls_before=0)
        audit = self.saved()["hold_cancel"]
        self.assertEqual(audit["capture_errors"], [{"source": "queue_events", "error_type": "RuntimeError"}])
        self.assertEqual(audit["calls"][0]["status"], "unknown")
        self.assertNotIn(observations.marker, json.dumps(self.saved()))
