from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..job_queue import InvalidTransition, IdempotencyConflict, QueueError
from ..runtime import get_runtime
from ..services.access import can_access_job, job_view, require_job
from ..services.companion_access import (
    answer_policy_provider, response_access, response_event_access, response_job_state, validate_response_access,
)
from ..v1_deps import get_platform_user, require_platform_roles

router = APIRouter(prefix="/api/v1", tags=["background-jobs"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResumeRequest(Input):
    confirm_paid: bool = Field(default=False, strict=True)


class BudgetPolicy(Input):
    scope_type: Literal["student_daily", "teaching", "project"]
    scope_id: str = Field(min_length=1, max_length=160)
    class_id: str | None = Field(default=None, max_length=160)
    limit_units: int = Field(ge=0, strict=True)
    emergency_units: int = Field(default=0, ge=0, strict=True)
    period: str | None = Field(default=None, max_length=10)


class Funding(Input):
    account_id: str = Field(min_length=1, max_length=160)
    units: int = Field(gt=0, strict=True)
    transaction_id: str = Field(min_length=1, max_length=160)


class Reconcile(Input):
    outcome: Literal["succeeded", "failed", "not_executed"]
    cost_units: int | None = Field(default=None, ge=0, strict=True)
    usage: dict | None = None
    result: dict | None = None
    reason: str = Field(min_length=1, max_length=1000)


class CostSettlement(Input):
    cost_units: int = Field(ge=0, strict=True)
    usage: dict | None = None
    reason: str = Field(min_length=1, max_length=1000)


def _queue_action(action):
    try:
        return action()
    except (InvalidTransition, IdempotencyConflict) as error:
        raise HTTPException(status_code=409, detail={"code": "job_conflict", "message": str(error)}) from error
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "任务不存在或不可见"}) from None
    except QueueError as error:
        raise HTTPException(status_code=409, detail={"code": "job_needs_attention", "message": "任务需要确认或对账"}) from error


@router.get("/jobs")
def list_jobs(request: Request, status: str | None = None, limit: int = Query(50, ge=1, le=100),
              offset: int = Query(0, ge=0), actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    jobs = runtime.queue.list_jobs(status=status, limit=limit, offset=offset)
    return {"items": [job_view(job) for job in jobs if can_access_job(runtime, actor, job)],
            "next_offset": offset + len(jobs) if len(jobs) == limit else None}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request, actor=Depends(get_platform_user)):
    return job_view(require_job(get_runtime(request), actor, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    job = require_job(runtime, actor, job_id, write=True)
    result = _queue_action(lambda: runtime.queue.cancel(job_id))
    from ..worker import finalize_domain_job
    finalize_domain_job(runtime, result, "cancelled")
    runtime.platform.record_audit_event("job_cancel_requested", actor.id, resource_type="job", resource_id=job_id)
    return job_view(result)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, payload: ResumeRequest, request: Request, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    job = require_job(runtime, actor, job_id, write=True)
    if job.cancel_requested or job.status == "cancelled":
        raise HTTPException(status_code=409, detail={"code": "cancelled_job", "message": "已取消的请求不能恢复，请重新发起业务请求"})
    if job.paid and not payload.confirm_paid:
        raise HTTPException(status_code=409, detail={"code": "confirmation_required", "message": "恢复可能产生新费用，需要明确确认"})
    if job.status == "awaiting_confirmation":
        result = _queue_action(lambda: runtime.queue.confirm_recovery(job_id, approved=True))
    else:
        result = _queue_action(lambda: runtime.queue.retry(job_id, confirm_paid=payload.confirm_paid))
    runtime.platform.record_audit_event("job_resume_confirmed", actor.id, resource_type="job", resource_id=job_id)
    return job_view(result)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = Query(0, ge=0),
                     once: bool = False, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    require_job(runtime, actor, job_id)
    raw_cursor = request.headers.get("last-event-id")
    if raw_cursor:
        try:
            after = max(after, int(raw_cursor))
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor", "message": "事件游标无效"}) from None

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            try:
                current_actor = get_platform_user(request, runtime.platform)
                job = require_job(runtime, current_actor, job_id)
            except HTTPException:
                yield 'event: revoked\ndata: {"code":"access_revoked"}\n\n'
                return
            events = runtime.queue.list_events(job_id, after=cursor)
            if not events:
                latest = runtime.queue.list_events(job_id)
                if cursor and latest and cursor > latest[-1].event_id:
                    cursor = latest[-1].event_id
                    yield f"id: {cursor}\nevent: snapshot\ndata: {json.dumps(job_view(job), ensure_ascii=False)}\n\n"
            for event in events:
                cursor = event.event_id
                data = {"job_id": job_id, "event_type": event.event_type,
                        "status": job.status, "created_at": event.created_at}
                yield f"id: {cursor}\nevent: status\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if once or job.status in {"succeeded", "failed", "cancelled", "awaiting_confirmation", "reconciling"}:
                yield f"event: snapshot\ndata: {json.dumps(job_view(job), ensure_ascii=False)}\n\n"
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/companion/responses/{response_id}/stream")
async def response_stream(response_id: str, request: Request, after: int = Query(0, ge=0),
                          once: bool = False, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    try:
        response_access(runtime, actor.id, response_id)
    except (LookupError, PermissionError):
        raise HTTPException(status_code=404, detail={"code": "response_not_found", "message": "回答不存在"}) from None
    raw = request.headers.get("last-event-id")
    if raw is not None:
        try:
            header_cursor = int(raw)
            if not 0 <= header_cursor <= 9223372036854775807:
                raise ValueError("cursor out of range")
            after = max(after, header_cursor)
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor", "message": "事件游标无效"}) from None
    if after > 9223372036854775807:
        raise HTTPException(status_code=422, detail={"code": "invalid_cursor", "message": "事件游标无效"})

    def frame(event_type, data, event_id):
        return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def snapshot(response):
        fields = ("response_id", "status", "text", "redacted", "notice", "error", "generation", "last_event_id",
                  "assistant_message_id", "content_type", "task_id", "question_version_id", "job_id", "job")
        return frame("snapshot", {key: response.get(key) for key in fields}, response["last_event_id"])

    def visibility_state(response):
        job = response.get("job") or {}
        return (response.get("redacted", False), response.get("notice"),
                response["status"] in {"failed", "cancelled"}, job.get("status"),
                job.get("cancel_requested"), job.get("reconciliation_required"), job.get("cost_pending"))

    async def stream():
        cursor, generation, visibility = after, None, None
        while not await request.is_disconnected():
            try:
                user = get_platform_user(request, runtime.platform)
                response, candidates = runtime.companion.read_response_events(
                    response_id, user.id, after=cursor, limit=1000,
                    answer_policy=answer_policy_provider(runtime, user.id),
                )
                validate_response_access(runtime, user.id, response)
                response["job"] = response_job_state(runtime, response)
                # This is a fresh check immediately before any output; only
                # event IDs from the page are buffered, never projected text.
                response = response_access(runtime, user.id, response_id)
            except (HTTPException, LookupError, PermissionError):
                yield 'event: revoked\ndata: {"code":"access_revoked"}\n\n'
                return
            current_visibility = visibility_state(response)
            # Every connection replaces any cached prefix before replay,
            # including idle tail resumes after policy or generation changes.
            if (generation is None or generation != response["generation"] or cursor > response["last_event_id"]
                    or visibility is not None and visibility != current_visibility):
                generation = response["generation"]
                cursor = response["last_event_id"]
                visibility = current_visibility
                yield snapshot(response)
                if response["status"] in {"completed", "failed", "cancelled"} or once:
                    return
                continue
            visibility = current_visibility
            changed = False
            for candidate in candidates:
                try:
                    user = get_platform_user(request, runtime.platform)
                    response, event = response_event_access(runtime, user.id, response_id, candidate["event_id"])
                except (HTTPException, LookupError, PermissionError):
                    yield 'event: revoked\ndata: {"code":"access_revoked"}\n\n'
                    return
                current_visibility = visibility_state(response)
                if response["generation"] != generation or event is None or current_visibility != visibility:
                    generation = response["generation"]
                    cursor = response["last_event_id"]
                    visibility = current_visibility
                    yield snapshot(response)
                    changed = True
                    break
                cursor = event["event_id"]
                data = {"response_id": response_id, "generation": event["generation"], **event["data"]}
                yield frame(event["event_type"], data, cursor)
            if changed:
                continue
            try:
                user = get_platform_user(request, runtime.platform)
                response = response_access(runtime, user.id, response_id)
            except (HTTPException, LookupError, PermissionError):
                yield 'event: revoked\ndata: {"code":"access_revoked"}\n\n'
                return
            if response["generation"] != generation or visibility_state(response) != visibility:
                continue
            terminal = response["status"] in {"completed", "failed", "cancelled"}
            if cursor >= response["last_event_id"] and (once or terminal):
                yield snapshot(response)
                return
            if once and not terminal:
                yield snapshot(response)
                return
            if not candidates:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.put("/budgets/limits")
def set_budget_limit(payload: BudgetPolicy, request: Request,
                     actor=Depends(require_platform_roles("teacher", "admin"))):
    runtime = get_runtime(request)
    if actor.role != "admin":
        if payload.scope_type != "student_daily" or not payload.class_id:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "教师只能设置所属学生日额度"})
        classroom = runtime.platform.get_class(payload.class_id)
        if classroom is None or classroom.get("owner_teacher_id") != actor.id:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "学生有效日额度由班级负责人确认"})
        if runtime.platform.get_class_membership(payload.class_id, payload.scope_id) is None:
            raise HTTPException(status_code=404, detail={"code": "student_not_found", "message": "该学生不属于该班级"})
    classroom = runtime.platform.get_class(payload.class_id) if payload.class_id else None
    if payload.class_id and classroom is None:
        raise HTTPException(status_code=404, detail={"code": "class_not_found", "message": "班级不存在"})
    result = runtime.queue.budget_ledger.set_limit(
        payload.scope_type, payload.scope_id, payload.limit_units,
        emergency_units=payload.emergency_units, period=payload.period,
        course_id=classroom["course_id"] if classroom else None, class_id=payload.class_id,
    )
    runtime.platform.record_audit_event("budget_limit_set", actor.id, resource_type="budget", resource_id=payload.scope_id,
                                        payload={"scope_type": payload.scope_type, "limit_units": payload.limit_units})
    return asdict(result)


@router.get("/budgets/limits")
def list_budget_limits(request: Request, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    limits = runtime.queue.budget_ledger.list_limits()
    if actor.role == "admin":
        visible = limits
    elif actor.role == "student":
        visible = [item for item in limits if item.scope_type == "student_daily" and item.scope_id == actor.id]
    else:
        classes = runtime.platform.list_accessible_classes(actor.id)
        class_ids = {classroom["id"] for classroom in classes}
        allowed = {member["user_id"] for classroom in classes
                   for member in runtime.platform.list_class_memberships(classroom["id"])}
        visible = [item for item in limits if item.scope_type == "student_daily" and item.scope_id in allowed
                   and (not item.class_id or item.class_id in class_ids)]
    return {"items": [asdict(item) for item in visible], "currency": runtime.settings.budget_currency}


@router.post("/admin/budgets/fund")
def fund_budget(payload: Funding, request: Request, actor=Depends(require_platform_roles("admin"))):
    runtime = get_runtime(request)
    result = _queue_action(lambda: runtime.queue.fund_budget(payload.account_id, payload.units,
                                                            transaction_id=payload.transaction_id))
    runtime.platform.record_audit_event("budget_funded", actor.id, resource_type="budget", resource_id=payload.account_id,
                                        payload={"units": payload.units, "transaction_id": payload.transaction_id})
    return asdict(result)


@router.post("/admin/jobs/{job_id}/calls/{call_id}/reconcile")
def reconcile_call(job_id: str, call_id: str, payload: Reconcile, request: Request,
                   actor=Depends(require_platform_roles("admin"))):
    runtime = get_runtime(request)
    require_job(runtime, actor, job_id)
    result = _queue_action(lambda: runtime.queue.reconcile_call(job_id, call_id, payload.outcome,
                                                               result=payload.result, usage=payload.usage,
                                                               cost_units=payload.cost_units))
    runtime.platform.record_audit_event("model_call_reconciled", actor.id, resource_type="call", resource_id=call_id,
                                        payload={"reason": payload.reason, "outcome": payload.outcome})
    return job_view(result)


@router.post("/admin/calls/{call_id}/settle")
def settle_call(call_id: str, payload: CostSettlement, request: Request,
                actor=Depends(require_platform_roles("admin"))):
    runtime = get_runtime(request)
    changed = _queue_action(lambda: runtime.queue.settle_call_cost(call_id, payload.cost_units, usage=payload.usage))
    runtime.platform.record_audit_event("model_cost_settled", actor.id, resource_type="call", resource_id=call_id,
                                        payload={"reason": payload.reason, "cost_units": payload.cost_units})
    return {"call_id": call_id, "changed": changed, "cost_pending": False}


@router.get("/jobs/{job_id}/calls")
def list_call_metadata(job_id: str, request: Request, actor=Depends(get_platform_user)):
    runtime = get_runtime(request)
    require_job(runtime, actor, job_id)
    fields = ("call_id", "status", "model_name", "model_version", "price_version", "input_version",
              "cost_units", "cost_pending", "created_at", "updated_at")
    return {"items": [{key: getattr(call, key) for key in fields}
                      for call in runtime.queue.list_calls(job_id)]}
