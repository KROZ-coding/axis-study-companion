"""Bounded, durable round scheduling under a teacher-confirmed plan."""

import json
import time


class LearningPlanning:
    def configure_plan(self, goal_id, actor_id, *, expected_version, request_id, future_round_count,
                       interval_seconds, ends_at, question_pool):
        from .learning_store import VersionConflict
        if not 1 <= future_round_count <= 100 or not 60 <= interval_seconds <= 366 * 86400 or ends_at <= time.time():
            raise ValueError("轮数、节奏或结束时间无效")
        with self._using_connection(write=True) as db:
            goal = self._goal_row(db, goal_id, actor_id); self._teacher_write(actor_id, goal)
            def apply():
                if goal["version"] != expected_version:
                    raise VersionConflict("目标版本已变化")
                policy = {"interval_seconds": interval_seconds, "question_pool": question_pool}
                db.execute("INSERT INTO goal_plans(goal_id,policy_json,remaining_rounds,next_open_at,ends_at,status) VALUES(?,?,?,?,?,'active') ON CONFLICT(goal_id) DO UPDATE SET policy_json=excluded.policy_json,remaining_rounds=excluded.remaining_rounds,next_open_at=excluded.next_open_at,ends_at=excluded.ends_at,status='active',version=goal_plans.version+1", (goal_id, self._json(policy), future_round_count, time.time() + interval_seconds, ends_at))
                db.execute("UPDATE learning_goals SET version=version+1 WHERE id=?", (goal_id,))
                return {"goal_id": goal_id, "future_round_count": future_round_count, "ends_at": ends_at}
            return self._command(db, actor_id, goal_id, request_id, {"op": "configure_plan", "rounds": future_round_count, "interval": interval_seconds, "ends": ends_at, "pool": question_pool}, apply)

    def tick_plans(self):
        with self._using_connection(write=True) as db:
            plans = db.execute("SELECT p.*,g.owner_id,g.version goal_version,g.status goal_status FROM goal_plans p JOIN learning_goals g ON g.id=p.goal_id WHERE p.status='active'").fetchall()
            for plan in plans:
                if plan["goal_status"] == "closed" or plan["ends_at"] <= time.time() or plan["remaining_rounds"] == 0:
                    db.execute("UPDATE goal_plans SET status='closed' WHERE goal_id=?", (plan["goal_id"],))
                    continue
                if plan["next_open_at"] and plan["next_open_at"] > time.time():
                    continue
                latest = db.execute("SELECT * FROM learning_rounds WHERE goal_id=? ORDER BY round_no DESC LIMIT 1", (plan["goal_id"],)).fetchone()
                if latest is None:
                    self._block_plan(db, plan, "请先发布第一轮")
                    continue
                tasks = db.execute("SELECT * FROM learning_tasks WHERE round_id=?", (latest["id"],)).fetchall()
                if any(task["first_attempt_id"] is None for task in tasks) and (latest["deadline_at"] is None or latest["deadline_at"] > time.time()):
                    continue
                if any(task["status"] in {"grading", "awaiting_manual", "submitted"} for task in tasks):
                    self._block_plan(db, plan, "前轮评分尚未齐备")
                    continue
                frozen = db.execute("SELECT policy_json FROM round_policies WHERE round_id=?", (latest["id"],)).fetchone()
                if frozen is None:
                    self._block_plan(db, plan, "缺少教师确认的冻结政策")
                    continue
                policy, config = json.loads(frozen[0]), json.loads(plan["policy_json"])
                count = len(policy["question_versions"])
                refs = []
                used = {row[0] for row in db.execute("SELECT DISTINCT ti.question_id FROM task_items ti JOIN learning_tasks t ON t.id=ti.task_id WHERE t.goal_id=?", (plan["goal_id"],))}
                for ref in config["question_pool"]:
                    q = db.execute("SELECT status FROM question_versions WHERE question_id=? AND version=?", (ref["question_id"], ref["version"])).fetchone()
                    if q and q[0] == "approved" and ref["question_id"] not in used:
                        refs.append(ref)
                    if len(refs) == count:
                        break
                if len(refs) != count:
                    self._block_plan(db, plan, "合格新题不足，等待备题或教师调整")
                    continue
                try:
                    self.publish_round(plan["owner_id"], plan["goal_id"], expected_version=plan["goal_version"],
                        request_id=f"scheduled:{latest['round_no'] + 1}", round_no=latest["round_no"] + 1,
                        student_ids=[task["student_id"] for task in tasks], question_versions=refs,
                        scheduled_at=None, deadline_at=min(plan["ends_at"], time.time() + config["interval_seconds"]),
                        allow_late=policy["allow_late"], allow_retries=policy["allow_retries"],
                        allow_retry_after_deadline=policy["allow_retry_after_deadline"], common_question_ratio=100)
                except (LookupError, PermissionError, ValueError) as error:
                    self._block_plan(db, plan, "计划范围或授权已变化，请教师复核")
                    continue
                db.execute("UPDATE goal_plans SET remaining_rounds=remaining_rounds-1,next_open_at=?,version=version+1 WHERE goal_id=?", (time.time() + config["interval_seconds"], plan["goal_id"]))

    def _block_plan(self, db, plan, reason):
        db.execute("UPDATE goal_plans SET status='blocked',reason=?,version=version+1 WHERE goal_id=?", (reason, plan["goal_id"]))
        self._notify(db, plan["owner_id"], "goal_plan_blocked", "goal", plan["goal_id"], {"reason": reason}, key=f"plan:{plan['goal_id']}:{plan['version']}")
