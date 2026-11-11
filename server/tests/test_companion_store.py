"""File-backed companion storage, worker fencing, projection and HTTP tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.companion_store import (
    APPLICATION_ID, SAFE_GUIDANCE, CompanionStore, Forbidden, IdempotencyConflict,
    InvalidTransition, NotFound, StaleGeneration,
)
from app.platform_store import PlatformStore
from app.routers.v1_companion import router
from app.models import User
from app.v1_deps import get_platform_store, get_platform_user


class MutablePolicy:
    def __init__(self, allowed=False, *, diagnostic=False, attempted=False):
        self.allowed = allowed
        self.diagnostic = diagnostic
        self.attempted = attempted

    def __call__(self, user_id, *, task_id=None, question_version_id=None):
        return {"allow_assistant_content": self.allowed, "allow_generation": self.allowed,
                "diagnostic": self.diagnostic, "attempted": self.attempted,
                "task_id": task_id, "question_version_id": question_version_id}


class CompanionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="companion-store-test-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "companion.sqlite3"
        self.policy = MutablePolicy()
        self.store = CompanionStore(self.database, answer_policy=self.policy)
        self.conversation = self.store.create_conversation("student-1", "Study", course_id="course-1", request_id="conversation-1")

    def post(self, text="question", *, request_id="message-1", task_id=None, question_version_id=None):
        return self.store.post_message(self.conversation["id"], "student-1", text, request_id=request_id,
                                       task_id=task_id, question_version_id=question_version_id)

    def test_database_identity_wal_foreign_keys_constraints_and_foreign_file_rejection(self):
        self.assertEqual(self.store.schema_version, 1)
        self.assertEqual(self.store.journal_mode, "wal")
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(CompanionStore._EXPECTED_TABLES.issubset(tables))
        self.assertNotIn("users", tables)
        self.assertNotIn("jobs", tables)

        bad = Path(self.directory.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(bad)) as db:
            db.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(RuntimeError, "unversioned"):
            CompanionStore(bad)

        newer = Path(self.directory.name) / "newer.sqlite3"
        with closing(sqlite3.connect(newer)) as db:
            db.execute("PRAGMA application_id=0x41584350")
            db.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(RuntimeError, "identity or schema"):
            CompanionStore(newer)

        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction() as db:
                db.execute("INSERT INTO conversations(id,user_id,title,request_id,request_hash,created_at,updated_at) VALUES('bad','ghost','','k',?,0,0)",
                           ("a" * 64,))

    def test_message_response_and_outbox_are_atomic_and_idempotent(self):
        posted = self.post(task_id="task-1", question_version_id="q1:1")
        self.assertEqual(set(posted), {"message_id", "conversation_id", "response_id", "job_id", "status", "message"})
        replay = self.post(task_id="task-1", question_version_id="q1:1")
        for key in ("message_id", "response_id", "job_id"):
            self.assertEqual(replay[key], posted[key])
        outbox, = self.store.pending_outbox()
        self.assertEqual(outbox["job_id"], posted["job_id"])
        self.assertEqual(outbox["payload"]["purpose"], "student")
        self.assertEqual(outbox["payload"]["resource_id"], posted["response_id"])
        with self.assertRaises(IdempotencyConflict):
            self.post("different", task_id="task-1", question_version_id="q1:1")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM responses").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM response_outbox").fetchone()[0], 1)

        with self.assertRaises(IdempotencyConflict):
            self.store.mark_outbox(outbox["outbox_id"], job_id="different-job")
        marked = self.store.mark_outbox(outbox["outbox_id"], job_id=posted["job_id"])
        self.assertEqual(marked["status"], "dispatched")
        self.assertEqual(self.store.pending_outbox(), [])
        self.assertEqual(self.store.mark_outbox(outbox["outbox_id"])["status"], "dispatched")

    def test_concurrent_idempotent_posts_produce_single_fact_and_job(self):
        stores = [CompanionStore(self.database, answer_policy=self.policy) for _ in range(5)]
        def create(index):
            return stores[index % len(stores)].post_message(self.conversation["id"], "student-1", "same",
                                                              request_id="race")
        with ThreadPoolExecutor(max_workers=10) as pool:
            rows = list(pool.map(create, range(10)))
        self.assertEqual(len({row["message_id"] for row in rows}), 1)
        self.assertEqual(len({row["response_id"] for row in rows}), 1)
        self.assertEqual(len({row["job_id"] for row in rows}), 1)
        self.assertEqual(len([row for row in self.store.pending_outbox() if row["response_id"] == rows[0]["response_id"]]), 1)

    def test_assistant_delta_commit_terminal_transaction_and_replay(self):
        posted = self.post(task_id="task-1", question_version_id="q1:1")
        with self.assertRaises(InvalidTransition):
            self.store.append_delta(posted["response_id"], 2, "skip", call_id="call", generation=1)
        self.store.append_delta(posted["response_id"], 1, "hello", call_id="call", generation=1)
        self.store.append_delta(posted["response_id"], 2, " world", call_id="call", generation=1)
        with self.assertRaises(IdempotencyConflict):
            self.store.append_delta(posted["response_id"], 1, "other", call_id="call", generation=1)
        with self.assertRaises(ValueError):
            self.store.append_delta(posted["response_id"], 3, "\x00", call_id="call", generation=1)
        self.policy.allowed = True
        completed = self.store.complete_response(posted["response_id"], "hello world", call_id="call", generation=1)
        self.assertEqual(completed["status"], "completed")
        messages = self.store.list_messages(self.conversation["id"], "student-1")
        self.assertEqual([row["text"] for row in messages], ["question", "hello world"])
        events = self.store.list_response_events(posted["response_id"], "student-1", answer_policy=self.policy)
        self.assertEqual([row["event_type"] for row in events], ["start", "delta", "delta", "end"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event_type='end'").fetchone()[0], 1)
        self.assertEqual(self.store.complete_response(posted["response_id"], "hello world", call_id="call", generation=1)["status"], "completed")
        with self.assertRaises(InvalidTransition):
            self.store.append_delta(posted["response_id"], 3, "late", call_id="call", generation=1)

    def test_generation_takeover_resets_committed_deltas_and_fences_stale_writer(self):
        posted = self.post()
        response_id = posted["response_id"]
        self.store.append_delta(response_id, 1, "old", call_id="call-1", generation=1)
        self.store.begin_response(response_id, call_id="call-2", generation=2, expected_generation=1)
        with self.assertRaises(StaleGeneration):
            self.store.append_delta(response_id, 2, "stale", call_id="call-1", generation=1)
        self.store.append_delta(response_id, 1, "new", call_id="call-2", generation=2)
        self.store.complete_response(response_id, "new", call_id="call-2", generation=2)
        events = self.store.list_response_events(response_id)
        self.assertEqual([row["event_type"] for row in events], ["reset", "delta", "end"])
        self.assertEqual([row["generation"] for row in events], [2, 2, 2])
        with self.assertRaises(InvalidTransition):
            self.store.begin_response(response_id, call_id="old", generation=3, expected_generation=1)

    def test_unknown_policy_hides_full_multi_question_history_but_not_safe_hints(self):
        posted = self.post(task_id="task-1", question_version_id="q1:1")
        self.store.append_delta(posted["response_id"], 1, "answer is 42", call_id="call", generation=1)
        self.store.complete_response(posted["response_id"], "answer is 42", call_id="call", generation=1)
        user_input = self.store.post_message(self.conversation["id"], "student-1", "unscoped follow-up", request_id="message-2")
        self.store.complete_response(user_input["response_id"], SAFE_GUIDANCE["try_first"], call_id="hint", generation=1)
        self.policy.allowed = False
        messages = self.store.list_messages(self.conversation["id"], "student-1", answer_policy=self.policy)
        self.assertEqual(messages[1]["text"], "")
        self.assertTrue(messages[1]["redacted"])
        self.assertIn("保护独立作答", messages[1]["notice"])
        self.assertEqual(messages[3]["text"], SAFE_GUIDANCE["try_first"])

        # When the learning API is missing, every freeform assistant item closes.
        no_policy_store = CompanionStore(self.database)
        unknown = no_policy_store.list_messages(self.conversation["id"], "student-1")
        self.assertEqual(unknown[1]["text"], "")
        self.assertTrue(unknown[1]["redacted"])
        self.assertEqual(unknown[3]["text"], SAFE_GUIDANCE["try_first"])
        response = self.store.get_response(posted["response_id"], "student-1", answer_policy=self.policy)
        self.assertEqual(response["task_id"], "task-1")
        self.assertEqual(response["question_version_id"], "q1:1")
        self.assertEqual(response["course_id"], "course-1")
        self.assertEqual(response["purpose"], "student")
        self.assertEqual(self.store.get_response(posted["response_id"])["text"], "answer is 42")

    def test_request_context_has_only_current_and_bounded_user_turns_and_fail_closes(self):
        first = self.post("ordinary question")
        # A selected task's freeform generation still checks the all-active fallback.
        self.store.append_delta(first["response_id"], 1, "ok", call_id="call", generation=1)
        self.store.complete_response(first["response_id"], "ok", call_id="call", generation=1)
        second = self.store.post_message(self.conversation["id"], "student-1", "question about task",
                                         request_id="message-2", task_id="task-1", question_version_id="q1:1")
        context = self.store.build_request_context(second["response_id"], answer_policy=self.policy)
        self.assertFalse(context["allow_generation"])
        self.assertEqual(context["current_question"], "question about task")
        self.assertNotIn("student-1", json.dumps(context))
        self.assertNotIn("course-1", json.dumps(context))
        self.policy.allowed = True
        context = self.store.build_request_context(second["response_id"], answer_policy=self.policy)
        self.assertTrue(context["allow_generation"])
        self.assertEqual(context["messages"][-1]["content"], "question about task")
        diagnostic = MutablePolicy(True, diagnostic=True)
        context = self.store.build_request_context(second["response_id"], answer_policy=diagnostic)
        self.assertEqual(context["safe_guidance_key"], "independent_attempt")

    def test_failed_response_and_safe_guidance_end_atomically(self):
        failed = self.post(request_id="failed")
        self.store.fail_response(failed["response_id"], "provider unavailable", call_id="call", generation=1)
        failed_view = self.store.get_response(failed["response_id"], "student-1", answer_policy=self.policy)
        self.assertEqual(failed_view["text"], "")
        self.assertIsNone(failed_view["error"])
        events = self.store.list_response_events(failed["response_id"])
        self.assertEqual([row["event_type"] for row in events], ["start", "end"])
        self.assertEqual(events[-1]["data"]["status"], "failed")
        with self.assertRaises(InvalidTransition):
            self.store.complete_response(failed["response_id"], call_id="call", generation=1)

        hint = self.post(request_id="hint")
        self.store.complete_response(hint["response_id"], SAFE_GUIDANCE["independent_attempt"], call_id="hint-call", generation=1)
        self.assertEqual(self.store.get_response(hint["response_id"], "student-1", answer_policy=self.policy)["text"], SAFE_GUIDANCE["independent_attempt"])

    def test_ownership_archive_restore_collection_notes_memories_and_dynamic_projection(self):
        with self.assertRaises(NotFound):
            self.store.list_messages(self.conversation["id"], "student-2")
        with self.assertRaises(NotFound):
            self.store.get_conversation(self.conversation["id"], "student-2")
        with self.assertRaises(NotFound):
            self.store.get_response("missing-response", "student-1")
        posted = self.post(task_id="task-1", question_version_id="q1:1")
        with self.assertRaises(NotFound):
            self.store.get_response(posted["response_id"], "student-2")
        self.store.append_delta(posted["response_id"], 1, "private answer", call_id="call", generation=1)
        self.store.complete_response(posted["response_id"], "private answer", call_id="call", generation=1)
        collection = self.store.create_collection("student-1", "Saved", request_id="collection")
        item = self.store.add_collection_item(collection["id"], "student-1", "question", request_id="question", task_id="task-1", question_version_id="q1:1")
        self.assertTrue(item["redacted"])
        note_text = "<script>alert('not executed')</script>\nplain note"
        note = self.store.create_note(item["id"], "student-1", note_text, request_id="note")
        self.assertEqual(note["text"], note_text)
        self.assertEqual(self.store.list_notes(item["id"], "student-1")[0]["text"], note_text)
        self.store.update_note(note["id"], "student-1", "edited", expected_version=1)
        with self.assertRaises(InvalidTransition):
            self.store.update_note(note["id"], "student-1", "stale edit", expected_version=1)
        self.policy.allowed = True
        self.assertFalse(self.store.list_collection_items(collection["id"], "student-1")[0]["redacted"])
        self.policy.allowed = False
        self.assertTrue(self.store.list_collection_items(collection["id"], "student-1")[0]["redacted"])
        saved_message = self.store.add_collection_item(collection["id"], "student-1", "message", request_id="message", message_id=self.store.list_messages(self.conversation["id"], "student-1")[1]["id"])
        self.assertTrue(saved_message["redacted"])
        memory = self.store.create_memory("student-1", message_id=saved_message["message_id"], request_id="memory")
        self.assertTrue(memory["redacted"])
        self.assertEqual(self.store.list_memories("student-1")[0]["text"], "")
        archived = self.store.archive_conversation(self.conversation["id"], "student-1")
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(len(self.store.list_messages(self.conversation["id"], "student-1")), 2)
        with self.assertRaises(InvalidTransition):
            self.post(request_id="cannot-post")
        self.assertEqual(self.store.restore_conversation(self.conversation["id"], "student-1")["status"], "active")
        self.store.update_collection(collection["id"], "student-1", archived=True)
        self.assertEqual(self.store.update_collection(collection["id"], "student-1", archived=False)["status"], "active")

    def test_notifications_are_idempotent_user_scoped_and_read_never_authorizes(self):
        notification = self.store.create_notification("student-1", "grade", "task", "task-1",
                                                       business_key="grade:attempt-1", payload={"task_id": "task-1", "summary": "score"})
        replay = self.store.create_notification("student-1", "grade", "task", "task-1",
                                                  business_key="grade:attempt-1", payload={"task_id": "task-1", "summary": "score"})
        self.assertEqual(notification["id"], replay["id"])
        with self.assertRaises(IdempotencyConflict):
            self.store.create_notification("student-1", "grade", "task", "task-1", business_key="grade:attempt-1", payload={"task_id": "task-1"})
        with self.assertRaises(NotFound):
            self.store.mark_notification_read(notification["id"], "student-2")
        self.assertIsNotNone(self.store.mark_notification_read(notification["id"], "student-1")["read_at"])
        self.assertEqual(self.store.list_notifications("student-2"), [])
        hidden = self.store.list_notifications("student-1")[0]
        self.assertTrue(hidden["redacted"])
        self.assertEqual(hidden["payload"], {})

    def test_discussion_binding_access_deferred_teacher_text_and_preset(self):
        thread = self.store.create_discussion_thread("student-1", "course-1", "class-1", "task-1", "attempt-1", "q1:1", 1,
                                                     created_by="teacher-1", request_id="thread")
        deferred = self.store.post_discussion_message(thread["id"], "teacher-1", "答案是42", author_role="teacher", request_id="freeform", can_manage=True)
        self.assertEqual(deferred["delivery"], "deferred")
        hidden = self.store.list_discussion_messages(thread["id"], "student-1", role="student", results_released=False)
        self.assertTrue(hidden[0]["redacted"])
        self.assertEqual(hidden[0]["text"], "")
        self.assertEqual(len(self.store.list_discussion_messages(thread["id"], "student-1", role="student", results_released=True)), 1)
        safe = self.store.post_discussion_message(thread["id"], "teacher-1", author_role="teacher", request_id="preset", preset_key="try_first", can_manage=True)
        self.assertEqual(safe["delivery"], "delivered")
        student = self.store.post_discussion_message(thread["id"], "student-1", "我的尝试", author_role="student", request_id="reply")
        self.assertEqual(student["author_role"], "student")
        with self.assertRaises(NotFound):
            self.store.post_discussion_message(thread["id"], "reader", "no", author_role="teacher", request_id="forged", can_manage=False)
        with self.assertRaises(ValueError):
            self.store.post_discussion_message(thread["id"], "teacher-1", "altered preset", author_role="teacher", request_id="unsafe", preset_key="try_first", results_released=False, can_manage=True)

    def test_static_filters_preserve_scope_order_and_pagination(self):
        threads = [self.store.create_discussion_thread(
            student, "course-1", classroom, task, f"attempt-{index}", "q1:1", 1,
            created_by="teacher-1", request_id=f"thread-{index}")
            for index, (student, classroom, task) in enumerate((
                ("student-1", "class-1", "task-1"),
                ("student-1", "class-2", "task-2"),
                ("student-2", "class-1", "task-1")))]
        with self.store.connect() as db:
            db.executemany("UPDATE discussion_threads SET updated_at=? WHERE id=?",
                           [(index + 1, row["id"]) for index, row in enumerate(threads)])
        self.assertEqual([row["id"] for row in self.store.list_discussion_threads(student_id="student-1")],
                         [threads[1]["id"], threads[0]["id"]])
        self.assertEqual([row["id"] for row in self.store.list_discussion_threads(class_id="class-1", task_id="task-1", limit=1)],
                         [threads[2]["id"]])
        self.assertEqual([row["id"] for row in self.store.list_discussion_threads(student_id="student-1", class_id="class-1", task_id="task-1")],
                         [threads[0]["id"]])
        self.assertEqual(self.store.list_discussion_threads(student_id="student-1' OR 1=1 --"), [])
        with self.assertRaises(ValueError):
            self.store.list_discussion_threads(task_id="task-1")

        notifications = [self.store.create_notification(
            "student-1", "grade", "task", "task-1", business_key=f"grade-{index}", payload={})
            for index in range(3)]
        with self.store.connect() as db:
            db.executemany("UPDATE notifications SET created_at=? WHERE id=?",
                           [(index + 1, row["id"]) for index, row in enumerate(notifications)])
        self.store.mark_notification_read(notifications[1]["id"], "student-1")
        self.assertEqual([row["id"] for row in self.store.list_notifications("student-1", limit=1, offset=1)],
                         [notifications[1]["id"]])
        self.assertEqual([row["id"] for row in self.store.list_notifications("student-1", unread_only=True)],
                         [notifications[2]["id"], notifications[0]["id"]])
        self.assertEqual([row["id"] for row in self.store.list_notifications("student-1", unread_only=True, limit=1, offset=1)],
                         [notifications[0]["id"]])
        self.assertEqual(self.store.list_notifications("student-2", unread_only=True), [])

        collection = self.store.create_collection("student-1", "Saved", request_id="hold-collection")
        scopes = (("all", None), ("user", "student-1"),
                  ("conversation", self.conversation["id"]),
                  ("collection", collection["id"]), ("discussion_thread", threads[0]["id"]))
        holds = [self.store.create_legal_hold(scope, resource_id, created_by="admin", reason="review",
                                             request_id=f"hold-{index}")
                 for index, (scope, resource_id) in enumerate(scopes)]
        with self.store.connect() as db:
            db.executemany("UPDATE legal_holds SET created_at=? WHERE id=?",
                           [(index + 1, row["id"]) for index, row in enumerate(holds)])
            db.execute("UPDATE legal_holds SET expires_at=? WHERE id=?", (100, holds[0]["id"]))
        self.store.release_legal_hold(holds[1]["id"], actor_id="admin", reason="closed")
        self.assertEqual([row["id"] for row in self.store.list_legal_holds(now=100)],
                         [row["id"] for row in reversed(holds[2:])])
        self.assertEqual([row["id"] for row in self.store.list_legal_holds(now=100, active_only=False)],
                         [row["id"] for row in reversed(holds)])
        for scope in ("conversation", "collection", "discussion_thread"):
            with self.subTest(scope=scope), self.assertRaises(NotFound):
                self.store.create_legal_hold(scope, "missing", created_by="admin", reason="review",
                                             request_id=f"missing-{scope}")

    def test_retention_holds_audit_preview_and_no_fact_deletion(self):
        old = self.conversation["id"]
        self.store.archive_conversation(old, "student-1")
        self.store.set_retention_policy("conversations", 1, actor_id="admin", reason="approved data policy")
        hold = self.store.create_legal_hold("conversation", old, created_by="admin", reason="case review", request_id="hold")
        with self.store.connect() as db:
            db.execute("UPDATE conversations SET updated_at=0 WHERE id=?", (old,))
        preview = self.store.preview_cleanup(now=200000, limit=5)
        entry = next(row for row in preview["policies"] if row["resource_type"] == "conversations")
        self.assertEqual(entry["eligible_count"], 0)
        self.assertEqual(entry["held_count"], 1)
        self.assertEqual(preview["deleted_count"], 0)
        self.assertEqual(self.store.release_legal_hold(hold["id"], actor_id="admin", reason="case closed")["released_by"], "admin")
        preview = self.store.preview_cleanup(now=200000)
        entry = next(row for row in preview["policies"] if row["resource_type"] == "conversations")
        self.assertEqual(entry["eligible_count"], 1)
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM conversations WHERE id=?", (old,)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events").fetchone()[0], 3)


class CompanionRouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="companion-router-test-")
        self.addCleanup(self.directory.cleanup)
        self.platform = PlatformStore(Path(self.directory.name) / "platform.sqlite3")
        self.owner = self.platform.create_user("owner", "Course owner", "hash", "teacher")
        self.teacher = self.platform.create_user("reader", "Course reader", "hash", "teacher")
        self.student = self.platform.create_user("student", "Student", "hash", "student")
        course = self.platform.create_course("Math", "2026", self.owner["id"], course_id="course")
        classroom = self.platform.create_class(course["id"], "Class 1", owner_teacher_id=self.owner["id"], class_id="class")
        self.platform.create_course_membership(course["id"], self.student["id"])
        self.platform.create_class_membership(classroom["id"], self.student["id"])
        self.platform.set_class_collaborator(classroom["id"], self.teacher["id"], "read", granted_by=self.owner["id"])
        self.admin = self.platform.create_user("admin", "Admin", "hash", "admin")
        self.policy = MutablePolicy()
        self.companion = CompanionStore(Path(self.directory.name) / "companion.sqlite3", answer_policy=self.policy)
        self.learning = FakeLearning(self.student["id"], {self.owner["id"], self.teacher["id"]})
        self.runtime = SimpleNamespace(companion=self.companion, learning=self.learning, platform=self.platform)
        self.app = FastAPI()
        self.app.state.runtime = self.runtime
        self.app.state.platform_store = self.platform
        self.app.dependency_overrides[get_platform_store] = lambda: self.platform
        self.app.dependency_overrides[get_platform_user] = self._current_user
        self.current = self.student
        self.client = TestClient(self.app)
        self.app.include_router(router)
        self.addCleanup(self.client.close)

    def _current_user(self):
        row = self.current
        return User(id=row["id"], username=row["username"], display_name=row["display_name"], password_hash="",
                    role=row["role"], status=row["status"])

    def test_routes_require_authenticated_user_keep_student_scope_and_never_trigger_ai(self):
        response = self.client.post("/api/v1/companion/conversations", json={"title": "Math", "request_id": "c"})
        self.assertEqual(response.status_code, 201, response.text)
        conversation_id = response.json()["id"]
        other = self.platform.create_user("other", "Other", "hash", "student")
        self.current = other
        self.assertEqual(self.client.get(f"/api/v1/companion/conversations/{conversation_id}").status_code, 404)
        self.current = self.student
        self.assertEqual(self.client.post(f"/api/v1/companion/conversations/{conversation_id}/messages", json={
            "request_id": "m", "text": "hello", "task_id": "task-1", "question_version_id": "q1:1",
        }).status_code, 202)
        self.assertEqual(self.learning.calls.count("get_answer_policy"), 0)
        self.assertFalse(hasattr(self.runtime, "gateway"))
        posted = self.companion.pending_outbox()[0]
        self.assertEqual(posted["payload"]["resource_id"], self.companion.pending_outbox()[0]["response_id"])
        self.assertEqual(self.client.get(f"/api/v1/companion/conversations/{conversation_id}/messages").status_code, 200)

    def test_collection_question_projection_does_not_leak_answer_and_retention_admin_only(self):
        collection = self.client.post("/api/v1/companion/collections", json={"title": "Saved", "request_id": "collection"})
        self.assertEqual(collection.status_code, 201)
        item = self.client.post(f"/api/v1/companion/collections/{collection.json()['id']}/items", json={
            "request_id": "question", "kind": "question", "task_id": "task-1", "question_version_id": "q1:1",
        })
        self.assertEqual(item.status_code, 201, item.text)
        self.assertNotIn("answer", item.json()["question"])
        self.assertEqual(self.client.get("/api/v1/companion/admin/retention-policies").status_code, 403)
        self.current = self.admin
        self.assertEqual(self.client.get("/api/v1/companion/admin/retention-policies").status_code, 200)

    def test_teacher_read_collaborator_can_read_threads_but_cannot_post_short_comment(self):
        self.current = self.owner
        thread = self.client.post("/api/v1/companion/discussion-threads", json={
            "request_id": "thread", "task_id": "task-1", "attempt_id": "attempt-1",
            "question_version_id": "q1:1", "grading_revision": 1,
        })
        self.assertEqual(thread.status_code, 201, thread.text)
        self.current = self.teacher
        listing = self.client.get("/api/v1/companion/discussion-threads", params={"class_id": "class"})
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([row["id"] for row in listing.json()], [thread.json()["id"]])
        self.assertEqual(self.client.post(f"/api/v1/companion/discussion-threads/{thread.json()['id']}/messages", json={
            "request_id": "comment", "text": "secret"}).status_code, 403)

    def test_student_thread_access_and_results_gate_revalidated_on_each_request(self):
        self.current = self.owner
        thread = self.client.post("/api/v1/companion/discussion-threads", json={
            "request_id": "thread", "task_id": "task-1", "attempt_id": "attempt-1",
            "question_version_id": "q1:1", "grading_revision": 1,
        }).json()
        self.assertEqual(self.client.post(f"/api/v1/companion/discussion-threads/{thread['id']}/messages", json={
            "request_id": "teacher", "text": "答案为42"}).status_code, 201)
        self.current = self.student
        hidden = self.client.get(f"/api/v1/companion/discussion-threads/{thread['id']}").json()["messages"]
        self.assertTrue(hidden[0]["redacted"])
        self.assertEqual(hidden[0]["text"], "")
        self.assertEqual(self.client.post(f"/api/v1/companion/discussion-threads/{thread['id']}/messages", json={
            "request_id": "student", "text": "我的尝试"}).status_code, 201)
        self.learning.released = True
        shown = self.client.get(f"/api/v1/companion/discussion-threads/{thread['id']}").json()["messages"]
        self.assertEqual(shown[0]["text"], "答案为42")


class FakeLearning:
    def __init__(self, student_id, teacher_ids=()):
        self.student_id = student_id
        self.teacher_ids = set(teacher_ids)
        self.calls = []
        self.released = False

    def get_task_scope(self, task_id, actor_id):
        if actor_id != self.student_id or task_id != "task-1":
            raise LookupError("task not found")
        return {"task_id": task_id, "student_id": self.student_id, "course_id": "course", "class_id": "class"}

    def get_task(self, task_id, actor_id):
        if actor_id != self.student_id:
            raise LookupError("task not found")
        return {"id": task_id, "questions": [{"id": "q1", "version": 1, "stem": "Safe question", "options": ["a"], "answer": "answer", "explanation": "secret"}]}

    def get_answer_policy(self, actor_id, *, task_id=None, question_version_id=None):
        self.calls.append("get_answer_policy")
        if actor_id != self.student_id:
            raise LookupError("actor mismatch")
        return {"allow_assistant_content": self.released, "allow_generation": self.released,
                "results_released": self.released, "attempted": self.released, "diagnostic": not self.released}

    def get_discussion_scope(self, actor_id, *, task_id, attempt_id, question_version_id, grading_revision):
        if actor_id not in self.teacher_ids | {self.student_id} or task_id != "task-1" or attempt_id != "attempt-1" or question_version_id != "q1:1" or grading_revision != 1:
            raise LookupError("binding unavailable")
        return {"student_id": self.student_id, "course_id": "course", "class_id": "class", "task_id": task_id,
                "attempt_id": attempt_id, "question_version_id": question_version_id, "grading_revision": grading_revision,
                "results_released": self.released}


if __name__ == "__main__":
    unittest.main()
