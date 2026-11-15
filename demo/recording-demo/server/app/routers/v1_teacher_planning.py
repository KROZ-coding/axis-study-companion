"""Authenticated teacher composition; application wiring only includes this router."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..content_store import ContentConflict
from ..learning_store import IdempotencyConflict, InvalidTransition, VersionConflict
from ..runtime import get_runtime
from ..schemas.v1_teacher_planning import PlanDraftSave, PlanPrepare, PlanPublish, TeachingPlanConfig
from ..services.teacher_planning import PlanningConflict, TeacherPlanning
from ..v1_deps import require_platform_roles


router = APIRouter(prefix="/api/v1/teaching-plans", tags=["teacher-planning"])


def _service(request):
    runtime = get_runtime(request)
    return runtime._get("teacher_planning", lambda: TeacherPlanning(runtime.learning, runtime.platform, runtime.content))


def _invoke(action):
    try:
        return action()
    except (LookupError, PermissionError):
        raise HTTPException(404, detail={"code": "planning_scope_not_found", "message": "课程或班级不存在或不可访问"}) from None
    except PlanningConflict as error:
        raise HTTPException(409, detail={"code": error.code, "message": str(error), "retryable": True}) from error
    except (IdempotencyConflict, InvalidTransition, VersionConflict, ContentConflict) as error:
        raise HTTPException(409, detail={"code": type(error).__name__, "message": str(error), "retryable": True}) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, detail={"code": "invalid_teaching_plan", "message": str(error)}) from error


@router.post("/preview")
def preview_plan(payload: TeachingPlanConfig, request: Request, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: _service(request).preview(actor.id, payload))


@router.post("/drafts", status_code=201)
def save_plan_draft(payload: PlanDraftSave, request: Request, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: _service(request).save_draft(actor.id, payload.config, request_id=payload.request_id))


@router.get("/drafts")
def list_plan_drafts(request: Request, class_id: str = Query(min_length=1, max_length=160),
                     limit: int = Query(default=50, ge=1, le=100), actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: _service(request).list_drafts(actor.id, class_id=class_id, limit=limit))


@router.post("/publish", status_code=201)
def publish_plan(payload: PlanPublish, request: Request, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: _service(request).publish(actor.id, payload.config, preview_token=payload.preview_token,
                                                    request_id=payload.request_id))


@router.post("/prepare", status_code=202)
def prepare_plan(payload: PlanPrepare, request: Request, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(lambda: _service(request).prepare(actor.id, payload.config, request_id=payload.request_id,
                                                    source_block_ids=payload.source_block_ids,
                                                    external_processing_allowed=payload.external_processing_allowed))
