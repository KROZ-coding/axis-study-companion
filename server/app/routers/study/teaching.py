import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...deps import (get_demo_context, get_store, get_study_data, require_demo_student,
                     require_demo_teacher)
from ...models.study import DemoContext
from ...schemas.study import (FeedbackCreate, FollowupCreate, FollowupUpdate, ReviewCreate,
                              TaskDraft, TaskResponse, TeachingContextResponse, VersionedRequest)
from ...services.study import content, insights
from ...services.study.context import describe_context
from ...services.study.workflow import TeachingService

router = APIRouter(prefix="/api/teaching", tags=["local-demo-teaching"])


def get_teaching_service(store=Depends(get_store), data=Depends(get_study_data), actor=Depends(get_demo_context)):
    return TeachingService(store, data, actor)


@router.get("/context", response_model=TeachingContextResponse)
def context(data=Depends(get_study_data), actor=Depends(get_demo_context)):
    return describe_context(data, actor)


@router.get("/content")
def learning_content(data=Depends(get_study_data), actor=Depends(get_demo_context)):
    scope = content.resolve_scope(data, student_ids=[actor.student_id] if actor.student_id else None)
    return {"topics": data["topics"], "knowledge_points": data["kps"],
            "materials": data["materials"], "scope": content.build_context(data, scope)}


@router.get("/materials/{material_id}")
def material(material_id: str, data=Depends(get_study_data), actor=Depends(get_demo_context)):
    return content.get_material(data, material_id)


@router.get("/questions/{question_id}")
def question(question_id: str, data=Depends(get_study_data), actor=Depends(get_demo_context)):
    return content.get_practice(data, question_id, include_answer=actor.role == "teacher")


@router.get("/insights")
def learning_insights(topic: str = "all", student_id: str | None = None, data=Depends(get_study_data),
                      actor: DemoContext = Depends(get_demo_context)):
    if actor.role == "student":
        if student_id is not None and student_id != actor.student_id:
            raise HTTPException(status_code=403, detail="演示学生只能查看自己的学习回顾")
        student_id = actor.student_id
    scope = content.resolve_scope(data, topic=topic, student_ids=[student_id] if student_id else None)
    return {"context": content.build_context(data, scope), **insights.build_insights(data, scope)}


@router.get("/dashboard", dependencies=[Depends(require_demo_teacher)])
def dashboard(service=Depends(get_teaching_service)):
    return service.dashboard()


@router.get("/reviews", dependencies=[Depends(require_demo_teacher)])
def list_reviews(service=Depends(get_teaching_service)):
    return {"reviews": service.list_reviews()}


@router.post("/reviews", status_code=201, dependencies=[Depends(require_demo_teacher)])
def create_review(payload: ReviewCreate, service=Depends(get_teaching_service)):
    return service.create_review(payload)


@router.get("/reviews/{review_id}", dependencies=[Depends(require_demo_teacher)])
def get_review(review_id: str, service=Depends(get_teaching_service)):
    return service.get_review(review_id)


@router.get("/followups", dependencies=[Depends(require_demo_teacher)])
def list_followups(service=Depends(get_teaching_service)):
    return {"followups": service.list_followups()}


@router.post("/followups", status_code=201, dependencies=[Depends(require_demo_teacher)])
def create_followup(payload: FollowupCreate, service=Depends(get_teaching_service)):
    return service.create_followup(payload)


@router.get("/followups/{followup_id}", dependencies=[Depends(require_demo_teacher)])
def get_followup(followup_id: str, service=Depends(get_teaching_service)):
    return service.get_followup(followup_id)


@router.put("/followups/{followup_id}", dependencies=[Depends(require_demo_teacher)])
def update_followup(followup_id: str, payload: FollowupUpdate, service=Depends(get_teaching_service)):
    return service.update_followup(followup_id, payload)


@router.post("/followups/{followup_id}/publish", dependencies=[Depends(require_demo_teacher)])
def publish_followup(followup_id: str, payload: VersionedRequest, service=Depends(get_teaching_service)):
    return service.publish_followup(followup_id, payload)


@router.post("/followups/{followup_id}/close", dependencies=[Depends(require_demo_teacher)])
def close_followup(followup_id: str, payload: VersionedRequest, service=Depends(get_teaching_service)):
    return service.close_followup(followup_id, payload)


@router.get("/followups/{followup_id}/preview/{student_id}", dependencies=[Depends(require_demo_teacher)])
def preview_task(followup_id: str, student_id: str, service=Depends(get_teaching_service)):
    return service.preview_task(followup_id, student_id)


@router.get("/tasks", dependencies=[Depends(require_demo_student)])
def list_tasks(service=Depends(get_teaching_service)):
    return {"tasks": service.list_tasks()}


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, service=Depends(get_teaching_service)):
    return service.get_task(task_id)


@router.post("/tasks/{task_id}/start", response_model=TaskResponse, dependencies=[Depends(require_demo_student)])
def start_task(task_id: str, payload: VersionedRequest, service=Depends(get_teaching_service)):
    return service.start_task(task_id, payload)


@router.put("/tasks/{task_id}/draft", response_model=TaskResponse, dependencies=[Depends(require_demo_student)])
def save_draft(task_id: str, payload: TaskDraft, service=Depends(get_teaching_service)):
    return service.save_draft(task_id, payload)


@router.post("/tasks/{task_id}/submit", response_model=TaskResponse, dependencies=[Depends(require_demo_student)])
def submit_task(task_id: str, payload: VersionedRequest, service=Depends(get_teaching_service)):
    return service.submit_task(task_id, payload)


@router.post("/tasks/{task_id}/feedback", response_model=TaskResponse, dependencies=[Depends(require_demo_teacher)])
def feedback(task_id: str, payload: FeedbackCreate, service=Depends(get_teaching_service)):
    return service.review_task(task_id, payload)


@router.get("/followups/{followup_id}/report.json", dependencies=[Depends(require_demo_teacher)])
def report_json(followup_id: str, service=Depends(get_teaching_service)):
    return Response(json.dumps(service.followup_report(followup_id), ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="teaching-followup.json"'})


@router.get("/followups/{followup_id}/report.csv", dependencies=[Depends(require_demo_teacher)])
def report_csv(followup_id: str, service=Depends(get_teaching_service)):
    report = service.followup_report(followup_id)
    fields = ["class_id", "review_id", "review_version", "data_version", "followup_id", "student_id", "task_id",
              "topic", "knowledge_point_id", "date_from", "date_to", "content_version", "evidence_ids",
              "status", "task_version", "attempt_count", "latest_attempt_id", "rule_score",
              "feedback_attempt_id", "feedback_decision", "feedback", "boundary"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for task in report["followup"]["tasks"]:
        attempt = task["attempts"][-1] if task["attempts"] else None
        feedback = next((item for item in reversed(task["feedback"])
                         if attempt and item["attempt_id"] == attempt["id"]), None)
        basis = next((item for item in report["historical_snapshot"]["learning_analysis"]
                      if item["student_id"] == task["student_id"]), {})
        row = {key: report[key] for key in ("class_id", "review_id", "review_version", "data_version", "boundary")}
        row.update(followup_id=followup_id, student_id=task["student_id"], task_id=task["id"], status=task["status"],
                   topic=report["scope"]["topic"], knowledge_point_id=report["scope"]["knowledge_point_ids"][0],
                   date_from=report["scope"]["date_from"] or "", date_to=report["scope"]["date_to"] or "",
                   content_version=task["content"]["content_version"], evidence_ids=basis.get("evidence_ids", ""),
                   task_version=task["version"], attempt_count=len(task["attempts"]),
                   latest_attempt_id=attempt["id"] if attempt else "",
                   rule_score=attempt["grading"]["total"] if attempt else "",
                   feedback_attempt_id=feedback["attempt_id"] if feedback else "",
                   feedback_decision=feedback["decision"] if feedback else "",
                   feedback=feedback["comment"] if feedback else "")
        writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                         for key, value in row.items()})
    return Response("\ufeff" + output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="teaching-followup.csv"'})
