"""Course-authorized imports and frozen organizer-data competition analyses."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import threading
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..models import User
from ..platform_store import PlatformStore
from ..runtime import Runtime, get_runtime
from ..services.competition_inputs import CompetitionInputError, LIMITS, parse_organizer_inputs, schema_description
from ..services.competition_runs import (CompetitionConflict, CompetitionRunError, CompetitionRunStore,
                                         export_run)
from ..v1_deps import get_platform_store, get_platform_user

router = APIRouter(prefix="/api/v1/competition", tags=["competition"])
_STORE_LOCK = threading.RLock()


class CreateTextPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=12 * 1024 * 1024)
    course_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=160)
    csv_texts: dict[str, str] = Field(min_length=1, max_length=8)
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class GroupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=100)
    student_ids: list[str] = Field(min_length=1, max_length=5000)


class MaterialMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_id: str = Field(min_length=1, max_length=120)
    topic_id: str = Field(min_length=1, max_length=120)
    indicator_ids: list[str] = Field(min_length=1, max_length=100)


class RunScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_ids: list[str] | None = Field(default=None, max_length=100)
    class_ids: list[str] | None = Field(default=None, max_length=5000)
    student_ids: list[str] | None = Field(default=None, max_length=5000)
    groups: list[GroupSelection] = Field(default_factory=list, max_length=100)
    representative_student_ids: list[str] | None = Field(default=None, max_length=3)
    material_mappings: list[MaterialMapping] = Field(default_factory=list, max_length=1000)


class ConfirmRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1)
    scope: RunScope = Field(default_factory=RunScope)


def _error(code: int, name: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": name, "message": message})


def get_competition_run_store(request: Request, runtime: Runtime = Depends(get_runtime)) -> CompetitionRunStore:
    store = getattr(request.app.state, "competition_run_store", None)
    if store is not None:
        return store
    with _STORE_LOCK:
        store = getattr(request.app.state, "competition_run_store", None)
        if store is None:
            store = CompetitionRunStore(runtime.data_dir / "competition.sqlite3")
            request.app.state.competition_run_store = store
        return store


def _fresh_actor(platform: PlatformStore, actor_id: str) -> dict:
    row = platform.get_user(actor_id)
    if row is None or row["status"] != "active":
        raise _error(401, "unauthorized", "请重新登录")
    if row["role"] not in {"teacher", "admin"}:
        raise _error(403, "forbidden", "仅教师或管理员可使用课程分析")
    return row


def _authorize(course_id: str, actor: User, platform: PlatformStore, *, write: bool = False) -> None:
    denied = _error(404, "competition_course_not_found", "课程不存在或当前账号不可访问")
    row = platform.get_user(actor.id)
    if row is None or row["status"] != "active":
        raise _error(401, "unauthorized", "请先登录")
    if row["role"] not in {"teacher", "admin"}:
        raise _error(403, "forbidden", "仅教师或管理员可使用课程分析")
    if platform.get_course(course_id) is None or not platform.can_access_course(actor.id, course_id, teaching=write):
        raise denied


def _inputs(files: Mapping[str, bytes]):
    try:
        return parse_organizer_inputs(files)
    except CompetitionInputError as error:
        raise _error(422, "invalid_competition_input", str(error)) from error


def _clean_scope(payload: RunScope) -> dict[str, Any]:
    value = payload.model_dump(exclude_none=True)
    value["groups"] = [group.model_dump() for group in payload.groups]
    value["material_mappings"] = [mapping.model_dump() for mapping in payload.material_mappings]
    return value


def _run_projection(run: dict) -> dict:
    return {"run": {"id": run["id"], "course_id": run["course_id"], "title": run["title"],
                     "status": run["status"], "revision": run["revision"], "created_at": run["created_at"],
                     "confirmed_at": run["confirmed_at"]},
            "preview": run["preview"], "result": run["result"]}


@router.get("/schema")
def schema(actor: User = Depends(get_platform_user)):
    if actor.role not in {"teacher", "admin"}:
        raise _error(403, "forbidden", "仅教师或管理员可使用课程分析")
    return schema_description()


@router.post("/runs", status_code=status.HTTP_201_CREATED)
def create_preview(payload: CreateTextPreview, response: Response, actor: User = Depends(get_platform_user),
                   platform: PlatformStore = Depends(get_platform_store),
                   store: CompetitionRunStore = Depends(get_competition_run_store)):
    _authorize(payload.course_id, actor, platform, write=True)
    if any(not name.lower().endswith(".csv") for name in payload.csv_texts):
        raise _error(422, "invalid_competition_input", "csv_texts 键必须为学情数据表CSV文件名")
    files = {name: text.encode("utf-8") for name, text in payload.csv_texts.items()}
    parsed = _inputs(files)
    # Parsing can take time; course membership and account status are checked again before persistence.
    _fresh_actor(platform, actor.id)
    _authorize(payload.course_id, actor, platform, write=True)
    try:
        run = store.create_preview(payload.course_id, actor.id, payload.title, parsed, request_id=payload.request_id,
                                   access_check=lambda: _authorize(payload.course_id, actor, platform, write=True))
    except CompetitionRunError as error:
        raise _error(422, "invalid_competition_preview", str(error)) from error
    except CompetitionConflict as error:
        raise _error(409, "competition_creation_conflict", "该请求标识已用于另一组输入，请恢复原分析记录或新建请求") from error
    response.headers["Cache-Control"] = "no-store"
    return _run_projection(run)


@router.post("/runs/upload", status_code=status.HTTP_201_CREATED)
async def upload_preview(response: Response, course_id: str = Form(min_length=1, max_length=200), title: str = Form(min_length=1, max_length=160),
                         files: list[UploadFile] = File(min_length=1, max_length=8),
                         request_id: str | None = Form(default=None, min_length=1, max_length=120),
                         actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
                         store: CompetitionRunStore = Depends(get_competition_run_store)):
    _authorize(course_id, actor, platform, write=True)
    if len(files) > LIMITS["max_files"]:
        raise _error(422, "invalid_competition_input", "上传文件数量超过上限")
    materialized = {}
    total = 0
    try:
        for upload in files:
            name = upload.filename or ""
            if name in materialized:
                raise _error(422, "invalid_competition_input", "不能重复上传同名表格")
            content = await upload.read(LIMITS["max_file_bytes"] + 1)
            total += len(content)
            if len(content) > LIMITS["max_file_bytes"] or total > LIMITS["max_total_bytes"]:
                raise _error(422, "invalid_competition_input", "上传文件或合计大小超过上限")
            materialized[name] = content
        parsed = _inputs(materialized)
        _fresh_actor(platform, actor.id)
        _authorize(course_id, actor, platform, write=True)
        try:
            run = store.create_preview(course_id, actor.id, title, parsed, request_id=request_id,
                                       access_check=lambda: _authorize(course_id, actor, platform, write=True))
        except CompetitionRunError as error:
            raise _error(422, "invalid_competition_preview", str(error)) from error
        except CompetitionConflict as error:
            raise _error(409, "competition_creation_conflict", "该请求标识已用于另一组输入，请恢复原分析记录或新建请求") from error
        response.headers["Cache-Control"] = "no-store"
        return _run_projection(run)
    finally:
        for upload in files:
            await upload.close()


@router.get("/runs")
def list_runs(course_id: str = Query(min_length=1, max_length=200), actor: User = Depends(get_platform_user),
              platform: PlatformStore = Depends(get_platform_store), store: CompetitionRunStore = Depends(get_competition_run_store)):
    _authorize(course_id, actor, platform)
    return {"items": store.list_course(course_id)}


@router.get("/runs/by-request/{request_id}")
def read_run_by_request(request_id: str, response: Response, course_id: str = Query(min_length=1, max_length=200),
                        actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
                        store: CompetitionRunStore = Depends(get_competition_run_store)):
    _authorize(course_id, actor, platform)
    try:
        run = store.get_by_request(course_id, actor.id, request_id)
    except CompetitionRunError as error:
        raise _error(422, "invalid_competition_request_id", str(error)) from error
    if run is None:
        raise _error(404, "competition_run_not_found", "分析记录不存在或当前账号不可访问")
    _authorize(course_id, actor, platform)
    response.headers["Cache-Control"] = "no-store"
    return _run_projection(run)


@router.get("/runs/{run_id}")
def read_run(run_id: str, response: Response, actor: User = Depends(get_platform_user),
             platform: PlatformStore = Depends(get_platform_store), store: CompetitionRunStore = Depends(get_competition_run_store)):
    run = store.get(run_id)
    if run is None:
        raise _error(404, "competition_run_not_found", "分析记录不存在或当前账号不可访问")
    _authorize(run["course_id"], actor, platform)
    response.headers["Cache-Control"] = "no-store"
    return _run_projection(run)


@router.post("/runs/{run_id}/confirm")
def confirm_run(run_id: str, payload: ConfirmRun, request: Request, response: Response,
                actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
                store: CompetitionRunStore = Depends(get_competition_run_store)):
    run = store.get(run_id)
    if run is None:
        raise _error(404, "competition_run_not_found", "分析记录不存在或当前账号不可访问")
    _authorize(run["course_id"], actor, platform, write=True)
    _fresh_actor(platform, actor.id)
    _authorize(run["course_id"], actor, platform, write=True)
    try:
        confirmed = store.confirm(run_id, run["course_id"], actor.id, run["title"], payload.expected_revision,
                                  _clean_scope(payload.scope),
                                  access_check=lambda: _authorize(run["course_id"], actor, platform, write=True))
    except CompetitionRunError as error:
        raise _error(422, "invalid_competition_scope", str(error)) from error
    except CompetitionConflict as error:
        raise _error(409, "competition_run_conflict", "预览修订已变化或分析已经冻结，不能再次修改") from error
    if confirmed is None:
        raise _error(404, "competition_run_not_found", "分析记录不存在或当前账号不可访问")
    response.headers["Cache-Control"] = "no-store"
    return _run_projection(confirmed)


@router.get("/runs/{run_id}/export/{format}")
def export_competition_run(run_id: str, format: str, actor: User = Depends(get_platform_user),
                           platform: PlatformStore = Depends(get_platform_store), store: CompetitionRunStore = Depends(get_competition_run_store)):
    run = store.get(run_id)
    if run is None:
        raise _error(404, "competition_run_not_found", "分析记录不存在或当前账号不可访问")
    _authorize(run["course_id"], actor, platform)
    if format not in {"json", "csv", "zip", "html"}:
        raise _error(422, "unsupported_competition_export", "导出格式仅支持JSON、CSV、ZIP或HTML")
    if run["status"] != "confirmed":
        raise _error(409, "competition_run_not_confirmed", "请先确认范围并冻结分析结果")
    try:
        content, media_type, filename = export_run(run, format)
    except CompetitionRunError as error:
        raise _error(422, "invalid_competition_export", str(error)) from error
    # Scope is rechecked after rendering and before returning the export bytes.
    _fresh_actor(platform, actor.id)
    _authorize(run["course_id"], actor, platform)
    platform.record_audit_event("competition_run_exported", actor.id, resource_type="competition_run", resource_id=run_id,
                                payload={"course_id": run["course_id"], "format": format,
                                         "input_sha256": run["preview"]["input_sha256"]})
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                             "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
                             "X-Competition-Run-Id": run_id,
                             "X-Competition-Input-SHA256": run["preview"]["input_sha256"]})
