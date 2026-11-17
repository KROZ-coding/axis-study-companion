"""Idempotent delivery from domain outboxes to the durable worker queue."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from .job_queue import IdempotencyConflict


def ensure_job(runtime, job_id: str, kind: str, payload: Mapping, *, steps=(), paid=False, **scope):
    try:
        existing = runtime.queue.get_job(job_id)
    except LookupError:
        try:
            return runtime.queue.enqueue(
                kind, payload, job_id=job_id, steps=steps, paid=paid,
                purpose=scope.get("purpose", payload.get("purpose", "")),
                student_id=scope.get("student_id", payload.get("student_id") or payload.get("user_id")),
                course_id=scope.get("course_id", payload.get("course_id")),
                class_id=scope.get("class_id", payload.get("class_id")),
                teaching_scope=scope.get("teaching_scope", payload.get("class_id")),
                project_id=scope.get("project_id", "platform"),
                budget_day=scope.get("budget_day", datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()),
            )
        except sqlite3.IntegrityError:
            existing = runtime.queue.get_job(job_id)
    if existing.kind != kind:
        raise IdempotencyConflict("domain event was delivered with different queue kind")
    if existing.payload != dict(payload):
        return runtime.queue.bind_job_scope(job_id, payload)
    return existing


class Dispatcher:
    def __init__(self, runtime):
        self.runtime = runtime

    def deliver(self) -> int:
        from .services.teacher_planning import TeacherPlanning
        planning = self.runtime._get("teacher_planning", lambda: TeacherPlanning(
            self.runtime.learning, self.runtime.platform, self.runtime.content))
        self.runtime.learning.tick_plans(teacher_planning=planning)
        self.runtime.learning.refresh_reports(self.runtime.reports)
        count = self._grading() + self._companion() + self._content() + self._appeals() + self._insights()
        for event in self.runtime.learning.pending_notifications(limit=100):
            self.runtime.companion.create_notification(
                event["user_id"], event["event_type"], event["resource_type"], event["resource_id"],
                business_key=event["id"], payload=json.loads(event["payload_json"]),
            )
            self.runtime.learning.acknowledge_notification(event["id"])
            count += 1
        for event in self.runtime.insights.pending_notifications(limit=100):
            self.runtime.companion.create_notification(
                event["user_id"], event["event_type"], event["resource_type"], event["resource_id"],
                business_key=event["business_key"], payload=event["payload"],
            )
            self.runtime.insights.acknowledge_notification(event["id"])
            count += 1
        return count

    def _appeals(self):
        count = 0
        for appeal in self.runtime.learning.pending_appeals(limit=100):
            payload = {"purpose": "grading", "resource_type": "appeal", "resource_id": appeal["id"],
                       "input_revision": appeal["grading_revision"], "student_id": appeal["student_id"],
                       "owner_id": appeal["owner_id"], "class_id": appeal["class_id"], "course_id": appeal["course_id"]}
            ensure_job(self.runtime, f"appeal:{appeal['id']}", "grading", payload, steps=("review",))
            count += 1
        return count

    def _grading(self) -> int:
        count = 0
        learning = self.runtime.learning
        for event in learning.list_grading_outbox(limit=100):
            data = learning.get_grading_input(event["attempt_id"])
            payload = {
                "purpose": "grading", "resource_type": "attempt", "resource_id": event["attempt_id"],
                "task_id": data["task_id"], "input_revision": event["attempt_revision"],
            }
            scope = data.get("scope", data)
            for field in ("student_id", "owner_id", "course_id", "class_id", "goal_id"):
                if scope.get(field) is not None:
                    payload[field] = scope[field]
            steps = [f"item:{question['id']}" for question in data["questions"]]
            ensure_job(self.runtime, event["job_id"], "grading", payload, steps=steps)
            learning.mark_grading_outbox(event["job_id"], queue_job_id=event["job_id"])
            count += 1
        return count

    def _insights(self) -> int:
        count = 0
        for insight in self.runtime.insights.pending_jobs(limit=100):
            payload = {
                "purpose": "summary", "step": "summary", "resource_type": "teaching_insight",
                "resource_id": insight["id"], "owner_id": insight["owner_id"],
                "class_id": insight["class_id"], "course_id": insight["course_id"],
                "report_id": insight["report_id"], "source_report_revision": insight["source_report_revision"],
                "input_hash": insight["input_hash"], "input_revision": insight["source_revision"],
            }
            ensure_job(self.runtime, insight["job_id"], "batch", payload, steps=("summary",), paid=True,
                       budget_day=datetime.fromtimestamp(insight["created_at"], ZoneInfo("Asia/Shanghai")).date().isoformat())
            self.runtime.insights.mark_dispatched(insight["id"], insight["job_id"])
            count += 1
        return count

    def _companion(self) -> int:
        count = 0
        from .services.companion_access import freeze_response_job_scope
        for event in self.runtime.companion.pending_outbox(limit=100):
            payload = dict(event.get("payload") or {})
            response = self.runtime.companion.get_response(event["response_id"])
            purpose = "hint" if response.get("intent") == "hint" else "student"
            if response["job_id"] != event["job_id"] or response["user_id"] != payload.get("user_id"):
                raise IdempotencyConflict("companion outbox binding changed")
            payload.update(purpose=purpose, resource_type="response", resource_id=event["response_id"])
            try:
                scope = freeze_response_job_scope(self.runtime, event["response_id"], payload.get("user_id", ""))
            except (LookupError, PermissionError):
                # Deliver revoked requests so the worker can close their domain
                # response, but it must validate/freeze before any paid call.
                scope = None
            if scope is not None:
                for key in ("student_id", "course_id", "class_id", "task_id", "project_id"):
                    payload.pop(key, None)
                payload.update(scope, budget_scope_frozen=True)
            ensure_job(self.runtime, event["job_id"], "interaction", payload, steps=("answer",), paid=True,
                       purpose=purpose, student_id=payload.get("student_id") or payload.get("user_id"),
                       course_id=payload.get("course_id"), class_id=payload.get("class_id"),
                       teaching_scope=payload.get("class_id"), project_id="platform",
                       budget_day=datetime.fromtimestamp(event["created_at"], ZoneInfo("Asia/Shanghai")).date().isoformat())
            self.runtime.companion.mark_outbox(event["outbox_id"], job_id=event["job_id"])
            count += 1
        return count

    def _content(self) -> int:
        count = 0
        for event in self.runtime.content.list_pending_jobs(limit=100):
            purpose = "parse" if event["kind"] == "material_parse" else "generate"
            payload = {
                "purpose": purpose, "resource_type": "content_job", "resource_id": event["id"],
                "course_id": event["course_id"], "owner_id": event["owner_id"],
            }
            if event.get("class_id"):
                payload["class_id"] = event["class_id"]
            steps = ("parse",) if purpose == "parse" else ("generate", "review", "compare", "recheck", "compare_recheck")
            ensure_job(self.runtime, f"content:{event['id']}", "batch", payload, steps=steps, paid=purpose != "parse")
            count += 1
        return count
