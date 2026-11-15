"""Isolated local demonstration application. Never imported by app.main."""
from __future__ import annotations

import os

# The separate launcher sets this before importing app.config. Also protect direct
# factory loading: demo imports must never load the platform's server/.env.
os.environ["SKIP_ENV_FILE"] = "1"
if os.environ.get("RECORDING_DEMO_ALLOW_LIVE") != "1":
    for _key in tuple(os.environ):
        if _key.startswith(("LLM_", "MAXKB_", "KNOWLEDGE_")):
            os.environ.pop(_key, None)

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .demo_fixture import QUESTIONS, DIAGNOSTICS, FORMAL_IDS, chat_text, hint_text, new_round, seed_world, simulate_rubric
from .learning_store import LearningStore
from .routers.v1_auth import login as platform_login
from .routers.v1_learning import _invoke
from .runtime import Runtime
from .schemas.api import LoginRequest
from .v1_deps import get_platform_user, require_platform_roles
from .worker import Worker

SERVER = Path(__file__).resolve().parents[1]
WEB = SERVER.parent / "web"
DATA_ROOT = SERVER / "data" / "recording-demo"


class DemoLearningStore(LearningStore):
    def get_answer_policy(self, user_id, *, task_id=None, question_version_id=None):
        policy = super().get_answer_policy(user_id, task_id=task_id, question_version_id=question_version_id)
        if task_id:
            # An older preserved demonstration must not block this task's hints.
            # Keep the platform's answer-release/generation gates unchanged.
            with self._using_connection() as db:
                task = self._student_task(db, task_id, user_id)
                scoped = self._policy(db, task_id)
                diagnostic = bool(scoped.get("diagnostic_required") and not scoped.get("diagnostic_completed"))
                attempted = db.execute("SELECT 1 FROM draft_answers WHERE task_id=? LIMIT 1", (task_id,)).fetchone() is not None
            policy.update(diagnostic=diagnostic, attempted=attempted,
                          hints_allowed=attempted and not diagnostic and task["status"] in {"not_started", "in_progress"})
        return policy


class DemoWorker(Worker):
    async def _ai_grade(self, job, step, question, answer):
        # All objective items still run the original grade_objective in _grade.
        # No fake provider, request, tokens or ledger entries are created here.
        if question.get("id") in {q["id"] for q in QUESTIONS if q["type"] == "text"}:
            return simulate_rubric(question, answer)
        raise ValueError("演示规则未覆盖此题，请教师按量表评分")


class RecordingRuntime(Runtime):
    @property
    def learning(self):
        return self._get("learning", lambda: DemoLearningStore(
            self.data_dir / "learning.sqlite3", access_checker=self.check_learning_access))

    def start(self):
        if not self.settings.worker_enabled:
            return
        with self._lock:
            if self._executor is not None:
                return
            self.stop_event.clear()
            self._executor = ThreadPoolExecutor(max_workers=self.settings.worker_concurrency, thread_name_prefix="recording-worker")
            self._futures = [self._executor.submit(DemoWorker(self).run, self.stop_event)
                             for _ in range(self.settings.worker_concurrency)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PersonaLogin(StrictInput):
    persona: Literal["teacher", "lin", "zhou"]


class HintInput(StrictInput):
    task_id: str = Field(min_length=1, max_length=100)
    question_id: str = Field(min_length=1, max_length=100)
    message: str = Field(default="请给我下一步提示", min_length=1, max_length=2000)


class ChatInput(StrictInput):
    message: str = Field(min_length=1, max_length=4000)


def _fresh_directory(data_dir):
    directory = Path(data_dir).resolve() if data_dir else DATA_ROOT / (
        "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("演示目录必须为空；每次启动使用新目录，不能复用或清空旧数据库")
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive marker also prevents two factory instances from sharing a run.
    (directory / "recording-demo.run").write_text("Fictional local recording demo. Existing data must be preserved.\n", encoding="utf-8")
    return directory


def create_recording_app(data_dir=None, *, allow_live=False, config=None):
    directory = _fresh_directory(data_dir)
    settings = config or Settings()
    settings = replace(settings, app_env="development", demo_enabled=False, persistence_enabled=True,
        data_dir=str(directory), cookie_secure=False, allowed_hosts=("127.0.0.1", "localhost", "testserver"),
        worker_poll_seconds=0.1, worker_concurrency=2, maxkb_url="", maxkb_api_key="", maxkb_dataset_ids=(),
        maxkb_course_dataset_ids=(), knowledge_dir="", knowledge_index_path="")
    if not allow_live:
        settings = replace(settings, llm_base_url="", llm_api_key="", llm_model="", llm_providers=(), llm_prices_json="")
    runtime = RecordingRuntime(settings)
    world_lock = RLock()

    @asynccontextmanager
    async def lifespan(application):
        runtime.initialize()
        application.state.platform_store = runtime.platform
        application.state.demo_world = seed_world(runtime)
        new_round(runtime, application.state.demo_world)
        save_manifest()
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="智慧学伴 · 全流程演示", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.platform_store = None
    app.state.platform_store_path = directory / "platform.sqlite3"
    app.state.recording_demo = True
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def local_same_origin(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                parsed = urlsplit(request.headers.get("origin", ""))
                allowed = parsed.scheme == request.url.scheme and parsed.netloc.lower() == request.headers.get("host", "").lower()
            except ValueError:
                allowed = False
            if not allowed:
                return JSONResponse(status_code=403, content={"detail": {"code": "same_origin_required", "message": "修改请求需要来自本站"}})
        response = await call_next(request)
        for key, value in {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "same-origin"}.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, error):
        return JSONResponse(status_code=409, content={"detail": {"code": "data_conflict", "message": "记录已存在或当前版本不允许此操作"}})

    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request, error):
        return JSONResponse(status_code=503, content={"detail": {"code": "storage_unavailable", "message": "记录暂未可靠保存，请保留输入重试"}})

    from .routers import (v1_auth, v1_companion, v1_courses, v1_input, v1_jobs,
                          v1_learning, v1_materials, v1_reports, v1_teacher_planning)
    for module in (v1_auth, v1_companion, v1_courses, v1_input, v1_jobs, v1_learning,
                   v1_materials, v1_reports, v1_teacher_planning):
        app.include_router(module.router)
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    app.mount("/prototype", StaticFiles(directory=WEB / "prototype", html=True), name="prototype")
    router = APIRouter(prefix="/api/demo")

    def status():
        world = app.state.demo_world
        return {"enabled": True, "title": "智慧学伴 · 全流程演示", "fictional": True,
                "course_id": world["course_id"], "class_id": world["class_id"], "goal_id": world["goal_id"],
                "round_number": world["round_number"], "run_id": directory.name,
                "material_id": world["material_id"], "task_ids": dict(world["task_ids"]),
                "personas": [{"key": key, "name": row["display_name"], "role": row["role"], "user_id": row["id"]}
                             for key, row in world["users"].items()],
                "capabilities": {"auth": "real", "saving": "real", "diagnostic": "real", "objective_grading": "real",
                    "subjective_grading": "simulation", "hints": "simulation", "reports": "real", "evidence": "real",
                    "chat": "live" if allow_live and settings.llm_configured_for("student") else "simulation",
                    "demo_chat": "simulation", "new_round": True, "question_count": len(FORMAL_IDS),
                    "library_question_count": len(QUESTIONS) + len(DIAGNOSTICS), "diagnostic_question_count": len(DIAGNOSTICS)},
                "chat_endpoint": "/api/v1/conversations" if allow_live and settings.llm_configured_for("student") else "/api/demo/chat",
                "live_enabled": bool(allow_live), "live_configured": bool(allow_live and settings.llm_configured_for("student")),
                "notice": "人物和课程为虚构演示；操作和记录真实保存。提示与简答评分使用已标注的本机模拟规则。"}

    def save_manifest():
        public = status()
        public["rounds"] = app.state.demo_world["rounds"]
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "manifest.json")

    @router.get("/status")
    def demo_status():
        with world_lock:
            return status()

    @router.post("/login")
    async def demo_login(payload: PersonaLogin, request: Request, response: Response):
        credentials = app.state.demo_world["credentials"][payload.persona]
        # The real password verifier, session record, audit and cookie are reused.
        return await platform_login(LoginRequest(**credentials), request, response, runtime.platform)

    @router.post("/new-round")
    def demo_new_round(actor=Depends(require_platform_roles("teacher"))):
        if actor.id != app.state.demo_world["users"]["teacher"]["id"]:
            raise HTTPException(403, "只能由演示教师开启新一轮")
        with world_lock:
            new_round(runtime, app.state.demo_world)
            save_manifest()
            return status()

    @router.post("/hint")
    def demo_hint(payload: HintInput, actor=Depends(require_platform_roles("student"))):
        def generate():
            with runtime.learning.transaction():
                task = runtime.learning.get_task(payload.task_id, actor.id)
                if task["student_id"] != actor.id:
                    raise PermissionError("只能查看本人任务")
                if task.get("diagnostic_required"):
                    from .learning_store import InvalidTransition
                    raise InvalidTransition("首诊需独立完成，不提供提示")
                if task["status"] not in {"not_started", "in_progress"}:
                    from .learning_store import InvalidTransition
                    raise InvalidTransition("本轮作答已提交，请查看评分反馈")
                question = next((q for q in task["questions"] if q["id"] == payload.question_id), None)
                if question is None:
                    raise LookupError("题目不属于本人正式卷")
                if payload.question_id not in task["draft_answers"]:
                    from .learning_store import InvalidTransition
                    raise InvalidTransition("请先保存这道题的尝试，再获取提示")
                with runtime.learning._using_connection() as db:
                    previous = db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND question_id=? AND event_type='help'",
                                          (payload.task_id, payload.question_id)).fetchone()[0]
                step = min(previous + 1, 4)
                text = hint_text({"stem": question["stem"]}, payload.message, step)
                event = runtime.learning.record_help(payload.task_id, actor.id, payload.question_id,
                    payload={"source": "simulation", "message": payload.message, "text": text, "step": step, "rule_version": "recording-hints-v1"},
                    request_id="demo-hint-" + uuid4().hex)
                return {"text": text, "source": "simulation", "task_id": payload.task_id,
                        "question_id": payload.question_id, "step": step, "occurred_at": event["occurred_at"]}
        return _invoke(generate)

    @router.post("/chat")
    async def demo_chat(payload: ChatInput, actor=Depends(require_platform_roles("student"))):
        # This is a one-shot response, never fake streaming. Free chat stays
        # conceptual; scoped exercise help uses /hint and its saved-attempt gate.
        text, source = chat_text(payload.message), "simulation"
        policy = runtime.learning.get_answer_policy(actor.id)
        if policy.get("diagnostic"):
            text = "当前有独立首诊待完成，请先独立作答或标记暂时不会；完成后可以讨论学习方法。以上为本机模拟学伴回复。"
        with runtime.platform.transaction():
            runtime.platform.record_audit_event("recording_demo_chat", actor.id,
                resource_type="recording_demo", resource_id=directory.name,
                payload={"message": payload.message, "text": text, "source": source})
        return {"text": text, "source": source}

    @router.get("/evidence")
    def demo_evidence(student_id: str | None = None, actor=Depends(get_platform_user)):
        world = app.state.demo_world
        if actor.role == "student":
            if student_id not in (None, actor.id):
                raise HTTPException(403, "只能查看本人证据")
            student_id = actor.id
        elif actor.role != "teacher" or not runtime.platform.can_access_class(actor.id, world["class_id"]):
            raise HTTPException(403, "无权查看该班级证据")
        allowed_students = {world["users"][key]["id"] for key in ("lin", "zhou")}
        if student_id and student_id not in allowed_students:
            raise HTTPException(404, "演示学生不存在")
        tasks = runtime.learning.list_tasks(actor.id, class_id=world["class_id"], limit=100)
        tasks = [task for task in tasks if student_id is None or task["student_id"] == student_id]
        items, help_count, submitted_count, graded_count = [], 0, 0, 0
        for task in tasks:
            # list_evidence is the platform's aggregate report projection, not
            # raw events. Read only the already-authorized task's durable facts.
            with runtime.learning._using_connection() as db:
                rows = db.execute("SELECT id,event_type,question_id,occurred_at,payload_json FROM task_events WHERE task_id=? ORDER BY occurred_at,id",
                                  (task["id"],)).fetchall()
                evidence = [{"id": row["id"], "event_type": row["event_type"], "question_id": row["question_id"],
                             "occurred_at": row["occurred_at"], "payload": json.loads(row["payload_json"])} for row in rows]
            help_count += sum(event["event_type"] == "help" for event in evidence)
            submitted_count += len(task["attempts"])
            graded_count += sum(attempt["status"] == "graded" for attempt in task["attempts"])
            with runtime.learning._using_connection() as db:
                diagnostic = db.execute("SELECT id,status,submitted_at,feedback_json FROM diagnostic_sessions WHERE task_id=?", (task["id"],)).fetchone()
            items.append({"task_id": task["id"], "goal_id": task["goal_id"], "student_id": task["student_id"],
                          "status": task["status"], "saved_answer_count": len(task["draft_answers"]),
                          "updated_at": task["updated_at"], "diagnostic": {"id": diagnostic["id"], "status": diagnostic["status"],
                          "submitted_at": diagnostic["submitted_at"], "feedback": json.loads(diagnostic["feedback_json"] or "{}")} if diagnostic else None,
                          "attempts": [{key: attempt.get(key) for key in ("id", "attempt_no", "kind", "status", "submitted_at", "independent_evidence", "total_score", "max_score")}
                                       for attempt in task["attempts"]],
                          "events": [{key: event[key] for key in ("id", "event_type", "question_id", "occurred_at", "payload")} for event in evidence]})
        return {"source": "persisted_records", "student_id": student_id, "items": items,
                "summary": {"task_count": len(items), "saved_answer_count": sum(item["saved_answer_count"] for item in items),
                            "help_event_count": help_count, "submitted_attempt_count": submitted_count, "graded_attempt_count": graded_count},
                "notice": "以下来自已保存的首诊、草稿、提交、评分与求助记录，不把点击或阅读直接视为掌握。"}

    app.include_router(router)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "platform", "storage": "sqlite-relational", "recording_demo": True,
                "workers": runtime.worker_status(), "upstream": "live-opt-in" if allow_live else "local-simulation"}

    @app.get("/")
    def home():
        return RedirectResponse("/prototype/")

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(SERVER / "static" / "favicon.svg", media_type="image/svg+xml")

    return app
