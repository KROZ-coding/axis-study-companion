"""Offline readiness reports must be useful without touching live state."""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import secrets
import socket
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import LLMProvider, Settings
from app.deployment_check import check_settings, main


class DeploymentCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="axis-deployment-check-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = Settings(
            app_env="production", demo_enabled=False, worker_enabled=True, worker_concurrency=2,
            cookie_secure=True, allowed_hosts=("study.example.org",), cors_origins=(),
            data_dir=str(self.root / "data"), llm_providers=(), llm_prices_json="",
            llm_reasoning_effort="high", budget_currency="fixture-unit",
        )
        self.key = secrets.token_urlsafe(32)
        channels = ("teacher", "student", "grading", "summary")
        self.providers = tuple(LLMProvider(
            name="fixture-" + channel, channel=channel, base_url="https://relay.example/v1", api_key=self.key,
            model="gpt-5.6-sol" if channel in {"teacher", "student"} else "gpt-5.6-terra",
            max_concurrency=2, queue_size=4, priority=1, quota_group=channel,
        ) for channel in channels)
        self.prices = [{"provider": provider.name, "model": provider.model, "version": "fixture-v1",
                        "currency": "fixture-unit", "input_units_per_million": 10,
                        "output_units_per_million": 20} for provider in self.providers]

    def configured(self, **changes):
        return replace(self.config, llm_providers=self.providers, llm_prices_json=json.dumps(self.prices), **changes)

    def failed(self, report):
        return {row["code"] for row in report["checks"] if row["status"] == "fail"}

    def test_valid_configuration_is_offline_and_never_creates_data_or_prints_key(self):
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS")), \
                patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("no database")):
            report = check_settings(self.configured(), production=True, require_models=True)
        self.assertEqual(report["status"], "configuration_valid")
        self.assertEqual(self.failed(report), set())
        self.assertFalse((self.root / "data").exists())
        self.assertNotIn(self.key, json.dumps(report))
        pending = {row["code"] for row in report["checks"] if row["status"] == "pending"}
        self.assertEqual(pending, {"budget_limits", "external_acceptance"})

    def test_unconfigured_paid_calls_are_pending_until_real_model_profile_is_requested(self):
        local = check_settings(self.config, production=True)
        self.assertEqual(local["status"], "configuration_valid")
        strict = check_settings(self.config, production=True, require_models=True)
        self.assertEqual(strict["status"], "blocked")
        self.assertTrue({"grouped_models", "teacher_route", "student_route", "grading_route", "summary_route"}
                        <= self.failed(strict))

    def test_missing_channel_wrong_model_and_missing_key_block_real_model_configuration(self):
        cases = (self.providers[:-1],
                 (replace(self.providers[0], model="gpt-5.6-terra"), *self.providers[1:]),
                 (replace(self.providers[0], api_key=""), *self.providers[1:]))
        for providers in cases:
            with self.subTest(providers=len(providers)):
                report = check_settings(replace(self.configured(), llm_providers=providers), require_models=True)
                self.assertEqual(report["status"], "blocked")
                self.assertTrue(any(code.endswith("_route") for code in self.failed(report)))

    def test_every_candidate_needs_an_https_url_and_matching_versioned_price(self):
        urls = ("http://relay.example/v1", "https://127.0.0.1/v1", "https://relay.example/v1?key=secret",
                "https://user:secret@relay.example/v1", "https://localhost/v1")
        for url in urls:
            with self.subTest(url=url):
                providers = (replace(self.providers[0], base_url=url), *self.providers[1:])
                report = check_settings(replace(self.configured(), llm_providers=providers), require_models=True)
                self.assertIn("teacher_1_url", self.failed(report))
                self.assertNotIn("secret", json.dumps(report))
        candidate = replace(self.providers[0], name="unpriced-fallback", priority=2)
        report = check_settings(replace(self.configured(), llm_providers=(*self.providers, candidate)))
        self.assertIn("teacher_2_price", self.failed(report))
        wrong_currency = [{**row, "currency": "other-unit"} for row in self.prices]
        report = check_settings(replace(self.configured(), llm_prices_json=json.dumps(wrong_currency)))
        self.assertIn("grading_1_price", self.failed(report))

    def test_invalid_prices_limits_and_production_transport_are_blocked(self):
        cases = (({"llm_prices_json": "[invalid]"}, "price_configuration"),
                 ({"llm_batch_max_tokens": 0}, "grading_route"),
                 ({"cookie_secure": False}, "secure_cookie"),
                 ({"allowed_hosts": ("*",)}, "explicit_hosts"),
                 ({"cors_origins": ("http://study.example.org",)}, "same_origin_configuration"),
                 ({"data_dir": "./data"}, "external_data_directory"),
                 ({"app_env": "development"}, "production_environment"),
                 ({"demo_enabled": True}, "formal_mode"),
                 ({"worker_enabled": False}, "worker_enabled"))
        for changes, code in cases:
            with self.subTest(code=code):
                report = check_settings(replace(self.configured(), **changes), production=True, require_models=True)
                self.assertIn(code, self.failed(report))

    def test_cli_exit_status_matches_report_and_parse_failure_is_sanitized(self):
        for config, expected in ((self.config, 0), (replace(self.config, cookie_secure=False), 1)):
            output = StringIO()
            with patch("app.config.Settings", return_value=config), redirect_stdout(output):
                status = main(["--profile", "production"])
            report = json.loads(output.getvalue())
            self.assertEqual(status, expected)
            self.assertEqual(report["status"], "configuration_valid" if expected == 0 else "blocked")
        output = StringIO()
        with patch("app.config.Settings", side_effect=ValueError(self.key)), redirect_stdout(output):
            status = main(["--profile", "production", "--require-models"])
        self.assertEqual(status, 1)
        self.assertNotIn(self.key, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["checks"][0]["code"], "configuration_invalid")


if __name__ == "__main__":
    unittest.main()
