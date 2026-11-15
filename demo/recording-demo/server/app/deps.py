from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from .models import User
from .models.study import DemoContext
from .store import Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_current_user(request: Request, store: Store = Depends(get_store)) -> User:
    sid = request.cookies.get("sid")
    session = store.get_session(sid or "")
    user = store.users.get(session[0] if session else "")
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "unauthorized", "message": "请先登录"})
    return user


def require_roles(*roles: str) -> Callable:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "forbidden", "message": "当前角色无权执行此操作"})
        return user

    return dependency


def get_study_data(request: Request) -> dict:
    return request.app.state.study_data


def get_demo_context(request: Request, data: dict = Depends(get_study_data)) -> DemoContext:
    role = request.headers.get("X-Demo-Role", "")
    if role not in {"teacher", "student"}:
        raise HTTPException(status_code=401, detail="请明确选择演示教师或演示学生身份")
    class_id = request.headers.get("X-Demo-Class-Id", request.app.state.study_class_id)
    if class_id != request.app.state.study_class_id:
        raise HTTPException(status_code=403, detail="当前演示身份不能访问该班级")
    student_id = request.headers.get("X-Demo-Student-Id")
    if role == "student":
        if student_id not in {row["id"] for row in data["students"]}:
            raise HTTPException(status_code=403, detail="请选择当前班级中的演示学生")
        return DemoContext(role, f"demo-student:{student_id}", class_id, student_id)
    if student_id:
        raise HTTPException(status_code=422, detail="演示教师身份不接受学生身份头；请通过任务预览查看指定学生")
    return DemoContext(role, "demo-teacher", class_id)


def require_demo_teacher(actor: DemoContext = Depends(get_demo_context)) -> DemoContext:
    if actor.role != "teacher":
        raise HTTPException(status_code=403, detail="该操作需要演示教师身份")
    return actor


def require_demo_student(actor: DemoContext = Depends(get_demo_context)) -> DemoContext:
    if actor.role != "student":
        raise HTTPException(status_code=403, detail="该操作需要演示学生身份")
    return actor
