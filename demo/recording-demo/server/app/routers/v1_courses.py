"""Course material governance and separately authorized class administration."""

from contextlib import contextmanager
import sqlite3
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ..models import User
from ..platform_store import PlatformStore
from ..v1_deps import get_platform_store, get_platform_user, require_current_actor, require_platform_roles

router = APIRouter(prefix="/api/v1", tags=["courses", "classes"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CourseApplication(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=64)


class CourseReview(StrictRequest):
    approve: bool
    note: str = Field(default="", max_length=500)


class ApplicationOut(BaseModel):
    id: str
    applicant_id: str
    name: str
    term: str
    status: Literal["pending", "approved", "rejected"]
    course_id: str | None
    reviewed_by: str | None
    review_note: str
    created_at: float
    reviewed_at: float | None


class CourseOut(BaseModel):
    id: str
    name: str
    term: str
    teacher_id: str


class CourseUpdate(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=64)


class ClassCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=100)
    owner_teacher_id: str = Field(min_length=1, max_length=100)


class ClassUpdate(StrictRequest):
    name: str = Field(min_length=1, max_length=100)


class ClassOut(BaseModel):
    id: str
    course_id: str
    name: str
    owner_teacher_id: str | None


class CollaboratorGrant(StrictRequest):
    teacher_id: str = Field(min_length=1, max_length=100)
    access_level: Literal["read", "manage"]


class CollaboratorOut(BaseModel):
    teacher_id: str
    access_level: Literal["read", "manage"]


class MemberRequest(StrictRequest):
    user_id: str = Field(min_length=1, max_length=100)


class MemberOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    status: Literal["pending", "active", "disabled"]


class MemberTransfer(StrictRequest):
    target_class_id: str = Field(min_length=1, max_length=100)


class OwnerTransfer(StrictRequest):
    owner_teacher_id: str = Field(min_length=1, max_length=100)


class MembershipOut(BaseModel):
    course_id: str
    class_id: str
    user_id: str


def _out(model, row: dict):
    return model(**{field: row[field] for field in model.model_fields})


def _forbidden(message: str = "无权管理该资源") -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "forbidden", "message": message})


@contextmanager
def _mutation(store: PlatformStore, actor: User) -> Iterator[None]:
    try:
        with store.transaction():
            current = require_current_actor(store, actor.id, "teacher", "admin")
            if current["role"] != actor.role:
                raise _forbidden("账号权限已变更，请重新登录")
            yield
    except PermissionError as error:
        raise _forbidden() from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "记录不存在"}) from error
    except (ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(status_code=409, detail={"code": "conflict", "message": "记录已存在或当前状态不允许此操作"}) from error


def _class_check(store: PlatformStore, actor: User, class_id: str, *, manage: bool = False) -> dict:
    classroom = store.get_class(class_id)
    if classroom is None:
        raise HTTPException(status_code=404, detail={"code": "class_not_found", "message": "班级不存在"})
    if not store.can_access_class(actor.id, class_id, teaching=manage):
        raise _forbidden("无权访问该班级")
    return classroom


def _course_check(store: PlatformStore, actor: User, course_id: str, *, manage: bool = False) -> dict:
    course = store.get_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(actor.id, course_id, teaching=manage):
        raise _forbidden("无权访问该课程材料")
    return course


@router.post("/course-applications", status_code=status.HTTP_202_ACCEPTED, response_model=ApplicationOut)
def apply_course(payload: CourseApplication, actor: User = Depends(require_platform_roles("teacher")),
                 store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        application = store.create_course_application(actor.id, payload.name, payload.term)
        store.record_audit_event("course_application_created", actor.id, resource_type="course_application", resource_id=application["id"])
    return _out(ApplicationOut, application)


@router.get("/course-applications", response_model=list[ApplicationOut])
def list_course_applications(actor: User = Depends(require_platform_roles("teacher", "admin")),
                             store: PlatformStore = Depends(get_platform_store)):
    rows = store.list_course_applications(applicant_id=None if actor.role == "admin" else actor.id)
    return [_out(ApplicationOut, row) for row in rows]


@router.post("/course-applications/{application_id}/review", response_model=ApplicationOut)
def review_course(application_id: str, payload: CourseReview, actor: User = Depends(require_platform_roles("admin")),
                  store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        application = store.review_course_application(application_id, reviewer_id=actor.id, approve=payload.approve, note=payload.note)
        store.record_audit_event("course_application_approved" if payload.approve else "course_application_rejected", actor.id,
                                 resource_type="course_application", resource_id=application_id,
                                 payload={"course_id": application["course_id"]})
    return _out(ApplicationOut, application)


@router.get("/courses", response_model=list[CourseOut])
def list_courses(actor: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)):
    # Class teaching rights do not imply material editing rights; listings include
    # material readers and students who actually belong to the course.
    return [_out(CourseOut, row) for row in store.list_courses() if store.can_access_course(actor.id, row["id"])]


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_course(course_id: str, actor: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)):
    return _out(CourseOut, _course_check(store, actor, course_id))


@router.patch("/courses/{course_id}", response_model=CourseOut)
def update_course(course_id: str, payload: CourseUpdate, actor: User = Depends(require_platform_roles("teacher", "admin")),
                  store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        _course_check(store, actor, course_id, manage=True)
        course = store.update_course(course_id, name=payload.name, term=payload.term)
        store.record_audit_event("course_updated", actor.id, resource_type="course", resource_id=course_id)
    return _out(CourseOut, course)


@router.get("/courses/{course_id}/collaborators", response_model=list[CollaboratorOut])
def list_material_collaborators(course_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                                store: PlatformStore = Depends(get_platform_store)):
    _course_check(store, actor, course_id, manage=True)
    return [_out(CollaboratorOut, row) for row in store.list_course_collaborators(course_id)]


@router.put("/courses/{course_id}/collaborators", response_model=CollaboratorOut)
def grant_material_collaborator(course_id: str, payload: CollaboratorGrant,
                                actor: User = Depends(require_platform_roles("teacher", "admin")), store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        result = store.set_course_collaborator(course_id, payload.teacher_id, payload.access_level, granted_by=actor.id)
        store.record_audit_event("course_material_collaborator_granted", actor.id, resource_type="course", resource_id=course_id,
                                 payload={"teacher_id": payload.teacher_id, "access_level": payload.access_level})
    return _out(CollaboratorOut, result)


@router.delete("/courses/{course_id}/collaborators/{teacher_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_material_collaborator(course_id: str, teacher_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                                 store: PlatformStore = Depends(get_platform_store)) -> None:
    with _mutation(store, actor):
        if not store.remove_course_collaborator(course_id, teacher_id, removed_by=actor.id):
            raise KeyError("course collaborator not found")
        store.record_audit_event("course_material_collaborator_removed", actor.id, resource_type="course", resource_id=course_id,
                                 payload={"teacher_id": teacher_id})


@router.post("/courses/{course_id}/classes", status_code=status.HTTP_201_CREATED, response_model=ClassOut)
def create_class(course_id: str, payload: ClassCreate, actor: User = Depends(require_platform_roles("teacher", "admin")),
                 store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        _course_check(store, actor, course_id, manage=True)
        classroom = store.create_class(course_id, payload.name, owner_teacher_id=payload.owner_teacher_id)
        store.record_audit_event("class_created", actor.id, resource_type="class", resource_id=classroom["id"],
                                 payload={"course_id": course_id, "owner_teacher_id": payload.owner_teacher_id})
    return _out(ClassOut, classroom)


@router.get("/courses/{course_id}/classes", response_model=list[ClassOut])
def list_course_classes(course_id: str, actor: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)):
    # A course owner receives no additional class rows here.
    return [_out(ClassOut, row) for row in store.list_accessible_classes(actor.id, course_id=course_id)]


@router.get("/classes", response_model=list[ClassOut])
def list_classes(actor: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)):
    return [_out(ClassOut, row) for row in store.list_accessible_classes(actor.id)]


@router.get("/classes/{class_id}", response_model=ClassOut)
def get_class(class_id: str, actor: User = Depends(get_platform_user), store: PlatformStore = Depends(get_platform_store)):
    return _out(ClassOut, _class_check(store, actor, class_id))


@router.patch("/classes/{class_id}", response_model=ClassOut)
def update_class(class_id: str, payload: ClassUpdate, actor: User = Depends(require_platform_roles("teacher", "admin")),
                 store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        _class_check(store, actor, class_id, manage=True)
        result = store.update_class(class_id, name=payload.name)
        store.record_audit_event("class_updated", actor.id, resource_type="class", resource_id=class_id)
    return _out(ClassOut, result)


@router.get("/classes/{class_id}/members", response_model=list[MemberOut])
def list_members(class_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                 store: PlatformStore = Depends(get_platform_store)):
    _class_check(store, actor, class_id)
    result = []
    for membership in store.list_class_memberships(class_id):
        row = store.get_user(membership["user_id"])
        if row is not None:
            result.append(MemberOut(user_id=row["id"], username=row["username"], display_name=row["display_name"], status=row["status"]))
    return result


@router.post("/classes/{class_id}/members", status_code=status.HTTP_201_CREATED, response_model=MembershipOut)
def add_member(class_id: str, payload: MemberRequest, actor: User = Depends(require_platform_roles("teacher", "admin")),
               store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        classroom = _class_check(store, actor, class_id, manage=True)
        student = store.get_user(payload.user_id)
        if student is None or student["role"] != "student" or student["status"] == "disabled":
            raise ValueError("available student account required")
        # A teacher cannot import a student from an unrelated class just by ID.
        if actor.role != "admin" and not any(
            store.get_class_membership(row["id"], student["id"]) is not None
            for row in store.list_accessible_classes(actor.id)
            if store.can_access_class(actor.id, row["id"], teaching=True)
        ):
            raise _forbidden("只能添加已在本人可管理班级的学生；新账号请走学生创建流程")
        if store.get_course_membership(classroom["course_id"], student["id"]) is None:
            store.create_course_membership(classroom["course_id"], student["id"])
        result = store.create_class_membership(class_id, student["id"])
        store.record_audit_event("class_member_added", actor.id, resource_type="class", resource_id=class_id,
                                 payload={"user_id": student["id"]})
    return _out(MembershipOut, result)


@router.delete("/classes/{class_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(class_id: str, user_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                  store: PlatformStore = Depends(get_platform_store)) -> None:
    with _mutation(store, actor):
        _class_check(store, actor, class_id, manage=True)
        if not store.delete_class_membership(class_id, user_id):
            raise KeyError("class membership not found")
        store.record_audit_event("class_member_removed", actor.id, resource_type="class", resource_id=class_id,
                                 payload={"user_id": user_id})


@router.post("/classes/{class_id}/members/{user_id}/transfer", response_model=MembershipOut)
def transfer_member(class_id: str, user_id: str, payload: MemberTransfer,
                    actor: User = Depends(require_platform_roles("teacher", "admin")), store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        result = store.transfer_class_member(class_id, payload.target_class_id, user_id, actor_id=actor.id)
        store.record_audit_event("class_member_transferred", actor.id, resource_type="user", resource_id=user_id,
                                 payload={"source_class_id": class_id, "target_class_id": payload.target_class_id})
    return _out(MembershipOut, result)


@router.get("/classes/{class_id}/collaborators", response_model=list[CollaboratorOut])
def list_collaborators(class_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                       store: PlatformStore = Depends(get_platform_store)):
    _class_check(store, actor, class_id)
    return [_out(CollaboratorOut, row) for row in store.list_class_collaborators(class_id)]


@router.put("/classes/{class_id}/collaborators", response_model=CollaboratorOut)
def grant_collaborator(class_id: str, payload: CollaboratorGrant, actor: User = Depends(require_platform_roles("teacher", "admin")),
                       store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        result = store.set_class_collaborator(class_id, payload.teacher_id, payload.access_level, granted_by=actor.id)
        store.record_audit_event("class_collaborator_granted", actor.id, resource_type="class", resource_id=class_id,
                                 payload={"teacher_id": payload.teacher_id, "access_level": payload.access_level})
    return _out(CollaboratorOut, result)


@router.delete("/classes/{class_id}/collaborators/{teacher_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_collaborator(class_id: str, teacher_id: str, actor: User = Depends(require_platform_roles("teacher", "admin")),
                        store: PlatformStore = Depends(get_platform_store)) -> None:
    with _mutation(store, actor):
        if not store.remove_class_collaborator(class_id, teacher_id, removed_by=actor.id):
            raise KeyError("class collaborator not found")
        store.record_audit_event("class_collaborator_removed", actor.id, resource_type="class", resource_id=class_id,
                                 payload={"teacher_id": teacher_id})


@router.post("/classes/{class_id}/owner", response_model=ClassOut)
def transfer_owner(class_id: str, payload: OwnerTransfer, actor: User = Depends(require_platform_roles("teacher", "admin")),
                   store: PlatformStore = Depends(get_platform_store)):
    with _mutation(store, actor):
        result = store.transfer_class_owner(class_id, payload.owner_teacher_id, transferred_by=actor.id)
        store.record_audit_event("class_owner_transferred", actor.id, resource_type="class", resource_id=class_id,
                                 payload={"owner_teacher_id": payload.owner_teacher_id})
    return _out(ClassOut, result)
