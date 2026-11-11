"""Idempotent delivery from domain outboxes to the durable worker queue."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from .job_queue import IdempotencyConflict


def ensure_job(runtime, job_id: str, kind: str, payload: Mapping, *, steps=(), paid=False):
    try:
        existing = runtime.queue.get_job(job_id)
    except LookupError:
        try:
            return runtime.queue.enqueue(
                kind, payload, job_id=job_id, steps=steps, paid=paid,
                purpose=payload.get("purpose", ""), student_id=payload.get("student_id") or payload.get("user_id"),
                course_id=payload.get("course_id"), class_id=payload.get("class_id"), teaching_scope=payload.get("class_id"),
                project_id="platform", budget_day=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            )
        except sqlite3.IntegrityError:
            existing = runtime.queue.get_job(job_id)
    if existing.kind != kind or existing.payload != dict(payload):
        raise IdempotencyConflict("domain event was delivered with different queue input")
    return existing


class Dispatcher:
    def __init__(self, runtime):
        self.runtime = runtime

    def deliver(self) -> int:
        self.runtime.learning.tick_plans()
        self.runtime.learning.refresh_reports(self.runtime.reports)
        count = self._grading() + self._companion() + self._content() + self._appeals()
        for event in self.runtime.learning.pending_notifications(limit=100):
            self.runtime.companion.create_notification(
                event["user_id"], event["event_type"], event["resource_type"], event["resource_id"],
                business_key=event["id"], payload=json.loads(event["payload_json"]),
            )
            self.runtime.learning.acknowledge_notification(event["id"])
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

    def _companion(self) -> int:
        count = 0
        for event in self.runtime.companion.pending_outbox(limit=100):
            payload = dict(event.get("payload") or {})
            payload.update(purpose="student", resource_type="response", resource_id=event["response_id"])
            ensure_job(self.runtime, event["job_id"], "interaction", payload, steps=("answer",), paid=True)
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
