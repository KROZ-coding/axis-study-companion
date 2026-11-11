"""Typed HTTP inputs for the authenticated learning API."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Mutation(Input):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class Versioned(Mutation):
    expected_version: int = Field(ge=1, strict=True)


class GoalCreate(Mutation):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10000)
    course_id: str = Field(min_length=1, max_length=100)
    class_id: str = Field(min_length=1, max_length=100)


class GoalUpdate(Versioned):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=10000)


class QuestionRef(Input):
    question_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1, strict=True)


class RoundPublish(Versioned):
    round_no: int = Field(ge=1, le=100, strict=True)
    student_ids: list[str] = Field(min_length=1, max_length=200)
    question_versions: list[QuestionRef] = Field(min_length=1, max_length=100)
    deadline_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    scheduled_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    allow_late: bool = Field(strict=True)
    allow_retries: bool = Field(strict=True)
    allow_retry_after_deadline: bool = Field(default=False, strict=True)
    common_question_ratio: int = Field(default=100, ge=0, le=100, strict=True)
    diagnostic_question_versions: list[QuestionRef] = Field(default_factory=list, max_length=20)
    personal_question_versions: dict[str, list[QuestionRef]] | None = None

    @model_validator(mode="after")
    def unique_members_and_questions(self):
        if len(set(self.student_ids)) != len(self.student_ids):
            raise ValueError("学生不能重复")
        ids = [item.question_id for item in self.question_versions]
        if len(set(ids)) != len(ids):
            raise ValueError("题目不能重复")
        if self.scheduled_at is not None and self.deadline_at is not None and self.scheduled_at >= self.deadline_at:
            raise ValueError("开放时间必须早于截止时间")
        return self


class LeaseAcquire(Input):
    device_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    takeover: bool = Field(default=False, strict=True)
    lease_seconds: int = Field(default=60, ge=15, le=300, strict=True)


class LeaseRenew(Input):
    lease_token: str = Field(min_length=1, max_length=160)
    fence_token: int = Field(ge=1, strict=True)
    lease_seconds: int = Field(default=60, ge=15, le=300, strict=True)


class DraftSave(Versioned):
    answers: dict[str, Any] = Field(max_length=100)
    lease_token: str = Field(min_length=1, max_length=160)
    fence_token: int = Field(ge=1, strict=True)


class Submit(Versioned):
    lease_token: str = Field(min_length=1, max_length=160)
    fence_token: int = Field(ge=1, strict=True)


class EvidenceEvent(Mutation):
    question_id: str | None = Field(default=None, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict, max_length=10)


class DeadlineExtension(Versioned):
    new_deadline: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)


class ManualGrade(Mutation):
    expected_revision: int = Field(ge=1, strict=True)
    score_units: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class DiagnosticSubmission(Mutation):
    answers: dict[str, Any] = Field(max_length=20)
    unknown_question_ids: list[str] = Field(default_factory=list, max_length=20)


class AppealCreate(Mutation):
    question_id: str = Field(min_length=1, max_length=160)
    grading_revision: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class TeacherJudgment(Mutation):
    judgment: Literal["mastered", "developing", "needs_practice", "insufficient_evidence", "needs_review"]
    knowledge_point_id: str = Field(min_length=1, max_length=160)


class QuestionStatus(Mutation):
    status: Literal["quarantined", "retired"]


class PlanConfigure(Versioned):
    future_round_count: int = Field(ge=1, le=100, strict=True)
    interval_seconds: int = Field(ge=60, le=31622400, strict=True)
    ends_at: float = Field(gt=0, allow_inf_nan=False)
    question_pool: list[QuestionRef] = Field(min_length=1, max_length=500)


class GoalPlan(Versioned):
    action: Literal["close", "renew"]
    reason: str = Field(min_length=1, max_length=2000)
    future_round_count: int | None = Field(default=None, ge=1, le=100, strict=True)
    new_end_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
