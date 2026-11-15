"""Independent diagnostics, teacher review, learning plans and append-only events."""

import json
import time
from uuid import uuid4

from .services.math_grading import grade_objective


class LearningLifecycle:
    def _student_task(self, db, task_id, actor):
        from .learning_store import TaskNotFound
        task = self._task_row(db, task_id, actor)
        if task["student_id"] != actor:
            raise TaskNotFound("只能操作本人的任务")
        return task

    def start_diagnostic(self, task_id, actor_id, *, request_id):
        from .learning_store import InvalidTransition
        with self._using_connection(write=True) as db:
            task = self._student_task(db, task_id, actor_id)
            policy = self._policy(db, task_id)
            def apply():
                old = db.execute("SELECT id FROM diagnostic_sessions WHERE task_id=?", (task_id,)).fetchone()
                if old:
                    return {"id": old[0]}
                if policy.get("scheduled_at") and time.time() < policy["scheduled_at"]:
                    raise InvalidTransition("尚未到开放时间")
                refs = policy.get("diagnostic_questions") or []
                if not refs:
                    raise InvalidTransition("本任务未要求独立诊断")
                diagnostic_id = uuid4().hex
                db.execute("INSERT INTO diagnostic_sessions(id,task_id,student_id,status,version,created_at) VALUES(?,?,?,'active',1,?)", (diagnostic_id, task_id, actor_id, time.time()))
                formal = {row[0] for row in db.execute("SELECT question_id FROM task_items WHERE task_id=?", (task_id,))}
                for position, ref in enumerate(refs):
                    row = db.execute("SELECT * FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
                    if row is None or row["status"] != "approved" or row["course_id"] != task["course_id"] or row["question_id"] in formal:
                        raise ValueError("诊断题必须合格、属于本课程且与正式题分开")
                    db.execute("INSERT INTO diagnostic_items VALUES(?,?,?,?,?)", (diagnostic_id, row["question_id"], row["version"], row["content_json"], position))
                return {"id": diagnostic_id}
            result = self._command(db, actor_id, task_id, request_id, {"op": "diagnostic_start"}, apply)
            return self.get_diagnostic(result["id"], actor_id)

    def get_diagnostic(self, diagnostic_id, actor_id):
        from .learning_store import TaskNotFound
        with self._using_connection() as db:
            session = db.execute("SELECT * FROM diagnostic_sessions WHERE id=?", (diagnostic_id,)).fetchone()
            if session is None:
                raise TaskNotFound("诊断不存在")
            task = self._task_row(db, session["task_id"], actor_id)
            questions = []
            for row in db.execute("SELECT * FROM diagnostic_items WHERE diagnostic_id=? ORDER BY position", (diagnostic_id,)):
                frozen = json.loads(row["frozen_json"])
                questions.append({key: frozen[key] for key in ("id", "type", "stem", "options")})
            result = {"id": diagnostic_id, "task_id": task["id"], "status": session["status"], "version": session["version"],
                      "questions": questions, "counts_toward_formal_score": False, "hints_allowed": False}
            if session["status"] != "active":
                result["feedback"] = json.loads(session["feedback_json"])
            return result

    def submit_diagnostic(self, diagnostic_id, actor_id, answers, unknown_question_ids, *, request_id):
        from .learning_store import TaskNotFound, InvalidTransition
        with self._using_connection(write=True) as db:
            session = db.execute("SELECT * FROM diagnostic_sessions WHERE id=?", (diagnostic_id,)).fetchone()
            if session is None:
                raise TaskNotFound("诊断不存在")
            self._student_task(db, session["task_id"], actor_id)
            def apply():
                if session["status"] != "active":
                    raise InvalidTransition("诊断已提交")
                questions = {row["question_id"]: json.loads(row["frozen_json"]) for row in db.execute("SELECT * FROM diagnostic_items WHERE diagnostic_id=?", (diagnostic_id,))}
                unknown = set(unknown_question_ids)
                if unknown.intersection(answers) or set(answers) | unknown != set(questions):
                    raise ValueError("请逐题作答或标记暂时不会")
                checked, correct, pending = 0, 0, 0
                for qid, answer in answers.items():
                    self._check_answer(questions[qid], answer)
                    result = grade_objective(questions[qid], answer)
                    if result["status"] == "graded":
                        checked += 1; correct += int(result["score_units"] > 0)
                    else:
                        pending += 1
                feedback = {"answered": len(answers), "unknown": len(unknown), "checked": checked, "correct": correct,
                            "pending_review": pending, "message": "短诊只用于了解起点，不计正式成绩，也不据此直接认定掌握。"}
                db.execute("UPDATE diagnostic_sessions SET status='completed',version=version+1,answers_json=?,feedback_json=?,submitted_at=? WHERE id=?", (self._json({"answers": answers, "unknown_question_ids": sorted(unknown)}), self._json(feedback), time.time(), diagnostic_id))
                policy = self._policy(db, session["task_id"]); policy["diagnostic_completed"] = True
                db.execute("UPDATE task_policies SET policy_json=? WHERE task_id=?", (self._json(policy), session["task_id"]))
                return {"id": diagnostic_id}
            self._command(db, actor_id, session["task_id"], request_id, {"op": "diagnostic_submit", "answers": answers, "unknown": unknown_question_ids}, apply)
            return self.get_diagnostic(diagnostic_id, actor_id)

    def apply_manual_grade(self, actor_id, attempt_id, item_id, *, score=None, score_units=None, reason, expected_revision, request_id=None):
        return self._review_score(actor_id, attempt_id, item_id, score=score, score_units=score_units, reason=reason,
                                  expected_revision=expected_revision, request_id=request_id, manual=True)

    def apply_correction(self, actor_id, attempt_id, item_id, *, score=None, score_units=None, reason, expected_revision, request_id=None):
        return self._review_score(actor_id, attempt_id, item_id, score=score, score_units=score_units, reason=reason,
                                  expected_revision=expected_revision, request_id=request_id, manual=False)

    def _review_score(self, actor, attempt_id, item_id, *, score, score_units, reason, expected_revision, request_id, manual):
        from .learning_store import TaskNotFound, VersionConflict, InvalidTransition
        reason = self._text(reason, "reason", maximum=2000)
        units = self._score_units(score) if score_units is None else score_units
        if type(units) is not int or units < 0:
            raise ValueError("分值必须为非负整数单位")
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise TaskNotFound("提交不存在")
            task = self._task_row(db, attempt["task_id"], actor); self._teacher_write(actor, task)
            def apply():
                if attempt["revision"] != expected_revision:
                    raise VersionConflict("评分版本已经变化")
                if attempt["status"] != ("review_required" if manual else "graded"):
                    raise InvalidTransition("当前提交状态不能执行此评分操作")
                item = db.execute("SELECT * FROM attempt_items WHERE attempt_id=? AND question_id=?", (attempt_id, item_id)).fetchone()
                question = db.execute("SELECT frozen_json FROM task_items WHERE task_id=? AND question_id=?", (task["id"], item_id)).fetchone()
                if item is None or question is None:
                    raise TaskNotFound("题目不属于此提交")
                maximum = json.loads(question[0])["points_units"]
                if units > maximum:
                    raise ValueError("分值超过冻结题目满分")
                revision = expected_revision + 1
                result = {"score_units": units, "score": units / 100, "why": reason, "source": "teacher", "revision": revision}
                if not manual:
                    db.execute("INSERT INTO grading_corrections VALUES(?,?,?,?,?,?,?,?,?)", (uuid4().hex, attempt_id, item_id, actor, item["score_units"], units, reason, revision, time.time()))
                db.execute("UPDATE attempt_items SET status='graded',score_units=?,original_grading_json=COALESCE(original_grading_json,grading_json,?),grading_json=?,result_hash=?,updated_at=? WHERE attempt_id=? AND question_id=?", (units, self._json(result), self._json(result), self._digest(result), time.time(), attempt_id, item_id))
                remaining = db.execute("SELECT COUNT(*) FROM attempt_items WHERE attempt_id=? AND status<>'graded'", (attempt_id,)).fetchone()[0]
                total = db.execute("SELECT COALESCE(SUM(score_units),0) FROM attempt_items WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
                db.execute("UPDATE attempts SET revision=?,total_score_units=? WHERE id=?", (revision, total if not remaining else None, attempt_id))
                if not remaining:
                    db.execute("UPDATE attempts SET status='graded',graded_at=? WHERE id=?", (time.time(), attempt_id))
                    # Correcting an old attempt must not terminate an active retry.
                    newest = db.execute("SELECT id FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task["id"],)).fetchone()[0]
                    if newest == attempt_id and task["status"] not in {"not_started", "in_progress"}:
                        db.execute("UPDATE learning_tasks SET status='results_ready',version=version+1,updated_at=? WHERE id=?", (time.time(), task["id"]))
                    db.execute("UPDATE grading_outbox SET status='completed' WHERE attempt_id=?", (attempt_id,))
                self._notify(db, task["student_id"], "grading_corrected" if not manual else "grading_reviewed", "task", task["id"], {"attempt_id": attempt_id, "revision": revision}, key=f"review:{attempt_id}:{revision}")
                return {"attempt_id": attempt_id, "question_id": item_id, "grading_revision": revision, "score_units": units,
                        "score": units / 100, "total_score": total / 100, "prior_score": self._score_value(item["score_units"])}
            return self._command(db, actor, attempt_id, request_id, {"op": "manual" if manual else "correction", "question": item_id, "units": units, "reason": reason}, apply)

    def extend_deadline(self, actor_id, task_id, new_deadline, reason, *, expected_version, request_id=None):
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id); self._teacher_write(actor_id, task)
            def apply():
                self._version_check(task, expected_version)
                if new_deadline <= (task["deadline_at"] or 0):
                    raise ValueError("新截止时间应晚于原截止时间")
                db.execute("INSERT INTO deadline_extensions VALUES(?,?,?,?,?,?,?)", (uuid4().hex, task_id, actor_id, task["deadline_at"], new_deadline, self._text(reason, "reason"), time.time()))
                db.execute("UPDATE learning_tasks SET deadline_at=?,version=version+1,updated_at=? WHERE id=?", (new_deadline, time.time(), task_id))
                self._notify(db, task["student_id"], "deadline_extended", "task", task_id, {"new_deadline": new_deadline}, key=f"deadline:{task_id}:{expected_version}")
                return {"task_id": task_id}
            self._command(db, actor_id, task_id, request_id, {"op": "extend", "deadline": new_deadline, "reason": reason}, apply)
            return self.get_task(task_id, actor_id)

    def record_help(self, task_id, actor_id, question_id, *, payload=None, request_id=None, **options):
        from .learning_store import InvalidTransition
        policy = self.get_answer_policy(actor_id, task_id=task_id)
        if policy["diagnostic"] or not policy["attempted"]:
            raise InvalidTransition("首诊不提供提示；正式作答请先尝试")
        with self._using_connection(write=True) as db:
            return self._command(db, actor_id, task_id, request_id, {"op": "help", "question": question_id, "payload": payload},
                lambda: super(LearningLifecycle, self).record_help(task_id, actor_id, question_id, payload=payload, **options))

    def record_reading(self, task_id, actor_id, material_id=None, *, question_id=None, payload=None, request_id=None, **options):
        material_id = material_id or (payload or {}).get("material_id")
        if not material_id:
            raise ValueError("阅读记录需要材料标识")
        with self._using_connection(write=True) as db:
            self._student_task(db, task_id, actor_id)
            return self._command(db, actor_id, task_id, request_id, {"op": "reading", "material": material_id},
                lambda: super(LearningLifecycle, self).record_reading(task_id, actor_id, material_id, **options))

    def change_goal_plan(self, goal_id, actor_id, *, action, reason, expected_version, request_id, future_round_count=None, new_end_at=None):
        from .learning_store import VersionConflict
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id); self._teacher_write(actor_id, goal)
            def apply():
                if goal["version"] != expected_version:
                    raise VersionConflict("目标版本已变化")
                if action == "renew" and (not future_round_count or not new_end_at or new_end_at <= time.time()):
                    raise ValueError("续期必须明确未来轮数和结束时间")
                state = {"action": action, "reason": self._text(reason, "reason"), "future_round_count": future_round_count, "new_end_at": new_end_at}
                db.execute("UPDATE learning_goals SET status=?,version=version+1,updated_at=? WHERE id=?", ("closed" if action == "close" else "active", time.time(), goal_id))
                db.execute("INSERT INTO goal_revisions VALUES(?,?,?,?,?)", (goal_id, expected_version + 1, self._json(state), actor_id, time.time()))
                return {"goal_id": goal_id, "version": expected_version + 1, **state}
            return self._command(db, actor_id, goal_id, request_id, {"op": "plan", "action": action, "reason": reason, "rounds": future_round_count, "end": new_end_at}, apply)

    def create_appeal(self, actor_id, attempt_id, *, question_id, grading_revision, reason, request_id):
        from .learning_store import TaskNotFound, VersionConflict, InvalidTransition
        with self._using_connection(write=True) as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise TaskNotFound("提交不存在")
            task = self._student_task(db, attempt["task_id"], actor_id)
            def apply():
                if attempt["revision"] != grading_revision:
                    raise VersionConflict("评分版本已变化")
                if task["status"] != "results_ready":
                    raise InvalidTransition("结果尚未全部开放")
                if db.execute("SELECT 1 FROM attempt_items WHERE attempt_id=? AND question_id=?", (attempt_id, question_id)).fetchone() is None:
                    raise TaskNotFound("题目不存在")
                old = db.execute("SELECT id FROM appeals WHERE attempt_id=? AND question_id=? AND grading_revision=?", (attempt_id, question_id, grading_revision)).fetchone()
                if old:
                    return {"id": old[0]}
                appeal_id = uuid4().hex
                db.execute("INSERT INTO appeals(id,attempt_id,question_id,student_id,grading_revision,reason,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)", (appeal_id, attempt_id, question_id, actor_id, grading_revision, self._text(reason, "reason", maximum=2000), time.time()))
                self._notify(db, task["owner_id"], "appeal_received", "task", task["id"], {"appeal_id": appeal_id})
                return {"id": appeal_id}
            return self._command(db, actor_id, attempt_id, request_id, {"op": "appeal", "question": question_id, "revision": grading_revision, "reason": reason}, apply)

    def save_teacher_judgment(self, actor_id, task_id, *, judgment, knowledge_point_id, request_id):
        if judgment not in {"mastered", "developing", "needs_practice", "insufficient_evidence", "needs_review"}:
            raise ValueError("教师判断状态无效")
        with self._using_connection(write=True) as db:
            task = self._task_row(db, task_id, actor_id); self._teacher_write(actor_id, task)
            def apply():
                revision = db.execute("SELECT revision FROM learning_revision WHERE id=1").fetchone()[0]
                record_id = uuid4().hex
                value = {"judgment": judgment, "source_revision": revision, "revision": revision + 1}
                db.execute("INSERT INTO teacher_judgments VALUES(?,?,?,?,?,?,?)", (record_id, task["student_id"], task["goal_id"], self._id(knowledge_point_id), actor_id, self._json(value), time.time()))
                self._notify(db, task["student_id"], "teacher_judgment_recorded", "task", task_id, {"judgment": judgment}, key=f"judgment:{record_id}")
                return {"id": record_id, **value}
            return self._command(db, actor_id, task_id, request_id, {"op": "judgment", "judgment": judgment, "kp": knowledge_point_id}, apply)

    def set_question_version_status(self, actor_id, question_id, version, status):
        from .learning_store import TaskNotFound
        if status not in self._QUESTION_STATUSES:
            raise ValueError("题目状态无效")
        with self._using_connection(write=True) as db:
            row = db.execute("SELECT * FROM question_versions WHERE question_id=? AND version=?", (question_id, version)).fetchone()
            if row is None:
                raise TaskNotFound("题目不存在")
            if self._access_checker:
                self._require_access(actor_id, {"kind": "question", "course_id": row["course_id"], "owner_id": row["owner_id"]}, write=True)
            elif actor_id != row["owner_id"]:
                raise TaskNotFound("题目不存在")
            db.execute("UPDATE question_versions SET status=? WHERE question_id=? AND version=?", (status, question_id, version))
            for task in db.execute("SELECT t.id,t.student_id,g.owner_id FROM task_items i JOIN learning_tasks t ON t.id=i.task_id JOIN learning_goals g ON g.id=t.goal_id WHERE i.question_id=? AND i.question_version=?", (question_id, version)):
                for user in (task["student_id"], task["owner_id"]):
                    self._notify(db, user, "question_status_changed", "task", task["id"], {"question_id": question_id, "status": status}, key=f"question:{question_id}:{version}:{status}:{user}:{task['id']}")
            return {"question_id": question_id, "version": version, "status": status}

    def pending_appeals(self, *, limit=100):
        with self._using_connection() as db:
            return [dict(row) for row in db.execute("SELECT p.*,t.goal_id,t.student_id,g.owner_id,g.course_id,g.class_id FROM appeals p JOIN attempts a ON a.id=p.attempt_id JOIN learning_tasks t ON t.id=a.task_id JOIN learning_goals g ON g.id=t.goal_id WHERE p.status='pending' ORDER BY p.created_at,p.id LIMIT ?", (limit,))]

    def review_appeal_input(self, appeal_id):
        from .learning_store import TaskNotFound
        with self._using_connection() as db:
            appeal = db.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            if appeal is None:
                raise TaskNotFound("异议不存在")
            data = self.get_grading_input(appeal["attempt_id"])
            question = next(item for item in data["questions"] if item["id"] == appeal["question_id"])
            return {"appeal": dict(appeal), "question": question, "answer": data["answers"][appeal["question_id"]], "scope": data["scope"]}

    def finish_appeal_review(self, appeal_id, result, *, reason=None):
        with self._using_connection(write=True) as db:
            appeal = db.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            if appeal is None or appeal["status"] != "pending":
                return
            data = self.review_appeal_input(appeal_id)
            # AI review is evidence for the authorized teacher; formal changes
            # still use the append-only correction endpoint and current revision.
            db.execute("UPDATE appeals SET status='awaiting_teacher',result_json=? WHERE id=?", (self._json({"review": result, "reason": reason}), appeal_id))
            self._notify(db, data["scope"]["owner_id"], "appeal_review_ready", "task", data["scope"]["task_id"] if "task_id" in data["scope"] else self.get_grading_input(appeal["attempt_id"])["task_id"], {"appeal_id": appeal_id}, key=f"appeal-review:{appeal_id}")

    def list_appeals(self, actor_id, *, task_id):
        with self._using_connection() as db:
            task = self._task_row(db, task_id, actor_id)
            rows = db.execute("SELECT p.* FROM appeals p JOIN attempts a ON a.id=p.attempt_id WHERE a.task_id=? ORDER BY p.created_at,p.id", (task_id,))
            return [{key: row[key] for key in ("id", "attempt_id", "question_id", "grading_revision", "status", "created_at")} for row in rows]

    def list_corrections(self, attempt_id, actor_id):
        with self._using_connection() as db:
            attempt = db.execute("SELECT task_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                from .learning_store import TaskNotFound
                raise TaskNotFound("提交不存在")
            task = self._task_row(db, attempt[0], actor_id)
            rows = super().list_corrections(attempt_id, actor_id)
            if actor_id == task["student_id"] and task["status"] != "results_ready":
                return [{key: row[key] for key in ("id", "attempt_id", "question_id", "grading_revision", "created_at")} for row in rows]
            return rows

    def list_evidence(self, goal_id, actor_id, *, student_id=None):
        data = self.report_input(scope={"kind": "goal", "goal_id": goal_id, **({"student_id": student_id} if student_id else {})}, actor_id=actor_id)
        return data["evidence"]
