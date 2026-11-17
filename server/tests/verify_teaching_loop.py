"""Real loopback HTTP acceptance for the fourth-stage teaching loop.

Run with the existing environment: python -I -B -X utf8 verify_teaching_loop.py
Add --serve to retain the isolated world for the official browser, but only
AFTER every preparation stage passes. Touch the stop_file in service-info.json
for a graceful stop. Credentials are local-only and deleted at shutdown.
All gateway selections are synthetic; this does not evaluate a real provider.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import importlib.util
import json
import logging
import math
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

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2]
SUPPORT = SCRIPT.with_name("teaching_loop_fixture.py")
_spec = importlib.util.spec_from_file_location("_teaching_loop_fixture", SUPPORT)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
SCRATCH = fixture.SCRATCH
REQUIRED_STAGES = (
    "isolated_server_and_auth", "diagnostic_unknown_and_gates", "draft_cas_and_restricted_hints",
    "independent_evidence_and_retry", "frozen_summary_and_permissions", "invalid_output_and_job_recovery",
    "next_round_cas_preview_publish", "browser_scenarios_and_final_audit",
)
INSIGHTS = "/api/v1/teaching-insights"
COMPANION = "/api/v1/companion"
CORRECT = dict(zip(fixture.FORMAL_IDS, (1, True, "5")))
WRONG = dict(zip(fixture.FORMAL_IDS, (0, False, "0")))


class VerificationFailure(RuntimeError):
    pass


def output_directory(requested):
    # Do not invoke Git. The project has an explicit /.scratch/ ignore rule.
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    if "/.scratch/" not in rules:
        raise ValueError("the dedicated scratch output is not explicitly ignored")
    scratch = SCRATCH.resolve()
    output = requested.resolve() if requested else scratch / ("teaching-loop-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4))
    if output == scratch or not output.is_relative_to(scratch) or output.exists():
        raise ValueError("output must be a NEW directory below the ignored .scratch directory")
    output.mkdir(parents=True, exist_ok=False)
    return output


class Verification:
    def __init__(self, output, args):
        self.output, self.args = output, args
        self.started = time.monotonic()
        self.process = self.server_log = self.data_dir = None
        self.clients, self.accounts, self.info, self.tasks, self.conversations = {}, {}, {}, {}, {}
        self.secrets = set()
        self.serial = 0
        self.stage_name = "startup"
        self.evidence = {"schema": "teaching-loop.acceptance.v1", "fixture_only": True,
                         "status": "preparing", "started_at": fixture.utc_now(), "stages": [], "http": [],
                         "failures": [], "checks": 0, "real_provider_calls": 0,
                         "gateway_origin": "local_synthetic_selection", "gui_executed": False,
                         "claim": "real HTTP/auth/dispatcher/worker; local synthetic JSON choices, no provider-quality claim"}

    def key(self, label):
        self.serial += 1
        return "loop-" + label + "-" + str(self.serial)

    def clean(self, value):
        text = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)(password|authorization|cookie|lease_token|preview_token)([\s\"'=:\\]+)[^\s,;\"}]+", r"\1\2[redacted]", text)
        return text[:1600]

    def collect_private(self, value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                self.collect_private(item, name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.collect_private(item, key)
        elif isinstance(value, str) and any(part in key.lower() for part in ("password", "token", "cookie", "session", "username")):
            self.secrets.add(value)

    def save(self):
        self.evidence["http_requests"] = len(self.evidence["http"])
        fixture.write_json(self.output / "evidence.json", self.evidence)

    def check(self, condition, message):
        self.evidence["checks"] += 1
        if not condition:
            raise VerificationFailure(message)

    @contextmanager
    def stage(self, name):
        self.stage_name = name
        row = {"stage": name, "status": "running", "started_at": fixture.utc_now(),
               "http_start": len(self.evidence["http"])}
        self.evidence["stages"].append(row)
        self.save()
        print("RUN " + name, flush=True)
        try:
            yield row
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__, error=self.clean(error))
            self.evidence["failures"].append({"stage": name, "type": type(error).__name__, "message": self.clean(error)})
            print("FAIL " + name + ": " + self.clean(error), flush=True)
            raise
        else:
            row["status"] = "passed"
            print("PASS " + name, flush=True)
        finally:
            row.update(finished_at=fixture.utc_now(), http_requests=len(self.evidence["http"]) - row["http_start"])
            self.save()

    def request(self, actor, method, path, *, expected=200, **options):
        self.check(path.startswith("/") and not path.startswith("//"), "HTTP path escaped loopback origin")
        response = self.clients[actor].request(method, path, **options)
        for cookie in response.cookies.jar:
            self.secrets.add(cookie.value)
        try:
            body = response.json()
        except ValueError:
            body = None
        self.collect_private(body)
        detail = body.get("detail") if isinstance(body, dict) else None
        error_code = detail.get("code") if isinstance(detail, dict) else None
        row = {"actor": actor, "method": method, "path": path.split("?", 1)[0],
               "status": response.status_code, "bytes": len(response.content), "stage": self.stage_name}
        if error_code:
            row["error_code"] = error_code
        self.evidence["http"].append(row)
        allowed = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in allowed:
            # Never record request bodies, cookies, credentials or database rows.
            shape = list(body) if isinstance(body, dict) else type(body).__name__
            validation = []
            if isinstance(detail, list):
                validation = [{"loc": item.get("loc"), "type": item.get("type")} for item in detail if isinstance(item, dict)]
            raise VerificationFailure(f"{actor} {method} {path.split('?', 1)[0]}: expected {sorted(allowed)}, got {response.status_code}; code={error_code}; shape={shape}; validation={validation}")
        self.check(fixture.BAD_MARKER not in response.text, "unvalidated gateway prose escaped to HTTP")
        return response

    def api(self, actor, method, path, **options):
        return self.request(actor, method, path, **options).json()

    def poll(self, read, accept, label):
        deadline = time.monotonic() + self.args.timeout
        value = None
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise VerificationFailure("isolated server exited while waiting for " + label)
            value = read()
            if accept(value):
                return value
            time.sleep(0.12)
        state = value.get("status") if isinstance(value, dict) else type(value).__name__
        raise VerificationFailure(f"timed out waiting for {label}; last status={state}")

    def events(self):
        return json.loads((self.output / "gateway-events.json").read_text(encoding="utf-8"))["calls"]

    def control(self, purpose, mode):
        fixture.write_json(self.output / "gateway-control.json", {"purpose": purpose, "mode": mode, "nonce": self.key("control")})

    def sql(self, database, statement, values=()):
        # Read-only auditing is restricted to this launcher's six temporary files.
        self.check(database in fixture.DATABASES and statement.lstrip().upper().startswith("SELECT"), "invalid fixture audit query")
        path = (self.data_dir / (database + ".sqlite3")).resolve()
        self.check(path.parent == self.data_dir.resolve(), "audit database escaped temporary directory")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(statement, values)]

    def start_service(self, data_dir):
        self.data_dir = data_dir.resolve()
        environment = fixture.isolated_environment(self.data_dir)
        self.parent_boundary = fixture.install_boundary(self.data_dir)
        import httpx
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.server_log = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", str(SCRIPT), "--fixture-child",
             "--output-dir", str(self.output), "--data-dir", str(self.data_dir), "--port", str(port)],
            cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL, stdout=self.server_log, stderr=self.server_log,
        )
        for actor in ("anonymous", *fixture.ACTORS):
            self.clients[actor] = httpx.Client(base_url=self.origin, headers={"Origin": self.origin},
                                              trust_env=False, follow_redirects=False, timeout=15)
        deadline = time.monotonic() + self.args.timeout
        ready = False
        while time.monotonic() < deadline:
            self.check(self.process.poll() is None, "fixture child exited during startup; see sanitized child diagnostic")
            try:
                health = self.api("anonymous", "GET", "/health")
                ready = health.get("mode") == "platform" and health.get("workers", {}).get("running") == 1
                if ready:
                    self.check(not health["workers"]["needs_attention"], "worker needs attention at startup")
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        self.check(ready, "real HTTP application did not become healthy")
        self.info = json.loads((self.output / "service-info.json").read_text(encoding="utf-8"))
        private = json.loads((self.output / "credentials.local.json").read_text(encoding="utf-8"))
        self.collect_private(private)
        self.accounts = private["accounts"]
        self.check(self.info["status"] == "preparing", "fixture announced ready before HTTP preparation")
        self.check({path.stem for path in self.data_dir.glob("*.sqlite3")} == set(fixture.DATABASES), "fixture did not create exactly six databases")
        self.request("anonymous", "GET", "/api/v1/tasks", expected=401)
        for actor in fixture.ACTORS:
            account = self.accounts[actor]
            self.api(actor, "POST", "/api/v1/auth/login", json={key: account[key] for key in ("username", "password", "role")})
            self.check(bool(self.clients[actor].cookies), "real cookie login failed")
            self.api(actor, "GET", "/api/v1/auth/me")
        self.request("teacher", "POST", "/api/v1/goals", json={}, headers={"Origin": "http://localhost:1"}, expected=403)
        self.evidence["startup"] = {"six_databases": True, "real_auth_accounts": len(fixture.ACTORS),
                                    "worker_running": 1, "origin": self.origin}

    def create_goal(self, actors, *, diagnostic=False, title="第四阶段合成闭环目标"):
        goal = self.api("teacher", "POST", "/api/v1/goals", expected=201, json={
            "request_id": self.key("goal"), "title": title, "description": "仅本机合成验收",
            "course_id": self.info["course_id"], "class_id": self.info["class_id"],
        })
        body = {"request_id": self.key("round"), "expected_version": goal["version"], "round_no": 1,
                "student_ids": [self.accounts[actor]["user_id"] for actor in actors],
                "question_versions": [{"question_id": qid, "version": 1} for qid in fixture.FORMAL_IDS],
                "diagnostic_question_versions": [{"question_id": qid, "version": 1} for qid in fixture.DIAGNOSTIC_IDS] if diagnostic else [],
                "allow_late": False, "allow_retries": True}
        self.api("teacher", "POST", f"/api/v1/goals/{goal['id']}/rounds", expected=201, json=body)
        tasks = self.api("teacher", "GET", "/api/v1/tasks", params={"goal_id": goal["id"]})["items"]
        mapping = {actor: next(row for row in tasks if row["student_id"] == self.accounts[actor]["user_id"])["id"] for actor in actors}
        self.check(len(tasks) == len(actors), "round task scope differs")
        return goal, mapping

    def conversation(self, actor):
        if actor not in self.conversations:
            row = self.api(actor, "POST", COMPANION + "/conversations", expected=201,
                           json={"title": "本机有限提示验收", "course_id": self.info["course_id"], "request_id": self.key("conversation")})
            self.conversations[actor] = row["id"]
        return self.conversations[actor]

    def policy(self, actor, qid=None, task=None):
        params = {"task_id": task or self.tasks[actor]}
        if qid:
            params["question_version_id"] = qid + ":1"
        return self.api(actor, "GET", COMPANION + "/answer-policy", params=params)

    def hint_request(self, actor, qid, level, *, expected=202, request_id=None, task=None):
        body = {"request_id": request_id or self.key("hint"), "text": "只请求本题允许的有限提示",
                "task_id": task or self.tasks[actor], "question_version_id": qid + ":1", "intent": "hint", "hint_level": level}
        return self.api(actor, "POST", COMPANION + f"/conversations/{self.conversation(actor)}/messages", expected=expected, json=body)

    def response(self, actor, response_id):
        row = self.poll(lambda: self.api(actor, "GET", COMPANION + "/responses/" + response_id),
                        lambda value: value["status"] in {"completed", "failed", "cancelled"}, "hint response")
        if row["status"] == "completed":
            # Companion commits its hidden template before learning commits the
            # delivery evidence. Queue success acknowledges both durable writes.
            self.job(actor, row["job_id"])
            row = self.api(actor, "GET", COMPANION + "/responses/" + response_id)
        return row

    def job(self, actor, job_id, states=("succeeded",)):
        def read():
            response = self.request(actor, "GET", "/api/v1/jobs/" + job_id, expected={200, 404})
            return response.json() if response.status_code == 200 else {"status": "not_dispatched"}
        row = self.poll(read, lambda value: value["status"] in set(states) | {"failed", "cancelled", "awaiting_confirmation", "reconciling"}, "background job")
        self.check(row["status"] in states, "background job ended in " + row["status"] + "; expected " + ",".join(states))
        return row

    def cancel_held_insight(self, insight, *, calls_before):
        """Wait through local no-send cleanup, never accept unresolved cancellation.

        FakeGateway's hold cancellation explicitly raises request_sent=False.
        Worker first commits unknown/reconciling, then reconciles not_executed
        in a second transaction. Only this fixture scenario may cross that
        window; job() and real unknown-outcome recovery remain strict.
        """
        job_id, insight_id = insight["job_id"], insight["id"]
        call_id = f"{job_id}:summary:1"
        path = "/api/v1/jobs/" + job_id
        audit = {"job_id": job_id, "call_id": call_id, "status": "running",
                 "contract": "local_hold_cancel_request_sent_false", "observations": []}
        self.evidence["hold_cancel"] = audit

        def observe(row, source):
            fields = ("status", "cancel_requested", "reconciliation_required", "cost_pending")
            audit["observations"].append({"observed_at": fixture.utc_now(), "source": source,
                                          **{key: row.get(key) for key in fields}})
            self.save()
            return row

        def matching_event(rows):
            return next((row for row in rows if row["call_id"] == call_id), None)

        def held(rows):
            event = matching_event(rows)
            return event is not None and event["mode"] == "hold" and event["status"] == "running"

        def read():
            row = observe(self.api("teacher", "GET", path), "poll")
            self.check(row["cancel_requested"] is True, "held summary lost its cancellation request")
            self.check(row["status"] in {"running", "reconciling", "cancelled"},
                       "held summary reached unexpected status " + row["status"])
            return row

        def capture():
            # Read-only metadata survives fixture cleanup. Never retain model
            # selections, event payloads, request bodies, credentials or results.
            errors = []

            def job_snapshot():
                row = self.api("teacher", "GET", path)
                return {key: row.get(key) for key in
                        ("status", "cancel_requested", "reconciliation_required", "cost_pending")}

            def call_snapshot():
                return [{key: row.get(key) for key in
                         ("call_id", "status", "cost_units", "cost_pending", "created_at", "updated_at")}
                        for row in self.api("teacher", "GET", path + "/calls")["items"]]

            def gateway_snapshot():
                rows = self.events()
                event = matching_event(rows)
                audit["gateway_calls_after"] = len(rows)
                return None if event is None else {key: event.get(key) for key in
                        ("call_id", "purpose", "mode", "status", "started_at", "finished_at")}

            def domain_snapshot():
                row = self.api("teacher", "GET", INSIGHTS + "/" + insight_id)
                return {"status": row["status"], "result_is_null": row["result"] is None}

            snapshots = (("final_job", job_snapshot), ("calls", call_snapshot),
                         ("queue_events", lambda: self.sql("jobs",
                          "SELECT event_id,event_type,created_at FROM events WHERE job_id=? ORDER BY event_id", (job_id,))),
                         ("gateway", gateway_snapshot), ("domain", domain_snapshot))
            for name, snapshot in snapshots:
                try:
                    audit[name] = snapshot()
                except Exception as error:
                    errors.append({"source": name, "error_type": type(error).__name__})
            if errors:
                audit["capture_errors"] = errors
            self.save()
            return errors

        try:
            rows = self.poll(self.events, held, "held local gateway call")
            self.check(len(rows) == calls_before + 1, "held summary added unexpected gateway calls")
            audit["gateway_calls_before_cancel"] = len(rows)
            observe(self.api("teacher", "POST", path + "/cancel"), "cancel_response")
            self.poll(read, lambda row: row["status"] == "cancelled", "held unsent summary cancellation")
            rejected = self.api("teacher", "POST", path + "/resume", json={"confirm_paid": True}, expected=409)
            audit["resume"] = {"http_status": 409, "error_code": rejected.get("detail", {}).get("code")}
            self.check(audit["resume"]["error_code"] == "cancelled_job", "cancelled summary resume was not rejected")
            self.check(not capture(), "held cancellation metadata capture failed")
            final = audit["final_job"]
            self.check(final["status"] == "cancelled" and final["cancel_requested"] is True
                       and final["reconciliation_required"] is False and final["cost_pending"] is False,
                       "held summary cancellation did not settle the job")
            calls = audit["calls"]
            self.check(len(calls) == 1 and calls[0]["call_id"] == call_id
                       and calls[0]["status"] == "not_executed" and calls[0]["cost_units"] == 0
                       and calls[0]["cost_pending"] is False, "held unsent call was not settled once at zero cost")
            self.check(audit["domain"] == {"status": "cancelled", "result_is_null": True},
                       "cancelled summary published a result")
            event = audit["gateway"] or {}
            self.check(event.get("mode") == "hold" and event.get("status") == "cancelled_before_result",
                       "held gateway did not confirm its no-send cancellation")
            self.check(audit["gateway_calls_after"] == calls_before + 1, "cancelled summary resent a gateway call")
        except Exception as error:
            audit.update(status="failed", error_type=type(error).__name__)
            capture()
            raise
        else:
            audit["status"] = "passed"
            self.save()

    def lease(self, actor, task=None):
        return self.api(actor, "POST", "/api/v1/tasks/" + (task or self.tasks[actor]) + "/edit-leases",
                        json={"device_id": "loop-http-" + actor, "lease_seconds": 300, "takeover": True})

    def save_draft(self, actor, answers, *, task=None):
        task = task or self.tasks[actor]
        lease = self.lease(actor, task)
        body = {"request_id": self.key("draft"), "expected_version": lease["task_version"], "answers": answers,
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}
        saved = self.api(actor, "PUT", "/api/v1/tasks/" + task + "/draft", json=body)
        return saved, lease, body

    def submit(self, actor, answers, *, task=None, replay=False):
        task = task or self.tasks[actor]
        saved, lease, _ = self.save_draft(actor, answers, task=task)
        body = {"request_id": self.key("submit"), "expected_version": saved["version"],
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"]}
        self.api(actor, "POST", "/api/v1/tasks/" + task + "/submit", expected=202, json=body)
        if replay:
            self.api(actor, "POST", "/api/v1/tasks/" + task + "/submit", expected=202, json=body)
        result = self.poll(lambda: self.api(actor, "GET", "/api/v1/tasks/" + task),
                           lambda row: row["status"] == "results_ready", "real worker objective grading")
        self.job(actor, f"grade:{task}:{result['attempts'][-1]['attempt_no']}")
        return result

    def report(self, goal_id):
        scope = {"kind": "goal", "goal_id": goal_id, "class_id": self.info["class_id"], "course_id": self.info["course_id"]}
        source = self.api("teacher", "GET", "/api/v1/reports/source-revision", params=scope)
        result = self.api("teacher", "POST", "/api/v1/reports", expected=201, headers={"Idempotency-Key": self.key("report")},
                          json={"scope": scope, "schema_version": "report.snapshot.v1", "rule_version": source["default_rule_version"],
                                "expected_source_revision": source["source_revision"]})
        self.check(fixture.digest(result["snapshot"]) == result["snapshot_sha256"], "frozen report hash differs")
        return result

    def insight(self, report, *, mode="valid", wait=True, request_id=None):
        self.control("summary", mode)
        body = {"report_id": report["report_id"], "source_report_revision": report["id"], "request_id": request_id or self.key("insight")}
        row = self.api("teacher", "POST", INSIGHTS, expected=202, json=body)
        if wait:
            row = self.poll(lambda: self.api("teacher", "GET", INSIGHTS + "/" + row["id"]),
                            lambda value: value["status"] in {"completed", "failed", "cancelled"}, "teaching insight")
        return row, body

    def draft(self, insight, *, config=None):
        body = {"request_id": self.key("next-draft")}
        if config:
            body["config"] = config
        return self.api("teacher", "POST", INSIGHTS + "/" + insight["id"] + "/next-round-drafts", expected=201, json=body)

    @staticmethod
    def draft_path(draft):
        return INSIGHTS + "/next-round-drafts/" + draft["id"]

    def run_diagnostics(self):
        with self.stage("diagnostic_unknown_and_gates"):
            self.goal, self.tasks = self.create_goal(fixture.STUDENTS, diagnostic=True)
            self.conversation("student")
            task = self.api("student", "GET", "/api/v1/tasks/" + self.tasks["student"])
            self.check(task["diagnostic_required"] and task["questions"] == [] and not task["attempts"], "diagnostic gate exposed formal questions or attempts")
            self.check(self.policy("student")["diagnostic"] is True, "diagnostic policy was not active")
            before = len(self.events())
            self.hint_request("student", fixture.FORMAL_IDS[0], 1, expected=403)
            self.request("student", "POST", "/api/v1/tasks/" + self.tasks["student"] + "/edit-leases",
                         json={"device_id": "diagnostic-blocked"}, expected=409)
            for actor in fixture.STUDENTS:
                task = self.api(actor, "GET", "/api/v1/tasks/" + self.tasks[actor])
                started = self.api(actor, "POST", "/api/v1/tasks/" + self.tasks[actor] + "/diagnostics", expected=201,
                                   json={"request_id": self.key("diagnostic"), "expected_version": task["version"]})
                path = "/api/v1/diagnostics/" + started["id"]
                view = self.api(actor, "GET", path)
                self.check(view["hints_allowed"] is False and view["counts_toward_formal_score"] is False, "diagnostic affects formal score or allows hints")
                self.check(len(view["questions"]) == 2 and fixture.PRIVATE_MARKER not in fixture.canonical(view), "diagnostic question projection is unsafe")
                if actor == "student":
                    self.request(actor, "POST", path + "/submit", expected=422, json={"request_id": self.key("overlap"),
                                 "answers": {fixture.DIAGNOSTIC_IDS[0]: 1}, "unknown_question_ids": list(fixture.DIAGNOSTIC_IDS)})
                    self.request("peer", "GET", path, expected={403, 404})
                body = {"request_id": self.key("unknown"), "answers": {}, "unknown_question_ids": list(fixture.DIAGNOSTIC_IDS)}
                self.api(actor, "POST", path + "/submit", json=body)
                self.api(actor, "POST", path + "/submit", json=body)
                view = self.api(actor, "GET", path)
                self.check(view["status"] == "completed" and view["feedback"]["unknown"] == 2 and view["feedback"]["answered"] == 0,
                           "all-unknown diagnostic did not complete correctly")
                opened = self.api(actor, "GET", "/api/v1/tasks/" + self.tasks[actor])
                self.check(not opened["diagnostic_required"] and len(opened["questions"]) == 3 and not opened["attempts"],
                           "diagnostic made a formal attempt or did not unlock formal paper")
            self.check(len(self.events()) == before, "diagnostic invoked gateway")
            events = self.api("teacher", "GET", f"/api/v1/goals/{self.goal['id']}/evidence")["items"]
            self.check(not any(row.get("event_type") == "attempt_submitted" for row in events), "diagnostic was counted as formal submission")

    def run_hints(self):
        with self.stage("draft_cas_and_restricted_hints"):
            qchoice, qjudge, qfill = fixture.FORMAL_IDS
            before = len(self.events())
            self.check(not self.policy("student", qchoice)["allow_hint_content"], "unattempted question allowed hints")
            self.hint_request("student", qchoice, 1, expected=409)
            saved, lease, body = self.save_draft("student", {qchoice: 1})
            path = "/api/v1/tasks/" + self.tasks["student"] + "/draft"
            replay = self.api("student", "PUT", path, json=body)
            self.check(replay["version"] == saved["version"] and replay["draft_answers"] == saved["draft_answers"], "draft replay changed saved version")
            self.request("student", "PUT", path, expected=409, json={**body, "answers": {qchoice: 0}})
            self.request("student", "PUT", path, expected=409, json={**body, "request_id": self.key("stale-draft")})
            new_lease = self.lease("student")
            self.request("student", "PUT", path, expected=409,
                         json={**body, "request_id": self.key("stale-fence"), "expected_version": new_lease["task_version"]})
            self.check(not self.policy("student", qjudge)["allow_hint_content"], "another question's draft unlocked hints")
            self.hint_request("student", qjudge, 1, expected=409)
            self.hint_request("student", "loop-next-judge-0", 1, expected=403)
            self.check(len(self.events()) == before, "blocked hint sent a model request")
            self.check(self.policy("student", qchoice)["allowed_hint_levels"] == [1, 2], "two hint levels were not available")
            self.hint_ids = []
            for level in (1, 2):
                request_id = self.key("valid-hint")
                posted = self.hint_request("student", qchoice, level, request_id=request_id)
                replay = self.hint_request("student", qchoice, level, request_id=request_id)
                self.check(replay["response_id"] == posted["response_id"], "hint replay created another response")
                response = self.response("student", posted["response_id"])
                self.check(response["status"] == "completed" and response["content_type"] == "restricted_hint"
                           and response["hint_metadata"]["status"] == "validated_delivered" and not response.get("redacted"),
                           "validated finite hint was not delivered")
                self.check(response["hint_level"] == level and response["hint_metadata"]["action_id"] in
                           {1: {"recall_choice"}, 2: {"next_choice_reason"}}[level], "hint action escaped its level catalogue")
                self.job("student", response["job_id"])
                self.hint_ids.append(response["response_id"])
                self.request("student", "GET", COMPANION + "/responses/" + response["response_id"] + "/stream", params={"once": "true"})
                self.api("student", "GET", COMPANION + "/responses/" + response["response_id"] + "/events")
            self.check(len(self.events()) == before + 2, "two replayed hint requests did not produce exactly two calls")
            self.save_draft("student", CORRECT)
            self.control("hint", "invalid_prose")
            bad = self.hint_request("student", qjudge, 1)
            bad_view = self.response("student", bad["response_id"])
            self.check(bad_view["status"] == "completed" and bad_view["content_type"] == "safe_guidance"
                       and bad_view["hint_metadata"]["status"] == "fallback", "invalid hint output did not use safe fallback")
            self.job("student", bad_view["job_id"])
            before = len(self.events())
            policy = self.policy("student", qfill)
            self.check(policy["allow_hint_content"] and not policy["allow_hint_generation"], "external-processing permission gate differs")
            denied = self.hint_request("student", qfill, 2)
            denied_view = self.response("student", denied["response_id"])
            self.check(denied_view["hint_metadata"]["status"] == "fallback" and denied_view["content_type"] == "safe_guidance",
                       "no-external-permission hint did not use fallback")
            self.job("student", denied_view["job_id"])
            self.check(len(self.events()) == before, "no-permission hint invoked gateway")
            help_path = "/api/v1/tasks/" + self.tasks["student"] + "/help-events"
            help_body = {"request_id": self.key("requested-help"), "question_id": qjudge,
                         "payload": {"hint_level": 1, "status": "requested"}}
            count_sql = "SELECT COUNT(*) AS count FROM task_events WHERE task_id=? AND question_id=? AND event_type='help'"
            count_args = (self.tasks["student"], qjudge)
            before_help = self.sql("learning", count_sql, count_args)[0]["count"]
            help_event = self.api("student", "POST", help_path, expected=201, json=help_body)
            replay = self.api("student", "POST", help_path, expected=201, json=help_body)
            after_help = self.sql("learning", count_sql, count_args)[0]["count"]
            self.check(help_event == replay and after_help == before_help + 1, "client help request replay created evidence twice")
            self.request("student", "POST", help_path, expected=422,
                         json={**help_body, "request_id": self.key("forge-help"), "payload": {"status": "validated_delivered", "hint_level": 1}})
            for actor in ("peer", "teacher", "readonly", "outsider"):
                self.request(actor, "GET", COMPANION + "/responses/" + self.hint_ids[0], expected={403, 404})
            self.request("peer", "GET", f"/api/v1/goals/{self.goal['id']}/evidence",
                         params={"student_id": self.accounts["student"]["user_id"]}, expected=403)
            self.evidence["hints"] = {"validated_levels": [1, 2], "invalid_output_fallback": True,
                                      "no_permission_zero_calls": True, "json_mode": True, "stream": False}

    def run_independence(self):
        with self.stage("independent_evidence_and_retry"):
            self.first_results = {}
            for actor, answers in (("student", CORRECT), ("peer", CORRECT), ("novice", WRONG)):
                result = self.submit(actor, answers, replay=True)
                self.check(len(result["attempts"]) == 1 and result["attempts"][0]["kind"] == "first", "submit replay duplicated first attempt")
                self.first_results[actor] = result
            before = len(self.events())
            peer = self.first_results["peer"]
            retry_body = {"request_id": self.key("retry"), "expected_version": peer["version"]}
            retry = self.api("peer", "POST", "/api/v1/tasks/" + self.tasks["peer"] + "/retry", json=retry_body)
            repeated = self.api("peer", "POST", "/api/v1/tasks/" + self.tasks["peer"] + "/retry", json=retry_body)
            self.check(repeated["version"] == retry["version"] and not retry["answer_released"], "retry replay/version or answer gate differs")
            self.check(all("answer" not in question and "explanation" not in question for question in retry["questions"]), "retry exposed answer key")
            retried = self.submit("peer", CORRECT, replay=True)
            self.check(len(retried["attempts"]) == 2 and retried["first_attempt_id"] == peer["first_attempt_id"]
                       and retried["attempts"][1]["kind"] == "retry" and not retried["attempts"][1]["independent_evidence"],
                       "retry replaced first attempt or gained independent credit")
            self.check(len(self.events()) == before, "objective grading unexpectedly invoked gateway")
            self.frozen = self.report(self.goal["id"])
            tables = self.frozen["snapshot"]["tables"]
            mastery = tables["mastery"]["rows"]
            expected = {"student": ("insufficient_evidence", None, 2), "peer": ("mastered", 100.0, 3),
                        "novice": ("needs_practice", 0.0, 3), "pending": ("insufficient_evidence", None, 0)}
            for actor, (status, percent, families) in expected.items():
                row = next(item for item in mastery if item["student_id"] == self.accounts[actor]["user_id"])
                self.check((row["status"], row["percent"], row["independent_families"]) == (status, percent, families),
                           "mastery independence mismatch for " + actor + f"; status={row['status']}; families={row['independent_families']}")
            evidence = tables["evidence"]["rows"]
            student = [row for row in evidence if row["student_id"] == self.accounts["student"]["user_id"]]
            self.check(len(student) == 3 and sum(row["exposure"] == "hinted" for row in student) == 1
                       and sum(row["exposure"] == "independent" for row in student) == 2,
                       "fallback/requested evidence was counted as delivered assistance")
            retries = [row for row in evidence if row["attempt_number"] > 1]
            self.check(len(retries) == 3 and all(row["exposure"] in {"redo", "answer_exposed"} for row in retries),
                       "report credited retry independently")
            self.request("outsider", "GET", f"/api/v1/goals/{self.goal['id']}/evidence", expected={403, 404})
            self.evidence["mastery_expectations"] = {actor: {"status": value[0], "percent": value[1], "independent_families": value[2]}
                                                      for actor, value in expected.items()}

    def run_summary(self):
        with self.stage("frozen_summary_and_permissions"):
            before = len(self.events())
            self.summary, body = self.insight(self.frozen)
            self.check(self.summary["status"] == "completed", "valid synthetic summary did not complete")
            self.job("teacher", self.summary["job_id"])
            replay = self.api("teacher", "POST", INSIGHTS, expected=202, json=body)
            self.check(replay["id"] == self.summary["id"] and len(self.events()) == before + 1, "summary replay called gateway twice")
            self.check(self.summary["source_report_revision"] == self.frozen["id"]
                       and self.summary["snapshot_sha256"] == self.frozen["snapshot_sha256"], "summary did not bind frozen report")
            self.check(self.summary["result"]["interpretation_mode"] == "validated_selection"
                       and len(self.summary["result"]["findings"]) == 4, "summary escaped finite selection contract")
            self.frozen_result_hash = fixture.digest(self.summary["result"])
            for actor in ("teacher", "readonly"):
                self.api(actor, "GET", INSIGHTS, params={"class_id": self.info["class_id"], "goal_id": self.goal["id"]})
                self.api(actor, "GET", INSIGHTS + "/" + self.summary["id"])
                self.api(actor, "GET", "/api/v1/jobs/" + self.summary["job_id"])
                self.api(actor, "GET", "/api/v1/jobs/" + self.summary["job_id"] + "/calls")
            self.request("readonly", "POST", INSIGHTS, expected=404, json={**body, "request_id": self.key("readonly")})
            self.request("readonly", "POST", "/api/v1/jobs/" + self.summary["job_id"] + "/cancel", expected=404)
            for actor in ("outsider", "student"):
                self.request(actor, "GET", INSIGHTS + "/" + self.summary["id"], expected={403, 404})
                self.request(actor, "GET", "/api/v1/jobs/" + self.summary["job_id"], expected=404)
                self.request(actor, "GET", "/api/v1/jobs/" + self.summary["job_id"] + "/calls", expected=404)
            notices = self.poll(lambda: self.api("teacher", "GET", "/api/v1/me/notifications"),
                                lambda rows: any(row["resource_id"] == self.summary["id"] for row in rows), "summary outbox notification")
            selected = [row for row in notices if row["resource_id"] == self.summary["id"] and row["event_type"] == "teaching_insight_ready"]
            self.check(len(selected) == 1 and not selected[0].get("redacted"), "summary ready notification missing or duplicated")
            self.check(fixture.BAD_MARKER not in fixture.canonical(notices), "notification contains unvalidated model prose")

    def run_recovery(self):
        with self.stage("invalid_output_and_job_recovery"):
            invalid_ids = []
            for mode in ("invalid_fact", "invalid_prose"):
                row, body = self.insight(self.frozen, mode=mode)
                self.check(row["status"] == "failed" and row["result"] is None
                           and row["error"]["code"] == "invalid_model_output", "invalid summary was not rejected: " + mode)
                self.job("teacher", row["job_id"], ("failed",))
                self.request("teacher", "POST", "/api/v1/jobs/" + row["job_id"] + "/resume",
                             json={"confirm_paid": True}, expected=409)
                before = len(self.events())
                replay = self.api("teacher", "POST", INSIGHTS, expected=202, json=body)
                self.check(replay["id"] == row["id"] and replay["status"] == "failed" and len(self.events()) == before,
                           "invalid summary replay silently retried bad output")
                invalid_ids.append(row["id"])
            fresh, _ = self.insight(self.frozen)
            self.check(fresh["status"] == "completed" and fresh["id"] not in invalid_ids, "new summary request did not recover from invalid output")
            self.job("teacher", fresh["job_id"])
            failed, _ = self.insight(self.frozen, mode="fail")
            self.check(failed["status"] == "failed", "known gateway failure did not finalize domain")
            self.job("teacher", failed["job_id"], ("failed",))
            self.request("teacher", "POST", "/api/v1/jobs/" + failed["job_id"] + "/resume", json={"confirm_paid": False}, expected=409)
            self.api("teacher", "POST", "/api/v1/jobs/" + failed["job_id"] + "/resume", json={"confirm_paid": True})
            self.job("teacher", failed["job_id"])
            recovered = self.api("teacher", "GET", INSIGHTS + "/" + failed["id"])
            self.check(recovered["status"] == "completed", "confirmed failed summary did not recover")
            unknown, _ = self.insight(self.frozen, mode="unknown", wait=False)
            job = self.job("teacher", unknown["job_id"], ("reconciling",))
            self.check(job["reconciliation_required"], "unknown call was not parked for reconciliation")
            calls = self.api("teacher", "GET", "/api/v1/jobs/" + unknown["job_id"] + "/calls")["items"]
            self.check(len(calls) == 1 and calls[0]["status"] == "unknown", "unknown call ledger missing")
            event = next(row for row in self.events() if row["call_id"] == calls[0]["call_id"])
            before = len(self.events())
            self.api("admin", "POST", "/api/v1/admin/jobs/" + unknown["job_id"] + "/calls/" + calls[0]["call_id"] + "/reconcile",
                     json={"outcome": "succeeded", "result": event["reconcile_result"], "usage": fixture.USAGE,
                           "cost_units": fixture.COST, "reason": "本机合成调用的持久结果对账"})
            self.job("teacher", unknown["job_id"], ("awaiting_confirmation",))
            self.request("teacher", "POST", "/api/v1/jobs/" + unknown["job_id"] + "/resume", json={"confirm_paid": False}, expected=409)
            self.api("teacher", "POST", "/api/v1/jobs/" + unknown["job_id"] + "/resume", json={"confirm_paid": True})
            self.job("teacher", unknown["job_id"])
            recovered = self.api("teacher", "GET", INSIGHTS + "/" + unknown["id"])
            self.check(recovered["status"] == "completed" and len(self.events()) == before, "reconciled summary resent gateway call")
            before = len(self.events())
            cancelled, _ = self.insight(self.frozen, mode="hold", wait=False)
            self.cancel_held_insight(cancelled, calls_before=before)
            self.evidence["recovery"] = {"invalid_output_requires_new_request": True, "failed_confirmed": True,
                                        "unknown_reconciled_without_resend": True, "awaiting_confirmation_confirmed": True,
                                        "cancelled_resume_conflict": True}

    def run_next_round(self):
        with self.stage("next_round_cas_preview_publish"):
            draft = self.draft(self.summary)
            path = self.draft_path(draft)
            self.api("readonly", "GET", path)
            self.request("outsider", "GET", path, expected=404)
            preview = self.api("teacher", "POST", path + "/preview", json={"expected_revision": draft["revision"]})
            self.check(not preview["ready"] and preview["preview_token"] is None, "incomplete previous round was publishable")
            self.submit("pending", CORRECT)
            frozen = self.api("teacher", "GET", f"/api/v1/reports/{self.frozen['report_id']}/revisions/{self.frozen['id']}")
            self.check(frozen["snapshot_sha256"] == self.frozen["snapshot_sha256"]
                       and fixture.digest(frozen["snapshot"]) == self.frozen["snapshot_sha256"], "old report changed after new evidence")
            summary = self.api("teacher", "GET", INSIGHTS + "/" + self.summary["id"])
            self.check(summary["stale"] and fixture.digest(summary["result"]) == self.frozen_result_hash,
                       "old summary did not remain frozen and stale")
            config = {**draft["config"], "title": "人工编辑的下一轮（本机合成）"}
            save = {"request_id": self.key("next-save"), "expected_revision": draft["revision"], "config": config, "paper_overrides": {}}
            self.request("readonly", "PUT", path, json=save, expected=404)
            changed = self.api("teacher", "PUT", path, json=save)
            replay = self.api("teacher", "PUT", path, json=save)
            self.check(changed["revision"] == draft["revision"] + 1 and replay["revision"] == changed["revision"], "next draft save CAS/replay differs")
            self.request("teacher", "PUT", path, expected=409, json={**save, "request_id": self.key("next-stale")})
            self.request("teacher", "PUT", path, expected=409, json={**save, "config": {**config, "title": "conflicting replay"}})
            self.request("teacher", "POST", path + "/preview", expected=409, json={"expected_revision": draft["revision"]})
            preview = self.api("teacher", "POST", path + "/preview", json={"expected_revision": changed["revision"]})
            self.check(preview["ready"] and bool(preview["preview_token"]), "completed first round lacks a valid next preview")
            self.check(preview["origin_goal_id"] == self.goal["id"] and preview["next_round"] == 2, "preview changed original goal or round")
            self.check(all(question["question_id"] not in fixture.FORMAL_IDS for question in preview["questions"]), "next round reused old questions")
            # A real revision invalidates a real preview token.
            revised = self.api("teacher", "PUT", path, json={"request_id": self.key("next-revise"),
                               "expected_revision": changed["revision"], "config": {**config, "description": "再次编辑以核验旧预览失效"}, "paper_overrides": {}})
            old_publish = {"request_id": self.key("old-preview"), "expected_revision": revised["revision"],
                           "preview_token": preview["preview_token"], "confirm": True}
            self.request("teacher", "POST", path + "/publish", expected=409, json=old_publish)
            shortage_config = {**config, "question_count": 30, "question_types": {"single_choice": 10, "judge": 10, "fill": 10, "text": 0}}
            shortage = self.draft(self.summary, config=shortage_config)
            blocked = self.api("teacher", "POST", self.draft_path(shortage) + "/preview", json={"expected_revision": shortage["revision"]})
            self.check(not blocked["ready"] and blocked["preview_token"] is None
                       and any(error["code"] == "question_shortage" for error in blocked["errors"]), "question shortage was not blocked")
            self.request("teacher", "POST", self.draft_path(shortage) + "/publish", expected=409,
                         json={"request_id": self.key("shortage-publish"), "expected_revision": shortage["revision"], "preview_token": "invalid", "confirm": True})
            preview = self.api("teacher", "POST", path + "/preview", json={"expected_revision": revised["revision"]})
            body = {"request_id": self.key("next-publish"), "expected_revision": revised["revision"], "preview_token": preview["preview_token"], "confirm": True}
            self.request("teacher", "POST", path + "/publish", expected=422, json={**body, "confirm": False})
            self.request("teacher", "POST", path + "/publish", expected=422, json={key: value for key, value in body.items() if key != "confirm"})
            published = self.api("teacher", "POST", path + "/publish", expected=201, json=body)
            replay = self.api("teacher", "POST", path + "/publish", expected=201, json=body)
            self.check(published == replay and published["goal_id"] == self.goal["id"] and published["round_no"] == 2
                       and len(published["task_ids"]) == 4, "confirmed next round did not reuse goal/idempotent tasks")
            self.request("teacher", "POST", path + "/publish", expected=409, json={**body, "request_id": self.key("republish")})
            goal = self.api("teacher", "GET", "/api/v1/goals/" + self.goal["id"])
            self.check(len(goal["rounds"]) == 2, "publish replay duplicated rounds")
            self.published = published
            self.evidence["next_round"] = {"origin_goal_preserved": True, "rounds": 2, "new_tasks": 4,
                                           "explicit_confirmation": True, "stale_preview_rejected": True,
                                           "shortage_blocked": True, "publish_replay_deduplicated": True}

    def run_browser_preparation(self):
        with self.stage("browser_scenarios_and_final_audit"):
            # Leave useful editable HTTP-created scenarios; browser actions are not simulated here.
            diagnostic_goal, diagnostic_tasks = self.create_goal(("diagnostic",), diagnostic=True, title="浏览器首诊与暂时不会（本机合成）")
            diagnostic_task = diagnostic_tasks["diagnostic"]
            view = self.api("diagnostic", "GET", "/api/v1/tasks/" + diagnostic_task)
            self.check(view["diagnostic_required"] and not view["questions"], "browser diagnostic fixture was already completed")
            browser_goal, browser_tasks = self.create_goal(("pending",), title="浏览器教师下一轮编辑（本机合成）")
            self.submit("pending", CORRECT, task=browser_tasks["pending"])
            browser_report = self.report(browser_goal["id"])
            browser_summary, _ = self.insight(browser_report)
            self.check(browser_summary["status"] == "completed", "browser summary not prepared")
            self.job("teacher", browser_summary["job_id"])
            browser_draft = self.draft(browser_summary)
            preview = self.api("teacher", "POST", self.draft_path(browser_draft) + "/preview", json={"expected_revision": browser_draft["revision"]})
            self.check(preview["ready"], "browser editable next round is not ready")
            next_tasks = self.api("student", "GET", "/api/v1/tasks", params={"goal_id": self.goal["id"]})["items"]
            next_task = next(row for row in next_tasks if row["round_id"] == self.published["round_id"])
            question = next((row for row in next_task["questions"] if row["id"].startswith("loop-next-")
                             and row["type"] in {"single_choice", "judge", "fill"}), None)
            self.check(question is not None, "browser hint scenario has no external-permitted question")
            attempted_answer = {"single_choice": 1, "judge": True, "fill": "0"}[question["type"]]
            self.save_draft("student", {question["id"]: attempted_answer}, task=next_task["id"])
            self.check(self.policy("student", question["id"], next_task["id"])["allow_hint_generation"], "browser saved attempt did not allow finite hint")
            self.info["scenarios"] = {"accepted_goal_id": self.goal["id"], "frozen_report_id": self.frozen["report_id"],
                                      "frozen_revision_id": self.frozen["id"], "frozen_insight_id": self.summary["id"],
                                      "diagnostic_goal_id": diagnostic_goal["id"], "diagnostic_task_id": diagnostic_task,
                                      "teacher_goal_id": browser_goal["id"], "teacher_report_id": browser_report["report_id"],
                                      "teacher_revision_id": browser_report["id"], "teacher_insight_id": browser_summary["id"],
                                      "teacher_next_draft_id": browser_draft["id"], "student_hint_task_id": next_task["id"],
                                      "student_hint_question_version_id": question["id"] + ":1"}
            fixture.write_json(self.output / "service-info.json", self.info)
            calls = self.events()
            self.check(bool(calls) and all(row["json_mode"] is True and row["stream"] is False
                                          and row["origin"] == "local_synthetic_selection" for row in calls), "gateway boundary contract violated")
            self.check({row["purpose"] for row in calls} == {"summary", "hint"}, "unexpected gateway purpose")
            for event in calls:
                ledger = self.sql("jobs", "SELECT status,cost_units,cost_pending,price_version FROM upstream_calls WHERE call_id=?", (event["call_id"],))
                self.check(len(ledger) == 1 and not ledger[0]["cost_pending"] and ledger[0]["price_version"] == fixture.PRICE_VERSION,
                           "gateway call lacks settled durable ledger")
                if event["status"] in {"returned", "fixture_unknown_outcome"}:
                    self.check(ledger[0]["cost_units"] == fixture.COST and ledger[0]["status"] == "succeeded", "synthetic call charge/result differs")
            active = self.sql("jobs", "SELECT COUNT(*) AS count FROM call_budget_reservations WHERE active=1")[0]["count"]
            self.check(active == 0, "completed fixture has active budget reservations")
            def pending_notifications():
                rows = self.sql("reports", "SELECT notification_events_json FROM teaching_insight_outbox")
                return sum(not event["acknowledged"] for row in rows for event in json.loads(row["notification_events_json"]))
            self.poll(pending_notifications, lambda count: count == 0, "summary domain notification outbox drain")
            health = self.api("anonymous", "GET", "/health")
            self.check(health["workers"]["running"] == 1 and not health["workers"]["needs_attention"], "worker unhealthy after HTTP preparation")
            self.evidence["gateway"] = {"total_calls": len(calls), "hint_calls": sum(row["purpose"] == "hint" for row in calls),
                                        "summary_calls": sum(row["purpose"] == "summary" for row in calls), "real_provider_calls": 0,
                                        "all_nonstreaming_json": True, "settled_ledger": True, "active_reservations": 0}

    def run(self):
        self.run_diagnostics()
        self.run_hints()
        self.run_independence()
        self.run_summary()
        self.run_recovery()
        self.run_next_round()
        self.run_browser_preparation()

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
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=8)
                        self.evidence["failures"].append({"stage": "shutdown", "type": "ForcedShutdown", "message": "fixture did not stop gracefully"})
                self.evidence["shutdown"] = {"exit_code": process.returncode, "reaped": process.poll() is not None}
                if process.returncode != 0:
                    self.evidence["failures"].append({"stage": "shutdown", "type": "ServerExit", "message": "isolated child exited unsuccessfully"})
            isolation_path = self.output / "isolation.json"
            if isolation_path.exists():
                isolation = json.loads(isolation_path.read_text(encoding="utf-8"))
                self.evidence["isolation"] = isolation
                self.check(isolation["env_file_skipped"] and not isolation["model_configured"] and not isolation["maxkb_configured"]
                           and isolation["real_provider_calls"] == 0, "child upstream isolation failed")
                self.check(all(isolation[key] == 0 for key in ("blocked_external_network", "blocked_database_access", "blocked_env_reads")),
                           "child attempted forbidden access")
                self.check(isolation["worker_after_stop"]["running"] == 0, "worker remained alive after stop")
            elif process:
                self.evidence["failures"].append({"stage": "shutdown", "type": "MissingIsolation", "message": "child isolation evidence missing"})
            self.evidence["launcher_boundary"] = getattr(self, "parent_boundary", {})
        finally:
            private_path = self.output / "credentials.local.json"
            if private_path.exists():
                self.collect_private(json.loads(private_path.read_text(encoding="utf-8")))
            if self.server_log is not None:
                self.server_log.seek(0)
                log = self.server_log.read(20000).decode("utf-8", errors="replace")
                # Only the explicit sanitized child failure line is retained.
                diagnostic = [line for line in log.splitlines() if line.startswith("FIXTURE CHILD FAILED")]
                if diagnostic:
                    self.evidence["child_diagnostic"] = self.clean(diagnostic[-1])
                self.server_log.close()
                self.server_log = None
            private_path.unlink(missing_ok=True)
            private_path.with_name(private_path.name + ".tmp").unlink(missing_ok=True)
            self.evidence["credentials_removed"] = not private_path.exists()
            self.accounts.clear()
            if self.info:
                self.info.update(status="stopped", stopped_at=fixture.utc_now(), credentials_removed=not private_path.exists())
                fixture.write_json(self.output / "service-info.json", self.info)
            self.process = None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help="retain service only after all HTTP preparation passes")
    parser.add_argument("--output-dir", type=Path, help="new ignored output below .scratch")
    parser.add_argument("--timeout", type=float, default=45, help="bounded polling deadline, 10..120 seconds")
    parser.add_argument("--fixture-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.flags.isolated or not sys.dont_write_bytecode or not sys.flags.utf8_mode:
        parser.error("invoke using python -I -B -X utf8")
    if not math.isfinite(args.timeout) or not 10 <= args.timeout <= 120:
        parser.error("timeout must be 10..120 seconds")
    logging.disable(logging.CRITICAL)
    if args.fixture_child:
        if args.output_dir is None or args.data_dir is None or args.port is None:
            parser.error("internal fixture arguments missing")
        output, data = args.output_dir.resolve(), args.data_dir.resolve()
        if not output.is_relative_to(SCRATCH.resolve()) or not output.is_dir() or not data.is_dir() or any(data.iterdir()):
            parser.error("child needs dedicated existing output and empty temporary data directory")
        try:
            return fixture.run_child(output, data, args.port)
        except Exception as error:
            # No exception values: a startup error can contain private parameters.
            frames = []
            trace = error.__traceback__
            while trace:
                frames.append(Path(trace.tb_frame.f_code.co_filename).name + ":" + str(trace.tb_lineno))
                trace = trace.tb_next
            print("FIXTURE CHILD FAILED (" + type(error).__name__ + ") at " + " > ".join(frames[-5:]), flush=True)
            return 1
    try:
        output = output_directory(args.output_dir)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Fixture output: " + str(output), flush=True)
    verification = Verification(output, args)
    interrupted = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signal, _frame: interrupted.set())
    temporary_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="teaching-loop-runtime-") as temporary:
            temporary_path = Path(temporary)
            try:
                with verification.stage("isolated_server_and_auth"):
                    verification.start_service(temporary_path)
                verification.run()
                passed = {row["stage"] for row in verification.evidence["stages"] if row["status"] == "passed"}
                verification.check(set(REQUIRED_STAGES) <= passed, "HTTP preparation incomplete")
                verification.evidence["preparation_passed"] = True
                if args.serve:
                    verification.info.update(status="ready", ready_at=fixture.utc_now(), preparation_passed=True)
                    fixture.write_json(output / "service-info.json", verification.info)
                    verification.evidence.update(status="serving", mode="serve")
                    verification.save()
                    print("READY origin=" + verification.origin + "; service-info=" + str(output / "service-info.json"), flush=True)
                    while not interrupted.wait(0.25):
                        if (output / "stop.requested").exists():
                            break
                        verification.check(verification.process.poll() is None, "fixture exited without a stop request")
            except KeyboardInterrupt:
                interrupted.set()
            except Exception as error:
                if not verification.evidence["failures"]:
                    verification.evidence["failures"].append({"stage": verification.stage_name, "type": type(error).__name__, "message": verification.clean(error)})
            finally:
                verification.stop_service()
    except Exception as error:
        verification.evidence["failures"].append({"stage": "cleanup", "type": type(error).__name__, "message": verification.clean(error)})
    verification.evidence["temporary_data_removed"] = temporary_path is not None and not temporary_path.exists()
    verification.evidence["databases_removed"] = {name: temporary_path is not None and not (temporary_path / (name + ".sqlite3")).exists()
                                                   for name in fixture.DATABASES}
    if not verification.evidence["temporary_data_removed"] or not verification.evidence.get("credentials_removed"):
        verification.evidence["failures"].append({"stage": "cleanup", "type": "CleanupIncomplete", "message": "temporary data or credentials remain"})
    passed = {row["stage"] for row in verification.evidence["stages"] if row["status"] == "passed"}
    missing = sorted(set(REQUIRED_STAGES) - passed)
    verification.evidence["missing_stages"] = missing
    if missing and not verification.evidence["failures"]:
        verification.evidence["failures"].append({"stage": "completion", "type": "IncompletePreparation", "message": "not all required stages passed"})
    status = "failed" if verification.evidence["failures"] else "stopped" if args.serve else "passed"
    verification.evidence.update(status=status, finished_at=fixture.utc_now(), elapsed_seconds=round(time.monotonic() - verification.started, 3))
    verification.save()
    print(f"RESULT {status}; stages={len(passed)}/{len(REQUIRED_STAGES)}; http_requests={len(verification.evidence['http'])}; evidence={output / 'evidence.json'}", flush=True)
    return 0 if status in {"passed", "stopped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
