from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from app.job_queue import (
    BudgetLedger,
    IdempotencyConflict,
    InsufficientBudget,
    InvalidTransition,
    JobQueue,
    LeaseLost,
    QueueSchemaError,
    ReconciliationRequired,
    _LEGACY_SCHEMA,
)


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="job-queue-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "queue.sqlite3"
        self.queue = JobQueue(self.database)

    def test_priority_fifo_steps_events_and_persistence(self) -> None:
        self.queue.enqueue("batch", {"n": 1}, steps=("load", "run"), job_id="batch-1", available_at=1)
        self.queue.enqueue("grading", {"n": 2}, job_id="grading-1", available_at=1)
        self.queue.enqueue("interaction", {"n": 3}, job_id="interaction-1", available_at=1)
        self.queue.enqueue("interaction", {"n": 4}, job_id="interaction-2", available_at=1)
        self.queue.enqueue("grading", {"n": 5}, job_id="grading-2", available_at=1)

        expected = ["interaction-1", "interaction-2", "grading-1", "grading-2", "batch-1"]
        for job_id in expected:
            claimed = self.queue.claim("worker", now=2)
            self.assertEqual(claimed.job_id, job_id)
            self.queue.finish(job_id, "worker", claimed.generation, success=False, retryable=False, now=2)

        persisted = JobQueue(self.database)
        job = persisted.get_job("batch-1")
        self.assertEqual(job.payload, {"n": 1})
        self.assertEqual([step.name for step in persisted.list_steps("batch-1")], ["load", "run"])
        events = persisted.list_events("batch-1")
        self.assertEqual([event.event_type for event in events], ["enqueued", "claimed", "failed"])
        self.assertEqual(persisted.list_events("batch-1", after=events[0].event_id), events[1:])

    def test_atomic_concurrent_claim_and_fencing_generation(self) -> None:
        ids = [f"job-{index}" for index in range(10)]
        for job_id in ids:
            self.queue.enqueue("batch", job_id=job_id, available_at=1)
        queues = [JobQueue(self.database) for _ in range(5)]

        def claim(index: int):
            return queues[index % len(queues)].claim(f"worker-{index}", now=2)

        with ThreadPoolExecutor(max_workers=10) as pool:
            claimed = [item for item in pool.map(claim, range(10)) if item is not None]
        self.assertEqual(len(claimed), len(ids))
        self.assertEqual({job.job_id for job in claimed}, set(ids))
        self.assertEqual(len({job.job_id for job in claimed}), len(claimed))

        original = claimed[0]
        with self.assertRaises(LeaseLost):
            self.queue.heartbeat(original.job_id, "other-worker", original.generation, now=2)
        for job in claimed[1:]:
            self.queue.finish(job.job_id, job.lease_owner, job.generation, success=False, now=2)
        recovered = self.queue.recover_expired(now=original.lease_until + 1)
        self.assertEqual(recovered, (original.job_id,))
        reclaimed = self.queue.claim("new-worker", now=original.lease_until + 2)
        self.assertEqual(reclaimed.job_id, original.job_id)
        self.assertGreater(reclaimed.generation, original.generation)
        with self.assertRaises(LeaseLost):
            self.queue.finish(original.job_id, "worker-0", original.generation, success=False, now=reclaimed.lease_until - 1)

    def test_expired_grading_recovers_and_paid_batch_waits_for_confirmation(self) -> None:
        self.queue.fund_budget("acct", 100)
        grading = self.queue.enqueue("grading", job_id="grading", available_at=1)
        paid = self.queue.enqueue("batch", job_id="paid", paid=True, budget_account_id="acct", budget_limit=30, available_at=1)
        first = self.queue.claim("worker", lease_seconds=5, now=2)
        self.assertEqual(first.job_id, grading.job_id)
        second = self.queue.claim("worker-2", lease_seconds=5, now=2)
        self.assertEqual(second.job_id, paid.job_id)

        recovered = JobQueue(self.database)
        affected = recovered.recover_expired(now=8)
        self.assertEqual(set(affected), {"grading", "paid"})
        self.assertEqual(recovered.get_job("grading").status, "queued")
        self.assertEqual(recovered.get_job("paid").status, "awaiting_confirmation")
        self.assertEqual(recovered.claim("worker-3", now=9).job_id, "grading")
        self.assertEqual(recovered.confirm_recovery("paid", approved=True, now=9).status, "queued")
        self.assertEqual(recovered.claim("worker-4", now=10).job_id, "paid")

    def test_expired_unresolved_call_requires_reconciliation_and_never_blindly_resends(self) -> None:
        self.queue.enqueue("grading", job_id="job", steps=("generate",), available_at=1)
        claimed = self.queue.claim("worker", lease_seconds=5, now=2)
        self.queue.start_step("job", "generate", "worker", claimed.generation, now=2)
        intent = self.queue.start_call("job", "provider-req-1", "worker", claimed.generation, step_name="generate", now=3)
        self.assertTrue(intent.should_send)

        expired = JobQueue(self.database).recover_expired(now=8)
        self.assertEqual(expired, ("job",))
        job = self.queue.get_job("job")
        self.assertEqual(job.status, "reconciling")
        self.assertTrue(job.reconciliation_required)
        job = self.queue.reconcile_call("job", "provider-req-1", "not_executed", now=9)
        self.assertEqual(job.status, "queued")
        self.assertFalse(job.reconciliation_required)

        next_claim = self.queue.claim("worker-2", now=11)
        self.assertGreater(next_claim.generation, claimed.generation)
        self.queue.start_step("job", "generate", "worker-2", next_claim.generation, now=11)
        retry_intent = self.queue.start_call("job", "provider-req-2", "worker-2", next_claim.generation, step_name="generate", now=12)
        self.assertTrue(retry_intent.should_send)

    def test_unknown_outcome_reconciles_success_without_replaying_provider_call(self) -> None:
        self.queue.enqueue("grading", job_id="job", steps=("grade",), available_at=1)
        claimed = self.queue.claim("worker", now=2)
        self.queue.start_step("job", "grade", "worker", claimed.generation, now=2)
        self.queue.start_call("job", "call", "worker", claimed.generation, step_name="grade", now=3)
        self.assertEqual(self.queue.mark_call_unknown("job", "call", "worker", claimed.generation, now=4).status, "reconciling")
        self.assertEqual(self.queue.reconcile_call("job", "call", "succeeded", result={"score": 9}, now=5).status, "queued")
        self.assertEqual(self.queue.list_steps("job")[0].status, "pending")
        next_claim = self.queue.claim("worker-2", now=6)
        self.queue.start_step("job", "grade", "worker-2", next_claim.generation, now=6)
        replay = self.queue.start_call("job", "call", "worker-2", next_claim.generation, step_name="grade", now=6)
        self.assertFalse(replay.should_send)
        self.assertEqual(replay.result, {"score": 9})
        self.queue.finish_step("job", "grade", "worker-2", next_claim.generation, success=True, output=replay.result, now=6)
        self.assertEqual(self.queue.list_steps("job")[0].status, "succeeded")

    def test_paid_job_reconciliation_requires_confirmation(self) -> None:
        self.queue.fund_budget("acct", 40)
        self.queue.enqueue("batch", job_id="paid", paid=True, budget_account_id="acct", budget_limit=20, available_at=1)
        claimed = self.queue.claim("worker", lease_seconds=5, now=2)
        self.queue.start_call("paid", "uncertain", "worker", claimed.generation, now=3)
        self.assertEqual(self.queue.recover_expired(now=8), ("paid",))
        self.assertEqual(self.queue.reconcile_call("paid", "uncertain", "not_executed", now=9).status, "awaiting_confirmation")
        self.assertEqual(self.queue.confirm_recovery("paid", approved=True, now=10).status, "queued")

    def test_budget_reservation_settlement_idempotency_and_insufficient_funds(self) -> None:
        self.queue.fund_budget("acct", 100)
        job = self.queue.enqueue("interaction", job_id="budgeted", paid=True, budget_account_id="acct", budget_limit=60, available_at=1)
        self.assertEqual((job.budget_reserved, self.queue.budget("acct").available_units), (60, 40))
        claimed = self.queue.claim("worker", now=2)
        self.queue.start_call("budgeted", "call-1", "worker", claimed.generation, now=2)
        self.assertTrue(self.queue.record_call_cost("budgeted", "call-1", 17, "worker", claimed.generation, now=3))
        self.assertFalse(self.queue.record_call_cost("budgeted", "call-1", 17, "worker", claimed.generation, now=4))
        self.assertEqual(self.queue.call_costs("budgeted"), {"call-1": 17})
        self.queue.finish_call("budgeted", "call-1", "worker", claimed.generation, success=False, now=4)
        self.assertEqual(self.queue.finish("budgeted", "worker", claimed.generation, success=False, now=5).status, "failed")
        self.assertEqual(self.queue.budget("acct").available_units, 83)
        self.assertEqual(self.queue.budget("acct").reserved_units, 0)
        self.queue.fund_budget("acct", 1)

        with self.assertRaises(InsufficientBudget):
            self.queue.enqueue("batch", job_id="too-large", budget_account_id="acct", budget_limit=85, available_at=10)
        exact = self.queue.enqueue("batch", job_id="exact", budget_account_id="acct", budget_limit=84, available_at=10)
        self.assertEqual(exact.budget_reserved, 84)
        with self.assertRaises(InsufficientBudget):
            self.queue.enqueue("batch", job_id="too-large-2", budget_account_id="acct", budget_limit=1, available_at=10)
        self.assertEqual(self.queue.budget("acct").available_units, 0)

    def test_cost_conflict_and_budget_overrun_roll_back_atomically(self) -> None:
        self.queue.fund_budget("acct", 30)
        self.queue.enqueue("grading", job_id="job", budget_account_id="acct", budget_limit=20, available_at=1)
        claimed = self.queue.claim("worker", now=2)
        self.queue.start_call("job", "call-1", "worker", claimed.generation, now=2)
        self.assertTrue(self.queue.record_call_cost("job", "call-1", 12, "worker", claimed.generation, now=3))
        with self.assertRaises(IdempotencyConflict):
            self.queue.record_call_cost("job", "call-1", 13, "worker", claimed.generation, now=4)
        self.queue.finish_call("job", "call-1", "worker", claimed.generation, success=True, result={"part": 1}, cost_units=12, now=4)
        self.queue.start_call("job", "call-2", "worker", claimed.generation, reserve_units=8, now=4)
        self.assertTrue(self.queue.record_call_cost("job", "call-2", 9, "worker", claimed.generation, now=5))
        self.assertEqual(self.queue.get_job("job").budget_spent, 21)
        self.assertTrue(self.queue.get_job("job").notification_required)
        self.assertEqual(self.queue.call_costs("job"), {"call-1": 12, "call-2": 9})
        self.queue.finish_call("job", "call-2", "worker", claimed.generation, success=True, cost_units=9, now=5)
        with self.assertRaises(InsufficientBudget):
            self.queue.start_call("job", "call-3", "worker", claimed.generation, reserve_units=1, now=6)

    def test_retry_cancel_and_confirmation_rules(self) -> None:
        self.queue.fund_budget("acct", 50)
        paid = self.queue.enqueue("batch", job_id="paid", paid=True, budget_account_id="acct", budget_limit=20, available_at=1)
        claimed = self.queue.claim("worker", now=2)
        failed = self.queue.finish("paid", "worker", claimed.generation, success=False, retryable=True, error="temporary", now=3)
        self.assertEqual(failed.status, "awaiting_confirmation")
        self.assertEqual(self.queue.confirm_recovery("paid", approved=True, now=4).status, "queued")
        claim = self.queue.claim("worker-2", now=5)
        self.assertEqual(self.queue.cancel("paid", now=6).status, "running")
        self.assertTrue(self.queue.get_job("paid").cancel_requested)
        self.assertEqual(self.queue.finish("paid", "worker-2", claim.generation, success=False, now=7).status, "cancelled")
        self.assertEqual(self.queue.budget("acct").reserved_units, 0)

        grading = self.queue.enqueue("grading", job_id="grading", available_at=8)
        claim = self.queue.claim("grader", now=8)
        self.assertEqual(self.queue.finish("grading", "grader", claim.generation, success=False, retryable=True, now=9).status, "queued")
        self.assertEqual(self.queue.cancel("grading", now=10).status, "cancelled")
        self.assertEqual(self.queue.retry("grading", now=11).status, "queued")
        self.assertEqual(self.queue.cancel("grading", now=12).status, "cancelled")

    def test_wal_foreign_keys_and_input_validation(self) -> None:
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            db.close()
        with self.assertRaises(ValueError):
            self.queue.enqueue("unknown")
        with self.assertRaises(ValueError):
            self.queue.fund_budget("acct", True)
        with self.assertRaises(ValueError):
            self.queue.enqueue("batch", payload={"bad": float("nan")})


    def test_versioned_schema_reopen_and_all_connections_enable_foreign_keys(self) -> None:
        with closing(self.queue._connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], JobQueue.APPLICATION_ID)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO steps(job_id,name,position,status) VALUES('missing','x',0,'pending')")
        self.assertEqual(JobQueue(self.database).list_jobs(), ())

    def test_foreign_newer_tampered_and_corrupt_databases_are_read_only_refused(self) -> None:
        variants = ("foreign", "newer", "tampered", "wrong-marker", "legacy-trigger", "corrupt")
        for variant in variants:
            with self.subTest(variant=variant):
                path = self.database.parent / f"{variant}.sqlite3"
                if variant == "corrupt":
                    path.write_bytes(b"not sqlite data")
                elif variant == "legacy-trigger":
                    with closing(sqlite3.connect(path)) as db:
                        for sql in _LEGACY_SCHEMA:
                            db.execute(sql)
                        db.execute("INSERT INTO queue_meta VALUES(1,0)")
                        db.execute("CREATE TRIGGER planted AFTER INSERT ON events BEGIN DELETE FROM steps; END")
                        db.commit()
                elif variant == "foreign":
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("CREATE TABLE budgets(account_id TEXT)")
                        db.commit()
                else:
                    JobQueue(path)
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        db.execute("PRAGMA journal_mode=DELETE")
                        if variant == "newer":
                            db.execute("PRAGMA user_version=999")
                        elif variant == "tampered":
                            db.execute("ALTER TABLE upstream_calls ADD COLUMN injected TEXT")
                        else:
                            db.execute("PRAGMA application_id=7")
                        db.commit()
                before = path.read_bytes()
                siblings_before = {item.name: item.read_bytes() for item in path.parent.iterdir() if item.name.startswith(path.name)}
                with self.assertRaises(QueueSchemaError):
                    JobQueue(path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual({item.name: item.read_bytes() for item in path.parent.iterdir() if item.name.startswith(path.name)}, siblings_before)

    def test_exact_unversioned_legacy_queue_migrates_once_with_pending_usage(self) -> None:
        path = self.database.parent / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            for sql in _LEGACY_SCHEMA:
                db.execute(sql)
            db.execute("INSERT INTO queue_meta VALUES(1,1)")
            db.execute("INSERT INTO budgets VALUES('wallet',40,60)")
            db.execute("""INSERT INTO jobs(job_id,queue_order,kind,payload_json,status,created_at,updated_at,available_at,
                max_attempts,paid,budget_account_id,budget_limit,budget_reserved,budget_spent,reservation_active,generation)
                VALUES('old',1,'grading','{}','failed',1,1,1,3,1,'wallet',60,60,17,1,1)""")
            db.execute("INSERT INTO steps(job_id,name,position,status) VALUES('old','grade',0,'pending')")
            db.execute("INSERT INTO events(job_id,event_type,created_at,data_json) VALUES('old','legacy',1,'{}')")
            db.execute("INSERT INTO upstream_calls VALUES('settled','old','grade','failed',1,NULL,1,1)")
            db.execute("INSERT INTO upstream_calls VALUES('pending','old','grade','failed',1,NULL,2,2)")
            db.execute("INSERT INTO call_charges VALUES('settled','old','wallet',17,1)")
            db.commit()
        migrated = JobQueue(path)
        self.assertEqual(migrated.get_job("old").budget_reserved, 43)
        self.assertEqual(migrated.budget("wallet").reserved_units, 43)
        self.assertEqual(migrated.call_costs("old"), {"settled": 17})
        self.assertTrue(migrated.get_call("pending").cost_pending)
        self.assertEqual(migrated.get_call("pending").reserved_units, 43)
        self.assertFalse(migrated.get_call("settled").cost_pending)
        self.assertEqual(JobQueue(path).get_job("old"), migrated.get_job("old"))
        self.assertTrue(migrated.settle_call_cost("pending", 3, now=4))
        self.assertEqual(migrated.budget("wallet").available_units, 80)
        self.assertEqual(migrated.budget("wallet").reserved_units, 0)

    def test_missing_usage_keeps_call_reservation_after_finish_until_late_settlement(self) -> None:
        self.queue.fund_budget("acct", 100, transaction_id="deposit")
        self.queue.enqueue("batch", job_id="pending", paid=True, budget_account_id="acct", budget_limit=60, available_at=1)
        job = self.queue.claim("worker", now=2)
        self.queue.start_call(job.job_id, "pending-call", "worker", job.generation, reserve_units=30, now=3)
        self.queue.finish_call(job.job_id, "pending-call", "worker", job.generation, success=True, result={"text": "done"}, now=4)
        finished = self.queue.finish(job.job_id, "worker", job.generation, success=True, now=5)
        self.assertTrue(finished.cost_pending)
        self.assertEqual(finished.budget_reserved, 30)
        self.assertEqual((self.queue.budget("acct").available_units, self.queue.budget("acct").reserved_units), (70, 30))
        before_result = self.queue.get_call("pending-call").result
        settled = self.queue.record_call_usage(job.job_id, "pending-call", usage={"input_tokens": 8, "output_tokens": 2}, cost_units=17, cost_pending=False, now=6)
        self.assertFalse(settled.cost_pending)
        self.assertTrue(settled.late_cost)
        self.assertEqual(settled.result, before_result)
        self.assertEqual(self.queue.get_job(job.job_id).status, "succeeded")
        self.assertEqual(self.queue.budget("acct").available_units, 83)
        self.assertEqual(self.queue.budget("acct").reserved_units, 0)
        self.assertFalse(self.queue.settle_call_cost("pending-call", 17, usage={"input_tokens": 8}, now=7))
        with self.assertRaises(IdempotencyConflict):
            self.queue.settle_call_cost("pending-call", 18, now=7)
        with self.assertRaises(LookupError):
            self.queue.record_call_usage(job.job_id, "not-a-call", cost_pending=False, now=8)

    def test_lost_lease_late_cost_cannot_adopt_result_or_clear_unknown_state(self) -> None:
        self.queue.fund_budget("acct", 50)
        self.queue.enqueue("grading", job_id="lost", budget_account_id="acct", budget_limit=20, steps=("grade",), available_at=1)
        job = self.queue.claim("old", lease_seconds=5, now=2)
        self.queue.start_step(job.job_id, "grade", "old", job.generation, now=2)
        self.queue.start_call(job.job_id, "lost-call", "old", job.generation, step_name="grade", reserve_units=20, now=3)
        self.queue.recover_expired(now=8)
        with self.assertRaises(LeaseLost):
            self.queue.finish_call(job.job_id, "lost-call", "old", job.generation, success=True, result={"score": 10}, cost_units=7, now=9)
        self.assertTrue(self.queue.record_call_cost(job.job_id, "lost-call", 7, "old", job.generation, now=9))
        call = self.queue.get_call("lost-call")
        self.assertEqual(call.status, "unknown")
        self.assertIsNone(call.result)
        self.assertFalse(call.cost_pending)
        self.assertEqual(self.queue.get_job(job.job_id).status, "reconciling")
        with self.assertRaises(InvalidTransition):
            self.queue.retry(job.job_id, now=9)
        with self.assertRaises(IdempotencyConflict):
            self.queue.reconcile_call(job.job_id, "lost-call", "not_executed", now=9)
        self.queue.reconcile_call(job.job_id, "lost-call", "succeeded", result={"score": 7}, cost_units=7, now=10)
        self.assertFalse(self.queue.get_call("lost-call").cost_pending)
        recovered = self.queue.claim("new", now=11)
        self.queue.start_step(job.job_id, "grade", "new", recovered.generation, now=11)
        previous = self.queue.list_calls(job.job_id, step_name="grade")[-1]
        self.assertEqual(previous.result, {"score": 7})
        self.queue.finish_step(job.job_id, "grade", "new", recovered.generation, success=True, output=previous.result, now=12)
        self.queue.finish(job.job_id, "new", recovered.generation, success=True, now=12)
        self.assertEqual(self.queue.budget("acct").available_units, 43)

    def test_cancelled_worker_can_record_cost_but_cannot_accept_business_output(self) -> None:
        self.queue.fund_budget("acct", 50)
        self.queue.enqueue("batch", job_id="cancel", paid=True, budget_account_id="acct", budget_limit=20, steps=("run",), available_at=1)
        job = self.queue.claim("worker", lease_seconds=5, now=2)
        self.queue.start_step(job.job_id, "run", "worker", job.generation, now=2)
        self.queue.start_call(job.job_id, "cancel-call", "worker", job.generation, step_name="run", reserve_units=20, now=3)
        self.queue.cancel(job.job_id, now=4)
        with self.assertRaises(InvalidTransition):
            self.queue.finish_call(job.job_id, "cancel-call", "worker", job.generation, success=True, result={"published": True}, now=4)
        self.queue.record_call_usage(job.job_id, "cancel-call", usage={"input_tokens": 3}, cost_units=5, now=4)
        self.queue.recover_expired(now=8)
        self.queue.reconcile_call(job.job_id, "cancel-call", "failed", cost_units=5, now=9)
        self.assertEqual(self.queue.get_job(job.job_id).status, "cancelled")
        self.assertIsNone(self.queue.get_call("cancel-call").result)
        self.assertIsNone(self.queue.list_steps(job.job_id)[0].output)
        self.assertEqual(self.queue.budget("acct").available_units, 45)

    def test_new_call_id_cannot_bypass_unknown_or_unsettled_usage(self) -> None:
        self.queue.enqueue("grading", job_id="blocked", available_at=1)
        job = self.queue.claim("worker", now=2)
        self.queue.start_call(job.job_id, "one", "worker", job.generation, now=2)
        with self.assertRaises(ReconciliationRequired):
            self.queue.start_call(job.job_id, "two", "worker", job.generation, now=3)
        self.queue.record_call_usage(job.job_id, "one", usage={"input_tokens": 1}, cost_pending=True, now=3)
        self.queue.finish_call(job.job_id, "one", "worker", job.generation, success=True, now=4)
        with self.assertRaises(ReconciliationRequired):
            self.queue.start_call(job.job_id, "two", "worker", job.generation, now=5)
        with self.assertRaises(ValueError):
            self.queue.record_call_usage(job.job_id, "one", cost_pending=False, now=5)
        self.queue.record_call_usage(job.job_id, "one", cost_units=0, cost_pending=False, now=6)
        self.assertTrue(self.queue.start_call(job.job_id, "two", "worker", job.generation, now=6).should_send)

    def test_call_metadata_versions_events_and_restart_result_reuse(self) -> None:
        self.queue.enqueue("grading", job_id="versions", steps=("grade",), model_name="terra", model_version="m1", price_version="p1", input_version="input-3", available_at=1)
        job = self.queue.claim("old", lease_seconds=5, now=2)
        self.queue.start_step(job.job_id, "grade", "old", job.generation, now=2)
        metadata = {"purpose": "grading", "price": {"input": 5}, "input_revision": "r3"}
        self.queue.start_call(job.job_id, "versioned-call", "old", job.generation, step_name="grade", reserve_units=0, metadata=metadata, now=3)
        metadata["price"]["input"] = 999
        self.queue.finish_call(job.job_id, "versioned-call", "old", job.generation, success=True, result={"score": 4}, usage={"input_tokens": 1}, cost_units=0, now=4)
        restarted = JobQueue(self.database)
        call = restarted.get_call("versioned-call")
        self.assertEqual((call.model_name, call.model_version, call.price_version, call.input_version), ("terra", "m1", "p1", "input-3"))
        self.assertEqual(call.metadata["price"]["input"], 5)
        self.assertEqual(restarted.list_steps(job.job_id)[0].status, "running")
        restarted.recover_expired(now=8)
        new = restarted.claim("new", now=9)
        self.assertGreater(new.generation, job.generation)
        calls = restarted.list_calls(job.job_id, step_name="grade")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].result, {"score": 4})
        restarted.start_step(job.job_id, "grade", "new", new.generation, now=9)
        replay = restarted.start_call(job.job_id, "versioned-call", "new", new.generation, step_name="grade", now=9)
        self.assertFalse(replay.should_send)
        with self.assertRaises(IdempotencyConflict):
            restarted.start_call(job.job_id, "versioned-call", "new", new.generation, step_name="grade", input_version="different", now=9)
        event = restarted.append_event(job.job_id, "progress", {"percent": 50}, worker_id="new", generation=new.generation, now=9)
        self.assertEqual(restarted.get_events(event.event_id - 1, job_id=job.job_id, limit=1), (event,))
        self.assertEqual(restarted.list_jobs(status="running", kind="grading")[0].job_id, job.job_id)
        with self.assertRaises(LeaseLost):
            restarted.append_event(job.job_id, "stale", {}, worker_id="old", generation=job.generation, now=9)
        restarted.finish_step(job.job_id, "grade", "new", new.generation, success=True, output=replay.result, now=10)
        restarted.finish(job.job_id, "new", new.generation, success=True, now=10)

    def test_funding_is_idempotent_under_concurrency_and_conflicts_roll_back(self) -> None:
        barrier = Barrier(6)
        def credit(index: int) -> None:
            barrier.wait(timeout=5)
            JobQueue(self.database).fund_budget("acct", 20, transaction_id="same-credit")
        with ThreadPoolExecutor(max_workers=6) as pool:
            tuple(pool.map(credit, range(6)))
        self.assertEqual(self.queue.budget("acct").available_units, 20)
        with self.assertRaises(IdempotencyConflict):
            self.queue.fund_budget("acct", 21, transaction_id="same-credit")
        with self.assertRaises(IdempotencyConflict):
            self.queue.fund_budget("other", 20, transaction_id="same-credit")
        self.assertEqual(self.queue.budget("other").available_units, 0)

    def test_scoped_student_daily_exhaustion_does_not_charge_necessary_teaching(self) -> None:
        ledger = self.queue.budget_ledger
        ledger.assign("student_daily", "student", 5, period="2026-11-11", course_id="course", class_id="class")
        ledger.assign("teaching", "course", 20)
        ledger.assign("project", "project", 100)
        self.queue.enqueue("interaction", job_id="chat", paid=True, purpose="chat", student_id="student", course_id="course", class_id="class", project_id="project", budget_day="2026-11-11", available_at=1)
        chat = self.queue.claim("worker", now=2)
        self.queue.start_call(chat.job_id, "chat-call", "worker", chat.generation, reserve_units=5, now=2)
        self.queue.finish_call(chat.job_id, "chat-call", "worker", chat.generation, success=True, cost_units=5, now=3)
        self.queue.finish(chat.job_id, "worker", chat.generation, success=True, now=3)
        self.queue.enqueue("interaction", job_id="chat-again", paid=True, purpose="chat", student_id="student", course_id="course", class_id="class", project_id="project", budget_day="2026-11-11", available_at=4)
        again = self.queue.claim("worker", now=4)
        with self.assertRaises(InsufficientBudget):
            self.queue.start_call(again.job_id, "denied", "worker", again.generation, reserve_units=1, now=4)
        self.queue.finish(again.job_id, "worker", again.generation, success=False, now=4)
        self.queue.enqueue("grading", job_id="grade", paid=True, purpose="grading", student_id="student", course_id="course", class_id="class", project_id="project", budget_day="2026-11-11", available_at=5)
        grade = self.queue.claim("worker", now=5)
        self.queue.start_call(grade.job_id, "grade-call", "worker", grade.generation, reserve_units=7, now=5)
        self.queue.finish_call(grade.job_id, "grade-call", "worker", grade.generation, success=True, cost_units=6, now=6)
        self.queue.finish(grade.job_id, "worker", grade.generation, success=True, now=6)
        self.assertEqual(ledger.query("student_daily", "student", period="2026-11-11", course_id="course", class_id="class").spent_units, 5)
        self.assertEqual(ledger.query("teaching", "course").spent_units, 6)
        self.assertEqual(ledger.query("project", "project").spent_units, 11)

    def test_necessary_emergency_overage_and_unexpected_invoice_are_never_dropped(self) -> None:
        ledger = self.queue.budget_ledger
        ledger.assign("teaching", "course", 5, emergency_units=10)
        ledger.assign("project", "project", 5, emergency_units=10)
        self.queue.enqueue("batch", job_id="summary", paid=True, purpose="summary", course_id="course", project_id="project", available_at=1)
        job = self.queue.claim("worker", now=2)
        self.queue.start_call(job.job_id, "summary-1", "worker", job.generation, reserve_units=7, now=2)
        self.queue.finish_call(job.job_id, "summary-1", "worker", job.generation, success=True, cost_units=8, now=3)
        self.assertTrue(self.queue.get_job(job.job_id).notification_required)
        self.assertEqual(ledger.query("project", "project").overage_units, 3)
        self.queue.start_call(job.job_id, "summary-2", "worker", job.generation, reserve_units=7, now=4)
        # The provider's actual cost is a fact, even when it breaches the cap.
        self.queue.finish_call(job.job_id, "summary-2", "worker", job.generation, success=True, cost_units=12, now=5)
        self.assertEqual(self.queue.get_job(job.job_id).budget_spent, 20)
        self.assertEqual(ledger.query("project", "project").emergency_exceeded_units, 5)
        self.assertTrue(ledger.query("project", "project").notification_required)
        with self.assertRaises(InsufficientBudget):
            self.queue.start_call(job.job_id, "summary-3", "worker", job.generation, reserve_units=1, now=6)
        self.assertEqual(self.queue.call_costs(job.job_id), {"summary-1": 8, "summary-2": 12})

    def test_optional_estimate_overrun_stops_followup_and_wallet_debt_is_visible(self) -> None:
        self.queue.fund_budget("wallet", 10)
        self.queue.enqueue("interaction", job_id="optional", paid=True, purpose="chat", budget_account_id="wallet", budget_limit=10, available_at=1)
        job = self.queue.claim("worker", now=2)
        self.queue.start_call(job.job_id, "optional-call", "worker", job.generation, reserve_units=10, now=2)
        self.queue.finish_call(job.job_id, "optional-call", "worker", job.generation, success=True, cost_units=17, now=3)
        self.assertEqual(self.queue.budget("wallet").overdraft_units, 7)
        self.assertEqual(self.queue.get_job(job.job_id).budget_overrun_units, 7)
        with self.assertRaises(InsufficientBudget):
            self.queue.start_call(job.job_id, "optional-next", "worker", job.generation, reserve_units=1, now=4)
        self.queue.finish(job.job_id, "worker", job.generation, success=True, now=4)
        self.queue.fund_budget("wallet", 9, transaction_id="repay")
        self.assertEqual(self.queue.budget("wallet").overdraft_units, 0)
        self.assertEqual(self.queue.budget("wallet").available_units, 2)

    def test_concurrent_scoped_reservations_have_one_winner_and_rollback_loser(self) -> None:
        ledger = self.queue.budget_ledger
        ledger.assign("teaching", "course", 10)
        ledger.assign("project", "project", 10)
        for name in ("one", "two"):
            self.queue.enqueue("interaction", job_id=name, paid=True, purpose="prepare", course_id="course", project_id="project", available_at=1)
        jobs = (self.queue.claim("w1", now=2), self.queue.claim("w2", now=2))
        barrier = Barrier(2)
        def reserve(job):
            barrier.wait(timeout=5)
            try:
                JobQueue(self.database).start_call(job.job_id, f"call-{job.job_id}", job.lease_owner, job.generation, reserve_units=6, now=3)
                return True
            except InsufficientBudget:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(reserve, jobs))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(ledger.query("project", "project").reserved_units, 6)
        self.assertEqual(sum(len(self.queue.list_calls(job.job_id)) for job in jobs), 1)

    def test_daily_template_class_scope_day_boundary_and_late_charge_keep_original_day(self) -> None:
        ledger = self.queue.budget_ledger
        ledger.assign("student_daily", "student", 8, course_id="course", class_id="class-a")
        ledger.assign("student_daily", "student", 20)
        ledger.assign("project", "project", 100)
        before_midnight = datetime(2026, 11, 11, 23, 59, 58, tzinfo=timezone.utc).timestamp()
        after_midnight = before_midnight + 5
        self.queue.enqueue("interaction", job_id="late-day", paid=True, purpose="chat", student_id="student", course_id="course", class_id="class-a", project_id="project", available_at=1)
        job = self.queue.claim("worker", lease_seconds=20, now=before_midnight)
        self.queue.start_call(job.job_id, "day-call", "worker", job.generation, reserve_units=8, now=before_midnight)
        self.queue.finish_call(job.job_id, "day-call", "worker", job.generation, success=True, now=before_midnight + 1)
        self.queue.finish(job.job_id, "worker", job.generation, success=True, now=before_midnight + 1)
        self.queue.settle_call_cost("day-call", 6, now=after_midnight)
        self.assertEqual(self.queue.get_call("day-call").budget_day, "2026-11-11")
        self.assertEqual(ledger.query("student_daily", "student", period="2026-11-11", course_id="course", class_id="class-a").spent_units, 6)
        self.assertEqual(ledger.query("student_daily", "student", period="2026-11-12", course_id="course", class_id="class-a").spent_units, 0)
        self.assertEqual(ledger.query("student_daily", "student", period="2026-11-11").spent_units, 6)
        self.assertIsNone(ledger.query("student_daily", "student", period="2026-11-11", course_id="course", class_id="class-b"))

    def test_scoped_paid_send_requires_limits_but_local_objective_work_does_not(self) -> None:
        self.queue.enqueue("grading", job_id="local", paid=False, steps=("objective",), available_at=1)
        local = self.queue.claim("worker", now=2)
        self.queue.start_step(local.job_id, "objective", "worker", local.generation, now=2)
        self.queue.finish_step(local.job_id, "objective", "worker", local.generation, success=True, output={"score": 1}, now=3)
        self.queue.finish(local.job_id, "worker", local.generation, success=True, now=3)
        self.queue.enqueue("grading", job_id="paid-unfunded", paid=True, purpose="grading", available_at=4)
        paid = self.queue.claim("worker", now=4)
        with self.assertRaises(InsufficientBudget):
            self.queue.start_call(paid.job_id, "no-limits", "worker", paid.generation, reserve_units=1, now=4)
        self.assertEqual(self.queue.list_calls(paid.job_id), ())
        self.assertEqual(self.queue.budget_ledger.entries(), ())

    def test_concurrent_late_settlement_and_usage_conflict_charge_only_once(self) -> None:
        self.queue.fund_budget("wallet", 100)
        self.queue.enqueue("batch", job_id="settlement", budget_account_id="wallet", budget_limit=20, available_at=1)
        job = self.queue.claim("worker", now=2)
        self.queue.start_call(job.job_id, "settlement-call", "worker", job.generation, reserve_units=20, now=2)
        self.queue.finish_call(job.job_id, "settlement-call", "worker", job.generation, success=True, now=3)
        self.queue.finish(job.job_id, "worker", job.generation, success=True, now=3)
        barrier = Barrier(6)
        def settle(index: int):
            barrier.wait(timeout=5)
            return self.queue.settle_call_cost("settlement-call", 9, usage={"input_tokens": 4}, now=4)
        with ThreadPoolExecutor(max_workers=6) as pool:
            charged = tuple(pool.map(settle, range(6)))
        self.assertEqual(sum(charged), 1)
        self.assertEqual(self.queue.get_job(job.job_id).budget_spent, 9)
        self.assertEqual(self.queue.budget("wallet").available_units, 91)
        with self.assertRaises(IdempotencyConflict):
            self.queue.record_call_usage(job.job_id, "settlement-call", usage={"input_tokens": 5}, cost_units=9, now=5)
        self.assertEqual(self.queue.get_call("settlement-call").usage, {"input_tokens": 4})

    def test_cancelled_expired_work_does_not_silently_requeue_and_summary_requires_confirmation(self) -> None:
        self.queue.enqueue("grading", job_id="cancel-safe", available_at=1)
        job = self.queue.claim("worker", lease_seconds=2, now=2)
        self.queue.cancel(job.job_id, now=3)
        self.queue.recover_expired(now=5)
        self.assertEqual(self.queue.get_job(job.job_id).status, "cancelled")
        self.queue.fund_budget("wallet", 20)
        self.queue.enqueue("grading", job_id="summary-priority", purpose="summary", paid=True, budget_account_id="wallet", budget_limit=10, available_at=6)
        summary = self.queue.claim("worker", lease_seconds=2, now=6)
        self.queue.recover_expired(now=9)
        self.assertEqual(self.queue.get_job(summary.job_id).status, "awaiting_confirmation")


if __name__ == "__main__":
    unittest.main()
