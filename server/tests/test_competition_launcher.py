"""Competition launch isolates configuration and never exposes credentials in readiness reports."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.competition_launcher import apply_config, main, read_config, setup
from app.config import LLMProvider, _llm_providers


class CompetitionLauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="axis-competition-launch-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = {"schema_version": 2, "base_url": "https://relay.example/v1",
                       "teacher_key_env": "POKE_TEACHER_API_KEY", "student_key_env": "POKE_STUDENT_API_KEY",
                       "data_dir": str(self.root / "data")}
        self.credentials = {self.config["teacher_key_env"]: secrets.token_urlsafe(32),
                            self.config["student_key_env"]: secrets.token_urlsafe(32)}

    def subprocess_env(self):
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(("LLM_", "MAXKB_", "POKE_")) and name != "SKIP_ENV_FILE"}
        env.update(self.credentials)
        env["SKIP_ENV_FILE"] = "1"
        return env

    def test_saved_config_check_uses_four_routes_without_prices_network_or_data_writes(self):
        path = self.root / "competition.local.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        env = self.subprocess_env()
        env["LLM_PROVIDERS_JSON"] = "invalid inherited value"
        result = subprocess.run([sys.executable, "-m", "app.competition_launcher", "--config", str(path), "--check"],
                                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "configuration_valid")
        self.assertEqual(report["billing_mode"], "unmetered_competition")
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["database_writes"], 0)
        self.assertFalse((self.root / "data").exists())
        for key in self.credentials.values():
            self.assertNotIn(key, result.stdout + result.stderr)

    def test_setup_preserves_existing_file_and_never_prompts_over_it(self):
        path = self.root / "competition.local.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        original = path.read_bytes()
        with patch("app.competition_launcher.getpass.getpass", side_effect=AssertionError("must not prompt")):
            with self.assertRaises(ValueError):
                setup(path, base_url=self.config["base_url"], data_dir=self.root / "other")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(read_config(path), self.config)

    def test_apply_configuration_drops_inherited_provider_and_price_settings(self):
        with patch.dict(os.environ, {**self.credentials, "LLM_API_KEY": secrets.token_urlsafe(32), "LLM_PRICES_JSON": "inherited",
                                    "MAXKB_API_KEY": secrets.token_urlsafe(32), "POKE_UNRELATED_KEY": secrets.token_urlsafe(32)}, clear=True):
            self.assertEqual(apply_config(self.config), self.root / "data")
            self.assertNotIn("LLM_API_KEY", os.environ)
            self.assertNotIn("LLM_PRICES_JSON", os.environ)
            self.assertNotIn("MAXKB_API_KEY", os.environ)
            self.assertNotIn("POKE_UNRELATED_KEY", os.environ)
            self.assertEqual(os.environ["POKE_TEACHER_API_KEY"], self.credentials["POKE_TEACHER_API_KEY"])
            self.assertEqual(os.environ["POKE_STUDENT_API_KEY"], self.credentials["POKE_STUDENT_API_KEY"])
            self.assertEqual(os.environ["STUDY_COMPETITION_UNMETERED"], "true")
            providers = json.loads(os.environ["LLM_PROVIDERS_JSON"])
            self.assertEqual({p["channel"] for p in providers}, {"teacher", "student", "grading", "summary"})
            self.assertTrue(all("api_key" not in p for p in providers))


    def test_responses_endpoint_infers_protocol_for_all_four_providers(self):
        value = {**self.config, "base_url": "https://www.poke2api.com/v1/responses"}
        path = self.root / "responses.local.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(read_config(path), value)
        with patch.dict(os.environ, self.credentials, clear=True):
            apply_config(read_config(path))
            providers = _llm_providers()
            self.assertEqual(len(providers), 4)
            self.assertTrue(all(provider.api_type == "responses" for provider in providers))
            self.assertTrue(all(provider.base_url == value["base_url"] for provider in providers))
            teacher = next(provider for provider in providers if provider.channel == "teacher")
            self.assertEqual(teacher.api_key, self.credentials[value["teacher_key_env"]])
            self.assertTrue(all(provider.api_key == self.credentials[value["student_key_env"]] for provider in providers if provider.channel != "teacher"))

    def test_chat_default_and_explicit_protocol_are_compatible(self):
        cases = (
            (self.config, "chat_completions"),
            ({**self.config, "api_type": "chat_completions"}, "chat_completions"),
            ({**self.config, "api_type": "responses"}, "responses"),
            ({**self.config, "base_url": "https://relay.example/v1/responses/"}, "responses"),
        )
        for index, (value, expected) in enumerate(cases):
            with self.subTest(expected=expected, value=index), patch.dict(os.environ, self.credentials, clear=True):
                path = self.root / f"protocol-{index}.local.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                apply_config(read_config(path))
                self.assertTrue(all(provider.api_type == expected for provider in _llm_providers()))

    def test_invalid_or_conflicting_protocol_rejected_before_environment_changes(self):
        cases = (
            {"api_type": "invalid"}, {"api_type": None}, {"api_type": []},
            {"base_url": "https://relay.example/v1/responses", "api_type": "chat_completions"},
            {"base_url": "https://relay.example/v1/chat/completions", "api_type": "responses"},
        )
        for index, changes in enumerate(cases):
            value = {**self.config, **changes}
            path = self.root / f"invalid-{index}.local.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    read_config(path)
                with patch.dict(os.environ, {"LLM_MODEL": "unchanged"}, clear=True):
                    with self.assertRaises(ValueError):
                        apply_config(value)
                    self.assertEqual(dict(os.environ), {"LLM_MODEL": "unchanged"})

    def test_responses_offline_check_preserves_endpoint_without_network_or_data_writes(self):
        path = self.root / "responses.local.json"
        value = {**self.config, "base_url": "https://www.poke2api.com/v1/responses"}
        path.write_text(json.dumps(value), encoding="utf-8")
        env = self.subprocess_env()
        result = subprocess.run([sys.executable, "-m", "app.competition_launcher", "--config", str(path), "--check"],
                                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["network_requests"], report["database_writes"]), ("configuration_valid", 0, 0))
        self.assertFalse((self.root / "data").exists())
        for key in self.credentials.values():
            self.assertNotIn(key, result.stdout + result.stderr)


    def test_explicit_env_values_are_read_before_prefixed_variables_are_cleared(self):
        value = {**self.config, "teacher_key_env": "POKE_SELECTED_TEACHER", "student_key_env": "LLM_SELECTED_STUDENT"}
        teacher_key, student_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with patch.dict(os.environ, {**self.credentials, "POKE_SELECTED_TEACHER": teacher_key,
                                    "LLM_SELECTED_STUDENT": student_key, "MAXKB_API_KEY": secrets.token_urlsafe(32)}, clear=True):
            apply_config(value)
            self.assertNotIn("POKE_SELECTED_TEACHER", os.environ)
            self.assertNotIn("LLM_SELECTED_STUDENT", os.environ)
            self.assertNotIn("MAXKB_API_KEY", os.environ)
            providers = _llm_providers()
            self.assertEqual(len(providers), 4)
            for provider in providers:
                self.assertEqual(provider.api_key, teacher_key if provider.channel == "teacher" else student_key)
            serialized = os.environ["LLM_PROVIDERS_JSON"]
            self.assertNotIn(teacher_key, serialized)
            self.assertNotIn(student_key, serialized)

    def test_missing_or_invalid_env_never_falls_back_or_mutates_configuration(self):
        for missing in ("teacher_key_env", "student_key_env"):
            for invalid in (None, "", "\n"):
                env = {**self.credentials, "LLM_API_KEY": secrets.token_urlsafe(32)}
                name = self.config[missing]
                if invalid is None:
                    env.pop(name)
                else:
                    env[name] = invalid
                with self.subTest(missing=missing, invalid=invalid), patch.dict(os.environ, env, clear=True):
                    original = dict(os.environ)
                    with self.assertRaises(ValueError) as caught:
                        apply_config(self.config)
                    self.assertIn("环境变量", str(caught.exception))
                    self.assertEqual(dict(os.environ), original)
                    for key in self.credentials.values():
                        self.assertNotIn(key, str(caught.exception))
        with patch.dict(os.environ, self.credentials, clear=True):
            value = {**self.config, "teacher_key_env": "POKE_MISSING_TEACHER",
                     "teacher_key": secrets.token_urlsafe(32), "student_key": secrets.token_urlsafe(32)}
            with self.assertRaises(ValueError):
                apply_config(value)

    def test_in_memory_key_entry_remains_supported_without_writing_a_file(self):
        teacher_key, student_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        value = {"schema_version": 1, "base_url": "https://relay.example/v1/responses", "data_dir": str(self.root / "data"),
                 "teacher_key": teacher_key, "student_key": student_key}
        with patch.dict(os.environ, {}, clear=True):
            apply_config(value)
            providers = _llm_providers()
            self.assertEqual(len(providers), 4)
            self.assertTrue(all(provider.api_type == "responses" for provider in providers))
            for provider in providers:
                self.assertEqual(provider.api_key, teacher_key if provider.channel == "teacher" else student_key)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_setup_writes_only_env_references_without_reading_credentials_or_prompting(self):
        path = self.root / "references.local.json"
        output = StringIO()
        with patch.dict(os.environ, self.credentials, clear=True), redirect_stdout(output), patch(
            "app.competition_launcher.getpass.getpass", side_effect=AssertionError("must not prompt")
        ), patch("app.competition_launcher.os.environ.get", side_effect=AssertionError("must not read credentials")):
            setup(path, base_url="https://www.poke2api.com/v1/responses", data_dir=self.root / "data")
        value = read_config(path)
        self.assertEqual(value, {**self.config, "base_url": "https://www.poke2api.com/v1/responses"})
        serialized = path.read_text(encoding="utf-8")
        self.assertNotIn('"teacher_key"', serialized)
        self.assertNotIn('"student_key"', serialized)
        for key in self.credentials.values():
            self.assertNotIn(key, serialized + output.getvalue())
        self.assertFalse((self.root / "data").exists())

    def test_cli_setup_defaults_to_full_responses_endpoint_without_tty(self):
        path = self.root / "cli.local.json"
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(StringIO()), patch(
            "app.competition_launcher.getpass.getpass", side_effect=AssertionError("must not prompt")
        ), patch("app.competition_launcher.sys.stdin.isatty", return_value=False):
            self.assertEqual(main(["--setup", "--config", str(path), "--data-dir", str(self.root / "data")]), 0)
        value = read_config(path)
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["base_url"], "https://www.poke2api.com/v1/responses")
        self.assertEqual((value["teacher_key_env"], value["student_key_env"]), ("POKE_TEACHER_API_KEY", "POKE_STUDENT_API_KEY"))

    def test_cli_setup_accepts_custom_env_names_and_rejects_invalid_names_without_writes(self):
        path = self.root / "custom.local.json"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["--setup", "--config", str(path), "--teacher-key-env", "COMPETITION_TEACHER",
                                   "--student-key-env", "COMPETITION_STUDENT"]), 0)
        value = read_config(path)
        self.assertEqual((value["teacher_key_env"], value["student_key_env"]), ("COMPETITION_TEACHER", "COMPETITION_STUDENT"))
        for invalid in ("", " key", "invalid-name", "x=y", "密钥", None):
            invalid_path = self.root / "invalid-env.local.json"
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                setup(invalid_path, base_url=self.config["base_url"], data_dir=self.root / "data", teacher_key_env=invalid)
            self.assertFalse(invalid_path.exists())

    def test_old_plaintext_config_is_rejected_with_migration_message_and_never_overwritten(self):
        value = {"schema_version": 1, "base_url": self.config["base_url"], "data_dir": self.config["data_dir"],
                 "teacher_key": secrets.token_urlsafe(32), "student_key": secrets.token_urlsafe(32)}
        path = self.root / "legacy.local.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        original = path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            read_config(path)
        self.assertIn("schema_version=2", str(caught.exception))
        self.assertIn("未自动覆盖", str(caught.exception))
        output = StringIO()
        with redirect_stdout(output), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["--check", "--config", str(path)]), 1)
        self.assertIn("teacher_key_env/student_key_env", output.getvalue())
        self.assertEqual(path.read_bytes(), original)
        for key in (value["teacher_key"], value["student_key"]):
            self.assertNotIn(key, str(caught.exception) + output.getvalue())

    def test_new_schema_requires_env_names_and_cli_missing_env_is_sanitized(self):
        path = self.root / "required.local.json"
        for changes in ({"schema_version": 1}, {"teacher_key_env": "invalid-name"}, {"student_key_env": None}, {"api_key": secrets.token_urlsafe(32)}):
            path.write_text(json.dumps({**self.config, **changes}), encoding="utf-8")
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                read_config(path)
        path.write_text(json.dumps(self.config), encoding="utf-8")
        output = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            self.assertEqual(main(["--check", "--config", str(path)]), 1)
        self.assertIn("环境变量缺失或无效", output.getvalue())
        self.assertFalse((self.root / "data").exists())


class ProviderProtocolConfigTests(unittest.TestCase):
    def test_default_field_preserves_positional_constructor_and_old_provider_json(self):
        key = secrets.token_urlsafe(32)
        provider = LLMProvider("fixture", "teacher", "https://relay.example/v1", key, "gpt-5.6-sol", 2, 4, 10, "fixture", 0, 0)
        self.assertEqual(provider.api_type, "chat_completions")
        self.assertNotIn(key, repr(provider))
        entry = {"name": "fixture", "channel": "teacher", "base_url": "https://relay.example/v1",
                 "model": "gpt-5.6-sol", "api_key_env": "PROTOCOL_FIXTURE_KEY"}
        with patch.dict(os.environ, {"PROTOCOL_FIXTURE_KEY": key, "LLM_PROVIDERS_JSON": json.dumps([entry])}, clear=True):
            self.assertEqual(_llm_providers()[0].api_type, "chat_completions")

    def test_provider_json_validates_protocol_without_echoing_credentials(self):
        entry = {"name": "fixture", "channel": "teacher", "base_url": "https://relay.example/v1/responses",
                 "model": "gpt-5.6-sol", "api_key_env": "PROTOCOL_FIXTURE_KEY"}
        key = secrets.token_urlsafe(32)
        for protocol in ("responses", "chat_completions", "other", "RESPONSES", " responses ", None, [], {}):
            with self.subTest(protocol=protocol), patch.dict(os.environ, {
                "PROTOCOL_FIXTURE_KEY": key, "LLM_PROVIDERS_JSON": json.dumps([{**entry, "api_type": protocol}]),
            }, clear=True):
                if protocol in ("responses", "chat_completions"):
                    self.assertEqual(_llm_providers()[0].api_type, protocol)
                else:
                    with self.assertRaises(ValueError) as caught:
                        _llm_providers()
                    self.assertNotIn(key, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
