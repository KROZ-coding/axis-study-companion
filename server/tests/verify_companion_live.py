"""Isolated companion fixture and real loopback HTTP verification.

Run from the repository root with the project's Python interpreter:
    server/.venv/Scripts/python.exe -I -B -X utf8 server/tests/verify_companion_live.py
    server/.venv/Scripts/python.exe -I -B -X utf8 server/tests/verify_companion_live.py \
        --serve --stream-seconds 8 --output-dir .scratch/companion-live-NEW

The serve directory must be NEW and below the repository's ignored .scratch/.
service-info.json contains the origin, local credentials path and scenario IDs;
passwords are generated per run and never printed. Serve persists until a signal
or creation of the reported stop_file. All six databases live in a separate
TemporaryDirectory and are removed after the service stops. Credentials are
removed on normal shutdown; only redacted evidence and service metadata remain.

The application, stores, sessions, dispatcher, workers, SSE and billing are real.
Only the gateway is replaced, before workers start, with deterministic local
streaming. No .env, inherited LLM/MaxKB configuration, real upstream, demo data,
TestClient or browser automation is used. Direct store writes are limited to
synthetic accounts, questions/tasks and precompleted pagination/history fixtures.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
SCRIPT = Path(__file__).resolve()
SCRATCH = ROOT / ".scratch"
MODEL = "gpt-5.6-sol"
PROVIDER = "companion-live-fixture"
CURRENCY = "fixture-unit"
PRICE_VERSION = "companion-live-price-v1"
USAGE = {"prompt_tokens": 17, "completion_tokens": 23}
EXPECTED_COST = USAGE["prompt_tokens"] + 2 * USAGE["completion_tokens"]
BUDGET_UNITS = 10_000_000
FAIL_MESSAGE = "[companion-fixture:fail]"
FAILED_PARTIAL = "这段未完成内容必须在失败后收起。"
STREAM_PARTS = (
    "这是隔离测试学伴的确定性回复。\n\n",
    "学习时，可以先用自己的话写出问题，",
    "再把已知条件和需要求出的量分开。\n\n",
    "第一步：整理定义与条件。\n",
    "第二步：选择一种方法并写出依据。\n",
    "第三步：检查每一步是否满足题目要求。\n\n",
    "如果某一步仍不确定，就缩小到一个具体问题，",
    "保留自己的尝试，再继续讨论。\n\n",
    "本回复由本机 FakeGateway 分段输出，",
    "刷新页面后应从已保存的消息和事件继续恢复。",
)
STREAM_TEXT = "".join(STREAM_PARTS)
SENSITIVE_KEYS = {
    "password", "password_hash", "current_password", "new_password", "activation_code",
    "reset_code", "lease_token", "cookie", "set-cookie", "sid", "token", "api_key",
    "authorization", "code_input",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value, *, private=False):
    """Replace metadata atomically so a reader never sees a partial document."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        if private:
            os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def apply_isolated_environment(data_dir):
    # Load this one known test helper by absolute path. -I excludes the script
    # directory/cwd; app.config must not be imported until env isolation is done.
    spec = importlib.util.spec_from_file_location("companion_live_platform_flow", SERVER / "tests" / "run_platform_flow.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("isolated environment helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = module.isolated_environment(data_dir.resolve())
    for key in tuple(os.environ):
        if key.startswith(("LLM_", "MAXKB_")):
            del os.environ[key]
    os.environ.update(environment)
    # This is deliberately fixture pricing, never an inherited site currency.
    os.environ["BUDGET_CURRENCY"] = CURRENCY
    sys.path.insert(0, str(SERVER))
    return dict(os.environ)


class FakeGateway:
    """No network/client dependency; use the worker's real delta callbacks."""

    def __init__(self, stream_seconds):
        self.stream_seconds = stream_seconds

    @property
    def configured(self):
        return True

    @staticmethod
    def configured_for(purpose):
        return purpose == "student"

    def describe(self, purpose):
        if purpose != "student":
            from app.services.model_gateway import GatewayError
            raise GatewayError("fixture_purpose_not_supported", request_sent=False)
        return {"provider": SimpleNamespace(name=PROVIDER), "model": MODEL,
                "reasoning_effort": "high", "max_tokens": 128,
                "timeout_seconds": self.stream_seconds + 10}

    async def complete(self, purpose, messages, *, call_id, json_mode=False, on_delta=None):
        from app.services.model_gateway import GatewayError, GatewayResult
        self.describe(purpose)
        if json_mode or not callable(on_delta):
            raise GatewayError("fixture_stream_required", request_sent=False)
        current_input = next((message["content"] for message in reversed(messages)
                              if message.get("role") == "user"), "")
        if current_input.strip() == FAIL_MESSAGE:
            await asyncio.sleep(self.stream_seconds / len(STREAM_PARTS))
            value = on_delta(FAILED_PARTIAL)
            if inspect.isawaitable(value):
                await value
            # Keep the partial visible across the real SSE poll interval,
            # including the shortest supported fixture stream setting.
            await asyncio.sleep(max(1.0, self.stream_seconds / 2))
            raise GatewayError("fixture_controlled_failure", request_sent=True,
                               unknown_outcome=False, usage=dict(USAGE),
                               provider=PROVIDER, model=MODEL, request_id="fixture-failure")
        for part in STREAM_PARTS:
            await asyncio.sleep(self.stream_seconds / len(STREAM_PARTS))
            value = on_delta(part)
            if inspect.isawaitable(value):
                await value
        return GatewayResult(STREAM_TEXT, None, dict(USAGE), "fixture-" + call_id,
                             MODEL, "high", PROVIDER, True, False)


def seed_fixture(runtime, output):
    """Only synthetic setup; subsequent new responses use HTTP and real workers."""
    from app.utils.security import hash_password
    runtime.initialize()
    platform, learning, companion = runtime.platform, runtime.learning, runtime.companion
    users, credentials = {}, {}
    labels = {"admin": "本机测试管理员", "teacher": "本机测试教师", "free": "自由聊天学生",
              "guided": "任务引导学生", "diagnostic": "独立短诊学生"}
    for name, label in labels.items():
        role = name if name in {"admin", "teacher"} else "student"
        password = secrets.token_urlsafe(30)
        username = "companion_fixture_" + name
        users[name] = platform.create_user(username, label, hash_password(password), role)
        credentials[name] = {"username": username, "password": password, "role": role,
                             "user_id": users[name]["id"]}
    write_json(output / "credentials.local.json", {"fixture_only": True, "accounts": credentials}, private=True)
    course = platform.create_course("本机学伴测试数学", "fixture-only", users["teacher"]["id"])
    classroom = platform.create_class(course["id"], "隔离测试班", owner_teacher_id=users["teacher"]["id"])
    for name in ("free", "guided", "diagnostic"):
        platform.create_course_membership(course["id"], users[name]["id"])
        platform.create_class_membership(classroom["id"], users[name]["id"])
        runtime.queue.budget_ledger.set_limit("student_daily", users[name]["id"], BUDGET_UNITS)
    runtime.queue.budget_ledger.set_limit("project", "platform", BUDGET_UNITS)
    refs = []
    for name, stem, options, answer in (
        ("guided", "计算 2(x + 3)，下面哪一步保持等式成立？", ["2x + 6", "2x + 3", "x + 6"], 0),
        ("formal", "若 x + 4 = 9，第一步应该怎样变形？", ["两边同时减 4", "只在左边减 4", "两边同时加 4"], 0),
        ("diagnostic", "3 + 2 的结果是什么？", ["4", "5", "6"], 1),
    ):
        question_id = "companion-fixture-" + name
        learning.create_question_version(users["teacher"]["id"], question_id, 1, {
            "type": "single_choice", "stem": stem, "options": options, "answer": answer,
            "points": 2, "explanation": "FIXTURE_PRIVATE_REFERENCE_NOT_PUBLIC",
            "family_id": "companion-fixture-family-" + name,
            "external_processing_permission": False,
        }, status="approved", course_id=course["id"])
        refs.append({"question_id": question_id, "version": 1})
    scenarios = {}
    for name, formal_refs, diagnostic_refs in (("guided", [refs[0]], []), ("diagnostic", [refs[1]], [refs[2]])):
        goal = learning.create_goal(users["teacher"]["id"], labels[name] + "目标",
                                    "本机合成题目，仅用于验证学伴交互。", course_id=course["id"], class_id=classroom["id"],
                                    request_id="fixture-goal-" + name)
        published = learning.publish_round(users["teacher"]["id"], goal["id"], expected_version=goal["version"],
                                           request_id="fixture-publish-" + name, round_no=1,
                                           student_ids=[users[name]["id"]], question_versions=formal_refs,
                                           diagnostic_question_versions=diagnostic_refs,
                                           allow_late=True, allow_retries=True)
        task_id = published["task_ids"][0]
        conversation = companion.create_conversation(users[name]["id"], labels[name] + "对话", course_id=course["id"],
                                                     request_id="fixture-conversation-" + name)
        scenarios[name] = {"account": name, "student_id": users[name]["id"], "task_id": task_id,
                           "goal_id": goal["id"], "conversation_id": conversation["id"],
                           "question_id": formal_refs[0]["question_id"],
                           "question_version_id": formal_refs[0]["question_id"] + ":1"}
    # The guided student already has a durable editable attempt, but no live
    # browser lease. Wait for a real short fixture lease to expire, never edit a
    # clock/database lease or make the browser take over an unrelated device.
    guided = scenarios["guided"]
    lease = learning.acquire_draft_lease(guided["task_id"], users["guided"]["id"], "fixture-seed", lease_seconds=0.05)
    learning.save_draft(guided["task_id"], users["guided"]["id"], {guided["question_id"]: 1},
                        expected_version=lease["task_version"], request_id="fixture-guided-draft",
                        lease_token=lease["lease_token"], fence_token=lease["fence_token"])
    while time.time() <= lease["lease_until"]:
        time.sleep(0.01)
    diagnostic = scenarios["diagnostic"]
    session = learning.start_diagnostic(diagnostic["task_id"], users["diagnostic"]["id"], request_id="fixture-diagnostic-start")
    diagnostic.update(diagnostic_id=session["id"], diagnostic_question_id=refs[2]["question_id"])
    free_user = users["free"]["id"]
    # 103 active conversations cross the frontend's 100-conversation page, and
    # 110 stored messages cross its 100-message history window.
    older_conversations = [companion.create_conversation(free_user, f"历史会话 {index + 1:03d}",
                                                        request_id=f"fixture-history-conversation-{index}")
                           for index in range(102)]
    history = companion.create_conversation(free_user, "长历史与刷新恢复", request_id="fixture-long-history")
    for index in range(55):
        posted = companion.post_message(history["id"], free_user, f"历史提问 {index + 1:03d}：怎样检查解题步骤？",
                                         request_id=f"fixture-history-message-{index}")
        fixture_call = f"fixture-precompleted:{index}"
        companion.begin_response(posted["response_id"], call_id=fixture_call, generation=1)
        companion.complete_response(posted["response_id"], f"历史讲解 {index + 1:03d}：保留条件、方法和检查依据。",
                                     call_id=fixture_call, generation=1)
        event = next(event for event in companion.pending_outbox() if event["response_id"] == posted["response_id"])
        companion.mark_outbox(event["outbox_id"], job_id=posted["job_id"])
    scenarios["free"] = {"account": "free", "student_id": free_user, "conversation_id": history["id"],
                         "history_conversation_id": history["id"], "history_message_count": 110,
                         "active_conversation_count": len(older_conversations) + 1,
                         "oldest_conversation_id": older_conversations[0]["id"], "active_task_count": 0}
    return {"scenarios": scenarios, "course_id": course["id"], "class_id": classroom["id"]}


def run_fixture_child(output, data, port, stream_seconds):
    """Run the genuine application with its normal lifespan and worker pool."""
    apply_isolated_environment(data)
    from app.config import Settings
    from app.main import create_app
    import uvicorn
    settings = Settings()
    if settings.llm_configured or settings.maxkb_url or settings.maxkb_api_key:
        raise RuntimeError("upstream isolation failed")
    price = {"provider": PROVIDER, "model": MODEL, "version": PRICE_VERSION, "currency": CURRENCY,
             "input_units_per_million": 1_000_000, "output_units_per_million": 2_000_000}
    settings = replace(settings, llm_prices_json=json.dumps([price]), budget_currency=CURRENCY,
                       worker_poll_seconds=0.1, worker_concurrency=1)
    application = create_app(settings)
    runtime = application.state.runtime
    runtime.install("gateway", FakeGateway(stream_seconds))
    seeded = seed_fixture(runtime, output)
    origin = f"http://127.0.0.1:{port}"
    info = {
        "schema": "companion.live-fixture.v1", "status": "starting", "fixture_only": True,
        "created_at": utc_now(), "pid": os.getpid(), "origin": origin,
        "prototype_url": origin + "/prototype/", "credentials_path": str((output / "credentials.local.json").resolve()),
        "stop_file": str((output / "stop.requested").resolve()), "data_dir": str(data.resolve()),
        "stream_seconds": stream_seconds, "failure_message": FAIL_MESSAGE,
        "fake_usage": USAGE, "expected_call_cost_units": EXPECTED_COST,
        "price": price, "budget_limit_units": BUDGET_UNITS, **seeded,
        "boundaries": {"application": "real FastAPI/uvicorn, real stores/auth/dispatcher/background worker",
                       "upstream": "FakeGateway injected before workers start; no network code",
                       "environment": "run_platform_flow.isolated_environment; .env skipped; inherited LLM_/MAXKB_ removed",
                       "history": "synthetic precompleted store fixtures; no worker/provider billing claimed",
                       "credentials": "generated per fixture; local ignored JSON only; removed on shutdown"},
    }
    write_json(output / "service-info.json", info)
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, workers=1,
                                          lifespan="on", access_log=False, log_level="critical",
                                          timeout_graceful_shutdown=15, log_config=None))
    watcher_stop = threading.Event()

    def watch_shutdown():
        while not watcher_stop.wait(0.2):
            if (output / "stop.requested").exists():
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch_shutdown, name="fixture-stop-watcher", daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        watcher_stop.set()
        watcher.join(timeout=2)
        runtime.stop()
    return 0


class VerificationFailure(AssertionError):
    pass


class LiveVerification:
    def __init__(self, output, args):
        self.output, self.args = output, args
        self.process = None
        self.clients = {}
        self.private = set()
        self.server_log = None
        self.info = None
        self.data = None
        self.stage_name = "bootstrap"
        self.started = time.monotonic()
        self.evidence = {
            "schema": "companion.live-http-smoke.v1", "status": "running", "started_at": utc_now(),
            "stages": [], "http": [], "failures": [],
            "boundary": "real loopback HTTP with cookies/Origin; FakeGateway only; no real upstream or browser",
            "not_covered": ["real provider/model quality and billing", "browser UI (coordinator owns official browser checks)"],
        }
        self.save()

    @staticmethod
    def check(condition, message):
        if not condition:
            raise VerificationFailure(message)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for private in sorted(self.private, key=len, reverse=True):
                value = value.replace(private, "[REDACTED]")
            return re.sub(r"(?i)\b(password|lease_token|sid|activation_code|api_key|authorization)([\s\"':=]+)[^\s,;\"'}]+",
                          r"\1\2[REDACTED]", value)
        return value

    def save(self):
        write_json(self.output / "evidence.json", self.clean(self.evidence))

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
            self.evidence["failures"].append({"stage": name, "type": type(error).__name__, "message": self.clean(str(error))})
            print(f"FAIL {name}: {self.clean(str(error))}", flush=True)
            raise
        else:
            row["status"] = "passed"
            print(f"PASS {name}", flush=True)
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def start_service(self, data):
        import httpx
        self.data = data.resolve()
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        self.server_log = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", str(SCRIPT), "--fixture-child",
             "--output-dir", str(self.output), "--data-dir", str(self.data), "--port", str(port),
             "--stream-seconds", str(self.args.stream_seconds)],
            cwd=ROOT, env=apply_isolated_environment(self.data), stdin=subprocess.DEVNULL,
            stdout=self.server_log, stderr=subprocess.STDOUT, shell=False,
        )
        self.clients = {name: httpx.Client(base_url=origin, headers={"Origin": origin},
                                          timeout=httpx.Timeout(max(self.args.timeout, self.args.stream_seconds + 10), connect=2),
                                          follow_redirects=False, trust_env=False)
                        for name in ("anonymous", "admin", "free", "guided", "diagnostic")}
        end = time.monotonic() + self.args.timeout
        while time.monotonic() < end:
            self.check(self.process.poll() is None, "fixture server exited before readiness")
            try:
                response = self.clients["anonymous"].get("/health")
            except httpx.TransportError:
                pass
            else:
                if response.status_code == 200 and (self.output / "service-info.json").is_file():
                    self.info = json.loads((self.output / "service-info.json").read_text(encoding="utf-8"))
                    health = response.json()
                    self.check(health.get("mode") == "platform", "fixture must use platform mode")
                    self.check(health["workers"]["running"] == 1 and not health["workers"]["needs_attention"], "real worker is unhealthy")
                    self.credentials = json.loads((self.output / "credentials.local.json").read_text(encoding="utf-8"))["accounts"]
                    self.private.update(account["password"] for account in self.credentials.values())
                    self.info.update(status="ready", ready_at=utc_now(), launcher_pid=os.getpid())
                    write_json(self.output / "service-info.json", self.info)
                    self.evidence["service"] = {"origin": origin, "pid": self.process.pid, "health": health,
                                                "stream_seconds": self.args.stream_seconds}
                    self.save()
                    return
            time.sleep(0.1)
        raise VerificationFailure("fixture readiness timed out")

    def request(self, actor, method, path, expected=200, *, origin=True, **options):
        client = self.clients[actor]
        request = client.build_request(method, path, **options)
        if not origin:
            request.headers.pop("Origin", None)
        response = client.send(request)
        for cookie in client.cookies.jar:
            self.private.add(cookie.value)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        row = {"stage": self.stage_name, "actor": actor, "method": method, "path": path,
               "status": response.status_code, "bytes": len(response.content)}
        if response.status_code >= 400:
            row["error_response"] = self.clean(body)
        self.evidence["http"].append(row)
        allowed = {expected} if isinstance(expected, int) else set(expected)
        self.check(response.status_code in allowed,
                   f"{method} {path}: expected {sorted(allowed)}, got {response.status_code}; body={self.clean(body)}")
        return response

    def api(self, actor, method, path, expected=200, **options):
        response = self.request(actor, method, path, expected, **options)
        return response.json() if response.content else None

    def poll(self, label, read, done):
        end = time.monotonic() + self.args.timeout + self.args.stream_seconds
        last = None
        while time.monotonic() < end:
            self.check(self.process.poll() is None, label + ": fixture stopped")
            last = read()
            if done(last):
                return last
            time.sleep(0.1)
        raise VerificationFailure(label + ": timed out; last=" + str(self.clean(last)))

    def login(self, name):
        account = self.credentials[name]
        response = self.request(name, "POST", "/api/v1/auth/login", json={key: account[key] for key in ("username", "password", "role")})
        self.check("HttpOnly" in response.headers.get("set-cookie", ""), "session cookie must be HttpOnly")
        self.check(bool(self.clients[name].cookies.get("sid")), "actual session cookie is missing")
        self.check(self.api(name, "GET", "/api/v1/auth/me")["id"] == account["user_id"], "cookie did not authenticate actor")

    def sql(self, database, query, parameters=()):
        path = self.data / (database + ".sqlite3")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    @staticmethod
    def parse_sse(text):
        result = []
        for frame in text.replace("\r\n", "\n").split("\n\n"):
            fields, data = {}, []
            for line in frame.splitlines():
                if line.startswith("id:"):
                    fields["id"] = int(line[3:].strip())
                elif line.startswith("event:"):
                    fields["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                fields["data"] = json.loads("\n".join(data))
                result.append(fields)
        return result

    def full_stream(self, actor, response_id, *, after=0, last_event_id=None, once=False):
        headers = {} if last_event_id is None else {"Last-Event-ID": str(last_event_id)}
        response = self.request(actor, "GET", f"/api/v1/companion/responses/{response_id}/stream",
                                params={"after": after, "once": str(once).lower()}, headers=headers)
        self.check(response.headers.get("content-type", "").startswith("text/event-stream"), "response is not SSE")
        self.check(response.headers.get("cache-control") == "no-store", "SSE must be no-store")
        return self.parse_sse(response.text)

    def first_delta(self, actor, response_id):
        client = self.clients[actor]
        started = time.monotonic()
        frames, buffer = [], []
        with client.stream("GET", f"/api/v1/companion/responses/{response_id}/stream") as response:
            self.check(response.status_code == 200, "live stream did not open")
            self.check(response.headers.get("content-type", "").startswith("text/event-stream"), "live response is not SSE")
            for line in response.iter_lines():
                if line:
                    buffer.append(line)
                    continue
                frames.extend(self.parse_sse("\n".join(buffer) + "\n\n"))
                buffer = []
                if frames and frames[-1].get("event") == "delta":
                    self.evidence["http"].append({"stage": self.stage_name, "actor": actor, "method": "GET",
                                                  "path": f"/api/v1/companion/responses/{response_id}/stream",
                                                  "status": 200, "disconnected_after_first_delta": True})
                    return frames, round(time.monotonic() - started, 3)
        raise VerificationFailure("live stream ended without a delta")

    def response(self, actor, response_id):
        return self.api(actor, "GET", f"/api/v1/companion/responses/{response_id}")

    def completed(self, actor, response_id):
        return self.poll("response terminal", lambda: self.response(actor, response_id),
                         lambda row: row["status"] in {"completed", "failed", "cancelled"})

    def terminal_job(self, actor, job_id):
        return self.poll("worker terminal", lambda: self.api(actor, "GET", f"/api/v1/jobs/{job_id}"),
                         lambda row: row["status"] in {"succeeded", "failed", "cancelled", "reconciling"})

    def post(self, actor, conversation_id, text, request_id, **scope):
        return self.api(actor, "POST", f"/api/v1/companion/conversations/{conversation_id}/messages", 202,
                        json={"text": text, "request_id": request_id, **scope})

    def budget_snapshot(self):
        return self.api("admin", "GET", "/api/v1/budgets/limits")["items"]

    @staticmethod
    def spent(rows, scope, scope_id):
        return sum(row["spent_units"] for row in rows if row["scope_type"] == scope and row["scope_id"] == scope_id)

    def assert_call(self, posted, actor, status="succeeded"):
        job = self.terminal_job(actor, posted["job_id"])
        self.check(job["status"] == ("succeeded" if status == "succeeded" else "failed"), "unexpected terminal worker status")
        calls = self.api(actor, "GET", f"/api/v1/jobs/{posted['job_id']}/calls")["items"]
        self.check(len(calls) == 1 and calls[0]["status"] == status, "response must have exactly one expected fake call")
        self.check(calls[0]["cost_units"] == EXPECTED_COST and calls[0]["cost_pending"] is False, "fake usage was not settled")
        self.check(calls[0]["model_name"] == MODEL and calls[0]["price_version"] == PRICE_VERSION, "fixture price/model differ")
        stored = self.sql("jobs", "SELECT usage_json,cost_units,cost_pending FROM upstream_calls WHERE job_id=?", (posted["job_id"],))
        self.check(len(stored) == 1 and json.loads(stored[0]["usage_json"]) == USAGE, "durable fake usage differs")
        reservations = self.sql("jobs", "SELECT scope_type,scope_id,reserved_units,active FROM call_budget_reservations WHERE call_id=?", (calls[0]["call_id"],))
        self.check({(row["scope_type"], row["scope_id"]) for row in reservations}
                   == {("student_daily", self.credentials[actor]["user_id"]), ("project", "platform")},
                   "free chat did not reserve personal daily and platform budgets")
        # Inactive bindings retain their original amount as an audit fact;
        # the live budget bucket, not that historical column, must be released.
        self.check(all(row["active"] == 0 for row in reservations), "settled reservation remains active")
        return calls[0]

    def run_smoke(self):
        with self.stage("cookie_origin_and_scenarios") as row:
            for actor in ("admin", "free", "guided", "diagnostic"):
                self.login(actor)
            self.api("anonymous", "GET", "/api/v1/companion/conversations", 401)
            payload = {"title": "Origin rejection", "request_id": "smoke-origin-denied"}
            self.api("free", "POST", "/api/v1/companion/conversations", 403, json=payload, origin=False)
            self.api("free", "POST", "/api/v1/companion/conversations", 403, json=payload, headers={"Origin": "https://foreign.invalid"})
            self.check(self.api("free", "GET", "/api/v1/tasks")["items"] == [], "free student must have no tasks")
            policies = {name: self.api(name, "GET", "/api/v1/companion/answer-policy") for name in ("free", "guided", "diagnostic")}
            self.check(policies["free"]["allow_generation"] is True, "free chat is unexpectedly blocked")
            self.check(not policies["guided"]["allow_generation"] and policies["guided"]["attempted"], "guided draft policy differs")
            self.check(policies["diagnostic"]["diagnostic"] and not policies["diagnostic"]["allow_generation"], "diagnostic is not protected")
            row["policies"] = policies
        free = self.info["scenarios"]["free"]
        history_id = free["history_conversation_id"]
        with self.stage("conversation_and_history_pagination") as row:
            first = self.api("free", "GET", "/api/v1/companion/conversations", params={"limit": 100})
            second = self.api("free", "GET", "/api/v1/companion/conversations", params={"limit": 100, "offset": 100})
            self.check(len(first) == 100 and len(second) == 3 and not {item["id"] for item in first}.intersection(item["id"] for item in second), "conversation pagination differs")
            initial = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"limit": 100})
            latest = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"latest": "true", "limit": 100})
            older = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"before": latest[0]["id"], "limit": 100})
            self.check(len(initial) == len(latest) == 100 and len(older) == 10 and initial[0]["id"] == older[0]["id"], "history windows differ")
            self.check(not {item["id"] for item in latest}.intersection(item["id"] for item in older), "history pages overlap")
            row.update(conversations=103, messages=110, latest_window=100, older_messages=10)
        with self.stage("stream_disconnect_replay_tail_and_usage") as row:
            before = self.budget_snapshot()
            prior_tail = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"latest": "true", "limit": 1})[0]
            posted = self.post("free", history_id, "请解释怎样整理一道题的条件。", "smoke-free-message")
            replay = self.post("free", history_id, "请解释怎样整理一道题的条件。", "smoke-free-message")
            self.check(all(replay[key] == posted[key] for key in ("message_id", "response_id", "job_id")), "message replay duplicated work")
            self.api("free", "POST", f"/api/v1/companion/conversations/{history_id}/messages", 409,
                     json={"text": "changed input", "request_id": "smoke-free-message"})
            first_events, first_seconds = self.first_delta("free", posted["response_id"])
            cursor = first_events[-1]["id"]
            partial = self.response("free", posted["response_id"])
            self.check(partial["status"] == "streaming" and partial["generation"] >= 1 and partial["text"], "partial response was not recoverable while streaming")
            tail = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"latest": "true", "limit": 100})
            recovered = next(item for item in tail if item["id"] == posted["message_id"])["response"]
            self.check(recovered["response_id"] == posted["response_id"] and recovered["status"] == "streaming"
                       and recovered["generation"] == partial["generation"] and recovered["last_event_id"] >= cursor,
                       "latest history did not contain pending response recovery metadata")
            rest = self.full_stream("free", posted["response_id"], last_event_id=cursor)
            self.check(rest and all(event.get("id", cursor + 1) >= cursor if event.get("event") == "snapshot"
                                    else event.get("id", cursor + 1) > cursor for event in rest),
                       "SSE resume moved backwards or repeated a durable event")
            events = first_events + rest
            ids = [event["id"] for event in events if "id" in event]
            row["sse_event_sequence"] = [{"id": event.get("id"), "event": event.get("event"),
                                            "generation": event.get("data", {}).get("generation")}
                                           for event in events]
            # Authoritative snapshots use their last durable event ID. A
            # terminal snapshot may therefore repeat the preceding end ID;
            # only durable events must be unique, and snapshots replace text.
            durable_ids = [event["id"] for event in events if event.get("event") != "snapshot" and "id" in event]
            self.check(ids == sorted(ids) and durable_ids == sorted(set(durable_ids)),
                       "SSE cursor moved backwards or duplicated a durable event; sequence=" + str(row["sse_event_sequence"]))
            reconstructed = ""
            for event in events:
                if event.get("event") == "snapshot":
                    reconstructed = event["data"].get("text", "")
                elif event.get("event") in {"start", "reset"}:
                    reconstructed = ""
                elif event.get("event") == "delta":
                    reconstructed += event["data"].get("text", "")
            self.check(reconstructed == STREAM_TEXT, "resumed SSE text differs from deterministic output")
            self.check(any(event.get("event") in {"end", "snapshot"} and event["data"].get("status") == "completed" for event in events),
                       "SSE terminal projection missing")
            self.check(events[-1]["event"] in {"end", "snapshot"} and events[-1]["data"]["status"] == "completed",
                       "SSE terminal projection missing")
            final = self.completed("free", posted["response_id"])
            self.check(final["status"] == "completed" and final["text"] == STREAM_TEXT and final["generation"] == partial["generation"], "final response differs")
            snapshot = self.full_stream("free", posted["response_id"], after=2**53 - 1, once=True)
            self.check(len(snapshot) == 1 and snapshot[0]["event"] == "snapshot" and snapshot[0]["data"]["text"] == STREAM_TEXT, "future cursor did not recover an authoritative snapshot")
            event_json = self.api("free", "GET", f"/api/v1/companion/responses/{posted['response_id']}/events", params={"after": cursor})
            self.check(event_json and all(item["event_id"] > cursor for item in event_json), "REST event cursor recovery differs")
            tail = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"after": prior_tail["id"], "limit": 100})
            self.check([item["role"] for item in tail] == ["user", "assistant"] and tail[-1]["text"] == STREAM_TEXT, "message tail did not recover exactly one completed turn")
            linked = tail[0]["response"]
            self.check(linked["status"] == "completed" and linked["assistant_message_id"] == tail[-1]["id"] and linked["generation"] == final["generation"], "history response snapshot differs")
            call = self.assert_call(posted, "free")
            after = self.budget_snapshot()
            for scope, scope_id in (("student_daily", free["student_id"]), ("project", "platform")):
                self.check(self.spent(after, scope, scope_id) - self.spent(before, scope, scope_id) == EXPECTED_COST, "budget delta differs from fake usage")
            self.check(not any(item["reserved_units"] for item in after), "terminal call left reserved budget")
            self.api("guided", "GET", f"/api/v1/companion/responses/{posted['response_id']}", 404)
            self.api("guided", "GET", f"/api/v1/jobs/{posted['job_id']}", 404)
            row.update(response_id=posted["response_id"], job_id=posted["job_id"], generation=final["generation"],
                       first_delta_seconds=first_seconds, resumed_events=len(rest), call_cost_units=call["cost_units"],
                       fake_usage=USAGE, student_daily_delta=EXPECTED_COST, project_platform_delta=EXPECTED_COST)
        with self.stage("controlled_failure_is_redacted_and_settled") as row:
            before = self.budget_snapshot()
            posted = self.post("free", history_id, FAIL_MESSAGE, "smoke-controlled-failure")
            first, _ = self.first_delta("free", posted["response_id"])
            self.check(any(event.get("event") == "delta" and event["data"].get("text") == FAILED_PARTIAL for event in first), "fixture failure did not expose a live partial before termination")
            final = self.completed("free", posted["response_id"])
            self.check(final["status"] == "failed" and final["text"] == "" and final["error"] == "response_failed", "failed response exposed stale partial")
            events = self.api("free", "GET", f"/api/v1/companion/responses/{posted['response_id']}/events")
            self.check(FAILED_PARTIAL not in json.dumps(events, ensure_ascii=False), "failed event replay exposed stale partial")
            tail = self.api("free", "GET", f"/api/v1/companion/conversations/{history_id}/messages", params={"latest": "true", "limit": 2})
            self.check(FAILED_PARTIAL not in json.dumps(tail, ensure_ascii=False) and tail[0]["response"]["status"] == "failed", "failed history exposed stale partial")
            self.assert_call(posted, "free", "failed")
            after = self.budget_snapshot()
            self.check(self.spent(after, "project", "platform") - self.spent(before, "project", "platform") == EXPECTED_COST, "known failed-call usage was not settled")
            row.update(status="passed", response_id=posted["response_id"], failure_public_code=final["error"], cost_units=EXPECTED_COST)
        with self.stage("guided_public_question_draft_help_and_safe_guidance") as row:
            scenario = self.info["scenarios"]["guided"]
            task_id = scenario["task_id"]
            task = self.api("guided", "GET", f"/api/v1/tasks/{task_id}")
            question = task["questions"][0]
            self.check(question["stem"] and len(question["options"]) == 3 and not {"answer", "explanation", "rubric"}.intersection(question), "guided question projection differs")
            self.check(task["draft_answers"] == {scenario["question_id"]: 1}, "guided draft was not preserved")
            lease = self.api("guided", "POST", f"/api/v1/tasks/{task_id}/edit-leases", json={"device_id": "smoke-guided", "lease_seconds": 15})
            self.private.add(lease["lease_token"])
            updated = self.api("guided", "PUT", f"/api/v1/tasks/{task_id}/draft", json={"answers": {scenario["question_id"]: 2},
                "expected_version": lease["task_version"], "request_id": "smoke-guided-draft", "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]})
            self.check(updated["draft_answers"] == {scenario["question_id"]: 2}, "guided attempt was not editable")
            help_payload = {"question_id": scenario["question_id"], "request_id": "smoke-guided-help",
                            "payload": {"source": "companion", "question_version_id": scenario["question_version_id"]}}
            help_event = self.api("guided", "POST", f"/api/v1/tasks/{task_id}/help-events", 201, json=help_payload)
            replay = self.api("guided", "POST", f"/api/v1/tasks/{task_id}/help-events", 201, json=help_payload)
            self.check(replay == help_event and help_event["event_type"] == "help", "help event replay differs")
            posted = self.post("guided", scenario["conversation_id"], "我已尝试，请引导我检查当前步骤。", "smoke-guided-message",
                               task_id=task_id, question_version_id=scenario["question_version_id"])
            result = self.completed("guided", posted["response_id"])
            self.terminal_job("guided", posted["job_id"])
            self.check(result["status"] == "completed" and result["content_type"] == "safe_guidance" and result["guidance_key"] == "await_results", "guided request generated freeform text")
            self.check(self.api("guided", "GET", f"/api/v1/jobs/{posted['job_id']}/calls")["items"] == [], "guided response called the fake model")
            stored = self.sql("learning", "SELECT COUNT(*) AS total FROM task_events WHERE task_id=? AND event_type='help'", (task_id,))
            self.check(stored[0]["total"] == 1, "help event was duplicated")
            job_scope = self.sql("jobs", "SELECT student_id,course_id,class_id,payload_json FROM jobs WHERE job_id=?", (posted["job_id"],))[0]
            self.check(job_scope["student_id"] == scenario["student_id"] and job_scope["course_id"] == self.info["course_id"] and job_scope["class_id"] == self.info["class_id"], "task chat queue scope differs")
            self.check(json.loads(job_scope["payload_json"])["task_id"] == task_id, "task binding missing from durable queue scope")
            row.update(task_id=task_id, help_events=1, safe_guidance_key=result["guidance_key"], model_calls=0)
        with self.stage("diagnostic_independence_and_safe_guidance") as row:
            scenario = self.info["scenarios"]["diagnostic"]
            task_id = scenario["task_id"]
            task = self.api("diagnostic", "GET", f"/api/v1/tasks/{task_id}")
            self.check(task["diagnostic_required"] and task["questions"] == [], "formal paper visible during diagnostic")
            self.api("diagnostic", "POST", f"/api/v1/tasks/{task_id}/help-events", 409,
                     json={"question_id": scenario["question_id"], "request_id": "smoke-diagnostic-help", "payload": {"source": "companion"}})
            diagnostic = self.api("diagnostic", "GET", f"/api/v1/diagnostics/{scenario['diagnostic_id']}")
            self.check(diagnostic["hints_allowed"] is False and diagnostic["counts_toward_formal_score"] is False, "diagnostic independence contract differs")
            self.check(diagnostic["questions"] and "answer" not in diagnostic["questions"][0], "diagnostic leaked answer")
            posted = self.post("diagnostic", scenario["conversation_id"], "请告诉我诊断题怎么作答。", "smoke-diagnostic-message", task_id=task_id)
            result = self.completed("diagnostic", posted["response_id"])
            self.terminal_job("diagnostic", posted["job_id"])
            self.check(result["status"] == "completed" and result["guidance_key"] == "independent_attempt", "diagnostic did not use independent guidance")
            self.check(self.api("diagnostic", "GET", f"/api/v1/jobs/{posted['job_id']}/calls")["items"] == [], "diagnostic called fake model")
            row.update(task_id=task_id, diagnostic_id=scenario["diagnostic_id"], safe_guidance_key=result["guidance_key"], model_calls=0)
        with self.stage("final_worker_health_and_redaction") as row:
            health = self.api("anonymous", "GET", "/health")
            self.check(health["workers"]["running"] == 1 and not health["workers"]["needs_attention"], "worker reported a failure")
            self.save()
            evidence_text = (self.output / "evidence.json").read_text(encoding="utf-8")
            info_text = (self.output / "service-info.json").read_text(encoding="utf-8")
            self.check(all(secret not in evidence_text + info_text for secret in self.private), "redacted output contains a secret")
            row["worker_healthy"] = True

    def stop_service(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        if self.process is None:
            return
        process = self.process
        if process.poll() is None:
            (self.output / "stop.requested").touch(exist_ok=True)
            try:
                process.wait(timeout=self.args.stream_seconds + 20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                self.evidence["failures"].append({"stage": "shutdown", "type": "ShutdownTimeout", "message": "fixture required forced shutdown"})
        self.evidence["shutdown"] = {"pid": process.pid, "exit_code": process.returncode, "reaped": process.poll() is not None}
        if process.returncode != 0:
            self.evidence["failures"].append({"stage": "shutdown", "type": "ServerExit", "message": f"fixture exited with code {process.returncode}"})
        self.process = None
        # Read generated credentials locally even if bootstrap failed between
        # account creation and readiness, so any retained diagnostic is redacted.
        credentials_path = self.output / "credentials.local.json"
        if credentials_path.is_file():
            accounts = json.loads(credentials_path.read_text(encoding="utf-8")).get("accounts", {})
            self.private.update(account["password"] for account in accounts.values() if account.get("password"))
        if self.server_log is not None:
            self.server_log.flush()
            self.server_log.seek(0)
            text = self.server_log.read().decode("utf-8", errors="replace")
            if text:
                self.evidence["server_diagnostics"] = self.clean(text[-12000:])
            self.server_log.close()
            self.server_log = None
        (self.output / "credentials.local.json").unlink(missing_ok=True)
        if self.info:
            self.info.update(status="stopped", stopped_at=utc_now(), credentials_removed=True)
            write_json(self.output / "service-info.json", self.info)


def output_directory(value):
    scratch = SCRATCH.resolve()
    output = (Path(value).expanduser().resolve() if value else scratch / ("companion-live-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)))
    if not output.is_relative_to(scratch) or output == scratch:
        raise ValueError("output-dir must be a NEW directory below the repository's ignored .scratch/")
    # Verify the actual ignore rule; never silently write local credentials into
    # a tracked directory if repository rules change.
    check = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--quiet", "--", str(output / "credentials.local.json")],
                           capture_output=True, check=False)
    if check.returncode != 0:
        raise ValueError("output directory is not ignored by Git")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help="keep isolated fixture running until signal or reported stop_file")
    parser.add_argument("--output-dir", type=Path, help="NEW local ignored directory below .scratch/")
    parser.add_argument("--stream-seconds", type=float, default=6.0, help="deterministic full stream duration, 2..30 seconds (default: 6)")
    parser.add_argument("--timeout", type=float, default=40.0, help="readiness/HTTP polling bound, 10..120 seconds")
    parser.add_argument("--fixture-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.flags.isolated or not sys.dont_write_bytecode or not sys.flags.utf8_mode:
        parser.error("invoke this script with python.exe -I -B -X utf8")
    if not math.isfinite(args.stream_seconds) or not 2 <= args.stream_seconds <= 30:
        parser.error("stream-seconds must be 2..30")
    if not math.isfinite(args.timeout) or not 10 <= args.timeout <= 120:
        parser.error("timeout must be 10..120")
    # Suppress request bodies, headers, account hashes and exception tracebacks.
    # Parent records only bounded redacted diagnostics from an anonymous file.
    logging.disable(logging.CRITICAL)
    if args.fixture_child:
        if args.output_dir is None or args.data_dir is None or args.port is None or not 1024 <= args.port <= 65535:
            parser.error("invalid internal fixture arguments")
        if not args.output_dir.resolve().is_relative_to(SCRATCH.resolve()) or not args.output_dir.is_dir():
            parser.error("internal fixture output must be an existing ignored scratch directory")
        if not args.data_dir.is_absolute() or not args.data_dir.is_dir() or any(args.data_dir.iterdir()):
            parser.error("internal data directory must be absolute, existing and empty")
        try:
            return run_fixture_child(args.output_dir.resolve(), args.data_dir.resolve(), args.port, args.stream_seconds)
        except Exception as error:
            print("FIXTURE CHILD FAILED (" + type(error).__name__ + ")", flush=True)
            return 1
    try:
        output = output_directory(args.output_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Fixture output: {output}", flush=True)
    verification = LiveVerification(output, args)
    interrupted = threading.Event()

    def requested_stop(signum, _frame):
        interrupted.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, requested_stop)
    try:
        with tempfile.TemporaryDirectory(prefix="companion-live-runtime-") as temporary:
            try:
                with verification.stage("isolated_server_start"):
                    verification.start_service(Path(temporary))
                if args.serve:
                    verification.evidence["mode"] = "serve"
                    verification.evidence["status"] = "serving"
                    verification.save()
                    print(f"READY origin={verification.info['origin']}; service-info={output / 'service-info.json'}", flush=True)
                    while not interrupted.wait(0.25) and verification.process.poll() is None:
                        if (output / "stop.requested").exists():
                            break
                else:
                    verification.run_smoke()
            except KeyboardInterrupt:
                interrupted.set()
            except Exception as error:
                if not verification.evidence["failures"]:
                    verification.evidence["failures"].append({"stage": verification.stage_name, "type": type(error).__name__, "message": verification.clean(str(error))})
            finally:
                verification.stop_service()
        verification.evidence["temporary_data_removed"] = not Path(temporary).exists()
    except Exception as error:
        verification.evidence["failures"].append({"stage": "cleanup", "type": type(error).__name__, "message": verification.clean(str(error))})
    verification.evidence.update(status="failed" if verification.evidence["failures"] else "stopped" if args.serve else "interrupted" if interrupted.is_set() else "passed",
                                 finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - verification.started, 3),
                                 http_requests=len(verification.evidence["http"]))
    verification.save()
    print(f"RESULT {verification.evidence['status']}; evidence={output / 'evidence.json'}", flush=True)
    return 0 if verification.evidence["status"] in {"passed", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
