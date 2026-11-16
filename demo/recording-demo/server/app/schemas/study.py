from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MutationRequest(StudyRequest):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class VersionedRequest(MutationRequest):
    expected_version: int = Field(ge=1, strict=True)


class ReviewCreate(MutationRequest):
    title: str = Field(min_length=1, max_length=160)
    topic: str = Field(default="all", min_length=1, max_length=80)
    student_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    knowledge_point_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def check_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class FollowupFields(StudyRequest):
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=1000)
    knowledge_point_id: str = Field(min_length=1, max_length=80)
    student_ids: list[str] = Field(min_length=1, max_length=100)
    material_ids: list[str] = Field(min_length=1, max_length=30)
    question_ids: list[str] = Field(min_length=1, max_length=30)
    instructions: str = Field(default="阅读指定材料，完成验证题，再提交本次作答。", min_length=1, max_length=2000)
    completion_condition: str = Field(default="确认已阅读指定材料并提交验证题，等待教师反馈。", min_length=1, max_length=1000)


class FollowupCreate(FollowupFields, MutationRequest):
    review_id: str = Field(min_length=1, max_length=100)


class FollowupUpdate(FollowupFields, VersionedRequest):
    pass


class TaskDraft(VersionedRequest):
    answers: dict[str, int] = Field(default_factory=dict, max_length=30)
    read_material_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="before")
    @classmethod
    def strict_answers(cls, value):
        if isinstance(value, dict) and isinstance(value.get("answers"), dict):
            if any(type(answer) is not int for answer in value["answers"].values()):
                raise ValueError("答案必须是整数选项编号")
        return value


class FeedbackCreate(VersionedRequest):
    attempt_id: str = Field(min_length=1, max_length=100)
    decision: Literal["completed", "needs_retry"]
    comment: str = Field(min_length=1, max_length=2000)


class TeachingContextResponse(BaseModel):
    mode: Literal["local-demo"] = "local-demo"
    authenticated: bool = False
    actor: dict
    classroom: dict
    scope: dict
    data_version: str
    sample_label: str


class TaskResponse(BaseModel):
    id: str
    class_id: str
    followup_id: str
    student_id: str
    status: str
    version: int
    draft_answers: dict[str, int]
    read_material_ids: list[str]
    attempt_ids: list[str]
    feedback_ids: list[str]
    updated_at: str | None
    followup_status: str
    title: str
    goal: str
    instructions: str
    completion_condition: str
    content: dict
    attempts: list[dict]
    feedback: list[dict]
