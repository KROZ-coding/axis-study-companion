import hashlib
import json
from copy import deepcopy

from ...models.study import DemoContext


def dataset_version(data: dict) -> str:
    source = {key: value for key, value in data.items() if key not in {"stats", "suggestions", "recommended"}}
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def describe_context(data: dict, actor: DemoContext) -> dict:
    student = next((row for row in data["students"] if row["id"] == actor.student_id), None)
    return {
        "mode": "local-demo", "authenticated": False,
        "actor": {"id": actor.actor_id, "role": actor.role,
                  "label": "演示教师" if actor.role == "teacher" else "演示学生",
                  "student": deepcopy(student)},
        "classroom": {"id": actor.class_id, "name": data["className"]},
        "scope": {"student_ids": [actor.student_id] if actor.student_id else [row["id"] for row in data["students"]],
                  "topics": deepcopy(data["topics"]), "as_of": data["asOf"]},
        "data_version": dataset_version(data), "sample_label": data["sampleLabel"],
    }
