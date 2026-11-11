"""Dependencies shared by the authenticated platform routes."""

from threading import RLock

from fastapi import Depends, HTTPException, Request

from .models import User
from .platform_store import PlatformStore, resolve_platform_db_path

_STORE_INIT_LOCK = RLock()


def get_platform_store(request: Request) -> PlatformStore:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        store = runtime.platform
        request.app.state.platform_store = store
        request.app.state.platform_store_path = store.db_path
        return store
    with _STORE_INIT_LOCK:
        store = getattr(request.app.state, "platform_store", None)
        if store is None:
            path = resolve_platform_db_path(request.app.state.settings)
            store = PlatformStore(path)
            request.app.state.platform_store = store
            request.app.state.platform_store_path = path
        return store


def platform_user(row: dict) -> User:
    return User(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        password_hash="", role=row["role"], status=row["status"],
        phone=row.get("phone", ""), student_number=row.get("student_number", ""),
    )


def get_platform_user(request: Request, store: PlatformStore = Depends(get_platform_store)) -> User:
    session = store.get_session(request.cookies.get("sid", ""))
    row = store.get_user(session["user_id"]) if session else None
    if row is None or row["status"] != "active":
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "请先登录"})
    return platform_user(row)


def require_platform_roles(*roles: str):
    def dependency(user: User = Depends(get_platform_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "当前角色无权执行此操作"})
        return user
    return dependency


def require_current_actor(store: PlatformStore, actor_id: str, *roles: str) -> dict:
    """Revalidate identity inside the mutation's transaction, after slow work."""
    row = store.get_user(actor_id)
    if row is None or row["status"] != "active":
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "请先登录"})
    if roles and row["role"] not in roles:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "当前角色无权执行此操作"})
    return row
