"""Teacher-only explanations of a selected immutable report revision.

The model selects evidence-bound explanation codes, never generates authority for
scores, identities, mastery or question allocation. No gateway is called here.
"""

from __future__ import annotations

import json

from ..report_store import SNAPSHOT_SCHEMA_VERSION, canonical_json, content_hash
from ..schemas.v1_teaching_insights import InsightCreate, InsightModelOutput
from ..teaching_insight_store import InsightConflict, TeachingInsightStore
from .mastery import build_mastery, get_rule


EXPLANATIONS = {
    "mastered": ("maintain_and_check", "当前独立作答证据达到本规则阈值，可巩固并用新题继续核验。"),
    "developing": ("consolidate_goal", "当前目标证据处于发展阶段，可安排巩固练习并继续观察。"),
    "needs_practice": ("practice_goal", "当前独立证据支持对本目标加强练习，尚不能定位具体薄弱知识点。"),
    "insufficient_evidence": ("collect_independent_evidence", "独立题族证据不足，暂不判断薄弱或掌握，应先补充独立作答证据。"),
}
LIMITATIONS = {
    "goal_level_only": "冻结快照仅含目标级指标，没有题干、答案或知识点映射，无法定位具体薄弱知识点。",
    "uncalibrated_rule": "掌握度使用未经校准的版本化规则，仅描述已有证据，不代表能力测量结论。",
    "insufficient_evidence": "证据不足者保留待评估状态，不能将缺失比例当作零分或断言薄弱。",
}
SUMMARY = {
    "frozen_goal_evidence": "依据选定冻结报告解释本目标的独立作答证据。",
    "mixed_goal_evidence": "冻结证据显示不同学生处于不同目标证据阶段，下一轮需由教师复核分层安排。",
    "needs_more_evidence": "当前冻结证据尚不足以形成可靠目标判断，优先补充独立作答。",
}


def _scope(runtime, actor_id, class_id, course_id=None, *, write=False):
    actor, classroom = runtime.platform.get_user(actor_id), runtime.platform.get_class(class_id)
    if (not actor or actor["status"] != "active" or actor["role"] != "teacher" or not classroom
            or (course_id is not None and classroom["course_id"] != course_id)
            or not runtime.platform.can_access_class(actor_id, class_id, teaching=write)):
        raise PermissionError("教学总结不存在或当前班级权限不足")
    return classroom


def teaching_insight_access(runtime, actor_id, insight_id, *, write=False):
    """Current teacher class authorization for non-job notification resources."""
    store = getattr(runtime, "insights", None) or runtime._get("insights", lambda: TeachingInsightStore(runtime.reports))
    row = store.get(insight_id)
    if row is None:
        raise LookupError("教学总结不存在")
    _scope(runtime, actor_id, row["class_id"], row["course_id"], write=write)
    return row


def teaching_insight_job_access(runtime, actor_id, job, *, write=False):
    """Resource binding gate for /jobs and call details, before role shortcuts."""
    try:
        payload = job.payload
        store = getattr(runtime, "insights", None) or runtime._get("insights", lambda: TeachingInsightStore(runtime.reports))
        row = store.get(payload.get("resource_id"))
        if row is None or job.job_id != row["job_id"] or job.kind != "batch":
            return False
        expected = {"resource_type": "teaching_insight", "resource_id": row["id"], "purpose": "summary", "step": "summary",
                    "class_id": row["class_id"], "course_id": row["course_id"], "owner_id": row["owner_id"],
                    "report_id": row["report_id"], "source_report_revision": row["source_report_revision"],
                    "input_hash": row["input_hash"], "input_revision": row["source_revision"]}
        if (any(payload.get(key) != value for key, value in expected.items())
                or type(payload.get("input_revision")) is not int or payload.get("student_id") is not None):
            return False
        actor = runtime.platform.get_user(actor_id)
        if actor and actor["status"] == "active" and actor["role"] == "admin":
            # Operational metadata/reconciliation only. Insight body access
            # retains its independent teacher-and-current-class check.
            return True
        _scope(runtime, actor_id, row["class_id"], row["course_id"], write=write)
        return True
    except (LookupError, PermissionError, ValueError, AttributeError):
        return False


def _freeze(revision):
    snapshot = revision["snapshot"]
    scope = snapshot.get("scope", {})
    if (snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION or scope.get("kind") != "goal"
            or "student_id" in scope or not all(scope.get(k) for k in ("goal_id", "class_id", "course_id"))
            or content_hash(snapshot) != revision["snapshot_sha256"]
            or snapshot.get("source_revision") != revision["source_revision"]):
        raise ValueError("必须选择完整班级目标的有效冻结报告修订")
    tables = snapshot["tables"]
    goals, students = tables["goals"]["rows"], tables["students"]["rows"]
    if len(goals) != 1 or any(goals[0].get(k) != scope[k] for k in ("goal_id", "class_id", "course_id")):
        raise ValueError("冻结报告目标范围不一致")
    version = goals[0].get("version")
    if type(version) is not int or version < 1 or not 1 <= len(students) <= 200:
        raise ValueError("冻结报告须有有效目标版本及一至二百名学生")
    student_ids = sorted(row["student_id"] for row in students)
    if len(set(student_ids)) != len(student_ids):
        raise ValueError("冻结学生范围重复")
    rule = get_rule(snapshot["rule"]["version"])
    evidence = tables["evidence"]["rows"]
    if any(row.get("student_id") not in student_ids or row.get("goal_ids") != [scope["goal_id"]] for row in evidence):
        raise ValueError("冻结证据超出原目标或学生范围")
    # Verify the snapshot's derived values with the original versioned rule.
    computed = build_mastery(evidence, students=student_ids, goals=[scope["goal_id"]],
                             rule_version=rule.version, source_revision=revision["source_revision"])
    original = tables["mastery"]["rows"]
    if len(original) != len(computed):
        raise ValueError("冻结掌握度矩阵不完整")
    original = {(row["student_id"], row["goal_id"]): row for row in original}
    if len(original) != len(computed):
        raise ValueError("冻结掌握度矩阵重复")
    codes, facts, refs = {}, [], {}
    refs["report:scope"] = {"table": "goals", "goal_id": scope["goal_id"], "source_report_revision": revision["id"]}
    refs["report:rule"] = {"table": "rules", "rule_version": rule.version}
    refs["report:students"] = {"table": "students", "row_count": len(student_ids)}
    for index, row in enumerate(computed, 1):
        frozen = original.get((row["student_id"], row["goal_id"]), {})
        for field in ("percent", "status", "independent_families", "observed_families", "evidence_revision", "evidence_refs"):
            if canonical_json(frozen.get(field)) != canonical_json(row[field]):
                raise ValueError("冻结掌握度与版本化证据计算不符")
        code = f"S{index:03d}"
        codes[code] = row["student_id"]
        metric_ref = f"mastery:{code}"
        refs[metric_ref] = {"table": "mastery", "student_id": row["student_id"], "goal_id": scope["goal_id"],
                            "evidence_revision": row["evidence_revision"], "evidence_refs": row["evidence_refs"]}
        # Outbound aliases do not expose attempt, student, family or evidence IDs.
        aliases = [metric_ref]
        for number, source in enumerate(row["evidence_refs"], 1):
            alias = f"evidence:{code}:{number}"
            refs[alias] = {"table": "evidence", **source}
            aliases.append(alias)
        facts.append({"student_code": code, "status": row["status"], "percent": row["percent"],
                      "independent_families": row["independent_families"], "observed_families": row["observed_families"],
                      "evidence_refs": aliases})
    counts = {status: sum(row["status"] == status for row in facts) for status in EXPLANATIONS}
    outbound = {"schema_version": "teaching.insight.input.v1", "rule": rule.to_dict(),
                "facts": {"student_count": len(facts), "status_counts": counts, "students": facts},
                "evidence_refs": list(refs), "limitations": ["goal_level_only", "uncalibrated_rule"]}
    if counts["insufficient_evidence"]:
        outbound["limitations"].append("insufficient_evidence")
    return {"schema_version": "teaching.insight.frozen.v1", "goal_version": version,
            "student_codes": codes, "evidence_index": refs, "model_input": outbound}


class TeachingInsights:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = getattr(runtime, "insights", None) or runtime._get("insights", lambda: TeachingInsightStore(runtime.reports))

    def _authorized(self, actor_id, insight_id, *, write=False):
        row = self.store.get(insight_id)
        if row is None:
            raise LookupError("教学总结不存在")
        _scope(self.runtime, actor_id, row["class_id"], row["course_id"], write=write)
        return row

    def _view(self, row):
        # A conservative, explicit global source token: unrelated updates may
        # also mark stale, but no updated score can silently retain a fresh label.
        with self.runtime.learning._using_connection() as db:
            current_source = db.execute("SELECT revision FROM learning_revision WHERE id=1").fetchone()[0]
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teacher_next_round_drafts'").fetchone()
            drafts = [dict(item) for item in db.execute("""SELECT id,revision,status,next_round,origin_goal_id,goal_version,
                source_report_revision,created_at,updated_at FROM teacher_next_round_drafts
                WHERE insight_id=? AND class_id=? AND course_id=? ORDER BY updated_at DESC,id""",
                (row["id"], row["class_id"], row["course_id"]))] if exists else []
        report = self.runtime.reports.get_report(row["report_id"])
        result = {key: value for key, value in row.items() if key not in {"input", "request_hash"}}
        result.update(facts=row["input"]["model_input"]["facts"], evidence_index=row["input"]["evidence_index"],
                      student_codes=row["input"]["student_codes"],
                      stale=current_source != row["source_revision"] or report["current_revision_id"] != row["source_report_revision"],
                      current_source_revision=current_source, stale_basis="learning_source_revision",
                      allocation_mode="deterministic_teacher_confirmed", bounded_plan_supported=True,
                      next_round_drafts=drafts)
        return result

    def create(self, actor_id, *, report_id, source_report_revision, request_id):
        request = InsightCreate.model_validate({"report_id": report_id, "source_report_revision": source_report_revision, "request_id": request_id})
        with self.runtime.platform.transaction():
            revision = self.runtime.reports.get_revision(request.source_report_revision)
            if revision is None or revision["report_id"] != request.report_id:
                raise LookupError("冻结报告修订不存在")
            scope = revision["snapshot"].get("scope", {})
            _scope(self.runtime, actor_id, scope.get("class_id"), scope.get("course_id"), write=True)
            authoritative = self.runtime.learning.get_goal_scope(scope.get("goal_id"), actor_id)
            if any(authoritative.get(key) != scope.get(key) for key in ("goal_id", "class_id", "course_id")):
                raise PermissionError("冻结报告目标范围不一致")
            fingerprint = {"report_id": request.report_id, "source_report_revision": request.source_report_revision}
            previous = self.store.find_request(actor_id, request.request_id, fingerprint)
            if previous is not None:
                return self._view(previous)
            frozen = _freeze(revision)
            return self._view(self.store.create(owner_id=actor_id, request_id=request.request_id, revision=revision, frozen_input=frozen))

    def get(self, actor_id, insight_id):
        with self.runtime.platform.transaction():
            return self._view(self._authorized(actor_id, insight_id))

    def list(self, actor_id, *, class_id, goal_id=None, limit=50):
        with self.runtime.platform.transaction():
            _scope(self.runtime, actor_id, class_id)
            return {"items": [self._view(row) for row in self.store.list(class_id=class_id, goal_id=goal_id, limit=limit)]}

    def prepare(self, insight_id, *, generation):
        with self.runtime.platform.transaction():
            row = self.store.get(insight_id)
            if row is None:
                raise LookupError("教学总结不存在")
            _scope(self.runtime, row["owner_id"], row["class_id"], row["course_id"], write=True)
            if row["generation"] != generation or row["status"] != "running":
                raise InsightConflict("generation_conflict", "总结已取消、完成或运行代次已变化")
            model_input = row["input"]["model_input"]
            instruction = (
                "你是教师冻结证据解释器。只输出严格JSON，不输出markdown或自由文本。"
                "只能基于给出的facts选择解释代码；不能计算、补造或改变学生、比例、状态、独立题族数。"
                "每位学生必须恰好一条finding，原样复制student_code/status/percent/independent_families，"
                "引用该学生mastery证据。证据不足只允许collect_independent_evidence。"
                "状态与解释对应：mastered=maintain_and_check；developing=consolidate_goal；"
                "needs_practice=practice_goal；insufficient_evidence=collect_independent_evidence。"
                "summary_code：全部证据不足为needs_more_evidence；状态多种为mixed_goal_evidence；否则frozen_goal_evidence。"
                "evidence_refs必须包含report:scope/report:rule/report:students。limitations须原样复制输入。"
                "实际配卷由规则与教师决定，模型只选择这些解释。输出schema="
                + canonical_json(InsightModelOutput.model_json_schema())
            )
            return {"messages": [{"role": "system", "content": instruction},
                                 {"role": "user", "content": canonical_json(model_input)}],
                    "purpose": "summary", "step": "summary", "input_hash": row["input_hash"]}

    @staticmethod
    def _validated_result(row, output):
        if isinstance(output, str):
            if len(output) > 250000:
                raise ValueError("model result too large")
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON field")
                    result[key] = value
                return result
            output = json.loads(output, object_pairs_hook=unique_pairs)
        selected = InsightModelOutput.model_validate(output).model_dump()
        model_input = row["input"]["model_input"]
        facts = model_input["facts"]["students"]
        statuses = {fact["status"] for fact in facts}
        expected_summary = "needs_more_evidence" if statuses == {"insufficient_evidence"} else "mixed_goal_evidence" if len(statuses) > 1 else "frozen_goal_evidence"
        if selected["summary_code"] != expected_summary or selected["limitations"] != model_input["limitations"]:
            raise ValueError("model summary changes frozen evidence state")
        global_refs = selected["evidence_refs"]
        if len(global_refs) != len(set(global_refs)) or set(global_refs) != {"report:scope", "report:rule", "report:students"}:
            raise ValueError("invalid summary evidence references")
        by_code = {finding["student_code"]: finding for finding in selected["findings"]}
        if len(by_code) != len(selected["findings"]) or set(by_code) != {fact["student_code"] for fact in facts}:
            raise ValueError("model student population differs from frozen report")
        findings = []
        for fact in facts:
            finding = by_code[fact["student_code"]]
            for field in ("status", "percent", "independent_families"):
                if canonical_json(finding[field]) != canonical_json(fact[field]):
                    raise ValueError("model changes a computed fact")
            refs = finding["evidence_refs"]
            if (len(refs) != len(set(refs)) or f"mastery:{fact['student_code']}" not in refs
                    or set(refs) - set(fact["evidence_refs"])):
                raise ValueError("model cites unknown or another student's evidence")
            explanation, text = EXPLANATIONS[fact["status"]]
            if finding["explanation_code"] != explanation:
                raise ValueError("model claims an unsupported mastery judgment")
            findings.append({**finding, "explanation": text})
        return {"schema_version": "teaching.insight.result.v1", "origin": "ai", "interpretation_mode": "validated_selection",
                "summary": SUMMARY[expected_summary], "summary_code": expected_summary, "evidence_refs": global_refs,
                "findings": findings, "limitations": [LIMITATIONS[code] for code in selected["limitations"]],
                "allocation_mode": "deterministic_teacher_confirmed", "input_hash": row["input_hash"]}

    def apply(self, insight_id, output, *, generation):
        with self.runtime.platform.transaction():
            row = self.store.get(insight_id)
            if row is None:
                raise LookupError("教学总结不存在")
            _scope(self.runtime, row["owner_id"], row["class_id"], row["course_id"], write=True)
            if row["generation"] != generation or row["status"] not in {"running", "completed"}:
                raise InsightConflict("generation_conflict", "总结已取消或运行代次已变化")
            if row["status"] == "completed":
                return self._view(row)
            try:
                result = self._validated_result(row, output)
            except (ValueError, TypeError, KeyError):
                if not self.store.fail(insight_id, generation=generation, code="invalid_model_output",
                                       message="模型输出未通过冻结证据校验；未生成有效AI总结，可核对规则事实后重试"):
                    raise InsightConflict("generation_conflict", "运行代次已变化") from None
            else:
                if not self.store.complete(insight_id, result, generation=generation):
                    raise InsightConflict("generation_conflict", "运行代次已变化")
            return self._view(self.store.get(insight_id))

    def fail(self, insight_id, *, generation, code="generation_failed", message="总结生成失败，请重试"):
        return self.store.fail(insight_id, generation=generation, code=code, message=message)

    def _planning(self):
        from .teacher_planning import TeacherPlanning
        return self.runtime._get("teacher_planning", lambda: TeacherPlanning(
            self.runtime.learning, self.runtime.platform, self.runtime.content))

    def create_next_round(self, actor_id, insight_id, *, request_id, config=None):
        row = self._authorized(actor_id, insight_id, write=True)
        if row["status"] != "completed":
            raise InsightConflict("insight_not_completed", "请等待有效总结完成后创建下一轮草稿")
        # Planning owns platform->learning and repeats current class access.
        return self._planning().create_next_round_draft(actor_id, row, request_id=request_id, config=config)

    def get_next_round(self, actor_id, draft_id):
        return self._planning().get_next_round_draft(actor_id, draft_id)

    def save_next_round(self, actor_id, draft_id, **options):
        return self._planning().save_next_round_draft(actor_id, draft_id, **options)

    def preview_next_round(self, actor_id, draft_id, *, expected_revision):
        return self._planning().preview_next_round_draft(actor_id, draft_id, expected_revision=expected_revision)

    def publish_next_round(self, actor_id, draft_id, **options):
        return self._planning().publish_next_round_draft(actor_id, draft_id, **options)

    def configure_bounded_plan(self, actor_id, draft_id, **options):
        return self._planning().configure_next_round_plan(actor_id, draft_id, **options)
