from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class DemoContext:
    role: Literal["teacher", "student"]
    actor_id: str
    class_id: str
    student_id: str | None = None


@dataclass(slots=True)
class LearningReview:
    id: str
    class_id: str
    title: str
    created_by: str
    created_at: str
    topic: str
    student_ids: list[str]
    knowledge_point_ids: list[str]
    data_version: str
    snapshot: dict
    version: int = 1


@dataclass(slots=True)
class Followup:
    id: str
    class_id: str
    review_id: str
    knowledge_point_id: str
    title: str
    goal: str
    instructions: str
    completion_condition: str
    student_ids: list[str]
    material_ids: list[str]
    question_ids: list[str]
    created_by: str
    created_at: str
    status: str = "draft"
    version: int = 1
    published_at: str | None = None
    closed_at: str | None = None
    frozen_content: dict = field(default_factory=dict)


@dataclass(slots=True)
class StudentTask:
    id: str
    class_id: str
    followup_id: str
    student_id: str
    status: str = "not_started"
    version: int = 1
    draft_answers: dict = field(default_factory=dict)
    read_material_ids: list[str] = field(default_factory=list)
    attempt_ids: list[str] = field(default_factory=list)
    feedback_ids: list[str] = field(default_factory=list)
    updated_at: str | None = None


@dataclass(slots=True)
class SubmissionAttempt:
    id: str
    task_id: str
    student_id: str
    number: int
    submitted_at: str
    answers: dict
    read_material_ids: list[str]
    grading: dict


@dataclass(slots=True)
class TeacherFeedback:
    id: str
    task_id: str
    attempt_id: str
    teacher_id: str
    decision: Literal["completed", "needs_retry"]
    comment: str
    created_at: str
