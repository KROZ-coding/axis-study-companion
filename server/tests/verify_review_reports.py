"""Isolated real-HTTP review, discussion and report fixture.

The fixture is deliberately self-contained.  It starts the real platform FastAPI
application in a child process, uses six temporary SQLite stores, and writes only
redacted evidence below ``.scratch``.  The first administrator and synthetic
approved question versions are the only direct store setup.  Every later state
transition is made through loopback HTTP.

Examples (from the repository root)::

    server/.venv/Scripts/python.exe -I -B -X utf8 server/tests/verify_review_reports.py
    server/.venv/Scripts/python.exe -I -B -X utf8 server/tests/verify_review_reports.py --serve

``--serve`` leaves the real service available for GUI/manual checks until a
signal or ``stop.requested`` is observed.  The generated credentials file is
removed during normal shutdown.  No configured LLM or MaxKB value is inherited,
and the local gateway fails closed if a worker ever attempts a model call.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import csv
import hashlib
import importlib.util
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
SCRIPT = Path(__file__).resolve()
SCRATCH = ROOT / ".scratch"
DATABASES = ("platform", "learning", "jobs", "content", "reports", "companion")
DEFAULT_TIMEOUT = 45.0
FORMATS = ("html", "pdf", "xlsx", "json", "csv")
ACTORS = ("admin", "teacher", "student", "peer", "readonly", "outsider")
REQUIRED_STAGES = (
    "isolated_server_start", "http_accounts_course_class", "synthetic_questions",
    "history_goals_and_tasks", "real_worker_partial_grading", "manual_review_and_replay",
    "graded_retry_history", "correction_appeal_and_resolution", "discussion_notifications",
    "goal_close_renew_and_task_close", "four_scopes_five_formats",
    "new_evidence_preserves_old_exports", "pagination_and_permissions", "prepared_fixture_and_no_upstream",
)
PRIVATE_MARKER = "REVIEW_REPORTS_PRIVATE_FIXTURE_MARKER"
SENSITIVE_KEYS = {
    "password", "password_hash", "current_password", "new_password", "activation_code", "reset_code",
    "lease_token", "cookie", "set-cookie", "sid", "token", "api_key", "authorization", "code_input",
}


class VerificationFailure(AssertionError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value, *, private: bool = False) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        if private:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def isolated_environment(data_dir: Path) -> dict[str, str]:
    """Reuse run_platform_flow's environment boundary without importing config."""
    helper_path = SERVER / "tests" / "run_platform_flow.py"
    spec = importlib.util.spec_from_file_location("review_reports_platform_flow", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("run_platform_flow isolated environment helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = module.isolated_environment(data_dir.resolve())
    for key in tuple(os.environ):
        if key.startswith(("LLM_", "MAXKB_")):
            del os.environ[key]
    os.environ.update(environment)
    # Keep the setting explicit even if a local shell supplied another value.
    os.environ.update(
        SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=str(data_dir.resolve()),
        STUDY_DATA_DIR=str(data_dir.resolve()), STUDY_DEMO_ENABLED="false",
        PERSISTENCE_ENABLED="true", STUDY_PERSISTENCE_ENABLED="true",
        STUDY_WORKER_ENABLED="true", STUDY_WORKER_CONCURRENCY="1", COOKIE_SECURE="false",
        ALLOWED_HOSTS="127.0.0.1,localhost", CORS_ORIGINS="",
        LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="", LLM_PRICES_JSON="",
        MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
        KNOWLEDGE_DIR="", KNOWLEDGE_INDEX_PATH="", PYTHONUNBUFFERED="1",
    )
    return dict(os.environ)


class NoModelGateway:
    """Fail closed and count every model boundary invocation."""

    configured = False

    def __init__(self):
        self.describe_calls = 0
        self.complete_calls = 0

    @staticmethod
    def configured_for(_purpose: str) -> bool:
        return False

    def describe(self, _purpose: str):
        from app.services.model_gateway import GatewayError
        self.describe_calls += 1
        raise GatewayError("review_fixture_model_disabled", request_sent=False)

    async def complete(self, _purpose, _messages, *, call_id, json_mode=False, on_delta=None):
        from app.services.model_gateway import GatewayError
        self.complete_calls += 1
        raise GatewayError("review_fixture_model_disabled", request_sent=False)


def _initialize_admin(runtime, output: Path) -> dict:
    """Only the first administrator is bootstrapped outside HTTP."""
    from app.init_admin import initialize_admin

    password = secrets.token_urlsafe(30)
    initialize_admin(runtime.data_dir / "platform.sqlite3", username="review_fixture_admin",
                     display_name="本机验收管理员", password=password)
    runtime.initialize()
    admin = runtime.platform.get_user_by_username("review_fixture_admin")
    credentials = {"admin": {"username": admin["username"], "password": password,
                             "role": "admin", "user_id": admin["id"]}}
    write_json(output / "credentials.local.json", {"fixture_only": True, "accounts": credentials}, private=True)
    return credentials


def _seed_questions(data_dir: Path, teacher_id: str, course_id: str) -> list[str]:
    """The sole later direct write is the explicitly allowed synthetic question fixture."""
    sys.path.insert(0, str(SERVER))
    from app.learning_store import LearningStore

    learning = LearningStore(data_dir / "learning.sqlite3")
    specs = (
        ("review-objective-a", "单选：2 + 2 等于多少？", ["3", "4", "5"], 1, 1),
        ("review-objective-b", "单选：3 + 1 等于多少？", ["3", "4", "5"], 1, 1),
        ("review-short", "简答：说明你如何检查一次计算结果。", [], "先重新计算并核对条件", 2),
        ("review-objective-c", "单选：5 - 2 等于多少？", ["2", "3", "4"], 1, 1),
    )
    with learning.transaction():
        for qid, stem, options, answer, points in specs:
            learning.create_question_version(teacher_id, qid, 1, {
                "type": "single_choice" if options else "short_answer", "stem": stem, "options": options,
                "answer": answer, "points": points, "explanation": PRIVATE_MARKER,
                "rubric": [{"point": "给出检查步骤", "score_units": 200}] if not options else [],
                "family_id": "family-" + qid, "external_processing_permission": False,
            }, status="approved", course_id=course_id)
    return [spec[0] for spec in specs]


def run_fixture_child(output: Path, data_dir: Path, port: int) -> int:
    """Run the real app and real worker with the fail-closed gateway installed."""
    apply_env = isolated_environment(data_dir)
    for key, value in apply_env.items():
        os.environ[key] = value
    sys.path.insert(0, str(SERVER))
    from app.config import Settings
    from app.main import create_app

    settings = Settings()
    if settings.llm_configured or settings.maxkb_url or settings.maxkb_api_key:
        raise RuntimeError("upstream isolation failed")
    settings = replace(settings, worker_poll_seconds=0.1, worker_concurrency=1,
                       llm_providers=(), llm_prices_json="[]")
    network = {"blocked_external_connections": 0}

    def block_external(event, arguments):
        if event == "socket.connect":
            address = arguments[1]
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
                network["blocked_external_connections"] += 1
                raise RuntimeError("fixture external network disabled")

    sys.addaudithook(block_external)
    application = create_app(settings)
    runtime = application.state.runtime
    gateway = NoModelGateway()
    runtime.install("gateway", gateway)
    credentials = _initialize_admin(runtime, output)
    info = {
        "schema": "review-reports.live-fixture.v1", "status": "starting", "fixture_only": True,
        "created_at": utc_now(), "pid": os.getpid(), "origin": f"http://127.0.0.1:{port}",
        "prototype_url": f"http://127.0.0.1:{port}/prototype/",
        "credentials_path": str((output / "credentials.local.json").resolve()),
        "stop_file": str((output / "stop.requested").resolve()), "data_dir": str(data_dir.resolve()),
        "databases": list(DATABASES), "accounts": sorted(credentials),
        "user_ids": {key: value["user_id"] for key, value in credentials.items()},
        "boundaries": {
            "application": "real FastAPI/uvicorn, real auth/stores/dispatcher/background worker",
            "environment": "run_platform_flow.isolated_environment; .env skipped; LLM_/MAXKB_ removed",
            "upstream": "NoModelGateway installed; model calls fail closed and no provider is contacted",
            "direct_store": "initial administrator and four synthetic approved question versions only",
            "http": "accounts, course/class/grants, goals, submissions, review, discussion and reports use HTTP",
        },
    }
    write_json(output / "service-info.json", info)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, workers=1,
                                           lifespan="on", access_log=False, log_level="critical",
                                           timeout_graceful_shutdown=15, log_config=None))
    stop_watcher = threading.Event()

    def watch_stop():
        while not stop_watcher.wait(0.2):
            if (output / "stop.requested").exists():
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch_stop, name="review-fixture-stop", daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        stop_watcher.set()
        watcher.join(timeout=2)
        runtime.stop()
        write_json(output / "isolation.json", {
            "env_file_skipped": os.environ.get("SKIP_ENV_FILE") == "1",
            "model_configured": settings.llm_configured,
            "maxkb_configured": bool(settings.maxkb_url or settings.maxkb_api_key),
            "gateway_describe_calls": gateway.describe_calls, "gateway_complete_calls": gateway.complete_calls,
            **network, "worker_after_stop": runtime.worker_status(),
        })
    return 0


def output_directory(value: Path | None) -> Path:
    scratch = SCRATCH.resolve()
    output = ((value.expanduser().resolve()) if value else
              scratch / ("review-reports-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)))
    if not output.is_relative_to(scratch) or output == scratch:
        raise ValueError("output-dir must be a NEW directory below the repository's ignored .scratch/")
    check = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--quiet", "--",
                            str(output / "credentials.local.json")], capture_output=True, check=False)
    if check.returncode != 0:
        raise ValueError("output directory is not ignored by Git")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    return output


class Verification:
    def __init__(self, output: Path, args):
        self.output, self.args = output, args
        self.process = None
        self.server_log = None
        self.clients = {}
        self.credentials = {}
        self.private = set()
        self.info = None
        self.data = None
        self.base = None
        self.stage_name = "bootstrap"
        self.job_ids = []
        self.reports = {}
        self.report_bytes = {}
        self.started = time.monotonic()
        self.evidence = {
            "schema": "review-reports.http-smoke.v1", "status": "running", "started_at": utc_now(),
            "stages": [], "http": [], "failures": [], "required_stages": list(REQUIRED_STAGES),
            "boundary": "real loopback HTTP with cookies/Origin; synthetic setup bounded; no model call",
            "not_covered": ["real model quality/provider billing", "full browser automation"],
        }
        self.save()

    def save(self):
        write_json(self.output / "evidence.json", self.clean(self.evidence))

    def collect_private(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in SENSITIVE_KEYS and isinstance(item, str) and item:
                    self.private.add(item)
                else:
                    self.collect_private(item)
        elif isinstance(value, list):
            for item in value:
                self.collect_private(item)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else self.clean(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self.private, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)\b(password|lease_token|sid|activation_code|reset_code|api_key|authorization)"
                          r"([\s\"':=]+)[^\s,;\"'}]+", r"\1\2[REDACTED]", value)
        return value

    @staticmethod
    def check(condition, message):
        if not condition:
            raise VerificationFailure(message)

    @contextmanager
    def stage(self, name):
        self.stage_name = name
        row = {"stage": name, "status": "running", "started_at": utc_now()}
        self.evidence["stages"].append(row)
        self.save()
        print(f"RUN {name}", flush=True)
        started = time.monotonic()
        try:
            yield row
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__, message=self.clean(str(error)))
            self.evidence["failures"].append({"stage": name, "type": type(error).__name__,
                                                "message": self.clean(str(error))})
            print(f"FAIL {name}: {self.clean(str(error))}", flush=True)
            raise
        else:
            row["status"] = "passed"
            print(f"PASS {name}", flush=True)
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def start_service(self, data: Path):
        import httpx
        self.data = data.resolve()
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.server_log = tempfile.TemporaryFile(mode="w+b")
        env = isolated_environment(self.data)
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", str(SCRIPT), "--fixture-child",
             "--output-dir", str(self.output), "--data-dir", str(self.data), "--port", str(port)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=self.server_log,
            stderr=subprocess.STDOUT, shell=False,
        )
        actors = ("anonymous", *ACTORS)
        self.clients = {actor: httpx.Client(base_url=self.base, headers={"Origin": self.base},
                                             timeout=httpx.Timeout(self.args.timeout, connect=2),
                                             follow_redirects=False, trust_env=False)
                        for actor in actors}
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            self.check(self.process.poll() is None, "fixture server exited before readiness")
            try:
                response = self.clients["anonymous"].get("/health")
            except httpx.TransportError:
                time.sleep(0.1)
                continue
            if response.status_code == 200 and (self.output / "service-info.json").is_file():
                health = response.json()
                self.check(health.get("mode") == "platform", "fixture must use platform mode")
                self.check(health["workers"]["running"] == 1 and not health["workers"]["needs_attention"],
                           "real worker is unhealthy")
                self.info = json.loads((self.output / "service-info.json").read_text(encoding="utf-8"))
                self.credentials = json.loads((self.output / "credentials.local.json").read_text(encoding="utf-8"))["accounts"]
                for item in self.credentials.values():
                    self.private.add(item["password"])
                self.info.update(status="preparing", launcher_pid=os.getpid())
                write_json(self.output / "service-info.json", self.info)
                self.evidence["service"] = {"origin": self.base, "pid": self.process.pid, "health": health}
                self.save()
                return
            time.sleep(0.1)
        raise VerificationFailure("fixture readiness timed out")

    def request(self, actor, method, path, expected=200, *, origin=True, **kwargs):
        import httpx
        request = self.clients[actor].build_request(method, path, **kwargs)
        if not origin:
            request.headers.pop("Origin", None)
        try:
            response = self.clients[actor].send(request)
        except httpx.TransportError as error:
            raise VerificationFailure(f"{method} {path}: transport {type(error).__name__}") from error
        for cookie in self.clients[actor].cookies.jar:
            self.private.add(cookie.value)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        self.collect_private(body)
        entry = {"stage": self.stage_name, "actor": actor, "method": method, "path": path,
                 "status": response.status_code, "bytes": len(response.content)}
        if response.status_code >= 400:
            entry["error_response"] = self.clean(body if body is not None else response.text[:1600])
        self.evidence["http"].append(entry)
        accepted = {expected} if isinstance(expected, int) else set(expected)
        self.check(response.status_code in accepted,
                   f"{method} {path}: expected {sorted(accepted)}, got {response.status_code}; body={self.clean(body)}")
        return response

    def api(self, actor, method, path, expected=200, **kwargs):
        response = self.request(actor, method, path, expected, **kwargs)
        return response.json() if response.content else None

    def login(self, actor):
        account = self.credentials[actor]
        response = self.request(actor, "POST", "/api/v1/auth/login", json={
            "username": account["username"], "password": account["password"], "role": account["role"],
        })
        self.check("HttpOnly" in response.headers.get("set-cookie", ""), f"{actor}: cookie must be HttpOnly")
        self.check(bool(self.clients[actor].cookies.get("sid")), f"{actor}: session cookie missing")
        self.check(self.api(actor, "GET", "/api/v1/auth/me")["id"] == account["user_id"],
                   f"{actor}: /me identity mismatch")

    def poll(self, label, read, done, *, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else self.args.timeout)
        last = None
        while time.monotonic() < deadline:
            self.check(self.process is not None and self.process.poll() is None, f"{label}: server stopped")
            last = read()
            if done(last):
                return last
            time.sleep(0.15)
        raise VerificationFailure(f"{label}: timed out; last={self.clean(last)}")

    def task(self, actor, task_id):
        return self.api(actor, "GET", f"/api/v1/tasks/{task_id}")

    def submit(self, actor, task_id, answers, key):
        task = self.task(actor, task_id)
        lease = self.api(actor, "POST", f"/api/v1/tasks/{task_id}/edit-leases",
                         json={"device_id": "review-" + actor, "lease_seconds": 60})
        self.private.add(lease["lease_token"])
        draft = self.api(actor, "PUT", f"/api/v1/tasks/{task_id}/draft", json={
            "expected_version": lease["task_version"], "request_id": key + "-draft",
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"], "answers": answers,
        })
        return self.api(actor, "POST", f"/api/v1/tasks/{task_id}/submit", 202, json={
            "expected_version": draft["version"], "request_id": key + "-submit",
            "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
        })

    def wait_task(self, actor, task_id, statuses=("results_ready", "awaiting_manual", "closed")):
        return self.poll("task terminal", lambda: self.task(actor, task_id),
                         lambda row: row.get("status") in statuses)

    def create_http_accounts(self):
        self.login("admin")
        labels = {"teacher": "合成教师", "readonly": "只读协作者", "outsider": "班外教师",
                  "student": "合成学生", "peer": "同班同学"}
        for actor in ("teacher", "readonly", "outsider", "student", "peer"):
            password = secrets.token_urlsafe(30)
            self.private.add(password)
            self.credentials[actor] = {"username": "review_fixture_" + actor, "password": password,
                                       "role": "student" if actor in {"student", "peer"} else "teacher"}
        for actor in ("teacher", "readonly", "outsider"):
            account = self.credentials[actor]
            application = self.api("anonymous", "POST", "/api/v1/auth/teacher-applications", 202, json={
                "username": account["username"], "password": account["password"], "display_name": labels[actor],
            })
            self.api("admin", "POST", f"/api/v1/auth/teacher-applications/{application['id']}/approve")
            account["user_id"] = application["id"]
            self.login(actor)
        application = self.api("teacher", "POST", "/api/v1/course-applications", 202,
                               json={"name": "第二三阶段评审报告验收课程", "term": "2026-review"})
        approved = self.api("admin", "POST", f"/api/v1/course-applications/{application['id']}/review",
                            json={"approve": True, "note": "仅本机合成验收"})
        course_id = approved["course_id"]
        classroom = self.api("teacher", "POST", f"/api/v1/courses/{course_id}/classes", 201,
                             json={"name": "第二三阶段验收班", "owner_teacher_id": self.credentials["teacher"]["user_id"]})
        class_id = classroom["id"]
        for actor in ("student", "peer"):
            account = self.credentials[actor]
            student = self.api("teacher", "POST", "/api/v1/auth/students", 201, json={
                "username": account["username"], "display_name": labels[actor],
                "course_id": course_id, "class_id": class_id,
            })
            self.private.add(student["activation_code"])
            self.api("anonymous", "POST", "/api/v1/auth/activate", 204,
                     json={"code": student["activation_code"], "password": account["password"]})
            account["user_id"] = student["student_id"]
            self.login(actor)
        for path in (f"/api/v1/classes/{class_id}/collaborators", f"/api/v1/courses/{course_id}/collaborators"):
            self.api("teacher", "PUT", path, json={"teacher_id": self.credentials["readonly"]["user_id"], "access_level": "read"})
        self.info.update(course_id=course_id, class_id=class_id, accounts=list(ACTORS),
                         user_ids={actor: self.credentials[actor]["user_id"] for actor in ACTORS})
        write_json(self.output / "credentials.local.json", {"fixture_only": True, "accounts": self.credentials}, private=True)
        write_json(self.output / "service-info.json", self.info)
        self.api("anonymous", "GET", "/api/v1/tasks", 401)
        self.api("outsider", "GET", f"/api/v1/classes/{class_id}", 403)
        self.check(not self.api("readonly", "GET", f"/api/v1/classes/{class_id}")["can_manage"],
                   "read-only collaborator acquired teaching permission")
        self.check(len(self.api("teacher", "GET", f"/api/v1/classes/{class_id}/members")) == 2,
                   "fixture class must have two students")

    def review(self, attempt_id, actor="teacher"):
        return self.api(actor, "GET", f"/api/v1/attempts/{attempt_id}/teacher-review")

    def closed_answers(self, task):
        self.check(task["answer_released"] is False, "partial task released answers")
        self.check(PRIVATE_MARKER not in json.dumps(task), "partial task leaked protected explanations")
        for question in task["questions"]:
            self.check(not {"answer", "explanation", "rubric", "reference_answer"}.intersection(question),
                       "partial task leaked answer fields")
        for attempt in task["attempts"]:
            self.check("items" not in attempt, "closed answer gate leaked per-item grading")
            if attempt["status"] != "graded":
                self.check(not {"total_score", "total_score_units"}.intersection(attempt),
                           "unfinished attempt leaked a partial total")

    def check_partial(self, task_id, actor):
        task = self.wait_task(actor, task_id, ("awaiting_manual",))
        self.closed_answers(task)
        review = self.review(task["attempts"][-1]["id"])
        self.check(review["can_grade"] and not review["results_released"], "partial review authority/release mismatch")
        self.check(review["attempt"]["status"] == "review_required" and "total_score" not in review["attempt"],
                   "partial attempt is not awaiting manual review")
        self.check(len(review["items"]) == 3, "review lost frozen items")
        for item in review["items"]:
            self.check(item["question_version_id"] == item["question_id"] + ":1", "review lost frozen version")
            if item["question_id"] == "review-short":
                self.check(item["status"] == "manual_pending" and item["score_units"] is None
                           and item["can_manual_grade"] and not item["can_correct"], "short answer manual boundary missing")
            else:
                self.check(item["status"] == "graded" and item["score_units"] == 100
                           and item["original_grading"]["score_units"] == 100
                           and not item["can_manual_grade"] and not item["can_correct"],
                           "real worker did not preserve objective partial grading")
        self.check(not {"fence_token", "job_id", "outbox_status", "lease_token"}.intersection(
            re.findall(r'"([a-z_]+)"\s*:', json.dumps(review))), "review leaked worker internals")
        return review

    def check_job(self, job_id):
        job = self.poll("real worker " + job_id,
                        lambda: self.api("teacher", "GET", f"/api/v1/jobs/{job_id}"),
                        lambda value: value["status"] in {"succeeded", "failed", "cancelled", "reconciling"})
        self.check(job["status"] == "succeeded", "worker job did not succeed: " + job_id)
        self.check(self.api("teacher", "GET", f"/api/v1/jobs/{job_id}/calls")["items"] == [],
                   "fixture job attempted upstream processing")
        if job_id not in self.job_ids:
            self.job_ids.append(job_id)

    def manual_grade(self, attempt_id, key, score_units=100):
        before = self.review(attempt_id)
        payload = {"request_id": key, "expected_revision": before["attempt"]["revision"],
                   "score_units": score_units, "reason": "按冻结量表核对计算检查步骤。"}
        path = f"/api/v1/attempts/{attempt_id}/items/review-short/manual-grade"
        result = self.api("teacher", "POST", path, json=payload)
        self.check(self.api("teacher", "POST", path, json=payload) == result, "manual replay changed result")
        self.api("teacher", "POST", path, 409, json={**payload, "score_units": score_units + 1})
        after = self.review(attempt_id)
        self.check(after["attempt"]["revision"] == before["attempt"]["revision"] + 1
                   and after["attempt"]["status"] == "graded", "manual grade did not finish the attempt")
        self.check(after["attempt"]["answers"] == before["attempt"]["answers"]
                   and after["attempt"]["submitted_at"] == before["attempt"]["submitted_at"],
                   "manual grading changed immutable submission")
        return result

    def notifications(self, actor):
        return self.api(actor, "GET", "/api/v1/me/notifications", params={"limit": 500})

    def notification(self, actor, event_type, **binding):
        def matches(item):
            return item["event_type"] == event_type and all(item.get("payload", {}).get(key) == value
                                                           for key, value in binding.items())
        items = self.poll("notification " + event_type, lambda: self.notifications(actor),
                          lambda values: any(matches(item) for item in values))
        found = [item for item in items if matches(item)]
        self.check(len(found) == 1, "notification replay was not deduplicated")
        return found[0]

    def new_thread(self, task_id, attempt_id, key):
        review = self.review(attempt_id)
        payload = {"request_id": key, "task_id": task_id, "attempt_id": attempt_id,
                   "question_version_id": "review-short:1", "grading_revision": review["attempt"]["revision"],
                   "title": "简答短评"}
        thread = self.api("teacher", "POST", "/api/v1/companion/discussion-threads", 201, json=payload)
        self.check(self.api("teacher", "POST", "/api/v1/companion/discussion-threads", 201, json=payload) == thread,
                   "thread creation replay changed binding")
        return thread

    def report(self, scope, key):
        source = self.api("teacher", "GET", "/api/v1/reports/source-revision", params=scope)
        payload = {"scope": scope, "schema_version": source["schema_version"],
                   "rule_version": source["default_rule_version"], "expected_source_revision": source["source_revision"]}
        result = self.api("teacher", "POST", "/api/v1/reports", 201, json=payload,
                          headers={"Idempotency-Key": key})
        replay = self.request("teacher", "POST", "/api/v1/reports", 200, json=payload,
                              headers={"Idempotency-Key": key})
        self.check(replay.headers.get("Idempotency-Replayed") == "true" and replay.json() == result,
                   "report replay did not return the original frozen revision")
        snapshot = result["snapshot"]
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        self.check(digest == result["snapshot_sha256"] and snapshot["scope"] == scope,
                   "canonical frozen snapshot/hash/scope mismatch")
        self.check(snapshot["source_revision"] == source["source_revision"], "report used another source revision")
        self.check(PRIVATE_MARKER not in json.dumps(snapshot), "snapshot contains protected question content")
        return result

    def export(self, report, format, actor="teacher"):
        response = self.request(actor, "GET", f"/api/v1/reports/{report['report_id']}/export/{format}",
                                params={"revision_id": report["id"]})
        self.check(response.headers.get("X-Report-Snapshot-SHA256") == report["snapshot_sha256"]
                   and response.headers.get("X-Report-Revision") == report["id"], "export revision/hash mismatch")
        self.check(response.headers.get("Cache-Control") == "no-store"
                   and response.headers.get("X-Content-Type-Options") == "nosniff"
                   and "attachment" in response.headers.get("Content-Disposition", ""), "unsafe export headers")
        content, snapshot = response.content, report["snapshot"]
        self.check(bool(content) and PRIVATE_MARKER.encode() not in content, "empty/private export")
        manifest = None
        if format in {"json", "html"}:
            if format == "json":
                value = json.loads(content)
            else:
                match = re.search(r'<script[^>]+id="report-data"[^>]*>(.*?)</script>', content.decode("utf-8"), re.S)
                self.check(match is not None, "HTML lacks embedded frozen report")
                value = json.loads(match.group(1))
            self.check(value["snapshot"] == snapshot, "embedded report differs from frozen snapshot")
            manifest = value["manifest"]
        elif format == "csv":
            with ZipFile(io.BytesIO(content)) as archive:
                self.check(set(archive.namelist()) == {name + ".csv" for name in snapshot["tables"]} | {"manifest.json"},
                           "CSV archive tables do not match snapshot")
                manifest = json.loads(archive.read("manifest.json"))
                for name, table in snapshot["tables"].items():
                    payload = archive.read(name + ".csv")
                    values = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
                    self.check(payload.startswith(b"\xef\xbb\xbf") and len(values) == len(table["rows"]) + 1
                               and values[0] == [column["key"] for column in table["columns"]], "CSV rows/columns mismatch")
                    self.check(hashlib.sha256(payload).hexdigest() == manifest["files"][name + ".csv"]["sha256"],
                               "CSV manifest file hash mismatch")
                    self.check(PRIVATE_MARKER not in payload.decode("utf-8-sig"), "compressed CSV leaked protected content")
        elif format == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), data_only=False, keep_links=True)
            try:
                self.check(len(workbook.worksheets) == len(snapshot["tables"]) + 1 and not workbook._external_links,
                           "XLSX tables/external links mismatch")
                self.check(workbook.properties.subject == "Frozen report " + report["snapshot_sha256"],
                           "XLSX frozen hash mismatch")
                for worksheet in workbook:
                    for cells in worksheet.iter_rows():
                        for cell in cells:
                            self.check(cell.data_type not in {"f", "e"} and cell.hyperlink is None
                                       and PRIVATE_MARKER not in str(cell.value), "unsafe/private XLSX cell")
            finally:
                workbook.close()
        elif format == "pdf":
            self.check(content.startswith(b"%PDF-"), "invalid PDF signature")
            try:
                from pypdf import PdfReader
            except ImportError:
                import pymupdf
                with pymupdf.open(stream=content, filetype="pdf") as document:
                    self.check(document.page_count > 0, "PDF has no pages")
                    value = json.loads(document.embfile_get("report.json"))
                    manifest = json.loads(document.embfile_get("manifest.json"))
                    text = "\n".join(page.get_text() for page in document)
            else:
                reader = PdfReader(io.BytesIO(content))
                self.check(len(reader.pages) > 0, "PDF has no pages")
                value = json.loads(reader.attachments["report.json"][0])
                manifest = json.loads(reader.attachments["manifest.json"][0])
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.check(value["snapshot"] == snapshot and PRIVATE_MARKER not in text, "PDF frozen content/privacy mismatch")
        if manifest is not None:
            self.check(manifest["snapshot_sha256"] == report["snapshot_sha256"]
                       and manifest["source_revision"] == report["source_revision"] and manifest["scope"] == snapshot["scope"],
                       "export manifest does not identify the frozen snapshot")
        return content

    def run_smoke(self):
        """The same mandatory HTTP checks prepare both smoke and --serve data."""
        with self.stage("http_accounts_course_class") as row:
            self.create_http_accounts()
            row.update(accounts=list(ACTORS), course_id=self.info["course_id"], class_id=self.info["class_id"])

        with self.stage("synthetic_questions") as row:
            self.info["question_ids"] = _seed_questions(self.data, self.info["user_ids"]["teacher"], self.info["course_id"])
            listing = self.api("teacher", "GET", f"/api/v1/courses/{self.info['course_id']}/questions")["items"]
            self.check(len(listing) == 4 and all(item["status"] == "approved" for item in listing),
                       "HTTP question library cannot see approved fixture")
            write_json(self.output / "service-info.json", self.info)
            row["approved_versions"] = 4

        with self.stage("history_goals_and_tasks") as row:
            course_id, class_id = self.info["course_id"], self.info["class_id"]
            self.goal_ids, self.task_ids = [], {}
            refs = [{"question_id": qid, "version": 1} for qid in self.info["question_ids"][:3]]
            for index, title in enumerate(("历史目标一：评分与重做", "历史目标二：待人工批改"), 1):
                goal = self.api("teacher", "POST", "/api/v1/goals", 201, json={
                    "request_id": f"review-goal-{index}", "title": title,
                    "description": "HTTP 合成验收目标", "course_id": course_id, "class_id": class_id,
                })
                self.goal_ids.append(goal["id"])
                published = self.api("teacher", "POST", f"/api/v1/goals/{goal['id']}/rounds", 201, json={
                    "request_id": f"review-round-{index}", "expected_version": goal["version"], "round_no": 1,
                    "student_ids": [self.info["user_ids"][key] for key in ("student", "peer")],
                    "question_versions": refs, "allow_late": False, "allow_retries": True,
                    "allow_retry_after_deadline": False, "common_question_ratio": 100,
                })
                tasks = [self.task("teacher", task_id) for task_id in published["task_ids"]]
                self.task_ids[goal["id"]] = {
                    actor: next(task["id"] for task in tasks if task["student_id"] == self.info["user_ids"][actor])
                    for actor in ("student", "peer")
                }
            row.update(goals=self.goal_ids, tasks=self.task_ids)

        with self.stage("real_worker_partial_grading") as row:
            self.answers = {"review-objective-a": 1, "review-objective-b": 1,
                            "review-short": "我重新计算并核对题目条件。"}
            self.attempt_ids = {}
            for index, goal_id in enumerate(self.goal_ids):
                self.attempt_ids[goal_id] = {}
                for actor in ("student", "peer"):
                    task_id = self.task_ids[goal_id][actor]
                    self.submit(actor, task_id, self.answers, f"review-submit-{index}-{actor}")
                    review = self.check_partial(task_id, actor)
                    self.attempt_ids[goal_id][actor] = review["attempt"]["id"]
                    self.check_job(f"grade:{task_id}:1")
            self.review_task = self.task_ids[self.goal_ids[0]]["student"]
            self.review_attempt = self.attempt_ids[self.goal_ids[0]]["student"]
            self.partial_task = self.task_ids[self.goal_ids[1]]["student"]
            self.partial_attempt = self.attempt_ids[self.goal_ids[1]]["student"]
            row.update(submissions=4, objective_items_graded=8, short_answers_manual_pending=4,
                       upstream_calls=0)

        with self.stage("manual_review_and_replay") as row:
            review = self.review(self.review_attempt)
            reader = self.review(self.review_attempt, "readonly")
            self.check(not reader["can_grade"] and not any(item["can_manual_grade"] for item in reader["items"]),
                       "read-only review permits grading")
            path = f"/api/v1/attempts/{self.review_attempt}/items/review-short/manual-grade"
            payload = {"request_id": "manual-denied", "expected_revision": review["attempt"]["revision"],
                       "score_units": 100, "reason": "验收权限边界"}
            for actor, status in (("student", 403), ("peer", 403), ("readonly", 404), ("outsider", 404)):
                self.api(actor, "POST", path, status, json=payload)
            self.api("teacher", "POST", path, 409, json={**payload, "expected_revision": review["attempt"]["revision"] + 1})
            self.api("teacher", "POST", path, 422, json={**payload, "score_units": 201})
            graded = self.manual_grade(self.review_attempt, "manual-student-first")
            self.manual_grade(self.attempt_ids[self.goal_ids[0]]["peer"], "manual-peer-first", 150)
            self.check(self.task("student", self.review_task)["status"] == "results_ready", "manual grade did not release results")
            row.update(attempt_id=self.review_attempt, grading_revision=graded["grading_revision"],
                       total_score=graded["total_score"], immutable_submission=True, idempotent=True)

        with self.stage("graded_retry_history") as row:
            before = self.review(self.review_attempt)
            task = self.task("student", self.review_task)
            payload = {"request_id": "review-retry", "expected_version": task["version"]}
            retry = self.api("student", "POST", f"/api/v1/tasks/{self.review_task}/retry", json=payload)
            self.check(not retry["answer_released"], "retry did not close the answer gate")
            answers = {**self.answers, "review-short": "重做时先核对条件，再独立计算两遍。"}
            self.submit("student", self.review_task, answers, "review-submit-retry")
            partial = self.check_partial(self.review_task, "student")
            self.retry_attempt = partial["attempt"]["id"]
            self.check_job(f"grade:{self.review_task}:2")
            self.manual_grade(self.retry_attempt, "manual-student-retry", 200)
            after = self.review(self.review_attempt)
            self.check(after["attempt"] == before["attempt"] and after["items"] == before["items"],
                       "retry modified the historical first attempt")
            history = self.task("teacher", self.review_task)["attempts"]
            self.check([item["attempt_no"] for item in history] == [1, 2]
                       and all(item["status"] == "graded" for item in history), "graded retry history incomplete")
            self.check(not history[1]["independent_evidence"], "retry counted as independent first evidence")
            row.update(task_id=self.review_task, first_attempt=self.review_attempt, retry_attempt=self.retry_attempt,
                       submission_count=5, first_attempt_unchanged=True)

        with self.stage("correction_appeal_and_resolution") as row:
            before = self.review(self.review_attempt)
            correction_path = f"/api/v1/attempts/{self.review_attempt}/items/review-objective-a/corrections"
            payload = {"request_id": "review-correction", "expected_revision": before["attempt"]["revision"],
                       "score_units": 0, "reason": "验收追加更正版本。"}
            correction = self.api("teacher", "POST", correction_path, json=payload)
            self.check(self.api("teacher", "POST", correction_path, json=payload) == correction,
                       "correction replay changed result")
            self.api("teacher", "POST", correction_path, 409, json={**payload, "reason": "不同更正理由"})
            self.api("teacher", "POST", correction_path, 409, json={**payload, "request_id": "stale-correction"})
            after = self.review(self.review_attempt)
            self.check(after["attempt"]["answers"] == before["attempt"]["answers"]
                       and after["attempt"]["submitted_at"] == before["attempt"]["submitted_at"],
                       "correction changed immutable submitted work")
            corrected_item = next(item for item in after["items"] if item["question_id"] == "review-objective-a")
            self.check(corrected_item["score_units"] == 0 and corrected_item["original_grading"]["score_units"] == 100,
                       "correction overwrote original grading")
            history = self.api("teacher", "GET", f"/api/v1/attempts/{self.review_attempt}/corrections")["items"]
            self.check(len(history) == 1 and history[0]["prior_score_units"] == 100
                       and history[0]["corrected_score_units"] == 0, "append-only correction history mismatch")
            appeal_payload = {"request_id": "review-appeal", "question_id": "review-objective-a",
                              "grading_revision": after["attempt"]["revision"], "reason": "请求复核客观题评分。"}
            appeal_path = f"/api/v1/attempts/{self.review_attempt}/appeals"
            self.api("student", "POST", appeal_path, 409,
                     json={**appeal_payload, "request_id": "stale-appeal", "grading_revision": before["attempt"]["revision"]})
            appeal = self.api("student", "POST", appeal_path, 202, json=appeal_payload)
            self.check(self.api("student", "POST", appeal_path, 202, json=appeal_payload) == appeal,
                       "appeal replay changed ID")
            self.appeal_id = appeal["id"]
            detail_path = f"/api/v1/appeals/{self.appeal_id}"
            detail = self.poll("appeal worker handoff", lambda: self.api("teacher", "GET", detail_path),
                               lambda value: value["status"] == "awaiting_teacher")
            self.check_job(f"appeal:{self.appeal_id}")
            self.check(detail["can_resolve"] and detail["current_revision"] == correction["grading_revision"],
                       "appeal lacks current teacher resolution revision")
            for event in ("appeal_received", "appeal_review_ready"):
                notification = self.notification("teacher", event, appeal_id=self.appeal_id,
                                                 attempt_id=self.review_attempt, task_id=self.review_task)
                self.check(not notification["redacted"], "teacher appeal notification lost its authorized payload")
            resolve = {"request_id": "review-appeal-resolve", "expected_revision": detail["current_revision"],
                       "reason": "已核对冻结答案与原始评分；本次结案不另改分。"}
            self.check(not self.api("readonly", "GET", detail_path)["can_resolve"], "reader can resolve appeal")
            self.api("readonly", "POST", detail_path + "/resolve", 404, json=resolve)
            self.api("student", "POST", detail_path + "/resolve", 403, json=resolve)
            self.api("peer", "GET", detail_path, 404)
            self.api("outsider", "GET", detail_path, 404)
            self.api("teacher", "POST", detail_path + "/resolve", 409,
                     json={**resolve, "expected_revision": detail["current_revision"] - 1})
            before_resolve = self.review(self.review_attempt)
            resolved = self.api("teacher", "POST", detail_path + "/resolve", json=resolve)
            self.check(resolved["status"] == "resolved"
                       and self.api("teacher", "POST", detail_path + "/resolve", json=resolve) == resolved,
                       "appeal resolution/replay mismatch")
            self.check(self.review(self.review_attempt) == before_resolve, "appeal resolution changed formal grading")
            self.check(self.api("student", "GET", detail_path)["result"] == resolved["result"],
                       "released student appeal lacks resolution")
            # Other partial tasks may close the student's general notification
            # policy. The event still must exist exactly once and belong to this task.
            notifications = self.poll("student appeal resolution notification", lambda: self.notifications("student"),
                                      lambda items: any(item["event_type"] == "appeal_resolved"
                                                        and item["resource_id"] == self.review_task for item in items))
            self.check(sum(item["event_type"] == "appeal_resolved" and item["resource_id"] == self.review_task
                           for item in notifications) == 1, "resolution notification replay duplicated")
            row.update(appeal_id=self.appeal_id, current_revision=resolved["current_revision"],
                       correction_records=1, teacher_notifications_visible=True, resolution_preserved_grading=True)

        with self.stage("discussion_notifications") as row:
            self.thread = self.new_thread(self.review_task, self.review_attempt, "review-thread")
            path = f"/api/v1/companion/discussion-threads/{self.thread['id']}"
            comment_payload = {"request_id": "review-short-comment", "text": "请说明核对条件与重复计算的具体步骤。"}
            comment = self.api("teacher", "POST", path + "/messages", 201, json=comment_payload)
            self.check(comment["preset_key"] is None and comment["text"] == comment_payload["text"],
                       "teacher short comment must be ordinary text")
            self.check(self.api("teacher", "POST", path + "/messages", 201, json=comment_payload) == comment,
                       "short comment replay changed message")
            reply_payload = {"request_id": "review-student-reply", "text": "收到，我会逐项核对条件，再重新计算。"}
            reply = self.api("student", "POST", path + "/messages", 201, json=reply_payload)
            self.check(self.api("student", "POST", path + "/messages", 201, json=reply_payload) == reply,
                       "student reply replay changed message")
            messages = self.api("student", "GET", path + "/messages")
            self.check([item["id"] for item in messages] == [comment["id"], reply["id"]]
                       and all(not item["redacted"] for item in messages), "released discussion message projection mismatch")
            bindings = {"task_id": self.review_task, "attempt_id": self.review_attempt,
                        "thread_id": self.thread["id"], "question_version_id": "review-short:1",
                        "grading_revision": self.thread["grading_revision"]}
            student_notice = self.notification("student", "discussion_teacher_message", **bindings, message_id=comment["id"])
            teacher_notice = self.notification("teacher", "discussion_student_reply", **bindings, message_id=reply["id"])
            for notice in (student_notice, teacher_notice):
                self.check(not notice["redacted"] and notice["resource_id"] == self.thread["id"]
                           and notice["payload"]["delivery"] == "delivered"
                           and not {"text", "title"}.intersection(notice["payload"]),
                           "discussion notification lacks canonical safe locators")
            self.check(not self.api("readonly", "GET", path)["can_reply"], "read-only teacher can reply")
            self.api("readonly", "POST", path + "/messages", 403, json={"request_id": "reader-reply", "text": "越权"})
            for actor in ("peer", "outsider"):
                self.api(actor, "GET", path, 404)
                self.api(actor, "POST", path + "/messages", 404, json={"request_id": "foreign-reply", "text": "越权"})
            for actor in ("peer", "readonly", "outsider"):
                self.check(not any(item["event_type"] in {"discussion_teacher_message", "discussion_student_reply"}
                                   for item in self.notifications(actor)), "discussion notification delivered to unauthorized recipient")
            read_path = f"/api/v1/me/notifications/{student_notice['id']}/read"
            self.api("peer", "POST", read_path, 404)
            first_read = self.api("student", "POST", read_path)
            second_read = self.api("student", "POST", read_path)
            self.check(first_read["read_at"] is not None and second_read["read_at"] == first_read["read_at"],
                       "notification read replay is not stable")
            self.pending_thread = self.new_thread(self.partial_task, self.partial_attempt, "pending-thread")
            pending_path = f"/api/v1/companion/discussion-threads/{self.pending_thread['id']}"
            pending = self.api("teacher", "POST", pending_path + "/messages", 201,
                               json={"request_id": "pending-comment", "text": "待全部评分完成后再核对这条教师短评。"})
            presets = self.api("teacher", "GET", "/api/v1/companion/discussion-presets")
            self.check(any(item["key"] == "await_results" for item in presets), "safe guidance preset missing")
            safe = self.api("teacher", "POST", pending_path + "/messages", 201,
                            json={"request_id": "pending-guidance", "preset_key": "await_results"})
            hidden = self.api("student", "GET", pending_path + "/messages")
            self.check(hidden[0]["id"] == pending["id"] and hidden[0]["text"] == "" and hidden[0]["redacted"]
                       and hidden[0]["delivery"] == "deferred", "partial task exposed free-text teacher comment")
            self.check(hidden[1]["id"] == safe["id"] and not hidden[1]["redacted"] and bool(hidden[1]["text"]),
                       "safe preset was incorrectly hidden")
            notice = self.notification("student", "discussion_teacher_message", message_id=pending["id"],
                                       thread_id=self.pending_thread["id"], task_id=self.partial_task)
            self.check(notice["redacted"] and notice["payload"]["delivery"] == "deferred",
                       "pending discussion notification exposed content")
            row.update(thread_id=self.thread["id"], pending_thread_id=self.pending_thread["id"],
                       reply_id=reply["id"], exact_events=True, deduplicated=True, partial_text_hidden=True)

        with self.stage("goal_close_renew_and_task_close") as row:
            goal_id = self.goal_ids[1]
            goal = self.api("teacher", "GET", f"/api/v1/goals/{goal_id}")
            plan_path = f"/api/v1/goals/{goal_id}/plan"
            close_payload = {"request_id": "review-goal-close", "action": "close", "reason": "验收关闭历史目标。",
                             "expected_version": goal["version"]}
            closed = self.api("teacher", "POST", plan_path, json=close_payload)
            self.check(self.api("teacher", "POST", plan_path, json=close_payload) == closed
                       and self.api("teacher", "GET", f"/api/v1/goals/{goal_id}")["status"] == "closed",
                       "goal close/replay mismatch")
            renew_payload = {"request_id": "review-goal-renew", "action": "renew", "reason": "验收续期目标。",
                             "expected_version": closed["version"], "future_round_count": 1,
                             "new_end_at": time.time() + 3600}
            renewed = self.api("teacher", "POST", plan_path, json=renew_payload)
            self.check(self.api("teacher", "POST", plan_path, json=renew_payload) == renewed
                       and self.api("teacher", "GET", f"/api/v1/goals/{goal_id}")["status"] == "active",
                       "goal renew/replay mismatch")
            self.api("outsider", "POST", plan_path, 404, json=renew_payload)
            self.closed_task = self.task_ids[goal_id]["peer"]
            task = self.task("teacher", self.closed_task)
            close_path = f"/api/v1/tasks/{self.closed_task}/close"
            payload = {"request_id": "review-task-close", "expected_version": task["version"],
                       "reason": "关闭新提交，保留现有人工批改与历史。"}
            self.api("readonly", "POST", close_path, 404, json=payload)
            self.api("peer", "POST", close_path, 403, json=payload)
            self.api("teacher", "POST", close_path, 409, json={**payload, "expected_version": task["version"] - 1})
            closed_task = self.api("teacher", "POST", close_path, json=payload)
            self.check(self.api("teacher", "POST", close_path, json=payload) == closed_task,
                       "task close replay changed result")
            self.check(closed_task["submission_closed"] and closed_task["status"] == "awaiting_manual"
                       and closed_task["attempts"] == task["attempts"], "task closure changed existing grading/history")
            self.closed_answers(self.task("peer", self.closed_task))
            self.api("peer", "POST", f"/api/v1/tasks/{self.closed_task}/edit-leases", 409,
                     json={"device_id": "closed-task"})
            self.api("peer", "POST", f"/api/v1/tasks/{self.closed_task}/retry", 409,
                     json={"request_id": "closed-retry", "expected_version": closed_task["version"]})
            self.check(any(item["can_manual_grade"] for item in self.review(self.attempt_ids[goal_id]["peer"])["items"]),
                       "closing submissions disabled pending manual grading")
            row.update(goal_id=goal_id, closed_version=closed["version"], renewed_version=renewed["version"],
                       closed_task_id=self.closed_task, manual_review_preserved=True)

        with self.stage("four_scopes_five_formats") as row:
            scopes = (
                {"kind": "class", "course_id": self.info["course_id"], "class_id": self.info["class_id"]},
                {"kind": "student", "course_id": self.info["course_id"], "class_id": self.info["class_id"],
                 "student_id": self.info["user_ids"]["student"]},
                {"kind": "goal", "course_id": self.info["course_id"], "class_id": self.info["class_id"],
                 "goal_id": self.goal_ids[0]},
                {"kind": "question_bank", "course_id": self.info["course_id"], "question_bank_id": "review-bank"},
            )
            rules = self.api("teacher", "GET", "/api/v1/reports/rules")
            self.check(set(rules["export_formats"]) == set(FORMATS), "required report export formats missing")
            artifacts = {}
            export_dir = self.output / "exports"
            export_dir.mkdir()
            for scope in scopes:
                kind = scope["kind"]
                report = self.report(scope, "review-report-" + kind)
                self.reports[kind] = report
                self.report_bytes[kind] = {}
                tables = report["snapshot"]["tables"]
                self.check(len(tables["questions"]["rows"]) >= 3, "report has no meaningful question evidence")
                if kind == "question_bank":
                    self.check(set(tables) == {"summary", "questions", "question_usage", "sources", "rules"}
                               and bool(tables["question_usage"]["rows"]), "question-bank aggregate scope mismatch")
                    self.check(all(identifier not in json.dumps(report["snapshot"])
                                   for identifier in (self.info["user_ids"]["student"], self.info["user_ids"]["peer"])),
                               "question-bank report leaked individual students")
                else:
                    self.check(bool(tables["evidence"]["rows"]), "report lacks graded attempt evidence")
                    if kind == "class":
                        self.check(len(tables["students"]["rows"]) == 2 and len(tables["goals"]["rows"]) == 2,
                                   "class report lacks two goals/two students")
                    if kind == "student":
                        self.check({item["student_id"] for item in tables["students"]["rows"]}
                                   == {self.info["user_ids"]["student"]}, "student report leaked peers")
                artifacts[kind] = {"report_id": report["report_id"], "revision_id": report["id"],
                                   "source_revision": report["source_revision"],
                                   "snapshot_sha256": report["snapshot_sha256"], "formats": {}}
                for format in FORMATS:
                    content = self.export(report, format)
                    self.report_bytes[kind][format] = content
                    artifact = export_dir / (kind + ("-csv.zip" if format == "csv" else "." + format))
                    artifact.write_bytes(content)
                    artifacts[kind]["formats"][format] = {
                        "path": str(artifact), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                    }
            row.update(scopes=4, formats=5, exports=20, artifacts=artifacts)

        with self.stage("new_evidence_preserves_old_exports") as row:
            before = self.review(self.review_attempt)
            old = self.reports["goal"]
            self.api("teacher", "POST", f"/api/v1/attempts/{self.review_attempt}/items/review-objective-a/corrections", json={
                "request_id": "review-report-source-change", "expected_revision": before["attempt"]["revision"],
                "score_units": 50, "reason": "第二次更正产生真实新证据，用于核对历史快照不变。",
            })
            # Ensure even timestamp-sensitive containers are repeated across a real second.
            time.sleep(1.1)
            self.new_report = self.report(old["snapshot"]["scope"], "review-report-goal-new")
            self.check(self.new_report["report_id"] == old["report_id"] and self.new_report["id"] != old["id"]
                       and self.new_report["source_revision"] > old["source_revision"]
                       and self.new_report["snapshot_sha256"] != old["snapshot_sha256"],
                       "real correction did not produce a distinct newer snapshot")
            old_items = old["snapshot"]["tables"]["evidence"]["rows"]
            new_items = self.new_report["snapshot"]["tables"]["evidence"]["rows"]
            def corrected_evidence(items):
                return next(item for item in items if item["attempt_id"] == self.review_attempt
                            and item["question_id"] == "review-objective-a")
            self.check(corrected_evidence(old_items)["score_ratio"] == 0
                       and corrected_evidence(new_items)["score_ratio"] == 0.5,
                       "new report changed revision without reflecting corrected evidence")
            for kind, report in self.reports.items():
                frozen = self.api("teacher", "GET", f"/api/v1/reports/{report['report_id']}/revisions/{report['id']}")
                self.check({key: value for key, value in frozen.items() if key != "is_current"}
                           == {key: value for key, value in report.items() if key != "is_current"},
                           "historical revision metadata/snapshot changed")
                if kind == "goal":
                    self.check(frozen["is_current"] is False, "old goal revision remained current after new evidence")
                for format in FORMATS:
                    self.check(self.export(report, format) == self.report_bytes[kind][format],
                               f"{kind}/{format}: historical export bytes changed after new evidence")
            self.api("teacher", "POST", "/api/v1/reports", 409, headers={"Idempotency-Key": "review-report-stale-source"},
                     json={"scope": old["snapshot"]["scope"], "schema_version": "report.snapshot.v1",
                           "rule_version": old["rule_version"], "expected_source_revision": old["source_revision"]})
            self.api("teacher", "GET", f"/api/v1/reports/{old['report_id']}/export/json", 404,
                     params={"revision_id": self.reports["class"]["id"]})
            row.update(old_revision=old["id"], new_revision=self.new_report["id"],
                       old_source_revision=old["source_revision"], new_source_revision=self.new_report["source_revision"],
                       old_sha256=old["snapshot_sha256"], new_sha256=self.new_report["snapshot_sha256"],
                       old_exports_byte_identical=20, actual_score_changed=True)


        with self.stage("pagination_and_permissions") as row:
            for endpoint, count in (("goals", 2), ("tasks", 4)):
                params = {"class_id": self.info["class_id"], "limit": 100}
                all_items = self.api("teacher", "GET", "/api/v1/" + endpoint, params=params)["items"]
                self.check(len(all_items) == count, "history list lost fixture records")
                pages = []
                for offset in range(count):
                    page = self.api("teacher", "GET", "/api/v1/" + endpoint,
                                    params={**params, "limit": 1, "offset": offset})["items"]
                    self.check(len(page) == 1, "history pagination omitted a record")
                    pages.extend(page)
                self.check([item["id"] for item in pages] == [item["id"] for item in all_items],
                           "history offset pages are unstable or repeated")
                self.check(self.api("teacher", "GET", "/api/v1/" + endpoint,
                                    params={**params, "offset": count})["items"] == [], "history pagination never ends")
                self.check(len(self.api("readonly", "GET", "/api/v1/" + endpoint, params=params)["items"]) == count,
                           "read-only teacher cannot read authorized history")
                self.check(self.api("outsider", "GET", "/api/v1/" + endpoint, params=params)["items"] == [],
                           "outsider history list leaked records")
            review_path = f"/api/v1/attempts/{self.review_attempt}/teacher-review"
            for actor, status in (("anonymous", 401), ("student", 403), ("peer", 403), ("outsider", 404)):
                self.api(actor, "GET", review_path, status)
            self.api("peer", "GET", f"/api/v1/tasks/{self.review_task}", 404)
            for kind, report in self.reports.items():
                path = f"/api/v1/reports/{report['report_id']}/revisions/{report['id']}"
                reader_report = self.api("readonly", "GET", path)
                self.check({key: value for key, value in reader_report.items() if key != "is_current"}
                           == {key: value for key, value in report.items() if key != "is_current"},
                           "read-only report history differs")
                self.check(self.export(report, "json", "readonly") == self.report_bytes[kind]["json"],
                           "read-only export differs from authorized snapshot")
                self.api("outsider", "GET", path, 404)
                self.api("peer", "GET", path, 404)
                self.api("readonly", "POST", "/api/v1/reports", 403,
                         headers={"Idempotency-Key": "readonly-report-" + kind}, json={
                             "scope": report["snapshot"]["scope"], "schema_version": "report.snapshot.v1",
                             "rule_version": report["rule_version"], "expected_source_revision": report["source_revision"],
                         })
            own = self.reports["student"]
            self.check(self.export(own, "json", "student") == self.report_bytes["student"]["json"],
                       "student cannot read own frozen report")
            self.api("student", "GET", f"/api/v1/reports/{self.reports['class']['report_id']}", 404)
            message_path = f"/api/v1/companion/discussion-threads/{self.thread['id']}/messages"
            first = self.api("teacher", "GET", message_path, params={"limit": 1, "offset": 0})
            second = self.api("teacher", "GET", message_path, params={"limit": 1, "offset": 1})
            self.check(len(first) == len(second) == 1 and first[0]["id"] != second[0]["id"],
                       "discussion pagination repeated first message")
            self.request("teacher", "POST", message_path, 403, origin=False,
                         json={"request_id": "origin-rejected", "text": "不应写入"})
            row.update(goals=2, tasks=4, offset_pagination=True, reader_reads=True,
                       reader_writes_denied=True, cross_student_denied=True, outsider_denied=True, origin_required=True)

        with self.stage("prepared_fixture_and_no_upstream") as row:
            self.check_partial(self.partial_task, "student")
            self.check(not self.task("teacher", self.partial_task)["submission_closed"],
                       "GUI fixture has no open manual-review task")
            self.check(len(self.task("teacher", self.review_task)["attempts"]) == 2, "GUI fixture lost retry history")
            for job_id in tuple(self.job_ids):
                self.check_job(job_id)
            health = self.api("anonymous", "GET", "/health")
            self.check(health["workers"]["running"] == 1 and not health["workers"]["needs_attention"],
                       "worker requires attention")
            self.check(all((self.data / (name + ".sqlite3")).is_file() for name in DATABASES),
                       "fixture did not initialize all six temporary databases")
            self.info.update(status="prepared", goals=self.goal_ids, tasks=self.task_ids, attempts=self.attempt_ids,
                             scenarios={
                                 "partial_manual": {"task_id": self.partial_task, "attempt_id": self.partial_attempt},
                                 "graded_history_retry": {"task_id": self.review_task, "first_attempt_id": self.review_attempt,
                                                          "retry_attempt_id": self.retry_attempt},
                                 "closed_submission_manual": {"task_id": self.closed_task},
                                 "discussion": {"thread_id": self.thread["id"], "pending_thread_id": self.pending_thread["id"]},
                                 "resolved_appeal": {"appeal_id": self.appeal_id},
                             }, reports={kind: {"report_id": report["report_id"], "revision_id": report["id"],
                                                "snapshot_sha256": report["snapshot_sha256"]}
                                         for kind, report in self.reports.items()},
                             newer_goal_revision_id=self.new_report["id"])
            write_json(self.output / "service-info.json", self.info)
            self.save()
            text = (self.output / "evidence.json").read_text(encoding="utf-8")
            info_text = (self.output / "service-info.json").read_text(encoding="utf-8")
            self.check(all(secret not in text + info_text for secret in self.private),
                       "redacted evidence contains a credential or cookie")
            row.update(worker_healthy=True, six_temporary_databases=True, real_jobs=len(self.job_ids),
                       upstream_call_records=0, gui_scenarios=self.info["scenarios"])

    def stop_service(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        process = self.process
        try:
            if process is not None:
                if process.poll() is None:
                    (self.output / "stop.requested").touch(exist_ok=True)
                    try:
                        process.wait(timeout=35)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                        self.evidence["failures"].append({"stage": "shutdown", "type": "ShutdownTimeout",
                                                          "message": "fixture required forced shutdown"})
                self.evidence["shutdown"] = {"pid": process.pid, "exit_code": process.returncode,
                                              "reaped": process.poll() is not None,
                                              "stop_requested": (self.output / "stop.requested").exists()}
                if process.returncode != 0:
                    self.evidence["failures"].append({"stage": "shutdown", "type": "ServerExit",
                                                      "message": f"fixture exited with code {process.returncode}"})
                self.process = None
            isolation_path = self.output / "isolation.json"
            if isolation_path.is_file():
                isolation = json.loads(isolation_path.read_text(encoding="utf-8"))
                self.evidence["isolation"] = isolation
                self.check(isolation["env_file_skipped"] and not isolation["model_configured"]
                           and not isolation["maxkb_configured"] and isolation["gateway_describe_calls"] == 0
                           and isolation["gateway_complete_calls"] == 0 and isolation["blocked_external_connections"] == 0,
                           "fixture attempted upstream access or failed isolation")
                self.check(isolation["worker_after_stop"]["running"] == 0, "worker remained running after shutdown")
            elif process is not None:
                raise VerificationFailure("child shutdown isolation evidence missing")
        finally:
            credentials = self.output / "credentials.local.json"
            if credentials.is_file():
                try:
                    self.collect_private(json.loads(credentials.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
            if self.server_log is not None:
                self.server_log.flush()
                self.server_log.seek(0, os.SEEK_END)
                self.server_log.seek(max(0, self.server_log.tell() - 12000))
                diagnostic = self.server_log.read().decode("utf-8", errors="replace")
                if diagnostic:
                    self.evidence["server_diagnostics"] = self.clean(diagnostic)
                self.server_log.close()
                self.server_log = None
            credentials.unlink(missing_ok=True)
            credentials.with_name(credentials.name + ".tmp").unlink(missing_ok=True)
            self.evidence["credentials_removed"] = not credentials.exists()
            if self.info:
                self.info.update(status="stopped", stopped_at=utc_now(), credentials_removed=not credentials.exists())
                write_json(self.output / "service-info.json", self.info)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help="keep the real isolated service until signal or stop.requested")
    parser.add_argument("--output-dir", type=Path, help="NEW ignored directory below .scratch")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="bounded readiness/HTTP polling timeout")
    parser.add_argument("--fixture-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.flags.isolated or not sys.dont_write_bytecode or not sys.flags.utf8_mode:
        parser.error("invoke this script with python.exe -I -B -X utf8")
    if not math.isfinite(args.timeout) or not 10 <= args.timeout <= 120:
        parser.error("timeout must be 10..120 seconds")
    logging.disable(logging.CRITICAL)
    if args.fixture_child:
        if args.output_dir is None or args.data_dir is None or args.port is None:
            parser.error("invalid internal fixture arguments")
        output = args.output_dir.resolve()
        data = args.data_dir.resolve()
        if not output.is_relative_to(SCRATCH.resolve()) or not output.is_dir():
            parser.error("internal fixture output must be an existing ignored scratch directory")
        if not args.data_dir.is_absolute() or not data.is_dir() or any(data.iterdir()):
            parser.error("internal data directory must be absolute, existing and empty")
        try:
            return run_fixture_child(output, data, args.port)
        except Exception as error:
            print("FIXTURE CHILD FAILED (" + type(error).__name__ + ")", flush=True)
            return 1
    try:
        output = output_directory(args.output_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Fixture output: {output}", flush=True)
    verification = Verification(output, args)
    interrupted = threading.Event()

    def requested_stop(_signum, _frame):
        interrupted.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, requested_stop)
    temporary = None
    try:
        with tempfile.TemporaryDirectory(prefix="review-reports-runtime-") as temporary:
            try:
                with verification.stage("isolated_server_start"):
                    verification.start_service(Path(temporary))
                verification.run_smoke()
                passed = {row["stage"] for row in verification.evidence["stages"] if row["status"] == "passed"}
                verification.check(set(REQUIRED_STAGES) <= passed, "required HTTP stages are incomplete")
                if args.serve:
                    verification.info.update(status="ready", ready_at=utc_now())
                    write_json(output / "service-info.json", verification.info)
                    verification.evidence.update(mode="serve", status="serving", preparation_passed=True)
                    verification.save()
                    print(f"READY origin={verification.info['origin']}; service-info={output / 'service-info.json'}", flush=True)
                    while not interrupted.wait(0.25):
                        if (output / "stop.requested").exists():
                            break
                        verification.check(verification.process.poll() is None, "fixture exited without stop request")
            except KeyboardInterrupt:
                interrupted.set()
            except Exception as error:
                if not verification.evidence["failures"]:
                    verification.evidence["failures"].append({"stage": verification.stage_name,
                                                              "type": type(error).__name__,
                                                              "message": verification.clean(str(error))})
            finally:
                verification.stop_service()
    except Exception as error:
        verification.evidence["failures"].append({"stage": "cleanup", "type": type(error).__name__,
                                                   "message": verification.clean(str(error))})
    removed = temporary is not None and not Path(temporary).exists()
    verification.evidence["temporary_data_removed"] = removed
    verification.evidence["databases_removed"] = {
        name: temporary is not None and not (Path(temporary) / (name + ".sqlite3")).exists()
        for name in DATABASES
    }
    if not removed or not verification.evidence.get("credentials_removed"):
        verification.evidence["failures"].append({"stage": "cleanup", "type": "CleanupIncomplete",
                                                  "message": "temporary databases or local credentials remain"})
    passed = {row["stage"] for row in verification.evidence["stages"] if row["status"] == "passed"}
    missing = sorted(set(REQUIRED_STAGES) - passed)
    verification.evidence["missing_stages"] = missing
    if missing and not verification.evidence["failures"]:
        verification.evidence["failures"].append({"stage": "completion", "type": "IncompleteSmoke",
                                                  "message": "required stages did not all pass"})
    status = "failed" if verification.evidence["failures"] else "stopped" if args.serve else "interrupted" if interrupted.is_set() else "passed"
    verification.evidence.update(status=status, finished_at=utc_now(),
                                 elapsed_seconds=round(time.monotonic() - verification.started, 3),
                                 http_requests=len(verification.evidence["http"]))
    verification.save()
    print(f"RESULT {status}; evidence={output / 'evidence.json'}", flush=True)
    return 0 if status in {"passed", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
