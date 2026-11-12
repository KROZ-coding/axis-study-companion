from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .routers.study.legacy import router as legacy_router
from .routers.study.teaching import router as teaching_router
from .routers.v1_auth import router as v1_auth_router
from .routers.v1_companion import router as v1_companion_router
from .routers.v1_courses import router as v1_courses_router
from .routers.v1_jobs import router as v1_jobs_router
from .routers.v1_input import router as v1_input_router
from .routers.v1_learning import router as v1_learning_router
from .routers.v1_materials import router as v1_materials_router
from .routers.v1_reports import router as v1_reports_router
from .runtime import Runtime, resolve_data_dir
from .services.study.content import ScopeNotFoundError
from .services.study_demo import SAMPLE
from .study_store import StudyStore, TeachingPersistenceError

ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"


def create_app(config: Settings | None = None, *, study_data: dict | None = None,
               class_id: str = "demo-math-7-1") -> FastAPI:
    if config is None:
        config = Settings()
        if config.demo_enabled:
            data_dir = os.getenv("STUDY_DATA_DIR", "").strip()
            config = replace(config, data_dir=data_dir or str(ROOT_DIR / "server" / "data" / "study"),
                             persistence_enabled=os.getenv("STUDY_PERSISTENCE_ENABLED", "true").lower() in {"1", "true", "yes", "on"})
    config = replace(config, data_dir=str(resolve_data_dir(config)))
    demo = config.demo_enabled and config.app_env.strip().lower() in {"development", "test"}
    runtime = Runtime(config)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not demo:
            runtime.initialize()
            application.state.platform_store = runtime.platform
            runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    application = FastAPI(title="智慧学伴", version="1.0.0", docs_url=None, redoc_url=None,
                          openapi_url=None, lifespan=lifespan)
    application.state.settings = config
    application.state.runtime = runtime
    application.state.platform_store_path = runtime.data_dir / "platform.sqlite3"
    application.state.platform_store = None
    application.state.store = StudyStore(config) if demo else None
    application.state.study_data = deepcopy(SAMPLE if study_data is None else study_data) if demo else None
    application.state.study_class_id = class_id if demo else None
    configured_hosts = [host for host in config.allowed_hosts if host != "*"]
    hosts = configured_hosts or ["127.0.0.1", "localhost", "testserver"]
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    application.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
    application.mount("/prototype", StaticFiles(directory=WEB_DIR / "prototype", html=True), name="prototype")
    if demo:
        application.include_router(legacy_router)
        application.include_router(teaching_router)
    else:
        for router in (v1_auth_router, v1_courses_router, v1_learning_router, v1_jobs_router,
                       v1_materials_router, v1_companion_router, v1_reports_router, v1_input_router):
            application.include_router(router)

    @application.exception_handler(sqlite3.IntegrityError)
    async def integrity_conflict(request: Request, error: sqlite3.IntegrityError):
        return JSONResponse(status_code=409, content={"detail": {"code": "data_conflict", "message": "记录已存在或当前版本不允许此操作", "retryable": False}})

    @application.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request: Request, error: sqlite3.OperationalError):
        return JSONResponse(status_code=503, content={"detail": {"code": "storage_unavailable", "message": "记录暂未可靠保存，请保留输入并重试", "retryable": True}})

    @application.exception_handler(ScopeNotFoundError)
    async def study_scope_not_found(request: Request, error: ScopeNotFoundError):
        return JSONResponse(status_code=404, content={"detail": error.args[0]})

    @application.exception_handler(TeachingPersistenceError)
    async def teaching_save_failed(request: Request, error: TeachingPersistenceError):
        return JSONResponse(status_code=503, content={"detail": "教学记录暂未可靠保存，本次操作已撤回；请保留输入并重试。"})

    @application.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            try:
                parsed = urlsplit(origin or "")
                allowed = bool(origin) and parsed.scheme == request.url.scheme and parsed.netloc.lower() == request.headers.get("host", "").lower()
            except ValueError:
                allowed = False
            strict = request.url.path.startswith("/api/v1/")
            if (strict and not allowed) or (origin and not allowed) or (request.headers.get("sec-fetch-site") == "cross-site" and not allowed):
                return JSONResponse(status_code=403, content={"detail": {
                    "code": "same_origin_required", "message": "修改请求需要来自本站", "retryable": False,
                }})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.get("/health")
    def health() -> dict:
        if demo:
            return {"status": "ok", "mode": "offline-sample", "data": application.state.study_data["sampleLabel"],
                    "storage": "sqlite" if config.persistence_enabled else "memory", "upstream": "audit-companion"}
        return {"status": "ok", "mode": "platform", "storage": "sqlite-relational",
                "workers": runtime.worker_status(), "upstream": "audit-companion"}

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "static" / "favicon.svg", media_type="image/svg+xml")

    @application.get("/", include_in_schema=False, response_model=None)
    def root() -> FileResponse | JSONResponse:
        if demo:
            return FileResponse(WEB_DIR / "index.html", media_type="text/html")
        return JSONResponse({"name": "智慧学伴", "mode": "platform", "api": "/api/v1",
                             "prototype": "/prototype/",
                             "message": "后端服务已启动。可点击原型在 /prototype/，尚未接入正式 API。"})

    return application


app = create_app()
