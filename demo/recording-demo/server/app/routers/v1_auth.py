from __future__ import annotations

from functools import lru_cache
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import User
from ..platform_store import PlatformStore
from ..schemas.api import LoginRequest, PasswordChangeRequest
from ..utils.security import hash_password, new_session_id, verify_password, verify_password_async
from ..v1_deps import get_platform_store, get_platform_user, require_current_actor, require_platform_roles

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TeacherApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("username", "display_name", mode="before")
    @classmethod
    def strip_names(cls, value):
        return value.strip() if isinstance(value, str) else value


class StudentCreate(StrictRequest):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=64)
    course_id: str = Field(min_length=1, max_length=100)
    class_id: str = Field(min_length=1, max_length=100)


class CodeRequest(StrictRequest):
    user_id: str = Field(min_length=1, max_length=100)
    course_id: str | None = Field(default=None, min_length=1, max_length=100)
    class_id: str | None = Field(default=None, min_length=1, max_length=100)


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class AccountOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: Literal["student", "teacher", "admin"]
    status: Literal["active", "pending", "disabled"]


class LoginOut(BaseModel):
    user: AccountOut


class ApplicationOut(BaseModel):
    id: str
    status: Literal["pending"] = "pending"
    message: str = "申请已提交，等待管理员审核"


class ReviewOut(BaseModel):
    user_id: str
    status: Literal["active", "disabled"]


class StudentCreated(BaseModel):
    student_id: str
    activation_code: str
    expires_in_seconds: int


class ResetCodeOut(BaseModel):
    user_id: str
    reset_code: str
    expires_in_seconds: int


class ActivationCodeOut(BaseModel):
    user_id: str
    activation_code: str
    expires_in_seconds: int


class StatusRequest(StrictRequest):
    status: Literal["active", "disabled"]


def _user_out(row: dict) -> AccountOut:
    return AccountOut(**{field: row[field] for field in AccountOut.model_fields})


@lru_cache(maxsize=1)
def _decoy_hash() -> str:
    return hash_password("unavailable-platform-login-account")


def _error(code: str, message: str, http_status: int = 400) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _rate_gate(store: PlatformStore, request: Request, operation: str, subject: str) -> None:
    settings = request.app.state.settings
    if operation == "application":
        limit = max(1, settings.registration_rate_limit)
        window = max(1, settings.registration_window_seconds)
    else:
        limit = max(1, settings.login_failure_limit)
        window = max(1, settings.login_lock_seconds)
    ip = request.client.host if request.client else "unknown"
    retry = 0
    with store.transaction():
        allowed_ip, retry_ip = store.consume_rate_limit(
            operation + ":ip", ip, limit=limit if operation == "application" else max(30, limit * 10),
            window_seconds=window,
        )
        allowed_subject, retry_subject = store.consume_rate_limit(
            operation + ":subject", subject, limit=limit, window_seconds=window,
        )
        if not allowed_ip or not allowed_subject:
            retry = max(retry_ip, retry_subject)
        store.record_audit_event("auth_rate_limited" if retry else "auth_attempt", payload={"operation": operation}, ip=ip)
    if retry:
        raise HTTPException(status_code=429, detail={"code": "rate_limited", "message": "尝试过于频繁，请稍后重试"},
                            headers={"Retry-After": str(retry)})


@router.post("/teacher-applications", status_code=status.HTTP_202_ACCEPTED, response_model=ApplicationOut)
def apply_teacher(payload: TeacherApplication, request: Request, store: PlatformStore = Depends(get_platform_store)):
    _rate_gate(store, request, "application", payload.username.casefold())
    password_hash = hash_password(payload.password)
    try:
        with store.transaction():
            user = store.create_user(payload.username, payload.display_name, password_hash, "teacher", status="pending")
            store.record_audit_event("teacher_application_created", user["id"], resource_type="user", resource_id=user["id"])
    except sqlite3.IntegrityError as error:
        raise _error("account_conflict", "用户名已被使用", 409) from error
    return ApplicationOut(id=user["id"])


@router.get("/teacher-applications", response_model=list[AccountOut])
def teacher_applications(actor: User = Depends(require_platform_roles("admin")), store: PlatformStore = Depends(get_platform_store)):
    return [_user_out(row) for row in store.list_users(role="teacher", status="pending")]


def _review_teacher(store: PlatformStore, actor: User, user_id: str, approve: bool) -> ReviewOut:
    with store.transaction():
        require_current_actor(store, actor.id, "admin")
        row = store.get_user(user_id)
        if row is None or row["role"] != "teacher" or row["status"] != "pending":
            raise _error("application_not_found", "待审核申请不存在", 404)
        new_status = "active" if approve else "disabled"
        store.update_user(user_id, status=new_status)
        store.record_audit_event("teacher_application_approved" if approve else "teacher_application_rejected", actor.id,
                                 resource_type="user", resource_id=user_id)
    return ReviewOut(user_id=user_id, status=new_status)


@router.post("/teacher-applications/{user_id}/approve", response_model=ReviewOut)
def approve_teacher(user_id: str, actor: User = Depends(require_platform_roles("admin")), store: PlatformStore = Depends(get_platform_store)):
    return _review_teacher(store, actor, user_id, True)


@router.post("/teacher-applications/{user_id}/reject", response_model=ReviewOut)
def reject_teacher(user_id: str, actor: User = Depends(require_platform_roles("admin")), store: PlatformStore = Depends(get_platform_store)):
    return _review_teacher(store, actor, user_id, False)


@router.patch("/users/{user_id}/status", response_model=AccountOut)
def set_account_status(user_id: str, payload: StatusRequest, actor: User = Depends(require_platform_roles("admin")),
                       store: PlatformStore = Depends(get_platform_store)):
    with store.transaction():
        require_current_actor(store, actor.id, "admin")
        row = store.get_user(user_id)
        if row is None:
            raise _error("user_not_found", "账号不存在", 404)
        if row["status"] == "pending":
            raise _error("approval_required", "待审核或激活账号需走审核或激活流程", 409)
        if row["role"] == "admin" and payload.status == "disabled" and not any(
            item["id"] != user_id and item["status"] == "active" for item in store.list_users(role="admin")
        ):
            raise _error("last_admin", "不能停用最后一个可用管理员", 409)
        result = store.update_user(user_id, status=payload.status)
        store.record_audit_event("account_status_changed", actor.id, resource_type="user", resource_id=user_id,
                                 payload={"status": payload.status})
    return _user_out(result)


@router.post("/students", status_code=status.HTTP_201_CREATED, response_model=StudentCreated)
def create_student(payload: StudentCreate, actor: User = Depends(require_platform_roles("teacher", "admin")),
                   store: PlatformStore = Depends(get_platform_store)):
    if not store.can_access_class(actor.id, payload.class_id, teaching=True):
        raise _error("forbidden", "无权在该班级创建学生", 403)
    password_hash = hash_password(secrets.token_urlsafe(32))
    code = secrets.token_urlsafe(32)
    try:
        with store.transaction():
            require_current_actor(store, actor.id, "teacher", "admin")
            classroom = store.get_class(payload.class_id)
            if classroom is None or classroom["course_id"] != payload.course_id:
                raise _error("class_not_found", "课程班级不存在", 404)
            if not store.can_access_class(actor.id, payload.class_id, teaching=True):
                raise _error("forbidden", "无权在该班级创建学生", 403)
            student = store.create_user(payload.username, payload.display_name, password_hash, "student", status="pending")
            store.create_course_membership(payload.course_id, student["id"])
            store.create_class_membership(payload.class_id, student["id"])
            store.create_activation_code(code, student["id"], actor.id, class_id=payload.class_id, expires_at=time.time() + 86400)
            store.record_audit_event("student_created", actor.id, resource_type="user", resource_id=student["id"],
                                     payload={"class_id": payload.class_id, "course_id": payload.course_id})
    except sqlite3.IntegrityError as error:
        raise _error("student_creation_conflict", "学生账号或成员关系已存在", 409) from error
    return StudentCreated(student_id=student["id"], activation_code=code, expires_in_seconds=86400)


def _issue_code(store: PlatformStore, actor: User, payload: CodeRequest, purpose: str) -> str:
    code = secrets.token_urlsafe(32)
    try:
        with store.transaction():
            require_current_actor(store, actor.id, "teacher", "admin")
            store.create_activation_code(code, payload.user_id, actor.id, purpose=purpose, course_id=payload.course_id,
                                         class_id=payload.class_id, expires_at=time.time() + (3600 if purpose == "reset" else 86400))
            store.record_audit_event("password_reset_code_issued" if purpose == "reset" else "activation_code_issued", actor.id,
                                     resource_type="user", resource_id=payload.user_id,
                                     payload={"course_id": payload.course_id, "class_id": payload.class_id})
    except (PermissionError, ValueError, sqlite3.IntegrityError) as error:
        raise _error("forbidden", "无权为该账号签发一次性码", 403) from error
    return code


@router.post("/password-reset-codes", status_code=status.HTTP_201_CREATED, response_model=ResetCodeOut)
def issue_reset_code(payload: CodeRequest, actor: User = Depends(require_platform_roles("teacher", "admin")),
                     store: PlatformStore = Depends(get_platform_store)):
    return ResetCodeOut(user_id=payload.user_id, reset_code=_issue_code(store, actor, payload, "reset"), expires_in_seconds=3600)


@router.post("/activation-codes", status_code=status.HTTP_201_CREATED, response_model=ActivationCodeOut)
def issue_activation_code(payload: CodeRequest, actor: User = Depends(require_platform_roles("teacher", "admin")),
                          store: PlatformStore = Depends(get_platform_store)):
    return ActivationCodeOut(user_id=payload.user_id, activation_code=_issue_code(store, actor, payload, "activation"), expires_in_seconds=86400)


def _consume_code(payload: ActivationRequest, request: Request, store: PlatformStore, purpose: str) -> None:
    _rate_gate(store, request, purpose, payload.code)
    password_hash = hash_password(payload.password)
    invalid = False
    with store.transaction():
        result = store.consume_activation_code(payload.code, purpose=purpose)
        if result is None:
            invalid = True
            store.record_audit_event("activation_failed" if purpose == "activation" else "password_reset_failed")
        else:
            changes = {"password_hash": password_hash}
            if purpose == "activation":
                changes["status"] = "active"
            store.update_user(result["id"], **changes)
            store.record_audit_event("account_activated" if purpose == "activation" else "password_reset_completed", result["id"],
                                     resource_type="user", resource_id=result["id"])
    if invalid:
        raise _error("invalid_activation_code" if purpose == "activation" else "invalid_reset_code", "一次性码无效或已过期")


@router.post("/activate", status_code=status.HTTP_204_NO_CONTENT)
def activate(payload: ActivationRequest, request: Request, store: PlatformStore = Depends(get_platform_store)) -> None:
    _consume_code(payload, request, store, "activation")


@router.post("/password-reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(payload: ActivationRequest, request: Request, store: PlatformStore = Depends(get_platform_store)) -> None:
    _consume_code(payload, request, store, "reset")


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginRequest, request: Request, response: Response, store: PlatformStore = Depends(get_platform_store)):
    username = payload.username.strip()
    _rate_gate(store, request, "login", username.casefold())
    row = store.get_user_by_username(username)
    valid = await verify_password_async(payload.password, row["password_hash"] if row else _decoy_hash())
    failure = None
    token = new_session_id()
    settings = request.app.state.settings
    ttl = settings.session_ttl_seconds
    with store.transaction():
        current = store.get_user(row["id"]) if row else None
        if not valid or current is None or current["password_hash"] != row["password_hash"] or current["authorization_version"] != row["authorization_version"]:
            failure = _error("login_failed", "账号或密码错误", 401)
        elif current["status"] != "active":
            failure = _error("account_inactive", "账号尚未激活或未通过审核", 403)
        elif payload.role and payload.role != current["role"]:
            failure = _error("role_mismatch", "账号角色不匹配", 403)
        if failure:
            store.record_audit_event("login_failed", resource_type="user", resource_id=current["id"] if current else None)
        else:
            store.create_session(token, current["id"], time.time() + ttl)
            store.clear_rate_limit("login:subject", username.casefold())
            store.record_audit_event("login_success", current["id"])
    if failure:
        raise failure
    response.set_cookie("sid", token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=ttl, path="/")
    return LoginOut(user=_user_out(current))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, store: PlatformStore = Depends(get_platform_store)) -> None:
    token = request.cookies.get("sid", "")
    with store.transaction():
        session = store.get_session(token)
        if token:
            store.delete_session(token)
        store.record_audit_event("logout", session["user_id"] if session else None)
    response.delete_cookie("sid", path="/", secure=request.app.state.settings.cookie_secure, httponly=True, samesite="lax")


@router.get("/me", response_model=AccountOut)
def me(user: User = Depends(get_platform_user)):
    return _user_out({"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role, "status": user.status})


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(payload: PasswordChangeRequest, user: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)) -> None:
    row = store.get_user(user.id)
    if row is None or not verify_password(payload.current_password, row["password_hash"]):
        raise _error("invalid_password", "当前密码错误", 401)
    if payload.current_password == payload.new_password:
        raise _error("same_password", "新密码不能与当前密码相同", 422)
    password_hash = hash_password(payload.new_password)
    with store.transaction():
        current = require_current_actor(store, user.id)
        if current["password_hash"] != row["password_hash"] or current["authorization_version"] != row["authorization_version"]:
            raise _error("account_changed", "账号已变更，请重新登录", 409)
        store.update_user(user.id, password_hash=password_hash)
        store.record_audit_event("password_changed", user.id, resource_type="user", resource_id=user.id)
