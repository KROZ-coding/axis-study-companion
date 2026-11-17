"""Current companion access, answer gates and authoritative billing scope."""

from __future__ import annotations

from typing import Any, Mapping


def require_student(runtime, user_id: str) -> None:
    user = runtime.platform.get_user(user_id)
    if user is None or user.get("status") != "active" or user.get("role") != "student":
        raise PermissionError("student account is inactive")


def question_matches(question: Mapping[str, Any], reference: str) -> bool:
    if question.get("question_version_id") == reference:
        return True
    question_id = question.get("id", question.get("question_id"))
    version = question.get("version")
    return question_id is not None and version is not None and reference in {
        f"{question_id}:{version}", f"{question_id}@{version}",
    }


def task_scope(runtime, user_id: str, task_id: str,
               question_version_id: str | None = None) -> dict[str, Any]:
    require_student(runtime, user_id)
    scope = runtime.learning.get_task_scope(task_id, user_id)
    if not isinstance(scope, Mapping) or scope.get("student_id") != user_id:
        raise PermissionError("task is not assigned to student")
    if scope.get("task_id", task_id) != task_id:
        raise PermissionError("task binding changed")
    course_id, class_id = scope.get("course_id"), scope.get("class_id")
    if class_id and not course_id:
        raise PermissionError("task class has no course")
    if course_id and not runtime.platform.can_access_course(user_id, course_id):
        raise PermissionError("task course is inaccessible")
    if class_id:
        classroom = runtime.platform.get_class(class_id)
        membership = runtime.platform.get_class_membership(class_id, user_id)
        if (classroom is None or classroom.get("course_id") != course_id
                or membership is None or membership.get("status", "active") != "active"):
            raise PermissionError("task class is inaccessible")
    if question_version_id is not None:
        task = runtime.learning.get_task(task_id, user_id)
        questions = task.get("questions") if isinstance(task, Mapping) else None
        if not isinstance(questions, list) or not any(
            isinstance(question, Mapping) and question_matches(question, question_version_id)
            for question in questions
        ):
            raise PermissionError("question version is not assigned to student")
    return {**scope, "task_id": task_id}


def answer_policy(runtime, user_id: str, *, task_id: str | None = None,
                  question_version_id: str | None = None) -> dict[str, Any]:
    """Use both the selected task and all active tasks, and fail closed."""
    require_student(runtime, user_id)
    if question_version_id is not None and task_id is None:
        raise PermissionError("question version requires an assigned task")
    if task_id:
        task_scope(runtime, user_id, task_id, question_version_id)
        scoped = runtime.learning.get_answer_policy(
            user_id, task_id=task_id, question_version_id=question_version_id,
        )
        global_policy = runtime.learning.get_answer_policy(user_id)
    else:
        scoped = global_policy = runtime.learning.get_answer_policy(user_id)
    if (not isinstance(scoped, Mapping) or not isinstance(global_policy, Mapping)
            or type(scoped.get("allow_assistant_content")) is not bool
            or type(global_policy.get("allow_assistant_content")) is not bool
            or type(scoped.get("allow_generation")) is not bool
            or type(global_policy.get("allow_generation")) is not bool):
        raise PermissionError("learning policy is unavailable")
    diagnostic = (scoped.get("diagnostic") is True or global_policy.get("diagnostic") is True
                  or scoped.get("phase") in {"diagnostic", "first_diagnostic"}
                  or global_policy.get("phase") in {"diagnostic", "first_diagnostic"})
    attempted = scoped.get("attempted") is True if task_id and question_version_id else (
        scoped.get("attempted") is True and global_policy.get("attempted") is True)
    allowed = scoped.get("allow_generation") is True and global_policy.get("allow_generation") is True and not diagnostic
    hint_content = bool(task_id and question_version_id and not diagnostic and scoped.get("allow_hint_content") is True)
    hint_generation = hint_content and scoped.get("allow_hint_generation") is True
    delivered = {}
    # All public hint projections consume this evidence map, including history,
    # message references, context and SSE. Keep durable delivery/billing facts;
    # cancellation only removes the evidence from the current read permission.
    for response_id, evidence in (scoped.get("delivered_hint_evidence", {}) if hint_content else {}).items():
        response = runtime.companion.get_response(response_id)
        job = response_job_state(runtime, response)
        if job and (job["cancel_requested"] or job["status"] == "cancelled"):
            continue
        delivered[response_id] = evidence
    return {
        **scoped,
        "allow_generation": allowed,
        "allow_assistant_content": (scoped["allow_assistant_content"]
                                    and global_policy["allow_assistant_content"] and not diagnostic),
        "allow_hint_generation": hint_generation, "allow_hint_content": hint_content,
        "allowed_hint_levels": scoped.get("allowed_hint_levels", []) if hint_content else [],
        "hint_binding": scoped.get("hint_binding") if hint_content else None,
        "delivered_hint_evidence": delivered,
        "diagnostic": diagnostic,
        "attempted": attempted,
        "active_count": global_policy.get("active_count", 0),
        "guidance_key": "independent_attempt" if diagnostic else "try_first" if not attempted else "await_results",
        "reason": "freeform" if allowed else "diagnostic" if diagnostic else "attempt_required" if not attempted else "await_results",
        "help_recordable": hint_content,
    }


def actor_task_scope(runtime, user_id: str, task_id: str,
                     question_version_id: str | None = None) -> dict[str, Any]:
    """Validate a notification/task locator using the actor's current role."""
    user = runtime.platform.get_user(user_id)
    if user is None or user.get("status") != "active":
        raise PermissionError("account is inactive")
    if user.get("role") == "student":
        return task_scope(runtime, user_id, task_id, question_version_id)
    if user.get("role") != "teacher":
        raise PermissionError("explicit student-content access grant required")
    scope = runtime.learning.get_task_scope(task_id, user_id)
    if not isinstance(scope, Mapping) or scope.get("task_id", task_id) != task_id:
        raise PermissionError("task binding changed")
    course_id, class_id, student_id = scope.get("course_id"), scope.get("class_id"), scope.get("student_id")
    classroom = runtime.platform.get_class(class_id) if class_id else None
    membership = runtime.platform.get_class_membership(class_id, student_id) if class_id and student_id else None
    if (classroom is None or classroom.get("course_id") != course_id
            or membership is None or membership.get("status", "active") != "active"
            or not runtime.platform.can_access_class(user_id, class_id)):
        raise PermissionError("task class is inaccessible")
    if question_version_id is not None:
        task = runtime.learning.get_task(task_id, user_id)
        questions = task.get("questions") if isinstance(task, Mapping) else None
        if not isinstance(questions, list) or not any(
            isinstance(question, Mapping) and question_matches(question, question_version_id)
            for question in questions
        ):
            raise PermissionError("question version is not assigned to task")
    return {**scope, "task_id": task_id}


def actor_answer_policy(runtime, user_id: str, *, task_id: str | None = None,
                        question_version_id: str | None = None) -> dict[str, Any]:
    user = runtime.platform.get_user(user_id)
    if user is None or user.get("status") != "active":
        raise PermissionError("account is inactive")
    if user.get("role") == "student":
        return answer_policy(runtime, user_id, task_id=task_id, question_version_id=question_version_id)
    if not task_id:
        raise PermissionError("teacher notification requires a task")
    actor_task_scope(runtime, user_id, task_id, question_version_id)
    # Teachers read class-scoped evidence before release. This is not a student
    # generation permission and does not consult a nonexistent teacher task.
    return {"allow_assistant_content": True, "allow_generation": False, "task_id": task_id}


def discussion_teacher_recipients(runtime, class_id: str) -> list[str]:
    classroom = runtime.platform.get_class(class_id)
    if classroom is None:
        raise LookupError("discussion class not found")
    candidates = {classroom.get("owner_teacher_id")}
    candidates.update(row["teacher_id"] for row in runtime.platform.list_class_collaborators(class_id))
    recipients = []
    for candidate in sorted(value for value in candidates if value):
        user = runtime.platform.get_user(candidate)
        if (user is not None and user.get("role") == "teacher" and user.get("status") == "active"
                and runtime.platform.can_access_class(candidate, class_id, teaching=True)):
            recipients.append(candidate)
    return recipients


def answer_policy_provider(runtime, user_id: str):
    def policy(_user_id, *, task_id=None, question_version_id=None):
        if _user_id != user_id:
            raise PermissionError("policy actor changed")
        return answer_policy(runtime, user_id, task_id=task_id, question_version_id=question_version_id)
    return policy


def validate_response_access(runtime, user_id: str, response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate current grants without replacing a previously read snapshot."""
    require_student(runtime, user_id)
    if response.get("user_id") != user_id:
        raise PermissionError("response is not owned by student")
    conversation = runtime.companion.get_conversation(response["conversation_id"], user_id)
    conversation_course = conversation.get("course_id")
    if conversation_course and not runtime.platform.can_access_course(user_id, conversation_course):
        raise PermissionError("conversation course is inaccessible")
    scope = {"student_id": user_id, "project_id": "platform"}
    if response.get("task_id"):
        task = task_scope(runtime, user_id, response["task_id"], response.get("question_version_id"))
        if conversation_course is not None and task.get("course_id") != conversation_course:
            raise PermissionError("task is outside conversation course")
        scope.update({key: task[key] for key in ("course_id", "class_id", "task_id") if task.get(key)})
    elif response.get("question_version_id"):
        raise PermissionError("question version requires an assigned task")
    # A course-selected conversation alone must not consume a class allowance.
    # Free chat always uses the student's personal/global daily budget.
    return scope


def response_job_state(runtime, response: Mapping[str, Any]) -> dict[str, Any] | None:
    queue = getattr(runtime, "queue", None)
    if queue is None or not response.get("job_id"):
        return None
    try:
        job = queue.get_job(response["job_id"])
    except LookupError:
        # The response exists before its outbox is dispatched to the queue.
        return None
    return {"status": job.status, "cancel_requested": job.cancel_requested,
            "reconciliation_required": job.reconciliation_required,
            "cost_pending": job.cost_pending}


def response_access(runtime, user_id: str, response_id: str) -> dict[str, Any]:
    response = runtime.companion.get_response(response_id, user_id,
                                             answer_policy=answer_policy_provider(runtime, user_id))
    scope = validate_response_access(runtime, user_id, response)
    return {**response, "student_id": user_id, "class_id": scope.get("class_id"),
            "job": response_job_state(runtime, response)}


def response_event_access(runtime, user_id: str, response_id: str, event_id: int):
    response, event = runtime.companion.get_response_event(
        response_id, event_id, user_id, answer_policy=answer_policy_provider(runtime, user_id),
    )
    validate_response_access(runtime, user_id, response)
    response["job"] = response_job_state(runtime, response)
    return response, event


def response_events(runtime, user_id: str, response_id: str, *, after: int = 0, limit: int = 1000):
    snapshot, candidates = runtime.companion.read_response_events(
        response_id, user_id, after=after, limit=limit,
        answer_policy=answer_policy_provider(runtime, user_id),
    )
    validate_response_access(runtime, user_id, snapshot)
    events = []
    for candidate in candidates:
        current, event = response_event_access(runtime, user_id, response_id, candidate["event_id"])
        if current["generation"] != snapshot["generation"]:
            return []
        if event is not None:
            events.append(event)
    current = response_access(runtime, user_id, response_id)
    if current["generation"] != snapshot["generation"]:
        return []
    # A JSON batch is emitted as one document. A policy change while checking
    # its later items must also close content already added to that document.
    if current.get("redacted") or current["status"] in {"failed", "cancelled"}:
        for event in events:
            if event["event_type"] == "delta":
                event["data"] = {"text": "", "redacted": current.get("redacted", False),
                                 "notice": current.get("notice")}
            elif event["event_type"] == "end":
                event["data"] = {"status": current["status"], "redacted": current.get("redacted", False)}
    return events


def response_job_scope(runtime, response_id: str, user_id: str) -> dict[str, Any]:
    response = runtime.companion.get_response(response_id, user_id)
    return validate_response_access(runtime, user_id, response)


def freeze_response_job_scope(runtime, response_id: str, user_id: str) -> dict[str, Any]:
    scope = response_job_scope(runtime, response_id, user_id)
    return runtime.companion.freeze_response_budget_scope(response_id, user_id, scope)
