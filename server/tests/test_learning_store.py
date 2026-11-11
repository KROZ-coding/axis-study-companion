"""Focused transactional, authorization, and answer-gate tests for LearningStore."""

from contextlib import closing
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

from app.learning_store import (
    IdempotencyConflict,
    InvalidTransition,
    LeaseLost,
    LearningStore,
    TaskNotFound,
    VersionConflict,
)


class LearningStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="learning-store-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "learning.sqlite3"
        self.store = LearningStore(self.database)
        self._seed()

    def _seed(self) -> None:
        self.store.create_goal("teacher-1", "Fractions", goal_id="goal-1", course_id="course-1", class_id="class-1")
        self.store.create_round("teacher-1", "goal-1", 1, round_id="round-1", deadline_at=500, allow_late=False)
        self.store.create_question_version("teacher-1", "q1", 1, {
            "type": "single_choice", "stem": "1 + 1 = ?", "options": ["1", "2"],
            "answer": 1, "explanation": "Add one and one.", "points": 2,
        }, status="approved")
        self.store.create_question_version("teacher-1", "q2", 3, {
            "type": "fill", "stem": "Type the word", "options": [], "answer": "yes",
            "explanation": "The expected word is yes.", "points": 1,
        }, status="approved")
        self.task = self.store.create_task(
            "teacher-1", "student-1", "goal-1", "round-1",
            [{"question_id": "q1", "version": 1}, {"question_id": "q2", "version": 3}],
            task_id="task-1",
        )

    def _lease(self, device="device-1", *, now=100, takeover=False):
        return self.store.acquire_draft_lease(
            "task-1", "student-1", device, lease_seconds=60, now=now, takeover=takeover,
        )

    def _save_complete_draft(self, *, request_id="draft-1", now=100):
        lease = self._lease(now=now)
        task = self.store.save_draft(
            "task-1", "student-1", {"q1": 1, "q2": "yes"},
            expected_version=lease["task_version"], request_id=request_id,
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=now + 1,
        )
        return lease, task

    def _submit(self, *, request_id="submit-1", now=104):
        lease, task = self._save_complete_draft(now=now - 3)
        submitted = self.store.submit_attempt(
            "task-1", "student-1", expected_version=task["version"], request_id=request_id,
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=now,
        )
        return lease, submitted

    def _dispatch(self, attempt_id):
        outbox = self.store.list_grading_outbox(status="pending")[0]
        self.assertEqual(outbox["attempt_id"], attempt_id)
        return self.store.mark_grading_outbox(
            outbox["job_id"], "dispatched", queue_job_id=outbox["job_id"], fence_token=1,
        )

    def test_database_is_isolated_versioned_wal_and_rejects_unversioned_store(self):
        self.assertEqual(self.store.schema_version, LearningStore.SCHEMA_VERSION)
        self.assertEqual(self.store.journal_mode, "wal")
        self.assertEqual(self.database.name, "learning.sqlite3")
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("grading_outbox", tables)
        self.assertNotIn("users", tables)
        self.assertNotIn("jobs", tables)
        self.assertEqual(LearningStore(self.database).schema_version, LearningStore.SCHEMA_VERSION)

        foreign = Path(self.directory.name) / "not-learning.sqlite3"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(RuntimeError, "unversioned"):
            LearningStore(foreign)

    def test_question_versions_are_frozen_and_student_dto_never_leaks_answer_before_release(self):
        task = self.store.get_task("task-1", "student-1")
        self.assertFalse(task["answer_released"])
        self.assertEqual([q["version"] for q in task["questions"]], [1, 3])
        for question in task["questions"]:
            self.assertNotIn("answer", question)
            self.assertNotIn("explanation", question)
            self.assertNotIn("rubric", question)
        self.assertNotIn("answer", self.store.get_question("q1", 1, "student-1", task_id="task-1"))
        with self.assertRaises(TaskNotFound):
            self.store.get_task("task-1", "student-2")
        with self.assertRaises(TaskNotFound):
            self.store.get_question("q1", 1, "student-2", task_id="task-1")
        teacher_question = self.store.get_question("q1", 1, "teacher-1")
        self.assertEqual(teacher_question["answer"], 1)

    def test_submit_requires_all_answers_and_failed_request_rolls_back_idempotency(self):
        lease = self._lease(now=100)
        draft = self.store.save_draft(
            "task-1", "student-1", {"q1": 1}, expected_version=lease["task_version"],
            request_id="partial", lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=101,
        )
        with self.assertRaisesRegex(ValueError, "all task questions"):
            self.store.submit_attempt(
                "task-1", "student-1", expected_version=draft["version"], request_id="submit-partial",
                lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=102,
            )
        self.assertEqual(self.store.get_task("task-1", "student-1")["attempts"], [])
        self.assertEqual(self.store.list_grading_outbox(), [])
        full = self.store.save_draft(
            "task-1", "student-1", {"q1": 1, "q2": "yes"}, expected_version=draft["version"],
            request_id="full", lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=103,
        )
        submitted = self.store.submit_attempt(
            "task-1", "student-1", expected_version=full["version"], request_id="submit-partial",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=104,
        )
        self.assertEqual(submitted["status"], "grading")
        self.assertEqual(len(submitted["attempts"]), 1)
        self.assertEqual(len(self.store.list_grading_outbox()), 1)

    def test_submit_idempotency_conflict_replay_and_transaction_rollback(self):
        lease, submitted = self._submit()
        attempt_id = submitted["attempts"][0]["id"]
        job_id = submitted["grading_job_id"]
        replay = self.store.submit_attempt(
                "task-1", "student-1", expected_version=lease["task_version"], request_id="submit-1",
                lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=105,
        )
        self.assertEqual([a["id"] for a in replay["attempts"]], [attempt_id])
        self.assertEqual(self.store.list_grading_outbox()[0]["job_id"], job_id)
        with self.assertRaises(IdempotencyConflict):
            self.store.submit_attempt(
            "task-1", "student-1", expected_version=lease["task_version"] + 1, request_id="submit-1",
            lease_token=lease["lease_token"], fence_token=2, now=105,
            )
        before = self.store.get_task("task-1", "student-1")
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.mark_grading_outbox(job_id, "dispatched", fence_token=1)
                raise RuntimeError("abort")
        self.assertEqual(self.store.list_grading_outbox()[0]["status"], "pending")
        self.assertEqual(self.store.get_task("task-1", "student-1")["version"], before["version"])

    def test_stale_version_and_fenced_device_cannot_overwrite_draft(self):
        first = self._lease(now=100)
        second = self._lease("device-2", now=101, takeover=True)
        with self.assertRaises(LeaseLost):
            self.store.save_draft(
                "task-1", "student-1", {"q1": 0}, expected_version=second["task_version"],
                request_id="stale", lease_token=first["lease_token"], fence_token=first["fence_token"], now=102,
            )
        saved = self.store.save_draft(
            "task-1", "student-1", {"q1": 0}, expected_version=second["task_version"],
            request_id="current", lease_token=second["lease_token"], fence_token=second["fence_token"], now=102,
        )
        with self.assertRaises(VersionConflict):
            self.store.save_draft(
                "task-1", "student-1", {"q1": 1}, expected_version=second["task_version"],
                request_id="stale-version", lease_token=second["lease_token"], fence_token=second["fence_token"], now=103,
            )
        self.assertEqual(saved["draft_answers"], {"q1": 0})

    def test_answer_validation_and_foreign_key_constraints(self):
        lease = self._lease(now=100)
        for invalid in ({"q1": True}, {"q1": 2}, {"q1": "1"}, {"not-task-question": 0}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.save_draft(
                    "task-1", "student-1", invalid, expected_version=lease["task_version"],
                    request_id="bad-" + str(len(invalid)) + str(type(next(iter(invalid.values())))),
                    lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=101,
                )
            self.assertEqual(self.store.get_task("task-1", "student-1")["draft_answers"], {})
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute("INSERT INTO draft_answers VALUES('task-1','missing','null',1)")

    def test_outbox_and_grading_input_persist_across_restart(self):
        _, submitted = self._submit()
        task_again = LearningStore(self.database).get_task("task-1", "student-1")
        attempt_id = task_again["attempts"][0]["id"]
        self.assertEqual(task_again["status"], "grading")
        outbox = self.store.list_grading_outbox()[0]
        self.assertEqual(outbox["job_id"], submitted["grading_job_id"])
        self.assertEqual(outbox["status"], "pending")
        data = self.store.get_grading_input(attempt_id)
        self.assertEqual(data["questions"][0]["answer"], 1)
        self.assertEqual(data["answers"], {"q1": 1, "q2": "yes"})

    def test_answer_gate_opens_only_when_all_items_graded_and_partial_failure_manual(self):
        _, submitted = self._submit()
        attempt_id = submitted["attempts"][0]["id"]
        outbox = self._dispatch(attempt_id)
        job_id = outbox["job_id"]
        self.store.apply_grading_result(
            attempt_id, "q1", {"score": 2, "feedback": "Correct"},
            expected_revision=1, job_id=job_id, fence_token=1,
        )
        student_view = self.store.get_task("task-1", "student-1")
        self.assertFalse(student_view["answer_released"])
        self.assertNotIn("answer", student_view["questions"][0])
        self.assertEqual(student_view["attempts"][0]["status"], "submitted")
        with self.assertRaises(LeaseLost):
            self.store.apply_grading_result(
                attempt_id, "q2", {"score": 1}, expected_revision=1, job_id=job_id, fence_token=2,
            )
        self.store.mark_grading_outbox(job_id, "dispatched", fence_token=1)
        manual = self.store.fail_grading(attempt_id, job_id=job_id, fence_token=1, reason="provider unavailable")
        self.assertEqual(manual["status"], "awaiting_manual")
        self.assertFalse(manual["answer_released"])
        self.assertNotIn("answer", manual["questions"][0])
        self.assertEqual(self.store.list_grading_outbox(status="all")[0]["status"], "manual")

    def test_complete_grading_releases_results_correction_adds_revision_and_report_revision(self):
        _, submitted = self._submit()
        attempt_id = submitted["attempts"][0]["id"]
        outbox = self._dispatch(attempt_id)
        result = self.store.apply_grading_result(
            attempt_id, "q1", {"score": 2, "feedback": "Correct"}, expected_revision=1,
            job_id=outbox["job_id"], fence_token=1,
        )
        self.assertFalse(result["answer_released"])
        result = self.store.apply_grading_result(
            attempt_id, "q2", {"score": 1, "feedback": "Correct"}, expected_revision=1,
            job_id=outbox["job_id"], fence_token=1,
        )
        self.assertTrue(result["answer_released"])
        self.assertEqual(result["status"], "results_ready")
        self.assertEqual(result["attempts"][0]["total_score"], 3)
        self.assertEqual(result["questions"][0]["answer"], 1)
        self.assertEqual(self.store.list_grading_outbox(status="all")[0]["status"], "completed")
        correction = self.store.apply_correction(
            "teacher-1", attempt_id, "q1", score=1, reason="rubric correction", expected_revision=1,
        )
        self.assertEqual(correction["grading_revision"], 2)
        self.assertEqual(correction["total_score"], 2)
        self.assertEqual(len(self.store.list_corrections(attempt_id, "student-1")), 1)
        report_input = self.store.report_input("goal-1", "teacher-1")
        self.assertTrue(report_input["input_digest"])
        saved_report = self.store.save_report_revision(
            "goal-1", "teacher-1", {"summary": "checked"}, expected_revision=0,
        )
        self.assertEqual(saved_report["revision"], 1)
        self.assertEqual(len(self.store.list_report_revisions("goal-1", "teacher-1")), 1)

    def test_retry_preserves_first_score_and_exposed_answer_means_retry_not_independent(self):
        _, first = self._submit()
        attempt_id = first["attempts"][0]["id"]
        outbox = self._dispatch(attempt_id)
        self.store.apply_grading_result(attempt_id, "q1", {"score": 2}, expected_revision=1,
                                        job_id=outbox["job_id"], fence_token=1)
        ready = self.store.apply_grading_result(attempt_id, "q2", {"score": 1}, expected_revision=1,
                                                job_id=outbox["job_id"], fence_token=1)
        self.assertEqual(ready["attempts"][0]["total_score"], 3)
        retry_task = self.store.start_retry(
            "task-1", "student-1", expected_version=ready["version"], request_id="retry-1",
        )
        self.assertEqual(retry_task["status"], "not_started")
        self.assertEqual(retry_task["attempts"][0]["total_score"], 3)
        self.store.record_exposure("task-1", "student-1", "q1", source="review")
        lease = self._lease(now=200)
        saved = self.store.save_draft(
            "task-1", "student-1", {"q1": 0, "q2": "yes"}, expected_version=lease["task_version"],
            request_id="retry-draft", lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=201,
        )
        retry = self.store.submit_attempt(
            "task-1", "student-1", expected_version=saved["version"], request_id="retry-submit",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=202,
        )
        self.assertEqual(retry["attempts"][0]["total_score"], 3)
        self.assertEqual(retry["attempts"][1]["kind"], "retry")
        self.assertFalse(retry["attempts"][1]["independent_evidence"])
        self.assertFalse(self.store.get_task("task-1", "student-1")["answer_released"])

    def test_late_submission_requires_policy_and_extension_is_audited(self):
        lease, task = self._save_complete_draft(now=493)
        with self.assertRaises(InvalidTransition):
            self.store.submit_attempt(
                "task-1", "student-1", expected_version=task["version"], request_id="late-denied",
                lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=501,
            )
        current = self.store.get_task("task-1", "teacher-1")
        extended = self.store.extend_deadline(
            "teacher-1", "task-1", 600, "school closure", expected_version=current["version"],
        )
        self.assertEqual(extended["deadline_at"], 600)
        submitted = self.store.submit_attempt(
            "task-1", "student-1", expected_version=extended["version"], request_id="late-accepted",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=501,
        )
        self.assertFalse(submitted["attempts"][0]["late"])

    def test_goal_scope_and_report_authorization_are_checked_in_store(self):
        with self.assertRaises(TaskNotFound):
            self.store.get_task_scope("task-1", "stranger")
        with self.assertRaises(Exception):
            self.store.get_goal_scope("goal-1", "stranger")
        with self.assertRaises(Exception):
            self.store.report_input("goal-1", "stranger")
        self.assertEqual(self.store.get_task_scope("task-1", "teacher-1")["student_id"], "student-1")


if __name__ == "__main__":
    unittest.main()
