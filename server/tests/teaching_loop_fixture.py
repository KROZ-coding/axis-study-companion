"""Private support for verify_teaching_loop; never imported by the application.

Only synthetic accounts/classes/approved questions are seeded directly. The
normal application lifespan starts the real dispatcher and worker. Gateway
fault controls are local files, not application routes or provider requests.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
SCRATCH = ROOT / ".scratch"
DATABASES = ("platform", "learning", "jobs", "content", "reports", "companion")
ACTORS = ("admin", "teacher", "readonly", "outsider", "student", "peer", "novice", "pending", "diagnostic")
STUDENTS = ("student", "peer", "novice", "pending")
PROVIDER = "teaching-loop-local-synthetic-selection"
MODELS = {"summary": "gpt-5.6-terra", "hint": "gpt-5.6-sol"}
CURRENCY = "fixture-unit"
PRICE_VERSION = "teaching-loop-fixture-v1"
USAGE = {"prompt_tokens": 17, "completion_tokens": 23}
COST = 63
BUDGET = 10_000_000
PRIVATE_MARKER = "TEACHING_LOOP_PRIVATE_ANSWER_KEY"
BAD_MARKER = "TEACHING_LOOP_UNTRUSTED_MODEL_PROSE"
FORMAL_IDS = ("loop-formal-choice", "loop-formal-judge", "loop-formal-fill")
DIAGNOSTIC_IDS = ("loop-diagnostic-choice", "loop-diagnostic-judge")
EXPLANATIONS = {"mastered": "maintain_and_check", "developing": "consolidate_goal",
                "needs_practice": "practice_goal", "insufficient_evidence": "collect_independent_evidence"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def write_json(path, value, *, private=False):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        if private:
            os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def isolated_environment(data_dir):
    """No app imports, .env loading, provider config or inherited proxy routing."""
    for key in tuple(os.environ):
        if key.startswith(("LLM_", "MAXKB_")) or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
            del os.environ[key]
    os.environ.update(
        SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=str(data_dir.resolve()),
        STUDY_DATA_DIR=str(data_dir.resolve()), STUDY_DEMO_ENABLED="false",
        PERSISTENCE_ENABLED="true", STUDY_PERSISTENCE_ENABLED="true",
        STUDY_WORKER_ENABLED="true", STUDY_WORKER_CONCURRENCY="1", COOKIE_SECURE="false",
        ALLOWED_HOSTS="127.0.0.1,localhost", CORS_ORIGINS="", NO_PROXY="127.0.0.1,localhost,::1",
        LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="", LLM_PRICES_JSON="",
        MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
        KNOWLEDGE_DIR="", KNOWLEDGE_INDEX_PATH="", PYTHONUNBUFFERED="1", BUDGET_CURRENCY=CURRENCY,
    )
    return dict(os.environ)


def install_boundary(data_dir):
    """Deny non-loopback DNS/connect/bind/sendto and non-fixture SQLite/.env reads."""
    data_dir = data_dir.resolve()
    stats = {"blocked_external_network": 0, "blocked_database_access": 0, "blocked_env_reads": 0}
    allowed = {str((data_dir / (name + ".sqlite3")).resolve()).casefold() for name in DATABASES}

    def loopback(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def audit(event, arguments):
        if event in {"socket.connect", "socket.bind", "socket.sendto"}:
            address = arguments[-1]
            if not isinstance(address, tuple) or not loopback(address[0]):
                stats["blocked_external_network"] += 1
                raise RuntimeError("teaching fixture permits loopback sockets only")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if not loopback(arguments[0]):
                stats["blocked_external_network"] += 1
                raise RuntimeError("teaching fixture permits loopback name resolution only")
        elif event == "sqlite3.connect":
            raw = str(arguments[0])
            if raw.startswith("file:"):
                parsed = urlsplit(raw)
                raw = unquote(parsed.path)
                if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
                    raw = raw[1:]
            if str(Path(raw).resolve()).casefold() not in allowed:
                stats["blocked_database_access"] += 1
                raise RuntimeError("teaching fixture refuses a non-temporary database")
        elif event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(arguments[0])).name.lower()
            if name == ".env" or name.startswith(".env."):
                stats["blocked_env_reads"] += 1
                raise RuntimeError("teaching fixture refuses environment files")

    sys.addaudithook(audit)
    return stats


def summary_selection(model_input):
    """Independent finite synthetic selector; never calls the production validator."""
    if model_input.get("schema_version") != "teaching.insight.input.v1":
        raise ValueError("summary input schema differs")
    facts = model_input["facts"]["students"]
    states = {fact["status"] for fact in facts}
    summary = ("needs_more_evidence" if states == {"insufficient_evidence"}
               else "mixed_goal_evidence" if len(states) > 1 else "frozen_goal_evidence")
    return {"schema_version": "teaching.insight.selection.v1", "summary_code": summary,
            "evidence_refs": ["report:scope", "report:rule", "report:students"],
            "findings": [{"student_code": fact["student_code"], "status": fact["status"],
                          "percent": fact["percent"], "independent_families": fact["independent_families"],
                          "evidence_refs": ["mastery:" + fact["student_code"]],
                          "explanation_code": EXPLANATIONS[fact["status"]]} for fact in facts],
            "limitations": list(model_input["limitations"])}


class FakeGateway:
    """Strict JSON selection only; all calls are visibly labelled local synthetic."""
    configured = True

    def __init__(self, output, forbidden_values):
        self.output, self.forbidden_values = output, tuple(forbidden_values)
        self.events, self.consumed = [], set()
        self.describe_calls = 0
        self.lock = threading.Lock()
        self.flush()

    @staticmethod
    def configured_for(purpose):
        return purpose in MODELS

    def describe(self, purpose):
        from app.services.model_gateway import GatewayError
        self.describe_calls += 1
        if purpose not in MODELS:
            raise GatewayError("fixture_disallows_this_purpose", request_sent=False)
        return {"provider": SimpleNamespace(name=PROVIDER), "model": MODELS[purpose],
                "reasoning_effort": "high", "max_tokens": 2048, "timeout_seconds": 30}

    def flush(self):
        write_json(self.output / "gateway-events.json", {
            "fixture_only": True, "origin": "local_synthetic_selection", "provider": PROVIDER,
            "real_provider_calls": 0, "describe_calls": self.describe_calls, "calls": self.events,
        })

    def control(self, purpose):
        path = self.output / "gateway-control.json"
        if not path.exists():
            return "valid"
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["purpose"] != purpose or value["nonce"] in self.consumed:
            return "valid"
        self.consumed.add(value["nonce"])
        if value["mode"] not in {"valid", "invalid_fact", "invalid_prose", "fail", "unknown", "hold"}:
            raise ValueError("unknown local fixture gateway mode")
        return value["mode"]

    async def complete(self, purpose, messages, *, call_id, json_mode=False, on_delta=None):
        from app.services.model_gateway import GatewayError, GatewayResult
        self.describe(purpose)
        if json_mode is not True or on_delta is not None:
            raise GatewayError("fixture_requires_nonstreaming_json", request_sent=False)
        user_messages = [message["content"] for message in messages if message.get("role") == "user"]
        if len(user_messages) != 1:
            raise GatewayError("fixture_requires_one_minimal_input", request_sent=False)
        raw = user_messages[0]
        if any(value and value in raw for value in self.forbidden_values) or PRIVATE_MARKER in raw:
            raise GatewayError("fixture_detected_private_model_input", request_sent=False)
        data = json.loads(raw)
        if purpose == "summary":
            if set(data) != {"schema_version", "rule", "facts", "evidence_refs", "limitations"}:
                raise GatewayError("fixture_unexpected_summary_fields", request_sent=False)
            selection = summary_selection(data)
        else:
            if set(data) != {"question", "saved_attempt", "hint_level", "allowed_action_ids"}:
                raise GatewayError("fixture_unexpected_hint_fields", request_sent=False)
            if set(data["question"]) - {"type", "stem", "options"} or not data["allowed_action_ids"]:
                raise GatewayError("fixture_hint_answer_key_or_empty_catalog", request_sent=False)
            selection = {"action_id": data["allowed_action_ids"][0]}
        mode = self.control(purpose)
        valid_result = {"text": canonical(selection), "data": selection, "usage": dict(USAGE),
                        "request_id": "local-synthetic-" + call_id, "model": MODELS[purpose],
                        "reasoning_effort": "high", "provider": PROVIDER}
        event = {"call_id": call_id, "purpose": purpose, "mode": mode, "json_mode": json_mode,
                 "stream": False, "origin": "local_synthetic_selection", "input_sha256": digest(data),
                 "started_at": utc_now(), "status": "running", "selection": selection}
        with self.lock:
            self.events.append(event)
            self.flush()
        try:
            if mode == "hold":
                await asyncio.sleep(20)
            else:
                await asyncio.sleep(0.05)
            if mode == "fail":
                raise GatewayError("fixture_known_failure", request_sent=False, unknown_outcome=False)
            if mode == "unknown":
                event["reconcile_result"] = valid_result
                raise GatewayError("fixture_unknown_outcome", request_sent=True, unknown_outcome=True,
                                   usage=dict(USAGE), provider=PROVIDER, model=MODELS[purpose])
            if mode == "invalid_fact":
                if purpose != "summary":
                    raise ValueError("invalid_fact is only a summary fault")
                selection = json.loads(canonical(selection))
                selection["findings"][0]["independent_families"] += 1
            elif mode == "invalid_prose":
                selection = {**selection, "text": BAD_MARKER}
            event.update(status="returned", returned_sha256=digest(selection))
            return GatewayResult(canonical(selection), selection, dict(USAGE), "local-synthetic-" + call_id,
                                 MODELS[purpose], "high", PROVIDER, True, False)
        except asyncio.CancelledError:
            event["status"] = "cancelled_before_result"
            raise GatewayError("fixture_cancelled_before_result", request_sent=False) from None
        except GatewayError as error:
            event["status"] = error.code
            raise
        finally:
            event["finished_at"] = utc_now()
            with self.lock:
                self.flush()


def seed_fixture(runtime, output):
    """Synthetic setup only; no seeded answers, attempts, evidence or summaries."""
    from app.utils.security import hash_password
    runtime.initialize()
    platform, learning = runtime.platform, runtime.learning
    users, credentials = {}, {}
    labels = {"admin": "本机夹具管理员", "teacher": "本机闭环教师", "readonly": "本机只读教师",
              "outsider": "本机跨班教师", "student": "提示证据学生", "peer": "独立全对学生",
              "novice": "独立练习学生", "pending": "待提交学生", "diagnostic": "浏览器首诊学生"}
    for actor in ACTORS:
        role = actor if actor == "admin" else "teacher" if actor in {"teacher", "readonly", "outsider"} else "student"
        password = secrets.token_urlsafe(30)
        users[actor] = platform.create_user("loop_fixture_" + actor, labels[actor], hash_password(password), role)
        credentials[actor] = {"username": users[actor]["username"], "password": password,
                              "role": role, "user_id": users[actor]["id"]}
    write_json(output / "credentials.local.json", {"fixture_only": True, "accounts": credentials}, private=True)
    owner, outsider = users["teacher"]["id"], users["outsider"]["id"]
    course = platform.create_course("第四阶段教学闭环本机合成课程", "fixture-only", owner)
    classroom = platform.create_class(course["id"], "第四阶段合成验收班", owner_teacher_id=owner)
    foreign = platform.create_class(course["id"], "独立权限边界班", owner_teacher_id=outsider)
    for actor in ("readonly", "outsider"):
        platform.set_course_collaborator(course["id"], users[actor]["id"], "read", granted_by=owner)
    platform.set_class_collaborator(classroom["id"], users["readonly"]["id"], "read", granted_by=owner)
    for actor in (*STUDENTS, "diagnostic"):
        platform.create_course_membership(course["id"], users[actor]["id"])
        platform.create_class_membership(classroom["id"], users[actor]["id"])
        runtime.queue.budget_ledger.set_limit("student_daily", users[actor]["id"], BUDGET)
    runtime.queue.budget_ledger.set_limit("project", "platform", BUDGET)
    runtime.queue.budget_ledger.set_limit("teaching", classroom["id"], BUDGET)
    specs = [
        (FORMAL_IDS[0], "single_choice", "【本机合成】观察三角形的边数，选择正确项。", ["两条", "三条", "四条"], 1, True),
        (FORMAL_IDS[1], "judge", "【本机合成】正方形的四条边长度相等。", [], True, True),
        (FORMAL_IDS[2], "fill", "【本机合成】写出二加三的和。", [], "5", False),
        (DIAGNOSTIC_IDS[0], "single_choice", "【本机合成首诊】偶数的个位可以是哪一个？", ["一", "二", "三"], 1, False),
        (DIAGNOSTIC_IDS[1], "judge", "【本机合成首诊】每个圆都有圆心。", [], True, False),
    ]
    stems = {
        "single_choice": ("辨认锐角的特征", "选择长方形面积的单位", "识别分数的分母", "比较整数在数轴的位置"),
        "judge": ("平行线在同一平面内不相交", "等边三角形的三个角相等", "零既不是正数也不是负数", "圆的直径经过圆心"),
        "fill": ("写出一星期的天数", "写出一年通常的月份数", "写出直角的度数", "写出一小时的分钟数"),
    }
    for kind, values in stems.items():
        for index, stem in enumerate(values):
            options = ["本机合成选项甲", "本机合成选项乙", "本机合成选项丙"] if kind == "single_choice" else []
            answer = 1 if kind == "single_choice" else True if kind == "judge" else ("7", "12", "90", "60")[index]
            specs.append((f"loop-next-{kind}-{index}", kind, "【本机新题合成】" + stem, options, answer, True))
    for qid, kind, stem, options, answer, external in specs:
        learning.create_question_version(owner, qid, 1, {
            "type": kind, "stem": stem, "options": options, "answer": answer, "points": 1,
            "explanation": PRIVATE_MARKER, "family_id": "family-" + qid,
            "external_processing_permission": external, "difficulty": "medium", "knowledge_points": [],
        }, status="approved", course_id=course["id"])
    return {"course_id": course["id"], "class_id": classroom["id"], "foreign_class_id": foreign["id"],
            "user_ids": {actor: row["id"] for actor, row in users.items()}, "accounts": list(ACTORS),
            "question_count": len(specs)}, credentials


def run_child(output, data_dir, port):
    isolated_environment(data_dir)
    network = install_boundary(data_dir)
    sys.path.insert(0, str(SERVER))
    from app.config import Settings
    from app.main import create_app
    import uvicorn
    settings = Settings()
    if settings.llm_configured or settings.maxkb_url or settings.maxkb_api_key:
        raise RuntimeError("upstream isolation failed")
    prices = [{"provider": PROVIDER, "model": model, "version": PRICE_VERSION, "currency": CURRENCY,
               "input_units_per_million": 1_000_000, "output_units_per_million": 2_000_000}
              for model in MODELS.values()]
    settings = replace(settings, llm_providers=(), llm_prices_json=canonical(prices), budget_currency=CURRENCY,
                       worker_concurrency=1, worker_poll_seconds=0.1, worker_lease_seconds=3)
    application = create_app(settings)
    runtime = application.state.runtime
    seeded, credentials = seed_fixture(runtime, output)
    forbidden = [value for account in credentials.values() for value in (account["password"], account["username"], account["user_id"])]
    gateway = FakeGateway(output, forbidden)
    runtime.install("gateway", gateway)
    origin = f"http://127.0.0.1:{port}"
    write_json(output / "service-info.json", {
        "schema": "teaching-loop.live-fixture.v1", "status": "preparing", "fixture_only": True,
        "created_at": utc_now(), "pid": os.getpid(), "origin": origin, "prototype_url": origin + "/prototype/",
        "credentials_path": str(output / "credentials.local.json"), "stop_file": str(output / "stop.requested"),
        "data_dir": str(data_dir), "databases": list(DATABASES), **seeded,
        "boundaries": {"application": "real FastAPI/uvicorn/auth/six SQLite stores/dispatcher/worker",
                       "gateway": "LOCAL SYNTHETIC SELECTION ONLY; no provider or model-quality claim",
                       "direct_seed": "synthetic accounts, class grants, budget templates and approved questions only",
                       "business_transitions": "loopback HTTP; no seeded attempt, mastery, insight or next round",
                       "isolation": ".env prohibited before app import; LLM/MAXKB cleared; loopback sockets only",
                       "browser": "official browser operated by coordinator after complete preparation passes"},
    })
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, workers=1,
        lifespan="on", access_log=False, log_level="critical", log_config=None, timeout_graceful_shutdown=15))
    stopped = threading.Event()

    def watch_stop():
        while not stopped.wait(0.2):
            if (output / "stop.requested").exists():
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch_stop, name="teaching-loop-stop", daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join(timeout=2)
        runtime.stop()
        gateway.flush()
        write_json(output / "isolation.json", {
            "env_file_skipped": os.environ.get("SKIP_ENV_FILE") == "1",
            "model_configured": settings.llm_configured, "maxkb_configured": bool(settings.maxkb_url or settings.maxkb_api_key),
            "gateway": "local_synthetic_selection", "real_provider_calls": 0,
            "fake_gateway_calls": len(gateway.events), **network, "worker_after_stop": runtime.worker_status(),
        })
    return 0
