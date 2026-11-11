"""Authenticated goals, frozen papers, drafts, grading and corrections."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..learning_store import (GoalNotFound, IdempotencyConflict, InvalidTransition,
                              LeaseLost, TaskNotFound, VersionConflict)
from ..runtime import get_runtime
from ..schemas.v1_learning import (AppealCreate, DeadlineExtension, DiagnosticSubmission, DraftSave,
                                   EvidenceEvent, GoalCreate, GoalPlan, GoalUpdate, LeaseAcquire,
                                   LeaseRenew, ManualGrade, PlanConfigure, QuestionStatus, TeacherJudgment, RoundPublish, Submit, Versioned)
from ..v1_deps import get_platform_user, require_platform_roles

router = APIRouter(prefix="/api/v1", tags=["learning"])


def _invoke(action):
    try:
        return action()
    except (GoalNotFound, TaskNotFound, LookupError, PermissionError):
        raise HTTPException(status_code=404, detail={"code": "resource_not_found", "message": "资源不存在或已不可访问"}) from None
    except (IdempotencyConflict, InvalidTransition, LeaseLost, VersionConflict) as error:
        raise HTTPException(status_code=409, detail={"code": type(error).__name__, "message": str(error), "retryable": True}) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_learning_input", "message": str(error)}) from error


def _class(runtime, actor, class_id: str, course_id: str, *, write=False):
    row = runtime.platform.get_class(class_id)
    if (row is None or row["course_id"] != course_id
            or not runtime.platform.can_access_class(actor.id, class_id, teaching=write)):
        raise HTTPException(status_code=404, detail={"code": "class_not_found", "message": "班级不存在或不可访问"})
    return row


@router.get("/courses/{course_id}/questions")
def list_course_questions(course_id: str, request: Request,
                          actor=Depends(require_platform_roles("teacher")),
                          limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = _invoke(lambda: get_runtime(request).learning.list_questions(course_id, actor.id, limit=limit, offset=offset))
    return {"items": [{key: row.get(key) for key in ("question_id", "version", "status", "course_id", "content")} for row in rows]}


@router.post("/questions/{question_id}/versions/{version}/status")
def set_question_status(question_id: str, version: int, payload: QuestionStatus, request: Request,
                        actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.set_question_version_status(actor.id, question_id, version, payload.status))


@router.post("/tasks/{task_id}/teacher-judgments", status_code=201)
def teacher_judgment(task_id: str, payload: TeacherJudgment, request: Request,
                     actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.save_teacher_judgment(actor.id, task_id, **payload.model_dump()))


@router.get("/goals")
def list_goals(request: Request, class_id: str | None = None, limit: int = Query(50, ge=1, le=100),
               actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    return {"items": _invoke(lambda: runtime.learning.list_goals(actor.id, class_id=class_id, limit=limit))}


@router.post("/goals", status_code=201)
def create_goal(payload: GoalCreate, request: Request, actor=Depends(require_platform_roles("teacher"))):
    runtime = get_runtime(request)
    _class(runtime, actor, payload.class_id, payload.course_id, write=True)
    return _invoke(lambda: runtime.learning.create_goal(
        actor.id, payload.title, payload.description, course_id=payload.course_id, class_id=payload.class_id,
        request_id=payload.request_id,
    ))


@router.get("/goals/{goal_id}")
def get_goal(goal_id: str, request: Request, actor=Depends(get_platform_user)):
    return _invoke(lambda: get_runtime(request).learning.get_goal(goal_id, actor.id))


@router.patch("/goals/{goal_id}")
def update_goal(goal_id: str, payload: GoalUpdate, request: Request,
                actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.update_goal(
        goal_id, actor.id, expected_version=payload.expected_version,
        title=payload.title, description=payload.description, request_id=payload.request_id,
    ))


@router.post("/goals/{goal_id}/rounds", status_code=201)
def publish_round(goal_id: str, payload: RoundPublish, request: Request,
                  actor=Depends(require_platform_roles("teacher"))):
    runtime = get_runtime(request)
    scope = _invoke(lambda: runtime.learning.get_goal_scope(goal_id, actor.id))
    _class(runtime, actor, scope["class_id"], scope["course_id"], write=True)
    for student_id in payload.student_ids:
        student = runtime.platform.get_user(student_id)
        if (student is None or student["status"] != "active" or student["role"] != "student"
                or runtime.platform.get_class_membership(scope["class_id"], student_id) is None):
            raise HTTPException(status_code=422, detail={"code": "student_not_in_class", "message": "发布范围包含不属于该班的有效学生"})
    result = _invoke(lambda: runtime.learning.publish_round(
        actor.id, goal_id, expected_version=payload.expected_version, request_id=payload.request_id,
        round_no=payload.round_no, student_ids=payload.student_ids,
        question_versions=[item.model_dump() for item in payload.question_versions],
        deadline_at=payload.deadline_at, scheduled_at=payload.scheduled_at,
        allow_late=payload.allow_late, allow_retries=payload.allow_retries,
        allow_retry_after_deadline=payload.allow_retry_after_deadline,
        common_question_ratio=payload.common_question_ratio,
        diagnostic_question_versions=[item.model_dump() for item in payload.diagnostic_question_versions],
        personal_question_versions={key: [item.model_dump() for item in refs] for key, refs in payload.personal_question_versions.items()} if payload.personal_question_versions else None,
    ))
    return result


@router.put("/goals/{goal_id}/schedule")
def configure_plan(goal_id: str, payload: PlanConfigure, request: Request,
                   actor=Depends(require_platform_roles("teacher"))):
    values = payload.model_dump()
    return _invoke(lambda: get_runtime(request).learning.configure_plan(goal_id, actor.id, **values))


@router.post("/goals/{goal_id}/plan")
def change_goal_plan(goal_id: str, payload: GoalPlan, request: Request,
                     actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.change_goal_plan(
        goal_id, actor.id, **payload.model_dump(),
    ))


@router.get("/tasks")
def list_tasks(request: Request, class_id: str | None = None, goal_id: str | None = None,
               limit: int = Query(50, ge=1, le=100), actor=Depends(get_platform_user)):
    return {"items": _invoke(lambda: get_runtime(request).learning.list_tasks(
        actor.id, class_id=class_id, goal_id=goal_id, limit=limit,
    ))}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request, actor=Depends(get_platform_user)):
    return _invoke(lambda: get_runtime(request).learning.get_task(task_id, actor.id))


@router.post("/tasks/{task_id}/edit-leases")
def acquire_lease(task_id: str, payload: LeaseAcquire, request: Request,
                  actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.acquire_draft_lease(
        task_id, actor.id, payload.device_id, lease_seconds=payload.lease_seconds, takeover=payload.takeover,
    ))


@router.post("/tasks/{task_id}/edit-leases/renew")
def renew_lease(task_id: str, payload: LeaseRenew, request: Request,
                actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.renew_draft_lease(
        task_id, actor.id, payload.lease_token, payload.fence_token, lease_seconds=payload.lease_seconds,
    ))


@router.put("/tasks/{task_id}/draft")
def save_draft(task_id: str, payload: DraftSave, request: Request,
               actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.save_draft(
        task_id, actor.id, payload.answers, expected_version=payload.expected_version,
        request_id=payload.request_id, lease_token=payload.lease_token, fence_token=payload.fence_token,
    ))


@router.post("/tasks/{task_id}/submit", status_code=202)
def submit_task(task_id: str, payload: Submit, request: Request,
                actor=Depends(require_platform_roles("student"))):
    result = _invoke(lambda: get_runtime(request).learning.submit_attempt(
        task_id, actor.id, **payload.model_dump(),
    ))
    return result


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, payload: Versioned, request: Request,
               actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.start_retry(task_id, actor.id, **payload.model_dump()))


@router.post("/tasks/{task_id}/help-events", status_code=201)
def help_event(task_id: str, payload: EvidenceEvent, request: Request,
               actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.record_help(
        task_id, actor.id, question_id=payload.question_id, payload=payload.payload,
        request_id=payload.request_id,
    ))


@router.post("/tasks/{task_id}/reading-events", status_code=201)
def reading_event(task_id: str, payload: EvidenceEvent, request: Request,
                  actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.record_reading(
        task_id, actor.id, question_id=payload.question_id, payload=payload.payload,
        request_id=payload.request_id,
    ))


@router.post("/tasks/{task_id}/deadline-extensions")
def extend_deadline(task_id: str, payload: DeadlineExtension, request: Request,
                    actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.extend_deadline(
        actor.id, task_id, payload.new_deadline, payload.reason,
        expected_version=payload.expected_version, request_id=payload.request_id,
    ))


@router.post("/attempts/{attempt_id}/items/{question_id}/manual-grade")
def manual_grade(attempt_id: str, question_id: str, payload: ManualGrade, request: Request,
                 actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.apply_manual_grade(
        actor.id, attempt_id, question_id, score_units=payload.score_units, reason=payload.reason,
        expected_revision=payload.expected_revision, request_id=payload.request_id,
    ))


@router.post("/attempts/{attempt_id}/items/{question_id}/corrections")
def correct_grade(attempt_id: str, question_id: str, payload: ManualGrade, request: Request,
                  actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: get_runtime(request).learning.apply_correction(
        actor.id, attempt_id, question_id, score_units=payload.score_units, reason=payload.reason,
        expected_revision=payload.expected_revision, request_id=payload.request_id,
    ))


@router.get("/attempts/{attempt_id}/corrections")
def grade_history(attempt_id: str, request: Request, actor=Depends(get_platform_user)):
    return {"items": _invoke(lambda: get_runtime(request).learning.list_corrections(attempt_id, actor.id))}


@router.get("/goals/{goal_id}/evidence")
def evidence(goal_id: str, request: Request, student_id: str | None = None, actor=Depends(get_platform_user)):
    if actor.role == "student":
        if student_id not in (None, actor.id):
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能查看本人的学习证据"})
        student_id = actor.id
    return {"items": _invoke(lambda: get_runtime(request).learning.list_evidence(goal_id, actor.id, student_id=student_id))}


@router.post("/tasks/{task_id}/diagnostics", status_code=201)
def start_diagnostic(task_id: str, payload: Versioned, request: Request,
                     actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.start_diagnostic(task_id, actor.id, request_id=payload.request_id))


@router.get("/diagnostics/{diagnostic_id}")
def get_diagnostic(diagnostic_id: str, request: Request, actor=Depends(get_platform_user)):
    return _invoke(lambda: get_runtime(request).learning.get_diagnostic(diagnostic_id, actor.id))


@router.post("/diagnostics/{diagnostic_id}/submit")
def submit_diagnostic(diagnostic_id: str, payload: DiagnosticSubmission, request: Request,
                      actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.submit_diagnostic(
        diagnostic_id, actor.id, payload.answers, payload.unknown_question_ids, request_id=payload.request_id,
    ))


@router.post("/attempts/{attempt_id}/appeals", status_code=202)
def create_appeal(attempt_id: str, payload: AppealCreate, request: Request,
                  actor=Depends(require_platform_roles("student"))):
    return _invoke(lambda: get_runtime(request).learning.create_appeal(actor.id, attempt_id, **payload.model_dump()))


@router.get("/tasks/{task_id}/appeals")
def list_appeals(task_id: str, request: Request, actor=Depends(get_platform_user)):
    return {"items": _invoke(lambda: get_runtime(request).learning.list_appeals(actor.id, task_id=task_id))}


@router.get("/tasks/{task_id}/answer-policy")
def answer_policy(task_id: str, request: Request, actor=Depends(require_platform_roles("student"))):
    _invoke(lambda: get_runtime(request).learning.get_task_scope(task_id, actor.id))
    policy = _invoke(lambda: get_runtime(request).learning.get_answer_policy(actor.id, task_id=task_id))
    fields = ("diagnostic", "attempted", "hints_allowed", "results_released", "allow_assistant_content")
    return {key: bool(policy.get(key, False)) for key in fields}
