"""Isolated real-SQLite composition/publication tests; network and models forbidden."""

import atexit
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="teacher-plan-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(
    SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name, STUDY_DATA_DIR=_IMPORT_DATA.name,
    PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false", STUDY_DEMO_ENABLED="false",
    STUDY_WORKER_ENABLED="false", LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
    MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.learning_store import IdempotencyConflict, LearningStore
from app.platform_store import PlatformStore
from app.routers.v1_teacher_planning import router
from app.runtime import Runtime
from app.schemas.v1_teacher_planning import TeachingPlanConfig
from app.services.material_parser import parse_material
from app.services.question_bank import QuestionBank
from app.services.teacher_planning import PlanningConflict, TeacherPlanning, TYPES


class PlanningFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="teacher-planning-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in ("socket.getaddrinfo", "socket.create_connection", "httpx.HTTPTransport.handle_request",
                       "httpx.AsyncHTTPTransport.handle_async_request"):
            guard = patch(target, side_effect=AssertionError("external network is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.runtime = Runtime(Settings(app_env="test", data_dir=str(self.root), worker_enabled=False,
                                        llm_providers=(), llm_base_url="", llm_api_key="", llm_model=""))
        self.gateway = Mock()
        self.gateway.complete = AsyncMock(side_effect=AssertionError("models are forbidden"))
        self.runtime.install("gateway", self.gateway)
        self.platform, self.learning, self.content = self.runtime.platform, self.runtime.learning, self.runtime.content
        self.users = {}
        for name, role in (("teacher", "teacher"), ("other", "teacher"), ("reader", "teacher"),
                           ("a", "student"), ("b", "student"), ("c", "student"), ("outsider", "student")):
            self.users[name] = self.platform.create_user("plan_" + name, "Name " + name, "unused", role, user_id=name)
        self.course = self.platform.create_course("Planning course", "2026", "teacher")
        self.other_course = self.platform.create_course("Other course", "2026", "other")
        self.classroom = self.platform.create_class(self.course["id"], "Planning class", owner_teacher_id="teacher")
        self.other_class = self.platform.create_class(self.course["id"], "Other class", owner_teacher_id="other")
        for student in ("a", "b", "c", "outsider"):
            self.platform.create_course_membership(self.course["id"], student)
            self.platform.create_class_membership(self.other_class["id"] if student == "outsider" else self.classroom["id"], student)
        self.platform.set_class_collaborator(self.classroom["id"], "reader", "read", granted_by="teacher")
        self.platform.set_course_collaborator(self.course["id"], "reader", "read", granted_by="teacher")
        self.planning = TeacherPlanning(self.learning, self.platform, self.content)
        self.runtime.install("teacher_planning", self.planning)
        self.config = {
            "course_id": self.course["id"], "class_id": self.classroom["id"], "title": "Teacher goal",
            "description": "Selected students and exact types", "student_ids": ["a", "c"],
            "question_count": 3, "question_types": {"single_choice": 1, "fill": 1, "judge": 1, "text": 0},
            "common_level": "balanced", "difficulty": "any", "knowledge_points": [],
            "deadline_at": time.time() + 3600, "scheduled_at": None,
            "allow_late": False, "allow_retries": True, "allow_retry_after_deadline": False,
        }

    def tearDown(self):
        self.gateway.complete.assert_not_called()

    def seed(self, *, per_type=5, kinds=("single_choice", "fill", "judge"), **metadata):
        for kind in kinds:
            for index in range(per_type):
                self.question(kind + "-" + chr(65 + index), kind=kind, **metadata)

    def question(self, qid, *, kind="single_choice", points=1, version=1, status="approved", course_id=None, author="teacher", **metadata):
        question = {"type": kind, "stem": "Distinct exercise " + qid,
                    "options": ["Alpha", "Beta"] if kind == "single_choice" else [],
                    "answer": {"single_choice": 1, "fill": "word", "judge": True, "text": "reason"}[kind],
                    "points": points, "rubric": [{"point": "reason", "score_units": int(points * 100)}] if kind == "text" else [],
                    **metadata}
        self.learning.create_question_version(author, qid, version, question, status=status,
                                              course_id=course_id or self.course["id"])
        return {"question_id": qid, "version": version}

    def publish(self, config=None, *, request_id="publish-one", preview=None):
        config = config or self.config
        preview = preview or self.planning.preview("teacher", config)
        self.assertTrue(preview["ready"], preview)
        return self.planning.publish("teacher", config, preview_token=preview["preview_token"], request_id=request_id)

    def counts(self):
        with self.learning._using_connection() as db:
            return {
                "learning_goals": db.execute("SELECT COUNT(*) FROM learning_goals").fetchone()[0],
                "learning_rounds": db.execute("SELECT COUNT(*) FROM learning_rounds").fetchone()[0],
                "learning_tasks": db.execute("SELECT COUNT(*) FROM learning_tasks").fetchone()[0],
                "task_items": db.execute("SELECT COUNT(*) FROM task_items").fetchone()[0],
            }

    def material(self, *, course_id=None, owner="teacher", confirm=True, external=True):
        text = "教学材料：理解概念与推理方法。".encode()
        material = self.content.import_material(course_id=course_id or self.course["id"], owner_id=owner,
            filename="lesson.txt", content=text, parsed=parse_material("lesson.txt", text),
            license_code="own", rights_confirmed=True, allow_generation=True, allow_retrieval=True,
            external_processing_allowed=external)
        block = self.content.list_blocks(material["id"])[0]
        if confirm:
            self.content.confirm_blocks(material["id"], [block["id"]], actor_id=owner, expected_revision=1)
        return self.content.get_material(material["id"]), self.content.list_blocks(material["id"])[0]

    def knowledge(self, *, confirmed=True):
        material, source = self.material()
        point = self.content.add_knowledge_point(self.course["id"], "concept-" + source["id"], "Concept",
                                                  created_by="teacher", source_block_ids=[source["id"]])
        if confirmed:
            self.content.confirm_curriculum("knowledge", point["id"], actor_id="teacher")
        return point, material, source


class TeacherPlanningTests(PlanningFixture, unittest.TestCase):
    def test_exact_selection_types_scores_frozen_publication_and_contract(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        self.assertTrue(preview["ready"])
        required = {"ready", "preview_token", "errors", "deficits", "common_count", "actual_common_ratio",
                    "question_count", "total_points", "estimated_minutes", "students", "questions", "suggestions"}
        self.assertFalse(required - set(preview))
        self.assertEqual([s["student_id"] for s in preview["students"]], ["a", "c"])
        self.assertEqual(preview["total_points"], 3)
        self.assertEqual(preview["estimated_minutes"], 6)
        expected = {s["student_id"]: [(q["question_id"], q["version"]) for q in s["questions"]] for s in preview["students"]}
        for student in preview["students"]:
            self.assertEqual(student["question_types"], self.config["question_types"])
            self.assertEqual(student["question_count"], 3)
            self.assertEqual(student["total_score_units"], 300)
            self.assertEqual(student["suggestion"]["status"], "pending_evaluation")
            self.assertTrue(all("answer" not in q and "rubric" not in q for q in student["questions"]))
        result = self.publish(preview=preview)
        self.assertEqual(result["status"], "open")
        self.assertEqual(self.learning.list_tasks("b", goal_id=result["goal_id"]), [])
        for student in ("a", "c"):
            task = self.learning.list_tasks(student, goal_id=result["goal_id"])[0]
            self.assertEqual([(q["id"], q["version"]) for q in task["questions"]], expected[student])
        self.assertEqual(self.counts(), {"learning_goals": 1, "learning_rounds": 1, "learning_tasks": 2, "task_items": 6})

    def test_all_levels_use_ceil_and_actual_intersection(self):
        self.seed()
        for level, target in (("light", 1), ("balanced", 2), ("heavy", 3)):
            with self.subTest(level=level):
                preview = self.planning.preview("teacher", {**self.config, "common_level": level})
                sets = [{q["question_id"] for q in student["questions"]} for student in preview["students"]]
                self.assertEqual(preview["target_common_count"], target)
                self.assertEqual(len(set.intersection(*sets)), target)
                self.assertEqual(preview["common_count"], target)
                self.assertAlmostEqual(preview["actual_common_ratio"], target * 100 / 3, places=3)

    def test_single_student_all_common_and_limited_pool_truthful_ratio(self):
        self.seed(per_type=1)
        preview = self.planning.preview("teacher", {**self.config, "student_ids": ["a"], "common_level": "light"})
        self.assertTrue(preview["ready"])
        self.assertEqual((preview["target_common_count"], preview["common_count"], preview["actual_common_ratio"]), (3, 3, 100))
        limited = self.planning.preview("teacher", {**self.config, "common_level": "light"})
        self.assertTrue(limited["ready"])
        self.assertEqual(limited["common_count"], 3)
        self.assertIn("additional_common_questions", [w["code"] for w in limited["warnings"]])

    def test_four_types_and_mixed_points_keep_equal_score_slots(self):
        for kind in TYPES:
            for suffix, points in (("Alpha", 1), ("Beta", 2), ("Gamma", 2), ("Delta", 3)):
                self.question(kind + suffix, kind=kind, points=points)
        config = {**self.config, "question_count": 8, "question_types": dict.fromkeys(TYPES, 2), "student_ids": ["a", "b", "c"], "common_level": "light"}
        preview = self.planning.preview("teacher", config)
        self.assertTrue(preview["ready"])
        self.assertEqual(len({s["total_score_units"] for s in preview["students"]}), 1)
        self.assertEqual(len({tuple(sorted((q["type"], q["points_units"]) for q in s["questions"])) for s in preview["students"]}), 1)
        self.publish(config, preview=preview)

    def test_latest_candidate_hides_old_approved_and_missing_type_never_falls_back(self):
        self.seed(per_type=1)
        self.question("judge-A", kind="judge", version=2, status="candidate")
        for index in range(5):
            self.question("extra-choice-" + chr(65 + index))
        preview = self.planning.preview("teacher", self.config)
        self.assertFalse(preview["ready"])
        self.assertIsNone(preview["preview_token"])
        self.assertEqual([(d["question_type"], d["missing"]) for d in preview["deficits"]], [("judge", 1)])
        with self.assertRaises(PlanningConflict):
            self.planning.publish("teacher", self.config, preview_token="unapproved", request_id="no-publish")
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_repeated_family_and_numeric_variants_cannot_share_one_paper(self):
        self.question("variant-alpha", stem="Compute 1 + 2", family_id="one-family")
        self.question("variant-beta", stem="Compute 3 + 4", family_id="different-claimed-family")
        config = {**self.config, "question_count": 2, "question_types": {"single_choice": 2, "fill": 0, "judge": 0, "text": 0}}
        preview = self.planning.preview("teacher", config)
        self.assertFalse(preview["ready"])
        self.assertEqual(preview["deficits"][0]["available"], 1)
        self.question("unrelated", stem="Choose a shape")
        preview = self.planning.preview("teacher", config)
        self.assertTrue(preview["ready"])
        for student in preview["students"]:
            self.assertIn("unrelated", [q["question_id"] for q in student["questions"]])

    def test_difficulty_and_only_confirmed_knowledge_and_sources_are_eligible(self):
        point, material, source = self.knowledge()
        self.seed(difficulty="easy", knowledge_points=[point["id"]], source_anchors=[{
            "block_id": source["id"], "file_id": material["id"], "anchor": source["anchor"],
            "source_revision": material["revision"], "sha256": source["sha256"],
        }])
        config = {**self.config, "difficulty": "easy", "knowledge_points": [point["id"]]}
        preview = self.planning.preview("teacher", config)
        self.assertTrue(preview["ready"])
        self.assertTrue(all(q["source_status"] == "confirmed" for q in preview["questions"]))
        self.assertFalse(self.planning.preview("teacher", {**config, "difficulty": "hard"})["ready"])
        draft, _, _ = self.knowledge(confirmed=False)
        invalid = self.planning.preview("teacher", {**config, "knowledge_points": [draft["id"]]})
        self.assertFalse(invalid["ready"])
        self.assertIn("knowledge_not_confirmed", [error["code"] for error in invalid["errors"]])

    def test_legacy_knowledge_aliases_must_uniquely_resolve_to_confirmed_ids(self):
        point, _, _ = self.knowledge()
        config = {**self.config, "knowledge_points": [point["id"]]}
        self.question("code-choice", knowledge_points=[point["code"]])
        self.question("title-fill", kind="fill", knowledge_points=[point["title"]])
        self.question("id-judge", kind="judge", knowledge_points=[point["id"]])
        self.question("arbitrary-choice", knowledge_points=["unconfirmed-label"])
        preview = self.planning.preview("teacher", config)
        self.assertTrue(preview["ready"])
        self.assertTrue(all(q["knowledge_points"] == [point["id"]] for q in preview["questions"]))
        self.assertNotIn("arbitrary-choice", [q["question_id"] for q in preview["questions"]])
        # A second confirmed title makes a prior title alias ambiguous. The
        # original immutable question is not rewritten or silently reassigned.
        self.knowledge()
        ambiguous = self.planning.preview("teacher", config)
        self.assertFalse(ambiguous["ready"])
        self.assertEqual(ambiguous["deficits"][0]["question_type"], "fill")
        with self.assertRaises(PlanningConflict):
            self.publish(config, preview=preview)
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_confirming_another_source_block_keeps_preview_and_questions_usable(self):
        text = b"First source paragraph.\n\nSecond independent paragraph.\n"
        material = self.content.import_material(course_id=self.course["id"], owner_id="teacher",
            filename="partial.txt", content=text, parsed=parse_material("partial.txt", text),
            license_code="own", rights_confirmed=True, allow_generation=True, allow_retrieval=True,
            external_processing_allowed=True)
        first, second = self.content.list_blocks(material["id"])
        self.content.confirm_blocks(material["id"], [first["id"]], actor_id="teacher", expected_revision=1)
        self.seed(per_type=1, source_anchors=[{
            "block_id": first["id"], "file_id": material["id"], "anchor": first["anchor"],
            "sha256": first["sha256"], "source_revision": 2,
        }])
        preview = self.planning.preview("teacher", self.config)
        self.assertTrue(preview["ready"])
        self.content.confirm_blocks(material["id"], [second["id"]], actor_id="teacher", expected_revision=2)
        current = self.content.confirmed_sources(self.course["id"], [first["id"]],
            purpose="generation", class_id=self.classroom["id"])[0]
        self.assertEqual(current["sha256"], first["sha256"])
        self.assertEqual(current["source_revision"], 3)
        # Both a fresh preview and the original token remain usable: only an
        # unrelated block was confirmed, not the cited content or its rights.
        refreshed = self.planning.preview("teacher", self.config)
        self.assertTrue(refreshed["ready"], refreshed["errors"])
        self.assertEqual(refreshed["questions"], preview["questions"])
        self.publish(preview=preview)

    def test_source_identity_and_current_rights_still_invalidate_preview(self):
        material, source = self.material()
        self.seed(per_type=1, source_anchors=[{
            "block_id": source["id"], "file_id": material["id"], "anchor": source["anchor"],
            "sha256": source["sha256"], "source_revision": material["revision"],
        }])
        preview = self.planning.preview("teacher", self.config)
        self.assertTrue(preview["ready"])
        confirmed_sources = self.content.confirmed_sources
        # Confirmed blocks are immutable through the store API. Substitute the
        # source projection to exercise stale/corrupt identity and permission data.
        changes = ({"id": "different-block"}, {"file_id": "different-file"}, {"anchor": "different-anchor"},
                   {"sha256": "0" * 64}, {"visibility": "private", "owner_id": "other"})
        for changeset in changes:
            with self.subTest(changes=changeset):
                def changed_source(*args, **kwargs):
                    return [{**row, **changeset} for row in confirmed_sources(*args, **kwargs)]
                with patch.object(self.content, "confirmed_sources", side_effect=changed_source):
                    with self.assertRaises(PlanningConflict) as failure:
                        self.publish(preview=preview)
                self.assertEqual(failure.exception.code, "preview_stale")
                self.assertEqual(self.counts()["learning_goals"], 0)
        with patch.object(self.content, "confirmed_sources", side_effect=PermissionError("source use revoked")):
            with self.assertRaises(PlanningConflict):
                self.publish(preview=preview)
        self.publish(preview=preview)

    def test_unconfirmed_cross_course_private_source_or_wrong_rubric_is_excluded(self):
        material, block = self.material(confirm=False)
        self.question("fake-source", source_anchors=[{"block_id": block["id"]}])
        _, foreign = self.material(course_id=self.other_course["id"], owner="other")
        self.question("foreign-source", source_anchors=[{"block_id": foreign["id"]}])
        _, private = self.material(owner="other")
        self.question("private-source", source_anchors=[{"block_id": private["id"]}])
        self.question("bad-rubric", kind="text", rubric=[{"point": "reason", "score_units": 90}])
        self.question("empty-rubric", kind="text", rubric=[])
        config = {**self.config, "question_count": 1, "question_types": {"single_choice": 1, "fill": 0, "judge": 0, "text": 0}}
        self.assertFalse(self.planning.preview("teacher", config)["ready"])
        config["question_types"] = {"single_choice": 0, "fill": 0, "judge": 0, "text": 1}
        self.assertFalse(self.planning.preview("teacher", config)["ready"])
        self.question("valid-rubric", kind="text", rubric=[{"point": "reason", "score": 1}])
        self.assertTrue(self.planning.preview("teacher", config)["ready"])

    def test_permissions_require_teaching_class_and_current_selected_members(self):
        self.seed()
        for actor in ("other", "reader", "a"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.planning.preview(actor, self.config)
        for changes in ({"student_ids": ["a", "outsider"]}, {"student_ids": ["teacher"]}, {"student_ids": ["missing"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.planning.preview("teacher", {**self.config, **changes})
        with self.assertRaises(PermissionError):
            self.planning.preview("teacher", {**self.config, "course_id": self.other_course["id"]})
        preview = self.planning.preview("teacher", self.config)
        self.platform.delete_class_membership(self.classroom["id"], "a")
        with self.assertRaises(ValueError):
            self.publish(preview=preview)
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_quarantine_or_new_version_after_preview_blocks_without_silent_reselection(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        selected = preview["questions"][0]
        self.learning.set_question_version_status("teacher", selected["question_id"], selected["version"], "quarantined")
        with self.assertRaises(PlanningConflict) as failure:
            self.publish(preview=preview)
        self.assertEqual(failure.exception.code, "preview_stale")
        self.assertEqual(self.counts()["learning_goals"], 0)
        preview = self.planning.preview("teacher", self.config)
        selected = preview["questions"][0]
        self.question(selected["question_id"], kind=selected["type"], version=2)
        with self.assertRaises(PlanningConflict):
            self.publish(preview=preview)
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_config_token_owner_and_expiry_are_bound(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        with self.assertRaises(PlanningConflict) as failure:
            self.planning.publish("teacher", {**self.config, "title": "changed"}, preview_token=preview["preview_token"], request_id="changed")
        self.assertEqual(failure.exception.code, "config_changed")
        self.platform.set_class_collaborator(self.classroom["id"], "other", "manage", granted_by="teacher")
        self.platform.set_course_collaborator(self.course["id"], "other", "read", granted_by="teacher")
        with self.assertRaises(PlanningConflict):
            self.planning.publish("other", self.config, preview_token=preview["preview_token"], request_id="stolen")
        with patch.object(self.planning, "clock", return_value=preview["preview_expires_at"]):
            with self.assertRaises(PlanningConflict) as failure:
                self.publish(preview=preview)
            self.assertEqual(failure.exception.code, "preview_expired")

    def test_idempotent_concurrent_publish_and_token_reuse_never_duplicate_goal(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        barrier = threading.Barrier(2)
        def action():
            barrier.wait(timeout=5)
            return self.planning.publish("teacher", self.config, preview_token=preview["preview_token"], request_id="same-command")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(action) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.publish(preview=preview, request_id="different-command"), results[0])
        with self.assertRaises(IdempotencyConflict):
            self.planning.publish("teacher", {**self.config, "title": "changed"}, preview_token=preview["preview_token"], request_id="same-command")
        self.assertEqual(self.counts()["learning_goals"], 1)
        with patch.object(self.planning, "clock", return_value=preview["preview_expires_at"] + 1):
            self.assertEqual(self.publish(preview=preview, request_id="same-command"), results[0])
        self.platform.update_user("teacher", status="disabled")
        with self.assertRaises(PermissionError):
            self.publish(preview=preview, request_id="same-command")

    def test_publication_failure_rolls_back_goal_commands_round_tasks_and_token(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        original = self.learning.create_task
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected freeze failure")
            return original(*args, **kwargs)
        with patch.object(self.learning, "create_task", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.publish(preview=preview)
        self.assertEqual(set(self.counts().values()), {0})
        with self.learning._using_connection() as db:
            self.assertIsNone(db.execute("SELECT published_result_json FROM teacher_plan_previews").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM goal_commands WHERE scope_id='teacher_planning:publish'").fetchone()[0], 0)
        self.publish(preview=preview)
        self.assertEqual(self.counts()["learning_goals"], 1)

    def test_publish_locks_platform_before_learning_until_learning_commit(self):
        self.seed(per_type=1)
        preview = self.planning.preview("teacher", self.config)
        separate = PlatformStore(self.platform.db_path, timeout=.05)
        connection, create_task = self.learning._using_connection, self.learning.create_task
        checkpoints = []
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(self.platform, "timeout", .05):
            def assert_membership_write_blocked():
                # The shared store uses a different thread-local connection; the
                # second store also proves this is a SQLite lock, not a Python lock.
                for store in (self.platform, separate):
                    try:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            executor.submit(store.delete_class_membership, self.classroom["id"], "c").result(timeout=5)
                    finally:
                        # Keep the fixture valid when demonstrating the old bug.
                        if self.platform.get_class_membership(self.classroom["id"], "c") is None:
                            self.platform.create_class_membership(self.classroom["id"], "c")

            @contextmanager
            def observe_commit(*, write=False):
                outer = getattr(self.learning._local, "connection", None) is None
                if outer and write:
                    assert_membership_write_blocked()
                    checkpoints.append("before_learning_lock")
                with connection(write=write) as db:
                    yield db
                    if outer and write:
                        assert_membership_write_blocked()
                        checkpoints.append("before_learning_commit")

            def observe_freeze(*args, **kwargs):
                task = create_task(*args, **kwargs)
                if task["student_id"] == "a":
                    assert_membership_write_blocked()
                    checkpoints.append("between_students")
                return task

            with patch.object(self.learning, "_using_connection", side_effect=observe_commit), \
                    patch.object(self.learning, "create_task", side_effect=observe_freeze):
                self.publish(preview=preview)
        self.assertEqual(checkpoints, ["before_learning_lock", "between_students", "before_learning_commit"])
        with self.learning.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM learning_tasks").fetchone()[0], 2)
        self.assertTrue(separate.delete_class_membership(self.classroom["id"], "c"))

    def test_publish_cannot_join_an_outer_learning_transaction(self):
        self.seed(per_type=1)
        preview = self.planning.preview("teacher", self.config)
        # Joining a caller's learning transaction would invert lock acquisition
        # and release the platform guard before that caller commits its tasks.
        with self.learning._using_connection(write=True):
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                self.publish(preview=preview)
        self.assertEqual(self.counts()["learning_goals"], 0)
        self.publish(preview=preview)

    def test_competing_quarantine_commits_before_publish_lock_is_seen(self):
        self.seed()
        preview = self.planning.preview("teacher", self.config)
        selected = preview["questions"][0]
        entered = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.learning._using_connection(write=True):
                self.learning.set_question_version_status("teacher", selected["question_id"], selected["version"], "quarantined")
                def publish_after_writer():
                    entered.set()
                    return self.planning.publish("teacher", self.config, preview_token=preview["preview_token"], request_id="race")
                future = executor.submit(publish_after_writer)
                self.assertTrue(entered.wait(timeout=5))
            with self.assertRaises(PlanningConflict):
                future.result(timeout=15)
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_schedule_deadline_retry_policies_and_expired_times(self):
        self.seed()
        config = {**self.config, "scheduled_at": time.time() + 120, "allow_late": True,
                  "allow_retries": True, "allow_retry_after_deadline": True}
        result = self.publish(config)
        self.assertEqual(result["status"], "scheduled")
        task = self.learning.list_tasks("a", goal_id=result["goal_id"])[0]
        self.assertEqual(task["scheduled_at"], config["scheduled_at"])
        self.assertEqual(task["deadline_at"], config["deadline_at"])
        self.assertEqual(task["questions"], [])
        with self.learning._using_connection() as db:
            policy = json.loads(db.execute("SELECT policy_json FROM round_policies WHERE round_id=?", (result["round_id"],)).fetchone()[0])
        self.assertEqual(policy["common_question_ratio"], 50)
        self.assertTrue(policy["allow_retry_after_deadline"])
        for change in ({"deadline_at": time.time() - 1}, {"scheduled_at": time.time() - 1}):
            self.assertFalse(self.planning.preview("teacher", {**self.config, **change})["ready"])

    def test_drafts_survive_restart_preserve_config_and_revalidate_owner_permissions(self):
        saved = self.planning.save_draft("teacher", self.config, request_id="draft-save")
        again = TeacherPlanning(LearningStore(self.learning.db_path, access_checker=self.runtime.check_learning_access), self.platform, self.content)
        self.assertEqual(saved, again.save_draft("teacher", self.config, request_id="draft-save"))
        self.assertEqual(again.list_drafts("teacher", class_id=self.classroom["id"])["items"], [saved])
        self.assertEqual(saved["config"], self.config)
        with self.assertRaises(IdempotencyConflict):
            again.save_draft("teacher", {**self.config, "title": "edited"}, request_id="draft-save")
        with self.assertRaises(PermissionError):
            again.list_drafts("reader", class_id=self.classroom["id"])
        self.platform.set_class_collaborator(self.classroom["id"], "other", "manage", granted_by="teacher")
        self.platform.set_course_collaborator(self.course["id"], "other", "read", granted_by="teacher")
        self.assertEqual(again.list_drafts("other", class_id=self.classroom["id"])["items"], [])
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_prepare_without_sources_or_external_permission_preserves_draft_only(self):
        prepared = self.planning.prepare("teacher", self.config, request_id="prepare-no-sources")
        self.assertEqual(prepared["status"], "source_required")
        self.assertFalse(prepared["auto_publish"])
        self.assertEqual(prepared["jobs"], [])
        self.assertEqual(prepared["draft"]["config"], self.config)
        _, source = self.material()
        prepared = self.planning.prepare("teacher", self.config, request_id="no-external", source_block_ids=[source["id"]])
        self.assertEqual(prepared["status"], "source_required")
        self.assertEqual(self.content.list_pending_jobs(), [])
        self.assertEqual(self.counts()["learning_goals"], 0)
        self.seed()
        ready = self.planning.prepare("teacher", self.config, request_id="ready-no-publish")
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(ready["auto_publish"])
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_prepare_authorized_generation_is_durable_idempotent_and_never_publishes(self):
        _, source = self.material()
        args = {"request_id": "prepare-authorized", "source_block_ids": [source["id"]], "external_processing_allowed": True}
        result = self.planning.prepare("teacher", self.config, **args)
        self.assertEqual(result["status"], "generation_pending")
        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual({j["question_type"] for j in result["jobs"]}, {"single_choice", "fill", "judge"})
        self.assertEqual(self.planning.prepare("teacher", self.config, **args), result)
        self.assertEqual(len(self.content.list_pending_jobs()), 3)
        self.assertEqual(self.counts()["learning_goals"], 0)
        for job in self.content.list_pending_jobs():
            self.assertEqual(job["input"]["source_block_ids"], [source["id"]])
            self.assertTrue(job["input"]["external_processing_allowed"])
            self.assertEqual(sum(job["input"]["question_types"].values()), job["input"]["count"])
            self.assertEqual(len(job["input"]["question_types"]), 1)
            self.assertNotIn("difficulty", job["input"])

    def test_prepare_passes_constraints_and_wrong_generated_type_is_quarantined(self):
        point, _, source = self.knowledge()
        config = {**self.config, "difficulty": "hard", "knowledge_points": [point["id"]]}
        prepared = self.planning.prepare("teacher", config, request_id="constrained-prepare",
            source_block_ids=[source["id"]], external_processing_allowed=True)
        for job in self.content.list_pending_jobs():
            self.assertEqual(job["input"]["difficulty"], "hard")
            self.assertEqual(job["input"]["knowledge_points"], [point["id"]])
            self.assertEqual(sum(job["input"]["question_types"].values()), 1)
        spec = next(job for job in prepared["jobs"] if job["question_type"] == "single_choice")
        bank = QuestionBank(self.content, self.learning)
        # No model call: feed a wrong-type local result through the real bank validator.
        result = bank.apply_result(spec["id"], "generate", {"questions": [{
            "type": "judge", "stem": "A strictly wrong requested type", "options": [], "answer": True,
            "points": 1, "rubric": [], "difficulty": "hard", "knowledge_points": [point["id"]], "source_refs": ["B1"],
        }]}, expected_revision=1)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.learning.list_questions(self.course["id"], "teacher"), [])
        self.assertEqual(self.counts()["learning_goals"], 0)

    def test_prepare_does_not_generate_for_unconfirmed_knowledge_or_invalid_times(self):
        _, source = self.material()
        for index, changes in enumerate(({"knowledge_points": ["unknown-point"]}, {"deadline_at": time.time() - 1})):
            prepared = self.planning.prepare("teacher", {**self.config, **changes}, request_id=f"invalid-{index}",
                source_block_ids=[source["id"]], external_processing_allowed=True)
            self.assertEqual(prepared["status"], "source_required")
            self.assertEqual(prepared["jobs"], [])
            self.assertFalse(prepared["preview"]["ready"])
        self.assertEqual(self.content.list_pending_jobs(), [])

    def test_prepare_crash_between_intent_and_jobs_recovers_same_job_ids(self):
        _, source = self.material()
        args = {"request_id": "prepare-recovery", "source_block_ids": [source["id"]], "external_processing_allowed": True}
        with patch.object(self.content, "get_job", side_effect=RuntimeError("after content commit")):
            with self.assertRaises(RuntimeError):
                self.planning.prepare("teacher", self.config, **args)
        with self.content.connect() as db:
            before = [row[0] for row in db.execute("SELECT id FROM content_jobs")]
        self.assertEqual(len(before), 1)
        result = self.planning.prepare("teacher", self.config, **args)
        self.assertIn(before[0], [job["id"] for job in result["jobs"]])
        self.assertEqual(len(self.content.list_pending_jobs()), 3)
        self.assertEqual(len(self.planning.list_drafts("teacher", class_id=self.classroom["id"])["items"]), 1)

    def test_partial_grading_is_pending_and_completed_first_attempt_is_real_evidence(self):
        point, _, _ = self.knowledge()
        self.seed(knowledge_points=[point["id"]])
        result = self.publish()
        task = self.learning.list_tasks("a", goal_id=result["goal_id"])[0]
        lease = self.learning.acquire_draft_lease(task["id"], "a", "test-device")
        answers = {q["id"]: {"single_choice": 1, "fill": "word", "judge": True}[q["type"]] for q in task["questions"]}
        saved = self.learning.save_draft(task["id"], "a", answers, expected_version=lease["task_version"], request_id="answer-save",
                                         lease_token=lease["lease_token"], fence_token=lease["fence_token"])
        submitted = self.learning.submit_attempt(task["id"], "a", expected_version=saved["version"], request_id="answer-submit",
                                                 lease_token=lease["lease_token"], fence_token=lease["fence_token"])
        attempt = submitted["attempts"][0]["id"]
        outbox = self.learning.list_grading_outbox()[0]
        self.learning.mark_grading_outbox(outbox["job_id"], "dispatched", fence_token=1)
        for index, question in enumerate(task["questions"]):
            self.learning.apply_grading_result(attempt, question["id"], {"score": 0}, expected_revision=1,
                                                job_id=outbox["job_id"], fence_token=1)
            preview = self.planning.preview("teacher", self.config)
            suggestion = next(s for s in preview["suggestions"] if s["student_id"] == "a")
            if index < 2:
                self.assertEqual(suggestion["status"], "pending_evaluation")
            else:
                self.assertEqual(suggestion["status"], "graded_evidence")
                self.assertEqual((suggestion["graded_attempt_count"], suggestion["graded_question_count"], suggestion["score_ratio"]), (1, 3, 0))
                self.assertEqual(suggestion["weak_knowledge_points"], [point["id"]])
                self.assertNotIn("independent", suggestion)
                self.assertEqual(next(s for s in preview["suggestions"] if s["student_id"] == "c")["status"], "pending_evaluation")

    def test_suggestions_resolve_unique_legacy_aliases_and_skip_ambiguous_or_private_points(self):
        coded, _, source = self.knowledge()
        titled = self.content.add_knowledge_point(self.course["id"], "title-code", "Legacy title",
            created_by="teacher", source_block_ids=[source["id"]])
        self.content.confirm_curriculum("knowledge", titled["id"], actor_id="teacher")
        self.question("legacy-choice", knowledge_points=[coded["code"]])
        self.question("legacy-fill", kind="fill", knowledge_points=[titled["title"]])
        self.question("plain-judge", kind="judge")
        publication = self.publish()
        task = self.learning.list_tasks("a", goal_id=publication["goal_id"])[0]
        lease = self.learning.acquire_draft_lease(task["id"], "a", "alias-device")
        answers = {q["id"]: {"single_choice": 1, "fill": "word", "judge": True}[q["type"]] for q in task["questions"]}
        saved = self.learning.save_draft(task["id"], "a", answers, expected_version=lease["task_version"], request_id="alias-save",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"])
        submitted = self.learning.submit_attempt(task["id"], "a", expected_version=saved["version"], request_id="alias-submit",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"])
        outbox = self.learning.list_grading_outbox()[0]
        self.learning.mark_grading_outbox(outbox["job_id"], "dispatched", fence_token=1)
        for question in task["questions"]:
            self.learning.apply_grading_result(submitted["attempts"][0]["id"], question["id"], {"score": 0},
                expected_revision=1, job_id=outbox["job_id"], fence_token=1)

        def suggestion():
            return next(item for item in self.planning.preview("teacher", self.config)["suggestions"] if item["student_id"] == "a")

        result = suggestion()
        self.assertEqual((result["graded_question_count"], result["score_ratio"]), (3, 0))
        self.assertEqual(result["weak_knowledge_points"], sorted([coded["id"], titled["id"]]))
        duplicate = self.content.add_knowledge_point(self.course["id"], "another-title-code", titled["title"],
            created_by="teacher", source_block_ids=[source["id"]])
        self.content.confirm_curriculum("knowledge", duplicate["id"], actor_id="teacher")
        self.assertEqual(suggestion()["weak_knowledge_points"], [coded["id"]])
        with patch.object(self.content, "confirmed_sources", side_effect=PermissionError("private source")):
            self.assertEqual(suggestion()["weak_knowledge_points"], [])
        with self.learning._using_connection() as db:
            frozen = {row["question_id"]: json.loads(row["frozen_json"]) for row in
                db.execute("SELECT question_id,frozen_json FROM task_items WHERE task_id=?", (task["id"],))}
        self.assertEqual(frozen["legacy-choice"]["knowledge_points"], [coded["code"]])
        self.assertEqual(frozen["legacy-fill"]["knowledge_points"], [titled["title"]])

    def test_strict_config_rejects_type_sum_duplicates_and_policy_inconsistency(self):
        for change in ({"question_count": 4}, {"student_ids": ["a", "a"]}, {"question_count": True},
                       {"allow_late": "false"}, {"common_level": "adaptive"}, {"deadline_at": True},
                       {"allow_retries": False, "allow_retry_after_deadline": True},
                       {"scheduled_at": self.config["deadline_at"]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                TeachingPlanConfig.model_validate({**self.config, **change})


class TeacherPlanningHTTPTests(PlanningFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))
        self.seed()
        self.as_user("teacher")

    def as_user(self, actor):
        self.client.cookies.clear()
        token = secrets.token_urlsafe(24)
        self.platform.create_session(token, actor, time.time() + 600)
        self.client.cookies.set("sid", token)

    def test_http_contract_draft_recovery_and_real_publish(self):
        preview = self.client.post("/api/v1/teaching-plans/preview", json=self.config)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["ready"])
        draft = self.client.post("/api/v1/teaching-plans/drafts", json={"config": self.config, "request_id": "http-draft"})
        self.assertEqual(draft.status_code, 201, draft.text)
        listing = self.client.get("/api/v1/teaching-plans/drafts", params={"class_id": self.classroom["id"]})
        self.assertEqual(listing.json()["items"], [draft.json()])
        payload = {"config": self.config, "preview_token": preview.json()["preview_token"], "request_id": "http-publish"}
        publish = self.client.post("/api/v1/teaching-plans/publish", json=payload)
        self.assertEqual(publish.status_code, 201, publish.text)
        self.assertEqual(self.client.post("/api/v1/teaching-plans/publish", json=payload).json(), publish.json())
        self.assertEqual(self.counts()["learning_tasks"], 2)

    def test_http_roles_scopes_and_strict_validation(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/teaching-plans/preview", json=self.config).status_code, 401)
        self.as_user("a")
        self.assertEqual(self.client.post("/api/v1/teaching-plans/preview", json=self.config).status_code, 403)
        self.as_user("reader")
        self.assertEqual(self.client.post("/api/v1/teaching-plans/preview", json=self.config).status_code, 404)
        self.as_user("teacher")
        self.assertEqual(self.client.post("/api/v1/teaching-plans/preview", json={**self.config, "student_ids": ["outsider"]}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/teaching-plans/preview", json={**self.config, "question_count": 7}).status_code, 422)

    def test_http_stale_and_prepare_do_not_publish(self):
        preview = self.client.post("/api/v1/teaching-plans/preview", json=self.config).json()
        selected = preview["questions"][0]
        self.learning.set_question_version_status("teacher", selected["question_id"], selected["version"], "quarantined")
        response = self.client.post("/api/v1/teaching-plans/publish", json={"config": self.config,
            "preview_token": preview["preview_token"], "request_id": "stale-http"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "preview_stale")
        missing = {**self.config, "difficulty": "hard"}
        prepared = self.client.post("/api/v1/teaching-plans/prepare", json={"config": missing, "request_id": "prepare-http"})
        self.assertEqual(prepared.status_code, 202, prepared.text)
        self.assertEqual(prepared.json()["status"], "source_required")
        self.assertFalse(prepared.json()["auto_publish"])
        self.assertEqual(self.counts()["learning_goals"], 0)


if __name__ == "__main__":
    unittest.main()
