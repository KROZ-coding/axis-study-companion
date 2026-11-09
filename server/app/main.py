from copy import deepcopy
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .routers.study.legacy import router as legacy_router
from .routers.study.teaching import router as teaching_router
from .services.study.content import ScopeNotFoundError
from .services.study_demo import SAMPLE
from .study_store import StudyStore, TeachingPersistenceError

ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"


def create_app(config: Settings | None = None, *, study_data: dict | None = None,
               class_id: str = "demo-math-7-1") -> FastAPI:
    if config is None:
        data_dir = os.getenv("STUDY_DATA_DIR", "").strip()
        path = Path(data_dir) if data_dir else ROOT_DIR / "server" / "data" / "study"
        if not path.is_absolute():
            path = ROOT_DIR / "server" / path
        config = Settings(
            data_dir=str(path), persistence_enabled=os.getenv("STUDY_PERSISTENCE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        )
    application = FastAPI(title="智慧学伴", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)
    application.state.store = StudyStore(config)
    application.state.study_data = deepcopy(SAMPLE if study_data is None else study_data)
    application.state.study_class_id = class_id
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    application.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
    application.include_router(legacy_router)
    application.include_router(teaching_router)

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
        return {"status": "ok", "mode": "offline-sample", "data": application.state.study_data["sampleLabel"],
                "storage": "sqlite" if config.persistence_enabled else "memory", "upstream": "audit-companion"}

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[1] / "static" / "favicon.svg", media_type="image/svg+xml")

    @application.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    return application


app = create_app()
