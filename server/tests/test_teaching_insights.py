"""Disposable SQLite teacher-insight/next-round tests; no network or live models.

Run only this file through tests/run_tests.py's isolated bootstrap (discovery
pattern can be supplied by the invoking test harness without changing that file).
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

# Existing fixture sets SKIP_ENV_FILE before application imports and installs
# disposable stores, network denial and a gateway that forbids provider calls.
from test_teacher_planning import PlanningFixture
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.learning_store import IdempotencyConflict
from app.report_store import ReportStore, canonical_json
from app.routers.v1_teaching_insights import router
from app.schemas.v1_teaching_insights import BoundedPlanConfirm, NextRoundPublish
from app.services.report_exports import build_report_snapshot
from app.services.teaching_insights import EXPLANATIONS, TeachingInsights, teaching_insight_access, teaching_insight_job_access
from app.services.teacher_planning import PlanningConflict
from app.teaching_insight_store import InsightConflict, TeachingInsightStore


def valid_selection(model_input):
    facts = model_input["facts"]["students"]
    statuses = {fact["status"] for fact in facts}
    summary = "needs_more_evidence" if statuses == {"insufficient_evidence"} else "mixed_goal_evidence" if len(statuses) > 1 else "frozen_goal_evidence"
    return {"schema_version": "teaching.insight.selection.v1", "summary_code": summary,
            "evidence_refs": ["report:scope", "report:rule", "report:students"],
            "findings": [{**{key: fact[key] for key in ("student_code", "status", "percent", "independent_families")},
                          "evidence_refs": [f"mastery:{fact['student_code']}"],
                          "explanation_code": EXPLANATIONS[fact["status"]][0]} for fact in facts],
            "limitations": model_input["limitations"]}


class InsightFixture(PlanningFixture):
    def setUp(self):
        super().setUp()
        self.platform.create_user("insight_admin", "Administrator", "unused", "admin", user_id="admin")
        self.insights = TeachingInsightStore(self.runtime.reports)
        self.runtime.install("insights", self.insights)
        self.service = TeachingInsights(self.runtime)
        for kind in ("single_choice", "fill", "judge"):
            for letter in "ABCDEFGHIJKL":
                qid = kind + "-" + letter
                self.question(qid, kind=kind, family_id=qid, difficulty="medium")
        self.publication = self.publish(request_id="origin-publish")
        self.goal_id = self.publication["goal_id"]

    def grade_round(self, publication=None, *, students=("a", "c"), score=1, partial=False):
        publication = publication or self.publication
        for student in students:
            task = next(task for task in self.learning.list_tasks(student, goal_id=self.goal_id)
                        if task["round_id"] == publication["round_id"])
            lease = self.learning.acquire_draft_lease(task["id"], student, "insight-test-device")
            answers = {q["id"]: {"single_choice": 1, "fill": "word", "judge": True}[q["type"]] for q in task["questions"]}
            saved = self.learning.save_draft(task["id"], student, answers, expected_version=lease["task_version"],
                request_id="save-" + task["id"], lease_token=lease["lease_token"], fence_token=lease["fence_token"])
            submitted = self.learning.submit_attempt(task["id"], student, expected_version=saved["version"],
                request_id="submit-" + task["id"], lease_token=lease["lease_token"], fence_token=lease["fence_token"])
            attempt = submitted["attempts"][0]["id"]
            outbox = next(item for item in self.learning.list_grading_outbox() if item["attempt_id"] == attempt)
            self.learning.mark_grading_outbox(outbox["job_id"], "dispatched", fence_token=1)
            for question in task["questions"][:1] if partial else task["questions"]:
                self.learning.apply_grading_result(attempt, question["id"], {"score": score}, expected_revision=1,
                    job_id=outbox["job_id"], fence_token=1)

    def freeze(self, *, scope_changes=None):
        scope = {"kind": "goal", "goal_id": self.goal_id, "class_id": self.classroom["id"], "course_id": self.course["id"], **(scope_changes or {})}
        source = self.learning.report_input(scope=scope, actor_id="teacher")
        for student in source["students"]:
            student["display_name"] = "Private Student Name " + student["id"]
        snapshot = build_report_snapshot(source, scope)
        stored = self.runtime.reports.create_snapshot(scope, source["source_revision"], snapshot, created_by="teacher")
        return self.runtime.reports.create_revision(stored["id"])

    def insight(self, *, request_id="summary-one", grade=True, completed=True):
        if grade:
            self.grade_round()
        revision = self.freeze()
        row = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id=request_id)
        if completed:
            self.insights.bind_generation(row["id"], 1)
            prepared = self.service.prepare(row["id"], generation=1)
            fake_gateway = AsyncMock(return_value=valid_selection(json.loads(prepared["messages"][-1]["content"])))
            output = asyncio.run(fake_gateway(messages=prepared["messages"], purpose=prepared["purpose"]))
            self.assertEqual(fake_gateway.await_count, 1)
            row = self.service.apply(row["id"], output, generation=1)
            self.assertEqual(row["status"], "completed", row.get("error"))
        return row, revision

    def next_draft(self, *, request_id="draft-one", grade=True):
        insight, revision = self.insight(grade=grade)
        draft = self.service.create_next_round("teacher", insight["id"], request_id=request_id)
        return draft, insight, revision

    def preview_next(self, draft):
        return self.service.preview_next_round("teacher", draft["id"], expected_revision=draft["revision"])

    def publish_next(self, draft, *, preview=None, request_id="publish-next"):
        preview = preview or self.preview_next(draft)
        self.assertTrue(preview["ready"], preview["errors"])
        return self.service.publish_next_round("teacher", draft["id"], request_id=request_id,
            expected_revision=draft["revision"], preview_token=preview["preview_token"], confirm=True)

    def job(self, insight):
        return SimpleNamespace(job_id=insight["job_id"], kind="batch", payload={
            "resource_type": "teaching_insight", "resource_id": insight["id"], "purpose": "summary", "step": "summary",
            **{key: insight[key] for key in ("course_id", "class_id", "owner_id", "report_id", "source_report_revision", "input_hash")},
            "input_revision": insight["source_revision"]})

    def unused_pool(self, draft):
        with self.learning._using_connection() as db:
            pool, _ = self.planning._catalog(db, "teacher", draft["config"], self.planning._knowledge("teacher", draft["config"]))
            return [q["ref"] for q in self.planning._unused_goal_pool(db, self.goal_id, pool)]

    def bounded(self, draft, *, rounds=2, pool=None, request_id="bounded-one"):
        current = self.service.get_next_round("teacher", draft["id"])
        return self.service.configure_bounded_plan("teacher", draft["id"], request_id=request_id, confirm=True,
            expected_goal_version=current["current_goal_version"], question_pool=pool or self.unused_pool(draft),
            future_round_count=rounds, interval_seconds=60, ends_at=time.time() + 7200)

    def plan(self):
        with self.learning._using_connection() as db:
            return dict(db.execute("SELECT * FROM goal_plans WHERE goal_id=?", (self.goal_id,)).fetchone())

    def tick_due(self):
        due = self.plan()["next_open_at"] + 1
        with patch("app.learning_planning.time.time", return_value=due), patch.object(self.planning, "clock", return_value=due):
            self.learning.tick_plans(teacher_planning=self.planning)


class TeachingInsightTests(InsightFixture, unittest.TestCase):
    def test_valid_frozen_summary_and_privacy(self):
        insight, revision = self.insight()
        self.assertEqual(insight["snapshot_sha256"], revision["snapshot_sha256"])
        self.assertEqual(insight["facts"]["status_counts"]["mastered"], 2)
        self.assertEqual(insight["result"]["origin"], "ai")
        self.assertEqual(insight["result"]["interpretation_mode"], "validated_selection")
        self.assertFalse(insight["stale"])
        stored = self.insights.get(insight["id"])
        outbound = canonical_json(stored["input"]["model_input"])
        for forbidden in ("Private Student Name", self.course["id"], self.goal_id, "Distinct exercise", "answer", "student_id", "attempt_id"):
            self.assertNotIn(forbidden, outbound)
        self.assertEqual({row["percent"] for row in insight["result"]["findings"]}, {100.0})
        self.assertEqual(self.runtime.reports.get_revision(revision["id"]), revision)

    def test_low_evidence_is_not_zero_or_weak(self):
        insight, _ = self.insight(grade=False)
        self.assertEqual(insight["result"]["summary_code"], "needs_more_evidence")
        self.assertTrue(all(f["percent"] is None and f["explanation_code"] == "collect_independent_evidence"
                            for f in insight["result"]["findings"]))
        self.assertTrue(all(f["evidence_refs"] for f in insight["result"]["findings"]))

    def test_invalid_model_evidence_numbers_students_states_and_prose_fail_closed(self):
        self.grade_round()
        revision = self.freeze()
        mutations = {
            "unknown_ref": lambda value: value["findings"][0].update(evidence_refs=["mastery:S999"]),
            "other_student_ref": lambda value: value["findings"][0].update(evidence_refs=["mastery:S001", "mastery:S002"]),
            "wrong_number": lambda value: value["findings"][0].update(percent=98.1),
            "unknown_number": lambda value: value["findings"][0].update(observed_families=999),
            "false_count": lambda value: value["findings"][0].update(independent_families=True),
            "wrong_student": lambda value: value["findings"][0].update(student_code="S999"),
            "wrong_state": lambda value: value["findings"][0].update(status="needs_practice", explanation_code="practice_goal"),
            "free_text": lambda value: value.update(summary="全班100%不及格"),
            "no_refs": lambda value: value["findings"][0].update(evidence_refs=[]),
            "duplicate_student": lambda value: value["findings"].append(deepcopy(value["findings"][0])),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                row = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id=name)
                self.insights.bind_generation(row["id"], 1)
                output = valid_selection(self.insights.get(row["id"])["input"]["model_input"])
                mutate(output)
                result = self.service.apply(row["id"], output, generation=1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], "invalid_model_output")
                self.assertIsNone(result["result"])

    def test_low_evidence_strong_judgment_and_duplicate_json_are_rejected(self):
        row, revision = self.insight(grade=False, completed=False)
        self.insights.bind_generation(row["id"], 1)
        output = valid_selection(self.insights.get(row["id"])["input"]["model_input"])
        output["findings"][0].update(status="needs_practice", percent=0.0, explanation_code="practice_goal")
        self.assertEqual(self.service.apply(row["id"], output, generation=1)["status"], "failed")
        other = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="duplicate-json")
        self.insights.bind_generation(other["id"], 1)
        output = valid_selection(self.insights.get(other["id"])["input"]["model_input"])
        duplicate = canonical_json(output)[:-1] + ',"summary_code":"needs_more_evidence"}'
        self.assertEqual(self.service.apply(other["id"], duplicate, generation=1)["status"], "failed")

    def test_current_class_permissions_readonly_students_admin_and_revocation(self):
        insight, revision = self.insight()
        self.assertEqual(self.service.get("reader", insight["id"])["id"], insight["id"])
        self.assertEqual(self.service.list("reader", class_id=self.classroom["id"])["items"][0]["id"], insight["id"])
        for actor in ("other", "a", "outsider", "admin"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.get(actor, insight["id"])
        with self.assertRaises(PermissionError):
            self.service.create("reader", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="reader-create")
        self.platform.set_class_collaborator(self.classroom["id"], "other", "manage", granted_by="teacher")
        self.assertEqual(teaching_insight_access(self.runtime, "other", insight["id"], write=True)["id"], insight["id"])
        self.platform.update_user("teacher", status="disabled")
        with self.assertRaises(PermissionError):
            self.service.get("teacher", insight["id"])
        with self.assertRaises(PermissionError):
            self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="summary-one")

    def test_worker_rechecks_creator_permissions_before_prepare_and_apply(self):
        insight, _ = self.insight(grade=False, completed=False)
        self.insights.bind_generation(insight["id"], 1)
        output = valid_selection(self.insights.get(insight["id"])["input"]["model_input"])
        self.platform.update_user("teacher", status="disabled")
        with self.assertRaises(PermissionError):
            self.service.prepare(insight["id"], generation=1)
        with self.assertRaises(PermissionError):
            self.service.apply(insight["id"], output, generation=1)
        self.assertEqual(self.insights.get(insight["id"])["status"], "running")

    def test_complete_job_binding_and_active_admin_metadata_only(self):
        insight, _ = self.insight(grade=False)
        job = self.job(insight)
        for actor in ("teacher", "reader", "admin"):
            self.assertTrue(teaching_insight_job_access(self.runtime, actor, job))
        self.assertFalse(teaching_insight_job_access(self.runtime, "reader", job, write=True))
        self.assertTrue(teaching_insight_job_access(self.runtime, "admin", job, write=True))
        for key in job.payload:
            bad = deepcopy(job)
            bad.payload[key] = "mismatch"
            for actor in ("teacher", "admin"):
                self.assertFalse(teaching_insight_job_access(self.runtime, actor, bad, write=True), key)
        wrong_id = deepcopy(job)
        wrong_id.job_id = "another-job"
        self.assertFalse(teaching_insight_job_access(self.runtime, "admin", wrong_id))
        self.platform.update_user("admin", status="disabled")
        self.assertFalse(teaching_insight_job_access(self.runtime, "admin", job, write=True))
        with self.assertRaises(PermissionError):
            self.service.get("admin", insight["id"])

    def test_report_selection_rejects_personal_and_cross_report_bindings(self):
        personal = self.freeze(scope_changes={"student_id": "a"})
        with self.assertRaises(ValueError):
            self.service.create("teacher", report_id=personal["report_id"], source_report_revision=personal["id"], request_id="personal")
        revision = self.freeze()
        with self.assertRaises(LookupError):
            self.service.create("teacher", report_id="wrong-report", source_report_revision=revision["id"], request_id="mismatch")
        with self.assertRaises(PermissionError):
            self.service.create("other", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="cross-class")

    def test_old_report_and_summary_remain_immutable_after_new_grades(self):
        insight, revision = self.insight(grade=False)
        result, frozen_input = deepcopy(insight["result"]), deepcopy(self.insights.get(insight["id"])["input"])
        self.grade_round()
        changed = self.service.get("teacher", insight["id"])
        self.assertTrue(changed["stale"])
        self.assertEqual(changed["result"], result)
        self.assertEqual(self.insights.get(insight["id"])["input"], frozen_input)
        self.assertEqual(self.runtime.reports.get_revision(revision["id"]), revision)
        with self.runtime.reports._write() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE teaching_insights SET input_hash=? WHERE id=?", ("0" * 64, insight["id"]))

    def test_create_request_idempotency_conflict_and_dispatch_restart(self):
        insight, revision = self.insight(grade=False, completed=False)
        repeat = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="summary-one")
        self.assertEqual(repeat["id"], insight["id"])
        self.assertEqual(len(self.insights.pending_jobs()), 1)
        reopened = TeachingInsightStore(self.runtime.reports.db_path)
        self.assertEqual(reopened.pending_jobs()[0]["job_id"], insight["job_id"])
        self.assertFalse(reopened.mark_dispatched(insight["id"], "wrong-job"))
        self.assertTrue(reopened.mark_dispatched(insight["id"], insight["job_id"]))
        self.assertEqual(reopened.pending_jobs(), [])
        self.grade_round()
        newer = self.freeze()
        with self.assertRaises(InsightConflict):
            self.service.create("teacher", report_id=newer["report_id"], source_report_revision=newer["id"], request_id="summary-one")

    def test_generation_cas_cancel_is_terminal_and_failed_only_resume(self):
        row, revision = self.insight(grade=False, completed=False)
        self.assertTrue(self.insights.bind_generation(row["id"], 1))
        self.assertTrue(self.insights.bind_generation(row["id"], 2))
        self.assertFalse(self.insights.bind_generation(row["id"], 1))
        self.assertFalse(self.insights.complete(row["id"], {}, generation=1))
        self.assertFalse(self.insights.cancel(row["id"], generation=1))
        self.assertTrue(self.insights.cancel(row["id"], generation=2))
        self.assertFalse(self.insights.resume(row["id"], generation=3))
        self.assertFalse(self.insights.bind_generation(row["id"], 3))
        with self.assertRaises(InsightConflict):
            self.service.apply(row["id"], {}, generation=2)
        failed = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="failed-resume")
        self.insights.bind_generation(failed["id"], 1)
        self.assertTrue(self.insights.fail(failed["id"], generation=1))
        self.assertFalse(self.insights.resume(failed["id"], generation=1))
        self.assertTrue(self.insights.resume(failed["id"], generation=2))
        self.assertTrue(self.insights.bind_generation(failed["id"], 2))
        output = valid_selection(self.insights.get(failed["id"])["input"]["model_input"])
        result = self.service.apply(failed["id"], output, generation=2)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.insights.resume(failed["id"], generation=3))

    def test_terminal_notifications_transactional_id_only_and_restart_ack(self):
        insight, revision = self.insight(grade=False)
        pending = self.insights.pending_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_type"], "teaching_insight_ready")
        self.assertEqual(set(pending[0]["payload"]), {"insight_id", "job_id", "generation"})
        self.assertEqual(pending[0]["resource_id"], insight["id"])
        reopened = TeachingInsightStore(self.runtime.reports.db_path)
        self.assertEqual(reopened.pending_notifications(), pending)
        self.assertTrue(reopened.acknowledge_notification(pending[0]["id"]))
        self.assertTrue(reopened.acknowledge_notification(pending[0]["id"]))
        self.assertEqual(reopened.pending_notifications(), [])
        row = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="notification-failure")
        self.insights.bind_generation(row["id"], 1)
        self.insights.fail(row["id"], generation=1)
        self.insights.resume(row["id"], generation=2)
        self.insights.bind_generation(row["id"], 2)
        self.insights.fail(row["id"], generation=2)
        self.insights.cancel(row["id"], generation=2)
        events = self.insights.pending_notifications()
        self.assertEqual(len(events), 3)
        self.assertEqual(len({event["business_key"] for event in events}), 3)
        self.assertEqual({event["event_type"] for event in events}, {"teaching_insight_failed", "teaching_insight_cancelled"})
        another = self.service.create("teacher", report_id=revision["report_id"], source_report_revision=revision["id"], request_id="rollback-notification")
        self.insights.bind_generation(another["id"], 1)
        with patch.object(self.insights, "_notification", side_effect=RuntimeError("notification intent failure")):
            with self.assertRaises(RuntimeError):
                self.insights.fail(another["id"], generation=1)
        self.assertEqual(self.insights.get(another["id"])["status"], "running")

    def test_report_v1_migration_preserves_existing_frozen_tables(self):
        path = self.root / "old-reports.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            ReportStore._migration_1(db)
            db.execute("PRAGMA user_version=1")
            db.commit()
        migrated = ReportStore(path)
        self.assertEqual(migrated.schema_version, 2)
        with migrated.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(ReportStore._TABLES))
        self.assertEqual(TeachingInsightStore(migrated).pending_jobs(), [])


class NextRoundTests(InsightFixture, unittest.TestCase):
    def test_editable_preview_original_goal_publish_and_replay(self):
        draft, insight, _ = self.next_draft()
        self.assertEqual(draft["next_round"], 2)
        self.assertEqual(draft["origin_goal_id"], self.goal_id)
        self.assertEqual(self.service.get("reader", insight["id"])["next_round_drafts"][0]["id"], draft["id"])
        preview = self.preview_next(draft)
        self.assertTrue(preview["ready"], preview["errors"])
        self.assertEqual(preview["draft_id"], draft["id"])
        self.assertTrue(all(item["weak_knowledge_points"] == [] for item in preview["suggestions"]))
        initial_counts = self.counts()
        result = self.publish_next(draft, preview=preview)
        self.assertEqual((result["goal_id"], result["round_no"]), (self.goal_id, 2))
        self.assertEqual(self.publish_next(draft, preview=preview), result)
        with self.assertRaises(PlanningConflict):
            self.publish_next(draft, preview=preview, request_id="unknown-new-request")
        self.assertEqual(self.counts()["learning_goals"], initial_counts["learning_goals"])
        self.assertEqual(self.counts()["learning_rounds"], 2)
        self.assertEqual(self.service.get_next_round("teacher", draft["id"])["published_result"], result)

    def test_save_cas_idempotency_and_required_count_consistency(self):
        draft, _, _ = self.next_draft()
        config = {**draft["config"], "common_level": "light"}
        saved = self.service.save_next_round("teacher", draft["id"], request_id="save-one", expected_revision=1, config=config)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.service.save_next_round("teacher", draft["id"], request_id="save-one", expected_revision=1, config=config), saved)
        with self.assertRaises(PlanningConflict):
            self.service.save_next_round("teacher", draft["id"], request_id="save-stale", expected_revision=1, config=config)
        with self.assertRaises(IdempotencyConflict):
            self.service.save_next_round("teacher", draft["id"], request_id="save-one", expected_revision=1,
                                         config={**config, "common_level": "heavy"})
        with self.assertRaises(ValidationError):
            self.service.save_next_round("teacher", draft["id"], request_id="wrong-count", expected_revision=2,
                                         config={**config, "question_count": 4})
        with self.assertRaises(PlanningConflict):
            self.preview_next(draft)

    def test_concurrent_draft_cas_has_one_winner(self):
        draft, _, _ = self.next_draft()
        barrier = threading.Barrier(2)
        def edit(level):
            barrier.wait(timeout=5)
            try:
                return self.service.save_next_round("teacher", draft["id"], request_id="save-" + level,
                    expected_revision=1, config={**draft["config"], "common_level": level})["revision"]
            except PlanningConflict as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, ["light", "heavy"]))
        self.assertCountEqual(results, [2, "draft_revision_conflict"])

    def test_goal_version_and_next_round_races_are_rejected(self):
        draft, insight, _ = self.next_draft()
        preview = self.preview_next(draft)
        # Published titles are immutable; simulate the version increment from
        # a concurrent lifecycle command within this disposable fixture only.
        with self.learning._using_connection(write=True) as db:
            db.execute("UPDATE learning_goals SET version=version+1 WHERE id=?", (self.goal_id,))
        with self.assertRaises(PlanningConflict) as failure:
            self.publish_next(draft, preview=preview)
        self.assertEqual(failure.exception.code, "goal_version_conflict")
        with self.assertRaises(PlanningConflict):
            self.service.create_next_round("teacher", insight["id"], request_id="after-goal-change")
        self.assertEqual(self.counts()["learning_rounds"], 1)

    def test_unfinished_and_partially_graded_previous_round_block(self):
        draft, _, _ = self.next_draft(grade=False)
        preview = self.preview_next(draft)
        self.assertFalse(preview["ready"])
        self.assertIn("previous_round_incomplete", [error["code"] for error in preview["errors"]])
        self.grade_round(partial=True)
        preview = self.preview_next(draft)
        self.assertFalse(preview["ready"])
        self.assertIn("previous_round_ungraded", [error["code"] for error in preview["errors"]])
        with self.assertRaises(PlanningConflict):
            self.service.publish_next_round("teacher", draft["id"], request_id="not-ready", expected_revision=1, preview_token="fake", confirm=True)
        self.assertEqual(self.counts()["learning_rounds"], 1)

    def test_historical_families_and_override_duplicates_cannot_be_reused(self):
        draft, _, _ = self.next_draft()
        with self.learning._using_connection() as db:
            used = list(db.execute("SELECT question_id,frozen_json FROM task_items LIMIT 1"))[0]
        question = json.loads(used["frozen_json"])
        self.question("claimed-new-family", kind=question["type"], stem=question["stem"], family_id="claimed-new-family")
        preview = self.preview_next(draft)
        self.assertTrue(preview["ready"], preview["errors"])
        self.assertNotIn("claimed-new-family", [q["question_id"] for q in preview["questions"]])
        refs = [{"question_id": q["question_id"], "version": q["version"]} for q in preview["students"][0]["questions"]]
        refs[1] = refs[0]
        saved = self.service.save_next_round("teacher", draft["id"], request_id="bad-override", expected_revision=1,
                                            config=draft["config"], paper_overrides={"a": refs})
        blocked = self.preview_next(saved)
        self.assertFalse(blocked["ready"])
        self.assertIn("invalid_paper_override", [error["code"] for error in blocked["errors"]])

    def test_teacher_paper_override_is_exactly_frozen(self):
        draft, _, _ = self.next_draft()
        preview = self.preview_next(draft)
        overrides = {student["student_id"]: [{"question_id": q["question_id"], "version": q["version"]}
                    for q in student["questions"]] for student in preview["students"]}
        saved = self.service.save_next_round("teacher", draft["id"], request_id="exact-override", expected_revision=1,
                                            config=draft["config"], paper_overrides=overrides)
        refreshed = self.preview_next(saved)
        result = self.publish_next(saved, preview=refreshed)
        for sid in saved["config"]["student_ids"]:
            task = next(t for t in self.learning.list_tasks(sid, goal_id=self.goal_id) if t["round_id"] == result["round_id"])
            self.assertEqual([(q["id"], q["version"]) for q in task["questions"]],
                             [(q["question_id"], q["version"]) for q in overrides[sid]])

    def test_shortage_blocks_without_new_questions_or_generation(self):
        draft, _, _ = self.next_draft()
        with self.learning._using_connection(write=True) as db:
            db.execute("UPDATE question_versions SET status='quarantined' WHERE question_id LIKE 'judge-%'")
        preview = self.preview_next(draft)
        self.assertFalse(preview["ready"])
        self.assertTrue(preview["deficits"])
        self.assertIsNone(preview["preview_token"])
        self.assertEqual(self.content.list_pending_jobs(), [])
        self.assertEqual(self.counts()["learning_rounds"], 1)

    def test_preview_owner_scope_and_quarantine_revalidated_at_publish(self):
        draft, _, _ = self.next_draft()
        preview = self.preview_next(draft)
        selected = preview["questions"][0]
        self.learning.set_question_version_status("teacher", selected["question_id"], selected["version"], "quarantined")
        with self.assertRaises(PlanningConflict) as failure:
            self.publish_next(draft, preview=preview)
        self.assertEqual(failure.exception.code, "preview_stale")
        self.assertEqual(self.service.get_next_round("reader", draft["id"])["id"], draft["id"])
        for actor in ("reader", "a", "other", "admin"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.save_next_round(actor, draft["id"], request_id="denied", expected_revision=1, config=draft["config"])
        with self.assertRaises(ValueError):
            self.service.save_next_round("teacher", draft["id"], request_id="scope-change", expected_revision=1,
                                         config={**draft["config"], "class_id": self.other_class["id"]})

    def test_publish_requires_confirmation_and_rolls_back_partial_round(self):
        draft, _, _ = self.next_draft()
        preview = self.preview_next(draft)
        with self.assertRaises(ValidationError):
            self.service.publish_next_round("teacher", draft["id"], request_id="no-confirm", expected_revision=1,
                                             preview_token=preview["preview_token"], confirm=False)
        original, calls = self.learning.create_task, []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected task freeze failure")
            return original(*args, **kwargs)
        with patch.object(self.learning, "create_task", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.publish_next(draft, preview=preview)
        self.assertEqual(self.counts()["learning_rounds"], 1)
        self.assertEqual(self.service.get_next_round("teacher", draft["id"])["status"], "draft")
        self.publish_next(draft, preview=preview)

    def test_competing_drafts_publish_at_most_one_round(self):
        draft, insight, _ = self.next_draft()
        second = self.service.create_next_round("teacher", insight["id"], request_id="second-draft")
        preview, other_preview = self.preview_next(draft), self.preview_next(second)
        self.publish_next(draft, preview=preview)
        with self.assertRaises(PlanningConflict):
            self.publish_next(second, preview=other_preview, request_id="competing-publish")
        self.assertEqual(self.counts()["learning_rounds"], 2)
        self.assertEqual(len(self.service.get("teacher", insight["id"])["next_round_drafts"]), 2)


class BoundedInsightPlanTests(InsightFixture, unittest.TestCase):
    def test_confirmed_plan_uses_original_goal_finite_pool_and_closes(self):
        draft, _, _ = self.next_draft()
        published = self.publish_next(draft)
        confirmed = self.bounded(draft, rounds=1)
        self.assertEqual(confirmed["goal_id"], self.goal_id)
        self.assertEqual(confirmed["status"], "active")
        self.grade_round(published)
        self.tick_due()
        self.assertEqual(self.counts()["learning_rounds"], 3)
        self.assertEqual(self.counts()["learning_goals"], 1)
        self.assertEqual((self.plan()["status"], self.plan()["remaining_rounds"]), ("closed", 0))
        self.tick_due()
        self.assertEqual(self.counts()["learning_rounds"], 3)
        with self.learning._using_connection() as db:
            policy = json.loads(db.execute("""SELECT p.policy_json FROM round_policies p JOIN learning_rounds r
                ON r.id=p.round_id WHERE r.goal_id=? AND r.round_no=3""", (self.goal_id,)).fetchone()[0])
        self.assertEqual(policy["common_question_ratio"], 50)
        self.assertEqual(set(policy["personal"]), {"a", "c"})
        confirmed_keys = {(r["question_id"], r["version"]) for r in confirmed["question_pool"]}
        self.assertTrue(all((r["question_id"], r["version"]) in confirmed_keys for paper in policy["personal"].values() for r in paper))

    def test_unfinished_previous_round_pauses_even_after_deadline(self):
        draft, _, _ = self.next_draft()
        self.publish_next(draft)
        self.bounded(draft)
        self.tick_due()
        self.assertEqual(self.plan()["status"], "blocked")
        self.assertIn("未完成", self.plan()["reason"])
        self.assertEqual(self.counts()["learning_rounds"], 2)

    def test_partial_grading_pauses_without_allocating(self):
        draft, _, _ = self.next_draft()
        published = self.publish_next(draft)
        self.bounded(draft)
        self.grade_round(published, partial=True)
        self.tick_due()
        self.assertEqual(self.plan()["status"], "blocked")
        self.assertIn("评分", self.plan()["reason"])
        self.assertEqual(self.counts()["learning_rounds"], 2)

    def test_pool_exhaustion_pauses_without_auto_generation(self):
        draft, _, _ = self.next_draft()
        published = self.publish_next(draft)
        pool = self.unused_pool(draft)
        minimal = [next(ref for ref in pool if ref["question_id"].startswith(kind + "-")) for kind in ("single_choice", "fill", "judge")]
        self.bounded(draft, rounds=2, pool=minimal)
        self.grade_round(published)
        self.tick_due()
        with self.learning._using_connection() as db:
            third = dict(db.execute("SELECT id round_id FROM learning_rounds WHERE goal_id=? AND round_no=3", (self.goal_id,)).fetchone())
        self.grade_round(third)
        self.tick_due()
        self.assertEqual(self.plan()["status"], "blocked")
        self.assertIn("不足", self.plan()["reason"])
        self.assertEqual(self.plan()["remaining_rounds"], 1)
        self.assertEqual(self.content.list_pending_jobs(), [])
        self.assertEqual(self.counts()["learning_rounds"], 3)

    def test_pool_quarantine_revocation_and_missing_service_pause(self):
        draft, _, _ = self.next_draft()
        published = self.publish_next(draft)
        confirmed = self.bounded(draft)
        self.grade_round(published)
        ref = confirmed["question_pool"][0]
        self.learning.set_question_version_status("teacher", ref["question_id"], ref["version"], "quarantined")
        self.tick_due()
        self.assertEqual(self.plan()["status"], "blocked")
        self.assertEqual(self.counts()["learning_rounds"], 2)
        self.assertIn("题池", self.plan()["reason"])

    def test_teacher_confirmation_scope_count_and_versions_are_required(self):
        draft, _, _ = self.next_draft()
        with self.assertRaises(PlanningConflict):
            self.bounded(draft)
        self.publish_next(draft)
        pool = self.unused_pool(draft)
        current = self.service.get_next_round("teacher", draft["id"])
        options = {"request_id": "b" * 100, "confirm": True, "expected_goal_version": current["current_goal_version"],
                   "question_pool": pool, "future_round_count": 2, "interval_seconds": 60, "ends_at": time.time() + 3600}
        for changes in ({"confirm": False}, {"future_round_count": 101}, {"future_round_count": True}, {"question_pool": []}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.service.configure_bounded_plan("teacher", draft["id"], **{**options, **changes})
        with self.assertRaises(PlanningConflict):
            self.service.configure_bounded_plan("teacher", draft["id"], **{**options, "expected_goal_version": 1})
        with self.assertRaises(PermissionError):
            self.service.configure_bounded_plan("reader", draft["id"], **options)
        plan = self.service.configure_bounded_plan("teacher", draft["id"], **options)
        self.assertEqual(self.service.configure_bounded_plan("teacher", draft["id"], **options), plan)
        self.assertEqual(self.service.get_next_round("reader", draft["id"])["bounded_plan"]["source_draft_id"], draft["id"])


class TeachingInsightHTTPTests(InsightFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))
        self.as_user("teacher")

    def as_user(self, actor):
        self.client.cookies.clear()
        token = secrets.token_urlsafe(24)
        self.platform.create_session(token, actor, time.time() + 600)
        self.client.cookies.set("sid", token)

    def test_http_summary_roles_replay_and_draft_contract(self):
        self.grade_round()
        revision = self.freeze()
        payload = {"report_id": revision["report_id"], "source_report_revision": revision["id"], "request_id": "http-summary"}
        response = self.client.post("/api/v1/teaching-insights", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        insight = response.json()
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/api/v1/teaching-insights", json=payload).json()["id"], insight["id"])
        self.insights.bind_generation(insight["id"], 1)
        output = valid_selection(self.insights.get(insight["id"])["input"]["model_input"])
        self.service.apply(insight["id"], output, generation=1)
        self.as_user("reader")
        self.assertEqual(self.client.get("/api/v1/teaching-insights/" + insight["id"]).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/teaching-insights", json=payload).status_code, 404)
        self.as_user("a")
        self.assertEqual(self.client.get("/api/v1/teaching-insights/" + insight["id"]).status_code, 403)
        self.as_user("teacher")
        response = self.client.post(f"/api/v1/teaching-insights/{insight['id']}/next-round-drafts", json={"request_id": "http-next"})
        self.assertEqual(response.status_code, 201, response.text)
        draft = response.json()
        prefix = f"/api/v1/teaching-insights/next-round-drafts/{draft['id']}"
        self.assertEqual(self.client.get(prefix).json()["id"], draft["id"])
        response = self.client.post(prefix + "/preview", json={"expected_revision": draft["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["ready"])
        self.assertIn("preview_token", preview)
        self.assertNotIn("preview", preview)
        response = self.client.post(prefix + "/publish", json={"request_id": "http-publish", "expected_revision": 1,
                                     "preview_token": preview["preview_token"], "confirm": True})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["goal_id"], self.goal_id)
        self.assertEqual(self.client.get("/api/v1/teaching-insights/" + insight["id"]).json()["next_round_drafts"][0]["status"], "published")


if __name__ == "__main__":
    unittest.main()
