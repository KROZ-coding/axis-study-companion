"""Learning application operations layered on the existing transactional core."""

from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
import time
from uuid import uuid4


class LearningOperations:
    def _command(self, db, actor, scope, request_id, payload, action):
        if request_id is None:
            return action()
        key = self._request_id(request_id)
        digest = self._digest(payload)
        previous = db.execute("SELECT * FROM goal_commands WHERE actor_id=? AND scope_id=? AND request_id=?", (actor, scope, key)).fetchone()
        if previous:
            if previous["payload_hash"] != digest:
                from .learning_store import IdempotencyConflict
                raise IdempotencyConflict("请求编号已用于不同参数")
            return json.loads(previous["result_json"])
        result = action()
        db.execute("INSERT INTO goal_commands VALUES(?,?,?,?,?,?)", (actor, scope, key, digest, self._json(result), time.time()))
        return result

    def _task_row(self, db, task_id, actor_id):
        from .learning_store import TaskNotFound
        row = db.execute("SELECT t.*,g.owner_id,g.course_id,g.class_id,g.title goal_title FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id WHERE t.id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound("任务不存在")
        if self._access_checker is None:
            if actor_id not in (row["student_id"], row["owner_id"]):
                raise TaskNotFound("任务不存在")
        else:
            self._require_access(actor_id, {"kind": "task", **dict(row)}, write=False)
        return row

    def _goal_row(self, db, goal_id, actor_id):
        from .learning_store import GoalNotFound
        row = db.execute("SELECT * FROM learning_goals WHERE id=?", (goal_id,)).fetchone()
        if row is None:
            raise GoalNotFound("目标不存在")
        personal = db.execute("SELECT student_id FROM learning_tasks WHERE goal_id=? AND student_id=? LIMIT 1", (goal_id, actor_id)).fetchone()
        scope = {"kind": "goal", "goal_id": goal_id, **dict(row)}
        if personal:
            scope["student_id"] = actor_id
        if self._access_checker is None:
            if row["owner_id"] != actor_id and not personal:
                raise GoalNotFound("目标不存在")
        else:
            self._require_access(actor_id, scope)
        return row

    def _teacher_write(self, actor, row):
        from .learning_store import TaskNotFound
        scope = {"kind": "task" if "student_id" in row.keys() else "goal", **dict(row)}
        if "student_id" in scope and scope["student_id"] == actor:
            raise TaskNotFound("学生不能执行教师操作")
        if self._access_checker is None:
            if row["owner_id"] != actor:
                raise TaskNotFound("资源不存在")
        else:
            self._require_access(actor, scope, write=True)

    def create_goal(self, actor_id, title, description="", *, goal_id=None, course_id=None, class_id=None, request_id=None):
        scope = {"kind": "goal", "owner_id": actor_id, "course_id": course_id, "class_id": class_id}
        self._require_access(actor_id, scope, write=True)
        with self._using_connection(write=True) as db:
            return self._command(db, actor_id, "create_goal", request_id,
                {"title": title, "description": description, "course_id": course_id, "class_id": class_id, "goal_id": goal_id},
                lambda: super(LearningOperations, self).create_goal(actor_id, title, description, goal_id=goal_id, course_id=course_id, class_id=class_id))

    def update_goal(self, goal_id, actor_id, *, expected_version, title=None, description=None, request_id=None):
        from .learning_store import InvalidTransition
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._teacher_write(actor_id, goal)
            def apply():
                if goal["status"] != "draft":
                    raise InvalidTransition("已发布目标不能改写，请另建目标")
                return super(LearningOperations, self).update_goal(goal_id, actor_id, expected_version=expected_version, title=title, description=description)
            return self._command(db, actor_id, goal_id, request_id, {"op": "update", "title": title, "description": description}, apply)

    def get_goal(self, goal_id, actor_id):
        with self._using_connection() as db:
            goal = dict(self._goal_row(db, goal_id, actor_id))
            goal["rounds"] = [dict(row) for row in db.execute("SELECT * FROM learning_rounds WHERE goal_id=? ORDER BY round_no", (goal_id,))]
            return goal

    def get_goal_scope(self, goal_id, actor_id):
        with self._using_connection() as db:
            row = self._goal_row(db, goal_id, actor_id)
            return {"goal_id": goal_id, **{key: row[key] for key in ("owner_id", "course_id", "class_id", "version")}}

    @staticmethod
    def _page_bounds(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")

    def list_goals(self, actor_id, *, class_id=None, limit=50, offset=0):
        self._page_bounds(limit, offset)
        result, skipped = [], 0
        with self._using_connection() as db:
            rows = db.execute("SELECT id FROM learning_goals WHERE (? IS NULL OR class_id=?) ORDER BY created_at DESC,id", (class_id, class_id))
            for row in rows:
                try:
                    goal = self._goal_row(db, row["id"], actor_id)
                except (LookupError, PermissionError):
                    continue
                # Offset counts authorized rows, never inaccessible candidates.
                if skipped < offset:
                    skipped += 1
                    continue
                result.append(dict(goal))
                if len(result) >= limit:
                    break
        return result

    def list_tasks(self, actor_id, *, class_id=None, goal_id=None, limit=50, offset=0):
        self._page_bounds(limit, offset)
        result, skipped = [], 0
        with self._using_connection() as db:
            rows = db.execute("SELECT t.id FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id WHERE (? IS NULL OR g.class_id=?) AND (? IS NULL OR t.goal_id=?) ORDER BY t.created_at DESC,t.id", (class_id, class_id, goal_id, goal_id))
            for row in rows:
                try:
                    task = self._task_row(db, row["id"], actor_id)
                except (LookupError, PermissionError):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                result.append(self._task_view(db, task, teacher=actor_id != task["student_id"]))
                if len(result) >= limit:
                    break
        return result

    def get_task(self, task_id, actor_id):
        with self._using_connection() as db:
            task = self._task_row(db, task_id, actor_id)
            return self._task_view(db, task, teacher=actor_id != task["student_id"])

    @classmethod
    def _task_view(cls, db, task, *, teacher=False):
        result = super()._task_view(db, task, teacher=teacher)
        released = teacher or task["status"] == "results_ready"
        result["answer_released"] = released
        result["course_id"], result["class_id"] = task["course_id"], task["class_id"]
        result["questions"] = cls._question_projection(db, task["id"], reveal=released)
        if not released:
            for attempt in result["attempts"]:
                attempt.pop("items", None)
                # First score stays visible during review retries, never a partial new score.
                if attempt["status"] != "graded":
                    attempt.pop("total_score", None)
                    attempt.pop("total_score_units", None)
        policy = cls._policy(db, task["id"])
        result["submission_closed"] = bool(policy.get("submission_closed") or task["status"] == "closed")
        result["submission_closed_at"] = policy.get("submission_closed_at")
        result["diagnostic_required"] = bool(policy.get("diagnostic_required") and not policy.get("diagnostic_completed"))
        result["scheduled_at"] = policy.get("scheduled_at")
        if not teacher and ((policy.get("scheduled_at") and time.time() < policy["scheduled_at"])
                            or result["diagnostic_required"]):
            result["question_count"] = len(result["questions"])
            result["questions"] = []
            result["notice"] = "尚未开放正式题卷，请先完成短诊或等待计划开放时间。"
        return result

    @staticmethod
    def _policy(db, task_id):
        row = db.execute("SELECT policy_json FROM task_policies WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _submission_open(self, db, task):
        from .learning_store import InvalidTransition
        if task["status"] == "closed" or self._policy(db, task["id"]).get("submission_closed"):
            raise InvalidTransition("任务已停止提交，已保存的草稿和历史结果保留")

    def _open_task(self, db, task, now=None):
        from .learning_store import InvalidTransition
        self._submission_open(db, task)
        policy = self._policy(db, task["id"])
        now = time.time() if now is None else now
        if policy.get("scheduled_at") and now < policy["scheduled_at"]:
            raise InvalidTransition("尚未到开放时间")
        if policy.get("diagnostic_required") and not policy.get("diagnostic_completed"):
            raise InvalidTransition("请先完成独立短诊")
        self._check_deadline(task, policy, now)

    @staticmethod
    def _check_deadline(task, policy, now):
        from .learning_store import InvalidTransition
        if task["deadline_at"] is None or now <= task["deadline_at"]:
            return
        if task["first_attempt_id"] is not None:
            if not task["allow_retries"] or not policy.get("allow_retry_after_deadline", True):
                raise InvalidTransition("发布规则不允许截止后重做")
        elif not task["allow_late"]:
            raise InvalidTransition("已截止，草稿保留，请联系教师延期")

    def publish_round(self, actor_id, goal_id, *, expected_version, request_id, round_no, student_ids, question_versions,
                      deadline_at=None, scheduled_at=None, allow_late=False, allow_retries=True,
                      allow_retry_after_deadline=False, common_question_ratio=100, diagnostic_question_versions=None,
                      personal_question_versions=None):
        from .learning_store import InvalidTransition, VersionConflict, TaskNotFound
        payload = {"op": "publish", "round_no": round_no, "student_ids": student_ids, "questions": question_versions,
                   "deadline_at": deadline_at, "scheduled_at": scheduled_at, "allow_late": allow_late,
                   "allow_retries": allow_retries, "allow_retry_after_deadline": allow_retry_after_deadline,
                   "common_question_ratio": common_question_ratio, "diagnostic_questions": diagnostic_question_versions,
                   "personal": personal_question_versions}
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._teacher_write(actor_id, goal)
            def apply():
                if goal["version"] != expected_version:
                    raise VersionConflict("目标版本已变化")
                if goal["status"] == "closed":
                    raise InvalidTransition("目标已关闭")
                if not student_ids or len(set(student_ids)) != len(student_ids):
                    raise ValueError("学生范围无效")
                if scheduled_at and deadline_at and scheduled_at >= deadline_at:
                    raise ValueError("开放时间必须早于截止")
                plans = personal_question_versions or {student: question_versions for student in student_ids}
                if set(plans) != set(student_ids):
                    raise ValueError("个人卷范围必须与学生范围一致")
                counts, totals = set(), set()
                for refs in plans.values():
                    counts.add(len(refs)); total = 0
                    for ref in refs:
                        q = db.execute("SELECT * FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
                        if q is None or q["course_id"] != goal["course_id"]:
                            raise TaskNotFound("题目不属于目标课程")
                        if q["status"] != "approved":
                            raise ValueError("只能发布审查合格的题目")
                        total += json.loads(q["content_json"])["points_units"]
                    totals.add(total)
                if len(counts) != 1 or len(totals) != 1 or 0 in counts:
                    raise ValueError("个人卷题量和总分必须一致")
                sets = [{(ref["question_id"], ref["version"]) for ref in refs} for refs in plans.values()]
                shared = set.intersection(*sets)
                if len(shared) * 100 < next(iter(counts)) * common_question_ratio:
                    raise ValueError("个人卷共同题比例低于发布要求")
                for ref in diagnostic_question_versions or []:
                    diagnostic = db.execute("SELECT status,course_id FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
                    if diagnostic is None or diagnostic["status"] != "approved" or diagnostic["course_id"] != goal["course_id"] or any((ref["question_id"], ref["version"]) in items for items in sets):
                        raise ValueError("诊断题必须合格且与正式题分开")
                round_id = "round_" + hashlib.sha256(f"{goal_id}:{round_no}".encode()).hexdigest()[:32]
                self.create_round(actor_id, goal_id, round_no, round_id=round_id, deadline_at=deadline_at, allow_late=allow_late)
                policy = {**payload, "goal_version": goal["version"], "diagnostic_required": bool(diagnostic_question_versions),
                          "question_versions": question_versions}
                db.execute("INSERT INTO round_policies VALUES(?,?)", (round_id, self._json(policy)))
                tasks = []
                for student in student_ids:
                    task_id = "task_" + hashlib.sha256(f"{round_id}:{student}".encode()).hexdigest()[:32]
                    self.create_task(actor_id, student, goal_id, round_id, plans[student], task_id=task_id,
                                     allow_retries=allow_retries, deadline_at=deadline_at, allow_late=allow_late)
                    db.execute("INSERT INTO task_policies VALUES(?,?)", (task_id, self._json(policy)))
                    self._notify(db, student, "task_published", "task", task_id, {"scheduled_at": scheduled_at})
                    tasks.append(task_id)
                db.execute("UPDATE learning_goals SET status='active',version=version+1,updated_at=? WHERE id=?", (time.time(), goal_id))
                return {"id": round_id, "round_id": round_id, "goal_id": goal_id, "round_no": round_no,
                        "task_ids": tasks, "status": "scheduled" if scheduled_at and scheduled_at > time.time() else "open"}
            return self._command(db, actor_id, goal_id, request_id, payload, apply)

    def create_round(self, actor_id, goal_id, round_no, **options):
        with self._using_connection(write=True) as db:
            self._teacher_write(actor_id, self._goal_row(db, goal_id, actor_id))
            return super().create_round(actor_id, goal_id, round_no, **options)

    def create_task(self, actor_id, student_id, goal_id, round_id, question_versions, **options):
        from .learning_store import TaskNotFound
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._teacher_write(actor_id, goal)
            for ref in question_versions:
                row = db.execute("SELECT owner_id,course_id FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
                if row and row["course_id"] not in (None, goal["course_id"]):
                    raise TaskNotFound("题目课程范围不匹配")
            return super().create_task(actor_id, student_id, goal_id, round_id, question_versions, **options)

    def acquire_draft_lease(self, task_id, actor_id, device_id, **options):
        with self._using_connection(write=True) as db:
            self._open_task(db, self._task_row(db, task_id, actor_id), options.get("now"))
            return super().acquire_draft_lease(task_id, actor_id, device_id, **options)

    def renew_draft_lease(self, task_id, actor_id, lease_token, fence_token, **options):
        with self._using_connection(write=True) as db:
            self._submission_open(db, self._task_row(db, task_id, actor_id))
            return super().renew_draft_lease(task_id, actor_id, lease_token, fence_token, **options)

    def save_draft(self, task_id, actor_id, answers, **options):
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            self._submission_open(db, task)
            replay = db.execute("SELECT 1 FROM idempotency WHERE actor_id=? AND task_id=? AND request_id=?", (actor_id, task_id, options.get("request_id"))).fetchone()
            if replay is None:
                self._open_task(db, task, options.get("now"))
            return super().save_draft(task_id, actor_id, answers, **options)

    def submit_attempt(self, task_id, actor_id, **options):
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            self._submission_open(db, task)
            return super().submit_attempt(task_id, actor_id, **options)

    def start_retry(self, task_id, actor_id, **options):
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id)
            self._submission_open(db, task)
            self._check_deadline(task, self._policy(db, task_id), time.time())
            return super().start_retry(task_id, actor_id, **options)

    @classmethod
    def _normalize_question(cls, question):
        if "score_units" in question and "points" not in question:
            question = {**question, "points": question["score_units"] / 100}
        normalized = super()._normalize_question(question)
        normalized["score_units"] = normalized["points_units"]
        normalized["points"] = question.get("points", normalized["points"])
        # Freeze the same bounded conditions that trusted grading/review may
        # send. Never discard a restriction and accidentally grade over R.
        from .services.model_gateway import GatewayError, _question_requirements
        try:
            normalized.update(_question_requirements(question))
        except GatewayError as error:
            raise ValueError("题目作答格式或数学条件无效或超出限制") from error
        for key in ("course_id", "class_id", "content_sha", "family_sha", "family_id", "source_anchors", "review_evidence",
                    "external_processing_permission", "pipeline_job_id", "source_revision", "knowledge_points", "difficulty", "reference_answer", "provenance", "hint_action_ids",
                    "generation_intent", "usage_scope"):
            if key in question:
                normalized[key] = question[key]
        return normalized

    def create_question_version(self, actor_id, question_id, version, question, *, status="candidate", course_id=None):
        from .learning_store import TaskNotFound
        course_id = course_id or question.get("course_id")
        if self._access_checker:
            self._require_access(actor_id, {"kind": "question", "owner_id": actor_id, "course_id": course_id}, write=True)
        with self._using_connection(write=True) as db:
            previous = db.execute("SELECT owner_id,course_id,MAX(version) latest FROM question_versions WHERE question_id=? GROUP BY question_id", (question_id,)).fetchone()
            if previous and (previous["owner_id"] != actor_id or previous["course_id"] != course_id):
                raise TaskNotFound("不能编辑他人的题目版本")
            if previous and version != previous["latest"] + 1:
                raise ValueError("编辑必须创建连续的新版本")
            value = {**dict(question), "course_id": course_id}
            result = super().create_question_version(actor_id, question_id, version, value, status=status, course_id=course_id)
            content = result["content"]
            metadata = {key: content[key] for key in ("content_sha", "family_sha", "source_anchors", "review_evidence", "external_processing_permission", "pipeline_job_id", "source_revision", "difficulty", "knowledge_points", "answer_format", "provenance") if key in content}
            db.execute("INSERT INTO question_metadata VALUES(?,?,?)", (question_id, version, self._json(metadata)))
            if course_id and content.get("content_sha") and content.get("family_sha"):
                db.execute("INSERT INTO question_family_index VALUES(?,?,?,?,?)", (course_id, content["content_sha"], content["family_sha"], question_id, version))
            return result

    def get_question(self, question_id, version, actor_id, *, task_id=None):
        if task_id:
            with self._using_connection() as db:
                task = self._task_row(db, task_id, actor_id)
                matches = [item for item in self._question_projection(db, task_id, reveal=actor_id != task["student_id"] or task["status"] == "results_ready")
                           if item["id"] == question_id and item["version"] == version]
                if not matches:
                    from .learning_store import TaskNotFound
                    raise TaskNotFound("题目不属于此任务")
                return matches[0]
        from .learning_store import TaskNotFound
        with self._using_connection() as db:
            row = db.execute("SELECT * FROM question_versions WHERE question_id=? AND version=?", (question_id, version)).fetchone()
            if row is None:
                raise TaskNotFound("题目不存在")
            if self._access_checker:
                self._require_access(actor_id, {"kind": "question", "course_id": row["course_id"], "owner_id": row["owner_id"]})
            elif actor_id != row["owner_id"]:
                raise TaskNotFound("题目不存在")
            return {**json.loads(row["content_json"]), "question_id": question_id, "version": version, "status": row["status"]}

    def list_questions(self, course_id, actor_id, *, status=None, limit=100, offset=0, q=None,
                       question_type=None, knowledge_point=None, difficulty=None, class_id=None):
        from .services.question_library import list_question_library
        return list_question_library(self, course_id, actor_id, status=status, limit=limit, offset=offset, q=q,
            question_type=question_type, knowledge_point=knowledge_point, difficulty=difficulty, class_id=class_id)["items"]

    def get_question_detail(self, course_id, question_id, version, actor_id):
        from .services.question_library import question_detail
        return question_detail(self, course_id, question_id, version, actor_id)

    def find_question_duplicate(self, course_id, content_sha, family_sha):
        with self._using_connection() as db:
            row = db.execute("SELECT f.* FROM question_family_index f JOIN question_versions q ON q.question_id=f.question_id AND q.version=f.version WHERE f.course_id=? AND (f.content_sha=? OR f.family_sha=?) AND q.status<>'retired' ORDER BY f.version DESC LIMIT 1", (course_id, content_sha, family_sha)).fetchone()
            return dict(row) if row else None

    def apply_grading_result(self, attempt_id, item_id, result, **options):
        value = dict(result)
        if "score_units" in value:
            if type(value["score_units"]) is not int or value["score_units"] < 0:
                raise ValueError("分值必须为非负整数单位")
            value["score"] = value["score_units"] / self.SCORE_SCALE
        with self._using_connection(write=True) as db:
            task = super().apply_grading_result(attempt_id, item_id, value, **options)
            attempt = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt["status"] == "graded":
                self._grading_notification(db, attempt_id, manual=False)
            return task

    def fail_grading(self, attempt_id, **options):
        with self._using_connection(write=True) as db:
            task = super().fail_grading(attempt_id, **options)
            self._grading_notification(db, attempt_id, manual=True)
            return task

    def _grading_notification(self, db, attempt_id, *, manual):
        row = db.execute("SELECT t.id,t.student_id,o.attempt_revision FROM attempts a JOIN learning_tasks t ON t.id=a.task_id JOIN grading_outbox o ON o.attempt_id=a.id WHERE a.id=?", (attempt_id,)).fetchone()
        # Match the historical worker key and payload exactly. A worker running
        # the old direct-delivery path and a dispatcher replay remain compatible.
        self._notify(db, row["student_id"], "grading_manual_required" if manual else "results_ready", "task", row["id"],
                     {"state": "awaiting_manual" if manual else "results_ready"},
                     key=f"grading:{attempt_id}:{row['attempt_revision']}")

    def _global_diagnostic(self, db, user_id):
        """A pending first diagnostic vetoes help across all accessible tasks."""
        for row in db.execute("SELECT id FROM learning_tasks WHERE student_id=? AND status<>'closed'", (user_id,)):
            try:
                self._task_row(db, row["id"], user_id)
            except (LookupError, PermissionError):
                continue
            policy = self._policy(db, row["id"])
            if policy.get("submission_closed"):
                continue
            if policy.get("diagnostic_required") and not policy.get("diagnostic_completed"):
                return True
            if db.execute("SELECT 1 FROM diagnostic_sessions WHERE task_id=? AND status='active'", (row["id"],)).fetchone():
                return True
        return False

    def _hint_context(self, db, user_id, task_id, question_version_id):
        """Trusted, minimal frozen question/draft snapshot; never returns an answer key."""
        from .learning_store import InvalidTransition, TaskNotFound
        from .services.restricted_hints import POLICY_VERSION, VALIDATOR_VERSION, allowed_actions
        task = self._task_row(db, task_id, user_id)
        if task["student_id"] != user_id:
            raise TaskNotFound("提示只允许当前学生的任务")
        self._require_access(user_id, {"kind": "task", **dict(task)}, write=True)
        if task["status"] != "in_progress":
            raise InvalidTransition("提示需要正在作答的正式任务")
        self._open_task(db, task)
        round_row = db.execute("SELECT status FROM learning_rounds WHERE id=?", (task["round_id"],)).fetchone()
        goal = db.execute("SELECT status FROM learning_goals WHERE id=?", (task["goal_id"],)).fetchone()
        if not round_row or round_row[0] != "open" or not goal or goal[0] == "closed":
            raise InvalidTransition("任务所属轮次或目标已关闭")
        if self._global_diagnostic(db, user_id):
            raise InvalidTransition("首诊期间必须独立完成")
        item = next((row for row in db.execute("SELECT * FROM task_items WHERE task_id=?", (task_id,))
                     if question_version_id in {f"{row['question_id']}:{row['question_version']}",
                                                f"{row['question_id']}@{row['question_version']}"}), None)
        if item is None:
            raise TaskNotFound("题目版本不属于当前任务")
        draft = db.execute("SELECT answer_json FROM draft_answers WHERE task_id=? AND question_id=?",
                           (task_id, item["question_id"])).fetchone()
        if draft is None:
            raise InvalidTransition("请先可靠保存当前题的尝试")
        question, answer = json.loads(item["frozen_json"]), json.loads(draft[0])
        self._check_answer(question, answer)
        cycle = int(db.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0])
        binding = {"task_id": task_id, "question_version_id": f"{item['question_id']}:{item['question_version']}",
                   "attempt_cycle": cycle, "draft_hash": self._digest(answer),
                   "policy_version": POLICY_VERSION, "validator_version": VALIDATOR_VERSION}
        minimal = {key: question[key] for key in ("type", "stem", "options")}
        # The teacher may narrow the server action catalogue, never inject text.
        actions = allowed_actions(question)
        permission = question.get("external_processing_permission")
        external_allowed = permission is True or (isinstance(permission, str) and permission in {"approved", "allowed"})
        return {"binding": binding, "question_id": item["question_id"], "question": minimal,
                "draft": answer, "actions": actions, "external_processing_allowed": external_allowed,
                "student_id": user_id, "course_id": task["course_id"], "class_id": task["class_id"]}

    def get_hint_context(self, user_id, *, task_id, question_version_id):
        with self._using_connection() as db:
            return self._hint_context(db, user_id, task_id, question_version_id)

    def get_answer_policy(self, user_id, *, task_id=None, question_version_id=None):
        from .learning_store import TaskNotFound
        with self._using_connection() as db:
            if task_id:
                task = self._task_row(db, task_id, user_id)
                if task["student_id"] != user_id:
                    raise TaskNotFound("题目未分配给当前学生")
            candidates = db.execute("SELECT id,first_attempt_id FROM learning_tasks WHERE student_id=? AND status NOT IN ('results_ready','closed')", (user_id,)).fetchall()
            active = []
            diagnostic = False; attempted = True
            for row in candidates:
                try:
                    self._task_row(db, row["id"], user_id)
                except (LookupError, PermissionError):
                    continue
                policy = self._policy(db, row["id"])
                # An abandoned, never-submitted task need not block unrelated
                # conversation forever. Its own scoped answer gate stays closed.
                if policy.get("submission_closed") and row["first_attempt_id"] is None:
                    continue
                active.append(row)
                diagnostic = diagnostic or bool(policy.get("diagnostic_required") and not policy.get("diagnostic_completed"))
                attempted = attempted and db.execute("SELECT 1 FROM draft_answers WHERE task_id=? LIMIT 1", (row["id"],)).fetchone() is not None
            blocked = bool(active)
            released = not blocked
            if task_id:
                # A scoped, unrelated historical question need not disappear
                # merely because another task is active. Freeform generation
                # remains guarded by the all-active policy in the companion.
                scoped_active = task["status"] not in {"results_ready", "closed"}
                same_family_active = False
                if question_version_id:
                    ref = question_version_id.replace("@", ":")
                    for row in active:
                        other_refs = {f"{item['question_id']}:{item['question_version']}" for item in db.execute("SELECT question_id,question_version FROM task_items WHERE task_id=?", (row["id"],))}
                        same_family_active = same_family_active or ref in other_refs
                blocked = scoped_active or same_family_active
                released = task["status"] == "results_ready" and not blocked
            diagnostic = self._global_diagnostic(db, user_id)
            hint_context = None
            if task_id and question_version_id and not diagnostic:
                try:
                    hint_context = self._hint_context(db, user_id, task_id, question_version_id)
                except (LookupError, PermissionError, ValueError, TypeError):
                    pass
                except Exception as error:
                    from .learning_store import InvalidTransition
                    if not isinstance(error, InvalidTransition):
                        raise
            levels = [level for level in (1, 2) if hint_context and hint_context["actions"].get(level)]
            hint_allowed = bool(levels)
            delivered = {}
            if hint_context:
                from .services.restricted_hints import hint_evidence_id, same_binding
                for event in db.execute("SELECT id,payload_json FROM task_events WHERE task_id=? AND actor_id=? AND question_id=? AND event_type='help'",
                                        (task_id, user_id, hint_context["question_id"])):
                    evidence = json.loads(event["payload_json"])
                    response_id = evidence.get("response_id")
                    if (evidence.get("source") == "restricted_hint" and evidence.get("status") == "validated_delivered"
                            and response_id and event["id"] == hint_evidence_id(response_id, "validated_delivered")
                            and same_binding(evidence, hint_context["binding"])):
                        delivered[response_id] = {**evidence, "evidence_id": event["id"]}
            return {"allow_assistant_content": not blocked and not diagnostic,
                    "allow_generation": not blocked and not diagnostic,
                    "results_released": released and not diagnostic, "diagnostic": diagnostic,
                    "attempted": bool(hint_context) if task_id and question_version_id else attempted,
                    "allow_hint_generation": hint_allowed and hint_context["external_processing_allowed"],
                    "allow_hint_content": hint_allowed, "allowed_hint_levels": levels,
                    "hint_binding": hint_context["binding"] if hint_context else None,
                    "delivered_hint_evidence": delivered,
                    "hints_allowed": hint_allowed, "active_count": len(active)}

    def get_discussion_scope(self, user_id, *, task_id, attempt_id=None, question_version_id=None, grading_revision=None):
        from .learning_store import TaskNotFound
        with self._using_connection() as db:
            task = self._task_row(db, task_id, user_id)
            if attempt_id:
                attempt = db.execute("SELECT * FROM attempts WHERE id=? AND task_id=?", (attempt_id, task_id)).fetchone()
                if attempt is None or (grading_revision is not None and grading_revision > attempt["revision"]):
                    raise TaskNotFound("提交或评分版本不存在")
            if question_version_id:
                refs = {f"{row['question_id']}:{row['question_version']}" for row in db.execute("SELECT question_id,question_version FROM task_items WHERE task_id=?", (task_id,))}
                if question_version_id.replace("@", ":") not in refs:
                    raise TaskNotFound("题目不属于此任务")
            return {"task_id": task_id, "attempt_id": attempt_id, "question_version_id": question_version_id,
                    "grading_revision": grading_revision, "student_id": task["student_id"], "user_id": task["student_id"],
                    "course_id": task["course_id"], "class_id": task["class_id"], "results_released": task["status"] == "results_ready"}

    def report_input(self, goal_id=None, actor_id=None, *, scope=None):
        if scope is None and isinstance(goal_id, Mapping):
            scope = goal_id
        if scope is not None:
            from .services.learning_reports import build_learning_report
            return build_learning_report(self, scope, actor_id)
        return super().report_input(goal_id, actor_id)

    def refresh_reports(self, reports):
        from .services.report_exports import build_report_snapshot
        with self._using_connection() as db:
            revision = db.execute("SELECT revision FROM learning_revision WHERE id=1").fetchone()[0]
        for report in reports.list_reports():
            if revision <= report["latest_source_revision"]:
                continue
            try:
                source = self.report_input(scope=report["scope"], actor_id=self._report_actor(report["scope"]))
                if source["source_revision"] <= report["latest_source_revision"]:
                    continue
                snapshot = build_report_snapshot(source, report["scope"], rule_version=report["rule_version"])
                frozen = reports.create_snapshot(report["scope"], source["source_revision"], snapshot,
                                                 created_by="system", rule_version=report["rule_version"])
                reports.create_revision(frozen["id"])
            except (LookupError, PermissionError, ValueError):
                continue

    def _report_actor(self, scope):
        if scope.get("student_id"):
            return scope["student_id"]
        with self._using_connection() as db:
            if scope.get("goal_id"):
                row = db.execute("SELECT owner_id FROM learning_goals WHERE id=?", (scope["goal_id"],)).fetchone()
            elif scope.get("class_id"):
                row = db.execute("SELECT owner_id FROM learning_goals WHERE class_id=? ORDER BY created_at DESC LIMIT 1", (scope["class_id"],)).fetchone()
            else:
                row = db.execute("SELECT owner_id FROM question_versions WHERE course_id=? ORDER BY created_at DESC LIMIT 1", (scope.get("course_id"),)).fetchone()
            if row is None:
                raise LookupError("报告范围没有可授权的数据来源")
            return row[0]

    def _notify(self, db, user_id, event_type, resource_type, resource_id, payload, *, key=None):
        identity = key or f"{user_id}:{event_type}:{resource_id}"
        db.execute("INSERT OR IGNORE INTO learning_outbox VALUES(?,?,?,?,?,?,?,NULL)", (identity, user_id, event_type, resource_type, resource_id, self._json(payload), time.time()))

    def pending_notifications(self, *, limit=100):
        with self._using_connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM learning_outbox WHERE delivered_at IS NULL ORDER BY created_at,id LIMIT ?", (limit,))]

    def acknowledge_notification(self, event_id):
        with self._using_connection(write=True) as db:
            db.execute("UPDATE learning_outbox SET delivered_at=COALESCE(delivered_at,?) WHERE id=?", (time.time(), event_id))
