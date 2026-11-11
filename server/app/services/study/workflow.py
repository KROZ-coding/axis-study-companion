from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException

from ...models import QuizSession
from ...models.study import Followup, LearningReview, StudentTask, SubmissionAttempt, TeacherFeedback
from ..grading_service import grade_session
from . import content, reports
from .context import dataset_version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _require(condition: bool, status: int, message: str) -> None:
    if not condition:
        raise HTTPException(status_code=status, detail=message)


def _entity(state: dict, collection: str, key: str, actor) -> dict:
    value = state[collection].get(key)
    _require(value is not None and value["class_id"] == actor.class_id, 404, "记录不存在于当前班级")
    return value


def _version(entity: dict, expected: int) -> None:
    _require(entity["version"] == expected, 409, "记录已更新，请重新加载后再操作")


def _unique(values: list[str], label: str) -> None:
    _require(len(values) == len(set(values)), 422, f"{label}不能重复")


class _IdempotentReplay(Exception):
    def __init__(self, response):
        self.response = deepcopy(response)


def _historical_snapshot(review: dict, followup: dict) -> dict:
    snapshot = deepcopy(review["snapshot"])
    student_ids = followup["student_ids"]
    kp_id = followup["knowledge_point_id"]
    snapshot["scope"] = {
        "topic": review["topic"], "student_ids": list(student_ids), "knowledge_point_ids": [kp_id],
        "date_from": snapshot["scope"].get("date_from"), "date_to": snapshot["scope"].get("date_to"),
    }
    snapshot["students"] = [row for row in snapshot["students"] if row["id"] in student_ids]
    snapshot["learning_contents"] = [row for row in snapshot["learning_contents"] if row["id"] == kp_id]
    for key in ("learning_analysis", "learning_recommendations"):
        snapshot[key] = [row for row in snapshot[key]
                         if row["student_id"] in student_ids and row["knowledge_point_id"] == kp_id]
    snapshot["records"] = [row for row in snapshot["records"] if row["sid"] in student_ids and row["kp"] == kp_id]
    topics = {row["topic"] for row in snapshot["learning_contents"]}
    snapshot["materials"] = [row for row in snapshot["materials"]
                             if row.get("kp") == kp_id or (row.get("kp") is None and row["topic"] in topics)]
    return snapshot


class TeachingService:
    def __init__(self, store, data: dict, actor):
        self.store = store
        self.data = data
        self.actor = actor

    def _mutate(self, operation: str, payload, action):
        body = payload.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps({"operation": operation, "body": body}, sort_keys=True).encode()).hexdigest()
        key = f"{self.actor.class_id}:{self.actor.actor_id}:{payload.request_id}"
        try:
            with self.store.teaching_transaction() as state:
                previous = state["idempotency"].get(key)
                if previous:
                    _require(previous["digest"] == digest, 409, "该请求编号已用于其他操作，请使用新的请求编号")
                    # Leave the transaction without saving an already committed result.
                    raise _IdempotentReplay(previous["response"])
                result = action(state)
                response = deepcopy(result)
                state["idempotency"][key] = {"digest": digest, "response": response}
                return deepcopy(response)
        except _IdempotentReplay as replay:
            return replay.response

    def list_reviews(self) -> list[dict]:
        state = self.store.teaching_snapshot()
        return [deepcopy(row) for row in state["reviews"].values() if row["class_id"] == self.actor.class_id]

    def get_review(self, review_id: str) -> dict:
        return deepcopy(_entity(self.store.teaching_snapshot(), "reviews", review_id, self.actor))

    def create_review(self, payload) -> dict:
        def action(state):
            scope = content.resolve_scope(
                self.data, topic=payload.topic, student_ids=payload.student_ids,
                kp_ids=payload.knowledge_point_ids,
                date_from=payload.date_from.isoformat() if payload.date_from else None,
                date_to=payload.date_to.isoformat() if payload.date_to else None,
            )
            snapshot = reports.export_payload(self.data, scope)
            student_ids = [row["id"] for row in snapshot["students"]]
            kp_ids = [row["id"] for row in snapshot["learning_contents"]]
            _require(bool(student_ids) and bool(kp_ids), 422, "回顾必须包含学生和知识点")
            review = asdict(LearningReview(
                _id("review"), self.actor.class_id, payload.title, self.actor.actor_id, _now(),
                payload.topic, student_ids, kp_ids, dataset_version(self.data), snapshot,
            ))
            state["reviews"][review["id"]] = review
            return review
        return self._mutate("create_review", payload, action)

    def _validate_followup(self, state: dict, review_id: str, payload) -> None:
        review = _entity(state, "reviews", review_id, self.actor)
        for values, label in ((payload.student_ids, "学生"), (payload.material_ids, "材料"), (payload.question_ids, "题目")):
            _unique(values, label)
        _require(set(payload.student_ids).issubset(review["student_ids"]), 422, "目标学生必须属于该学习回顾范围")
        _require(payload.knowledge_point_id in review["knowledge_point_ids"], 422, "知识点必须属于该学习回顾范围")
        kp = next((row for row in self.data["kps"] if row["id"] == payload.knowledge_point_id), None)
        _require(kp is not None, 422, "当前数据源中该知识点已不可用")
        for material_id in payload.material_ids:
            material = content.get_material(self.data, material_id)
            _require(material.get("kp") == kp["id"] or (not material.get("kp") and material.get("topic") == kp["topic"]),
                     422, "指定材料不适用于该知识点")
        practices = {row["id"]: key for key, row in self.data["practice"].items()}
        for question_id in payload.question_ids:
            _require(practices.get(question_id) == kp["id"], 422, "验证题不适用于该知识点")
            content.get_practice(self.data, question_id, include_answer=True)

    def create_followup(self, payload) -> dict:
        def action(state):
            self._validate_followup(state, payload.review_id, payload)
            row = asdict(Followup(
                id=_id("followup"), class_id=self.actor.class_id, review_id=payload.review_id,
                knowledge_point_id=payload.knowledge_point_id, title=payload.title, goal=payload.goal,
                instructions=payload.instructions, completion_condition=payload.completion_condition,
                student_ids=list(payload.student_ids), material_ids=list(payload.material_ids),
                question_ids=list(payload.question_ids), created_by=self.actor.actor_id, created_at=_now(),
            ))
            state["followups"][row["id"]] = row
            return row
        return self._mutate("create_followup", payload, action)

    def update_followup(self, followup_id: str, payload) -> dict:
        def action(state):
            row = _entity(state, "followups", followup_id, self.actor)
            _version(row, payload.expected_version)
            _require(row["status"] == "draft", 409, "已发布安排不能修改，请建立新的安排")
            self._validate_followup(state, row["review_id"], payload)
            fields = payload.model_dump(exclude={"request_id", "expected_version"})
            row.update(fields)
            row["version"] += 1
            return row
        return self._mutate(f"update_followup:{followup_id}", payload, action)

    def publish_followup(self, followup_id: str, payload) -> dict:
        def action(state):
            row = _entity(state, "followups", followup_id, self.actor)
            _version(row, payload.expected_version)
            _require(row["status"] == "draft", 409, "只有草稿可以发布")
            from types import SimpleNamespace
            self._validate_followup(state, row["review_id"], SimpleNamespace(**row))
            review = _entity(state, "reviews", row["review_id"], self.actor)
            history = _historical_snapshot(review, row)
            row["frozen_content"] = {
                "review_id": review["id"], "review_version": review["version"], "data_version": review["data_version"],
                "content_version": dataset_version(self.data),
                "materials": [deepcopy(content.get_material(self.data, key)) for key in row["material_ids"]],
                "questions": [deepcopy(content.get_practice(self.data, key, include_answer=True)) for key in row["question_ids"]],
                "basis": history["learning_analysis"], "historical_snapshot": history,
                "sample_label": self.data["sampleLabel"],
                "diagnostic_boundary": "历史回顾保持原范围；阅读、规则评分与任务完成不表示掌握度提高。",
            }
            row.update(status="published", published_at=_now(), version=row["version"] + 1)
            for student_id in row["student_ids"]:
                task = asdict(StudentTask(_id("task"), row["class_id"], row["id"], student_id))
                state["tasks"][task["id"]] = task
            return row
        return self._mutate(f"publish_followup:{followup_id}", payload, action)

    def close_followup(self, followup_id: str, payload) -> dict:
        def action(state):
            row = _entity(state, "followups", followup_id, self.actor)
            _version(row, payload.expected_version)
            _require(row["status"] == "published", 409, "只有已发布安排可以关闭")
            row.update(status="closed", closed_at=_now(), version=row["version"] + 1)
            return row
        return self._mutate(f"close_followup:{followup_id}", payload, action)

    def list_followups(self) -> list[dict]:
        state = self.store.teaching_snapshot()
        return [self._followup_view(state, row) for row in state["followups"].values()
                if row["class_id"] == self.actor.class_id]

    def _followup_view(self, state, row):
        result = deepcopy(row)
        tasks = [task for task in state["tasks"].values() if task["followup_id"] == row["id"]]
        result["progress"] = {key: sum(task["status"] == key for task in tasks)
                              for key in ("not_started", "in_progress", "submitted", "needs_retry", "completed")}
        result["tasks"] = [self._task_view(state, task) for task in tasks]
        return result

    def get_followup(self, followup_id: str) -> dict:
        state = self.store.teaching_snapshot()
        return self._followup_view(state, _entity(state, "followups", followup_id, self.actor))

    def _task(self, state, task_id):
        task = _entity(state, "tasks", task_id, self.actor)
        if self.actor.role == "student":
            _require(task["student_id"] == self.actor.student_id, 404, "该任务未分配给当前学生")
        return task

    def _task_view(self, state, task):
        row = state["followups"][task["followup_id"]]
        frozen = row["frozen_content"]
        questions = []
        for original in frozen["questions"]:
            question = {key: deepcopy(original[key]) for key in ("id", "stem", "options")}
            if task["attempt_ids"]:
                question.update(answer=original["answer"], why=original["why"])
            questions.append(question)
        public_content = {
            key: deepcopy(frozen[key]) for key in ("review_id", "review_version", "data_version", "content_version",
                                                   "materials", "sample_label", "diagnostic_boundary")
        }
        public_content["questions"] = questions
        public_content["basis"] = [deepcopy(item) for item in frozen["basis"] if item["student_id"] == task["student_id"]]
        return {
            **deepcopy(task), "followup_status": row["status"], "title": row["title"], "goal": row["goal"],
            "instructions": row["instructions"], "completion_condition": row["completion_condition"],
            "content": public_content,
            "attempts": [deepcopy(state["attempts"][key]) for key in task["attempt_ids"]],
            "feedback": [deepcopy(state["feedback"][key]) for key in task["feedback_ids"]],
        }

    def list_tasks(self) -> list[dict]:
        state = self.store.teaching_snapshot()
        return [self._task_view(state, row) for row in state["tasks"].values()
                if row["class_id"] == self.actor.class_id and row["student_id"] == self.actor.student_id]

    def get_task(self, task_id: str) -> dict:
        state = self.store.teaching_snapshot()
        return self._task_view(state, self._task(state, task_id))

    def preview_task(self, followup_id: str, student_id: str) -> dict:
        state = self.store.teaching_snapshot()
        _entity(state, "followups", followup_id, self.actor)
        task = next((row for row in state["tasks"].values()
                     if row["followup_id"] == followup_id and row["student_id"] == student_id), None)
        _require(task is not None, 404, "该安排尚未向此学生发布任务")
        return {"read_only": True, "preview_student_id": student_id, "task": self._task_view(state, task)}

    def _editable_task(self, state, task_id, expected):
        task = self._task(state, task_id)
        _version(task, expected)
        row = _entity(state, "followups", task["followup_id"], self.actor)
        _require(row["status"] == "published", 409, "安排已关闭，无法继续保存或提交")
        _require(task["status"] in {"not_started", "in_progress", "needs_retry"}, 409, "本次作答已提交，请等待教师反馈")
        return task, row

    def start_task(self, task_id, payload):
        def action(state):
            task, _ = self._editable_task(state, task_id, payload.expected_version)
            if task["status"] == "not_started":
                task["status"] = "in_progress"
            task.update(version=task["version"] + 1, updated_at=_now())
            return self._task_view(state, task)
        return self._mutate(f"start_task:{task_id}", payload, action)

    def save_draft(self, task_id, payload):
        def action(state):
            task, row = self._editable_task(state, task_id, payload.expected_version)
            questions = {question["id"]: question for question in row["frozen_content"]["questions"]}
            _require(set(payload.answers).issubset(questions), 422, "草稿包含任务外的题目")
            for key, answer in payload.answers.items():
                _require(type(answer) is int and 0 <= answer < len(questions[key]["options"]), 422, "作答选项无效")
            _unique(payload.read_material_ids, "阅读材料")
            _require(set(payload.read_material_ids).issubset(row["material_ids"]), 422, "阅读确认包含任务外的材料")
            task.update(draft_answers=deepcopy(payload.answers), read_material_ids=list(payload.read_material_ids),
                        version=task["version"] + 1, updated_at=_now())
            if task["status"] == "not_started":
                task["status"] = "in_progress"
            return self._task_view(state, task)
        return self._mutate(f"save_draft:{task_id}", payload, action)

    def submit_task(self, task_id, payload):
        def action(state):
            task, row = self._editable_task(state, task_id, payload.expected_version)
            _require(set(task["draft_answers"]) == set(row["question_ids"]), 422, "请先保存全部验证题的作答")
            _require(set(task["read_material_ids"]) == set(row["material_ids"]), 422, "请先确认已阅读全部指定材料")
            questions = [{"id": question["id"], "type": "single_choice", "stem": question["stem"],
                          "options": question["options"], "answer": question["answer"],
                          "knowledge_points": [row["knowledge_point_id"]], "score": 1}
                         for question in row["frozen_content"]["questions"]]
            session = QuizSession(_id("grading"), task["student_id"], row["class_id"], questions)
            grading = asdict(grade_session(session, deepcopy(task["draft_answers"])))
            grading["graded_at"] = grading["graded_at"].isoformat()
            grading["boundary"] = "仅为本次验证题规则评分，等待教师处理，不更新历史诊断。"
            attempt = asdict(SubmissionAttempt(
                _id("attempt"), task["id"], task["student_id"], len(task["attempt_ids"]) + 1, _now(),
                deepcopy(task["draft_answers"]), list(task["read_material_ids"]), grading,
            ))
            state["attempts"][attempt["id"]] = attempt
            task["attempt_ids"].append(attempt["id"])
            task.update(status="submitted", version=task["version"] + 1, updated_at=_now())
            return self._task_view(state, task)
        return self._mutate(f"submit_task:{task_id}", payload, action)

    def review_task(self, task_id, payload):
        def action(state):
            task = self._task(state, task_id)
            _version(task, payload.expected_version)
            _require(task["status"] == "submitted", 409, "只有待反馈提交可以处理")
            _require(task["attempt_ids"][-1] == payload.attempt_id, 409, "请针对最新一次提交给出反馈")
            row = _entity(state, "followups", task["followup_id"], self.actor)
            _require(payload.decision != "needs_retry" or row["status"] == "published", 409, "安排已关闭，不能要求再次提交")
            feedback = asdict(TeacherFeedback(_id("feedback"), task["id"], payload.attempt_id,
                                             self.actor.actor_id, payload.decision, payload.comment, _now()))
            state["feedback"][feedback["id"]] = feedback
            task["feedback_ids"].append(feedback["id"])
            task.update(status=payload.decision, version=task["version"] + 1, updated_at=_now())
            if payload.decision == "needs_retry":
                task["draft_answers"] = {}
            return self._task_view(state, task)
        return self._mutate(f"review_task:{task_id}", payload, action)

    def followup_report(self, followup_id):
        state = self.store.teaching_snapshot()
        row = _entity(state, "followups", followup_id, self.actor)
        review = _entity(state, "reviews", row["review_id"], self.actor)
        history = deepcopy(row["frozen_content"].get("historical_snapshot")) or _historical_snapshot(review, row)
        return {
            "class_id": row["class_id"], "review_id": review["id"], "review_version": review["version"],
            "data_version": review["data_version"], "scope": history["scope"], "historical_snapshot": history,
            "followup": self._followup_view(state, row),
            "boundary": "历史依据与本次任务结果分列；任务完成、阅读确认和单题答对不代表掌握度提高。",
        }

    def dashboard(self):
        state = self.store.teaching_snapshot()
        followups = [row for row in state["followups"].values() if row["class_id"] == self.actor.class_id]
        tasks = [task for task in state["tasks"].values() if task["class_id"] == self.actor.class_id]
        return {"published_count": sum(row["status"] == "published" for row in followups),
                "pending_review_count": sum(task["status"] == "submitted" for task in tasks),
                "recent_reviews": [row for row in state["reviews"].values() if row["class_id"] == self.actor.class_id][-5:],
                "pending_tasks": [self._task_view(state, task) for task in tasks if task["status"] == "submitted"]}
