"""Bounded, durable round scheduling under a teacher-confirmed plan."""

from contextlib import nullcontext
import json
import math
import time


class LearningPlanning:
    def configure_plan(self, goal_id, actor_id, *, expected_version, request_id, future_round_count,
                       interval_seconds, ends_at, question_pool):
        from .learning_store import VersionConflict
        if (type(future_round_count) is not int or not 1 <= future_round_count <= 100
                or type(interval_seconds) is not int or not 60 <= interval_seconds <= 366 * 86400
                or type(ends_at) not in {int, float} or not math.isfinite(ends_at)
                or ends_at <= time.time() + interval_seconds):
            raise ValueError("轮数、节奏或结束时间无效")
        if (not isinstance(question_pool, list) or not 1 <= len(question_pool) <= 2000
                or any(not isinstance(ref, dict) or set(ref) != {"question_id", "version"}
                       or type(ref["version"]) is not int or ref["version"] < 1 for ref in question_pool)):
            raise ValueError("持续计划须有教师确认的有限题池")
        keys = [(self._id(ref["question_id"], "question_id"), ref["version"]) for ref in question_pool]
        if len(keys) != len(set(keys)):
            raise ValueError("持续计划题池不能重复")
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id)
            self._teacher_write(actor_id, goal)
            def apply():
                if goal["version"] != expected_version:
                    raise VersionConflict("目标版本已变化")
                if goal["status"] == "closed":
                    raise ValueError("目标已关闭")
                for question_id, version in keys:
                    row = db.execute("SELECT course_id,status FROM question_versions WHERE question_id=? AND version=?", (question_id, version)).fetchone()
                    if row is None or row["course_id"] != goal["course_id"] or row["status"] != "approved":
                        raise ValueError("持续计划题池必须属于原目标课程且已审核")
                policy = {"interval_seconds": interval_seconds, "question_pool": question_pool, "actor_id": actor_id}
                db.execute("""INSERT INTO goal_plans(goal_id,policy_json,remaining_rounds,next_open_at,ends_at,status)
                    VALUES(?,?,?,?,?,'active') ON CONFLICT(goal_id) DO UPDATE SET policy_json=excluded.policy_json,
                    remaining_rounds=excluded.remaining_rounds,next_open_at=excluded.next_open_at,ends_at=excluded.ends_at,
                    status='active',reason=NULL,version=goal_plans.version+1""",
                    (goal_id, self._json(policy), future_round_count, time.time() + interval_seconds, ends_at))
                db.execute("UPDATE learning_goals SET version=version+1 WHERE id=?", (goal_id,))
                return {"goal_id": goal_id, "future_round_count": future_round_count, "ends_at": ends_at}
            return self._command(db, actor_id, goal_id, request_id,
                {"op": "configure_plan", "rounds": future_round_count, "interval": interval_seconds,
                 "ends": ends_at, "pool": question_pool, "expected_version": expected_version}, apply)

    def tick_plans(self, *, teacher_planning=None):
        from .learning_store import InvalidTransition, VersionConflict
        guard = teacher_planning.platform.transaction() if teacher_planning is not None else nullcontext()
        # The confirmed pool reuses TeacherPlanning's live source/permission
        # checks; platform remains locked until the learning transaction commits.
        with guard, self._using_connection(write=True) as db:
            plans = db.execute("""SELECT p.*,g.owner_id,g.version goal_version,g.status goal_status
                FROM goal_plans p JOIN learning_goals g ON g.id=p.goal_id WHERE p.status='active'""").fetchall()
            for plan in plans:
                now = time.time()
                if plan["goal_status"] == "closed" or plan["ends_at"] <= now or plan["remaining_rounds"] == 0:
                    db.execute("UPDATE goal_plans SET status='closed' WHERE goal_id=?", (plan["goal_id"],))
                    continue
                if plan["next_open_at"] and plan["next_open_at"] > now:
                    continue
                latest = db.execute("SELECT * FROM learning_rounds WHERE goal_id=? ORDER BY round_no DESC LIMIT 1", (plan["goal_id"],)).fetchone()
                if latest is None:
                    self._block_plan(db, plan, "请先发布第一轮")
                    continue
                tasks = db.execute("SELECT * FROM learning_tasks WHERE round_id=?", (latest["id"],)).fetchall()
                if not tasks or any(task["first_attempt_id"] is None for task in tasks):
                    self._block_plan(db, plan, "前轮仍有未完成首次作答，等待教师复核")
                    continue
                pending = db.execute("""SELECT 1 FROM attempts a JOIN learning_tasks t ON t.id=a.task_id
                    WHERE t.round_id=? AND a.status!='graded' LIMIT 1""", (latest["id"],)).fetchone()
                pending_items = db.execute("""SELECT 1 FROM attempt_items ai JOIN attempts a ON a.id=ai.attempt_id
                    JOIN learning_tasks t ON t.id=a.task_id WHERE t.round_id=? AND ai.status!='graded' LIMIT 1""", (latest["id"],)).fetchone()
                if pending or pending_items or any(task["status"] in {"grading", "awaiting_manual", "submitted"} for task in tasks):
                    self._block_plan(db, plan, "前轮评分尚未齐备")
                    continue
                frozen = db.execute("SELECT policy_json FROM round_policies WHERE round_id=?", (latest["id"],)).fetchone()
                if frozen is None:
                    self._block_plan(db, plan, "缺少教师确认的冻结政策")
                    continue
                policy, config = json.loads(frozen[0]), json.loads(plan["policy_json"])
                db.execute("SAVEPOINT bounded_plan_round")
                try:
                    if config.get("schema_version") == "teaching.confirmed.plan.v1":
                        if teacher_planning is None:
                            raise ValueError("持续计划缺少题池授权校验服务，请教师复核")
                        teacher_planning.advance_confirmed_plan(db, plan, config)
                    else:
                        refs = self._legacy_plan_refs(db, plan["goal_id"], policy, config)
                        self.publish_round(config.get("actor_id", plan["owner_id"]), plan["goal_id"], expected_version=plan["goal_version"],
                            request_id=f"scheduled:{latest['round_no'] + 1}", round_no=latest["round_no"] + 1,
                            student_ids=[task["student_id"] for task in tasks], question_versions=refs,
                            scheduled_at=None, deadline_at=min(plan["ends_at"], now + config["interval_seconds"]),
                            allow_late=policy["allow_late"], allow_retries=policy["allow_retries"],
                            allow_retry_after_deadline=policy["allow_retry_after_deadline"], common_question_ratio=100)
                except (LookupError, PermissionError, ValueError, InvalidTransition, VersionConflict) as error:
                    # No partially frozen round may survive a rejected schedule.
                    db.execute("ROLLBACK TO bounded_plan_round")
                    db.execute("RELEASE bounded_plan_round")
                    reason = str(error) if isinstance(error, (ValueError, InvalidTransition)) else "计划范围或授权已变化，请教师复核"
                    self._block_plan(db, plan, reason)
                    continue
                db.execute("RELEASE bounded_plan_round")
                db.execute("""UPDATE goal_plans SET policy_json=?,remaining_rounds=remaining_rounds-1,next_open_at=?,
                    status=CASE WHEN remaining_rounds=1 THEN 'closed' ELSE 'active' END,reason=NULL,version=version+1 WHERE goal_id=?""",
                    (self._json(config), now + config["interval_seconds"], plan["goal_id"]))

    def _legacy_plan_refs(self, db, goal_id, policy, config):
        """Keep the existing common-paper plan while rejecting used variants."""
        from .services.question_bank import question_fingerprints
        from .services.teacher_planning import TeacherPlanning
        used, used_families = set(), set()
        for row in db.execute("""SELECT ti.question_id,ti.frozen_json FROM task_items ti
            JOIN learning_tasks t ON t.id=ti.task_id WHERE t.goal_id=?""", (goal_id,)):
            question = json.loads(row["frozen_json"])
            used.add(row["question_id"])
            _, family = question_fingerprints({**question, "rubric": TeacherPlanning._rubric(question)})
            used_families.add("computed:" + family)
            used_families.update(key + ":" + question[key] for key in ("family_id", "family_sha") if question.get(key))
        slots, choices = [], []
        for ref in policy["question_versions"]:
            row = db.execute("SELECT content_json FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
            if row is None:
                raise ValueError("缺少前轮冻结题目结构")
            question = json.loads(row[0])
            slots.append((question["type"], question["points_units"]))
        for ref in config["question_pool"]:
            row = db.execute("""SELECT q.* FROM question_versions q JOIN learning_goals g ON g.course_id=q.course_id
                WHERE g.id=? AND q.question_id=? AND q.version=? AND q.status='approved'
                AND q.version=(SELECT MAX(v.version) FROM question_versions v WHERE v.question_id=q.question_id)""",
                (goal_id, ref["question_id"], ref["version"])).fetchone()
            if row is None or ref["question_id"] in used:
                continue
            question = json.loads(row["content_json"])
            _, family = question_fingerprints({**question, "rubric": TeacherPlanning._rubric(question)})
            identities = {"computed:" + family} | {key + ":" + question[key] for key in ("family_id", "family_sha") if question.get(key)}
            if identities & used_families:
                continue
            choices.append({"ref": ref, "key": (ref["question_id"], ref["version"]), "family": family,
                            "identities": identities, "group": (question["type"], question["points_units"])})
        # Same declared family or computed numeric family remains one slot.
        parents = {}
        def root(key):
            parents.setdefault(key, key)
            while parents[key] != key:
                parents[key] = parents[parents[key]]
                key = parents[key]
            return key
        for question in choices:
            identities = sorted(question["identities"])
            for key in identities[1:]:
                parents[root(key)] = root(identities[0])
        for question in choices:
            question["family"] = root(sorted(question["identities"])[0])
        chosen = TeacherPlanning._match(slots, choices)
        if not chosen or any(question is None for question in chosen):
            raise ValueError("合格新题族不足，等待备题或教师调整")
        return [question["ref"] for question in chosen]

    def _block_plan(self, db, plan, reason):
        db.execute("UPDATE goal_plans SET status='blocked',reason=?,version=version+1 WHERE goal_id=?", (reason, plan["goal_id"]))
        self._notify(db, plan["owner_id"], "goal_plan_blocked", "goal", plan["goal_id"], {"reason": reason}, key=f"plan:{plan['goal_id']}:{plan['version']}")
