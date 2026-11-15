from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...services.study.content import ScopeNotFoundError, StudyScope, get_practice, resolve_scope, scoped_students
from ...services.study.insights import build_insights, focus_report, student_report
from ...services.study.reports import export_csv, export_json
from ...services.study_demo import SAMPLE

router = APIRouter(prefix="/api/study", tags=["local-sample-compatibility"])


class AnswerSubmission(BaseModel):
    answer_index: int = Field(ge=0, le=3, strict=True)


class LegacyPracticeStore(Protocol):
    """Only last-known choices; no task attempts or historical-diagnosis writes."""

    persistence_enabled: bool

    def practice_answers(self, student_id: str) -> dict[str, int]: ...

    def save_practice(self, student_id: str, practice_id: str, answer: int) -> bool: ...


def get_legacy_practice_store(request: Request) -> LegacyPracticeStore:
    """Replace via app.dependency_overrides when the fact store changes."""
    return request.app.state.store


def _scope(topic: str = "all", student_id: str | None = None) -> StudyScope:
    try:
        return resolve_scope(SAMPLE, topic=topic, student_ids=[student_id] if student_id is not None else None)
    except ScopeNotFoundError as error:
        raise HTTPException(status_code=404, detail=error.args[0]) from error


@router.get("/sample")
def sample_data(store: LegacyPracticeStore = Depends(get_legacy_practice_store)) -> dict[str, Any]:
    # This whole-class, answer-bearing response is only for the existing local demo.
    # Student task APIs must use scoped services and explicit response schemas instead.
    data = deepcopy(SAMPLE)
    data.update(build_insights(SAMPLE, _scope()))
    data["practiceAnswers"] = {
        student["id"]: store.practice_answers(student["id"]) for student in SAMPLE["students"]
    }
    scopes = [[kp["id"] for kp in SAMPLE["kps"]]] + [
        [kp["id"] for kp in SAMPLE["kps"] if kp["topic"] == topic["id"]]
        for topic in SAMPLE["topics"]
    ]
    data["focus"] = {
        student["id"]: {
            ",".join(kp_ids): focus_report(data["stats"][student["id"]], kp_ids) for kp_ids in scopes
        }
        for student in SAMPLE["students"]
    }
    return data


@router.get("/students/{student_id}/report")
def report(student_id: str) -> dict[str, Any]:
    scope = _scope(student_id=student_id)
    return {
        "student": scoped_students(SAMPLE, scope)[0], **student_report(SAMPLE, student_id, scope=scope),
        "asOf": SAMPLE["asOf"], "sampleLabel": SAMPLE["sampleLabel"],
    }


@router.get("/practice/state")
def practice_state(
    student_id: str = "S07", store: LegacyPracticeStore = Depends(get_legacy_practice_store),
) -> dict[str, Any]:
    _scope(student_id=student_id)
    return {"answers": store.practice_answers(student_id), "sampleLabel": SAMPLE["sampleLabel"]}


@router.post("/practice/{student_id}/{practice_id}")
def submit_practice(
    student_id: str, practice_id: str, payload: AnswerSubmission,
    store: LegacyPracticeStore = Depends(get_legacy_practice_store),
) -> dict[str, Any]:
    _scope(student_id=student_id)
    practice = get_practice(SAMPLE, practice_id, include_answer=True)
    if payload.answer_index >= len(practice["options"]):
        raise HTTPException(status_code=422, detail="答案选项无效")
    if not store.save_practice(student_id, practice_id, payload.answer_index):
        raise HTTPException(status_code=503, detail="样例练习暂未可靠保存")
    return {
        "answer_index": payload.answer_index, "correct": payload.answer_index == practice["answer"],
        "explanation": practice["why"], "sampleLabel": SAMPLE["sampleLabel"],
        "persisted": store.persistence_enabled,
    }


@router.get("/export.json")
def download_json(topic: str = "all", student_id: str | None = None) -> Response:
    return Response(
        export_json(SAMPLE, _scope(topic, student_id)), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="study-results.json"'},
    )


@router.get("/export.csv")
def download_csv(topic: str = "all", student_id: str | None = None) -> Response:
    return Response(
        export_csv(SAMPLE, _scope(topic, student_id)), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="study-results.csv"'},
    )
