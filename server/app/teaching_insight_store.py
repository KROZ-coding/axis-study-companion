"""Frozen teacher insight inputs and durable dispatch intent in reports.sqlite3."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from .report_store import ReportStore, canonical_json, content_hash


class InsightConflict(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def migrate_insights(db):
    """ReportStore v2 migration; caller owns the migration transaction."""
    statements = (
        """CREATE TABLE teaching_insights (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL, course_id TEXT NOT NULL, class_id TEXT NOT NULL,
            origin_goal_id TEXT NOT NULL, goal_version INTEGER NOT NULL CHECK(goal_version>0),
            report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE RESTRICT,
            source_report_revision TEXT NOT NULL REFERENCES report_revisions(id) ON DELETE RESTRICT,
            source_revision INTEGER NOT NULL CHECK(source_revision>=0), snapshot_sha256 TEXT NOT NULL,
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            input_json TEXT NOT NULL, input_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
            result_json TEXT, error_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(owner_id,request_id)
        )""",
        "CREATE INDEX teaching_insights_scope ON teaching_insights(class_id,origin_goal_id,created_at DESC)",
        """CREATE TABLE teaching_insight_outbox (
            insight_id TEXT PRIMARY KEY REFERENCES teaching_insights(id) ON DELETE RESTRICT,
            job_id TEXT NOT NULL UNIQUE, dispatched_at REAL, created_at REAL NOT NULL,
            notification_events_json TEXT NOT NULL DEFAULT '[]'
        )""",
        """CREATE TRIGGER teaching_insights_frozen BEFORE UPDATE OF
            id,job_id,owner_id,course_id,class_id,origin_goal_id,goal_version,report_id,
            source_report_revision,source_revision,snapshot_sha256,request_id,request_hash,input_json,input_hash,created_at
            ON teaching_insights BEGIN SELECT RAISE(ABORT,'teaching insight input is immutable'); END""",
    )
    for statement in statements:
        db.execute(statement)


class TeachingInsightStore:
    def __init__(self, reports):
        self.reports = reports if isinstance(reports, ReportStore) else ReportStore(reports)
        self.db_path = self.reports.db_path

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        for name in ("input", "result", "error"):
            raw = result.pop(name + "_json")
            result[name] = json.loads(raw) if raw is not None else None
        return result

    @staticmethod
    def _generation(value):
        if type(value) is not int or value < 1:
            raise ValueError("generation must be a positive integer")
        return value

    def get(self, insight_id):
        with self.reports.connect() as db:
            return self._row(db.execute("SELECT * FROM teaching_insights WHERE id=?", (insight_id,)).fetchone())

    def find_request(self, owner_id, request_id, fingerprint):
        with self.reports.connect() as db:
            row = db.execute("SELECT * FROM teaching_insights WHERE owner_id=? AND request_id=?", (owner_id, request_id)).fetchone()
            if row is not None and row["request_hash"] != content_hash(fingerprint):
                raise InsightConflict("request_conflict", "请求编号已用于不同的冻结报告")
            return self._row(row)

    def create(self, *, owner_id, request_id, revision, frozen_input):
        fingerprint = {"report_id": revision["report_id"], "source_report_revision": revision["id"]}
        digest, encoded = content_hash(fingerprint), canonical_json(frozen_input)
        scope = revision["snapshot"]["scope"]
        now, insight_id = time.time(), "insight_" + uuid4().hex
        job_id = "teaching-insight:" + insight_id
        with self.reports._write() as db:
            previous = db.execute("SELECT * FROM teaching_insights WHERE owner_id=? AND request_id=?", (owner_id, request_id)).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise InsightConflict("request_conflict", "请求编号已用于不同的冻结报告")
                return self._row(previous)
            db.execute("""INSERT INTO teaching_insights
                (id,job_id,owner_id,course_id,class_id,origin_goal_id,goal_version,report_id,source_report_revision,
                 source_revision,snapshot_sha256,request_id,request_hash,input_json,input_hash,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (insight_id, job_id, owner_id, scope["course_id"], scope["class_id"], scope["goal_id"],
                 frozen_input["goal_version"], revision["report_id"], revision["id"], revision["source_revision"],
                 revision["snapshot_sha256"], request_id, digest, encoded, content_hash(frozen_input), now, now))
            db.execute("INSERT INTO teaching_insight_outbox(insight_id,job_id,dispatched_at,created_at) VALUES(?,?,NULL,?)", (insight_id, job_id, now))
            return self._row(db.execute("SELECT * FROM teaching_insights WHERE id=?", (insight_id,)).fetchone())

    def list(self, *, class_id, goal_id=None, limit=50):
        with self.reports.connect() as db:
            return [self._row(row) for row in db.execute("""SELECT * FROM teaching_insights
                WHERE class_id=? AND (? IS NULL OR origin_goal_id=?) ORDER BY created_at DESC,id LIMIT ?""",
                (class_id, goal_id, goal_id, limit))]

    def pending_jobs(self, limit=100):
        with self.reports.connect() as db:
            return [self._row(row) for row in db.execute("""SELECT i.* FROM teaching_insights i
                JOIN teaching_insight_outbox o ON o.insight_id=i.id
                WHERE i.status='pending' AND o.dispatched_at IS NULL ORDER BY o.created_at,i.id LIMIT ?""", (limit,))]

    def mark_dispatched(self, insight_id, job_id=None):
        with self.reports._write() as db:
            return db.execute("""UPDATE teaching_insight_outbox SET dispatched_at=?
                WHERE insight_id=? AND (? IS NULL OR job_id=?) AND EXISTS
                (SELECT 1 FROM teaching_insights i WHERE i.id=insight_id AND i.status IN ('pending','running'))""",
                (time.time(), insight_id, job_id, job_id)).rowcount > 0

    def bind_generation(self, insight_id, generation):
        generation = self._generation(generation)
        with self.reports._write() as db:
            return db.execute("""UPDATE teaching_insights SET generation=?,status='running',error_json=NULL,updated_at=?
                WHERE id=? AND status IN ('pending','running') AND generation<=?""",
                (generation, time.time(), insight_id, generation)).rowcount > 0

    def complete(self, insight_id, result, *, generation):
        generation = self._generation(generation)
        encoded = canonical_json(result)
        with self.reports._write() as db:
            updated = db.execute("""UPDATE teaching_insights SET status='completed',result_json=?,error_json=NULL,updated_at=?
                WHERE id=? AND generation=? AND status='running'""",
                (encoded, time.time(), insight_id, generation)).rowcount > 0
            if updated:
                self._notification(db, insight_id, "ready")
            return updated

    def fail(self, insight_id, *, generation, code="generation_failed", message="总结生成失败，请重试"):
        generation = self._generation(generation)
        with self.reports._write() as db:
            updated = db.execute("""UPDATE teaching_insights SET status='failed',result_json=NULL,error_json=?,updated_at=?
                WHERE id=? AND generation=? AND status IN ('pending','running')""",
                (canonical_json({"code": code, "message": message}), time.time(), insight_id, generation)).rowcount > 0
            if updated:
                self._notification(db, insight_id, "failed")
            return updated

    def cancel(self, insight_id, *, generation=None):
        if generation is not None:
            self._generation(generation)
        with self.reports._write() as db:
            updated = db.execute("""UPDATE teaching_insights SET status='cancelled',result_json=NULL,error_json=NULL,updated_at=?
                WHERE id=? AND status IN ('pending','running','failed') AND (? IS NULL OR generation=?)""",
                (time.time(), insight_id, generation, generation)).rowcount > 0
            if updated:
                self._notification(db, insight_id, "cancelled")
            return updated

    @staticmethod
    def _notification(db, insight_id, state):
        row = db.execute("SELECT owner_id,job_id,generation FROM teaching_insights WHERE id=?", (insight_id,)).fetchone()
        suffix = state if state == "ready" else f"{state}:{row['generation']}"
        key = f"teaching-insight:{insight_id}:{suffix}"
        event = {"id": key, "business_key": key, "user_id": row["owner_id"], "event_type": "teaching_insight_" + state,
                 "resource_type": "teaching_insight", "resource_id": insight_id,
                 "payload": {"insight_id": insight_id, "job_id": row["job_id"], "generation": row["generation"]},
                 "acknowledged": False}
        events = json.loads(db.execute("SELECT notification_events_json FROM teaching_insight_outbox WHERE insight_id=?", (insight_id,)).fetchone()[0])
        if not any(previous["id"] == key for previous in events):
            events.append(event)
            db.execute("UPDATE teaching_insight_outbox SET notification_events_json=? WHERE insight_id=?", (canonical_json(events), insight_id))

    def pending_notifications(self, limit=100):
        result = []
        with self.reports.connect() as db:
            for row in db.execute("SELECT notification_events_json FROM teaching_insight_outbox ORDER BY created_at,insight_id"):
                for event in json.loads(row[0]):
                    if not event["acknowledged"]:
                        result.append({key: value for key, value in event.items() if key != "acknowledged"})
                        if len(result) >= limit:
                            return result
        return result

    def acknowledge_notification(self, notification_id):
        with self.reports._write() as db:
            for row in db.execute("SELECT insight_id,notification_events_json FROM teaching_insight_outbox"):
                events = json.loads(row["notification_events_json"])
                for event in events:
                    if event["id"] == notification_id:
                        if event["acknowledged"]:
                            return True
                        event["acknowledged"] = True
                        db.execute("UPDATE teaching_insight_outbox SET notification_events_json=? WHERE insight_id=?",
                                   (canonical_json(events), row["insight_id"]))
                        return True
        return False

    def resume(self, insight_id, *, generation):
        generation = self._generation(generation)
        with self.reports._write() as db:
            updated = db.execute("""UPDATE teaching_insights SET status='pending',generation=?,result_json=NULL,error_json=NULL,updated_at=?
                WHERE id=? AND status='failed' AND generation<?""",
                (generation, time.time(), insight_id, generation)).rowcount > 0
            if updated:
                db.execute("UPDATE teaching_insight_outbox SET dispatched_at=NULL WHERE insight_id=?", (insight_id,))
            return updated
