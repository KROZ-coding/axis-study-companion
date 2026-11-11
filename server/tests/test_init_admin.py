import atexit
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="admin-init-import-")
atexit.register(_IMPORT_DATA.cleanup)
os.environ.update(SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=_IMPORT_DATA.name, PERSISTENCE_ENABLED="false",
                  LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="")

from app.config import Settings
from app.init_admin import initialize_admin, main
from app.platform_store import PlatformStore, resolve_platform_db_path
from app.utils.security import hash_password


class InitialAdminTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="admin-init-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "platform.sqlite3"
        self.config = Settings(app_env="test", data_dir=str(self.root), persistence_enabled=False, llm_providers=())

    def test_first_admin_is_explicit_and_only_created_once(self):
        def unlocked_hash(password):
            # No database is even created until expensive password hashing finishes.
            self.assertFalse(self.path.exists())
            return hash_password(password)
        with patch("app.init_admin.hash_password", side_effect=unlocked_hash):
            user_id = initialize_admin(settings=self.config, username="first-admin", display_name="管理员", password="runtime-secret")
        self.assertEqual(resolve_platform_db_path(self.config), self.path)
        store = PlatformStore(self.path)
        self.assertEqual((store.get_user(user_id)["role"], store.get_user(user_id)["status"]), ("admin", "active"))
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            initialize_admin(settings=self.config, username="second-admin", display_name="Admin", password="another-secret")
        self.assertEqual(len(store.list_users(role="admin")), 1)
        self.assertEqual(store.list_audit_events()[0]["event_type"], "initial_admin_created")

    def test_programmatic_and_cli_validation_match_and_do_not_create_database(self):
        invalid = (
            {"username": "x", "display_name": "Admin", "password": "valid-long-password"},
            {"username": "bad name", "display_name": "Admin", "password": "valid-long-password"},
            {"username": "admin", "display_name": "  ", "password": "valid-long-password"},
            {"username": "admin", "display_name": "Admin", "password": "short"},
            {"username": "admin", "display_name": "Admin", "password": " " * 16},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    initialize_admin(self.path, **fields)
                self.assertFalse(self.path.exists())
        with patch("sys.argv", ["init_admin", "--db", str(self.path), "--username", "admin", "--display-name", "Admin"]), patch(
            "app.init_admin.getpass.getpass", side_effect=["short", "short"],
        ), patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main()
        self.assertFalse(self.path.exists())

    def test_admin_and_audit_are_atomic(self):
        with patch.object(PlatformStore, "record_audit_event", side_effect=OSError("audit unavailable")):
            with self.assertRaises(OSError):
                initialize_admin(self.path, username="admin", display_name="Admin", password="runtime-secret")
        store = PlatformStore(self.path)
        self.assertEqual(store.list_users(), [])
        self.assertEqual(store.list_audit_events(), [])


if __name__ == "__main__":
    unittest.main()
