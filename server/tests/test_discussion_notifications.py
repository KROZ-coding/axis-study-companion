"""Discussion notification transactions and current access, using disposable databases."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.companion_store import CompanionStore, SAFE_GUIDANCE
from app.models import User
from app.platform_store import PlatformStore
from app.routers.v1_companion import router
from app.v1_deps import get_platform_store, get_platform_user


class LearningScope:
    """Only answer release and frozen task bindings; never calls an upstream model."""

    def __init__(self, student_id, teacher_ids):
        self.student_id = student_id
        self.teacher_ids = set(teacher_ids)
        self.status = "grading"
        self.scope_calls = []

    def get_task_scope(self, task_id, actor_id):
        self.scope_calls.append((task_id, actor_id))
        if task_id != "task" or actor_id not in self.teacher_ids | {self.student_id}:
            raise LookupError("task not found")
        return {"task_id": task_id, "student_id": self.student_id, "course_id": "course", "class_id": "class"}

    def get_task(self, task_id, actor_id):
        self.get_task_scope(task_id, actor_id)
        return {"id": task_id, "status": self.status,
                "questions": [{"id": "q1", "version": 1}, {"id": "q2", "version": 1}]}

    def get_answer_policy(self, actor_id, *, task_id=None, question_version_id=None):
        if actor_id != self.student_id:
            raise PermissionError("answer policy requires student")
        if task_id is not None:
            self.get_task_scope(task_id, actor_id)
        released = self.status == "results_ready"
        return {"allow_assistant_content": released, "allow_generation": released,
                "results_released": released, "attempted": True, "diagnostic": False}

    def get_discussion_scope(self, actor_id, *, task_id, attempt_id, question_version_id, grading_revision):
        scope = self.get_task_scope(task_id, actor_id)
        if (attempt_id != "attempt" or question_version_id not in {"q1:1", "q2:1"}
                or grading_revision != 1):
            raise LookupError("discussion binding not found")
        return {**scope, "attempt_id": attempt_id, "question_version_id": question_version_id,
                "grading_revision": grading_revision, "results_released": self.status == "results_ready"}


class DiscussionNotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="discussion-notification-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.platform = PlatformStore(self.root / "platform.sqlite3")
        self.users = {}
        for name, role in (("owner", "teacher"), ("manager", "teacher"), ("reader", "teacher"),
                           ("unrelated", "teacher"), ("disabled", "teacher"),
                           ("student", "student"), ("other", "student"), ("admin", "admin")):
            self.users[name] = self.platform.create_user(name, name, "test-hash", role, user_id=name)
        self.platform.create_course("Math", "2026", "owner", course_id="course")
        self.platform.create_class("course", "Class", owner_teacher_id="owner", class_id="class")
        self.platform.create_class("course", "Other class", owner_teacher_id="unrelated", class_id="other-class")
        for user, classroom in (("student", "class"), ("other", "other-class")):
            self.platform.create_course_membership("course", user)
            self.platform.create_class_membership(classroom, user)
        for user, level in (("manager", "manage"), ("reader", "read"), ("disabled", "manage")):
            self.platform.set_class_collaborator("class", user, level, granted_by="owner")
        self.platform.update_user("disabled", status="disabled")
        self.platform.set_course_collaborator("course", "unrelated", "manage", granted_by="owner")
        self.learning = LearningScope("student", {"owner", "manager", "reader", "unrelated", "disabled"})
        self.companion = CompanionStore(self.root / "companion.sqlite3", busy_timeout=2)
        self.runtime = SimpleNamespace(platform=self.platform, learning=self.learning, companion=self.companion)
        self.current = "owner"
        app = FastAPI()
        app.state.runtime = self.runtime
        app.dependency_overrides[get_platform_store] = lambda: self.platform
        app.dependency_overrides[get_platform_user] = self.current_user
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.thread = self.create_thread()
        self.messages_url = f"/api/v1/companion/discussion-threads/{self.thread['id']}/messages"

    def current_user(self):
        row = self.platform.get_user(self.current)
        return User(id=row["id"], username=row["username"], display_name=row["display_name"],
                    password_hash="", role=row["role"], status=row["status"])

    def create_thread(self, question="q1:1", request_id="thread", title="教师短评"):
        response = self.client.post("/api/v1/companion/discussion-threads", json={
            "task_id": "task", "attempt_id": "attempt", "question_version_id": question,
            "grading_revision": 1, "request_id": request_id, "title": title,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def post(self, actor, key, text="", preset=None, expected=201):
        self.current = actor
        body = {"request_id": key, "text": text}
        if preset is not None:
            body["preset_key"] = preset
        response = self.client.post(self.messages_url, json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def notifications(self, actor, **params):
        self.current = actor
        response = self.client.get("/api/v1/me/notifications", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def counts(self):
        with self.companion.connect() as db:
            return (db.execute("SELECT count(*) FROM discussion_messages").fetchone()[0],
                    db.execute("SELECT count(*) FROM notifications").fetchone()[0])

    def assert_locator(self, notification, message):
        self.assertEqual(notification["resource_type"], "discussion_thread")
        self.assertEqual(notification["resource_id"], self.thread["id"])
        payload = notification["payload"]
        expected = {"task_id": "task", "attempt_id": "attempt", "thread_id": self.thread["id"],
                    "message_id": message["id"], "question_version_id": "q1:1", "question_version": "q1:1",
                    "grading_revision": 1, "class_id": "class", "course_id": "course", "student_id": "student"}
        for key, value in expected.items():
            self.assertEqual(payload[key], value, key)
        self.assertEqual(payload["event_type"], notification["event_type"])

    def test_free_text_and_preset_follow_whole_task_release_and_retry(self):
        text = "答案是42；另一题的答案是秘密"
        deferred = self.post("owner", "comment", text)
        safe = self.post("owner", "preset", preset="try_first")
        self.assertEqual(deferred["delivery"], "deferred")
        self.assertEqual(safe["delivery"], "delivered")
        for state, released in (("grading", False), ("awaiting_manual", False),
                                ("results_ready", True), ("in_progress", False)):
            with self.subTest(state=state):
                self.learning.status = state
                notices = {row["payload"]["message_id"]: row for row in self.notifications("student")}
                free_notice, safe_notice = notices[deferred["id"]], notices[safe["id"]]
                self.assert_locator(free_notice, deferred)
                self.assertEqual(free_notice["redacted"], not released)
                self.assertEqual(free_notice["payload"]["delivery"], "delivered" if released else "deferred")
                self.assertFalse(safe_notice["redacted"])
                self.assertEqual(safe_notice["payload"]["delivery"], "delivered")
                # Notifications never embed free text, including after release.
                self.assertNotIn(text, json.dumps(notices, ensure_ascii=False))
                rows = self.client.get(self.messages_url).json()
                self.assertEqual(rows[0]["text"], text if released else "")
                self.assertEqual(rows[0]["redacted"], not released)
                self.assertEqual(rows[1]["text"], SAFE_GUIDANCE["try_first"])
        with self.companion.connect() as db:
            self.assertTrue(all(text not in row[0] for row in db.execute("SELECT payload_json FROM notifications")))

    def test_replays_preserve_message_and_notification_identity_without_self_notification(self):
        first = self.post("owner", "comment", "私有讲解")
        self.assertEqual(self.post("owner", "comment", "私有讲解")["id"], first["id"])
        self.assertEqual(self.create_thread()["id"], self.thread["id"])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.notifications("owner"), [])
        self.assertEqual(self.notifications("manager"), [])
        student_notice, = self.notifications("student")
        self.assertEqual(student_notice["event_type"], "discussion_teacher_message")
        self.assertEqual(student_notice["business_key"], f"discussion:{first['id']}:student")
        self.post("owner", "comment", "changed", expected=409)
        self.assertEqual(self.counts(), (1, 1))

    def test_student_reply_only_notifies_current_management_teachers_and_replay_does_not_refanout(self):
        reply = self.post("student", "reply", "请核对我的步骤")
        for actor in ("owner", "manager"):
            notification, = self.notifications(actor)
            self.assertEqual(notification["event_type"], "discussion_student_reply")
            self.assertFalse(notification["redacted"])
            self.assert_locator(notification, reply)
        for actor in ("reader", "unrelated", "disabled", "student", "admin"):
            self.assertEqual(self.notifications(actor), [])
        self.platform.set_class_collaborator("class", "reader", "manage", granted_by="owner")
        self.platform.remove_class_collaborator("class", "manager", removed_by="owner")
        replay = self.post("student", "reply", "请核对我的步骤")
        self.assertEqual(replay["id"], reply["id"])
        self.assertEqual(self.counts(), (1, 2))
        self.assertEqual(self.notifications("reader"), [])
        self.post("student", "next-reply", "继续说明")
        self.assertEqual(len(self.notifications("owner")), 2)
        self.assertEqual(len(self.notifications("reader")), 1)
        manager_notice, = self.notifications("manager")
        self.assertTrue(manager_notice["redacted"])
        self.assertEqual(manager_notice["payload"], {})

    def test_message_and_all_recipient_notifications_roll_back_together(self):
        before = self.companion.get_discussion_thread(self.thread["id"])["updated_at"]
        insert = self.companion._insert_notification
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("injected transaction failure")
            return insert(*args, **kwargs)

        with patch.object(self.companion, "_insert_notification", side_effect=fail_second):
            error = self.post("student", "retryable", "应当原子保存", expected=503)
        self.assertTrue(error["detail"]["retryable"])
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.companion.get_discussion_thread(self.thread["id"])["updated_at"], before)
        self.post("student", "retryable", "应当原子保存")
        self.assertEqual(self.counts(), (1, 2))

    def test_concurrent_message_replay_has_one_fact_and_one_notification_per_recipient(self):
        def post(_):
            return self.companion.post_discussion_message(
                self.thread["id"], "student", "同一回复", author_role="student", request_id="concurrent",
                teacher_recipient_ids=("owner", "manager", "owner", "student"),
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(post, range(8)))
        self.assertEqual(len({row["id"] for row in replies}), 1)
        self.assertEqual(self.counts(), (1, 2))
        self.assertEqual(self.notifications("student"), [])

    def test_read_teacher_cannot_write_and_demotion_redacts_old_reply_notification(self):
        self.post("owner", "comment", "教师可读原文")
        self.current = "reader"
        detail = self.client.get(f"/api/v1/companion/discussion-threads/{self.thread['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["can_reply"])
        self.assertEqual(detail.json()["messages"][0]["text"], "教师可读原文")
        self.post("reader", "blocked", "不可写", expected=403)
        denied = self.client.post("/api/v1/companion/discussion-threads", json={
            "request_id": "blocked-thread", "task_id": "task", "attempt_id": "attempt",
            "question_version_id": "q2:1", "grading_revision": 1,
        })
        self.assertEqual(denied.status_code, 403)
        self.post("student", "reply", "回复")
        notification, = self.notifications("manager")
        self.assertFalse(notification["redacted"])
        self.platform.set_class_collaborator("class", "manager", "read", granted_by="owner")
        hidden, = self.notifications("manager")
        self.assertTrue(hidden["redacted"])
        self.assertEqual(hidden["payload"], {})
        self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        self.post("manager", "demoted", "blocked", expected=403)
        marked = self.client.post(f"/api/v1/me/notifications/{notification['id']}/read")
        self.assertEqual(marked.status_code, 200)
        self.assertEqual(marked.json()["payload"], {})

    def test_cross_class_and_revoked_student_cannot_read_or_write_thread(self):
        self.post("owner", "comment", "私有")
        for actor in ("unrelated", "other", "admin"):
            with self.subTest(actor=actor):
                self.current = actor
                self.assertIn(self.client.get(self.messages_url).status_code, (403, 404))
                self.post(actor, "foreign", "blocked", expected=404 if actor != "admin" else 404)
        self.platform.delete_class_membership("class", "student")
        hidden, = self.notifications("student")
        self.assertEqual(hidden["payload"], {})
        self.assertTrue(hidden["redacted"])
        self.assertEqual(self.client.get(self.messages_url).status_code, 404)
        self.post("student", "revoked", "blocked", expected=404)
        self.current = "owner"
        self.assertEqual(self.client.get(self.messages_url).status_code, 404)
        self.assertEqual(self.counts(), (1, 1))

    def test_mark_read_is_owned_idempotent_and_reprojects_after_retry(self):
        message = self.post("owner", "comment", "完整解析")
        notice, = self.notifications("student")
        self.current = "other"
        url = f"/api/v1/me/notifications/{notice['id']}/read"
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertEqual(len(self.notifications("student", unread_only=True)), 1)
        self.learning.status = "results_ready"
        first = self.client.post(url).json()
        self.assertFalse(first["redacted"])
        self.assert_locator(first, message)
        self.learning.status = "in_progress"
        second = self.client.post(url).json()
        self.assertEqual(second["read_at"], first["read_at"])
        self.assertTrue(second["redacted"])
        self.assertEqual(second["payload"]["delivery"], "deferred")
        self.assertEqual(self.notifications("student", unread_only=True), [])
        self.assertEqual(len(self.notifications("student")), 1)

    def test_teacher_task_notification_uses_actor_scope_and_current_class_access(self):
        created = self.companion.create_notification(
            "manager", "appeal_received", "task", "task", business_key="appeal:one:manager",
            payload={"appeal_id": "appeal-one", "task_id": "task", "attempt_id": "attempt",
                     "question_version_id": "q1:1", "grading_revision": 1},
        )
        notice, = self.notifications("manager")
        self.assertFalse(notice["redacted"])
        self.assertEqual(notice["payload"]["appeal_id"], "appeal-one")
        self.assertIn(("task", "manager"), self.learning.scope_calls)
        self.assertEqual(notice["payload"]["attempt_id"], "attempt")
        self.platform.remove_class_collaborator("class", "manager", removed_by="owner")
        hidden, = self.notifications("manager")
        self.assertTrue(hidden["redacted"])
        self.assertEqual(hidden["payload"], {})
        response = self.client.post(f"/api/v1/me/notifications/{created['id']}/read")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"], {})

    def test_generic_notification_payload_remains_answer_gated_for_student(self):
        self.companion.create_notification("student", "results_ready", "task", "task", business_key="result:attempt",
                                           payload={"task_id": "task", "summary": "不能提前显示的评分原文"})
        for state, released in (("grading", False), ("results_ready", True), ("in_progress", False)):
            self.learning.status = state
            notice, = self.notifications("student")
            self.assertEqual(notice["redacted"], not released)
            self.assertEqual(bool(notice["payload"]), released)
        with patch.object(self.learning, "get_answer_policy", side_effect=LookupError("unavailable")):
            notice, = self.notifications("student")
            self.assertEqual(notice["payload"], {})
            self.assertTrue(notice["redacted"])

    def test_thread_message_and_notification_pagination_keep_array_contract(self):
        second_thread = self.create_thread("q2:1", "thread-2", title="未发布标题答案")
        with self.companion.connect() as db:
            db.execute("UPDATE discussion_threads SET updated_at=1 WHERE id=?", (self.thread["id"],))
            db.execute("UPDATE discussion_threads SET updated_at=2 WHERE id=?", (second_thread["id"],))
        self.current = "student"
        url = "/api/v1/companion/discussion-threads"
        first = self.client.get(url, params={"class_id": "class", "task_id": "task", "limit": 1}).json()
        second = self.client.get(url, params={"class_id": "class", "task_id": "task", "limit": 1, "offset": 1}).json()
        self.assertIsInstance(first, list)
        self.assertEqual(first[0]["id"], second_thread["id"])
        self.assertEqual(first[0]["title"], "教师短评")
        self.assertEqual(second[0]["id"], self.thread["id"])
        self.assertEqual(self.client.get(url, params={"offset": -1}).status_code, 422)
        messages = [self.post("owner", f"comment-{index}", f"短评{index}") for index in range(3)]
        notices = self.notifications("student")
        with self.companion.connect() as db:
            for index, message in enumerate(messages):
                db.execute("UPDATE notifications SET created_at=? WHERE business_key=?",
                           (index + 1, f"discussion:{message['id']}:student"))
        page = self.client.get(self.messages_url, params={"limit": 1, "offset": 1}).json()
        self.assertEqual([row["id"] for row in page], [messages[1]["id"]])
        self.assertTrue(page[0]["redacted"])
        middle, = self.notifications("student", limit=1, offset=1)
        self.assertEqual(middle["payload"]["message_id"], messages[1]["id"])
        self.client.post(f"/api/v1/me/notifications/{middle['id']}/read")
        unread, = self.notifications("student", unread_only=True, limit=1, offset=1)
        self.assertEqual(unread["payload"]["message_id"], messages[0]["id"])
        self.assertEqual(len(notices), 3)

    def test_notification_projection_rebuilds_locators_and_never_trusts_cached_free_text(self):
        message = self.post("owner", "comment", "当前不可见")
        notice, = self.notifications("student")
        with self.companion.connect() as db:
            payload = {**notice["payload"], "text": "旧结果开放时的答案", "title": "不能泄漏", "task_id": "forged"}
            db.execute("UPDATE notifications SET payload_json=? WHERE id=?", (json.dumps(payload), notice["id"]))
        projected, = self.notifications("student")
        self.assert_locator(projected, message)
        self.assertTrue(projected["redacted"])
        self.assertNotIn("text", projected["payload"])
        self.assertNotIn("title", projected["payload"])


if __name__ == "__main__":
    unittest.main()
