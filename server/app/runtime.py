"""Application-owned repositories and background execution lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable

from fastapi import Request

from .config import Settings

SERVER_DIR = Path(__file__).resolve().parents[1]


def resolve_data_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir)
    return path.resolve() if path.is_absolute() else (SERVER_DIR / path).resolve()


class Runtime:
    """Open each repository once, without touching disk during application import."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data_dir = resolve_data_dir(settings)
        self._stores: dict[str, Any] = {}
        self._lock = RLock()
        self.stop_event = Event()
        self._executor: ThreadPoolExecutor | None = None
        self._futures = []
        self.last_worker_error: str | None = None

    def _get(self, name: str, factory: Callable[[], Any]) -> Any:
        with self._lock:
            if name not in self._stores:
                self._stores[name] = factory()
            return self._stores[name]

    @property
    def platform(self):
        from .platform_store import PlatformStore
        return self._get("platform", lambda: PlatformStore(self.data_dir / "platform.sqlite3"))

    @property
    def queue(self):
        from .job_queue import JobQueue
        return self._get("queue", lambda: JobQueue(self.data_dir / "jobs.sqlite3"))

    @property
    def learning(self):
        from .learning_store import LearningStore
        return self._get("learning", lambda: LearningStore(
            self.data_dir / "learning.sqlite3", access_checker=self.check_learning_access,
        ))

    def check_learning_access(self, actor_id: str, scope: dict, *, write: bool = False) -> bool:
        user = self.platform.get_user(actor_id)
        if user is None or user["status"] != "active":
            return False
        if user["role"] == "student":
            return scope.get("student_id") == actor_id
        if scope.get("class_id"):
            return self.platform.can_access_class(actor_id, scope["class_id"], teaching=write)
        if scope.get("course_id") and scope.get("kind") in {"question", "question_bank"}:
            return self.platform.can_access_course(actor_id, scope["course_id"], teaching=write)
        return user["role"] == "admin" or scope.get("owner_id") == actor_id

    @property
    def content(self):
        from .content_store import ContentStore
        return self._get("content", lambda: ContentStore(self.data_dir / "content.sqlite3"))

    @property
    def reports(self):
        from .report_store import ReportStore
        return self._get("reports", lambda: ReportStore(self.data_dir / "reports.sqlite3"))

    @property
    def companion(self):
        from .companion_store import CompanionStore
        return self._get("companion", lambda: CompanionStore(self.data_dir / "companion.sqlite3", answer_policy=self.learning.get_answer_policy))

    @property
    def gateway(self):
        from .services.model_gateway import ModelGateway
        return self._get("gateway", lambda: ModelGateway(self.settings))

    def install(self, name: str, instance: Any) -> None:
        """Inject a repository or test gateway before workers are started."""
        with self._lock:
            if self._executor is not None:
                raise RuntimeError("runtime dependencies cannot change while workers are running")
            self._stores[name] = instance

    def initialize(self) -> None:
        for name in ("platform", "learning", "queue", "content", "reports", "companion"):
            getattr(self, name)

    def start(self) -> None:
        if not self.settings.worker_enabled:
            return
        with self._lock:
            if self._executor is not None:
                return
            from .worker import Worker
            self.stop_event.clear()
            self._executor = ThreadPoolExecutor(
                max_workers=self.settings.worker_concurrency, thread_name_prefix="study-worker",
            )
            self._futures = [
                self._executor.submit(Worker(self).run, self.stop_event)
                for _ in range(self.settings.worker_concurrency)
            ]

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def worker_status(self) -> dict:
        return {
            "enabled": self.settings.worker_enabled,
            "running": sum(not future.done() for future in self._futures),
            "needs_attention": self.last_worker_error is not None,
        }


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime
