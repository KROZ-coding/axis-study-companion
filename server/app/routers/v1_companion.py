"""Authenticated companion HTTP API; writes persist facts and never invoke AI.

The application owns the runtime and mounts this router. Response events are a
bounded JSON read of committed events; the shared job SSE route owns streaming,
including revalidation of the session and current learning policy at each poll.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..companion_store import (
    CompanionStore, Forbidden, IdempotencyConflict, InvalidTransition, NotFound, SAFE_GUIDANCE, StaleGeneration,
)
from ..models import User
from ..platform_store import PlatformStore
from ..v1_deps import get_platform_store, get_platform_user


def _error(status: int, code: str, message: str, *, retryable: bool = False) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "retryable": retryable})


class CompanionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
                response.headers.setdefault("Cache-Control", "no-store")
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                return response
            except HTTPException:
                raise
            except (NotFound, LookupError) as error:
                raise _error(404, "not_found", "资源不存在或无权访问") from error
            except (Forbidden, PermissionError) as error:
                raise _error(403, "forbidden", "当前账号无权执行此操作") from error
            except (IdempotencyConflict, InvalidTransition, StaleGeneration) as error:
                raise _error(409, "conflict", "请求与当前资源状态不一致，请刷新后重试") from error
            except ValueError as error:
                raise _error(422, "invalid_request", "请求参数不符合要求") from error
            except sqlite3.Error as error:
                raise _error(503, "storage_unavailable", "记录暂未可靠保存，请保留输入并重试", retryable=True) from error

        return handler


router = APIRouter(prefix="/api/v1", tags=["companion"], route_class=CompanionRoute)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestKey(StrictRequest):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^\S(?:[^\x00]{0,98}\S)?$")


class ConversationCreate(RequestKey):
    title: str = Field(min_length=1, max_length=200)
    course_id: str | None = Field(default=None, min_length=1, max_length=200)


class ConversationRename(StrictRequest):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(RequestKey):
    text: str = Field(min_length=1, max_length=20000)
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    question_version_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def question_requires_task(self):
        if self.question_version_id is not None and self.task_id is None:
            raise ValueError("question_version_id requires task_id")
        return self


class CollectionCreate(RequestKey):
    title: str = Field(min_length=1, max_length=200)


class CollectionUpdate(StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class CollectionItemCreate(RequestKey):
    kind: Literal["question", "message", "explanation"]
    message_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    question_version_id: str | None = Field(default=None, min_length=1, max_length=200)
    text: str = Field(default="", max_length=20000)


class NoteCreate(RequestKey):
    text: str = Field(min_length=1, max_length=20000)


class NoteUpdate(StrictRequest):
    text: str = Field(min_length=1, max_length=20000)
    expected_version: int = Field(ge=1, strict=True)


class MemoryCreate(RequestKey):
    text: str = Field(default="", max_length=20000)
    message_id: str | None = Field(default=None, min_length=1, max_length=200)


class MemoryArchive(StrictRequest):
    archived: bool = True


class ThreadCreate(RequestKey):
    task_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    question_version_id: str = Field(min_length=1, max_length=200)
    grading_revision: int = Field(ge=1, strict=True)
    title: str = Field(default="教师短评", min_length=1, max_length=200)


class ThreadMessageCreate(RequestKey):
    text: str = Field(default="", max_length=20000)
    preset_key: str | None = Field(default=None, min_length=1, max_length=100)


class RetentionUpdate(StrictRequest):
    retention_days: int = Field(ge=1, le=36500, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class HoldCreate(RequestKey):
    scope: Literal["all", "user", "conversation", "collection", "discussion_thread"]
    resource_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: float | None = None


class HoldRelease(StrictRequest):
    reason: str = Field(min_length=1, max_length=2000)


@dataclass
class Access:
    user: User
    platform: PlatformStore
    runtime: Any

    @property
    def companion(self) -> CompanionStore:
        return self.runtime.companion

    def course(self, course_id: str | None) -> None:
        if course_id is not None and not self.platform.can_access_course(self.user.id, course_id):
            raise NotFound("course not found")

    def conversation(self, conversation_id: str) -> dict:
        row = self.companion.get_conversation(conversation_id, self.user.id)
        self.course(row["course_id"])
        return row

    def policy(self, user_id: str, *, task_id: str | None = None, question_version_id: str | None = None) -> Mapping[str, Any]:
        """Only the learning public interface decides visibility, never request flags."""
        if user_id != self.user.id:
            return {"allow_assistant_content": False, "allow_generation": False}
        method = getattr(self.runtime.learning, "get_answer_policy", None)
        if not callable(method):
            return {}
        try:
            if task_id is not None:
                self.task(task_id, question_version_id=question_version_id)
            value = method(user_id, task_id=task_id, question_version_id=question_version_id)
            if not isinstance(value, Mapping):
                return {}
            # An unscoped message can contain any active question. Its policy must
            # be learning's all-active fallback, never a client-selected task.
            return value
        except Exception:
            return {}

    def task(self, task_id: str, *, question_version_id: str | None = None) -> dict[str, Any]:
        learning = self.runtime.learning
        scope = learning.get_task_scope(task_id, self.user.id)
        if scope.get("student_id") != self.user.id:
            raise NotFound("task not assigned to actor")
        self.course(scope.get("course_id"))
        if scope.get("class_id") and not self.platform.can_access_class(self.user.id, scope["class_id"]):
            raise NotFound("task class is inaccessible")
        if question_version_id is not None:
            self.question(task_id, question_version_id, scope=scope)
        return scope

    @staticmethod
    def question_matches(question: Mapping[str, Any], reference: str) -> bool:
        if question.get("question_version_id") == reference:
            return True
        question_id, version = question.get("id", question.get("question_id")), question.get("version")
        return question_id is not None and version is not None and reference in {f"{question_id}:{version}", f"{question_id}@{version}"}

    def question(self, task_id: str, question_version_id: str, *, scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if scope is None:
            self.task(task_id)
        task = self.runtime.learning.get_task(task_id, self.user.id)
        question = next((row for row in task.get("questions", []) if self.question_matches(row, question_version_id)), None)
        if question is None:
            raise NotFound("question version not found in task")
        return dict(question)

    def question_projection(self, task_id: str, question_version_id: str) -> dict[str, Any]:
        question = self.question(task_id, question_version_id)
        # A field whitelist also protects an older learning implementation that
        # included answer keys on a retry merely because an earlier attempt graded.
        visible = {key: question[key] for key in ("id", "question_id", "question_version_id", "version", "type", "stem", "options", "points") if key in question}
        policy = self.policy(self.user.id, task_id=task_id, question_version_id=question_version_id)
        if policy.get("allow_assistant_content") is True:
            visible.update({key: question[key] for key in ("answer", "explanation", "rubric", "reference_answer") if key in question})
        return visible

    def message(self, message_id: str) -> dict[str, Any]:
        result = self.companion.get_message(message_id, self.user.id, answer_policy=self.policy)
        self.conversation(result["conversation_id"])
        if result.get("task_id"):
            self.task(result["task_id"], question_version_id=result.get("question_version_id"))
        return result

    def admin(self) -> None:
        if self.user.role != "admin":
            raise Forbidden("administrator role required")

    def discussion_scope(self, task_id: str, attempt_id: str, question_version_id: str,
                         grading_revision: int, *, student_id: str | None = None) -> dict[str, Any]:
        method = getattr(self.runtime.learning, "get_discussion_scope", None)
        if not callable(method):
            raise _error(503, "learning_scope_unavailable", "暂时无法核验讨论绑定范围", retryable=True)
        scope = method(self.user.id, task_id=task_id, attempt_id=attempt_id,
                       question_version_id=question_version_id, grading_revision=grading_revision)
        if not isinstance(scope, Mapping):
            raise NotFound("discussion scope not found")
        result = dict(scope)
        result["student_id"] = scope.get("student_id", scope.get("user_id"))
        if not all(result.get(key) for key in ("student_id", "course_id", "class_id")):
            raise NotFound("incomplete discussion scope")
        for key, expected in (("task_id", task_id), ("attempt_id", attempt_id),
                              ("question_version_id", question_version_id), ("grading_revision", grading_revision)):
            if key in result and result[key] != expected:
                raise NotFound("discussion binding changed")
        if student_id is not None and result["student_id"] != student_id:
            raise NotFound("discussion student changed")
        classroom = self.platform.get_class(result["class_id"])
        if classroom is None or classroom["course_id"] != result["course_id"]:
            raise NotFound("discussion class not found")
        if self.platform.get_class_membership(result["class_id"], result["student_id"]) is None:
            raise NotFound("student is outside discussion class")
        if self.user.role == "student":
            if result["student_id"] != self.user.id or not self.platform.can_access_class(self.user.id, result["class_id"]):
                raise NotFound("discussion not found")
        elif self.user.role == "teacher":
            if not self.platform.can_access_class(self.user.id, result["class_id"]):
                raise NotFound("discussion not found")
        else:
            # Full student conversations/threads require an explicit audited grant;
            # this version deliberately provides no default administrator grant.
            raise Forbidden("explicit student-content access grant required")
        return result

    def thread(self, thread_id: str, *, write: bool = False) -> tuple[dict, dict, bool]:
        thread = self.companion.get_discussion_thread(thread_id)
        scope = self.discussion_scope(thread["task_id"], thread["attempt_id"], thread["question_version_id"],
                                      thread["grading_revision"], student_id=thread["student_id"])
        if scope["course_id"] != thread["course_id"] or scope["class_id"] != thread["class_id"]:
            raise NotFound("discussion scope changed")
        manage = self.user.role == "teacher" and self.platform.can_access_class(self.user.id, thread["class_id"], teaching=True)
        if write and self.user.role == "teacher" and not manage:
            raise Forbidden("class management permission required")
        return thread, scope, manage


def get_access(request: Request, user: User = Depends(get_platform_user),
               platform: PlatformStore = Depends(get_platform_store)) -> Access:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise _error(503, "runtime_unavailable", "服务暂未就绪", retryable=True)
    return Access(user, platform, runtime)


@router.post("/companion/conversations", status_code=201)
def create_conversation(payload: ConversationCreate, access: Access = Depends(get_access)):
    access.course(payload.course_id)
    return access.companion.create_conversation(access.user.id, payload.title, course_id=payload.course_id, request_id=payload.request_id)


@router.get("/companion/conversations")
def list_conversations(archived: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                       access: Access = Depends(get_access)):
    rows = access.companion.list_conversations(access.user.id, archived=archived, limit=limit, offset=offset)
    return [row for row in rows if row["course_id"] is None or access.platform.can_access_course(access.user.id, row["course_id"])]


@router.get("/companion/conversations/{conversation_id}")
def get_conversation(conversation_id: str, access: Access = Depends(get_access)):
    return access.conversation(conversation_id)


@router.patch("/companion/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRename, access: Access = Depends(get_access)):
    access.conversation(conversation_id)
    return access.companion.rename_conversation(conversation_id, access.user.id, payload.title)


@router.post("/companion/conversations/{conversation_id}/archive")
def archive_conversation(conversation_id: str, access: Access = Depends(get_access)):
    access.conversation(conversation_id)
    return access.companion.archive_conversation(conversation_id, access.user.id)


@router.post("/companion/conversations/{conversation_id}/restore")
def restore_conversation(conversation_id: str, access: Access = Depends(get_access)):
    access.conversation(conversation_id)
    return access.companion.restore_conversation(conversation_id, access.user.id)


@router.post("/companion/conversations/{conversation_id}/messages", status_code=202)
def post_message(conversation_id: str, payload: MessageCreate, access: Access = Depends(get_access)):
    conversation = access.conversation(conversation_id)
    if payload.task_id:
        scope = access.task(payload.task_id, question_version_id=payload.question_version_id)
        if conversation["course_id"] is not None and scope.get("course_id") != conversation["course_id"]:
            raise NotFound("task is outside conversation course")
    return access.companion.post_message(conversation_id, access.user.id, payload.text, request_id=payload.request_id,
                                         task_id=payload.task_id, question_version_id=payload.question_version_id)


@router.get("/companion/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str, after: str | None = None, limit: int = Query(100, ge=1, le=500),
                  access: Access = Depends(get_access)):
    access.conversation(conversation_id)
    return access.companion.list_messages(conversation_id, access.user.id, after=after, limit=limit, answer_policy=access.policy)


@router.get("/companion/responses/{response_id}")
def get_response(response_id: str, access: Access = Depends(get_access)):
    response = access.companion.get_response(response_id, access.user.id, answer_policy=access.policy)
    access.conversation(response["conversation_id"])
    return response


@router.get("/companion/responses/{response_id}/events")
def response_events(response_id: str, after: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=1000),
                    access: Access = Depends(get_access)):
    response = access.companion.get_response(response_id, access.user.id, answer_policy=access.policy)
    access.conversation(response["conversation_id"])
    return access.companion.list_response_events(response_id, access.user.id, after=after, limit=limit, answer_policy=access.policy)


@router.post("/companion/collections", status_code=201)
def create_collection(payload: CollectionCreate, access: Access = Depends(get_access)):
    return access.companion.create_collection(access.user.id, payload.title, request_id=payload.request_id)


@router.get("/companion/collections")
def list_collections(archived: bool = False, access: Access = Depends(get_access)):
    return access.companion.list_collections(access.user.id, archived=archived)


@router.get("/companion/collections/{collection_id}")
def get_collection(collection_id: str, access: Access = Depends(get_access)):
    return access.companion.get_collection(collection_id, access.user.id)


@router.patch("/companion/collections/{collection_id}")
def update_collection(collection_id: str, payload: CollectionUpdate, access: Access = Depends(get_access)):
    return access.companion.update_collection(collection_id, access.user.id, title=payload.title, archived=payload.archived)


def _collection_projection(item: dict, access: Access) -> dict:
    if item["kind"] == "question":
        try:
            item["question"] = access.question_projection(item["task_id"], item["question_version_id"])
        except (LookupError, PermissionError):
            item.update(question=None, redacted=True, notice="该题当前不可访问。")
    elif item["kind"] == "message":
        try:
            item["message"] = access.message(item["message_id"])
            item["redacted"] = item["message"]["redacted"]
        except (LookupError, PermissionError):
            item.update(message=None, redacted=True, notice="该消息当前不可访问。")
    return item


@router.post("/companion/collections/{collection_id}/items", status_code=201)
def add_collection_item(collection_id: str, payload: CollectionItemCreate, access: Access = Depends(get_access)):
    if payload.kind == "question":
        if not payload.task_id or not payload.question_version_id:
            raise _error(422, "invalid_request", "收藏题目需要任务和题目版本")
        access.task(payload.task_id, question_version_id=payload.question_version_id)
    elif payload.kind == "message":
        if not payload.message_id:
            raise _error(422, "invalid_request", "收藏讲解需要本人消息引用")
        access.message(payload.message_id)
    item = access.companion.add_collection_item(collection_id, access.user.id, payload.kind, request_id=payload.request_id,
                                                message_id=payload.message_id, task_id=payload.task_id,
                                                question_version_id=payload.question_version_id, text=payload.text,
                                                answer_policy=access.policy)
    return _collection_projection(item, access)


@router.get("/companion/collections/{collection_id}/items")
def list_collection_items(collection_id: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                          access: Access = Depends(get_access)):
    items = access.companion.list_collection_items(collection_id, access.user.id, limit=limit, offset=offset, answer_policy=access.policy)
    return [_collection_projection(item, access) for item in items]


@router.post("/companion/collection-items/{collection_item_id}/notes", status_code=201)
def create_note(collection_item_id: str, payload: NoteCreate, access: Access = Depends(get_access)):
    # Notes remain plain strings in a JSON document; no HTML/Markdown is evaluated.
    return access.companion.create_note(collection_item_id, access.user.id, payload.text, request_id=payload.request_id)


@router.get("/companion/collection-items/{collection_item_id}/notes")
def list_notes(collection_item_id: str, access: Access = Depends(get_access)):
    return access.companion.list_notes(collection_item_id, access.user.id)


@router.patch("/companion/notes/{note_id}")
def update_note(note_id: str, payload: NoteUpdate, access: Access = Depends(get_access)):
    return access.companion.update_note(note_id, access.user.id, payload.text, expected_version=payload.expected_version)


@router.post("/companion/memories", status_code=201)
def create_memory(payload: MemoryCreate, access: Access = Depends(get_access)):
    if payload.message_id is not None:
        access.message(payload.message_id)
    return access.companion.create_memory(access.user.id, payload.text, request_id=payload.request_id,
                                          message_id=payload.message_id, answer_policy=access.policy)


@router.get("/companion/memories")
def list_memories(archived: bool = False, access: Access = Depends(get_access)):
    items = access.companion.list_memories(access.user.id, archived=archived, answer_policy=access.policy)
    for item in items:
        if item.get("message_id"):
            try:
                item["message"] = access.message(item["message_id"])
            except (LookupError, PermissionError):
                item.update(text="", message=None, redacted=True, notice="原消息当前不可访问。")
    return items


@router.patch("/companion/memories/{memory_id}")
def archive_memory(memory_id: str, payload: MemoryArchive, access: Access = Depends(get_access)):
    return access.companion.archive_memory(memory_id, access.user.id, archived=payload.archived)


@router.get("/companion/discussion-presets")
def discussion_presets(access: Access = Depends(get_access)):
    if access.user.role not in {"teacher", "student"}:
        raise Forbidden("teacher or student role required")
    return [{"key": key, "text": text} for key, text in SAFE_GUIDANCE.items() if key != "response_failed"]


@router.post("/companion/discussion-threads", status_code=201)
def create_discussion_thread(payload: ThreadCreate, access: Access = Depends(get_access)):
    if access.user.role != "teacher":
        raise Forbidden("only teachers may create a feedback thread")
    scope = access.discussion_scope(payload.task_id, payload.attempt_id, payload.question_version_id, payload.grading_revision)
    if not access.platform.can_access_class(access.user.id, scope["class_id"], teaching=True):
        raise Forbidden("class management permission required")
    return access.companion.create_discussion_thread(scope["student_id"], scope["course_id"], scope["class_id"], payload.task_id,
                                                      payload.attempt_id, payload.question_version_id, payload.grading_revision,
                                                      created_by=access.user.id, request_id=payload.request_id, title=payload.title)


@router.get("/companion/discussion-threads")
def list_discussion_threads(class_id: str | None = None, task_id: str | None = None,
                            limit: int = Query(100, ge=1, le=500), access: Access = Depends(get_access)):
    if access.user.role == "student":
        if class_id is not None and not access.platform.can_access_class(access.user.id, class_id):
            raise NotFound("class not found")
        rows = access.companion.list_discussion_threads(student_id=access.user.id, class_id=class_id, task_id=task_id, limit=limit)
    elif access.user.role == "teacher":
        if class_id is None:
            raise _error(422, "class_required", "请指定讨论所属班级")
        if not access.platform.can_access_class(access.user.id, class_id):
            raise NotFound("class not found")
        rows = access.companion.list_discussion_threads(class_id=class_id, task_id=task_id, limit=limit)
    else:
        raise Forbidden("explicit student-content access grant required")
    result = []
    for row in rows:
        try:
            _, scope, _ = access.thread(row["id"])
            if access.user.role == "student" and scope.get("results_released") is not True:
                row["title"] = "教师短评"
            result.append(row)
        except (LookupError, PermissionError):
            continue
    return result


@router.get("/companion/discussion-threads/{thread_id}")
def get_discussion_thread(thread_id: str, access: Access = Depends(get_access)):
    thread, scope, manage = access.thread(thread_id)
    if access.user.role == "student" and scope.get("results_released") is not True:
        thread["title"] = "教师短评"
    return dict(thread, can_reply=access.user.role == "student" or manage,
                messages=access.companion.list_discussion_messages(thread_id, access.user.id, role=access.user.role,
                                                                    can_read=True, results_released=scope.get("results_released") is True))


@router.get("/companion/discussion-threads/{thread_id}/messages")
def list_discussion_messages(thread_id: str, access: Access = Depends(get_access)):
    _, scope, _ = access.thread(thread_id)
    return access.companion.list_discussion_messages(thread_id, access.user.id, role=access.user.role, can_read=True,
                                                     results_released=scope.get("results_released") is True)


@router.post("/companion/discussion-threads/{thread_id}/messages", status_code=201)
def post_discussion_message(thread_id: str, payload: ThreadMessageCreate, access: Access = Depends(get_access)):
    _, scope, manage = access.thread(thread_id, write=True)
    result = access.companion.post_discussion_message(thread_id, access.user.id, payload.text, author_role=access.user.role,
                                                       request_id=payload.request_id, results_released=scope.get("results_released") is True,
                                                       preset_key=payload.preset_key, can_manage=manage)
    # A teacher's posted text is returned to that teacher only. Student reads
    # dynamically hide every teacher free-text message while results are closed.
    return result


def _notification_projection(item: dict, access: Access) -> dict:
    try:
        kind, resource_id = item["resource_type"], item["resource_id"]
        if kind == "task":
            access.task(resource_id)
        elif kind == "response":
            response = access.companion.get_response(resource_id, access.user.id, answer_policy=access.policy)
            access.conversation(response["conversation_id"])
        elif kind in {"discussion_thread", "thread"}:
            access.thread(resource_id)
        elif kind in {"attempt", "grade", "feedback", "result"}:
            task_id = item.get("payload", {}).get("task_id")
            if task_id is None:
                raise NotFound("notification does not convey resource access")
            access.task(task_id)
        else:
            raise NotFound("notification does not convey resource access")
    except (LookupError, PermissionError):
        item.update(payload={}, redacted=True, notice="关联资源当前不可访问；通知已读不会授予访问权限。")
    return item


@router.get("/me/notifications")
@router.get("/companion/notifications", include_in_schema=False)
def list_notifications(unread_only: bool = False, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                       access: Access = Depends(get_access)):
    items = access.companion.list_notifications(access.user.id, unread_only=unread_only, limit=limit, offset=offset,
                                                answer_policy=access.policy)
    return [_notification_projection(item, access) for item in items]


@router.post("/me/notifications/{notification_id}/read")
@router.post("/companion/notifications/{notification_id}/read", include_in_schema=False)
def mark_notification_read(notification_id: str, access: Access = Depends(get_access)):
    item = access.companion.mark_notification_read(notification_id, access.user.id, answer_policy=access.policy)
    return _notification_projection(item, access)


@router.get("/companion/admin/retention-policies")
def list_retention_policies(access: Access = Depends(get_access)):
    access.admin()
    return access.companion.list_retention_policies()


@router.put("/companion/admin/retention-policies/{resource_type}")
def set_retention_policy(resource_type: str, payload: RetentionUpdate, access: Access = Depends(get_access)):
    access.admin()
    return access.companion.set_retention_policy(resource_type, payload.retention_days, actor_id=access.user.id, reason=payload.reason)


@router.post("/companion/admin/legal-holds", status_code=201)
def create_legal_hold(payload: HoldCreate, access: Access = Depends(get_access)):
    access.admin()
    if payload.scope == "user" and access.platform.get_user(payload.resource_id or "") is None:
        raise NotFound("hold account not found")
    return access.companion.create_legal_hold(payload.scope, payload.resource_id, created_by=access.user.id, reason=payload.reason,
                                              request_id=payload.request_id, expires_at=payload.expires_at)


@router.get("/companion/admin/legal-holds")
def list_legal_holds(active_only: bool = True, access: Access = Depends(get_access)):
    access.admin()
    return access.companion.list_legal_holds(active_only=active_only)


@router.post("/companion/admin/legal-holds/{hold_id}/release")
def release_legal_hold(hold_id: str, payload: HoldRelease, access: Access = Depends(get_access)):
    access.admin()
    return access.companion.release_legal_hold(hold_id, actor_id=access.user.id, reason=payload.reason)


@router.get("/companion/admin/cleanup-preview")
def cleanup_preview(limit: int = Query(100, ge=1, le=1000), access: Access = Depends(get_access)):
    access.admin()
    return access.companion.preview_cleanup(limit=limit)
