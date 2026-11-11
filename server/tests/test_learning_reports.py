"""Canonical reporting against real LearningStore v2 and current platform grants.

Fixtures use explicit named columns in the real repository schema. No substitute
learning database or fabricated source-revision migration is installed here.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.learning_store import LearningStore
from app.platform_store import PlatformStore
from app.report_store import ReportStore, content_hash
from app.services.learning_reports import build_learning_report
from app.services.report_exports import EXPORT_FORMATS, build_report_snapshot, render_export


PRIVATE = "PRIVATE_WORK_ANSWER_EXPLANATION_CHAT_TEACHER_NOTE"
CLASS_SCOPE = {"kind": "class", "class_id": "class-1", "course_id": "course-1"}
STUDENT_SCOPE = {"kind": "student", "student_id": "student-1", "class_id": "class-1", "course_id": "course-1"}
GOAL_SCOPE = {"kind": "goal", "goal_id": "goal-1", "class_id": "class-1", "course_id": "course-1"}
BANK_SCOPE = {"kind": "question_bank", "question_bank_id": "bank-1", "course_id": "course-1"}


class LearningReportsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="learning-report-integration-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.platform = PlatformStore(self.root / "platform.sqlite3")
        for identifier, role in (("teacher-1", "teacher"), ("teacher-2", "teacher"), ("reader", "teacher"),
                                 ("course-owner", "teacher"), ("student-1", "student"), ("student-2", "student"),
                                 ("admin", "admin")):
            self.platform.create_user(identifier, identifier, "argon2$isolated-test", role, user_id=identifier)
        self.platform.create_course("Course", "2026", "course-owner", course_id="course-1")
        self.platform.create_class("course-1", "First class", owner_teacher_id="teacher-1", class_id="class-1")
        self.platform.create_class("course-1", "Second class", owner_teacher_id="teacher-2", class_id="class-2")
        self.platform.set_class_collaborator("class-1", "reader", "read", granted_by="teacher-1")
        for student in ("student-1", "student-2"):
            self.platform.create_course_membership("course-1", student)
            self.platform.create_class_membership("class-1", student)
        self.store = LearningStore(self.root / "learning.sqlite3", access_checker=self.access)
        self._seed()

    def access(self, actor_id, scope, *, write=False):
        user = self.platform.get_user(actor_id)
        if not user or user["status"] != "active":
            return False
        if user["role"] == "student":
            return scope.get("kind") in {"student", "goal", "task"} and scope.get("student_id") == actor_id
        if scope.get("class_id"):
            return self.platform.can_access_class(actor_id, scope["class_id"], teaching=write)
        if scope.get("course_id") and scope.get("kind") in {"question", "question_bank"}:
            return self.platform.can_access_course(actor_id, scope["course_id"], teaching=write)
        return user["role"] == "admin" or scope.get("owner_id") == actor_id

    def _seed(self):
        with self.store._using_connection(write=True) as db:
            for goal, owner, classroom in (("goal-1", "teacher-1", "class-1"), ("goal-2", "teacher-2", "class-2")):
                db.execute("INSERT INTO learning_goals(id,owner_id,course_id,class_id,title,description,status,version,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,'active',1,1,1)", (goal, owner, "course-1", classroom, "Goal " + goal, PRIVATE))
            for number, goal in ((1, "goal-1"), (2, "goal-1"), (3, "goal-2")):
                db.execute("INSERT INTO learning_rounds(id,goal_id,round_no,status,deadline_at,allow_late,version,created_at) "
                           "VALUES(?,?,?,'open',NULL,1,1,1)", ("round-" + str(number), goal, number))
            self.questions = {}
            for number in range(1, 5):
                question = {"id": f"q{number}", "version": 1, "type": "fill", "stem": PRIVATE, "answer": PRIVATE,
                            "explanation": PRIVATE, "rubric": [{"text": PRIVATE}], "points": 1, "points_units": 100,
                            "family_id": f"family-{number}", "source_anchors": [
                                {"block_id": f"block-{number}", "file_id": "file-1", "anchor": "page:12",
                                 "sha256": "c" * 64, "export_allowed": True, "raw_text": PRIVATE, "license_note": PRIVATE}]}
                if number == 2:
                    question.pop("family_id")
                    question["family_sha"] = "b" * 64
                self.questions[number] = question
                db.execute("INSERT INTO question_versions(question_id,version,owner_id,course_id,status,content_json,created_at) "
                           "VALUES(?,1,'teacher-1','course-1',?,?,1)",
                           (f"q{number}", "quarantined" if number == 4 else "approved", json.dumps(question)))
            for task, student, goal, round_id in (("task-1", "student-1", "goal-1", "round-1"),
                                                 ("task-2", "student-2", "goal-1", "round-1"),
                                                 ("task-3", "student-1", "goal-2", "round-3"),
                                                 ("task-empty", "student-1", "goal-1", "round-2")):
                db.execute("INSERT INTO learning_tasks(id,goal_id,round_id,student_id,status,version,deadline_at,allow_late,allow_retries,created_at,updated_at) "
                           "VALUES(?,?,?,?,'results_ready',1,NULL,1,1,1,1)", (task, goal, round_id, student))
                for number, question in self.questions.items():
                    db.execute("INSERT INTO task_items(task_id,question_id,question_version,position,frozen_json) VALUES(?,?,1,?,?)",
                               (task, f"q{number}", number, json.dumps(question)))
            self._insert_attempt(db, "task-1", "attempt-1", 1, 100)
            self._insert_attempt(db, "task-1", "attempt-retry", 2, 200)
            self._insert_attempt(db, "task-2", "attempt-other-student", 1, 100)
            self._insert_attempt(db, "task-3", "attempt-other-class", 1, 100)
            self._insert_attempt(db, "task-empty", "attempt-pending", 1, 100, status="grading")
            # A later help event and another question's help cannot taint the
            # first independent result. Their payloads are never selected.
            for event_id, kind, question, occurred in (("help-q2", "help", "q2", 90),
                                                        ("future-help-q1", "help", "q1", 150),
                                                        ("future-exposure", "exposure", "q3", 150)):
                db.execute("INSERT INTO task_events(id,task_id,actor_id,event_type,question_id,payload_json,occurred_at) VALUES(?,'task-1','student-1',?,?,?,?)",
                           (event_id, kind, question, json.dumps({"chat": PRIVATE}), occurred))
            db.execute("INSERT INTO teacher_judgments(id,student_id,goal_id,knowledge_point_id,actor_id,judgment_json,created_at) "
                       "VALUES('judgment-1','student-1','goal-1','kp-1','teacher-1',?,300)",
                       (json.dumps({"judgment": "needs_review", "revision": 2, "evidence_revision": "a" * 64,
                                    "internal_note": PRIVATE, "calibrated": True}),))
            db.execute("INSERT INTO teacher_judgments(id,student_id,goal_id,knowledge_point_id,actor_id,judgment_json,created_at) "
                       "VALUES('judgment-other','student-2','goal-1','kp-1','teacher-1',?,300)",
                       (json.dumps({"judgment": "mastered", "internal_note": PRIVATE}),))

    def _insert_attempt(self, db, task, attempt, number, occurred, *, status="graded"):
        db.execute("INSERT INTO attempts(id,task_id,attempt_no,kind,status,revision,answers_json,submitted_at,graded_at,total_score_units,max_score_units,late,independent_evidence) "
                   "VALUES(?,?,?,?,?,1,?,?,?,400,400,0,?)",
                   (attempt, task, number, "first" if number == 1 else "retry", status,
                    json.dumps({"raw_answer": PRIVATE}), occurred, occurred + 1, int(number == 1)))
        for number in range(1, 5):
            db.execute("INSERT INTO attempt_items(attempt_id,task_id,question_id,status,score_units,original_grading_json,grading_json,updated_at) "
                       "VALUES(?,?,?,'graded',100,?,?,?)", (attempt, task, f"q{number}",
                       json.dumps({"feedback": PRIVATE}), json.dumps({"score_units": 100, "feedback": PRIVATE}), occurred + 1))

    def test_safe_integer_evidence_time_bounded_assistance_and_quarantine(self):
        result = build_learning_report(self.store, CLASS_SCOPE, "teacher-1")
        with self.store.connect() as db:
            self.assertEqual(result["source_revision"], db.execute("SELECT revision FROM learning_revision WHERE id=1").fetchone()[0])
        self.assertIs(type(result["source_revision"]), int)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(result["students"], [{"id": "student-1"}, {"id": "student-2"}])
        self.assertTrue(all("name" not in student for student in result["students"]))
        self.assertEqual({row["id"] for row in result["questions"]}, {"q1", "q2", "q3"})
        self.assertFalse(any(row["question_id"] == "q4" for row in result["evidence"]))
        self.assertFalse(any(row["attempt_id"] == "attempt-pending" for row in result["evidence"]))
        first = {row["question_id"]: row for row in result["evidence"] if row["attempt_id"] == "attempt-1"}
        self.assertTrue(first["q1"]["independent_evidence"])
        self.assertFalse(first["q1"]["assisted"])
        self.assertTrue(first["q2"]["assisted"])
        self.assertFalse(first["q2"]["independent_evidence"])
        self.assertFalse(first["q3"]["exposed"])
        self.assertEqual(first["q2"]["family_id"], "b" * 64)
        for row in result["evidence"]:
            self.assertIs(type(row["score_units"]), int)
            self.assertIs(type(row["max_score_units"]), int)
            if row["attempt_id"] == "attempt-retry":
                self.assertTrue(row["retry"])
                self.assertTrue(row["exposed"])
                self.assertFalse(row["independent_evidence"])

    def test_personal_history_never_contains_another_students_evidence_or_judgment(self):
        result = build_learning_report(self.store, STUDENT_SCOPE, "student-1")
        self.assertEqual(result["students"], [{"id": "student-1"}])
        self.assertTrue(result["evidence"])
        self.assertTrue(all(row["student_id"] == "student-1" for row in result["evidence"]))
        self.assertEqual([row["id"] for row in result["teacher_judgments"]], ["judgment-1"])
        marker = result["teacher_judgments"][0]
        self.assertNotIn("calibrated", marker)
        self.assertNotIn("internal_note", marker)
        self.platform.transfer_class_member("class-1", "class-2", "student-1", actor_id="admin")
        historical = build_learning_report(self.store, STUDENT_SCOPE, "student-1")
        self.assertEqual(historical["evidence"], result["evidence"])
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, STUDENT_SCOPE, "student-2")

    def test_each_task_passes_current_gate_with_the_original_actor(self):
        original = self.store._task_row
        with patch.object(self.store, "_task_row", wraps=original) as gate:
            result = build_learning_report(self.store, CLASS_SCOPE, "reader")
            self.assertTrue(result["evidence"])
            calls = [(call.args[1], call.args[2]) for call in gate.call_args_list]
        self.assertEqual(set(calls), {("task-1", "reader"), ("task-2", "reader"), ("task-empty", "reader")})
        self.platform.remove_class_collaborator("class-1", "reader", removed_by="teacher-1")
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, CLASS_SCOPE, "reader")
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, CLASS_SCOPE, "course-owner")

    def test_goal_without_class_is_resolved_against_authoritative_scope(self):
        bare = {"kind": "goal", "goal_id": "goal-1"}
        self.assertTrue(build_learning_report(self.store, bare, "teacher-1")["evidence"])
        self.assertTrue(build_learning_report(self.store, {**bare, "student_id": "student-1"}, "student-1")["evidence"])
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, {**GOAL_SCOPE, "class_id": "class-2"}, "teacher-2")
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, {"kind": "class", "class_id": "class-1"}, "student-1")

    def test_question_bank_meta_and_usage_contain_no_personal_details(self):
        result = build_learning_report(self.store, BANK_SCOPE, "course-owner")
        self.assertEqual(result["students"], [])
        self.assertEqual(result["goals"], [])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["teacher_judgments"], [])
        self.assertEqual({row["id"] for row in result["questions"]}, {"q1", "q2", "q3"})
        self.assertTrue(all(row["question_bank_id"] == "bank-1" for row in result["questions"]))
        self.assertTrue(all(not row["goal_ids"] for row in result["questions"]))
        self.assertNotIn(PRIVATE, json.dumps(result))
        for row in result["question_usage"]:
            self.assertEqual(set(row), {"question_id", "version", "usage_count", "independent_count", "assisted_count", "retry_count", "exposed_count"})
        # Course quality access exposes aggregate usage without personal detail.
        self.assertTrue(all(row["usage_count"] > 0 for row in result["question_usage"]))
        administrator = build_learning_report(self.store, BANK_SCOPE, "admin")
        self.assertTrue(all(row["usage_count"] > 0 for row in administrator["question_usage"]))
        self.assertNotIn("student-1", json.dumps(administrator))
        with self.assertRaises((LookupError, PermissionError)):
            build_learning_report(self.store, BANK_SCOPE, "student-1")

    def test_source_revision_changes_on_new_facts_and_freezing_preserves_old_result(self):
        first = build_learning_report(self.store, STUDENT_SCOPE, "student-1")
        report_store = ReportStore(self.root / "reports.sqlite3")
        frozen = build_report_snapshot(first, STUDENT_SCOPE)
        old = report_store.create_snapshot(STUDENT_SCOPE, first["source_revision"], frozen, created_by="student-1")
        old_revision = report_store.create_revision(old["id"])
        with self.store._using_connection(write=True) as db:
            db.execute("UPDATE attempt_items SET score_units=50 WHERE attempt_id='attempt-1' AND question_id='q1'")
            db.execute("UPDATE attempts SET revision=revision+1 WHERE id='attempt-1'")
        second = build_learning_report(self.store, STUDENT_SCOPE, "student-1")
        self.assertGreater(second["source_revision"], first["source_revision"])
        revised = next(row for row in second["evidence"] if row["attempt_id"] == "attempt-1" and row["question_id"] == "q1")
        original = next(row for row in first["evidence"] if row["id"] == revised["id"])
        self.assertEqual((original["score_units"], revised["score_units"]), (100, 50))
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(report_store.get_revision(old_revision["id"])["snapshot"], frozen)
        self.assertEqual(old_revision["snapshot_sha256"], content_hash(frozen))

    def test_all_four_real_scopes_feed_unchanged_report_builder_and_five_exports(self):
        for scope, actor in ((CLASS_SCOPE, "teacher-1"), (STUDENT_SCOPE, "student-1"), (GOAL_SCOPE, "teacher-1"), (BANK_SCOPE, "course-owner")):
            with self.subTest(kind=scope["kind"]):
                canonical = build_learning_report(self.store, scope, actor)
                before = deepcopy(canonical)
                snapshot = build_report_snapshot(canonical, scope)
                for format in EXPORT_FORMATS:
                    artifact = render_export(snapshot, format)
                    self.assertTrue(artifact.content)
                    self.assertEqual(artifact.manifest["snapshot_sha256"], content_hash(snapshot))
                self.assertEqual(canonical, before)
                self.assertNotIn(PRIVATE, json.dumps(snapshot))

    def test_invalid_scope_or_missing_version_is_not_silently_invented(self):
        for scope in ({"kind": "class"}, {"kind": "question_bank", "question_bank_id": "bank-1"},
                      {"kind": "class", "class_id": "class-1", "student_id": "student-1"},
                      {"kind": "student", "student_id": "student-1", "raw": True}):
            with self.subTest(scope=scope), self.assertRaises((ValueError, TypeError)):
                build_learning_report(self.store, scope, "teacher-1")
        with self.store._using_connection(write=True) as db:
            db.execute("DELETE FROM learning_revision WHERE id=1")
        with self.assertRaisesRegex(RuntimeError, "source revision"):
            build_learning_report(self.store, CLASS_SCOPE, "teacher-1")


if __name__ == "__main__":
    unittest.main()
