"""Explicit local creation of the first platform administrator."""

import argparse
import getpass
from pathlib import Path
import re
from typing import Any

from .platform_store import PlatformStore, resolve_platform_db_path
from .utils.security import hash_password


def validate_admin_input(*, username: str, display_name: str, password: str) -> tuple[str, str]:
    username, display_name = username.strip(), display_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("username must contain 3-32 letters, digits, dots, underscores or dashes")
    if not 1 <= len(display_name) <= 64:
        raise ValueError("display name must contain 1-64 non-whitespace characters")
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or not password.strip():
        raise ValueError("password must contain 12-128 characters and must not be blank")
    return username, display_name


def initialize_admin(
    db_path: str | Path | None = None, *, username: str, display_name: str, password: str,
    settings: Any = None,
) -> str:
    username, display_name = validate_admin_input(username=username, display_name=display_name, password=password)
    if db_path is None:
        if settings is None:
            from .config import Settings
            settings = Settings()
        db_path = resolve_platform_db_path(settings)
    password_hash = hash_password(password)
    store = PlatformStore(db_path)
    with store.transaction():
        if store.list_users(role="admin"):
            raise RuntimeError("an administrator already exists")
        user = store.create_user(username, display_name, password_hash, "admin", status="active")
        store.record_audit_event("initial_admin_created", user["id"], resource_type="user", resource_id=user["id"])
    return user["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first local platform administrator")
    parser.add_argument("--db", type=Path, default=None, help="Explicit database override; default uses configured DATA_DIR")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("New administrator password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if password != confirmation:
        parser.error("passwords do not match")
    try:
        user_id = initialize_admin(args.db, username=args.username, display_name=args.display_name, password=password)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Created administrator {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
