"""Current resource authorization shared by jobs, events and call details."""

from fastapi import HTTPException


def can_access_job(runtime, actor, job, *, write: bool = False) -> bool:
    scope = job.payload
    purpose = scope.get("purpose")
    if actor.role == "admin":
        return True
    if purpose in {"student", "hint", "summary"} and scope.get("resource_type") == "response":
        try:
            response = runtime.companion.get_response(scope["resource_id"], actor.id)
        except (LookupError, PermissionError):
            return False
        return response is not None
    if actor.role == "student":
        return not write and scope.get("student_id") == actor.id
    if actor.role != "teacher":
        return False
    if scope.get("class_id"):
        return runtime.platform.can_access_class(actor.id, scope["class_id"], teaching=write)
    if scope.get("resource_type") in {"task", "attempt", "goal", "diagnostic", "appeal"}:
        return False
    if scope.get("resource_type") == "content_job" and scope.get("course_id"):
        return runtime.platform.can_access_course(actor.id, scope["course_id"], teaching=write)
    return scope.get("owner_id") == actor.id


def require_job(runtime, actor, job_id: str, *, write: bool = False):
    try:
        job = runtime.queue.get_job(job_id)
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "任务不存在或不可见"}) from None
    if not can_access_job(runtime, actor, job, write=write):
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "任务不存在或不可见"})
    return job


def job_view(job):
    return {
        "job_id": job.job_id, "kind": job.kind, "status": job.status,
        "resource_type": job.payload.get("resource_type"), "resource_id": job.payload.get("resource_id"),
        "attempts": job.attempts, "max_attempts": job.max_attempts,
        "cancel_requested": job.cancel_requested,
        "cost_pending": getattr(job, "cost_pending", False),
        "reconciliation_required": job.reconciliation_required,
        "created_at": job.created_at,
    }
