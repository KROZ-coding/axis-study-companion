"""Authenticated, scoped access to immutable reports and local file exports."""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import User
from ..platform_store import PlatformStore
from ..report_store import ReportConflict, ReportStore, SNAPSHOT_SCHEMA_VERSION, normalize_scope
from ..runtime import Runtime, get_runtime
from ..services.mastery import DEFAULT_RULE_VERSION, get_rule, list_rules
from ..services.report_exports import EXPORT_FORMATS, ExportDependencyUnavailable, build_report_snapshot, render_export
from ..v1_deps import get_platform_store, get_platform_user


router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


class ReportScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["class", "student", "goal", "question_bank"]
    course_id: str | None = Field(default=None, min_length=1, max_length=200)
    class_id: str | None = Field(default=None, min_length=1, max_length=200)
    student_id: str | None = Field(default=None, min_length=1, max_length=200)
    goal_id: str | None = Field(default=None, min_length=1, max_length=200)
    question_bank_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_scope(self):
        normalize_scope(self.model_dump(exclude_none=True))
        return self


class GenerateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: ReportScope
    schema_version: Literal["report.snapshot.v1"]
    rule_version: str = Field(min_length=1, max_length=100)
    expected_source_revision: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_rule(self):
        get_rule(self.rule_version)
        return self


def _error(code: int, name: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": name, "message": message})


def get_report_store(runtime: Runtime = Depends(get_runtime)) -> ReportStore:
    return runtime.reports


def _pending() -> HTTPException:
    return _error(501, "report_scope_pending", "此范围的版本化学习证据接口尚未就绪")


def _authorize(
    scope: Mapping[str, Any], actor: User, platform: PlatformStore, learning: Any, *, creating: bool = False,
) -> dict[str, str]:
    scope = normalize_scope(scope)
    denied = _error(403 if creating else 404, "forbidden" if creating else "report_not_found", "无权访问该范围的报告" if creating else "报告不存在")
    row = platform.get_user(actor.id)
    if row is None or row["status"] != "active":
        raise _error(401, "unauthorized", "请先登录")
    role = row["role"]
    if role == "student":
        if scope["kind"] not in {"student", "goal"} or scope.get("student_id") != actor.id:
            raise denied
    elif role not in {"teacher", "admin"}:
        raise denied
    class_id, course_id = scope.get("class_id"), scope.get("course_id")
    if scope["kind"] == "question_bank":
        if course_id is None:
            raise denied
        if creating and role == "teacher":
            allowed = platform.can_manage_course_materials(actor.id, course_id)
        else:
            allowed = platform.can_access_course(actor.id, course_id)
        if not allowed:
            raise denied
        if class_id is not None:
            classroom = platform.get_class(class_id)
            if classroom is None or classroom["course_id"] != course_id:
                raise denied
        return scope
    if role == "student" and not creating:
        # Ownership of a frozen personal report survives transfers out of its
        # historical class. Teachers still need a current grant to that class.
        return scope
    if scope["kind"] == "goal" and creating:
        try:
            authoritative = learning.get_goal_scope(scope["goal_id"], actor.id)
        except (LookupError, PermissionError) as error:
            raise denied from error
        if not isinstance(authoritative, Mapping):
            raise denied
        if class_id is not None and authoritative.get("class_id") != class_id:
            raise denied
        if course_id is not None and authoritative.get("course_id") != course_id:
            raise denied
        class_id = authoritative.get("class_id")
        course_id = authoritative.get("course_id")
        if class_id is not None:
            scope["class_id"] = class_id
        if course_id is not None:
            scope["course_id"] = course_id
    # A teacher's role is never a global grant. Even goal owners need a current
    # class grant, and a guessed class ID cannot authorize another class's goal.
    if role in {"teacher", "admin"} and class_id is None:
        raise denied
    if scope["kind"] == "class" and class_id is None:
        raise denied
    if class_id is not None:
        classroom = platform.get_class(class_id)
        if classroom is None or (course_id is not None and classroom["course_id"] != course_id):
            raise denied
        if not platform.can_access_class(actor.id, class_id, teaching=creating and role == "teacher"):
            raise denied
        # New reports require the subject's current membership. Frozen history
        # retains its original class scope after a student transfers.
        if creating and scope.get("student_id") and platform.get_class_membership(class_id, scope["student_id"]) is None:
            raise denied
    elif role == "student" and scope["kind"] == "goal":
        raise denied
    return scope


def _report_input(learning: Any, scope: Mapping[str, Any], actor_id: str) -> dict[str, Any]:
    method = getattr(learning, "report_input", None)
    if method is None:
        raise _pending()
    parameters = inspect.signature(method).parameters
    if "scope" not in parameters:
        # Legacy report_input(goal_id, actor_id) exposes raw tasks and has no
        # source token. Never infer a revision from its hashes or timestamps.
        raise _pending()
    try:
        value = method(scope=scope, actor_id=actor_id)
    except NotImplementedError as error:
        raise _pending() from error
    except (LookupError, PermissionError) as error:
        raise _error(404, "report_scope_not_found", "报告范围不存在或已不可访问") from error
    if not isinstance(value, Mapping) or type(value.get("source_revision")) is not int:
        raise _pending()
    if value.get("capability") == "pending" or value.get("status") == "pending":
        raise _pending()
    return dict(value)


def _ensure_subject(snapshot: Mapping[str, Any], actor: User) -> None:
    if actor.role != "student":
        return
    if snapshot["scope"].get("student_id") != actor.id:
        raise _error(404, "report_not_found", "报告不存在")
    for name in ("students", "mastery", "evidence"):
        if any(row.get("student_id") != actor.id for row in snapshot["tables"][name]["rows"]):
            raise _error(404, "report_not_found", "报告不存在")


def _fresh_actor(request: Request, platform: PlatformStore) -> User:
    return get_platform_user(request, platform)


def _read_report(
    report_id: str, actor: User, platform: PlatformStore, runtime: Runtime, store: ReportStore,
) -> dict[str, Any]:
    report = store.get_report(report_id)
    if report is None:
        raise _error(404, "report_not_found", "报告不存在")
    _authorize(report["scope"], actor, platform, runtime.learning)
    return report


def _read_revision(
    report: Mapping[str, Any], revision_id: str | None, actor: User, store: ReportStore,
) -> dict[str, Any]:
    revision_id = revision_id or report["current_revision_id"]
    revision = store.get_revision(revision_id) if revision_id else None
    if revision is None or revision["report_id"] != report["id"]:
        raise _error(404, "report_revision_not_found", "报告修订不存在")
    _ensure_subject(revision["snapshot"], actor)
    return revision


@router.get("/rules")
def rules(actor: User = Depends(get_platform_user)):
    return {"default_rule_version": DEFAULT_RULE_VERSION, "rules": list_rules(), "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "export_formats": list(EXPORT_FORMATS)}


@router.get("/source-revision")
def source_revision(
    scope: ReportScope = Depends(), actor: User = Depends(get_platform_user),
    platform: PlatformStore = Depends(get_platform_store), runtime: Runtime = Depends(get_runtime),
):
    authorized_scope = _authorize(scope.model_dump(exclude_none=True), actor, platform, runtime.learning, creating=True)
    source = _report_input(runtime.learning, authorized_scope, actor.id)
    return {"scope": authorized_scope, "source_revision": source["source_revision"],
            "schema_version": SNAPSHOT_SCHEMA_VERSION, "default_rule_version": DEFAULT_RULE_VERSION}


@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/generate", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def generate_report(
    payload: GenerateReport, request: Request, response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
    runtime: Runtime = Depends(get_runtime), store: ReportStore = Depends(get_report_store),
):
    scope = _authorize(payload.scope.model_dump(exclude_none=True), actor, platform, runtime.learning, creating=True)
    fingerprint = {**payload.model_dump(mode="json"), "scope": scope}
    try:
        previous = store.get_idempotent_snapshot(actor.id, idempotency_key, request_fingerprint=fingerprint)
        if previous is not None:
            # Replays are authorized again and return the original frozen revision.
            revision = store.create_revision(previous["id"])
            _ensure_subject(revision["snapshot"], actor)
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["Cache-Control"] = "no-store"
            return revision
        source = _report_input(runtime.learning, scope, actor.id)
        if source["source_revision"] != payload.expected_source_revision:
            raise _error(409, "source_revision_conflict", "学习证据已更新，请基于最新源修订重新生成")
        snapshot = build_report_snapshot(source, scope, rule_version=payload.rule_version)
        _ensure_subject(snapshot, actor)
        with platform.transaction():
            current_actor = _fresh_actor(request, platform)
            _authorize(scope, current_actor, platform, runtime.learning, creating=True)
            frozen = store.create_snapshot(scope, source["source_revision"], snapshot, created_by=actor.id,
                                           rule_version=payload.rule_version, idempotency_key=idempotency_key,
                                           request_fingerprint=fingerprint)
            revision = store.create_revision(frozen["id"])
            platform.record_audit_event("report_created", actor.id, resource_type="report", resource_id=revision["report_id"],
                                        payload={"revision_id": revision["id"], "source_revision": revision["source_revision"],
                                                 "snapshot_sha256": revision["snapshot_sha256"]})
    except ReportConflict as error:
        raise _error(409, "report_conflict", "幂等键或源修订已被用于不同的报告请求") from error
    except ValueError as error:
        raise _error(422, "invalid_report_evidence", "报告证据不符合版本化接口约定") from error
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = f"/api/v1/reports/{revision['report_id']}/revisions/{revision['id']}"
    return revision


@router.get("")
def list_reports(
    actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
    runtime: Runtime = Depends(get_runtime), store: ReportStore = Depends(get_report_store),
):
    items = []
    for report in store.list_reports():
        try:
            _authorize(report["scope"], actor, platform, runtime.learning)
            if report["current_revision_id"]:
                _read_revision(report, None, actor, store)
            items.append(report)
        except HTTPException as error:
            if error.status_code not in {403, 404}:
                raise
    return {"items": items}


@router.get("/{report_id}")
def read_current(
    report_id: str, response: Response, actor: User = Depends(get_platform_user),
    platform: PlatformStore = Depends(get_platform_store), runtime: Runtime = Depends(get_runtime),
    store: ReportStore = Depends(get_report_store),
):
    report = _read_report(report_id, actor, platform, runtime, store)
    response.headers["Cache-Control"] = "no-store"
    return _read_revision(report, None, actor, store)


@router.get("/{report_id}/revisions")
def list_revisions(
    report_id: str, actor: User = Depends(get_platform_user), platform: PlatformStore = Depends(get_platform_store),
    runtime: Runtime = Depends(get_runtime), store: ReportStore = Depends(get_report_store),
):
    report = _read_report(report_id, actor, platform, runtime, store)
    if actor.role == "student":
        _read_revision(report, None, actor, store)
    return {"report_id": report_id, "items": store.list_revisions(report_id)}


@router.get("/{report_id}/revisions/{revision_id}")
def read_revision(
    report_id: str, revision_id: str, response: Response, actor: User = Depends(get_platform_user),
    platform: PlatformStore = Depends(get_platform_store), runtime: Runtime = Depends(get_runtime),
    store: ReportStore = Depends(get_report_store),
):
    report = _read_report(report_id, actor, platform, runtime, store)
    response.headers["Cache-Control"] = "no-store"
    return _read_revision(report, revision_id, actor, store)


@router.get("/{report_id}/export/{format}")
def export_report(
    report_id: str, format: Literal["html", "pdf", "xlsx", "json", "csv"], request: Request,
    revision_id: str | None = Query(default=None, max_length=100), actor: User = Depends(get_platform_user),
    platform: PlatformStore = Depends(get_platform_store), runtime: Runtime = Depends(get_runtime),
    store: ReportStore = Depends(get_report_store),
):
    report = _read_report(report_id, actor, platform, runtime, store)
    revision = _read_revision(report, revision_id, actor, store)
    try:
        artifact = render_export(revision, format)
    except ExportDependencyUnavailable as error:
        raise _error(503, "export_dependency_unavailable", "服务器暂未配置此格式所需的本地导出依赖") from error
    except ValueError as error:
        raise _error(422, "invalid_export_snapshot", "冻结快照无法安全导出为此格式") from error
    # Permission/session changes during a long PDF render take effect before any
    # bytes are released; report IDs are never bearer download capabilities.
    current_actor = _fresh_actor(request, platform)
    _authorize(report["scope"], current_actor, platform, runtime.learning)
    _ensure_subject(revision["snapshot"], current_actor)
    platform.record_audit_event("report_exported", actor.id, resource_type="report", resource_id=report_id,
                                payload={"revision_id": revision["id"], "format": format,
                                         "snapshot_sha256": revision["snapshot_sha256"], "artifact_sha256": artifact.sha256})
    return Response(content=artifact.content, media_type=artifact.media_type,
                    headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "X-Report-Snapshot-SHA256": revision["snapshot_sha256"],
                             "X-Report-Revision": revision["id"],
                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})
