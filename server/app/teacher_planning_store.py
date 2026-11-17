"""Planning records live in LearningStore's transaction, not a second question authority."""

import json

from .content_store import ContentConflict


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS teacher_plan_drafts (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, course_id TEXT NOT NULL,
        class_id TEXT NOT NULL, config_fingerprint TEXT NOT NULL, config_json TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(owner_id,config_fingerprint)
    )""",
    """CREATE INDEX IF NOT EXISTS teacher_plan_drafts_class
        ON teacher_plan_drafts(owner_id,class_id,updated_at)""",
    """CREATE TABLE IF NOT EXISTS teacher_plan_previews (
        token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        published_result_json TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS teacher_plan_previews_expiry
        ON teacher_plan_previews(expires_at)""",
    """CREATE TABLE IF NOT EXISTS teacher_next_round_drafts (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, course_id TEXT NOT NULL, class_id TEXT NOT NULL,
        insight_id TEXT NOT NULL, origin_goal_id TEXT NOT NULL REFERENCES learning_goals(id),
        source_report_revision TEXT NOT NULL, goal_version INTEGER NOT NULL,
        next_round INTEGER NOT NULL CHECK(next_round>1), revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK(status IN ('draft','published')),
        config_json TEXT NOT NULL, overrides_json TEXT NOT NULL, basis_json TEXT NOT NULL,
        published_result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS teacher_next_round_scope
        ON teacher_next_round_drafts(class_id,origin_goal_id,updated_at)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS teacher_next_round_published
        ON teacher_next_round_drafts(origin_goal_id,next_round) WHERE status='published'""",
    """CREATE TRIGGER IF NOT EXISTS teacher_next_round_frozen BEFORE UPDATE OF
        id,owner_id,course_id,class_id,insight_id,origin_goal_id,source_report_revision,
        goal_version,next_round,basis_json,created_at ON teacher_next_round_drafts
        BEGIN SELECT RAISE(ABORT,'next round origin is immutable'); END""",
)


def ensure_schema(db):
    # execute(), never executescript(): the caller owns BEGIN/COMMIT.
    for statement in SCHEMA:
        db.execute(statement)


def draft_view(row):
    return {
        "id": row["id"], "config": json.loads(row["config_json"]),
        "config_fingerprint": row["config_fingerprint"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


class GenerationIntentContent:
    """Give the existing bank a durable job identity for replay across databases.

    A prepare command first commits its intended job IDs in learning.sqlite3.
    Replaying after a crash fills missing content jobs using those same IDs. The
    original QuestionBank still validates selected sources and creates the job;
    this adapter only supplies an idempotent ContentStore.create_job boundary.
    """

    def __init__(self, content, job_id):
        self.content, self.job_id = content, job_id

    def __getattr__(self, name):
        return getattr(self.content, name)

    def create_job(self, kind, *, course_id, owner_id, payload, class_id=None, job_id=None):
        import time

        if kind != "question_generate" or job_id not in (None, self.job_id):
            raise ValueError("planning only prepares question generation jobs")
        encoded = self.content._json(dict(payload))
        with self.content.transaction() as db:
            row = db.execute("SELECT * FROM content_jobs WHERE id=?", (self.job_id,)).fetchone()
            if row:
                if (row["kind"] != kind or row["course_id"] != course_id or row["class_id"] != class_id
                        or row["owner_id"] != owner_id or json.loads(row["input_json"]) != dict(payload)):
                    raise ContentConflict("generation intent changed")
            else:
                now = time.time()
                db.execute("""INSERT INTO content_jobs
                    (id,course_id,class_id,owner_id,kind,stage,status,input_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,'generate','pending',?,?,?)""",
                    (self.job_id, course_id, class_id, owner_id, kind, encoded, now, now))
        return self.content.get_job(self.job_id)
