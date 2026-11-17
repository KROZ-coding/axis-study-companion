"""Teacher class-scoped frozen insights and confirmed original-goal next rounds."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..learning_store import IdempotencyConflict, InvalidTransition, VersionConflict
from ..runtime import get_runtime
from ..schemas.v1_teaching_insights import BoundedPlanConfirm, InsightCreate, NextRoundCreate, NextRoundPreview, NextRoundPublish, NextRoundSave
from ..services.teaching_insights import TeachingInsights
from ..services.teacher_planning import PlanningConflict
from ..teaching_insight_store import InsightConflict
from ..v1_deps import require_platform_roles


router = APIRouter(prefix="/api/v1/teaching-insights", tags=["teaching-insights"])


def _service(request):
    runtime = get_runtime(request)
    return TeachingInsights(runtime)


def _invoke(response, action):
    response.headers["Cache-Control"] = "no-store"
    try:
        return action()
    except (LookupError, PermissionError):
        raise HTTPException(404, detail={"code": "insight_not_found", "message": "教学总结或草稿不存在或当前权限不足"}) from None
    except (InsightConflict, PlanningConflict) as error:
        raise HTTPException(409, detail={"code": error.code, "message": str(error), "retryable": True}) from error
    except (IdempotencyConflict, InvalidTransition, VersionConflict) as error:
        raise HTTPException(409, detail={"code": type(error).__name__, "message": str(error), "retryable": True}) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, detail={"code": "invalid_teaching_insight", "message": str(error)}) from error


@router.post("", status_code=202)
def create_insight(payload: InsightCreate, request: Request, response: Response, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).create(actor.id, **payload.model_dump()))


@router.get("")
def list_insights(request: Request, response: Response, class_id: str = Query(min_length=1, max_length=160),
                  goal_id: str | None = Query(default=None, min_length=1, max_length=160),
                  limit: int = Query(default=50, ge=1, le=100), actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).list(actor.id, class_id=class_id, goal_id=goal_id, limit=limit))


# Fixed next-round paths must be registered before /{insight_id}.
@router.get("/next-round-drafts/{draft_id}")
def read_next_round(draft_id: str, request: Request, response: Response, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).get_next_round(actor.id, draft_id))


@router.put("/next-round-drafts/{draft_id}")
def save_next_round(draft_id: str, payload: NextRoundSave, request: Request, response: Response,
                    actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).save_next_round(actor.id, draft_id, **payload.model_dump()))


@router.post("/next-round-drafts/{draft_id}/preview")
def preview_next_round(draft_id: str, payload: NextRoundPreview, request: Request, response: Response,
                       actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).preview_next_round(actor.id, draft_id, **payload.model_dump()))


@router.post("/next-round-drafts/{draft_id}/publish", status_code=201)
def publish_next_round(draft_id: str, payload: NextRoundPublish, request: Request, response: Response,
                       actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).publish_next_round(actor.id, draft_id, **payload.model_dump()))


@router.post("/next-round-drafts/{draft_id}/bounded-plan", status_code=201)
def configure_bounded_plan(draft_id: str, payload: BoundedPlanConfirm, request: Request, response: Response,
                           actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).configure_bounded_plan(actor.id, draft_id, **payload.model_dump()))


@router.get("/{insight_id}")
def read_insight(insight_id: str, request: Request, response: Response, actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).get(actor.id, insight_id))


@router.post("/{insight_id}/next-round-drafts", status_code=201)
def create_next_round(insight_id: str, payload: NextRoundCreate, request: Request, response: Response,
                      actor=Depends(require_platform_roles("teacher"))):
    return _invoke(response, lambda: _service(request).create_next_round(actor.id, insight_id, **payload.model_dump()))
