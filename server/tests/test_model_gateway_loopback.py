"""Real TLS loopback coverage for ModelGateway.

The upstream hostname is synthetic and the test-only pin hook redirects the
request to the temporary loopback listener. Production URL validation and pin
rules remain untouched; only this boundary adapter permits the local fixture.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

# Bootstrap before importing app.config. This test must never read a developer
# .env or inherit a real provider/MaxKB endpoint or credential.
os.environ["SKIP_ENV_FILE"] = "1"
for _name in tuple(os.environ):
    if _name.startswith(("LLM_", "MAXKB_")):
        os.environ.pop(_name, None)

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import httpx

from app.config import LLMProvider, Settings
from app.services.model_gateway import GatewayError, ModelGateway


MESSAGES = [{"role": "user", "content": "Explain the synthetic fixture."}]
HOSTNAME = "relay.loopback.test"


@dataclass(frozen=True)
class _Certificates:
    ca_cert: Path
    leaf_cert: Path
    leaf_key: Path


def _run_openssl(executable: str, *arguments: str) -> None:
    try:
        subprocess.run(
            [executable, *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("temporary TLS certificate generation failed") from error


def _make_certificates(executable: str, directory: Path) -> _Certificates:
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.crt"
    leaf_key = directory / "leaf.key"
    leaf_csr = directory / "leaf.csr"
    leaf_cert = directory / "leaf.crt"
    leaf_ext = directory / "leaf.ext"

    _run_openssl(
        executable,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_cert),
        "-sha256",
        "-days",
        "1",
        "-subj",
        "/CN=synthetic loopback test CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    _run_openssl(
        executable,
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(leaf_key),
        "-out",
        str(leaf_csr),
        "-sha256",
        "-subj",
        f"/CN={HOSTNAME}",
    )
    leaf_ext.write_text(
        "subjectAltName=DNS:" + HOSTNAME + "\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="ascii",
    )
    _run_openssl(
        executable,
        "x509",
        "-req",
        "-in",
        str(leaf_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(leaf_cert),
        "-days",
        "1",
        "-sha256",
        "-extfile",
        str(leaf_ext),
    )
    return _Certificates(ca_cert=ca_cert, leaf_cert=leaf_cert, leaf_key=leaf_key)


@dataclass(frozen=True)
class _Request:
    path: str
    headers: dict[str, str]
    body: bytes


class _LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True

    def __init__(self, certificates: _Certificates):
        self._lock = threading.Lock()
        self._route = None
        self._requests: list[_Request] = []
        self._errors: list[BaseException] = []
        self._active = 0
        self._idle = threading.Event()
        self._idle.set()
        self.timeout_release = threading.Event()
        self.sse_release = threading.Event()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=certificates.leaf_cert, keyfile=certificates.leaf_key)
        super().__init__(("127.0.0.1", 0), _LoopbackRequestHandler)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self._thread = threading.Thread(target=self.serve_forever, name="model-gateway-loopback", daemon=False)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def configure(self, route) -> None:
        self.release_waiters()
        if not self._idle.wait(2):
            raise AssertionError("loopback handler did not become idle")
        with self._lock:
            self._route = route
            self._requests = []
            self._errors = []
            self.timeout_release.clear()
            self.sse_release.clear()

    def release_waiters(self) -> None:
        self.timeout_release.set()
        self.sse_release.set()

    def record_start(self, request: _Request):
        with self._lock:
            self._requests.append(request)
            self._active += 1
            self._idle.clear()
            return self._route

    def record_done(self) -> None:
        with self._lock:
            self._active -= 1
            if self._active == 0:
                self._idle.set()

    def record_error(self, error: BaseException) -> None:
        with self._lock:
            self._errors.append(error)

    @property
    def requests(self) -> list[_Request]:
        with self._lock:
            return list(self._requests)

    def wait_idle(self, timeout: float = 2.0) -> bool:
        return self._idle.wait(timeout)

    def assert_no_handler_errors(self) -> None:
        with self._lock:
            errors = list(self._errors)
        if errors:
            kinds = ", ".join(type(error).__name__ for error in errors)
            raise AssertionError(f"loopback handler failed: {kinds}")

    def close_cleanly(self) -> None:
        self.release_waiters()
        self._idle.wait(2)
        self.shutdown()
        self.server_close()
        self._thread.join(2)
        if self._thread.is_alive():
            raise AssertionError("loopback server thread did not stop")
        self.assert_no_handler_errors()


class _LoopbackRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        server: _LoopbackHTTPServer = self.server  # type: ignore[assignment]
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative content length")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ConnectionError("short request body")
            request = _Request(
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
            )
            route = server.record_start(request)
            if route is None:
                raise RuntimeError("no loopback route configured")
            route(self, body)
        except BaseException as error:
            server.record_error(error)
        finally:
            self.close_connection = True
            server.record_done()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib API
        return


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _send_fixed(handler: _LoopbackRequestHandler, status: int, body: bytes, content_type: str = "application/json") -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _sse_frame(value: object) -> bytes:
    return b"data: " + _json_bytes(value) + b"\n\n"


def _fragment(frame: bytes) -> list[bytes]:
    points = {0, len(frame)}
    points.update(range(17, len(frame), 17))
    needle = "你".encode("utf-8")
    index = frame.find(needle)
    if index >= 0:
        points.update({index + 1, index + 2})
    ordered = sorted(points)
    return [frame[start:end] for start, end in zip(ordered, ordered[1:]) if start < end]


def _send_sse(handler: _LoopbackRequestHandler, frames: list[bytes]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    for frame in frames:
        for fragment in _fragment(frame):
            handler.wfile.write(fragment)
            handler.wfile.flush()
            time.sleep(0.002)


class ModelGatewayLoopbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        executable = shutil.which("openssl")
        if executable is None:
            raise unittest.SkipTest("openssl is required for the TLS loopback test")
        cls._temporary_directory = tempfile.TemporaryDirectory(prefix="stage5-model-gateway-")
        try:
            directory = Path(cls._temporary_directory.name)
            cls._certificates = _make_certificates(executable, directory)
            cls._server = _LoopbackHTTPServer(cls._certificates)
        except BaseException:
            cls._temporary_directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            server = getattr(cls, "_server", None)
            if server is not None:
                server.close_cleanly()
        finally:
            temporary_directory = getattr(cls, "_temporary_directory", None)
            if temporary_directory is not None:
                temporary_directory.cleanup()
            super().tearDownClass()

    def setUp(self) -> None:
        self.server = self.__class__._server
        self.server.release_waiters()
        if not self.server.wait_idle(2):
            raise AssertionError("previous loopback handler is still active")
        self.server.assert_no_handler_errors()

    def tearDown(self) -> None:
        self.server.release_waiters()
        if not self.server.wait_idle(2):
            raise AssertionError("loopback handler did not stop")
        self.server.assert_no_handler_errors()

    def _provider(
        self,
        name: str = "synthetic-primary",
        *,
        priority: int = 10,
        api_key: str = "synthetic-primary-token",
    ) -> LLMProvider:
        return LLMProvider(
            name=name,
            channel="teacher",
            base_url=f"https://{HOSTNAME}:{self.server.port}/v1",
            api_key=api_key,
            model="gpt-5.6-sol",
            max_concurrency=1,
            queue_size=1,
            priority=priority,
            quota_group=name,
        )

    @staticmethod
    def _settings(*providers: LLMProvider, timeout: float = 1.0) -> Settings:
        return Settings(
            app_env="test",
            maxkb_url="",
            maxkb_api_key="",
            maxkb_dataset_ids=(),
            maxkb_course_dataset_ids=(),
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            llm_providers=tuple(providers),
            llm_interaction_timeout_seconds=timeout,
            llm_batch_timeout_seconds=timeout,
            llm_interaction_max_tokens=128,
            llm_batch_max_tokens=128,
            llm_reasoning_effort="high",
            llm_prices_json="",
        )

    def _gateway(self, settings: Settings):
        verify_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        verify_context.load_verify_locations(cafile=str(self.__class__._certificates.ca_cert))
        verify_context.check_hostname = True
        verify_context.verify_mode = ssl.CERT_REQUIRED
        self.assertTrue(verify_context.check_hostname)
        self.assertEqual(verify_context.verify_mode, ssl.CERT_REQUIRED)
        calls = []

        def client_factory(**options):
            # ModelGateway's production contract must still pass verify=True;
            # the test factory only replaces it with the temporary CA context.
            self.assertIs(options.get("verify"), True)
            self.assertNotIn("transport", options)
            self.assertIs(options.get("trust_env"), False)
            self.assertIs(options.get("follow_redirects"), False)
            options = dict(options)
            options["verify"] = verify_context
            calls.append(options.copy())
            return httpx.AsyncClient(**options)

        return ModelGateway(settings, client_factory=client_factory), calls, verify_context

    @contextmanager
    def _loopback_security_boundary(self):
        pinned_calls = []

        def pin_remote_url(original: str):
            parsed = urlsplit(original)
            pinned_calls.append(original)
            return (
                f"https://127.0.0.1:{self.server.port}{parsed.path}",
                parsed.netloc,
                parsed.hostname or "",
            )

        def check_pin(original: str, pinned: str, host: str, sni: str) -> None:
            # This narrow test boundary admits only the known local fixture.
            # Production _check_pin still rejects private and loopback IPs.
            original_parts = urlsplit(original)
            pinned_parts = urlsplit(pinned)
            self.assertEqual(pinned_parts.scheme, "https")
            self.assertEqual(pinned_parts.hostname, "127.0.0.1")
            self.assertEqual(pinned_parts.port, self.server.port)
            self.assertEqual(pinned_parts.path, original_parts.path)
            self.assertEqual(host, original_parts.netloc)
            self.assertEqual(sni, original_parts.hostname)

        with patch("app.services.model_gateway.pin_remote_url", side_effect=pin_remote_url), patch.object(
            ModelGateway,
            "_check_pin",
            new=staticmethod(check_pin),
        ):
            yield pinned_calls

    async def test_real_tls_json_success_uses_verified_default_transport(self):
        usage = {"prompt_tokens": 5, "completion_tokens": 3}

        def route(handler, body):
            payload = json.loads(body)
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["model"], "gpt-5.6-sol")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            _send_fixed(
                handler,
                200,
                _json_bytes(
                    {
                        "id": "synthetic-json-request",
                        "model": "gpt-5.6-sol",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": '{"ok":true}'},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": usage,
                    }
                ),
            )

        self.server.configure(route)
        gateway, factory_calls, verify_context = self._gateway(self._settings(self._provider()))
        with self._loopback_security_boundary() as pinned_calls:
            result = await gateway.complete("teacher", MESSAGES, call_id="synthetic-json-success", json_mode=True)

        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.usage, usage)
        self.assertEqual(result.request_id, "synthetic-json-request")
        self.assertFalse(result.cost_pending)
        self.assertEqual(len(factory_calls), 1)
        self.assertIs(factory_calls[0]["verify"], verify_context)
        self.assertEqual(len(pinned_calls), 1)
        requests = self.server.requests
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].path, "/v1/chat/completions")
        self.assertEqual(requests[0].headers["host"], f"{HOSTNAME}:{self.server.port}")
        self.assertEqual(requests[0].headers["accept"], "application/json")

    async def test_strict_json_error_keeps_usage_over_real_tls(self):
        usage = {"prompt_tokens": 7, "completion_tokens": 4}

        def route(handler, body):
            payload = json.loads(body)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            _send_fixed(
                handler,
                200,
                _json_bytes(
                    {
                        "id": "synthetic-json-invalid",
                        "model": "gpt-5.6-sol",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": '{"ok":1,"ok":2}'},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": usage,
                    }
                ),
            )

        self.server.configure(route)
        gateway, factory_calls, _ = self._gateway(self._settings(self._provider()))
        with self._loopback_security_boundary():
            with self.assertRaises(GatewayError) as caught:
                await gateway.complete("teacher", MESSAGES, call_id="synthetic-json-invalid", json_mode=True)

        error = caught.exception
        self.assertEqual(error.code, "invalid_json")
        self.assertEqual(error.usage, usage)
        self.assertTrue(error.request_sent)
        self.assertFalse(error.unknown_outcome)
        self.assertFalse(error.cost_pending)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(self.server.requests), 1)

    async def test_fragmented_utf8_sse_exposes_public_content_and_tail_usage(self):
        usage = {"prompt_tokens": 11, "completion_tokens": 6}

        def route(handler, body):
            payload = json.loads(body)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["stream_options"], {"include_usage": True})
            _send_sse(
                handler,
                [
                    _sse_frame(
                        {
                            "id": "synthetic-sse-request",
                            "model": "gpt-5.6-sol",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "reasoning_content": "private"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    ),
                    _sse_frame(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "你", "reasoning_content": "private"},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    ),
                    _sse_frame(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "好"},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    ),
                    _sse_frame(
                        {
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                    ),
                    _sse_frame({"choices": [], "usage": usage}),
                    b"data: [DONE]\n\n",
                ],
            )

        self.server.configure(route)
        gateway, factory_calls, _ = self._gateway(self._settings(self._provider()))
        visible = []
        with self._loopback_security_boundary():
            result = await gateway.complete(
                "teacher",
                MESSAGES,
                call_id="synthetic-sse",
                on_delta=visible.append,
            )

        self.assertEqual(visible, ["你", "好"])
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.usage, usage)
        self.assertFalse(result.cost_pending)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0].headers["accept"], "text/event-stream")

    async def test_real_unknown_outcomes_are_single_send(self):
        cases = (
            ("truncated", "incomplete_stream", None),
            ("read-timeout", "timeout", 0.2),
        )
        for case, expected_code, timeout in cases:
            with self.subTest(case=case):
                if case == "truncated":
                    def route(handler, body):
                        _send_sse(
                            handler,
                            [_sse_frame({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})],
                        )
                else:
                    def route(handler, body):
                        handler.send_response(200)
                        handler.send_header("Content-Type", "application/json")
                        handler.send_header("Connection", "close")
                        handler.end_headers()
                        handler.wfile.write(b'{"id":"partial","choices":[')
                        handler.wfile.flush()
                        self.server.timeout_release.wait(2)

                self.server.configure(route)
                gateway, factory_calls, _ = self._gateway(
                    self._settings(self._provider(), timeout=1.0 if timeout is None else timeout)
                )
                try:
                    with self._loopback_security_boundary():
                        with self.assertRaises(GatewayError) as caught:
                            await gateway.complete("teacher", MESSAGES, call_id=f"synthetic-{case}") if case == "read-timeout" else await gateway.complete(
                                "teacher", MESSAGES, call_id=f"synthetic-{case}", on_delta=lambda _text: None
                            )
                finally:
                    self.server.release_waiters()
                    self.assertTrue(self.server.wait_idle(2))

                error = caught.exception
                self.assertEqual(error.code, expected_code)
                self.assertTrue(error.request_sent)
                self.assertTrue(error.unknown_outcome)
                self.assertEqual(len(factory_calls), 1)
                self.assertEqual(len(self.server.requests), 1)

    async def test_real_429_is_single_send_without_failover(self):
        def route(handler, body):
            _send_fixed(
                handler,
                429,
                _json_bytes({"error": {"message": "synthetic rate limit"}}),
            )

        self.server.configure(route)
        primary = self._provider()
        secondary = self._provider(
            "synthetic-secondary",
            priority=20,
            api_key="synthetic-secondary-token",
        )
        gateway, factory_calls, _ = self._gateway(self._settings(primary, secondary))
        with self._loopback_security_boundary():
            with self.assertRaises(GatewayError) as caught:
                await gateway.complete("teacher", MESSAGES, call_id="synthetic-429")

        error = caught.exception
        self.assertEqual(error.code, "rate_limited")
        self.assertEqual(error.status_code, 429)
        self.assertTrue(error.request_sent)
        self.assertFalse(error.unknown_outcome)
        self.assertEqual(error.provider, "synthetic-primary")
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0].headers["authorization"], "Bearer synthetic-primary-token")


if __name__ == "__main__":
    unittest.main()
