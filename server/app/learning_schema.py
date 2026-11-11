"""Transactional schema v2 additions for the learning repository."""

import sqlite3

V2_TABLES = frozenset({
    "learning_revision", "goal_commands", "round_policies", "task_policies",
    "teacher_judgments", "diagnostic_sessions", "diagnostic_items",
    "question_family_index", "question_metadata", "learning_outbox", "appeals", "goal_plans",
})


def migrate_v2(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE learning_revision(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=0))")
    db.execute("INSERT INTO learning_revision VALUES(1,0)")
    db.execute("""CREATE TABLE goal_commands(
        actor_id TEXT NOT NULL, scope_id TEXT NOT NULL, request_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(actor_id,scope_id,request_id))""")
    db.execute("""CREATE TABLE round_policies(
        round_id TEXT PRIMARY KEY REFERENCES learning_rounds(id), policy_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE task_policies(
        task_id TEXT PRIMARY KEY REFERENCES learning_tasks(id), policy_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE question_metadata(
        question_id TEXT NOT NULL, version INTEGER NOT NULL, metadata_json TEXT NOT NULL,
        PRIMARY KEY(question_id,version),
        FOREIGN KEY(question_id,version) REFERENCES question_versions(question_id,version))""")
    db.execute("""CREATE TABLE question_family_index(
        course_id TEXT NOT NULL, content_sha TEXT NOT NULL, family_sha TEXT NOT NULL,
        question_id TEXT NOT NULL, version INTEGER NOT NULL,
        PRIMARY KEY(course_id,question_id,version),
        FOREIGN KEY(question_id,version) REFERENCES question_versions(question_id,version))""")
    db.execute("CREATE INDEX question_content_idx ON question_family_index(course_id,content_sha)")
    db.execute("CREATE INDEX question_family_idx ON question_family_index(course_id,family_sha)")
    db.execute("""CREATE TABLE teacher_judgments(
        id TEXT PRIMARY KEY, student_id TEXT NOT NULL, goal_id TEXT NOT NULL REFERENCES learning_goals(id),
        knowledge_point_id TEXT NOT NULL, actor_id TEXT NOT NULL, judgment_json TEXT NOT NULL, created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE diagnostic_sessions(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES learning_tasks(id),
        student_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','completed','skipped')),
        version INTEGER NOT NULL, answers_json TEXT NOT NULL DEFAULT '{}', feedback_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, submitted_at REAL)""")
    db.execute("""CREATE TABLE diagnostic_items(
        diagnostic_id TEXT NOT NULL REFERENCES diagnostic_sessions(id), question_id TEXT NOT NULL,
        question_version INTEGER NOT NULL, frozen_json TEXT NOT NULL, position INTEGER NOT NULL,
        PRIMARY KEY(diagnostic_id,question_id), UNIQUE(diagnostic_id,position))""")
    db.execute("""CREATE TABLE learning_outbox(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event_type TEXT NOT NULL, resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL, delivered_at REAL)""")
    db.execute("CREATE INDEX learning_outbox_pending_idx ON learning_outbox(delivered_at,created_at)")
    db.execute("""CREATE TABLE appeals(
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id), question_id TEXT NOT NULL,
        student_id TEXT NOT NULL, grading_revision INTEGER NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','awaiting_teacher','resolved')),
        result_json TEXT, created_at REAL NOT NULL, UNIQUE(attempt_id,question_id,grading_revision))""")
    db.execute("""CREATE TABLE goal_plans(
        goal_id TEXT PRIMARY KEY REFERENCES learning_goals(id), policy_json TEXT NOT NULL,
        remaining_rounds INTEGER NOT NULL CHECK(remaining_rounds>=0), next_open_at REAL,
        ends_at REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','blocked','closed')),
        reason TEXT, version INTEGER NOT NULL DEFAULT 1)""")
    db.execute("CREATE TRIGGER revision_goals_ai AFTER INSERT ON learning_goals BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_goals_au AFTER UPDATE ON learning_goals BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_tasks_ai AFTER INSERT ON learning_tasks BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_tasks_au AFTER UPDATE ON learning_tasks BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_attempts_ai AFTER INSERT ON attempts BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_attempts_au AFTER UPDATE ON attempts BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_items_au AFTER UPDATE ON attempt_items BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_events_ai AFTER INSERT ON task_events BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_questions_ai AFTER INSERT ON question_versions BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_questions_au AFTER UPDATE ON question_versions BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_metadata_ai AFTER INSERT ON question_metadata BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_diagnostic_au AFTER UPDATE ON diagnostic_sessions BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_policies_au AFTER UPDATE ON task_policies BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("CREATE TRIGGER revision_judgments_ai AFTER INSERT ON teacher_judgments BEGIN UPDATE learning_revision SET revision=revision+1 WHERE id=1; END")
    db.execute("""CREATE TRIGGER immutable_question_content BEFORE UPDATE OF question_id,version,owner_id,course_id,content_json ON question_versions
        BEGIN SELECT RAISE(ABORT,'question versions are immutable'); END""")
    db.execute("CREATE TRIGGER immutable_question_delete BEFORE DELETE ON question_versions BEGIN SELECT RAISE(ABORT,'question versions are immutable'); END")
    db.execute("CREATE TRIGGER immutable_answers BEFORE UPDATE OF answers_json,task_id,attempt_no,kind,submitted_at ON attempts BEGIN SELECT RAISE(ABORT,'submitted answers are immutable'); END")
    db.execute("CREATE TRIGGER immutable_corrections_au BEFORE UPDATE ON grading_corrections BEGIN SELECT RAISE(ABORT,'corrections are append-only'); END")
    db.execute("CREATE TRIGGER immutable_corrections_ad BEFORE DELETE ON grading_corrections BEGIN SELECT RAISE(ABORT,'corrections are append-only'); END")
    db.execute("CREATE TRIGGER immutable_goal_revisions_au BEFORE UPDATE ON goal_revisions BEGIN SELECT RAISE(ABORT,'goal revisions are immutable'); END")
    db.execute("CREATE TRIGGER immutable_report_revisions_au BEFORE UPDATE ON report_revisions BEGIN SELECT RAISE(ABORT,'report revisions are immutable'); END")
