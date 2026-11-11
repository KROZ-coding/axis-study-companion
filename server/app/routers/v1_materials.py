"""Authenticated material/curriculum endpoints. No GET handler invokes a model."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..content_store import ContentConflict, ContentStore
from ..models import User
from ..platform_store import PlatformStore
from ..services.material_parser import MaterialParseError, ParseLimits, parse_material, preview_grade_mapping
from ..services.question_bank import QuestionBank
from ..v1_deps import get_platform_store, get_platform_user, require_platform_roles

router = APIRouter(prefix="/api/v1", tags=["materials"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None,min_length=1,max_length=240)
    license_code: Literal["own","permission","cc0","cc_by","cc_by_sa","unknown","restricted"] | None = None
    license_note: str | None = Field(default=None,max_length=2000)
    rights_confirmed: StrictBool | None = None
    allow_generation: StrictBool | None = None
    allow_retrieval: StrictBool | None = None
    allow_student_view: StrictBool | None = None
    external_processing_allowed: StrictBool | None = None


class Confirmation(StrictModel):
    expected_revision: int = Field(ge=1)
    block_ids: list[str] = Field(min_length=1,max_length=5000)
    public_block_ids: list[str] = Field(default_factory=list,max_length=5000)
    public_content_attested: StrictBool = False
    note: str = Field(default="",max_length=2000)


class IssueResolution(StrictModel):
    expected_revision: int = Field(ge=1)
    resolution: str = Field(min_length=1,max_length=2000)
    replacement_text: str | None = Field(default=None,min_length=1,max_length=100000)


class ParseJobRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class GenerationRequest(StrictModel):
    source_block_ids: list[str] = Field(min_length=1,max_length=8)
    class_id: str | None = Field(default=None,min_length=1,max_length=100)
    count: int = Field(default=1,ge=1,le=10)
    instruction: str = Field(default="",max_length=2000)
    external_processing_allowed: StrictBool = False


class QuestionEdit(GenerationRequest):
    expected_version: int = Field(ge=1)
    question: dict[str,Any]


class GradePreview(StrictModel):
    block_id: str = Field(min_length=1,max_length=100)
    mapping: dict[str,Any]
    existing: list[dict[str,Any]] = Field(default_factory=list,max_length=20000)


class CurriculumCreate(StrictModel):
    text: str = Field(min_length=1,max_length=10000)
    source_block_ids: list[str] = Field(min_length=1,max_length=100)


class KnowledgeCreate(StrictModel):
    code: str = Field(min_length=1,max_length=100)
    title: str = Field(min_length=1,max_length=240)
    description: str = Field(default="",max_length=10000)
    source_block_ids: list[str] = Field(min_length=1,max_length=100)
    prerequisite_ids: list[str] = Field(default_factory=list,max_length=100)


class PrerequisiteUpdate(StrictModel):
    prerequisite_ids: list[str] = Field(max_length=100)


def get_content_store(request: Request) -> ContentStore:
    return request.app.state.runtime.content


def _bank(request: Request, content: ContentStore) -> QuestionBank:
    return QuestionBank(content,request.app.state.runtime.learning)


@contextmanager
def _content_errors():
    try:
        yield
    except HTTPException:
        raise
    except KeyError as error:
        raise HTTPException(404,detail={"code":"content_not_found","message":"内容不存在"}) from error
    except PermissionError as error:
        raise HTTPException(403,detail={"code":"content_forbidden","message":str(error)}) from error
    except ContentConflict as error:
        raise HTTPException(409,detail={"code":"content_conflict","message":str(error)}) from error
    except MaterialParseError as error:
        raise HTTPException(413 if error.code=="file_too_large" else 422,detail={"code":error.code,"message":str(error)}) from error
    except (ValueError,TypeError) as error:
        raise HTTPException(422,detail={"code":"invalid_content","message":str(error)}) from error


def _scope(platform: PlatformStore, actor: User, course_id: str, class_id: str | None = None, *, teaching: bool = False) -> dict[str,Any]:
    course = platform.get_course(course_id)
    if course is None or not platform.can_access_course(actor.id,course_id,teaching=teaching):
        raise HTTPException(404,detail={"code":"course_not_found","message":"课程不存在或无权访问"})
    if class_id is not None:
        classroom = platform.get_class(class_id)
        if classroom is None or classroom["course_id"]!=course_id or not platform.can_access_class(actor.id,class_id,teaching=teaching):
            raise HTTPException(404,detail={"code":"class_not_found","message":"班级不存在或无权访问"})
    return course


def _material(content: ContentStore, platform: PlatformStore, actor: User, file_id: str, *, write: bool = False) -> tuple[dict[str,Any],dict[str,Any]]:
    material = content.get_material(file_id)
    if material is None:
        raise HTTPException(404,detail={"code":"material_not_found","message":"材料不存在"})
    course = _scope(platform,actor,material["course_id"],material["class_id"],teaching=write)
    # Uploaders manage only their own unconfirmed drafts. Course owners may inspect shared drafts for confirmation.
    is_course_owner = actor.role=="admin" or course["teacher_id"]==actor.id
    if actor.role=="student":
        if (material["visibility"]!="shared" or material["status"] not in {"partial","confirmed"}
                or not material["allow_student_view"] or not material["rights_confirmed"]
                or not content.list_blocks(file_id,student=True)):
            raise HTTPException(404,detail={"code":"material_not_found","message":"材料不存在"})
    elif material["owner_id"]!=actor.id:
        if material["visibility"]!="shared" or (material["status"] not in {"partial","confirmed"} and not is_course_owner):
            raise HTTPException(404,detail={"code":"material_not_found","message":"材料不存在"})
    return material,course


def _draft_reader(actor: User, material: dict[str,Any], course: dict[str,Any]) -> bool:
    return actor.role in {"teacher","admin"} and (
        material["owner_id"]==actor.id or actor.role=="admin" or course["teacher_id"]==actor.id)


def _public_file(material: dict[str,Any], *, student: bool = False) -> dict[str,Any]:
    if student:
        keys = {"id","course_id","class_id","title","format","status","revision","license_code","license_note"}
    else:
        keys = set(material)-{"storage_key"}
    return {key:material[key] for key in keys if key in material}


def _public_block(block: dict[str,Any], *, student: bool = False) -> dict[str,Any]:
    keys = {"id","file_id","anchor","kind","text","data","status"} if student else set(block)
    return {key:block[key] for key in keys if key in block}


def _assert_source_access(content: ContentStore, platform: PlatformStore, actor: User, course_id: str, block_ids: list[str], *, purpose: str, class_id: str | None = None) -> None:
    sources = content.confirmed_sources(course_id,block_ids,purpose=purpose,class_id=class_id)
    for file_id in {s["file_id"] for s in sources}:
        _material(content,platform,actor,file_id)


@router.post("/courses/{course_id}/materials",status_code=status.HTTP_201_CREATED)
async def upload_material(course_id: str, request: Request,
                          file: Annotated[UploadFile,File()],
                          class_id: Annotated[str | None,Form(max_length=100)] = None,
                          title: Annotated[str,Form(max_length=240)] = "",
                          visibility: Annotated[Literal["private","shared"],Form()] = "private",
                          license_code: Annotated[Literal["own","permission","cc0","cc_by","cc_by_sa","unknown","restricted"],Form()] = "unknown",
                          license_note: Annotated[str,Form(max_length=2000)] = "",
                          rights_confirmed: Annotated[Literal["true","false"],Form()] = "false",
                          allow_generation: Annotated[Literal["true","false"],Form()] = "false",
                          allow_retrieval: Annotated[Literal["true","false"],Form()] = "false",
                          allow_student_view: Annotated[Literal["true","false"],Form()] = "false",
                          external_processing_allowed: Annotated[Literal["true","false"],Form()] = "false",
                          actor: User = Depends(require_platform_roles("teacher","admin")),
                          platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,class_id,teaching=True)
    maximum, chunks, length = ParseLimits().max_bytes, [], 0
    try:
        while True:
            chunk = await file.read(min(65536,maximum-length+1))
            if not chunk:
                break
            length += len(chunk)
            if length>maximum:
                raise HTTPException(413,detail={"code":"file_too_large","message":"文件超过上传大小限制"})
            chunks.append(chunk)
        data = b"".join(chunks)
        with _content_errors():
            parsed = parse_material(file.filename or "",data)
            material = content.import_material(course_id=course_id,class_id=class_id,owner_id=actor.id,
                filename=file.filename or "",title=title,content=data,parsed=parsed,visibility=visibility,
                license_code=license_code,license_note=license_note,rights_confirmed=rights_confirmed=="true",
                allow_generation=allow_generation=="true",allow_retrieval=allow_retrieval=="true",
                allow_student_view=allow_student_view=="true",external_processing_allowed=external_processing_allowed=="true")
        return {"material":_public_file(material),"blocks":content.list_blocks(material["id"]),"issues":content.list_issues(material["id"])}
    finally:
        await file.close()


@router.get("/courses/{course_id}/materials")
def list_materials(course_id: str, actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),
                   content: ContentStore = Depends(get_content_store),limit: int = Query(default=100,ge=1,le=200),offset: int = Query(default=0,ge=0)):
    _scope(platform,actor,course_id)
    result = []
    for row in content.list_materials(course_id,limit=limit,offset=offset):
        try:
            _material(content,platform,actor,row["id"])
        except HTTPException as error:
            if error.status_code==404:
                continue
            raise
        result.append(_public_file(row,student=actor.role=="student"))
    return {"items":result,"limit":limit,"offset":offset}


@router.get("/materials/{file_id}")
def get_material(file_id: str,actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,course = _material(content,platform,actor,file_id)
    student = actor.role=="student"
    draft_reader = _draft_reader(actor,material,course)
    result = {"material":_public_file(material,student=student),"blocks":[_public_block(block,student=student) for block in content.list_blocks(file_id,confirmed_only=not draft_reader,student=student)]}
    if draft_reader:
        result["issues"] = content.list_issues(file_id)
    return result


@router.get("/materials/{file_id}/blocks")
def list_blocks(file_id: str,actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,course = _material(content,platform,actor,file_id)
    return {"items":[_public_block(block,student=actor.role=="student") for block in content.list_blocks(file_id,confirmed_only=not _draft_reader(actor,material,course),student=actor.role=="student")]}


@router.get("/materials/{file_id}/issues")
def list_issues(file_id: str,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,course = _material(content,platform,actor,file_id)
    if not _draft_reader(actor,material,course):
        raise HTTPException(404,detail={"code":"draft_not_found","message":"草稿疑点不可见"})
    return {"items":content.list_issues(file_id)}


@router.patch("/materials/{file_id}")
def update_material(file_id: str,payload: DraftUpdate,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _material(content,platform,actor,file_id,write=True)
    with _content_errors():
        row = content.update_draft(file_id,actor_id=actor.id,**payload.model_dump(exclude_none=True))
    return {"material":_public_file(row)}


@router.delete("/materials/{file_id}",status_code=status.HTTP_204_NO_CONTENT)
def delete_material(file_id: str,expected_revision: int = Query(ge=1),actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _material(content,platform,actor,file_id,write=True)
    with _content_errors():
        content.delete_draft(file_id,actor_id=actor.id,expected_revision=expected_revision)
    return Response(status_code=204)


@router.post("/materials/{file_id}/confirm")
def confirm_material(file_id: str,payload: Confirmation,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,course = _material(content,platform,actor,file_id,write=True)
    owner_confirmation = actor.role=="admin" or course["teacher_id"]==actor.id
    if material["visibility"]=="shared" and not owner_confirmation:
        raise HTTPException(403,detail={"code":"course_owner_required","message":"共享材料须由课程负责人确认"})
    with _content_errors():
        result = content.confirm_blocks(file_id,actor_id=actor.id,course_owner_confirmation=owner_confirmation,**payload.model_dump())
    result["material"] = _public_file(result["material"])
    return result


@router.post("/materials/{file_id}/issues/{issue_id}/resolve")
def resolve_material_issue(file_id: str,issue_id: str,payload: IssueResolution,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _material(content,platform,actor,file_id,write=True)
    with _content_errors():
        row = content.resolve_issue(file_id,issue_id,actor_id=actor.id,**payload.model_dump())
    return {"material":_public_file(row),"issues":content.list_issues(file_id)}


@router.post("/materials/{file_id}/parse-jobs",status_code=status.HTTP_202_ACCEPTED)
def create_parse_job(file_id: str,payload: ParseJobRequest,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,_ = _material(content,platform,actor,file_id,write=True)
    if material["owner_id"]!=actor.id:
        raise HTTPException(403,detail={"code":"draft_owner_required","message":"仅上传者可管理自己的草稿"})
    if material["revision"]!=payload.expected_revision or any(b["status"]=="confirmed" for b in content.list_blocks(file_id)):
        raise HTTPException(409,detail={"code":"material_changed","message":"材料已确认或版本已变化"})
    job = content.create_job("material_parse",course_id=material["course_id"],class_id=material["class_id"],owner_id=actor.id,
                             payload={"file_id":file_id,"material_revision":material["revision"]})
    return {"job":job}


@router.post("/materials/{file_id}/grade-mapping-preview")
def grade_mapping(file_id: str,payload: GradePreview,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    material,course = _material(content,platform,actor,file_id,write=True)
    if not _draft_reader(actor,material,course):
        raise HTTPException(404,detail={"code":"draft_not_found","message":"草稿表格不可见"})
    block = next((b for b in content.list_blocks(file_id) if b["id"]==payload.block_id),None)
    if block is None or block["kind"]!="table":
        raise HTTPException(404,detail={"code":"table_not_found","message":"结构化成绩表不存在"})
    with _content_errors():
        return preview_grade_mapping(block["data"]["rows"],payload.mapping,existing=payload.existing)


@router.get("/courses/{course_id}/sources/search")
def search_sources(course_id: str,q: str = Query(min_length=1,max_length=200),class_id: str | None = Query(default=None,max_length=100),
                   limit: int = Query(default=20,ge=1,le=100),actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,class_id)
    with _content_errors():
        blocks = content.search_confirmed(course_id,q,class_id=class_id,student=actor.role=="student",limit=limit)
    result = []
    for block in blocks:
        try:
            _material(content,platform,actor,block["file_id"])
        except HTTPException as error:
            if error.status_code==404:
                continue
            raise
        result.append(_public_block(block,student=actor.role=="student"))
    return {"items":result}


@router.post("/courses/{course_id}/question-jobs",status_code=status.HTTP_202_ACCEPTED)
def generate_questions(course_id: str,payload: GenerationRequest,request: Request,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,payload.class_id,teaching=True)
    with _content_errors():
        _assert_source_access(content,platform,actor,course_id,payload.source_block_ids,purpose="generation",class_id=payload.class_id)
        job = _bank(request,content).create_generation(course_id=course_id,owner_id=actor.id,**payload.model_dump())
    return {"job":job}


@router.post("/courses/{course_id}/questions/{question_id}/edit-jobs",status_code=status.HTTP_202_ACCEPTED)
def edit_question(course_id: str,question_id: str,payload: QuestionEdit,request: Request,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,payload.class_id,teaching=True)
    with _content_errors():
        _assert_source_access(content,platform,actor,course_id,payload.source_block_ids,purpose="generation",class_id=payload.class_id)
        values = payload.model_dump(exclude={"count","instruction"})
        job = _bank(request,content).create_edit(course_id=course_id,owner_id=actor.id,question_id=question_id,**values)
    return {"job":job}


@router.get("/content-jobs/{job_id}")
def get_content_job(job_id: str,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    job = content.get_job(job_id)
    if job is None or (job["owner_id"]!=actor.id and actor.role!="admin"):
        raise HTTPException(404,detail={"code":"job_not_found","message":"任务不存在"})
    _scope(platform,actor,job["course_id"],job["class_id"],teaching=True)
    return {"job":job}


@router.post("/courses/{course_id}/requirements",status_code=status.HTTP_201_CREATED)
def create_requirement(course_id: str,payload: CurriculumCreate,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,teaching=True)
    with _content_errors():
        _assert_source_access(content,platform,actor,course_id,payload.source_block_ids,purpose="retrieval")
        row = content.add_requirement(course_id,payload.text,created_by=actor.id,source_block_ids=payload.source_block_ids)
    return row


@router.get("/courses/{course_id}/requirements")
def list_requirements(course_id: str,actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    course = _scope(platform,actor,course_id)
    privileged = actor.role=="admin" or course["teacher_id"]==actor.id
    rows = content.list_requirements(course_id,include_drafts=actor.role!="student",created_by=None if privileged else actor.id)
    if actor.role=="student":
        rows = _student_curriculum(content,course_id,rows)
    return {"items":rows}


@router.post("/courses/{course_id}/knowledge-points",status_code=status.HTTP_201_CREATED)
def create_knowledge_point(course_id: str,payload: KnowledgeCreate,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,teaching=True)
    with _content_errors():
        _assert_source_access(content,platform,actor,course_id,payload.source_block_ids,purpose="retrieval")
        row = content.add_knowledge_point(course_id,payload.code,payload.title,description=payload.description,created_by=actor.id,source_block_ids=payload.source_block_ids)
        if payload.prerequisite_ids:
            content.set_prerequisites(row["id"],payload.prerequisite_ids,actor_id=actor.id)
            row["prerequisite_ids"] = payload.prerequisite_ids
    return row


@router.get("/courses/{course_id}/knowledge-points")
def list_knowledge_points(course_id: str,actor: User = Depends(get_platform_user),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    course = _scope(platform,actor,course_id)
    privileged = actor.role=="admin" or course["teacher_id"]==actor.id
    rows = content.list_knowledge_points(course_id,include_drafts=actor.role!="student",created_by=None if privileged else actor.id)
    if actor.role=="student":
        rows = _student_curriculum(content,course_id,rows)
    return {"items":rows}


def _student_curriculum(content: ContentStore,course_id: str,rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
    result = []
    for row in rows:
        try:
            content.confirmed_sources(course_id,row["source_block_ids"],purpose="retrieval",student=True)
        except (PermissionError,ValueError):
            continue
        result.append({key:value for key,value in row.items() if key not in {"created_by","confirmed_by"}})
    return result


@router.put("/courses/{course_id}/knowledge-points/{knowledge_id}/prerequisites")
def update_prerequisites(course_id: str,knowledge_id: str,payload: PrerequisiteUpdate,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    _scope(platform,actor,course_id,teaching=True)
    row = next((r for r in content.list_knowledge_points(course_id,include_drafts=True,created_by=actor.id) if r["id"]==knowledge_id),None)
    if row is None:
        raise HTTPException(404,detail={"code":"knowledge_not_found","message":"知识点不存在"})
    with _content_errors():
        content.set_prerequisites(knowledge_id,payload.prerequisite_ids,actor_id=actor.id)
    return {"knowledge_id":knowledge_id,"prerequisite_ids":payload.prerequisite_ids}


@router.post("/courses/{course_id}/curriculum/{kind}/{record_id}/confirm")
def confirm_curriculum(course_id: str,kind: Literal["requirement","knowledge"],record_id: str,actor: User = Depends(require_platform_roles("teacher","admin")),platform: PlatformStore = Depends(get_platform_store),content: ContentStore = Depends(get_content_store)):
    course = _scope(platform,actor,course_id,teaching=True)
    if actor.role!="admin" and course["teacher_id"]!=actor.id:
        raise HTTPException(403,detail={"code":"course_owner_required","message":"课程要求与知识点须由课程负责人确认"})
    rows = content.list_requirements(course_id,include_drafts=True) if kind=="requirement" else content.list_knowledge_points(course_id,include_drafts=True)
    if not any(row["id"]==record_id for row in rows):
        raise HTTPException(404,detail={"code":"curriculum_not_found","message":"课程内容不存在"})
    with _content_errors():
        content.confirm_curriculum(kind,record_id,actor_id=actor.id)
    return {"id":record_id,"status":"confirmed"}
