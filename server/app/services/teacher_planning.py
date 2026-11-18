"""Teacher-selected paper previews and atomic publication through LearningStore.

No model is called here. A preview locks concrete approved versions, and publish
revalidates that snapshot under the same write transaction that freezes tasks.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import secrets
import time
from uuid import uuid4

from ..learning_store import InvalidTransition
from ..schemas.v1_teacher_planning import PlanPrepare, TeachingPlanConfig
from ..teacher_planning_store import GenerationIntentContent, draft_view, ensure_schema
from .question_bank import QuestionBank, normalize_knowledge_points, question_fingerprints, validate_question


TYPES = ("single_choice", "fill", "judge", "text")
COMMON_RATIOS = {"light": 25, "balanced": 50, "heavy": 75}
MINUTES = {"single_choice": 2, "fill": 3, "judge": 1, "text": 8}


class PlanningConflict(InvalidTransition):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TeacherPlanning:
    PREVIEW_SECONDS = 600

    def __init__(self, learning, platform, content, *, clock=None):
        self.learning, self.platform, self.content = learning, platform, content
        self.clock = clock or time.time
        with self.learning._using_connection(write=True) as db:
            ensure_schema(db)

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _hash(cls, value):
        return hashlib.sha256(cls._json(value).encode()).hexdigest()

    @staticmethod
    def _config(value):
        model = value if isinstance(value, TeachingPlanConfig) else TeachingPlanConfig.model_validate(value)
        return model.model_dump()

    def _scope(self, actor_id, config, *, students=True):
        actor = self.platform.get_user(actor_id)
        classroom = self.platform.get_class(config["class_id"])
        if (actor is None or actor["status"] != "active" or actor["role"] != "teacher"
                or classroom is None or classroom["course_id"] != config["course_id"]
                or not self.platform.can_access_class(actor_id, config["class_id"], teaching=True)
                or not self.platform.can_access_course(actor_id, config["course_id"])):
            raise PermissionError("课程或班级不存在或不可管理")
        selected = []
        if students:
            for student_id in config["student_ids"]:
                student = self.platform.get_user(student_id)
                if (student is None or student["status"] != "active" or student["role"] != "student"
                        or self.platform.get_class_membership(config["class_id"], student_id) is None
                        or self.platform.get_course_membership(config["course_id"], student_id) is None):
                    raise ValueError("发布范围包含不属于该班的有效学生")
                selected.append({"student_id": student_id, "display_name": student["display_name"]})
        return selected

    def _time_errors(self, config):
        now = self.clock()
        errors = []
        if config["deadline_at"] is not None and config["deadline_at"] <= now:
            errors.append({"code": "deadline_passed", "message": "截止时间已过，请选择未来时间后重新预览"})
        if config["scheduled_at"] is not None and config["scheduled_at"] <= now:
            errors.append({"code": "schedule_passed", "message": "计划开放时间已过，请修改时间或选择立即发布"})
        return errors

    def _confirmed_sources(self, actor_id, config, block_ids, *, purpose):
        sources = self.content.confirmed_sources(config["course_id"], block_ids,
                                                 purpose=purpose, class_id=config["class_id"])
        for source in sources:
            if source.get("visibility") == "private" and source.get("owner_id") != actor_id:
                raise PermissionError("来源属于其他上传者的私有材料")
        return sources

    def _knowledge(self, actor_id, config):
        result = {}
        for item in self.content.list_knowledge_points(config["course_id"]):
            try:
                self._confirmed_sources(actor_id, config, item["source_block_ids"], purpose="retrieval")
            except (ValueError, PermissionError):
                continue
            result[item["id"]] = item
        return result

    @staticmethod
    def _rubric(question):
        # Older authored versions use score (points); the bank uses score_units.
        # Both are validated without changing the authoritative frozen question.
        rubric = []
        for entry in question.get("rubric", []):
            if not isinstance(entry, dict) or set(entry) - {"point", "score", "score_units"}:
                raise ValueError("评分标准无效")
            units = entry.get("score_units")
            if units is None:
                try:
                    value = Decimal(str(entry.get("score"))) * 100
                except InvalidOperation:
                    raise ValueError("评分标准分值无效") from None
                if not value.is_finite() or value != value.to_integral_value():
                    raise ValueError("评分标准分值无效")
                units = int(value)
            if "score" in entry and Decimal(str(entry["score"])) * 100 != units:
                raise ValueError("评分标准分值不一致")
            rubric.append({"point": entry.get("point"), "score_units": units})
        if question.get("type") == "text" and not rubric:
            raise ValueError("作答题必须有明确评分标准")
        return rubric

    def _question(self, row, actor_id, config, knowledge, source_cache):
        question = json.loads(row["content_json"])
        if (question.get("generation_intent", "production") != "production"
                or question.get("usage_scope", "production") != "production"):
            return None
        if question.get("type") not in TYPES or question.get("class_id") not in (None, config["class_id"]):
            return None
        normalized = validate_question({**question, "rubric": self._rubric(question)})
        units = int(Decimal(str(normalized["points"])) * 100)
        if question.get("points_units") != units:
            raise ValueError("题目分值无效")
        difficulty = question.get("difficulty")
        if difficulty not in (None, "easy", "medium", "hard"):
            raise ValueError("题目难度无效")
        if config["difficulty"] != "any" and difficulty != config["difficulty"]:
            return None
        points = question.get("knowledge_points", [])
        if not isinstance(points, list) or any(not isinstance(p, str) for p in points):
            raise ValueError("题目知识点无效")
        if any(p not in knowledge for p in points):
            cache_key = ("knowledge", tuple(points))
            if cache_key not in source_cache:
                source_cache[cache_key] = normalize_knowledge_points(self.content, config["course_id"], points)
            points = source_cache[cache_key]
        if any(p not in knowledge for p in points) or len(set(points)) != len(points):
            raise ValueError("题目知识点尚未确认或不可访问")
        if config["knowledge_points"] and not set(points).intersection(config["knowledge_points"]):
            return None
        anchors = question.get("source_anchors", [])
        if not isinstance(anchors, list):
            raise ValueError("题目来源无效")
        safe_sources, source_state = [], []
        for anchor in anchors:
            if not isinstance(anchor, dict) or not isinstance(anchor.get("block_id"), str):
                raise ValueError("题目来源缺少真实区块")
            block_id = anchor["block_id"]
            if block_id not in source_cache:
                source_cache[block_id] = self._confirmed_sources(actor_id, config, [block_id], purpose="generation")[0]
            source = source_cache[block_id]
            # source_revision belongs to the whole material: confirming another
            # block changes it without changing this immutable source or its rights.
            for key, actual in (("block_id", source["id"]), ("anchor", source["anchor"]),
                                ("file_id", source["file_id"]), ("sha256", source["sha256"])):
                if key in anchor and anchor[key] != actual:
                    raise ValueError("题目引用的来源已变化")
            safe_sources.append({"block_id": block_id, "file_id": source["file_id"], "anchor": source["anchor"]})
            source_state.append({key: source.get(key) for key in (
                "id", "file_id", "anchor", "sha256", "status", "rights_confirmed", "allow_generation",
                "visibility", "owner_id", "course_id", "class_id", "license_code", "license_note")})
        _, computed_family = question_fingerprints(normalized)
        families = {"computed:" + computed_family}
        for field in ("family_id", "family_sha"):
            if question.get(field):
                if not isinstance(question[field], str):
                    raise ValueError("题目家族标识无效")
                families.add(field + ":" + question[field])
        ref = {"question_id": row["question_id"], "version": row["version"]}
        summary = {**ref, "type": normalized["type"], "stem": normalized["stem"], "options": normalized["options"],
                   "points_units": units, "points": units / 100, "difficulty": difficulty,
                   "knowledge_points": points, "source_anchors": safe_sources,
                   "source_status": "confirmed" if anchors else "not_recorded"}
        return {"ref": ref, "summary": summary, "families": families,
                "key": (row["question_id"], row["version"]), "group": (normalized["type"], units),
                "fingerprint": self._hash({"content": question, "sources": source_state,
                                            "knowledge": [knowledge[p] for p in points]})}

    def _catalog(self, db, actor_id, config, knowledge):
        pool, excluded, sources = [], Counter(), {}
        rows = db.execute("""SELECT q.* FROM question_versions q
            WHERE q.course_id=? AND q.status='approved'
              AND q.version=(SELECT MAX(latest.version) FROM question_versions latest
                             WHERE latest.question_id=q.question_id)
            ORDER BY q.question_id,q.version""", (config["course_id"],))
        for row in rows:
            try:
                question = self._question(row, actor_id, config, knowledge, sources)
                if question is not None:
                    pool.append(question)
            except (ValueError, TypeError, KeyError, InvalidOperation, PermissionError):
                excluded["invalid_question_metadata"] += 1
        # Union explicit and computed identities: a variant cannot bypass family
        # exclusion merely by using a different family_id or point value.
        parents = {}
        def root(key):
            parents.setdefault(key, key)
            while parents[key] != key:
                parents[key] = parents[parents[key]]
                key = parents[key]
            return key
        for question in pool:
            keys = sorted(question["families"])
            for key in keys[1:]:
                parents[root(key)] = root(keys[0])
        for question in pool:
            question["family"] = root(sorted(question["families"])[0])
        return pool, excluded

    @staticmethod
    def _match(slots, pool, *, excluded_families=frozenset(), rank=None):
        """Bipartite slot/family matching, with same-type/same-score replacements."""
        candidates = []
        for kind, units in slots:
            choices = [q for q in pool if q["family"] not in excluded_families
                       and q["group"][0] == kind and (units is None or q["group"][1] == units)]
            candidates.append(sorted(choices, key=rank or (lambda q: q["key"])))
        owners, chosen = {}, {}
        def augment(slot, visited):
            for question in candidates[slot]:
                family = question["family"]
                if family in visited:
                    continue
                visited.add(family)
                previous = owners.get(family)
                if previous is None or augment(previous, visited):
                    owners[family] = slot
                    chosen[slot] = question
                    return True
            return False
        for slot in sorted(range(len(slots)), key=lambda i: len({q["family"] for q in candidates[i]})):
            augment(slot, set())
        return [chosen.get(i) for i in range(len(slots))]

    def _suggestions(self, db, config, knowledge):
        result, aliases = {}, {}
        # Only the first completed attempt per task contributes. Submitted,
        # partially graded, manual-pending and retry scores do not imply mastery.
        for student in config["student_ids"]:
            rows = db.execute("""SELECT a.id attempt_id,ai.score_units,ti.frozen_json
                FROM learning_tasks t JOIN learning_goals g ON g.id=t.goal_id
                JOIN attempts a ON a.id=t.first_attempt_id AND a.status='graded'
                JOIN attempt_items ai ON ai.attempt_id=a.id AND ai.status='graded'
                JOIN task_items ti ON ti.task_id=t.id AND ti.question_id=ai.question_id
                WHERE g.course_id=? AND g.class_id=? AND t.student_id=?
                ORDER BY a.submitted_at,a.id,ti.position""",
                (config["course_id"], config["class_id"], student))
            attempts, count, earned, maximum, point_scores = set(), 0, 0, 0, defaultdict(lambda: [0, 0])
            for row in rows:
                frozen = json.loads(row["frozen_json"])
                units = frozen.get("points_units")
                if type(units) is not int or units <= 0 or type(row["score_units"]) is not int:
                    continue
                attempts.add(row["attempt_id"])
                count += 1
                earned += row["score_units"]
                maximum += units
                points = set()
                for point in frozen.get("knowledge_points", []):
                    if not isinstance(point, str):
                        continue
                    if point not in knowledge:
                        if point not in aliases:
                            try:
                                aliases[point] = normalize_knowledge_points(self.content, config["course_id"], [point])[0]
                            except (ValueError, PermissionError):
                                aliases[point] = None
                        point = aliases[point]
                    if point in knowledge:
                        points.add(point)
                for point in points:
                    point_scores[point][0] += row["score_units"]
                    point_scores[point][1] += units
            weak = sorted(p for p, (score, total) in point_scores.items() if score / total < .6)
            result[student] = {
                "student_id": student, "status": "graded_evidence" if count else "pending_evaluation",
                "graded_attempt_count": len(attempts), "graded_question_count": count,
                "score_ratio": round(earned / maximum, 4) if maximum else None,
                "weak_knowledge_points": weak,
                "message": (f"依据本班已完成评分的 {count} 道首次作答题；建议优先练习有薄弱证据的知识点。"
                            if weak else f"已有 {count} 道首次作答题完成评分，尚无可定位的薄弱知识点。")
                           if count else "待评估：暂无本班已完成评分的首次作答证据，按教师设置配卷。",
            }
        return result

    def _build(self, db, actor_id, config, *, frozen_suggestions=None, origin_goal_id=None, paper_overrides=None, confirmed_pool=None):
        students = self._scope(actor_id, config)
        knowledge = self._knowledge(actor_id, config)
        errors = self._time_errors(config)
        if set(config["knowledge_points"]) - set(knowledge):
            errors.append({"code": "knowledge_not_confirmed", "message": "所选知识点不是本课程可访问的已确认知识点"})
        pool, excluded = self._catalog(db, actor_id, config, knowledge)
        if origin_goal_id is not None:
            pool = self._unused_goal_pool(db, origin_goal_id, pool)
        if confirmed_pool is not None:
            allowed = {(ref["question_id"], ref["version"]): ref["fingerprint"] for ref in confirmed_pool}
            pool = [q for q in pool if allowed.get(q["key"]) == q["fingerprint"]]
        suggestions = frozen_suggestions if frozen_suggestions is not None else self._suggestions(db, config, knowledge)
        ratio = COMMON_RATIOS[config["common_level"]]
        target = config["question_count"] if len(students) == 1 else math.ceil(config["question_count"] * ratio / 100)
        slots = [(kind, None) for kind in TYPES for _ in range(config["question_types"][kind])]
        diversity = Counter()
        for group in {q["group"] for q in pool}:
            diversity[group] = len({q["family"] for q in pool if q["group"] == group})
        baseline = self._match(slots, pool, rank=lambda q: (-diversity[q["group"]], q["group"][1], q["key"]))
        deficits = []
        for kind in TYPES:
            required = config["question_types"][kind]
            assigned = sum(q is not None and q["group"][0] == kind for q in baseline)
            if assigned < required:
                deficits.append({"question_type": kind, "required": required, "available": assigned,
                                 "missing": required - assigned,
                                 "reason": "符合课程、最新审核状态、筛选条件和家族去重要求的题目不足"})
        if deficits:
            errors.append({"code": "question_shortage", "message": "题库不足，需补齐指定题型后重新预览"})
        papers, common = {}, []
        if not deficits:
            baseline = [q for q in baseline if q is not None]
            common = sorted(baseline, key=lambda q: (diversity[q["group"]], TYPES.index(q["group"][0]), q["key"]))[:target]
            common_keys = {q["key"] for q in common}
            personal_slots = [q["group"] for q in baseline if q["key"] not in common_keys]
            common_families = {q["family"] for q in common}
            intersection, usage = set(), Counter()
            for student in students:
                sid = student["student_id"]
                weak = set(suggestions[sid]["weak_knowledge_points"])
                def rank(q):
                    preferred = suggestions[sid].get("preferred_difficulty")
                    return (q["summary"]["difficulty"] != preferred if preferred else False,
                            q["key"] in intersection, -len(weak.intersection(q["summary"]["knowledge_points"])),
                            usage[q["key"]], self._hash([sid, q["key"]]))
                personal = self._match(personal_slots, pool, excluded_families=common_families, rank=rank)
                if any(q is None for q in personal):
                    # Baseline guarantees a solution; fail closed if that invariant is broken.
                    raise RuntimeError("validated paper slots could not be filled")
                paper = sorted(common + personal, key=lambda q: (TYPES.index(q["group"][0]), q["key"]))
                papers[sid] = paper
                keys = {q["key"] for q in personal}
                intersection = keys if not usage else intersection & keys
                usage.update(keys)
        if paper_overrides:
            current = {q["key"]: q for q in pool}
            valid = set(paper_overrides).issubset(config["student_ids"]) and bool(papers)
            for sid, refs in paper_overrides.items():
                keys = [(ref["question_id"], ref["version"]) for ref in refs]
                chosen = [current[key] for key in keys if key in current]
                if (len(chosen) != len(keys) or len(chosen) != config["question_count"]
                        or len({q["family"] for q in chosen}) != len(chosen)
                        or Counter(q["group"][0] for q in chosen) != Counter({k: v for k, v in config["question_types"].items() if v})):
                    valid = False
                papers[sid] = chosen
            structures = {tuple(sorted(q["group"] for q in paper)) for paper in papers.values()}
            shared = set.intersection(*({q["key"] for q in paper} for paper in papers.values())) if papers else set()
            if not valid or len(structures) != 1 or len(shared) < target:
                errors.append({"code": "invalid_paper_override", "message": "编辑卷须使用未练过的合格题族，保持等题型、等分值结构与共同题比例"})
                papers = {}
        actual = set.intersection(*({q["key"] for q in paper} for paper in papers.values())) if papers else set()
        selected = {q["key"]: q for paper in papers.values() for q in paper}
        common_count = len(actual)
        total_units = sum(q["group"][1] for q in next(iter(papers.values()), []))
        warnings = []
        if common_count > target:
            warnings.append({"code": "additional_common_questions", "message": "等题型、等分值题池有限，部分个人题仍为全员共同题，已按实际交集展示"})
        if any(not q["summary"]["source_anchors"] for q in selected.values()):
            warnings.append({"code": "source_not_recorded", "message": "部分已审核题目未记录来源，预览不推断或补造来源与知识点"})
        if excluded:
            warnings.append({"code": "ineligible_questions", "message": f"已排除 {sum(excluded.values())} 道来源、知识点或评分结构无效的题目"})
        for student in students:
            paper = papers.get(student["student_id"], [])
            student.update(suggestion=suggestions[student["student_id"]], question_count=len(paper),
                           total_score_units=total_units if paper else 0, total_score=total_units / 100 if paper else 0,
                           question_types={kind: sum(q["group"][0] == kind for q in paper) for kind in TYPES},
                           questions=[{**q["summary"], "common": q["key"] in actual} for q in paper])
        ready = not errors
        result = {
            "config": config, "config_fingerprint": self._hash(config), "readiness": "ready" if ready else "blocked",
            "ready": ready, "errors": errors, "deficits": deficits, "warnings": warnings,
            "question_count": config["question_count"], "total_points": total_units / 100,
            "estimated_minutes": sum(config["question_types"][kind] * MINUTES[kind] for kind in TYPES),
            "estimate_basis": "按单选2分钟、填空3分钟、判断1分钟、作答8分钟每题估算，非学习表现预测",
            "common_level": config["common_level"], "requested_common_ratio": ratio,
            "target_common_count": target, "common_count": common_count,
            "actual_common_ratio": round(common_count * 100 / config["question_count"], 4),
            "students": students, "suggestions": list(suggestions.values()),
            "questions": [{**q["summary"], "common": key in actual} for key, q in sorted(selected.items())],
            "common_questions": [selected[key]["summary"] for key in sorted(actual)],
            "counts": {"student_count": len(students), "question_count": config["question_count"],
                       "common_count": common_count, "personal_count": config["question_count"] - common_count,
                       "question_types": config["question_types"]},
            "scores": {"total_score_units": total_units, "total_score": total_units / 100},
            "preview_token": None, "preview_expires_at": None,
        }
        snapshot = {"config": config, "papers": {sid: [q["ref"] for q in paper] for sid, paper in papers.items()},
                    "questions": [{**q["ref"], "fingerprint": q["fingerprint"]} for _, q in sorted(selected.items())],
                    "common_count": common_count, "actual_common_ratio": result["actual_common_ratio"],
                    "total_score_units": total_units}
        return result, snapshot

    def preview(self, actor_id, config):
        config = self._config(config)
        with self.learning._using_connection(write=True) as db:
            result, snapshot = self._build(db, actor_id, config)
            if result["ready"]:
                token = secrets.token_urlsafe(32)
                now = self.clock()
                expires = now + self.PREVIEW_SECONDS
                db.execute("INSERT INTO teacher_plan_previews VALUES(?,?,?,?,?,?,NULL)",
                           (self._hash(token), actor_id, result["config_fingerprint"], self._json(snapshot), now, expires))
                result.update(preview_token=token, preview_expires_at=expires)
            return result

    def _save_draft(self, db, actor_id, config):
        digest, now = self._hash(config), self.clock()
        row = db.execute("SELECT * FROM teacher_plan_drafts WHERE owner_id=? AND config_fingerprint=?", (actor_id, digest)).fetchone()
        if row is None:
            db.execute("INSERT INTO teacher_plan_drafts VALUES(?,?,?,?,?,?,?,?)",
                       ("plan_" + uuid4().hex, actor_id, config["course_id"], config["class_id"], digest, self._json(config), now, now))
        else:
            db.execute("UPDATE teacher_plan_drafts SET updated_at=? WHERE id=?", (now, row["id"]))
        return draft_view(db.execute("SELECT * FROM teacher_plan_drafts WHERE owner_id=? AND config_fingerprint=?", (actor_id, digest)).fetchone())

    def save_draft(self, actor_id, config, *, request_id):
        config = self._config(config)
        with self.learning._using_connection(write=True) as db:
            self._scope(actor_id, config)
            return self.learning._command(db, actor_id, "teacher_planning:draft", request_id, config,
                                          lambda: self._save_draft(db, actor_id, config))

    def list_drafts(self, actor_id, *, class_id, limit=50):
        classroom = self.platform.get_class(class_id)
        if classroom is None:
            raise PermissionError("班级不存在或不可管理")
        self._scope(actor_id, {"class_id": class_id, "course_id": classroom["course_id"]}, students=False)
        with self.learning._using_connection() as db:
            rows = db.execute("SELECT * FROM teacher_plan_drafts WHERE owner_id=? AND class_id=? ORDER BY updated_at DESC,id LIMIT ?",
                              (actor_id, class_id, limit))
            return {"items": [draft_view(row) for row in rows]}

    def publish(self, actor_id, config, *, preview_token, request_id):
        config = self._config(config)
        payload = {"config": config, "preview_token": preview_token}
        if getattr(self.learning._local, "connection", None) is not None:
            raise RuntimeError("publish must own the learning transaction")
        # Acquire platform before learning, matching platform-authorized callers.
        # BEGIN IMMEDIATE fences membership/permission writers across connections;
        # unwind learning first so the guard lasts through its commit or rollback.
        # Never join an outer learning transaction: that would invert this order
        # and let the caller commit tasks after the platform guard was released.
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            # Revalidate current rights even when replaying an old successful command.
            self._scope(actor_id, config)
            def apply():
                row = db.execute("SELECT * FROM teacher_plan_previews WHERE token_hash=? AND owner_id=?",
                                 (self._hash(preview_token), actor_id)).fetchone()
                if row is None:
                    raise PlanningConflict("preview_required", "请先生成当前配置的可发布预览")
                if row["config_fingerprint"] != self._hash(config):
                    raise PlanningConflict("config_changed", "配置已改变，请重新预览后发布")
                if row["published_result_json"]:
                    return json.loads(row["published_result_json"])
                if row["expires_at"] <= self.clock():
                    raise PlanningConflict("preview_expired", "预览已过期，请重新预览后发布")
                if self._time_errors(config):
                    raise PlanningConflict("preview_stale", "开放或截止时间已过，请重新预览")
                snapshot = json.loads(row["snapshot_json"])
                knowledge = self._knowledge(actor_id, config)
                if set(config["knowledge_points"]) - set(knowledge):
                    raise PlanningConflict("preview_stale", "所选知识点已不可用，请重新预览")
                pool, _ = self._catalog(db, actor_id, config, knowledge)
                current = {q["key"]: q for q in pool}
                for ref in snapshot["questions"]:
                    question = current.get((ref["question_id"], ref["version"]))
                    if question is None or question["fingerprint"] != ref["fingerprint"]:
                        raise PlanningConflict("preview_stale", "预览题目版本、审核状态或来源已变化，请重新预览")
                for refs in snapshot["papers"].values():
                    questions = [current[(ref["question_id"], ref["version"])] for ref in refs]
                    if (len({q["family"] for q in questions}) != len(questions)
                            or Counter(q["group"][0] for q in questions) != Counter({k: v for k, v in config["question_types"].items() if v})
                            or sum(q["group"][1] for q in questions) != snapshot["total_score_units"]):
                        raise PlanningConflict("preview_stale", "预览卷结构已变化，请重新预览")
                self._scope(actor_id, config)
                goal = self.learning.create_goal(actor_id, config["title"], config["description"],
                                                 course_id=config["course_id"], class_id=config["class_id"])
                publication = self.learning.publish_round(
                    actor_id, goal["id"], expected_version=goal["version"], request_id=request_id,
                    round_no=1, student_ids=config["student_ids"],
                    question_versions=snapshot["papers"][config["student_ids"][0]],
                    personal_question_versions=snapshot["papers"],
                    common_question_ratio=COMMON_RATIOS[config["common_level"]],
                    **{key: config[key] for key in ("deadline_at", "scheduled_at", "allow_late", "allow_retries", "allow_retry_after_deadline")},
                )
                result = {**publication, "common_count": snapshot["common_count"],
                          "actual_common_ratio": snapshot["actual_common_ratio"], "config_fingerprint": self._hash(config)}
                db.execute("UPDATE teacher_plan_previews SET published_result_json=? WHERE token_hash=?", (self._json(result), row["token_hash"]))
                return result
            return self.learning._command(db, actor_id, "teacher_planning:publish", request_id, payload, apply)

    def prepare(self, actor_id, config, *, request_id, source_block_ids=None, external_processing_allowed=False):
        request = PlanPrepare.model_validate({"config": self._config(config), "request_id": request_id,
            "source_block_ids": source_block_ids or [], "external_processing_allowed": external_processing_allowed})
        config, source_block_ids = request.config.model_dump(), request.source_block_ids
        external_processing_allowed, request_id = request.external_processing_allowed, request.request_id
        payload = {"config": config, "source_block_ids": source_block_ids, "external_processing_allowed": external_processing_allowed}
        # Persist the draft and generation intent before touching the other DB.
        # A process restart can replay the same request to fill missing jobs.
        with self.learning._using_connection(write=True) as db:
            self._scope(actor_id, config)
            def apply():
                draft = self._save_draft(db, actor_id, config)
                preview, _ = self._build(db, actor_id, config)
                specs = []
                status = "ready" if preview["ready"] else "source_required"
                if (preview["deficits"] and source_block_ids and external_processing_allowed is True
                        and all(error["code"] == "question_shortage" for error in preview["errors"])):
                    if len(config["knowledge_points"]) > 32:
                        raise ValueError("每次补题最多选择32个已确认知识点")
                    if not self.platform.can_access_course(actor_id, config["course_id"], teaching=True):
                        raise PermissionError("需要课程材料管理权限才能申请生成")
                    sources = self._confirmed_sources(actor_id, config, source_block_ids, purpose="generation")
                    QuestionBank._approved_blocks(sources, actor_id)
                    for deficit in preview["deficits"]:
                        remaining = deficit["missing"]
                        while remaining:
                            count = min(remaining, 10)
                            specs.append({"id": "planjob_" + self._hash([actor_id, request_id, len(specs)])[:32],
                                          "question_type": deficit["question_type"], "count": count})
                            remaining -= count
                    status = "generation_pending"
                return {"draft": draft, "preview": preview, "status": status, "jobs": specs,
                        "source_requirement": {"required": bool(preview["deficits"]),
                            "message": "补题须在题库选择真实已确认来源并明确允许外部处理；生成审核完成后需重新预览并手动发布",
                            "course_id": config["course_id"], "class_id": config["class_id"],
                            "question_types": {d["question_type"]: d["missing"] for d in preview["deficits"]},
                            "difficulty": config["difficulty"], "knowledge_points": config["knowledge_points"]},
                        "auto_publish": False}
            intent = self.learning._command(db, actor_id, "teacher_planning:prepare", request_id, payload, apply)
        jobs = []
        for spec in intent["jobs"]:
            # Permissions are checked again on a replay before any job is created.
            self._scope(actor_id, config)
            if not self.platform.can_access_course(actor_id, config["course_id"], teaching=True):
                raise PermissionError("需要课程材料管理权限才能申请生成")
            instruction = (f"仅补充 {spec['question_type']} 题型；每题须来自所选确认来源；"
                           f"难度筛选：{config['difficulty']}。作答题须给出合计等于题目分值的rubric。")
            job = QuestionBank(GenerationIntentContent(self.content, spec["id"]), self.learning).create_generation(
                course_id=config["course_id"], class_id=config["class_id"], owner_id=actor_id,
                source_block_ids=source_block_ids, external_processing_allowed=external_processing_allowed,
                count=spec["count"], question_types={spec["question_type"]: spec["count"]},
                difficulty=None if config["difficulty"] == "any" else config["difficulty"],
                knowledge_points=config["knowledge_points"], instruction=instruction,
            )
            jobs.append({**spec, "status": job["status"], "stage": job["stage"]})
        return {**intent, "jobs": jobs}

    def _unused_goal_pool(self, db, goal_id, pool):
        """Exclude all historically exposed families, including numeric variants."""
        used_ids, used_families = set(), set()
        for row in db.execute("""SELECT ti.question_id,ti.frozen_json FROM task_items ti
                JOIN learning_tasks t ON t.id=ti.task_id WHERE t.goal_id=?""", (goal_id,)):
            used_ids.add(row["question_id"])
            question = json.loads(row["frozen_json"])
            try:
                _, family = question_fingerprints({**question, "rubric": self._rubric(question)})
            except (ValueError, TypeError, KeyError):
                raise PlanningConflict("historical_family_invalid", "历史题族无法核对，须先复核原目标题目") from None
            used_families.add("computed:" + family)
            used_families.update(field + ":" + question[field] for field in ("family_id", "family_sha") if question.get(field))
        # The catalog already unions identities across candidate questions. If
        # one identity was used, exclude the entire connected candidate family.
        blocked = {q["family"] for q in pool if q["key"][0] in used_ids or q["families"] & used_families}
        return [q for q in pool if q["family"] not in blocked]

    @staticmethod
    def _next_view(row):
        result = dict(row)
        for field in ("config", "overrides", "basis", "published_result"):
            encoded = result.pop(field + "_json")
            result["paper_overrides" if field == "overrides" else field] = json.loads(encoded) if encoded else None
        result["allocation_mode"] = "deterministic_teacher_confirmed"
        result["bounded_plan_supported"] = True
        return result

    def _next_access(self, db, actor_id, draft_id, *, write=False):
        row = db.execute("SELECT * FROM teacher_next_round_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise LookupError("下一轮草稿不存在")
        actor, classroom = self.platform.get_user(actor_id), self.platform.get_class(row["class_id"])
        if (not actor or actor["status"] != "active" or actor["role"] != "teacher" or not classroom
                or classroom["course_id"] != row["course_id"]
                or not self.platform.can_access_class(actor_id, row["class_id"], teaching=write)):
            raise PermissionError("下一轮草稿不存在或当前权限不足")
        result = self._next_view(row)
        goal = db.execute("SELECT version FROM learning_goals WHERE id=?", (row["origin_goal_id"],)).fetchone()
        result["current_goal_version"] = goal[0] if goal else None
        plan = db.execute("SELECT * FROM goal_plans WHERE goal_id=?", (row["origin_goal_id"],)).fetchone()
        result["bounded_plan"] = self._bounded_view(plan) if plan else None
        return result

    @staticmethod
    def _bounded_view(row):
        policy = json.loads(row["policy_json"])
        return {"goal_id": row["goal_id"], "status": row["status"], "remaining_rounds": row["remaining_rounds"],
                "next_open_at": row["next_open_at"], "ends_at": row["ends_at"], "reason": row["reason"], "version": row["version"],
                "interval_seconds": policy["interval_seconds"], "source_draft_id": policy.get("source_draft_id"),
                "question_pool": policy["question_pool"]}

    @staticmethod
    def _next_revision(draft, expected_revision):
        if type(expected_revision) is not int or expected_revision != draft["revision"]:
            raise PlanningConflict("draft_revision_conflict", "草稿已在其他页面修改，请重新载入")
        if draft["status"] != "draft":
            raise PlanningConflict("draft_already_published", "此草稿已经发布，不能再次修改或生成新轮次")

    def _next_guard(self, db, actor_id, draft, *, require_finished=True):
        goal = self.learning._goal_row(db, draft["origin_goal_id"], actor_id)
        self.learning._teacher_write(actor_id, goal)
        if (goal["class_id"] != draft["class_id"] or goal["course_id"] != draft["course_id"]
                or goal["version"] != draft["goal_version"]):
            raise PlanningConflict("goal_version_conflict", "原目标版本或范围已改变，请从新报告重新创建下一轮草稿")
        if goal["status"] in {"closed", "paused"}:
            raise PlanningConflict("goal_unavailable", "原目标已关闭或暂停")
        latest = db.execute("SELECT * FROM learning_rounds WHERE goal_id=? ORDER BY round_no DESC LIMIT 1", (goal["id"],)).fetchone()
        if latest is None or latest["round_no"] + 1 != draft["next_round"]:
            raise PlanningConflict("next_round_conflict", "原目标的最新轮次已改变，请重新创建草稿")
        plan = db.execute("SELECT status FROM goal_plans WHERE goal_id=?", (goal["id"],)).fetchone()
        if plan and plan["status"] == "active":
            raise PlanningConflict("bounded_plan_active", "请先暂停原目标的持续计划，再确认单轮发布")
        if require_finished:
            tasks = db.execute("SELECT id,status,first_attempt_id FROM learning_tasks WHERE round_id=?", (latest["id"],)).fetchall()
            if not tasks or any(task["first_attempt_id"] is None for task in tasks):
                raise PlanningConflict("previous_round_incomplete", "前轮仍有未完成首次作答，暂不能发布下一轮")
            pending = db.execute("""SELECT 1 FROM attempts a JOIN learning_tasks t ON t.id=a.task_id
                WHERE t.round_id=? AND a.status!='graded' LIMIT 1""", (latest["id"],)).fetchone()
            pending_items = db.execute("""SELECT 1 FROM attempt_items ai JOIN attempts a ON a.id=ai.attempt_id
                JOIN learning_tasks t ON t.id=a.task_id WHERE t.round_id=? AND ai.status!='graded' LIMIT 1""", (latest["id"],)).fetchone()
            if pending or pending_items or any(task["status"] in {"submitted", "grading", "awaiting_manual"} for task in tasks):
                raise PlanningConflict("previous_round_ungraded", "前轮仍有待评分或人工复核，暂不能发布下一轮")
        return goal, latest

    def _next_config(self, config, draft):
        config = self._config(config)
        if config["class_id"] != draft["class_id"] or config["course_id"] != draft["course_id"]:
            raise ValueError("下一轮不能改变原目标的课程或班级")
        if set(config["student_ids"]) - set(draft["basis"]["students"]):
            raise ValueError("下一轮只能选择冻结报告中的学生")
        return config

    def _next_suggestions(self, draft):
        result = {}
        preferences = {"mastered": "hard", "developing": "medium", "needs_practice": "easy", "insufficient_evidence": None}
        for sid in draft["config"]["student_ids"]:
            fact = draft["basis"]["students"][sid]
            status = fact["status"]
            result[sid] = {"student_id": sid, "student_code": fact["student_code"], "status": status,
                           "percent": fact["percent"], "independent_families": fact["independent_families"],
                           "preferred_difficulty": preferences[status], "weak_knowledge_points": [],
                           "evidence_refs": fact["evidence_refs"], "basis": "frozen_goal_mastery",
                           "message": "按冻结目标掌握度规则优先匹配难度；没有知识点薄弱映射，教师可调整题目。"
                           if status != "insufficient_evidence" else "独立证据不足，待评估；按教师配置取新题，不推断薄弱。"}
        return result

    def create_next_round_draft(self, actor_id, insight, *, request_id, config=None):
        supplied = self._config(config) if config is not None else None
        payload = {"insight_id": insight["id"], "config": supplied}
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            # Current class rights are checked before command replay as well.
            self._scope(actor_id, {"class_id": insight["class_id"], "course_id": insight["course_id"]}, students=False)
            def apply():
                if insight["status"] != "completed":
                    raise PlanningConflict("insight_not_completed", "请等待有效冻结总结完成")
                latest = db.execute("SELECT * FROM learning_rounds WHERE goal_id=? ORDER BY round_no DESC LIMIT 1", (insight["origin_goal_id"],)).fetchone()
                if latest is None:
                    raise PlanningConflict("previous_round_required", "原目标尚未发布前轮")
                frozen = insight["input"]
                students = {frozen["student_codes"][fact["student_code"]]: fact for fact in frozen["model_input"]["facts"]["students"]}
                basis = {"students": students, "input_hash": insight["input_hash"], "snapshot_sha256": insight["snapshot_sha256"],
                         "source_revision": insight["source_revision"]}
                draft = {"id": "next_" + uuid4().hex, "owner_id": actor_id, "course_id": insight["course_id"], "class_id": insight["class_id"],
                         "insight_id": insight["id"], "origin_goal_id": insight["origin_goal_id"],
                         "source_report_revision": insight["source_report_revision"], "goal_version": insight["goal_version"],
                         "next_round": latest["round_no"] + 1, "basis": basis}
                goal, _ = self._next_guard(db, actor_id, draft, require_finished=False)
                selected = supplied
                if selected is None:
                    policy_row = db.execute("SELECT policy_json FROM round_policies WHERE round_id=?", (latest["id"],)).fetchone()
                    if policy_row is None:
                        raise PlanningConflict("previous_policy_missing", "前轮缺少冻结教师政策，请提供明确组卷配置")
                    policy = json.loads(policy_row[0])
                    first = db.execute("SELECT id FROM learning_tasks WHERE round_id=? ORDER BY student_id LIMIT 1", (latest["id"],)).fetchone()
                    kinds = Counter(json.loads(row[0])["type"] for row in db.execute("SELECT frozen_json FROM task_items WHERE task_id=?", (first[0],)))
                    if set(kinds) - set(TYPES):
                        raise PlanningConflict("unsupported_previous_types", "前轮含不支持的题型，请提供明确组卷配置")
                    ratio = policy.get("common_question_ratio", 50)
                    level = min(COMMON_RATIOS, key=lambda key: abs(COMMON_RATIOS[key] - ratio))
                    selected = {"course_id": goal["course_id"], "class_id": goal["class_id"], "title": goal["title"],
                                "description": goal["description"], "student_ids": sorted(students), "question_count": sum(kinds.values()),
                                "question_types": {kind: kinds[kind] for kind in TYPES}, "common_level": level,
                                "allow_late": policy["allow_late"], "allow_retries": policy["allow_retries"],
                                "allow_retry_after_deadline": policy["allow_retry_after_deadline"]}
                selected = self._next_config(selected, draft)
                self._scope(actor_id, selected)
                now = self.clock()
                db.execute("""INSERT INTO teacher_next_round_drafts
                    (id,owner_id,course_id,class_id,insight_id,origin_goal_id,source_report_revision,goal_version,
                     next_round,revision,status,config_json,overrides_json,basis_json,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,1,'draft',?,'{}',?,?,?)""",
                    (draft["id"], actor_id, draft["course_id"], draft["class_id"], draft["insight_id"], draft["origin_goal_id"],
                     draft["source_report_revision"], draft["goal_version"], draft["next_round"], self._json(selected), self._json(basis), now, now))
                return self._next_access(db, actor_id, draft["id"], write=True)
            stored = self.learning._command(db, actor_id, "teaching_insight:next_round", request_id, payload, apply)
            return self._next_access(db, actor_id, stored["id"], write=True)

    def get_next_round_draft(self, actor_id, draft_id):
        with self.platform.transaction(), self.learning._using_connection() as db:
            return self._next_access(db, actor_id, draft_id)

    def save_next_round_draft(self, actor_id, draft_id, *, request_id, expected_revision, config, paper_overrides=None):
        from ..schemas.v1_teaching_insights import NextRoundSave
        command = NextRoundSave.model_validate({"request_id": request_id, "expected_revision": expected_revision,
                                               "config": self._config(config), "paper_overrides": paper_overrides or {}}).model_dump()
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            draft = self._next_access(db, actor_id, draft_id, write=True)
            def apply():
                self._next_revision(draft, expected_revision)
                self._next_guard(db, actor_id, draft, require_finished=False)
                selected = self._next_config(command["config"], draft)
                self._scope(actor_id, selected)
                if set(command["paper_overrides"]) - set(selected["student_ids"]):
                    raise ValueError("编辑卷包含未选择的学生")
                db.execute("""UPDATE teacher_next_round_drafts SET config_json=?,overrides_json=?,revision=revision+1,updated_at=?
                    WHERE id=? AND revision=? AND status='draft'""",
                    (self._json(selected), self._json(command["paper_overrides"]), self.clock(), draft_id, expected_revision))
                return self._next_access(db, actor_id, draft_id, write=True)
            return self.learning._command(db, actor_id, "next_round:save:" + draft_id, request_id, command, apply)

    def preview_next_round_draft(self, actor_id, draft_id, *, expected_revision):
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            draft = self._next_access(db, actor_id, draft_id, write=True)
            self._next_revision(draft, expected_revision)
            self._next_guard(db, actor_id, draft, require_finished=False)
            config = self._next_config(draft["config"], draft)
            result, snapshot = self._build(db, actor_id, config, frozen_suggestions=self._next_suggestions(draft),
                                          origin_goal_id=draft["origin_goal_id"], paper_overrides=draft["paper_overrides"])
            try:
                self._next_guard(db, actor_id, draft)
            except PlanningConflict as error:
                result["errors"].append({"code": error.code, "message": str(error)})
                result.update(ready=False, readiness="blocked")
            result.update(draft_id=draft_id, draft_revision=draft["revision"], origin_goal_id=draft["origin_goal_id"],
                          next_round=draft["next_round"], insight_id=draft["insight_id"],
                          allocation_mode="deterministic_teacher_confirmed", bounded_plan_supported=True)
            if result["ready"]:
                token, now = secrets.token_urlsafe(32), self.clock()
                binding = {"kind": "teaching_next_round", "draft_id": draft_id, "draft_revision": draft["revision"],
                           "origin_goal_id": draft["origin_goal_id"], "goal_version": draft["goal_version"],
                           "next_round": draft["next_round"], "insight_id": draft["insight_id"]}
                snapshot["next_round_binding"] = binding
                db.execute("INSERT INTO teacher_plan_previews VALUES(?,?,?,?,?,?,NULL)",
                    (self._hash(token), actor_id, self._hash({"config": config, "overrides": draft["paper_overrides"], **binding}),
                     self._json(snapshot), now, now + self.PREVIEW_SECONDS))
                result.update(preview_token=token, preview_expires_at=now + self.PREVIEW_SECONDS)
            return result

    def publish_next_round_draft(self, actor_id, draft_id, *, request_id, expected_revision, preview_token, confirm):
        from ..schemas.v1_teaching_insights import NextRoundPublish
        command = NextRoundPublish.model_validate({"request_id": request_id, "expected_revision": expected_revision,
                                                   "preview_token": preview_token, "confirm": confirm}).model_dump()
        if getattr(self.learning._local, "connection", None) is not None:
            raise RuntimeError("publish must own the learning transaction")
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            draft = self._next_access(db, actor_id, draft_id, write=True)
            self._scope(actor_id, draft["config"])
            def apply():
                if draft["status"] == "published":
                    raise PlanningConflict("draft_already_published", "草稿已发布；请读取原发布结果，不要使用新的请求编号重发")
                self._next_revision(draft, expected_revision)
                self._next_guard(db, actor_id, draft)
                row = db.execute("SELECT * FROM teacher_plan_previews WHERE token_hash=? AND owner_id=?",
                                 (self._hash(preview_token), actor_id)).fetchone()
                if row is None:
                    raise PlanningConflict("preview_required", "请先预览当前下一轮草稿")
                snapshot = json.loads(row["snapshot_json"])
                binding = {"kind": "teaching_next_round", "draft_id": draft_id, "draft_revision": draft["revision"],
                           "origin_goal_id": draft["origin_goal_id"], "goal_version": draft["goal_version"],
                           "next_round": draft["next_round"], "insight_id": draft["insight_id"]}
                config = self._next_config(draft["config"], draft)
                expected_hash = self._hash({"config": config, "overrides": draft["paper_overrides"], **binding})
                if snapshot.get("next_round_binding") != binding or row["config_fingerprint"] != expected_hash:
                    raise PlanningConflict("preview_stale", "预览不属于当前草稿修订")
                if row["expires_at"] <= self.clock() or self._time_errors(config):
                    raise PlanningConflict("preview_expired", "预览或计划时间已过期，请重新预览")
                knowledge = self._knowledge(actor_id, config)
                if set(config["knowledge_points"]) - set(knowledge):
                    raise PlanningConflict("preview_stale", "所选知识点已不可访问")
                pool, _ = self._catalog(db, actor_id, config, knowledge)
                pool = self._unused_goal_pool(db, draft["origin_goal_id"], pool)
                current = {q["key"]: q for q in pool}
                for ref in snapshot["questions"]:
                    question = current.get((ref["question_id"], ref["version"]))
                    if question is None or question["fingerprint"] != ref["fingerprint"]:
                        raise PlanningConflict("preview_stale", "题目、题族或来源已变化，请重新预览")
                structures = set()
                for refs in snapshot["papers"].values():
                    questions = [current[(ref["question_id"], ref["version"])] for ref in refs]
                    structures.add(tuple(sorted(q["group"] for q in questions)))
                    if (len({q["family"] for q in questions}) != len(questions)
                            or Counter(q["group"][0] for q in questions) != Counter({k: v for k, v in config["question_types"].items() if v})
                            or sum(q["group"][1] for q in questions) != snapshot["total_score_units"]):
                        raise PlanningConflict("preview_stale", "编辑卷结构已变化")
                if len(structures) != 1 or set(snapshot["papers"]) != set(config["student_ids"]):
                    raise PlanningConflict("preview_stale", "个人卷范围或等分值结构不一致")
                result = self.learning.publish_round(actor_id, draft["origin_goal_id"], expected_version=draft["goal_version"],
                    request_id="next-round:" + draft_id, round_no=draft["next_round"], student_ids=config["student_ids"],
                    question_versions=snapshot["papers"][config["student_ids"][0]], personal_question_versions=snapshot["papers"],
                    common_question_ratio=COMMON_RATIOS[config["common_level"]],
                    **{key: config[key] for key in ("deadline_at", "scheduled_at", "allow_late", "allow_retries", "allow_retry_after_deadline")})
                result.update(draft_id=draft_id, insight_id=draft["insight_id"], origin_goal_id=draft["origin_goal_id"],
                              common_count=snapshot["common_count"], actual_common_ratio=snapshot["actual_common_ratio"],
                              source_report_revision=draft["source_report_revision"], goal_version=draft["goal_version"] + 1)
                db.execute("UPDATE teacher_next_round_drafts SET status='published',published_result_json=?,revision=revision+1,updated_at=? WHERE id=?",
                           (self._json(result), self.clock(), draft_id))
                db.execute("UPDATE teacher_plan_previews SET published_result_json=? WHERE token_hash=?", (self._json(result), row["token_hash"]))
                return result
            return self.learning._command(db, actor_id, "next_round:publish:" + draft_id, request_id, command, apply)

    def configure_next_round_plan(self, actor_id, draft_id, **options):
        from ..schemas.v1_teaching_insights import BoundedPlanConfirm
        command = BoundedPlanConfirm.model_validate(options).model_dump()
        with self.platform.transaction(), self.learning._using_connection(write=True) as db:
            draft = self._next_access(db, actor_id, draft_id, write=True)
            self._scope(actor_id, draft["config"])
            def apply():
                if draft["status"] != "published":
                    raise PlanningConflict("next_round_not_published", "请先确认并发布下一轮，再配置持续计划")
                goal = self.learning._goal_row(db, draft["origin_goal_id"], actor_id)
                if goal["version"] != command["expected_goal_version"] or goal["status"] != "active":
                    raise PlanningConflict("goal_version_conflict", "原目标版本已改变或不再开放，请重新确认")
                latest = db.execute("SELECT round_no FROM learning_rounds WHERE goal_id=? ORDER BY round_no DESC LIMIT 1", (goal["id"],)).fetchone()
                if latest is None or latest[0] != draft["next_round"]:
                    raise PlanningConflict("next_round_conflict", "持续计划必须从最近一次教师确认发布的轮次开始")
                if command["ends_at"] <= self.clock() + command["interval_seconds"]:
                    raise ValueError("持续计划结束时间须晚于首次下一轮开放时间")
                config = {**draft["config"], "scheduled_at": None, "deadline_at": None}
                knowledge = self._knowledge(actor_id, config)
                pool, _ = self._catalog(db, actor_id, config, knowledge)
                pool = self._unused_goal_pool(db, goal["id"], pool)
                current = {q["key"]: q for q in pool}
                keys = [(ref["question_id"], ref["version"]) for ref in command["question_pool"]]
                if any(key not in current for key in keys):
                    raise PlanningConflict("invalid_confirmed_pool", "题池含已练题族、旧版本、未审核或来源不可用题目")
                selected = [{**current[key]["ref"], "fingerprint": current[key]["fingerprint"]} for key in keys]
                preview, _ = self._build(db, actor_id, config, frozen_suggestions=self._next_suggestions(draft),
                                         origin_goal_id=goal["id"], confirmed_pool=selected)
                if not preview["ready"]:
                    raise PlanningConflict("confirmed_pool_shortage", "教师确认题池不足以组成首个持续轮次，请补齐题池")
                self.learning.configure_plan(goal["id"], actor_id, expected_version=command["expected_goal_version"],
                    request_id="bounded:" + self._hash([draft_id, command["request_id"]]), future_round_count=command["future_round_count"],
                    interval_seconds=command["interval_seconds"], ends_at=command["ends_at"], question_pool=command["question_pool"])
                policy = {"schema_version": "teaching.confirmed.plan.v1", "interval_seconds": command["interval_seconds"],
                          "question_pool": command["question_pool"], "confirmed_pool": selected, "config": config,
                          "source_draft_id": draft_id, "actor_id": actor_id, "insight_id": draft["insight_id"],
                          "basis": draft["basis"], "expected_goal_version": command["expected_goal_version"] + 1}
                db.execute("UPDATE goal_plans SET policy_json=?,reason=NULL WHERE goal_id=?", (self._json(policy), goal["id"]))
                plan = self._bounded_view(db.execute("SELECT * FROM goal_plans WHERE goal_id=?", (goal["id"],)).fetchone())
                return {**plan, "goal_version": command["expected_goal_version"] + 1, "future_round_count": command["future_round_count"]}
            return self.learning._command(db, actor_id, "next_round:bounded:" + draft_id, command["request_id"], command, apply)

    def advance_confirmed_plan(self, db, plan, config):
        """Called inside platform->learning by tick_plans; never calls models."""
        actor_id = config["actor_id"]
        goal = self.learning._goal_row(db, plan["goal_id"], actor_id)
        self.learning._teacher_write(actor_id, goal)
        if goal["version"] != config["expected_goal_version"]:
            raise PlanningConflict("goal_version_conflict", "原目标版本已改变，请教师重新确认持续计划")
        selection = {**config["config"], "scheduled_at": None,
                     "deadline_at": min(plan["ends_at"], self.clock() + config["interval_seconds"])}
        self._scope(actor_id, selection)
        knowledge = self._knowledge(actor_id, selection)
        all_pool, _ = self._catalog(db, actor_id, selection, knowledge)
        # Confirmed versions/sources are immutable intent. A changed unused
        # member pauses instead of silently widening or replacing the pool.
        eligible = {q["key"]: q for q in all_pool}
        used_ids = {row[0] for row in db.execute("""SELECT DISTINCT ti.question_id FROM task_items ti
            JOIN learning_tasks t ON t.id=ti.task_id WHERE t.goal_id=?""", (goal["id"],))}
        for ref in config["confirmed_pool"]:
            if ref["question_id"] in used_ids:
                continue
            question = eligible.get((ref["question_id"], ref["version"]))
            if question is None or question["fingerprint"] != ref["fingerprint"]:
                raise PlanningConflict("confirmed_pool_changed", "教师确认题池的版本、审核或来源已变化")
        pseudo_draft = {"config": selection, "basis": config["basis"]}
        preview, snapshot = self._build(db, actor_id, selection, frozen_suggestions=self._next_suggestions(pseudo_draft),
                                        origin_goal_id=goal["id"], confirmed_pool=config["confirmed_pool"])
        if not preview["ready"]:
            raise PlanningConflict("confirmed_pool_shortage", "教师确认的新题族不足或筛选范围已变化，等待教师调整")
        latest = db.execute("SELECT MAX(round_no) FROM learning_rounds WHERE goal_id=?", (goal["id"],)).fetchone()[0]
        result = self.learning.publish_round(actor_id, goal["id"], expected_version=goal["version"],
            request_id=f"confirmed-plan:{plan['version']}:{latest + 1}", round_no=latest + 1,
            student_ids=selection["student_ids"], question_versions=snapshot["papers"][selection["student_ids"][0]],
            personal_question_versions=snapshot["papers"], common_question_ratio=COMMON_RATIOS[selection["common_level"]],
            **{key: selection[key] for key in ("deadline_at", "scheduled_at", "allow_late", "allow_retries", "allow_retry_after_deadline")})
        config["expected_goal_version"] = goal["version"] + 1
        return result
