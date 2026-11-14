"""Strict inputs shared by teacher planning HTTP and service entry points."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]
Count = Annotated[int, Field(ge=0, le=100, strict=True)]


class PlanningInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QuestionTypes(PlanningInput):
    single_choice: Count = 1
    fill: Count = 1
    judge: Count = 1
    text: Count = 0


class TeachingPlanConfig(PlanningInput):
    course_id: Identifier
    class_id: Identifier
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10000)
    student_ids: list[Identifier] = Field(min_length=1, max_length=200)
    question_count: int = Field(default=3, ge=1, le=100, strict=True)
    question_types: QuestionTypes = Field(default_factory=QuestionTypes)
    common_level: Literal["light", "balanced", "heavy"] = "balanced"
    difficulty: Literal["any", "easy", "medium", "hard"] = "any"
    knowledge_points: list[Identifier] = Field(default_factory=list, max_length=100)
    deadline_at: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    scheduled_at: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    allow_late: bool = Field(default=False, strict=True)
    allow_retries: bool = Field(default=True, strict=True)
    allow_retry_after_deadline: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def consistent_selection(self):
        if len(set(self.student_ids)) != len(self.student_ids):
            raise ValueError("学生不能重复")
        if len(set(self.knowledge_points)) != len(self.knowledge_points):
            raise ValueError("知识点不能重复")
        if sum(self.question_types.model_dump().values()) != self.question_count:
            raise ValueError("各题型数量之和必须等于每人题量")
        if self.scheduled_at is not None and self.deadline_at is not None and self.scheduled_at >= self.deadline_at:
            raise ValueError("开放时间必须早于截止时间")
        if self.allow_retry_after_deadline and not self.allow_retries:
            raise ValueError("允许截止后重做须同时允许重做")
        return self


class PlanDraftSave(PlanningInput):
    config: TeachingPlanConfig
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class PlanPublish(PlanDraftSave):
    preview_token: str = Field(min_length=1, max_length=160)


class PlanPrepare(PlanDraftSave):
    source_block_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    external_processing_allowed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_block_ids)) != len(self.source_block_ids):
            raise ValueError("来源不能重复")
        return self
