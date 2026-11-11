"""Run the approved, isolated platform flow against a real uvicorn process.

Usage (from the repository root):
    python.exe -I server/tests/run_platform_flow.py
    python.exe -I server/tests/run_platform_flow.py --output-dir <NEW-directory>

Only initial-admin creation, synthetic approved question fixtures, event-specific
SQLite fault injection, backup/restore and read-only persistence checks cross the
HTTP boundary. No TestClient, gateway replacement, manual worker execution,
clock manipulation, real account data or configured upstream service is used.
The JSON evidence contains no passwords, activation codes, cookies or raw DBs.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile


SERVER = Path(__file__).resolve().parents[1]
DATABASES = ("platform", "learning", "jobs", "content", "reports", "companion")
PRIVATE_MARKER = "PRIVATE_PLATFORM_FLOW_REFERENCE"
STAGES = (
    "bootstrap", "server_start", "teacher_application_approval_rollback",
    "course_application_approval_rollback", "class_and_student_creation_rollback",
    "activation_rollback_and_cookie_login", "material_upload_and_confirmation",
    "synthetic_approved_question_fixture", "goal_publish_replay_and_origin",
    "diagnostic_gate_and_submission", "continuous_plan_configuration",
    "authorization_and_fenced_lease", "draft_submit_rollback_replay_and_worker",
    "peer_submission_and_worker", "reports_from_real_learning_evidence",
    "five_http_exports", "xlsx_repeat_across_seconds", "appeal_worker_teacher_handoff",
    "correction_and_frozen_report_revision", "input_confirmation_and_audio_unavailable",
    "continuous_plan_real_elapsed_round", "stop_before_backup",
    "six_database_backup_inspect_restore", "restore_sidecars_and_hashes",
    "restore_runtime_layout", "restored_real_service_http_checks", "final_shutdown",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def isolated_environment(data_dir):
    # Override before importing app.config, whose dataclass defaults read env at
    # import time. The exact same environment is passed to both real servers.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("LLM_", "MAXKB_"))}
    env.update(
        SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=str(data_dir), STUDY_DATA_DIR=str(data_dir),
        STUDY_DEMO_ENABLED="false", PERSISTENCE_ENABLED="true", STUDY_PERSISTENCE_ENABLED="true",
        STUDY_WORKER_ENABLED="true", STUDY_WORKER_CONCURRENCY="1", COOKIE_SECURE="false",
        ALLOWED_HOSTS="127.0.0.1,localhost", CORS_ORIGINS="",
        LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="", LLM_PRICES_JSON="",
        MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
        KNOWLEDGE_DIR="", KNOWLEDGE_INDEX_PATH="", PYTHONUNBUFFERED="1",
    )
    return env


class FlowFailure(AssertionError):
    pass


class Flow:
    def __init__(self, root, output, args):
        self.root, self.output, self.args = root, output, args
        self.data = root / "source-runtime"
        self.env = isolated_environment(self.data)
        os.environ.update(self.env)
        # Explicitly remove inherited optional provider variables, not merely
        # omit them from the child's environment.
        for key in tuple(os.environ):
            if key.startswith(("LLM_", "MAXKB_")) and key not in self.env:
                del os.environ[key]
        sys.path.insert(0, str(SERVER))
        self.secrets = set()
        self.passwords = {name: self.secret(secrets.token_urlsafe(24))
                          for name in ("admin", "teacher", "student", "peer")}
        self.clients = {}
        self.process = None
        self.server_log = None
        self.stage_name = "bootstrap"
        self.started = time.monotonic()
        self.evidence = {
            "schema": "platform.real-http-flow.v1", "started_at": utc_now(), "status": "running",
            "python": sys.version.split()[0], "dependencies": {}, "stages": [], "http": [],
            "failures": [], "boundary": {
                "transport": "real loopback httpx clients with real Cookie jars and matching Origin",
                "application": "uvicorn app.main:app; formal platform routes; real background worker enabled",
                "direct_store_operations": [
                    "initialize_admin for the first isolated administrator (no public bootstrap API)",
                    "Runtime.learning.create_question_version for five synthetic approved objective fixtures",
                    "event-specific SQLite triggers and read-only rollback/persistence checks in isolated databases",
                    "BackupManager six-database backup/inspect/restore, hash checks and runtime-layout copying",
                ],
                "synthetic_question_approval": "fixture only; does not accept AI generation/review quality",
                "upstream": "NOT ACCEPTED: all LLM/MaxKB endpoints, credentials, providers and prices explicitly disabled; .env ignored",
                "materials": "real HTTP upload/confirmation/read; private originals verified by backup hashes (no public original-download API)",
            },
            "not_covered": [
                {"feature": "real LLM generation/review/subjective grading/conversation and provider billing", "reason": "upstream explicitly disabled; separate configured-provider acceptance required"},
                {"feature": "real MaxKB retrieval", "reason": "upstream explicitly disabled"},
                {"feature": "real ASR transcription", "reason": "current API is unconfigured; only its explicit HTTP 503 is verified"},
                {"feature": "manual grading and goal close/renew endpoints", "reason": "available existing APIs; outside this bounded flow (teacher correction is covered)"},
            ],
        }
        self.save()

    def secret(self, value):
        if isinstance(value, str) and value:
            self.secrets.add(value)
        return value

    def collect_secrets(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"password", "password_hash", "activation_code", "reset_code", "lease_token", "sid", "cookie", "set-cookie"}:
                    self.secret(item)
                else:
                    self.collect_secrets(item)
        elif isinstance(value, list):
            for item in value:
                self.collect_secrets(item)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in {
                "password", "password_hash", "current_password", "new_password", "activation_code", "reset_code",
                "lease_token", "cookie", "set-cookie", "sid", "token", "code_input", "input",
            } else self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for private in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(private, "[REDACTED]")
            value = re.sub(r"(?i)(sid|lease_token|activation_code|reset_code|password)([\s\"':=]+)[^\s,;\"'}]+", r"\1\2[REDACTED]", value)
            return value
        return value

    def save(self):
        (self.output / "evidence.json").write_text(
            json.dumps(self.clean(self.evidence), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
        )

    @contextmanager
    def stage(self, name, *, fatal=True):
        self.stage_name = name
        row = {"stage": name, "status": "running", "started_at": utc_now()}
        self.evidence["stages"].append(row)
        self.save()
        print(f"RUN {name}", flush=True)
        start = time.monotonic()
        try:
            yield row
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__, error=self.clean(str(error)))
            self.evidence["failures"].append({"stage": name, "type": type(error).__name__, "message": self.clean(str(error))})
            print(f"FAIL {name}: {self.clean(str(error))}", flush=True)
            if fatal:
                raise
        else:
            row["status"] = "passed"
            print(f"PASS {name}", flush=True)
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - start, 3)
            self.save()

    @staticmethod
    def check(condition, message):
        if not condition:
            raise FlowFailure(message)

    def request(self, actor, method, path, expected=200, *, origin=True, **kwargs):
        client = self.clients[actor]
        request = client.build_request(method, path, **kwargs)
        if not origin:
            request.headers.pop("Origin", None)
        try:
            response = client.send(request)
        except Exception as error:
            raise FlowFailure(f"{method} {path}: transport {type(error).__name__}: {self.clean(str(error))}") from error
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        self.collect_secrets(body)
        for cookie in client.cookies.jar:
            self.secret(cookie.value)
        entry = {"stage": self.stage_name, "actor": actor, "method": method,
                 "path": path, "status": response.status_code, "bytes": len(response.content)}
        if response.status_code >= 400:
            entry["error_response"] = self.clean(body if body is not None else response.text[:2000])
        self.evidence["http"].append(entry)
        accepted = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in accepted:
            detail = body if body is not None else response.text[:3000]
            raise FlowFailure(f"{method} {path}: expected {sorted(accepted)}, got {response.status_code}; response={self.clean(detail)}")
        return response

    def api(self, actor, method, path, expected=200, **kwargs):
        response = self.request(actor, method, path, expected, **kwargs)
        return response.json() if response.content else None

    def login(self, actor):
        role = "student" if actor in {"student", "peer"} else actor
        response = self.request(actor, "POST", "/api/v1/auth/login", json={
            "username": "flow_" + actor, "password": self.passwords[actor], "role": role,
        })
        self.check("HttpOnly" in response.headers.get("set-cookie", ""), f"{actor}: login must set HttpOnly cookie")
        self.check(bool(self.clients[actor].cookies.get("sid")), f"{actor}: missing actual session cookie")
        user = response.json()["user"]
        self.check(self.api(actor, "GET", "/api/v1/auth/me")["id"] == user["id"], "cookie did not authenticate /me")
        return user

    def start_server(self, data_dir):
        import httpx
        self.check(self.process is None, "previous server was not stopped")
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        env = dict(self.env, DATA_DIR=str(data_dir), STUDY_DATA_DIR=str(data_dir))
        self.server_log = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-m", "uvicorn", "app.main:app", "--app-dir", str(SERVER),
             "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--lifespan", "on",
             "--no-access-log", "--log-level", "warning", "--timeout-graceful-shutdown", "10"],
            cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=self.server_log, stderr=subprocess.STDOUT,
        )
        self.clients = {actor: httpx.Client(base_url=self.base, headers={"Origin": self.base},
                                         timeout=httpx.Timeout(20, connect=2), trust_env=False, follow_redirects=False)
                        for actor in ("anonymous", "admin", "teacher", "student", "peer")}
        end = time.monotonic() + self.args.timeout
        last = "no HTTP health response"
        while time.monotonic() < end:
            self.check(self.process.poll() is None, f"uvicorn exited before readiness (exit={self.process.returncode})")
            try:
                response = self.clients["anonymous"].get("/health")
            except httpx.TransportError as error:
                last = type(error).__name__
            else:
                last = f"HTTP {response.status_code}: {response.text[:1000]}"
                if response.status_code == 200:
                    health = response.json()
                    self.check(health.get("mode") == "platform", f"wrong application mode: {health}")
                    self.check(health["workers"]["enabled"] and health["workers"]["running"] == 1
                               and not health["workers"]["needs_attention"], f"worker not running: {health}")
                    self.evidence.setdefault("servers", []).append({"port": port, "pid": self.process.pid, "health": health})
                    return
            time.sleep(0.15)
        raise FlowFailure(f"uvicorn readiness timeout: {last}")

    def stop_server(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        if self.process is None:
            return
        process = self.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                self.evidence["failures"].append({"stage": self.stage_name, "type": "ShutdownTimeout", "message": "uvicorn exceeded terminate/wait timeout; kill/wait required"})
        else:
            self.evidence["failures"].append({"stage": self.stage_name, "type": "UnexpectedServerExit", "message": f"uvicorn exited unexpectedly: {process.returncode}"})
        self.check(process.poll() is not None, "uvicorn process was not reaped")
        self.evidence.setdefault("shutdowns", []).append({"pid": process.pid, "exit_code": process.returncode, "reaped": True})
        self.process = None
        if self.server_log is not None:
            self.server_log.flush()
            self.server_log.seek(0)
            # No access logs or request headers/bodies are enabled. Only retain
            # a bounded, redacted diagnostic suffix, never the raw log file.
            text = self.server_log.read().decode("utf-8", errors="replace")
            if text:
                self.evidence.setdefault("server_diagnostics", []).append(self.clean(text[-16000:]))
            self.server_log.close()
            self.server_log = None

    def poll(self, label, read, done, *, timeout=None):
        end = time.monotonic() + (self.args.timeout if timeout is None else timeout)
        last = None
        while time.monotonic() < end:
            self.check(self.process is not None and self.process.poll() is None, f"{label}: uvicorn stopped")
            last = read()
            if done(last):
                return last
            time.sleep(0.2)
        raise FlowFailure(f"{label}: timed out; last_response={self.clean(last)}")

    def sql(self, domain, query, parameters=()):
        path = self.data / (domain + ".sqlite3")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
            connection.execute("PRAGMA query_only=ON")
            return connection.execute(query, parameters).fetchall()

    @contextmanager
    def fault(self, domain, table, condition):
        # A missing function fails at statement preparation, including rows whose
        # WHEN condition is false. abs(INT64_MIN) instead raises OperationalError
        # only while executing this specific event, without changing app code.
        name = "platform_flow_fault_" + secrets.token_hex(8)
        path = self.data / (domain + ".sqlite3")
        self.check(path.is_relative_to(self.root), "fault database must be isolated")
        with closing(sqlite3.connect(path, timeout=10)) as connection:
            connection.execute(f'CREATE TRIGGER "{name}" BEFORE INSERT ON "{table}" WHEN {condition} BEGIN SELECT abs(-9223372036854775808); END')
            connection.commit()
        try:
            yield
        finally:
            with closing(sqlite3.connect(path, timeout=10)) as connection:
                connection.execute(f'DROP TRIGGER "{name}"')
                connection.commit()

    def unavailable(self, response):
        detail = response.json().get("detail", {})
        self.check(detail.get("code") == "storage_unavailable" and detail.get("retryable") is True,
                   f"expected retryable storage 503, got {self.clean(detail)}")

    def closed_answers(self, task):
        self.check(task.get("answer_released") is False, "answers released before full grading")
        for question in task["questions"]:
            self.check(not {"answer", "reference_answer", "explanation", "rubric"}.intersection(question), "private answer fields leaked")
        self.check(PRIVATE_MARKER not in json.dumps(task), "private reference marker leaked")
        for attempt in task["attempts"]:
            self.check(not {"items", "total_score", "total_score_units"}.intersection(attempt), "partial score leaked before grading")

    def task(self, actor, task_id):
        return self.api(actor, "GET", f"/api/v1/tasks/{task_id}")

    def grade(self, actor, task_id, score_units):
        task = self.poll("real worker grading", lambda: self.task(actor, task_id),
                         lambda item: item["status"] in {"results_ready", "awaiting_manual", "closed"})
        self.check(task["status"] == "results_ready", f"objective grading did not complete: {task}")
        self.check(task["answer_released"] and len(task["attempts"]) == 1, "graded task has wrong answer gate/attempt count")
        attempt = task["attempts"][0]
        self.check(attempt["status"] == "graded" and attempt["total_score_units"] == score_units
                   and attempt["total_score"] == score_units / 100, f"unexpected objective score: {attempt}")
        self.check({item["id"]: item["answer"] for item in task["questions"]} == self.answers, "released answer keys differ from frozen questions")
        job_id = f"grade:{task_id}:1"
        job = self.poll("queue grading success", lambda: self.api(actor, "GET", f"/api/v1/jobs/{job_id}"),
                        lambda item: item["status"] in {"succeeded", "failed", "cancelled", "reconciling"})
        self.check(job["status"] == "succeeded", f"grading queue failed: {job}")
        self.check(self.api(actor, "GET", f"/api/v1/jobs/{job_id}/calls")["items"] == [], "objective grade used an upstream call")
        self.check(self.api(actor, "GET", f"/api/v1/tasks/{task_id}/answer-policy")["results_released"], "answer policy still closed after grading")
        return task

    def draft_and_submit(self, actor, task_id, answers, *, lease=None, inject_fault=False):
        lease = lease or self.api(actor, "POST", f"/api/v1/tasks/{task_id}/edit-leases", json={"device_id": "flow-" + actor, "lease_seconds": 300})
        draft_payload = {"expected_version": lease["task_version"], "request_id": actor + "-draft",
                         "lease_token": lease["lease_token"], "fence_token": lease["fence_token"], "answers": answers}
        draft = self.api(actor, "PUT", f"/api/v1/tasks/{task_id}/draft", json=draft_payload)
        replay = self.api(actor, "PUT", f"/api/v1/tasks/{task_id}/draft", json=draft_payload)
        self.check(replay["version"] == draft["version"] and replay["draft_answers"] == answers, "draft replay changed state")
        self.api(actor, "PUT", f"/api/v1/tasks/{task_id}/draft", 409,
                 json={**draft_payload, "answers": {**answers, next(iter(answers)): 2}})
        self.closed_answers(self.task(actor, task_id))
        payload = {"expected_version": draft["version"], "request_id": actor + "-submit",
                   "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}
        if inject_fault:
            before = self.task(actor, task_id)
            counts = self.submission_counts(task_id, payload["request_id"])
            with self.fault("learning", "grading_outbox", f"NEW.job_id='grade:{task_id}:1'"):
                self.unavailable(self.request(actor, "POST", f"/api/v1/tasks/{task_id}/submit", 503, json=payload))
                self.check(self.task(actor, task_id) == before, "failed submit changed task, draft, version or attempts")
                self.check(self.submission_counts(task_id, payload["request_id"]) == counts, "failed submit committed attempt/items/outbox/idempotency")
        submitted = self.api(actor, "POST", f"/api/v1/tasks/{task_id}/submit", 202, json=payload)
        self.check(submitted["status"] == "grading", f"submission did not return grading state: {submitted}")
        self.closed_answers(submitted)
        replay = self.api(actor, "POST", f"/api/v1/tasks/{task_id}/submit", 202, json=payload)
        self.check([item["id"] for item in replay["attempts"]] == [item["id"] for item in submitted["attempts"]], "submit replay duplicated attempt")
        self.api(actor, "POST", f"/api/v1/tasks/{task_id}/submit", 409, json={**payload, "fence_token": lease["fence_token"] + 1})
        self.check(self.submission_counts(task_id, payload["request_id"]) == (1, 2, 1, 1), "submission produced duplicate durable records")
        return submitted

    def submission_counts(self, task_id, request_id):
        return tuple(self.sql("learning", query, parameters)[0][0] for query, parameters in (
            ("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)),
            ("SELECT COUNT(*) FROM attempt_items WHERE task_id=?", (task_id,)),
            ("SELECT COUNT(*) FROM grading_outbox o JOIN attempts a ON a.id=o.attempt_id WHERE a.task_id=?", (task_id,)),
            ("SELECT COUNT(*) FROM idempotency WHERE task_id=? AND request_id=?", (task_id, request_id)),
        ))

    def report(self, key):
        source = self.api("teacher", "GET", "/api/v1/reports/source-revision", params=self.scope)
        payload = {"scope": self.scope, "schema_version": source["schema_version"],
                   "rule_version": source["default_rule_version"], "expected_source_revision": source["source_revision"]}
        report = self.api("teacher", "POST", "/api/v1/reports/generate", 201, json=payload,
                          headers={"Idempotency-Key": key})
        self.check(report["snapshot"]["source_revision"] == source["source_revision"], "report source token changed")
        self.check(digest(canonical(report["snapshot"])) == report["snapshot_sha256"], "snapshot checksum is not its canonical JSON hash")
        return report, payload

    def export(self, report, fmt):
        response = self.request("teacher", "GET", f"/api/v1/reports/{report['report_id']}/export/{fmt}", params={"revision_id": report["id"]})
        self.check(response.headers.get("X-Report-Snapshot-SHA256") == report["snapshot_sha256"], "export snapshot checksum header differs")
        self.check(response.headers.get("X-Report-Revision") == report["id"], "export revision header differs")
        self.check(response.headers.get("Cache-Control") == "no-store" and "attachment;" in response.headers.get("Content-Disposition", ""), "unsafe export response headers")
        return response

    def run(self):
        with self.stage("bootstrap") as row:
            from app.config import Settings
            from app.init_admin import initialize_admin
            settings = Settings()
            self.check(not settings.llm_configured and not settings.maxkb_url and not settings.maxkb_api_key,
                       "upstream was not explicitly disabled before importing app.config")
            self.check(settings.worker_enabled and not settings.demo_enabled, "formal worker config is wrong")
            self.data.mkdir()
            initialize_admin(self.data / "platform.sqlite3", username="flow_admin", display_name="Synthetic Administrator", password=self.passwords["admin"])
            self.evidence["dependencies"] = {name: version(name) for name in ("fastapi", "uvicorn", "httpx", "openpyxl")}
            row["upstream_configured"] = False
            # Verify exception type and event isolation before injecting any DB.
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("CREATE TABLE events(event_type TEXT)")
                connection.execute("CREATE TRIGGER fault BEFORE INSERT ON events WHEN NEW.event_type='target' BEGIN SELECT abs(-9223372036854775808); END")
                connection.execute("INSERT INTO events VALUES('other')")
                try:
                    connection.execute("INSERT INTO events VALUES('target')")
                except sqlite3.OperationalError:
                    row["fault_exception"] = "OperationalError (event-specific integer overflow)"
                else:
                    raise FlowFailure("fault probe did not raise OperationalError")

        with self.stage("server_start"):
            self.start_server(self.data)
            self.api("anonymous", "GET", "/api/v1/tasks", 401)
            self.api("anonymous", "GET", "/api/study/sample", 404)
            self.users = {"admin": self.login("admin")}

        with self.stage("teacher_application_approval_rollback"):
            application = self.api("anonymous", "POST", "/api/v1/auth/teacher-applications", 202, json={
                "username": "flow_teacher", "display_name": "Synthetic Teacher", "password": self.passwords["teacher"],
            })
            self.api("teacher", "POST", "/api/v1/auth/login", 403, json={"username": "flow_teacher", "password": self.passwords["teacher"]})
            before = self.api("admin", "GET", "/api/v1/auth/teacher-applications")
            path = f"/api/v1/auth/teacher-applications/{application['id']}/approve"
            with self.fault("platform", "audit_events", "NEW.event_type='teacher_application_approved'"):
                self.unavailable(self.request("admin", "POST", path, 503))
                self.check(self.api("admin", "GET", "/api/v1/auth/teacher-applications") == before, "teacher approval failed to roll back")
            self.api("admin", "POST", path)
            self.users["teacher"] = self.login("teacher")

        with self.stage("course_application_approval_rollback"):
            application = self.api("teacher", "POST", "/api/v1/course-applications", 202, json={"name": "Synthetic Mathematics", "term": "2026-flow"})
            path = f"/api/v1/course-applications/{application['id']}/review"
            self.api("teacher", "POST", path, 403, json={"approve": True})
            before_courses = self.api("admin", "GET", "/api/v1/courses")
            before_applications = self.api("admin", "GET", "/api/v1/course-applications")
            with self.fault("platform", "audit_events", "NEW.event_type='course_application_approved'"):
                self.unavailable(self.request("admin", "POST", path, 503, json={"approve": True}))
                self.check(self.api("admin", "GET", "/api/v1/courses") == before_courses, "failed course approval committed a course")
                self.check(self.api("admin", "GET", "/api/v1/course-applications") == before_applications, "failed course approval changed application")
            approved = self.api("admin", "POST", path, json={"approve": True, "note": "Synthetic acceptance"})
            self.course_id = approved["course_id"]
            self.api("admin", "POST", path, 409, json={"approve": True})

        with self.stage("class_and_student_creation_rollback"):
            classroom = self.api("teacher", "POST", f"/api/v1/courses/{self.course_id}/classes", 201,
                                 json={"name": "Synthetic Class", "owner_teacher_id": self.users["teacher"]["id"]})
            self.class_id = classroom["id"]
            before = self.api("teacher", "GET", f"/api/v1/classes/{self.class_id}/members")
            counts = self.platform_counts()
            payload = {"username": "flow_student", "display_name": "Synthetic Student", "course_id": self.course_id, "class_id": self.class_id}
            with self.fault("platform", "audit_events", "NEW.event_type='student_created'"):
                self.unavailable(self.request("teacher", "POST", "/api/v1/auth/students", 503, json=payload))
                self.check(self.api("teacher", "GET", f"/api/v1/classes/{self.class_id}/members") == before, "failed student creation changed roster")
                self.check(self.platform_counts() == counts, "failed student creation committed user/memberships/code/audit")
            self.created = {"student": self.api("teacher", "POST", "/api/v1/auth/students", 201, json=payload)}
            self.created["peer"] = self.api("teacher", "POST", "/api/v1/auth/students", 201,
                                           json={**payload, "username": "flow_peer", "display_name": "Synthetic Peer"})

        with self.stage("activation_rollback_and_cookie_login"):
            for actor, created in self.created.items():
                payload = {"code": created["activation_code"], "password": self.passwords[actor]}
                if actor == "student":
                    before = self.sql("platform", "SELECT status,authorization_version FROM users WHERE id=?", (created["student_id"],))
                    with self.fault("platform", "audit_events", "NEW.event_type='account_activated'"):
                        self.unavailable(self.request("anonymous", "POST", "/api/v1/auth/activate", 503, json=payload))
                        self.check(self.sql("platform", "SELECT status,authorization_version FROM users WHERE id=?", (created["student_id"],)) == before, "failed activation changed account")
                        self.check(self.sql("platform", "SELECT consumed_at FROM activation_codes WHERE user_id=?", (created["student_id"],)) == [(None,)], "failed activation consumed code")
                        self.api(actor, "POST", "/api/v1/auth/login", 401, json={"username": "flow_" + actor, "password": self.passwords[actor]})
                self.api("anonymous", "POST", "/api/v1/auth/activate", 204, json=payload)
                self.api("anonymous", "POST", "/api/v1/auth/activate", 400, json=payload)
                self.users[actor] = self.login(actor)

        with self.stage("material_upload_and_confirmation") as row:
            self.material_bytes = b"Synthetic mathematics handout. Add and compare whole numbers.\n"
            material = self.api("teacher", "POST", f"/api/v1/courses/{self.course_id}/materials", 201,
                                files={"file": ("synthetic-lesson.txt", self.material_bytes, "text/plain")},
                                data={"visibility": "shared", "license_code": "own", "rights_confirmed": "true",
                                      "allow_generation": "false", "allow_retrieval": "true", "allow_student_view": "true",
                                      "external_processing_allowed": "false"})
            self.material_id = material["material"]["id"]
            self.api("student", "GET", f"/api/v1/materials/{self.material_id}", 404)
            self.api("teacher", "POST", f"/api/v1/materials/{self.material_id}/confirm", json={
                "expected_revision": material["material"]["revision"], "block_ids": [item["id"] for item in material["blocks"]],
                "public_block_ids": [item["id"] for item in material["blocks"]], "public_content_attested": True,
            })
            self.material_student = self.api("student", "GET", f"/api/v1/materials/{self.material_id}")
            self.material_teacher = self.api("teacher", "GET", f"/api/v1/materials/{self.material_id}")
            self.check("storage_key" not in json.dumps(self.material_teacher), "private material storage path leaked")
            self.check(self.material_teacher["material"]["sha256"] == digest(self.material_bytes), "HTTP-uploaded material hash differs")
            self.check(self.material_student["blocks"] and "Synthetic mathematics" in self.material_student["blocks"][0]["text"], "confirmed student material missing")
            self.api("student", "GET", f"/api/v1/materials/{self.material_id}/original", 404)
            self.api("student", "GET", f"/api/v1/materials/{self.material_id}/issues", 403)
            self.source_block = material["blocks"][0]
            row["original_sha256"] = digest(self.material_bytes)

        with self.stage("synthetic_approved_question_fixture") as row:
            from app.config import Settings
            from app.runtime import Runtime
            fixture_runtime = Runtime(Settings())
            self.refs, self.future_refs = [], []
            for index, answer, points in ((1, 1, 2), (2, 0, 3), (3, 1, 1), (4, 1, 2), (5, 0, 3)):
                qid = f"synthetic-flow-choice-{index}"
                fixture_runtime.learning.create_question_version(self.users["teacher"]["id"], qid, 1, {
                    "type": "single_choice", "stem": f"Synthetic objective check {index}",
                    "options": ["Option A", "Option B", "Option C"], "answer": answer, "points": points,
                    "family_id": f"synthetic-family-{index}", "explanation": PRIVATE_MARKER,
                    "rubric": [{"point": PRIVATE_MARKER, "score": points}], "external_processing_permission": False,
                    "source_anchors": [{"block_id": self.source_block["id"], "file_id": self.material_id,
                                        "anchor": self.source_block["anchor"], "export_allowed": True}],
                }, status="approved", course_id=self.course_id)
                ref = {"question_id": qid, "version": 1}
                if index <= 2:
                    self.refs.append(ref)
                elif index == 3:
                    self.diagnostic_ref = ref
                else:
                    self.future_refs.append(ref)
            self.answers = {self.refs[0]["question_id"]: 1, self.refs[1]["question_id"]: 0}
            listing = self.api("teacher", "GET", f"/api/v1/courses/{self.course_id}/questions")["items"]
            self.check(len(listing) == 5 and all(item["status"] == "approved" for item in listing), "HTTP cannot see seeded approved fixture")
            row["fixture_count"] = 5
            row["approval_boundary"] = "direct real LearningStore fixture, not model quality acceptance"

        with self.stage("goal_publish_replay_and_origin"):
            payload = {"request_id": "flow-goal-create", "title": "Synthetic arithmetic goal", "description": "Two objective questions",
                       "course_id": self.course_id, "class_id": self.class_id}
            self.api("teacher", "POST", "/api/v1/goals", 403, json=payload, origin=False)
            self.api("teacher", "POST", "/api/v1/goals", 403, json=payload, headers={"Origin": "https://foreign.invalid"})
            self.api("student", "POST", "/api/v1/goals", 403, json=payload)
            self.goal = self.api("teacher", "POST", "/api/v1/goals", 201, json=payload)
            self.check(self.api("teacher", "POST", "/api/v1/goals", 201, json=payload) == self.goal, "goal replay differs")
            self.api("teacher", "POST", "/api/v1/goals", 409, json={**payload, "title": "Conflicting goal"})
            self.goal_id = self.goal["id"]
            publish = {"request_id": "flow-publish", "expected_version": self.goal["version"], "round_no": 1,
                       "student_ids": [self.users[name]["id"] for name in ("student", "peer")], "question_versions": self.refs,
                       "diagnostic_question_versions": [self.diagnostic_ref], "deadline_at": time.time() + 600,
                       "allow_late": False, "allow_retries": True, "allow_retry_after_deadline": False, "common_question_ratio": 100}
            published = self.api("teacher", "POST", f"/api/v1/goals/{self.goal_id}/rounds", 201, json=publish)
            self.check(self.api("teacher", "POST", f"/api/v1/goals/{self.goal_id}/rounds", 201, json=publish) == published, "round replay differs")
            self.api("teacher", "POST", f"/api/v1/goals/{self.goal_id}/rounds", 409, json={**publish, "allow_late": True})
            self.tasks = {}
            for actor in ("student", "peer"):
                tasks = self.api(actor, "GET", "/api/v1/tasks", params={"goal_id": self.goal_id})["items"]
                self.check(len(tasks) == 1 and tasks[0]["student_id"] == self.users[actor]["id"], "task publication leaked or duplicated tasks")
                self.tasks[actor] = tasks[0]["id"]

        with self.stage("diagnostic_gate_and_submission") as row:
            for actor, task_id in self.tasks.items():
                task = self.task(actor, task_id)
                self.closed_answers(task)
                self.check(task["diagnostic_required"] and task["questions"] == [], "formal paper visible before diagnostic")
                self.api(actor, "POST", f"/api/v1/tasks/{task_id}/edit-leases", 409, json={"device_id": "too-early"})
                diagnostic = self.api(actor, "POST", f"/api/v1/tasks/{task_id}/diagnostics", 201,
                                      json={"request_id": actor + "-diagnostic-start", "expected_version": task["version"]})
                self.check(diagnostic["counts_toward_formal_score"] is False and diagnostic["hints_allowed"] is False, "diagnostic counted as formal or allowed hints")
                self.check({item["id"] for item in diagnostic["questions"]} == {self.diagnostic_ref["question_id"]}, "diagnostic did not use separate question")
                self.check(PRIVATE_MARKER not in json.dumps(diagnostic) and all("answer" not in item for item in diagnostic["questions"]), "diagnostic leaked answers")
                done = self.api(actor, "POST", f"/api/v1/diagnostics/{diagnostic['id']}/submit", json={
                    "request_id": actor + "-diagnostic-submit", "answers": {self.diagnostic_ref["question_id"]: 1}, "unknown_question_ids": [],
                })
                self.check(done["status"] == "completed" and done["feedback"]["correct"] == 1, "diagnostic did not complete")
                formal = self.task(actor, task_id)
                self.check(not formal["diagnostic_required"] and len(formal["questions"]) == 2 and not formal["attempts"], "diagnostic did not unlock separate formal paper")
                self.closed_answers(formal)
            row["formal_attempts_from_diagnostics"] = 0

        with self.stage("continuous_plan_configuration"):
            goal = self.api("teacher", "GET", f"/api/v1/goals/{self.goal_id}")
            self.plan_started = time.monotonic()
            self.api("teacher", "PUT", f"/api/v1/goals/{self.goal_id}/schedule", json={
                "request_id": "flow-continuous-plan", "expected_version": goal["version"], "future_round_count": 1,
                "interval_seconds": 60, "ends_at": time.time() + 600, "question_pool": self.future_refs,
            })

        task_id = self.tasks["student"]
        with self.stage("authorization_and_fenced_lease"):
            lease = self.api("student", "POST", f"/api/v1/tasks/{task_id}/edit-leases", json={"device_id": "browser-a", "lease_seconds": 300})
            self.api("student", "POST", f"/api/v1/tasks/{task_id}/edit-leases", 409, json={"device_id": "browser-b"})
            replacement = self.api("student", "POST", f"/api/v1/tasks/{task_id}/edit-leases", json={"device_id": "browser-b", "takeover": True, "lease_seconds": 300})
            self.check(replacement["fence_token"] > lease["fence_token"], "lease takeover did not advance fence")
            stale = {"request_id": "stale-fence", "expected_version": replacement["task_version"], "lease_token": lease["lease_token"],
                     "fence_token": lease["fence_token"], "answers": self.answers}
            self.api("student", "PUT", f"/api/v1/tasks/{task_id}/draft", 409, json=stale)
            self.api("student", "POST", f"/api/v1/tasks/{task_id}/edit-leases/renew", json={
                "lease_token": replacement["lease_token"], "fence_token": replacement["fence_token"], "lease_seconds": 300,
            })
            self.api("peer", "GET", f"/api/v1/tasks/{task_id}", 404)
            self.api("peer", "GET", f"/api/v1/tasks/{task_id}/answer-policy", 404)
            self.api("peer", "POST", f"/api/v1/tasks/{task_id}/edit-leases", 404, json={"device_id": "intruder"})
            self.api("peer", "PUT", f"/api/v1/tasks/{task_id}/draft", 404, json=stale)
            self.api("peer", "POST", f"/api/v1/tasks/{task_id}/submit", 404, json={key: value for key, value in stale.items() if key != "answers"})
            self.api("peer", "GET", f"/api/v1/goals/{self.goal_id}/evidence", 403, params={"student_id": self.users["student"]["id"]})
            self.api("teacher", "POST", f"/api/v1/tasks/{task_id}/edit-leases", 403, json={"device_id": "teacher"})
            self.api("anonymous", "GET", f"/api/v1/tasks/{task_id}", 401)

        with self.stage("draft_submit_rollback_replay_and_worker") as row:
            self.draft_and_submit("student", task_id, self.answers, lease=replacement, inject_fault=True)
            self.graded_student = self.grade("student", task_id, 500)
            row.update(total_score_units=500, attempts=1, outbox_records=1, upstream_calls=0)

        with self.stage("peer_submission_and_worker") as row:
            self.draft_and_submit("peer", self.tasks["peer"], {**self.answers, self.refs[0]["question_id"]: 0})
            self.graded_peer = self.grade("peer", self.tasks["peer"], 300)
            self.api("peer", "GET", f"/api/v1/jobs/grade:{task_id}:1", 404)
            row.update(total_score_units=300, attempts=1, upstream_calls=0)

        with self.stage("reports_from_real_learning_evidence") as row:
            self.scope = {"kind": "goal", "course_id": self.course_id, "class_id": self.class_id, "goal_id": self.goal_id}
            self.first_report, self.first_report_payload = self.report("flow-report-original")
            evidence = self.first_report["snapshot"]["tables"]["evidence"]["rows"]
            attempt_ids = {self.graded_student["attempts"][0]["id"], self.graded_peer["attempts"][0]["id"]}
            self.check(len(evidence) == 4 and {item["attempt_id"] for item in evidence} == attempt_ids, "report did not use same-run real learning attempts")
            self.check({item["student_id"] for item in evidence} == {self.users[name]["id"] for name in ("student", "peer")}, "report subjects differ")
            self.check(sorted(item["score_ratio"] for item in evidence) == [0, 1, 1, 1], "report scores differ from actual grading")
            self.check(all(item["exposure"] == "independent" for item in evidence), "independent attempts misclassified")
            self.check(PRIVATE_MARKER not in json.dumps(self.first_report) and "Synthetic objective check" not in json.dumps(self.first_report), "raw private question content entered report")
            replay = self.request("teacher", "POST", "/api/v1/reports/generate", 200, json=self.first_report_payload, headers={"Idempotency-Key": "flow-report-original"})
            self.check(replay.headers.get("Idempotency-Replayed") == "true" and replay.json()["id"] == self.first_report["id"], "report replay not idempotent")
            self.api("student", "GET", f"/api/v1/reports/{self.first_report['report_id']}", 404)
            self.api("student", "GET", f"/api/v1/reports/{self.first_report['report_id']}/export/json", 404)
            self.api("student", "POST", "/api/v1/reports/generate", 403, json=self.first_report_payload, headers={"Idempotency-Key": "flow-student-denied"})
            row.update(source_revision=self.first_report["source_revision"], evidence_rows=4, snapshot_sha256=self.first_report["snapshot_sha256"])

        with self.stage("five_http_exports") as row:
            self.exports = {}
            for fmt in ("html", "pdf", "xlsx", "json", "csv"):
                response = self.export(self.first_report, fmt)
                payload = response.content
                self.check(len(payload) > 0, f"{fmt} export is empty")
                if fmt == "json":
                    self.check(response.json()["snapshot"] == self.first_report["snapshot"], "JSON export differs from frozen snapshot")
                elif fmt == "pdf":
                    self.check(payload.startswith(b"%PDF-"), "PDF export is not a PDF")
                elif fmt == "html":
                    self.check(b'<!doctype html>' in payload.lower() and self.first_report["snapshot_sha256"].encode() in payload, "HTML export missing report identity")
                else:
                    with ZipFile(BytesIO(payload)) as archive:
                        self.check(archive.testzip() is None, f"{fmt} archive checksum failure")
                        if fmt == "xlsx":
                            self.check("xl/workbook.xml" in archive.namelist(), "XLSX archive has no workbook")
                        else:
                            manifest = json.loads(archive.read("manifest.json"))
                            self.check(manifest["snapshot_sha256"] == self.first_report["snapshot_sha256"], "CSV manifest snapshot mismatch")
                            for filename, metadata in manifest["files"].items():
                                self.check(digest(archive.read(filename)) == metadata["sha256"], "CSV file differs from export manifest")
                self.exports[fmt] = payload
            row["artifacts"] = {fmt: {"bytes": len(payload), "sha256": digest(payload)} for fmt, payload in self.exports.items()}

        with self.stage("xlsx_repeat_across_seconds", fatal=False) as row:
            # More than one actual wall-clock second, not a mocked clock or two
            # requests incidentally completed in the same timestamp bucket.
            start = time.monotonic()
            time.sleep(1.2)
            repeated = self.export(self.first_report, "xlsx").content
            row.update(elapsed_between_requests_seconds=round(time.monotonic() - start, 3),
                       original_sha256=digest(self.exports["xlsx"]), repeated_sha256=digest(repeated))
            self.check(repeated == self.exports["xlsx"], "same frozen report XLSX bytes changed across >1 second")

        with self.stage("appeal_worker_teacher_handoff") as row:
            attempt = self.graded_student["attempts"][0]
            appeal = self.api("student", "POST", f"/api/v1/attempts/{attempt['id']}/appeals", 202, json={
                "request_id": "flow-appeal", "question_id": self.refs[0]["question_id"], "grading_revision": attempt["revision"],
                "reason": "Synthetic request to verify the frozen objective score",
            })
            result = self.poll("real worker appeal handoff", lambda: self.api("student", "GET", f"/api/v1/tasks/{task_id}/appeals")["items"],
                               lambda items: any(item["id"] == appeal["id"] and item["status"] == "awaiting_teacher" for item in items))
            job = self.poll("appeal queue success", lambda: self.api("teacher", "GET", f"/api/v1/jobs/appeal:{appeal['id']}"),
                            lambda item: item["status"] in {"succeeded", "failed"})
            self.check(job["status"] == "succeeded", f"appeal worker failed: {job}")
            self.check(self.task("student", task_id)["attempts"][0]["total_score_units"] == 500, "appeal changed formal score without teacher correction")
            self.check(self.api("teacher", "GET", f"/api/v1/jobs/appeal:{appeal['id']}/calls")["items"] == [], "objective appeal invoked model")
            row.update(status_result="awaiting_teacher", review="real worker objective programmatic recheck; not a claim of model fallback quality")

        with self.stage("correction_and_frozen_report_revision") as row:
            attempt = self.task("student", task_id)["attempts"][0]
            correction_payload = {"request_id": "flow-correction", "expected_revision": attempt["revision"],
                                  "score_units": 100, "reason": "Synthetic append-only correction to verify frozen report history"}
            path = f"/api/v1/attempts/{attempt['id']}/items/{self.refs[0]['question_id']}/corrections"
            self.api("student", "POST", path, 403, json=correction_payload)
            correction = self.api("teacher", "POST", path, json=correction_payload)
            self.check(self.api("teacher", "POST", path, json=correction_payload) == correction, "correction replay differs")
            self.check(self.task("student", task_id)["attempts"][0]["total_score_units"] == 400, "teacher correction did not update total")
            self.check(len(self.api("teacher", "GET", f"/api/v1/attempts/{attempt['id']}/corrections")["items"]) == 1, "correction replay appended twice")
            self.new_report, _ = self.report("flow-report-corrected")
            self.check(self.new_report["source_revision"] > self.first_report["source_revision"], "real learning source revision did not increase")
            self.check(self.new_report["snapshot_sha256"] != self.first_report["snapshot_sha256"], "corrected evidence did not change report")
            self.check(self.export(self.first_report, "json").content == self.exports["json"], "historical report export was rewritten")
            old = self.api("teacher", "GET", f"/api/v1/reports/{self.first_report['report_id']}/revisions/{self.first_report['id']}")
            self.check(old["snapshot"] == self.first_report["snapshot"] and old["snapshot_sha256"] == self.first_report["snapshot_sha256"], "old snapshot changed after correction")
            replay = self.api("teacher", "POST", "/api/v1/reports/generate", 200, json=self.first_report_payload, headers={"Idempotency-Key": "flow-report-original"})
            self.check(replay["id"] == self.first_report["id"], "report replay selected new evidence instead of frozen original")
            row.update(old_source_revision=self.first_report["source_revision"], new_source_revision=self.new_report["source_revision"], corrected_total_score_units=400)

        with self.stage("input_confirmation_and_audio_unavailable"):
            self.check(self.api("student", "GET", "/api/v1/input/math/templates")["confirmation_required"], "formula templates skipped confirmation")
            formula = self.api("student", "POST", "/api/v1/input/math/confirm", json={"raw": "x + x"})
            self.check(formula["status"] == "confirmation_required", "unconfirmed formula was accepted")
            formula = self.api("student", "POST", "/api/v1/input/math/confirm", json={"raw": "x + x", "confirmed": "2*x"})
            self.check(formula["status"] == "confirmed" and formula["supported_for_programmatic_check"], "formula confirmation failed")
            response = self.api("student", "POST", "/api/v1/input/audio/transcriptions", 503,
                                files={"file": ("synthetic.wav", b"RIFF\x00\x00\x00\x00WAVE", "audio/wav")})
            self.check(response["detail"]["code"] == "asr_not_configured", "audio did not explicitly report unavailable ASR")

        with self.stage("continuous_plan_real_elapsed_round") as row:
            goal = self.poll("continuous plan next round", lambda: self.api("teacher", "GET", f"/api/v1/goals/{self.goal_id}"),
                             lambda item: len(item["rounds"]) == 2, timeout=self.args.plan_timeout)
            elapsed = time.monotonic() - self.plan_started
            self.check(elapsed >= 59.5, "next round appeared without the real 60-second interval")
            self.check({item["round_no"] for item in goal["rounds"]} == {1, 2}, "continuous plan duplicated or skipped a round")
            self.restored_task_ids = {}
            for actor in ("student", "peer"):
                tasks = self.api(actor, "GET", "/api/v1/tasks", params={"goal_id": self.goal_id})["items"]
                self.check(len(tasks) == 2, "continuous plan did not publish exactly one future task per student")
                future = next(item for item in tasks if item["id"] != self.tasks[actor])
                self.closed_answers(future)
                self.check({item["id"] for item in future["questions"]} == {ref["question_id"] for ref in self.future_refs}, "future round reused old or unapproved questions")
                self.restored_task_ids[actor] = sorted(item["id"] for item in tasks)
            self.check(self.sql("learning", "SELECT remaining_rounds FROM goal_plans WHERE goal_id=?", (self.goal_id,)) == [(0,)], "future round budget was not decremented")
            self.check(self.api("anonymous", "GET", "/health")["workers"]["needs_attention"] is False, "background worker reported an error")
            row.update(elapsed_real_seconds=round(elapsed, 3), rounds=2, new_tasks=2, future_round_budget_remaining=0)

        with self.stage("stop_before_backup"):
            self.stop_server()
            self.check(not self.sql("platform", "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'platform_flow_fault_%'"), "fault trigger left in source DB")
            self.check(not self.sql("learning", "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'platform_flow_fault_%'"), "learning fault trigger left in source DB")

        with self.stage("six_database_backup_inspect_restore") as row:
            from app.backup import BackupManager
            manager = BackupManager({name: self.data / f"{name}.sqlite3" for name in DATABASES}, self.data,
                                    additional_attachment_dirs={"material_originals": self.data / "material_originals"})
            bundle = manager.write_backup(self.root / "verified-backup.zip")
            self.inspection = manager.inspect(bundle)
            self.check(self.inspection["verified"] and set(self.inspection["databases"]) == set(DATABASES), "backup did not verify six stores")
            attachments = self.inspection["attachments"]
            self.check(any(item["path"].startswith("attachments/material_originals/") and item["sha256"] == digest(self.material_bytes) for item in attachments), "real HTTP uploaded original missing from backup")
            self.check(not any(item["path"].startswith("attachments/shared/material_originals/") for item in attachments), "original duplicated into shared backup root")
            staging = self.root / "staging"
            staging.mkdir()
            self.restored = manager.restore_to_staging(bundle, staging)
            row.update(bundle_sha256=digest(bundle.read_bytes()), databases=self.inspection["databases"], attachments=attachments,
                       member_count=self.inspection["member_count"], verified=True)

        with self.stage("restore_sidecars_and_hashes", fatal=False) as row:
            sidecars = [item.relative_to(self.restored).as_posix() for item in self.restored.rglob("*")
                        if item.is_file() and item.name.endswith(("-wal", "-shm", "-journal"))]
            row["sidecars"] = sidecars
            hashes = {}
            for item in [*self.inspection["databases"].values(), *self.inspection["attachments"]]:
                payload = (self.restored / item["path"]).read_bytes()
                hashes[item["path"]] = digest(payload)
                self.check(len(payload) == item["size"] and hashes[item["path"]] == item["sha256"], f"restored hash/size mismatch: {item['path']}")
            row["verified_hashes"] = hashes
            self.check(sidecars == [], f"BackupManager returned SQLite sidecars: {sidecars}")

        with self.stage("restore_runtime_layout"):
            self.restored_data = self.root / "restored-runtime"
            self.restored_data.mkdir()
            # Backup staging is not the runtime layout. Shared-root members map
            # to DATA_DIR; dedicated originals map to DATA_DIR/material_originals.
            shared = self.restored / "attachments" / "shared"
            if shared.exists():
                shutil.copytree(shared, self.restored_data, dirs_exist_ok=True)
            originals = self.restored / "attachments" / "material_originals"
            self.check(originals.is_dir(), "restored private material-original root missing")
            shutil.copytree(originals, self.restored_data / "material_originals")
            for name in DATABASES:
                target = self.restored_data / f"{name}.sqlite3"
                self.check(not target.exists(), "shared attachments collided with runtime database")
                shutil.copy2(self.restored / "databases" / target.name, target)
                self.check(digest(target.read_bytes()) == self.inspection["databases"][name]["sha256"], "runtime copy changed restored database")
            self.check(not any(item.name.endswith(("-wal", "-shm", "-journal")) for item in self.restored_data.rglob("*")), "SQLite sidecars copied into runtime")

        with self.stage("restored_real_service_http_checks") as row:
            self.start_server(self.restored_data)
            for actor in ("admin", "teacher", "student", "peer"):
                self.check(self.login(actor)["id"] == self.users[actor]["id"], "restored HTTP login returned different account")
            for actor, expected_score in (("student", 400), ("peer", 300)):
                tasks = self.api(actor, "GET", "/api/v1/tasks", params={"goal_id": self.goal_id})["items"]
                self.check(sorted(item["id"] for item in tasks) == self.restored_task_ids[actor], "restored task listing differs")
                task = self.task(actor, self.tasks[actor])
                self.check(task["status"] == "results_ready" and len(task["attempts"]) == 1
                           and task["attempts"][0]["total_score_units"] == expected_score, "restored score/attempt changed")
                self.check(self.api(actor, "GET", f"/api/v1/jobs/grade:{self.tasks[actor]}:1")["status"] == "succeeded", "restored grading job missing")
            old = self.api("teacher", "GET", f"/api/v1/reports/{self.first_report['report_id']}/revisions/{self.first_report['id']}")
            new = self.api("teacher", "GET", f"/api/v1/reports/{self.new_report['report_id']}/revisions/{self.new_report['id']}")
            self.check(old["snapshot"] == self.first_report["snapshot"] and new["snapshot"] == self.new_report["snapshot"], "restored report revisions changed")
            self.check(self.export(self.first_report, "json").content == self.exports["json"], "restored historical JSON bytes differ")
            self.check(self.api("student", "GET", f"/api/v1/materials/{self.material_id}") == self.material_student, "restored student material differs")
            self.check(self.api("teacher", "GET", f"/api/v1/materials/{self.material_id}") == self.material_teacher, "restored teacher material differs")
            self.check(self.api("anonymous", "GET", "/health")["workers"]["needs_attention"] is False, "restored worker unhealthy")
            row.update(logins=4, tasks=4, grades=[400, 300], historical_reports_verified=2, material_sha256=digest(self.material_bytes))

        with self.stage("final_shutdown"):
            self.stop_server()

    def platform_counts(self):
        return tuple(self.sql("platform", f"SELECT COUNT(*) FROM {table}")[0][0] for table in
                     ("users", "course_memberships", "class_memberships", "activation_codes", "audit_events"))

    def finish(self):
        try:
            self.stop_server()
        except Exception as error:
            self.evidence["failures"].append({"stage": "cleanup", "type": type(error).__name__, "message": self.clean(str(error))})
        completed = {item["stage"] for item in self.evidence["stages"]}
        self.evidence["not_run_stages"] = [name for name in STAGES if name not in completed]
        self.evidence.update(status="failed" if self.evidence["failures"] or self.evidence["not_run_stages"] else "passed",
                             finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - self.started, 3),
                             http_requests=len(self.evidence["http"]))
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, help="new directory for redacted JSON evidence; must not exist")
    parser.add_argument("--timeout", type=float, default=30, help="bounded readiness/worker polling timeout (seconds)")
    parser.add_argument("--plan-timeout", type=float, default=85, help="bounded continuous-plan poll (real interval is 60 seconds)")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 120 or not 65 <= args.plan_timeout <= 180:
        parser.error("timeout must be 5..120 and plan-timeout must be 65..180 seconds")
    if args.output_dir is None:
        output = Path(tempfile.mkdtemp(prefix="platform-flow-evidence-"))
    else:
        output = args.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=False)
    print(f"Evidence directory: {output}", flush=True)
    flow = None
    try:
        with tempfile.TemporaryDirectory(prefix="platform-real-flow-") as directory:
            try:
                flow = Flow(Path(directory), output, args)
                flow.run()
            except KeyboardInterrupt:
                if flow:
                    flow.evidence["failures"].append({"stage": flow.stage_name, "type": "KeyboardInterrupt", "message": "run interrupted"})
            except Exception as error:
                if flow and not flow.evidence["failures"]:
                    flow.evidence["failures"].append({"stage": flow.stage_name, "type": type(error).__name__, "message": flow.clean(str(error))})
                elif flow is None:
                    raise
            finally:
                if flow:
                    flow.finish()
        # TemporaryDirectory has now removed isolated databases and secrets.
    except Exception as error:
        if flow:
            flow.evidence["failures"].append({"stage": "temporary_directory_cleanup", "type": type(error).__name__, "message": flow.clean(str(error))})
            flow.evidence["status"] = "failed"
            flow.save()
        else:
            (output / "evidence.json").write_text(json.dumps({"status": "failed", "stage": "bootstrap", "error_type": type(error).__name__}), encoding="utf-8")
    status = flow.evidence["status"] if flow else "failed"
    print(f"RESULT {status}; evidence={output / 'evidence.json'}", flush=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
