"""Teacher library/import regressions using disposable stores and structured fake results."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.content_store import ContentConflict, ContentStore
from app.job_queue import JobQueue
from app.learning_store import LearningStore
from app.platform_store import PlatformStore
from app.routers.v1_materials import router
from app.services.material_parser import MaterialParseError, parse_material
from app.services.question_bank import QuestionBank, QuestionValidationError, question_fingerprints, validate_question
from app.services.question_library import (
    list_question_library, parse_question_import, question_detail, question_import_template, question_job_view,
)


def question(kind="single_choice", stem="二加三等于多少？", **metadata):
    value = {"type": kind, "stem": stem, "options": [], "answer": "五", "explanation": "参考解释 SECRET_EXPLANATION", "points": 1, "rubric": []}
    if kind == "single_choice":
        value.update(options=["四", "五"], answer=1)
    elif kind == "judge":
        value["answer"] = True
    elif kind == "fill":
        value.update(answer=["五", "5"], answer_format="text")
    elif kind == "text":
        value["rubric"] = [{"point": "正确说明原因 SECRET_RUBRIC", "score_units": 100}]
    value.update(metadata)
    return value


class QuestionLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="question-library-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.platform = PlatformStore(root / "platform.sqlite3")
        self.content = ContentStore(root / "content.sqlite3")
        self.owner = self.platform.create_user("owner", "负责人", "fixture-hash", "teacher")
        self.reader = self.platform.create_user("reader", "协作教师", "fixture-hash", "teacher")
        self.other = self.platform.create_user("other", "其他教师", "fixture-hash", "teacher")
        self.student = self.platform.create_user("student", "学生", "fixture-hash", "student")
        self.platform.create_course("数学", "2026", self.owner["id"], course_id="course")
        self.platform.create_class("course", "甲班", owner_teacher_id=self.owner["id"], class_id="class-a")
        self.platform.create_class("course", "乙班", owner_teacher_id=self.other["id"], class_id="class-b")
        self.platform.set_course_collaborator("course", self.reader["id"], "read", granted_by=self.owner["id"])
        self.platform.create_course_membership("course", self.student["id"])
        self.platform.create_class_membership("class-a", self.student["id"])
        self.learning = LearningStore(root / "learning.sqlite3", access_checker=self._access)
        self.queue = JobQueue(root / "jobs.sqlite3")
        self.bank = QuestionBank(self.content, self.learning)
        self.gateway = Mock()
        self.runtime = SimpleNamespace(platform=self.platform, content=self.content, learning=self.learning, queue=self.queue, gateway=self.gateway)
        app = FastAPI()
        app.state.runtime = self.runtime
        app.include_router(router)
        self.client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        for name in ("owner", "reader", "other", "student"):
            self.platform.create_session("session-" + name, getattr(self, name)["id"], 9_999_999_999)
        self.login("owner")
        data = "二加三等于五，偶数可被二整除。课程来源。".encode()
        material = self.content.import_material(course_id="course", owner_id=self.owner["id"], filename="lesson.txt", content=data,
            parsed=parse_material("lesson.txt", data), visibility="shared", license_code="own", rights_confirmed=True,
            allow_generation=True, allow_retrieval=True, external_processing_allowed=True)
        self.source = self.content.list_blocks(material["id"])[0]
        self.content.confirm_blocks(material["id"], [self.source["id"]], actor_id=self.owner["id"], expected_revision=1, course_owner_confirmation=True)
        self.knowledge = self.content.add_knowledge_point("course", "K-ADD", "加法", created_by=self.owner["id"], source_block_ids=[self.source["id"]])
        self.content.confirm_curriculum("knowledge", self.knowledge["id"], actor_id=self.owner["id"])
        self.draft_knowledge = self.content.add_knowledge_point("course", "K-DRAFT", "未确认知识", created_by=self.owner["id"], source_block_ids=[self.source["id"]])

    def _access(self, actor, scope, *, write=False):
        user = self.platform.get_user(actor)
        if not user or user["status"] != "active":
            return False
        if user["role"] == "student":
            return scope.get("student_id") == actor
        if scope.get("class_id"):
            return self.platform.can_access_class(actor, scope["class_id"], teaching=write)
        if scope.get("course_id") and scope.get("kind") in {"question", "question_bank"}:
            return self.platform.can_access_course(actor, scope["course_id"], teaching=write)
        return scope.get("owner_id") == actor

    def login(self, name):
        self.client.cookies.clear()
        if name:
            self.client.cookies.set("sid", "session-" + name)

    def values(self, **extra):
        return {"course_id": "course", "owner_id": self.owner["id"], "source_block_ids": [self.source["id"]],
                "external_processing_allowed": True, **extra}

    def apply(self, job, data):
        return self.bank.apply_result(job["id"], job["stage"], {"data": data, "text": "fixture", "model": "fixture",
            "request_sent": False, "usage": None}, expected_revision=job["revision"])

    def finish(self, job, answer=1):
        job = self.apply(job, {"solutions": [{"index": 0, "answer": answer, "solvable": True, "reasoning": "独立验证"}]})
        return self.apply(job, {"comparisons": [{"index": 0, "agrees": True, "valid": True, "reason": "有来源支持"}]})

    def seed(self, qid, value, version=1, status="approved"):
        exact, family = question_fingerprints(value)
        return self.learning.create_question_version(self.owner["id"], qid, version,
            {**value, "course_id": "course", "content_sha": exact, "family_sha": family}, status=status, course_id="course")

    def preview(self, values, filename="questions.json"):
        return self.client.post("/api/v1/courses/course/question-imports/preview", files={"file": (filename, json.dumps(values, ensure_ascii=False).encode())},
                                data={"source_block_ids": json.dumps([self.source["id"]])})

    def request_specs(self, key="stable-request"):
        self.seed("q-edit-target", question(stem="既有题目"))
        base = {"source_block_ids": [self.source["id"]], "external_processing_allowed": True, "request_id": key}
        return [
            ("candidate", "/api/v1/courses/course/question-candidates", {**base, "question": question(stem="手动题")}),
            ("generation", "/api/v1/courses/course/question-jobs", {**base, "count": 1, "instruction": "原始命题要求"}),
            ("edit", "/api/v1/courses/course/questions/q-edit-target/edit-jobs",
             {**base, "expected_version": 1, "question": question(stem="既有题目", explanation="编辑后的解释")}),
        ]

    def test_request_ids_replay_pending_and_completed_jobs_and_conflict_on_payload_changes(self):
        for operation, path, payload in self.request_specs():
            with self.subTest(operation=operation):
                first = self.client.post(path, json=payload)
                self.assertEqual(first.status_code, 202, first.text)
                job = first.json()["job"]
                second = self.client.post(path, json=dict(reversed(list(payload.items()))))
                self.assertEqual(second.status_code, 202, second.text)
                self.assertEqual(second.json()["job"]["id"], job["id"])
                self.assertEqual(second.json()["job"]["revision"], job["revision"])
                if operation == "generation":
                    job = self.apply(job, {"questions": [{**question(stem="生成题"), "source_refs": ["B1"]}]})
                finished = self.finish(job)
                self.assertEqual(finished["status"], "completed", finished.get("error"))
                # A new connection simulates process restart. No in-memory deduplication is involved.
                self.runtime.content = ContentStore(self.content.db_path)
                replay = self.client.post(path, json=payload)
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(replay.json()["job"]["id"], finished["id"])
                self.assertEqual(replay.json()["job"]["status"], "completed")
                self.assertEqual(replay.json()["job"]["revision"], finished["revision"])
                if operation == "candidate":
                    self.assertEqual(replay.json()["candidate_status"], "approved")
                changed = deepcopy(payload)
                if operation == "generation":
                    changed["instruction"] = "改动命题要求"
                else:
                    changed["question"]["explanation"] += "不同内容"
                conflict = self.client.post(path, json=changed)
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertEqual(conflict.json()["detail"]["code"], "content_conflict")
                # A different key is a new operation, so it must not bypass duplicate/latest checks.
                if operation != "generation":
                    new_request = self.client.post(path, json={**payload, "request_id": "different-request"})
                    self.assertEqual(new_request.status_code, 422 if operation == "candidate" else 409, new_request.text)
        with self.content.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM content_jobs").fetchone()[0], 3)
        self.gateway.complete.assert_not_called()

    def test_keyed_creation_is_single_across_concurrent_store_connections(self):
        self.seed("q-race-edit", question(stem="并发编辑原题"))
        base = self.values(request_id="concurrent-key")
        specs = [
            ("candidate", {**base, "question": question(stem="并发手工题")}),
            ("generation", {**base, "instruction": "并发生成"}),
            ("edit", {**base, "question_id": "q-race-edit", "expected_version": 1,
                      "question": question(stem="并发编辑原题", explanation="新解释")}),
        ]
        for operation, values in specs:
            with self.subTest(operation=operation):
                barrier = Barrier(4)
                def submit(_):
                    bank = QuestionBank(ContentStore(self.content.db_path), LearningStore(self.learning.db_path, access_checker=self._access))
                    barrier.wait(timeout=10)
                    return getattr(bank, "create_" + operation)(**values)["id"]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ids = list(pool.map(submit, range(4)))
                self.assertEqual(len(set(ids)), 1)
                with self.content.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM content_jobs WHERE id=?", (ids[0],)).fetchone()[0], 1)
        # Different payloads racing under one key have exactly one winner, not two jobs.
        barrier = Barrier(2)
        def compete(instruction):
            bank = QuestionBank(ContentStore(self.content.db_path), self.learning)
            barrier.wait(timeout=10)
            try:
                return bank.create_generation(**self.values(request_id="conflicting-race", instruction=instruction))["id"]
            except ContentConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ("甲", "乙")))
        self.assertEqual(results.count("conflict"), 1)
        with self.content.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM content_jobs").fetchone()[0], 4)

    def test_keyed_replays_recheck_course_class_and_source_permissions(self):
        specs = self.request_specs("permission-key")
        jobs = []
        for operation, path, body in specs:
            payload = {**body, "class_id": "class-a"}
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 202, response.text)
            jobs.append((operation, path, payload, response.json()["job"]["id"]))
        with self.content.transaction() as db:
            db.execute("UPDATE material_files SET external_processing_allowed=0 WHERE id=?", (self.source["file_id"],))
        for operation, path, payload, job_id in jobs:
            with self.subTest(operation=operation, revoked="external"):
                self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        with self.content.transaction() as db:
            db.execute("UPDATE material_files SET external_processing_allowed=1,allow_generation=0 WHERE id=?", (self.source["file_id"],))
        for _, path, payload, _ in jobs:
            self.assertEqual(self.client.post(path, json=payload).status_code, 403)
        with self.content.transaction() as db:
            db.execute("UPDATE material_files SET allow_generation=1 WHERE id=?", (self.source["file_id"],))
        with self.platform._using_connection(write=True) as db:
            db.execute("UPDATE classes SET owner_teacher_id=? WHERE id='class-a'", (self.other["id"],))
        for _, path, payload, _ in jobs:
            self.assertEqual(self.client.post(path, json=payload).status_code, 404)
        with self.platform._using_connection(write=True) as db:
            db.execute("UPDATE classes SET owner_teacher_id=? WHERE id='class-a'", (self.owner["id"],))
            db.execute("UPDATE courses SET teacher_id=? WHERE id='course'", (self.other["id"],))
        for _, path, payload, _ in jobs:
            self.assertEqual(self.client.post(path, json=payload).status_code, 404)
        with self.content.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM content_jobs").fetchone()[0], 3)
        self.gateway.complete.assert_not_called()

    def test_request_key_scope_validation_unkeyed_calls_and_planning_adapter_compatibility(self):
        for operation, path, payload in self.request_specs("scope-key"):
            with self.subTest(operation=operation):
                for invalid in ("", "bad key", "x" * 101, 7, True):
                    response = self.client.post(path, json={**payload, "request_id": invalid})
                    self.assertEqual(response.status_code, 422, response.text)
                unkeyed = {key: value for key, value in payload.items() if key != "request_id"}
                first = self.client.post(path, json=unkeyed)
                second = self.client.post(path, json={**unkeyed, "request_id": None})
                self.assertEqual(first.status_code, 202, first.text)
                self.assertEqual(second.status_code, 202, second.text)
                self.assertNotEqual(first.json()["job"]["id"], second.json()["job"]["id"])
        self.platform.set_course_collaborator("course", self.reader["id"], "manage", granted_by=self.owner["id"])
        first = self.bank.create_generation(**self.values(request_id="scope-key"))
        other_actor = self.bank.create_generation(**self.values(request_id="scope-key", owner_id=self.reader["id"]))
        self.assertNotEqual(first["id"], other_actor["id"])
        with self.assertRaises(ContentConflict):
            self.bank.create_generation(**self.values(request_id="scope-key", class_id="class-a"))
        self.platform.create_course("另一课程", "2026", self.owner["id"], course_id="other-course")
        data = b"Approved source for another course."
        material = self.content.import_material(course_id="other-course", owner_id=self.owner["id"], filename="other.txt", content=data,
            parsed=parse_material("other.txt", data), license_code="own", rights_confirmed=True, allow_generation=True,
            external_processing_allowed=True)
        source_id = self.content.list_blocks(material["id"])[0]["id"]
        self.content.confirm_blocks(material["id"], [source_id], actor_id=self.owner["id"], expected_revision=1)
        other_course = self.bank.create_generation(**self.values(request_id="scope-key", course_id="other-course", source_block_ids=[source_id]))
        self.assertNotEqual(first["id"], other_course["id"])
        from app.teacher_planning_store import GenerationIntentContent
        adapter = GenerationIntentContent(self.content, "planjob-compatible")
        planning = QuestionBank(adapter, self.learning)
        intent = planning.create_generation(**self.values())
        replay = planning.create_generation(**self.values())
        self.assertEqual(intent["id"], "planjob-compatible")
        self.assertEqual(replay["id"], intent["id"])
        self.gateway.complete.assert_not_called()

    def test_generation_metadata_is_canonical_and_model_contract_is_enforced(self):
        config = self.values(question_types={"single_choice": 1}, difficulty="easy", knowledge_points=["K-ADD"])
        job = self.bank.create_generation(**config)
        self.assertEqual(job["input"]["count"], 1)
        self.assertEqual(job["input"]["knowledge_points"], [self.knowledge["id"]])
        context = json.loads(self.bank.prepare_input(job)["messages"][1]["content"])
        self.assertEqual(context["knowledge_catalog"][0]["title"], "加法")
        for candidate in (question("judge", difficulty="easy", knowledge_points=[self.knowledge["id"]]),
                          question(difficulty="hard", knowledge_points=[self.knowledge["id"]]),
                          question(difficulty="easy", knowledge_points=[]),
                          question(difficulty="easy", knowledge_points=["任意标签"])):
            with self.subTest(candidate=candidate):
                current = self.bank.create_generation(**config)
                result = self.apply(current, {"questions": [{**candidate, "source_refs": ["B1"]}]})
                self.assertEqual(result["status"], "quarantined", result)
        valid = {**question(difficulty="easy", knowledge_points=["加法"]), "source_refs": ["B1"]}
        job = self.finish(self.apply(job, {"questions": [valid]}))
        self.assertEqual(job["status"], "completed", job.get("error"))
        ref = job["state"]["published_versions"][0]
        detail = question_detail(self.learning, "course", ref["question_id"], 1, self.owner["id"])
        self.assertEqual(detail["question"]["content"]["knowledge_points"], [self.knowledge["id"]])
        self.assertEqual(detail["question"]["content"]["difficulty"], "easy")
        self.assertEqual(detail["sources"][0]["block_id"], self.source["id"])
        self.assertEqual(detail["reviews"]["comparisons"][0]["stage"], "compare")

    def test_invalid_counts_and_unconfirmed_or_ambiguous_knowledge_are_rejected(self):
        for extra in ({"question_types": {"judge": True}}, {"question_types": {"judge": 2}, "count": 1},
                      {"question_types": {"essay": 1}}, {"knowledge_points": [self.draft_knowledge["id"]]},
                      {"knowledge_points": ["kp_another_course"]}, {"knowledge_points": ["K-ADD", "加法"]}):
            with self.subTest(extra=extra), self.assertRaises(QuestionValidationError):
                self.bank.create_generation(**self.values(**extra))
        another = self.content.add_knowledge_point("course", "K-ADD-SECOND", "加法", created_by=self.owner["id"], source_block_ids=[self.source["id"]])
        self.content.confirm_curriculum("knowledge", another["id"], actor_id=self.owner["id"])
        with self.assertRaises(QuestionValidationError):
            self.bank.create_candidate(**self.values(question=question(knowledge_points=["加法"])))
        self.assertEqual(self.content.list_pending_jobs(), [])

    def test_fill_format_and_text_rubric_survive_independent_review(self):
        for kind, answer in (("fill", "五"), ("text", "五")):
            with self.subTest(kind=kind):
                value = question(kind, stem="填空示例" if kind == "fill" else "解释五的来源", difficulty="medium", knowledge_points=[self.knowledge["id"]], answer_format="text")
                job = self.bank.create_candidate(**self.values(question=value))
                self.assertEqual(job["stage"], "review")
                prompt = self.bank.prepare_input(job)
                self.assertNotIn("SECRET_EXPLANATION", str(prompt))
                self.assertNotIn("SECRET_RUBRIC", str(prompt))
                finished = self.finish(job, answer)
                self.assertEqual(finished["status"], "completed", finished.get("error"))
                ref = finished["state"]["published_versions"][0]
                saved = self.learning.get_question(ref["question_id"], 1, self.owner["id"])
                self.assertEqual(saved["answer_format"], "text")
                self.assertNotIn("blank_count", saved)
                self.assertEqual(saved["provenance"]["origin"], "manual")
        for value in (question("text", rubric=[]), question("text", rubric=[{"point": "不足分值", "score_units": 50}]),
                      question("fill", blank_count=2), question("fill", answer_format={"blank_count": 2})):
            with self.assertRaises(QuestionValidationError):
                validate_question(value)

    def test_latest_version_filters_facets_pagination_and_class_scope(self):
        value = question(difficulty="easy", knowledge_points=[self.knowledge["id"]])
        self.seed("q-old", value)
        self.seed("q-old", {**value, "difficulty": "hard"}, version=2, status="retired")
        self.seed("q-visible", question("judge", stem="100% 的确定事实", difficulty="easy", knowledge_points=[self.knowledge["id"]]))
        self.seed("q-private-class", question(stem="乙班题目", class_id="class-b", difficulty="easy"))
        result = list_question_library(self.learning, "course", self.owner["id"], status="approved", limit=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["question_id"], "q-visible")
        self.assertEqual(result["facets"]["statuses"], [{"value": "approved", "count": 1}, {"value": "retired", "count": 1}])
        self.assertEqual(list_question_library(self.learning, "course", self.owner["id"], q="%", question_type="judge", difficulty="easy", knowledge_point=self.knowledge["id"])["total"], 1)
        self.assertEqual(list_question_library(self.learning, "course", self.owner["id"], offset=20)["items"], [])
        detail = question_detail(self.learning, "course", "q-old", 1, self.owner["id"])
        self.assertEqual([row["version"] for row in detail["versions"]], [2, 1])
        with self.assertRaises(LookupError):
            question_detail(self.learning, "course", "q-private-class", 1, self.owner["id"])
        with self.assertRaises(LookupError):
            list_question_library(self.learning, "course", self.student["id"])

    def test_usage_counts_real_frozen_version_and_graded_first_attempt(self):
        self.seed("q-used", question(points=2))
        owner, student = self.owner["id"], self.student["id"]
        self.learning.create_goal(owner, "练习", goal_id="goal", course_id="course", class_id="class-a")
        self.learning.create_round(owner, "goal", 1, round_id="round")
        self.learning.create_task(owner, student, "goal", "round", [{"question_id": "q-used", "version": 1}], task_id="task")
        lease = self.learning.acquire_draft_lease("task", student, "device", now=100)
        task = self.learning.save_draft("task", student, {"q-used": 1}, expected_version=lease["task_version"], request_id="save",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=101)
        submitted = self.learning.submit_attempt("task", student, expected_version=task["version"], request_id="submit",
            lease_token=lease["lease_token"], fence_token=lease["fence_token"], now=102)
        outbox = self.learning.list_grading_outbox(status="pending")[0]
        self.learning.mark_grading_outbox(outbox["job_id"], "dispatched", queue_job_id=outbox["job_id"], fence_token=1)
        self.learning.apply_grading_result(submitted["attempts"][0]["id"], "q-used", {"score_units": 100}, expected_revision=1,
            job_id=outbox["job_id"], fence_token=1)
        self.seed("q-used", question(points=2, explanation="新版"), version=2)
        usage = question_detail(self.learning, "course", "q-used", 1, owner)["usage"]
        self.assertEqual((usage["task_count"], usage["student_count"], usage["attempt_count"], usage["first_graded_count"]), (1, 1, 1, 1))
        self.assertEqual(usage["average_score"], 1)
        self.assertEqual(usage["average_score_rate"], 0.5)
        self.assertEqual(question_detail(self.learning, "course", "q-used", 2, owner)["usage"]["task_count"], 0)
        self.assertEqual(question_detail(self.learning, "course", "q-used", 1, self.reader["id"])["usage"]["task_count"], 0)

    def test_import_preview_is_local_isolates_rows_and_confirmation_is_idempotent(self):
        values = [question(knowledge_points=["K-ADD"]), question(), question("text", rubric=[]), question("judge", stem="零是偶数")]
        response = self.preview(values)
        self.assertEqual(response.status_code, 201, response.text)
        preview = response.json()
        self.assertEqual(preview["summary"], {"total": 4, "valid": 2, "invalid": 1, "duplicate": 1})
        self.assertEqual(preview["rows"][0]["question"]["knowledge_points"], [self.knowledge["id"]])
        self.assertEqual(self.content.list_pending_jobs(), [])
        self.assertEqual(list_question_library(self.learning, "course", self.owner["id"])["total"], 0)
        path = f"/api/v1/courses/course/question-imports/{preview['import_id']}/confirm"
        self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.post(path, json={"external_processing_allowed": "true"}).status_code, 422)
        accepted = self.client.post(path, json={"external_processing_allowed": True, "row_numbers": [1, 2, 3, 4]})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        result = accepted.json()
        self.assertEqual((result["accepted_count"], result["failed_count"]), (2, 2))
        self.assertTrue(all(job["stage"] == "review" for job in result["jobs"]))
        replay = self.client.post(path, json={"external_processing_allowed": True, "row_numbers": [4, 3, 2, 1]})
        self.assertEqual([job["id"] for job in replay.json()["jobs"]], [job["id"] for job in result["jobs"]])
        self.assertEqual(self.client.post(path, json={"external_processing_allowed": True, "row_numbers": [1]}).status_code, 409)
        failed = self.apply(result["jobs"][0], {"solutions": []})
        self.assertEqual(failed["status"], "quarantined")
        finished = self.finish(result["jobs"][1], True)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        ref = finished["state"]["published_versions"][0]
        saved = self.learning.get_question(ref["question_id"], 1, self.owner["id"])
        self.assertEqual(saved["provenance"]["row_number"], 4)
        self.assertEqual(saved["provenance"]["import_id"], preview["import_id"])
        self.gateway.complete.assert_not_called()

    def test_confirm_rechecks_database_duplicates_and_knowledge_status(self):
        preview = self.preview([question(), question("judge", stem="是否为整数", knowledge_points=["K-ADD"])]).json()
        self.seed("q-existing", question())
        with self.content.transaction() as db:
            db.execute("UPDATE knowledge_points SET status='draft' WHERE id=?", (self.knowledge["id"],))
        response = self.client.post(f"/api/v1/courses/course/question-imports/{preview['import_id']}/confirm", json={"external_processing_allowed": True})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual((response.json()["accepted_count"], response.json()["failed_count"]), (0, 2))
        self.assertEqual(self.content.list_pending_jobs(), [])

    def test_three_templates_round_trip_and_xlsx_formulas_are_rejected(self):
        for format in ("csv", "json", "xlsx"):
            with self.subTest(format=format):
                response = self.client.get("/api/v1/courses/course/question-imports/template", params={"format": format})
                self.assertEqual(response.status_code, 200, response.text if format != "xlsx" else "")
                rows = parse_question_import("questions." + format, response.content)
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertEqual(validate_question(row["question"])["knowledge_points"], [])
                upload = self.client.post("/api/v1/courses/course/question-imports/preview", files={"file": ("questions." + format, response.content)},
                    data={"source_block_ids": json.dumps([self.source["id"]])})
                self.assertEqual(upload.status_code, 201, upload.text)
                self.assertEqual(upload.json()["summary"]["valid"], 4)
        from openpyxl import load_workbook
        data, _, _ = question_import_template("xlsx")
        workbook = load_workbook(BytesIO(data))
        workbook.active["B2"] = "=1+1"
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        with self.assertRaises(MaterialParseError):
            parse_question_import("formula.xlsx", stream.getvalue())

    def test_routes_enforce_role_course_class_and_preview_ownership(self):
        preview = self.preview([question()]).json()
        self.seed("q-one", question())
        read_paths = ["/api/v1/courses/course/question-library", "/api/v1/courses/course/questions/q-one/versions/1",
                      "/api/v1/courses/course/question-jobs", "/api/v1/courses/course/question-imports/template"]
        for name, expected in ((None, 401), ("student", 403), ("other", 404)):
            self.login(name)
            for path in read_paths:
                self.assertEqual(self.client.get(path).status_code, expected, (name, path))
        self.login("reader")
        self.assertEqual(self.client.get(read_paths[0]).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/courses/course/question-imports/{preview['import_id']}").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/courses/course/question-candidates", json={"question": question(), "source_block_ids": [self.source["id"]], "external_processing_allowed": True}).status_code, 404)
        self.login("owner")
        self.assertEqual(self.client.get(read_paths[0], params={"class_id": "class-b"}).status_code, 404)
        self.gateway.complete.assert_not_called()

    def test_jobs_expose_real_queue_recovery_without_dispatching_get(self):
        response = self.client.post("/api/v1/courses/course/question-candidates", json={"question": question(), "source_block_ids": [self.source["id"]], "external_processing_allowed": True})
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()["job"]
        self.assertEqual(job["queue_status"], "not_dispatched")
        self.assertEqual(job["queue_job_id"], "content:" + job["id"])
        queue_id = job["queue_job_id"]
        self.queue.enqueue("batch", {"purpose": "generate", "resource_type": "content_job", "resource_id": job["id"], "course_id": "course", "owner_id": self.owner["id"]},
                           job_id=queue_id, steps=("review", "compare"), paid=True, available_at=100)
        claimed = self.queue.claim("worker", lease_seconds=1, now=101)
        self.queue.recover_expired(now=103)
        self.assertEqual(claimed.job_id, queue_id)
        projected = question_job_view(self.runtime, self.content.get_job(job["id"]))
        self.assertEqual(projected["queue_status"], "awaiting_confirmation")
        self.assertTrue(projected["can_resume"])
        self.assertTrue(projected["requires_paid_confirmation"])
        listing = self.client.get("/api/v1/courses/course/question-jobs", params={"status": "awaiting_confirmation"}).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["queue_job_id"], queue_id)
        self.assertEqual(self.client.get(f"/api/v1/courses/course/question-jobs/{job['id']}").status_code, 200)
        self.assertEqual(self.queue.get_job(queue_id).status, "awaiting_confirmation")
        self.gateway.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
