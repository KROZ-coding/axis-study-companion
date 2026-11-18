"""Gateway contract tests: mock transports, no credentials, DNS or live models."""

import asyncio
import json
import os
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["SKIP_ENV_FILE"] = "1"

import httpx

from app.config import LLMProvider, Settings
from app.services.model_gateway import (
    GatewayError, ModelGateway, build_context, build_minimal_context,
)


MESSAGES = [{"role": "user", "content": "Explain 1/2 using the approved example."}]
DNS = [(2, 1, 6, "", ("93.184.216.34", 443))]


def provider(name="teacher", channel="teacher", model="gpt-5.6-sol", **changes):
    values = dict(name=name, channel=channel, model=model, base_url="https://relay.example/v1",
                  api_key=secrets.token_urlsafe(24), max_concurrency=2, queue_size=4, priority=10, quota_group=name)
    values.update(changes)
    return LLMProvider(**values)


def settings(*providers, **changes):
    return Settings(llm_providers=tuple(providers), llm_base_url="", llm_api_key="", llm_model="", **changes)


def response(text="Answer", usage=None, **changes):
    body = {"id": "body-request", "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        body["usage"] = usage
    body.update(changes)
    return httpx.Response(200, json=body, headers={"x-request-id": "upstream-request"})


def frame(delta=None, *, finish=None, **changes):
    body = {"choices": [{"index": 0, "delta": {} if delta is None else delta, "finish_reason": finish}]}
    body.update(changes)
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode()


def responses_body(text="Answer", usage=None, **changes):
    body = {
        "id": "responses-request", "object": "response", "model": "gpt-5.6-sol", "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private thought"}]},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text}]},
        ],
    }
    if usage is not None:
        body["usage"] = usage
    body.update(changes)
    return body


def responses_frame(kind, **changes):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **changes}, ensure_ascii=False) + "\n\n").encode()


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, BaseException):
                raise part
            yield part

    async def aclose(self):
        self.closed = True


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dns = patch("app.utils.url_security.socket.getaddrinfo", return_value=DNS)
        self.dns.start()
        self.addCleanup(self.dns.stop)

    async def complete(self, gateway, purpose="teacher", messages=None, **options):
        return await gateway.complete(purpose, MESSAGES if messages is None else messages, call_id="fixture-call", **options)

    async def test_exact_routing_high_purpose_limits_and_pinned_tls(self):
        records = []
        config = settings(provider(), provider("student", "student", "gpt-5.6-sol"),
                          provider("grading", "grading", "gpt-5.6-terra"),
                          provider("summary", "summary", "gpt-5.6-terra"),
                          llm_reasoning_effort="low", llm_max_tokens=1, llm_timeout_seconds=1)

        def handler(request):
            records.append(request)
            payload = json.loads(request.content)
            return response(usage={"prompt_tokens": 19, "completion_tokens": 11}, model=payload["model"])

        gateway = ModelGateway(config, transport=httpx.MockTransport(handler))
        expected = {
            "teacher": ("gpt-5.6-sol", 4096), "suggest": ("gpt-5.6-sol", 4096),
            "generate": ("gpt-5.6-sol", 8192), "review": ("gpt-5.6-sol", 8192),
            "student": ("gpt-5.6-sol", 4096), "hint": ("gpt-5.6-sol", 4096),
            "grading": ("gpt-5.6-terra", 8192), "summary": ("gpt-5.6-terra", 8192),
        }
        for purpose, (model, tokens) in expected.items():
            with self.subTest(purpose=purpose):
                result = await self.complete(gateway, purpose)
                self.assertEqual((result.text, result.model, result.reasoning_effort), ("Answer", model, "high"))
                self.assertEqual(result.usage, {"prompt_tokens": 19, "completion_tokens": 11})
                self.assertEqual(result.request_id, "upstream-request")
                self.assertTrue(result.request_sent)
                self.assertFalse(result.cost_pending)
                request = records[-1]
                payload = json.loads(request.content)
                self.assertEqual((payload["model"], payload["reasoning_effort"], payload["max_tokens"]), (model, "high", tokens))
                self.assertEqual(payload["messages"], MESSAGES)
                self.assertNotIn("tools", payload)
                self.assertNotIn("reasoning", payload)
                self.assertEqual(request.url.host, "93.184.216.34")
                self.assertEqual(request.url.path, "/v1/chat/completions")
                self.assertEqual(request.headers["host"], "relay.example")
                self.assertEqual(request.extensions["sni_hostname"], "relay.example")
                self.assertEqual(request.headers["idempotency-key"], "fixture-call")
                self.assertEqual(request.extensions["timeout"]["read"], 180 if tokens == 4096 else 360)
        self.assertEqual(len(records), 8)

    async def test_new_limits_override_legacy_and_read_only_preflight_does_not_resolve(self):
        config = SimpleNamespace(llm_providers=(provider(),), llm_interaction_timeout_seconds=215,
                                 llm_batch_timeout_seconds=480, llm_interaction_max_tokens=6000,
                                 llm_batch_max_tokens=12000, llm_timeout_seconds=1, llm_max_tokens=1)
        gateway = ModelGateway(config)
        with patch("app.services.model_gateway.pin_remote_url", side_effect=AssertionError("no DNS")):
            self.assertIs(gateway.provider_for("teacher"), config.llm_providers[0])
            descriptor = gateway.describe("generate")
            self.assertEqual(descriptor["model"], "gpt-5.6-sol")
            self.assertEqual(descriptor["reasoning_effort"], "high")
            self.assertEqual(descriptor["max_tokens"], 12000)
            self.assertEqual(descriptor["timeout_seconds"], 480)
        calls = []
        gateway = ModelGateway(config, transport=httpx.MockTransport(lambda request: calls.append(request) or response()))
        await self.complete(gateway)
        self.assertEqual(json.loads(calls[0].content)["max_tokens"], 6000)
        self.assertEqual(calls[0].extensions["timeout"]["read"], 215)

    async def test_client_factory_enforces_verified_tls_without_env_or_redirects(self):
        options = []
        factory_transport = httpx.MockTransport(lambda request: response())

        def factory(**kwargs):
            options.append(kwargs.copy())
            return httpx.AsyncClient(transport=factory_transport, **kwargs)

        await self.complete(ModelGateway(settings(provider()), client_factory=factory))
        self.assertEqual(len(options), 1)
        self.assertIs(options[0]["verify"], True)
        self.assertIs(options[0]["trust_env"], False)
        self.assertIs(options[0]["follow_redirects"], False)

    async def test_usage_missing_partial_or_invalid_remains_none(self):
        values = (None, {}, {"prompt_tokens": 5}, {"prompt_tokens": "5", "completion_tokens": 2},
                  {"prompt_tokens": True, "completion_tokens": 2}, {"prompt_tokens": -1, "completion_tokens": 2})
        for usage in values:
            with self.subTest(usage=usage):
                gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response(usage=usage)))
                result = await self.complete(gateway)
                self.assertIsNone(result.usage)
                self.assertTrue(result.cost_pending)
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response(usage={"prompt_tokens": 0, "completion_tokens": 0})))
        result = await self.complete(gateway)
        self.assertEqual(result.usage, {"prompt_tokens": 0, "completion_tokens": 0})
        self.assertFalse(result.cost_pending)

    async def test_no_legacy_shared_wrong_group_or_model_fallback(self):
        configs = (
            settings(provider("shared", "shared")),
            Settings(llm_providers=(), llm_base_url="https://relay.example/v1", llm_api_key=secrets.token_urlsafe(24), llm_model="gpt-5.6-sol"),
            settings(provider("student", "student", "gpt-5.6-sol")),
            settings(provider(model="gpt-6-sol"), provider("shared", "shared")),
            settings(provider("wrong", "teacher", "gpt-5.6-terra"), provider("correct")),
        )
        for config in configs:
            with self.subTest(config=config), patch("app.services.model_gateway.pin_remote_url", side_effect=AssertionError("must not resolve")):
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(ModelGateway(config))
                self.assertFalse(caught.exception.request_sent)
                self.assertFalse(caught.exception.unknown_outcome)
                self.assertEqual(caught.exception.status_code, 503)
        grading = provider("grading", "grading", "wrong")
        gateway = ModelGateway(settings(grading, provider("student", "student", "gpt-5.6-sol")))
        with self.assertRaises(GatewayError) as caught:
            gateway.provider_for("grading")
        self.assertEqual(caught.exception.code, "model_mismatch")
        specific = provider("grading", "grading", "gpt-5.6-terra")
        summary = provider("summary", "summary", "gpt-5.6-terra")
        gateway = ModelGateway(settings(specific, summary, provider("student", "student", "gpt-5.6-sol")))
        self.assertIs(gateway.provider_for("grading"), specific)
        self.assertIs(gateway.provider_for("summary"), summary)

    async def test_student_sol_and_explicit_terra_groups_are_required_before_send(self):
        cases = (
            ("student", "gpt-5.6-terra", "model_mismatch"),
            ("hint", "gpt-5.6-terra", "model_mismatch"),
            ("grading", "gpt-5.6-sol", "not_configured"),
            ("summary", "gpt-5.6-sol", "not_configured"),
            ("grading", "gpt-5.6-terra", "not_configured"),
            ("summary", "gpt-5.6-terra", "not_configured"),
        )
        sends = []
        for purpose, student_model, code in cases:
            gateway = ModelGateway(settings(provider("student", "student", student_model)),
                                   transport=httpx.MockTransport(lambda request: sends.append(request) or response()))
            with self.subTest(purpose=purpose, student_model=student_model), patch(
                "app.services.model_gateway.pin_remote_url", side_effect=AssertionError("must not resolve"),
            ):
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(gateway, purpose)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.request_sent)
                self.assertFalse(caught.exception.unknown_outcome)
        self.assertEqual(sends, [])

    async def test_no_retry_or_failover_after_upstream_error(self):
        requests = []
        first = provider("first")

        def failure(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {"message": first.api_key + " must never escape"}})

        gateway = ModelGateway(settings(first, provider("second", priority=20)), transport=httpx.MockTransport(failure))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway)
        self.assertEqual(len(requests), 1)
        error = caught.exception
        self.assertEqual((error.code, error.request_sent, error.unknown_outcome), ("rate_limited", True, False))
        self.assertTrue(error.cost_pending)
        self.assertNotIn(first.api_key, str(error))
        self.assertEqual(error.provider, "first")

    async def test_redirect_is_not_followed(self):
        requests = []
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(307, headers={"location": "https://other.example/v1"})))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway)
        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(len(requests), 1)

    async def test_unsafe_urls_dns_and_invalid_pin_never_send(self):
        urls = ("http://relay.example/v1", "https://127.0.0.1/v1", "https://localhost/v1", "https://[::1]/v1",
                "https://name:secret@relay.example/v1", "https://relay.example/v1?token=secret", "https://relay.example/v1#fragment")
        sends = []
        for url in urls:
            with self.subTest(url=url):
                gateway = ModelGateway(settings(provider(base_url=url)), transport=httpx.MockTransport(lambda request: sends.append(request) or response()))
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(gateway)
                self.assertEqual(caught.exception.code, "unsafe_url")
                self.assertFalse(caught.exception.request_sent)
        self.assertEqual(sends, [])
        with patch("app.utils.url_security.socket.getaddrinfo", return_value=DNS + [(2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(GatewayError):
                await self.complete(ModelGateway(settings(provider())))
        for pinned in (("https://93.184.216.34/v1/chat/completions", "other.example", "relay.example"),
                       ("https://93.184.216.34/v1/chat/completions", "relay.example", "other.example")):
            with patch("app.services.model_gateway.pin_remote_url", return_value=pinned):
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(ModelGateway(settings(provider())))
                self.assertFalse(caught.exception.request_sent)

    async def test_strict_json_entire_object_only_and_retain_usage_on_parse_error(self):
        usage = {"prompt_tokens": 4, "completion_tokens": 7}
        for text in ('prefix {"ok":true}', '```json\n{"ok":true}\n```', '{"ok":1}{"ok":2}',
                     '[{"ok":true}]', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'):
            with self.subTest(text=text):
                gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response(text, usage)))
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(gateway, json_mode=True)
                self.assertEqual(caught.exception.code, "invalid_json")
                self.assertEqual(caught.exception.usage, usage)
                self.assertTrue(caught.exception.request_sent)
                self.assertFalse(caught.exception.unknown_outcome)
                self.assertFalse(caught.exception.cost_pending)
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response(' \n {"ok": true} \t ', usage)))
        result = await self.complete(gateway, json_mode=True)
        self.assertEqual(result.data, {"ok": True})

    async def test_model_mismatch_truncation_tools_and_empty_reasoning_are_errors(self):
        bodies = (
            (dict(model="gpt-6-sol"), "model_mismatch"),
            ({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}, "output_truncated"),
            ({"choices": [{"message": {"content": "", "reasoning_content": "hidden"}, "finish_reason": "stop"}]}, "invalid_response"),
            ({"choices": [{"message": {"content": "text", "tool_calls": [{"name": "run"}]}, "finish_reason": "tool_calls"}]}, "unsupported_tool_call"),
        )
        for changes, code in bodies:
            with self.subTest(code=code):
                gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response(**changes)))
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(gateway)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.unknown_outcome)

    async def test_messages_reject_tools_reasoning_identity_metadata_and_invalid_types_before_send(self):
        messages = ([], [{"role": "tool", "content": "x"}], [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                    [{"role": "user", "content": "x", "student_id": "private"}],
                    [{"role": "assistant", "content": "x", "reasoning_content": "hidden"}],
                    [{"role": "assistant", "content": "x", "tool_calls": []}],
                    [{"role": "user", "content": "x" * 64_001}])
        with patch("app.services.model_gateway.pin_remote_url", side_effect=AssertionError("no DNS")):
            for value in messages:
                with self.subTest(value=str(value)[:100]):
                    with self.assertRaises(GatewayError) as caught:
                        await self.complete(ModelGateway(settings(provider())), messages=value)
                    self.assertFalse(caught.exception.request_sent)
            with self.assertRaises(GatewayError) as caught:
                await ModelGateway(settings(provider())).complete("other", MESSAGES, call_id="x")
            self.assertEqual(caught.exception.code, "invalid_purpose")
            with self.assertRaises(GatewayError):
                await ModelGateway(settings(provider())).complete("teacher", MESSAGES, call_id="x\r\nsecret")

    async def test_safe_real_sse_increments_only_public_content(self):
        visible = []
        first_observed = asyncio.Event()
        closed = asyncio.Event()

        class LiveStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": heartbeat\r\n\r\n"
                yield frame({"role": "assistant", "reasoning_content": "private thought"})
                content = frame({"content": "你好", "reasoning": "also private"})
                split = content.index("你".encode()) + 1
                yield content[:split]
                yield content[split:]
                # If aiter buffers the whole response, this deadline fails.
                await asyncio.wait_for(first_observed.wait(), timeout=1)
                yield frame({"content": " world"})
                yield frame(finish="stop")
                yield b'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 8}}\n\n'
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                closed.set()

        def callback(text):
            visible.append(text)
            first_observed.set()

        for purpose in ("teacher", "student"):
            with self.subTest(purpose=purpose):
                visible.clear()
                first_observed.clear()
                closed.clear()
                requests = []
                gateway = ModelGateway(settings(provider(purpose, purpose)), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, stream=LiveStream(), headers={"content-type": "text/event-stream; charset=utf-8"})))
                result = await self.complete(gateway, purpose, on_delta=callback)
                self.assertEqual(visible, ["你好", " world"])
                self.assertEqual(result.text, "你好 world")
                self.assertEqual(result.model, "gpt-5.6-sol")
                self.assertEqual(result.usage, {"prompt_tokens": 9, "completion_tokens": 8})
                self.assertFalse(result.cost_pending)
                self.assertTrue(closed.is_set())
                self.assertEqual(len(requests), 1)
                payload = json.loads(requests[0].content)
                self.assertEqual((payload["model"], payload["reasoning_effort"]), ("gpt-5.6-sol", "high"))
                self.assertTrue(payload["stream"])
                self.assertEqual(payload["stream_options"], {"include_usage": True})
                self.assertNotIn("private", repr(result))

    async def test_async_delta_json_sse_and_usage_missing(self):
        deltas = []

        async def callback(text):
            deltas.append(text)
            await asyncio.sleep(0)

        stream = ByteStream([frame({"content": '{"ok":'}), frame({"content": 'true}'}), frame(finish="stop"), b"data: [DONE]\n\n"])
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})))
        result = await self.complete(gateway, on_delta=callback, json_mode=True)
        self.assertEqual(deltas, ['{"ok":', 'true}'])
        self.assertEqual(result.data, {"ok": True})
        self.assertIsNone(result.usage)
        self.assertTrue(result.cost_pending)

    async def test_sse_missing_done_malformed_tools_or_wrong_content_type_fail_once(self):
        cases = (
            ([frame({"content": "partial"})], "incomplete_stream", True),
            ([b"data: not-json\n\n"], "invalid_sse", True),
            ([frame({"tool_calls": [{"function": "do"}]})], "unsupported_tool_call", True),
            ([frame({"content": "partial"}, finish="length")], "output_truncated", False),
            ([b"data: [DONE]\n\n"], "invalid_sse", False),
        )
        for chunks, code, unknown in cases:
            stream = ByteStream(chunks)
            requests = []
            gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})))
            with self.subTest(code=code), self.assertRaises(GatewayError) as caught:
                await self.complete(gateway, on_delta=lambda text: None)
            self.assertEqual((caught.exception.code, caught.exception.unknown_outcome), (code, unknown))
            self.assertEqual(len(requests), 1)
            self.assertTrue(stream.closed)
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response()))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway, on_delta=lambda text: None)
        self.assertEqual(caught.exception.code, "invalid_sse")
        self.assertFalse(caught.exception.unknown_outcome)

    async def test_transport_timeout_sent_unknown_classification(self):
        cases = ((httpx.ConnectError("fixture"), "transport_error", False),
                 (httpx.ConnectTimeout("fixture"), "timeout", False),
                 (httpx.PoolTimeout("fixture"), "timeout", False),
                 (httpx.ReadTimeout("fixture"), "timeout", True),
                 (httpx.WriteTimeout("fixture"), "timeout", True),
                 (httpx.ReadError("fixture"), "transport_error", True))
        for failure, code, sent in cases:
            requests = []

            def handler(request):
                requests.append(request)
                raise failure

            with self.subTest(error=type(failure).__name__), self.assertRaises(GatewayError) as caught:
                await self.complete(ModelGateway(settings(provider()), transport=httpx.MockTransport(handler)))
            self.assertEqual((caught.exception.code, caught.exception.request_sent, caught.exception.unknown_outcome), (code, sent, sent))
            self.assertEqual(len(requests), 1)

    async def test_total_timeout_before_send_and_after_send(self):
        config = SimpleNamespace(llm_providers=(provider(),), llm_interaction_timeout_seconds=0.02)
        async def delayed_pin(*args):
            await asyncio.sleep(1)

        with patch("app.services.model_gateway.asyncio.to_thread", side_effect=delayed_pin):
            with self.assertRaises(GatewayError) as caught:
                await self.complete(ModelGateway(config))
            self.assertEqual((caught.exception.code, caught.exception.request_sent, caught.exception.unknown_outcome), ("timeout", False, False))

        async def slow(request):
            await asyncio.sleep(1)
            return response()

        async def pinned_immediately(*args):
            return "https://93.184.216.34/v1/chat/completions", "relay.example", "relay.example"

        with patch("app.services.model_gateway.asyncio.to_thread", side_effect=pinned_immediately):
            with self.assertRaises(GatewayError) as caught:
                await self.complete(ModelGateway(config, transport=httpx.MockTransport(slow)))
        self.assertEqual((caught.exception.code, caught.exception.request_sent, caught.exception.unknown_outcome), ("timeout", True, True))

    async def test_cancellation_before_send_and_during_stream(self):
        reached = asyncio.Event()

        async def blocked_pin(*args):
            reached.set()
            await asyncio.Event().wait()

        with patch("app.services.model_gateway.asyncio.to_thread", side_effect=blocked_pin):
            task = asyncio.create_task(self.complete(ModelGateway(settings(provider()))))
            await asyncio.wait_for(reached.wait(), 1)
            task.cancel()
            with self.assertRaises(GatewayError) as caught:
                await task
            self.assertEqual((caught.exception.code, caught.exception.request_sent, caught.exception.unknown_outcome), ("cancelled", False, False))
        observed = asyncio.Event()
        closed = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": "saved partial"})
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=WaitingStream(), headers={"content-type": "text/event-stream"})))
        task = asyncio.create_task(self.complete(gateway, on_delta=lambda text: observed.set()))
        await asyncio.wait_for(observed.wait(), 1)
        task.cancel()
        with self.assertRaises(GatewayError) as caught:
            await task
        self.assertEqual((caught.exception.code, caught.exception.request_sent, caught.exception.unknown_outcome), ("cancelled", True, True))
        self.assertTrue(closed.is_set())

    async def test_sse_crlf_chunk_boundaries_multiline_data_and_final_cr(self):
        raw = (b': heartbeat\r\nid: 1\r\ndata: {"choices":[{"index":0,\r\ndata: "delta":{"content":"ok"},"finish_reason":null}]}\r\n\r\n'
               + frame(finish="stop").replace(b"\n", b"\r\n") + b"data: [DONE]\r")
        stream = ByteStream([raw[index:index + 1] for index in range(len(raw))])
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})))
        deltas = []
        result = await self.complete(gateway, on_delta=deltas.append)
        self.assertEqual(deltas, ["ok"])
        self.assertEqual(result.text, "ok")
        self.assertTrue(stream.closed)

    async def test_stream_error_keeps_sent_state_and_observed_usage(self):
        stream = ByteStream([frame({"content": "partial"}, usage={"prompt_tokens": 3, "completion_tokens": 2}), httpx.ConnectError("late failure")])
        gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway, on_delta=lambda text: None)
        self.assertEqual((caught.exception.request_sent, caught.exception.unknown_outcome), (True, True))
        self.assertEqual(caught.exception.usage, {"prompt_tokens": 3, "completion_tokens": 2})
        self.assertTrue(stream.closed)

    async def test_bounded_response_and_no_fake_success(self):
        with patch("app.services.model_gateway._MAX_RESPONSE_BYTES", 64):
            gateway = ModelGateway(settings(provider()), transport=httpx.MockTransport(lambda request: response("x" * 1000)))
            with self.assertRaises(GatewayError) as caught:
                await self.complete(gateway)
            self.assertEqual(caught.exception.code, "response_too_large")
            self.assertTrue(caught.exception.request_sent)


    async def test_responses_exact_endpoint_payload_json_limits_and_pinned_tls(self):
        requests = []
        selected = provider(api_type="responses", base_url="https://www.poke2api.com/v1/responses")

        def handler(request):
            requests.append(request)
            payload = json.loads(request.content)
            return httpx.Response(200, json=responses_body('{"ok": true}', {"input_tokens": 19, "output_tokens": 11}, model=payload["model"]),
                                  headers={"x-request-id": "responses-header"})

        config = settings(selected, provider("student", "student", api_type="responses"),
                          provider("grading", "grading", "gpt-5.6-terra", api_type="responses"),
                          provider("summary", "summary", "gpt-5.6-terra", api_type="responses"), llm_reasoning_effort="low")
        gateway = ModelGateway(config, transport=httpx.MockTransport(handler))
        for purpose in ("teacher", "suggest", "generate", "review", "student", "hint", "grading", "summary"):
            with self.subTest(purpose=purpose):
                result = await self.complete(gateway, purpose, json_mode=True)
                request = requests[-1]
                payload = json.loads(request.content)
                tokens = 4096 if purpose in {"teacher", "suggest", "student", "hint"} else 8192
                self.assertEqual(payload, {"model": result.model, "input": MESSAGES, "max_output_tokens": tokens,
                                          "reasoning": {"effort": "high"}, "store": False, "stream": False,
                                          "text": {"format": {"type": "json_object"}}})
                self.assertEqual(request.url.path, "/v1/responses")
                self.assertEqual(request.url.host, "93.184.216.34")
                host = "www.poke2api.com" if purpose in {"teacher", "suggest", "generate", "review"} else "relay.example"
                self.assertEqual(request.headers["host"], host)
                self.assertEqual(request.extensions["sni_hostname"], host)
                self.assertEqual(request.extensions["timeout"]["read"], 180 if tokens == 4096 else 360)
                self.assertEqual(result.usage, {"prompt_tokens": 19, "completion_tokens": 11})
                self.assertEqual(result.data, {"ok": True})
                self.assertEqual(result.request_id, "responses-header")
                self.assertNotIn("private", repr(result))
        self.assertEqual(len(requests), 8)

    async def test_responses_collects_only_message_output_text_and_never_top_level_or_reasoning(self):
        body = responses_body()
        body["output"][1]["content"] = [{"type": "output_text", "text": "公开"}, {"type": "output_text", "text": "答案"}]
        body["output"].append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "."}]})
        body["output_text"] = "private alternate text"
        gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        result = await self.complete(gateway)
        self.assertEqual(result.text, "公开答案.")
        self.assertEqual(result.request_id, "responses-request")
        self.assertIsNone(result.usage)
        self.assertTrue(result.cost_pending)
        body["output"] = body["output"][:1]
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertFalse(caught.exception.unknown_outcome)
        self.assertNotIn("private", str(caught.exception))

    async def test_responses_usage_is_strict_and_never_inferred_from_totals_or_chat_fields(self):
        values = (None, {}, {"input_tokens": 5}, {"input_tokens": "5", "output_tokens": 2},
                  {"input_tokens": True, "output_tokens": 2}, {"input_tokens": -1, "output_tokens": 2},
                  {"input_tokens": 1, "output_tokens": 1.0}, {"input_tokens": 10**12 + 1, "output_tokens": 2},
                  {"total_tokens": 8}, {"prompt_tokens": 5, "completion_tokens": 3})
        for usage in values:
            with self.subTest(usage=usage):
                gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=responses_body(usage=usage))))
                result = await self.complete(gateway)
                self.assertIsNone(result.usage)
                self.assertTrue(result.cost_pending)
        gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses_body(usage={"input_tokens": 0, "output_tokens": 0}))))
        self.assertEqual((await self.complete(gateway)).usage, {"prompt_tokens": 0, "completion_tokens": 0})

    async def test_responses_terminal_errors_retain_metadata_and_are_never_retried(self):
        cases = (
            ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "output_truncated", False),
            ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "content_filtered", False),
            ({"status": "incomplete", "incomplete_details": {"reason": "other"}}, "output_incomplete", False),
            ({"status": "failed", "error": {"message": "private upstream failure"}}, "upstream_error", False),
            ({"status": "cancelled"}, "cancelled", False),
            ({"status": "in_progress"}, "incomplete_response", True),
            ({"status": None}, "incomplete_response", True),
            ({"model": "wrong-model"}, "model_mismatch", False),
            ({"output": [{"type": "function_call", "arguments": "private"}]}, "unsupported_tool_call", False),
            ({"output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "private refusal"}]}]}, "refusal", False),
        )
        for changes, code, unknown in cases:
            calls = []
            body = responses_body(usage={"input_tokens": 13, "output_tokens": 7}, **changes)
            gateway = ModelGateway(settings(provider(api_type="responses"), provider("backup", api_type="responses", priority=20)),
                                   transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json=body)))
            with self.subTest(code=code, changes=changes), self.assertRaises(GatewayError) as caught:
                await self.complete(gateway)
            error = caught.exception
            self.assertEqual((error.code, error.request_sent, error.unknown_outcome), (code, True, unknown))
            self.assertEqual(error.usage, {"prompt_tokens": 13, "completion_tokens": 7})
            self.assertEqual(error.request_id, "responses-request")
            self.assertEqual(error.model, "gpt-5.6-sol")
            self.assertFalse(error.cost_pending)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private", str(error))

    async def test_responses_strict_json_preserves_usage_on_invalid_output(self):
        for text in ('prefix {"ok":true}', '```json\n{"ok":true}\n```', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '[]'):
            with self.subTest(text=text):
                gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=responses_body(text, {"input_tokens": 4, "output_tokens": 7}))))
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(gateway, json_mode=True)
                self.assertEqual(caught.exception.code, "invalid_json")
                self.assertEqual(caught.exception.usage, {"prompt_tokens": 4, "completion_tokens": 7})
                self.assertFalse(caught.exception.unknown_outcome)
        raw = '{"id":"duplicate","status":"completed","status":"failed"}'
        gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(lambda request: httpx.Response(200, text=raw)))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(caught.exception.unknown_outcome)

    async def test_responses_http_errors_map_usage_and_redact_body_without_retry(self):
        selected = provider(api_type="responses")
        requests = []
        gateway = ModelGateway(settings(selected), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(
            429, json={"id": "failed-request", "error": {"message": selected.api_key}, "usage": {"input_tokens": 8, "output_tokens": 2}})))
        with self.assertRaises(GatewayError) as caught:
            await self.complete(gateway)
        self.assertEqual((caught.exception.code, caught.exception.unknown_outcome), ("rate_limited", False))
        self.assertEqual(caught.exception.usage, {"prompt_tokens": 8, "completion_tokens": 2})
        self.assertEqual(caught.exception.request_id, "failed-request")
        self.assertNotIn(selected.api_key, str(caught.exception))
        self.assertEqual(len(requests), 1)

    async def test_responses_sse_live_public_deltas_completed_usage_without_done(self):
        observed = asyncio.Event()
        visible = []
        requests = []
        terminal = responses_body("你好 world", {"input_tokens": 9, "output_tokens": 8})

        class LiveResponsesStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield responses_frame("response.created", response={"id": "responses-request", "status": "in_progress", "model": "gpt-5.6-sol"})
                yield responses_frame("response.reasoning_summary_text.delta", delta="private thought")
                yield responses_frame("response.output_item.added", item={"type": "reasoning", "summary": []})
                raw = responses_frame("response.output_text.delta", delta="你好", output_index=1, content_index=0)
                split = raw.index("你".encode()) + 1
                yield raw[:split]
                yield raw[split:]
                await asyncio.wait_for(observed.wait(), timeout=1)
                yield responses_frame("response.output_text.delta", delta=" world", output_index=1, content_index=0)
                yield responses_frame("response.output_text.done", text="你好 world", output_index=1, content_index=0)
                yield responses_frame("response.completed", response=terminal).rstrip(b"\n")

            async def aclose(self):
                self.closed = True

        async def callback(text):
            visible.append(text)
            observed.set()
            await asyncio.sleep(0)

        stream = LiveResponsesStream()
        gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream; charset=utf-8"})))
        result = await self.complete(gateway, on_delta=callback)
        self.assertEqual((visible, result.text), (["你好", " world"], "你好 world"))
        self.assertEqual(result.usage, {"prompt_tokens": 9, "completion_tokens": 8})
        self.assertFalse(result.cost_pending)
        self.assertTrue(stream.closed)
        payload = json.loads(requests[0].content)
        self.assertTrue(payload["stream"])
        self.assertNotIn("stream_options", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("private", repr(result))
        self.assertEqual(len(requests), 1)

    async def test_responses_sse_terminal_failure_refusal_tools_and_unknown_disconnect(self):
        usage = {"input_tokens": 3, "output_tokens": 2}
        created = responses_frame("response.created", response={"id": "responses-request", "status": "in_progress", "usage": usage})
        text = responses_frame("response.output_text.delta", delta="partial")
        cases = (
            ([created, text], "incomplete_stream", True),
            ([created, text, httpx.ReadError("private transport failure")], "transport_error", True),
            ([created, text, b"data: [DONE]\n\n"], "incomplete_stream", True),
            ([created, b"data: not-json\n\n"], "invalid_sse", True),
            ([created, responses_frame("response.failed", response=responses_body(usage=usage, status="failed", error={"message": "private failure"}))], "upstream_error", False),
            ([created, text, responses_frame("response.incomplete", response=responses_body(usage=usage, status="incomplete", incomplete_details={"reason": "max_output_tokens"}))], "output_truncated", False),
            ([created, responses_frame("response.refusal.delta", delta="private refusal"), responses_frame("response.completed", response=responses_body(usage=usage, output=[{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "private refusal"}]}]))], "refusal", False),
            ([created, responses_frame("response.output_item.added", item={"type": "function_call", "arguments": "private"}), responses_frame("response.completed", response=responses_body(usage=usage, output=[{"type": "function_call", "arguments": "private"}]))], "unsupported_tool_call", False),
            ([created, text, responses_frame("response.completed", response=responses_body("different", usage))], "invalid_sse", False),
            ([created, responses_frame("response.completed", response=responses_body(usage=usage, model="wrong"))], "model_mismatch", False),
        )
        for chunks, code, unknown in cases:
            stream = ByteStream(chunks)
            calls, visible = [], []
            gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(
                200, stream=stream, headers={"content-type": "text/event-stream"})))
            with self.subTest(code=code, unknown=unknown), self.assertRaises(GatewayError) as caught:
                await self.complete(gateway, on_delta=visible.append)
            self.assertEqual((caught.exception.code, caught.exception.unknown_outcome), (code, unknown))
            self.assertEqual(caught.exception.usage, {"prompt_tokens": 3, "completion_tokens": 2})
            self.assertEqual(caught.exception.request_id, "responses-request")
            self.assertNotIn("private", str(caught.exception) + repr(visible))
            self.assertEqual(len(calls), 1)
            self.assertTrue(stream.closed)

    async def test_responses_sse_json_missing_usage_and_terminal_only_public_output(self):
        for include_delta in (True, False):
            terminal = responses_body('{"ok":true}')
            chunks = [responses_frame("response.output_text.delta", delta='{"ok":true}')] if include_delta else []
            chunks.append(responses_frame("response.completed", response=terminal))
            stream = ByteStream(chunks)
            visible, requests = [], []
            gateway = ModelGateway(settings(provider(api_type="responses")), transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(
                200, stream=stream, headers={"content-type": "text/event-stream"})))
            with self.subTest(include_delta=include_delta):
                result = await self.complete(gateway, on_delta=visible.append, json_mode=True)
                self.assertEqual(result.data, {"ok": True})
                self.assertEqual(visible, ['{"ok":true}'])
                self.assertIsNone(result.usage)
                self.assertTrue(result.cost_pending)
                self.assertEqual(json.loads(requests[0].content)["text"], {"format": {"type": "json_object"}})

    async def test_responses_wrong_protocol_configuration_fails_before_dns_or_send(self):
        for changes in ({"api_type": "other"}, {"api_type": "responses", "base_url": "https://relay.example/v1/chat/completions"},
                        {"base_url": "https://relay.example/v1/responses"}):
            with self.subTest(changes=changes), patch("app.services.model_gateway.pin_remote_url", side_effect=AssertionError("no DNS")):
                with self.assertRaises(GatewayError) as caught:
                    await self.complete(ModelGateway(settings(provider(**changes))))
                self.assertEqual(caught.exception.code, "invalid_configuration")
                self.assertFalse(caught.exception.request_sent)


class ContextTests(unittest.TestCase):
    def test_permission_required_and_only_selected_text_leaves_helper(self):
        for permission in (None, False, "pending", "unknown", "true", 1):
            with self.subTest(permission=permission), self.assertRaises(GatewayError) as caught:
                build_context([{"text": "approved math", "external_processing_permission": permission}])
            self.assertEqual(caught.exception.code, "external_processing_not_approved")
            self.assertFalse(caught.exception.request_sent)
        blocks = [{"id": "internal-id", "source_path": "private", "student_name": "private-name",
                   "owner_id": "private-account", "text": "x + x = 2*x", "external_processing_permission": True},
                  {"content": "1/2", "external_processing_permission": "approved"}]
        self.assertEqual(build_context(blocks), [{"reference": "B1", "text": "x + x = 2*x"}, {"reference": "B2", "text": "1/2"}])

    def test_minimal_context_has_explicit_fields_and_no_history_identity_or_reference_by_default(self):
        question = {"id": "q-internal", "type": "math", "stem": "Simplify 1/2", "answer": "0.5",
                    "reference_answer": "1/2", "options": ["1/2", "1"], "teacher_name": "private",
                    "students": ["private-roster"], "conversation": ["private-history"],
                    "rubric": [{"point": "fraction", "score_units": 100, "secret": "private"}]}
        result = build_minimal_context(question=question, answer="0.5", hint="Use fractions", learning_impression="Needs fraction practice",
                                       aggregate={"total_count": 4, "student_ids": ["private"], "messages": ["private"]})
        self.assertEqual(set(result["question"]), {"type", "stem", "options"})
        self.assertEqual(result["aggregate"], {"total_count": 4})
        self.assertNotIn("private", repr(result))
        self.assertNotIn("answer", result["question"])
        self.assertEqual(build_minimal_context(question=question, include_reference=True)["question"]["rubric"], [{"point": "fraction", "score_units": 100}])
        with self.assertRaises(TypeError):
            build_minimal_context(conversation=["must not be accepted"])
        with self.assertRaises(GatewayError):
            build_minimal_context(aggregate={"total_count": True})


if __name__ == "__main__":
    unittest.main()
