import asyncio
import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

os.environ["SKIP_ENV_FILE"] = "1"

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.llm_client import LLMClient
from app.services.maxkb_client import MaxKBClient
from app.services.study_demo import SAMPLE, _status, student_report
from app.study_store import StudyStore
from app.utils.url_security import pin_remote_url, validate_remote_url


class StudyDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Settings(app_env="test", demo_enabled=True, worker_enabled=False, persistence_enabled=False, llm_providers=(), llm_base_url="", llm_api_key="", llm_model="")
        self.app = create_app(self.config)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_home_assets_health_and_no_audit_seed(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("/assets/bootstrap.js", home.text)
        for asset in ("app.js", "styles.css", "bootstrap.js"):
            self.assertEqual(self.client.get("/assets/" + asset).status_code, 200)
        self.assertEqual(self.client.get("/favicon.svg").status_code, 200)
        self.assertEqual(self.client.get("/health").json()["mode"], "offline-sample")
        self.assertEqual(self.app.state.store.courses, {})
        self.assertEqual(self.app.state.store.users, {})
        self.assertEqual(self.client.get("/assets/../server/.env").status_code, 404)

    def test_sample_statistics_reconcile_with_input_records(self) -> None:
        data = self.client.get("/api/study/sample").json()
        self.assertEqual(len(data["students"]), 50)
        self.assertEqual(data["rawCount"] - data["dupRemoved"], len(data["records"]))
        keys = {(row["sid"], row["item"], row["date"], row["correct"]) for row in data["records"]}
        self.assertEqual(len(keys), len(data["records"]))
        for sid, stats in data["stats"].items():
            for kp, stat in stats.items():
                records = [row for row in data["records"] if row["sid"] == sid and row["kp"] == kp]
                self.assertEqual(stat["n"], len(records))
                self.assertEqual(stat["right"], sum(row["correct"] for row in records))
                self.assertEqual(stat["status"], _status(len(records), stat["right"]))
        self.assertEqual(data["stats"]["S07"]["K5"]["right"], 2)
        self.assertEqual(data["stats"]["S07"]["K5"]["n"], 6)
        self.assertEqual(data["focus"]["S07"]["K4,K5"], student_report("S07", ["K4", "K5"]))

    def test_rule_thresholds_and_insufficient_records(self) -> None:
        for n, right, expected in ((0, 0, "none"), (2, 2, "none"), (4, 1, "weak"), (4, 2, "shaky"), (5, 4, "ok")):
            self.assertEqual(_status(n, right), expected)
        self.assertTrue(student_report("S07")["none"])
        self.assertEqual(self.client.get("/api/study/students/missing/report").status_code, 404)

    def test_mixed_mastered_and_missing_evidence_stays_insufficient(self) -> None:
        report = student_report("S07", ["K1", "K6"])
        self.assertIsNone(report["focus"])
        self.assertEqual({item["status"] for item in report["stats"]}, {"none", "ok"})
        self.assertEqual(report["overall"], "none")

    def test_recommendations_reference_existing_materials(self) -> None:
        materials = {row["id"]: row for row in SAMPLE["materials"]}
        for sid, suggestions in SAMPLE["suggestions"].items():
            for kp, material in suggestions.items():
                self.assertIn(material["id"], materials)
                self.assertEqual(material["kp"], kp)
                if SAMPLE["stats"][sid][kp]["topItem"]:
                    self.assertEqual(material["forItem"], SAMPLE["stats"][sid][kp]["topItem"])

    def test_scoped_exports_contain_three_tasks_and_traceable_evidence(self) -> None:
        result = self.client.get("/api/study/export.json", params={"student_id": "S07"}).json()
        self.assertEqual(len(result["students"]), 1)
        self.assertEqual(len(result["learning_contents"]), 8)
        self.assertEqual(len(result["learning_analysis"]), 8)
        self.assertEqual(len(result["learning_recommendations"]), 8)
        self.assertTrue(any(item["type"] == "小结" for item in result["materials"]))
        records = {row["id"] for row in result["records"]}
        materials = {row["id"] for row in result["materials"]}
        for row in result["learning_analysis"]:
            self.assertTrue(set(filter(None, row["evidence_ids"].split("|"))).issubset(records))
            self.assertIn(row["material_id"], materials)
        csv_result = self.client.get("/api/study/export.csv", params={"topic": "T2"})
        self.assertTrue(csv_result.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(csv_result.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 100)
        self.assertEqual({row["knowledge_point_id"] for row in rows}, {"K4", "K5"})
        self.assertEqual(self.client.get("/api/study/export.json?topic=missing").status_code, 404)

    def test_practice_restores_from_fresh_application(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(app_env="development", demo_enabled=True, worker_enabled=False, persistence_enabled=True, data_dir=directory)
            with TestClient(create_app(config)) as first:
                response = first.post("/api/study/practice/S07/P-501", json={"answer_index": 1})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["correct"])
            self.assertTrue(Path(directory, "store.sqlite3").is_file())
            with TestClient(create_app(config)) as second:
                self.assertEqual(second.get("/api/study/practice/state").json()["answers"], {"P-501": 1})
                self.assertEqual(second.get("/api/study/sample").json()["stats"]["S07"]["K5"]["n"], 6)
                self.assertEqual(second.app.state.store.courses, {})

    def test_failed_save_rolls_back_answer(self) -> None:
        with patch.object(self.app.state.store, "save", return_value=False):
            response = self.client.post("/api/study/practice/S07/P-501", json={"answer_index": 2})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/api/study/practice/state").json()["answers"], {})

    def test_write_validation_and_cross_site_protection(self) -> None:
        path = "/api/study/practice/S07/P-501"
        for value in (True, -1, 4, "1"):
            self.assertEqual(self.client.post(path, json={"answer_index": value}).status_code, 422)
        self.assertEqual(self.client.post(path, json={"answer_index": 1}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/health", headers={"Host": "evil.example"}).status_code, 400)

    def test_corrupt_sqlite_refuses_fresh_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "store.sqlite3").write_bytes(b"invalid sqlite")
            with self.assertRaises(OSError):
                StudyStore(Settings(app_env="test", persistence_enabled=True, data_dir=directory))

    def test_all_retained_modules_import(self) -> None:
        import importlib
        for path in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
            relative = path.relative_to(Path(__file__).resolve().parents[1]).with_suffix("")
            importlib.import_module(".".join(relative.parts))


class OutboundValidationTests(unittest.TestCase):
    def test_rejects_local_private_reserved_and_non_http(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com", "http://localhost", "http://foo.localhost.",
                    "http://127.0.0.1", "http://10.1.1.1", "http://[::1]", "http://[::ffff:127.0.0.1]",
                    "http://169.254.169.254", "http://192.0.2.1", "https://user:pass@example.com"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_remote_url(url)

    def test_rejects_mixed_public_private_dns(self) -> None:
        answers = [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=answers), self.assertRaises(ValueError):
            validate_remote_url("https://mixed.example")

    def test_accepts_public_dns(self) -> None:
        answers = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=answers):
            self.assertEqual(validate_remote_url("https://example.com"), "https://example.com")
            url, host, sni = pin_remote_url("https://example.com/path?q=1")
        self.assertEqual(url, "https://93.184.216.34/path?q=1")
        self.assertEqual(host, "example.com")
        self.assertEqual(sni, "example.com")

    def test_dns_rebinding_uses_pinned_ip_after_single_resolution(self) -> None:
        answers = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=answers) as resolver:
            pinned, host, sni = pin_remote_url("https://example.com/v1")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(pinned, "https://93.184.216.34/v1")
        self.assertEqual((host, sni), ("example.com", "example.com"))

    def test_model_adapter_sends_only_pinned_ip_with_original_host_and_tls_name(self) -> None:
        requests = []

        def relay(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

        original_sync, original_async = httpx.Client, httpx.AsyncClient
        config = Settings(llm_base_url="https://relay.example/v1", llm_api_key="fixture", llm_model="fixture", llm_providers=())
        answers = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=answers), patch(
            "app.services.llm_client.httpx.Client",
            side_effect=lambda **kwargs: original_sync(transport=httpx.MockTransport(relay), **kwargs),
        ):
            self.assertEqual(LLMClient(config).complete_json([]), {"ok": True})
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=answers), patch(
            "app.services.llm_client.httpx.AsyncClient",
            side_effect=lambda **kwargs: original_async(transport=httpx.MockTransport(relay), **kwargs),
        ):
            self.assertEqual(asyncio.run(LLMClient(config).complete_json_async([])), {"ok": True})
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers["host"], "relay.example")
            self.assertEqual(request.extensions["sni_hostname"], "relay.example")

    def test_model_and_maxkb_do_not_send_to_private_hosts(self) -> None:
        config = Settings(llm_base_url="http://127.0.0.1/v1", llm_api_key="fixture", llm_model="fixture", llm_providers=(),
                          maxkb_url="http://127.0.0.1", maxkb_api_key="fixture", maxkb_dataset_ids=("fixture",))
        with patch("httpx.Client") as sync, patch("httpx.AsyncClient") as asynchronous:
            client = LLMClient(config)
            self.assertIsNone(client.complete([]))
            self.assertEqual(client.last_error, "unsafe_url")
            self.assertFalse(client.last_request_sent)
            self.assertIsNone(asyncio.run(MaxKBClient(config).retrieve("fixture")))
            sync.assert_not_called()
            asynchronous.assert_not_called()


if __name__ == "__main__":
    unittest.main()
