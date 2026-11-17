"""Strict HTTP commands and evidence-bound model selections for teacher insights."""

from typing import Literal

from pydantic import Field, model_validator

from .v1_teacher_planning import Identifier, PlanningInput, TeachingPlanConfig


class InsightCreate(PlanningInput):
    report_id: Identifier
    source_report_revision: Identifier
    request_id: Identifier = Field(max_length=100)


class InsightFinding(PlanningInput):
    student_code: str = Field(pattern=r"^S[0-9]{3,4}$", max_length=5)
    status: Literal["mastered", "developing", "needs_practice", "insufficient_evidence"]
    percent: float | None = Field(ge=0, le=100, strict=True, allow_inf_nan=False)
    independent_families: int = Field(ge=0, strict=True)
    evidence_refs: list[str] = Field(min_length=1, max_length=1000)
    explanation_code: Literal["maintain_and_check", "consolidate_goal", "practice_goal", "collect_independent_evidence"]


class InsightModelOutput(PlanningInput):
    # Deliberately no unrestricted prose. Model selections are rendered from
    # verified facts; neither numerals nor invented students can hide in text.
    schema_version: Literal["teaching.insight.selection.v1"]
    summary_code: Literal["frozen_goal_evidence", "mixed_goal_evidence", "needs_more_evidence"]
    evidence_refs: list[str] = Field(min_length=1, max_length=1000)
    findings: list[InsightFinding] = Field(max_length=200)
    limitations: list[Literal["goal_level_only", "uncalibrated_rule", "insufficient_evidence"]] = Field(min_length=2, max_length=3)


class NextRoundCreate(PlanningInput):
    request_id: Identifier = Field(max_length=100)
    config: TeachingPlanConfig | None = None


class QuestionRef(PlanningInput):
    question_id: Identifier
    version: int = Field(ge=1, strict=True)


class NextRoundSave(PlanningInput):
    request_id: Identifier = Field(max_length=100)
    expected_revision: int = Field(ge=1, strict=True)
    config: TeachingPlanConfig
    paper_overrides: dict[Identifier, list[QuestionRef]] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def bounded_papers(self):
        if any(not 1 <= len(paper) <= 100 for paper in self.paper_overrides.values()):
            raise ValueError("每份编辑卷须包含一至一百道题")
        return self


class BoundedPlanConfirm(PlanningInput):
    request_id: Identifier = Field(max_length=100)
    expected_goal_version: int = Field(ge=1, strict=True)
    confirm: bool = Field(strict=True)
    question_pool: list[QuestionRef] = Field(min_length=1, max_length=2000)
    future_round_count: int = Field(ge=1, le=100, strict=True)
    interval_seconds: int = Field(ge=60, le=31622400, strict=True)
    ends_at: float = Field(gt=0, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def explicit_pool_confirmation(self):
        if self.confirm is not True:
            raise ValueError("持续计划需要教师明确确认题池、轮数与期限")
        keys = [(ref.question_id, ref.version) for ref in self.question_pool]
        if len(keys) != len(set(keys)):
            raise ValueError("持续计划题池不能重复")
        return self


class NextRoundPreview(PlanningInput):
    expected_revision: int = Field(ge=1, strict=True)


class NextRoundPublish(NextRoundPreview):
    request_id: Identifier = Field(max_length=100)
    preview_token: str = Field(min_length=1, max_length=160)
    confirm: bool = Field(strict=True)

    @model_validator(mode="after")
    def teacher_confirmation(self):
        if self.confirm is not True:
            raise ValueError("发布下一轮需要教师明确确认")
        return self
