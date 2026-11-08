from copy import deepcopy
import csv
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .services.study_demo import KPS, SAMPLE, STATUS, student_report
from .study_store import StudyStore

ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"


class AnswerSubmission(BaseModel):
    answer_index: int = Field(ge=0, le=3, strict=True)


def _scope(topic: str, student_id: str | None) -> tuple[list[dict], list[str]]:
    if topic != "all" and topic not in {item["id"] for item in SAMPLE["topics"]}:
        raise HTTPException(status_code=404, detail="主题不存在")
    if student_id is not None and student_id not in SAMPLE["stats"]:
        raise HTTPException(status_code=404, detail="样例中没有该学生")
    students = [item for item in SAMPLE["students"] if student_id is None or item["id"] == student_id]
    kp_ids = [item["id"] for item in KPS if topic == "all" or item["topic"] == topic]
    return students, kp_ids


def _export_rows(topic: str = "all", student_id: str | None = None) -> list[dict]:
    students, kp_ids = _scope(topic, student_id)
    kp_by_id = {item["id"]: item for item in KPS}
    rows = []
    for student in students:
        for kp_id in kp_ids:
            stat = SAMPLE["stats"][student["id"]][kp_id]
            material = SAMPLE["suggestions"][student["id"]][kp_id]
            rows.append({
                "student_id": student["id"], "student_name": student["name"],
                "knowledge_point_id": kp_id, "knowledge_point": kp_by_id[kp_id]["name"],
                "status": STATUS[stat["status"]]["label"], "right_count": stat["right"], "record_count": stat["n"],
                "evidence_ids": "|".join(item["id"] for item in stat["records"]),
                "material_id": material["id"], "material_title": material["title"],
                "reason": "先补充记录核实" if stat["status"] == "none" else "依据已登记的错误与知识点选择材料",
                "next_observation": "阅读讲解后完成下一题并说明步骤；新练习单独保存，不用于宣称学习效果",
                "sample_label": SAMPLE["sampleLabel"],
            })
    return rows


def create_app(config: Settings | None = None) -> FastAPI:
    if config is None:
        data_dir = os.getenv("STUDY_DATA_DIR", "").strip()
        path = Path(data_dir) if data_dir else ROOT_DIR / "server" / "data" / "study"
        if not path.is_absolute():
            path = ROOT_DIR / "server" / path
        config = Settings(
            data_dir=str(path), persistence_enabled=os.getenv("STUDY_PERSISTENCE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        )
    application = FastAPI(title="智慧学伴", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    application.state.store = StudyStore(config)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    application.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @application.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            try:
                parsed = urlsplit(origin or "")
                allowed = bool(origin) and parsed.scheme == request.url.scheme and parsed.netloc.lower() == request.headers.get("host", "").lower()
            except ValueError:
                allowed = False
            if (origin and not allowed) or (request.headers.get("sec-fetch-site") == "cross-site" and not allowed):
                return Response("拒绝跨站修改请求", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": "offline-sample", "data": SAMPLE["sampleLabel"],
                "storage": "sqlite" if config.persistence_enabled else "memory", "upstream": "audit-companion"}

    @application.get("/api/study/sample")
    def sample_data() -> dict:
        data = deepcopy(SAMPLE)
        data["practiceAnswers"] = {
            student["id"]: application.state.store.practice_answers(student["id"])
            for student in SAMPLE["students"]
        }
        scopes = [[kp["id"] for kp in KPS]] + [
            [kp["id"] for kp in KPS if kp["topic"] == topic["id"]]
            for topic in SAMPLE["topics"]
        ]
        data["focus"] = {
            student["id"]: {",".join(kp_ids): student_report(student["id"], kp_ids) for kp_ids in scopes}
            for student in SAMPLE["students"]
        }
        return data

    @application.get("/api/study/students/{student_id}/report")
    def report(student_id: str) -> dict:
        students, kp_ids = _scope("all", student_id)
        return {"student": students[0], **student_report(student_id, kp_ids), "asOf": SAMPLE["asOf"], "sampleLabel": SAMPLE["sampleLabel"]}

    @application.get("/api/study/practice/state")
    def practice_state(student_id: str = "S07") -> dict:
        _scope("all", student_id)
        return {"answers": application.state.store.practice_answers(student_id), "sampleLabel": SAMPLE["sampleLabel"]}

    @application.post("/api/study/practice/{student_id}/{practice_id}")
    def submit_practice(student_id: str, practice_id: str, payload: AnswerSubmission) -> dict:
        _scope("all", student_id)
        practice = next((item for item in SAMPLE["practice"].values() if item["id"] == practice_id), None)
        if practice is None:
            raise HTTPException(status_code=404, detail="练习不存在")
        if payload.answer_index >= len(practice["options"]):
            raise HTTPException(status_code=422, detail="答案选项无效")
        if not application.state.store.save_practice(student_id, practice_id, payload.answer_index):
            raise HTTPException(status_code=503, detail="样例练习暂未可靠保存")
        return {"answer_index": payload.answer_index, "correct": payload.answer_index == practice["answer"],
                "explanation": practice["why"], "sampleLabel": SAMPLE["sampleLabel"], "persisted": config.persistence_enabled}

    @application.get("/api/study/export.json")
    def export_json(topic: str = "all", student_id: str | None = None) -> Response:
        students, kp_ids = _scope(topic, student_id)
        student_ids = {item["id"] for item in students}
        rows = _export_rows(topic, student_id)
        payload = {
            "sample_label": SAMPLE["sampleLabel"], "as_of": SAMPLE["asOf"], "rule": SAMPLE["rule"],
            "scope": {"topic": topic, "student_id": student_id},
            "learning_contents": [item for item in KPS if item["id"] in kp_ids],
            "materials": [item for item in SAMPLE["materials"] if item["topic"] in {kp["topic"] for kp in KPS if kp["id"] in kp_ids}],
            "students": students, "learning_analysis": rows,
            "learning_recommendations": [{key: row[key] for key in ("student_id", "knowledge_point_id", "material_id", "reason", "next_observation")} for row in rows],
            "records": [item for item in SAMPLE["records"] if item["sid"] in student_ids and item["kp"] in kp_ids],
            "field_notes": {"record_count": "该知识点有效记录数；零记录不是零分", "evidence_ids": "对应 records[].id，以 | 分隔",
                            "material_id": "对应 materials[].id；教材引用是样例登记", "status": "演示阈值，非官方评测结论"},
        }
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="study-results.json"'})

    @application.get("/api/study/export.csv")
    def export_csv(topic: str = "all", student_id: str | None = None) -> Response:
        rows = _export_rows(topic, student_id)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        return Response("\ufeff" + output.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="study-results.csv"'})

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "static" / "favicon.svg", media_type="image/svg+xml")

    @application.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    return application


app = create_app()
