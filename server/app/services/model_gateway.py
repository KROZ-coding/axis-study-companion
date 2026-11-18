"""Single-attempt, purpose-routed model requests with explicit accounting metadata.

``complete`` is asynchronous. Supplying ``on_delta`` requests real SSE and calls
that callback (sync or async) with *only* public text deltas. The
caller owns authorization, conversation selection, durable call deduplication,
and billing. No request or hidden model reasoning is logged or retained here.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import inspect
import ipaddress
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import LLMProvider, Settings
from ..utils.url_security import pin_remote_url


_PURPOSES = {
    "teacher": ("teacher", "gpt-5.6-sol", "interaction"),
    "suggest": ("teacher", "gpt-5.6-sol", "interaction"),
    "generate": ("teacher", "gpt-5.6-sol", "batch"),
    "review": ("teacher", "gpt-5.6-sol", "batch"),
    "student": ("student", "gpt-5.6-sol", "interaction"),
    "hint": ("student", "gpt-5.6-sol", "interaction"),
    "grading": ("grading", "gpt-5.6-terra", "batch"),
    "summary": ("summary", "gpt-5.6-terra", "batch"),
}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_EVENT_CHARS = 256 * 1024
_MAX_INPUT_CHARS = 64_000
_MAX_CONTEXT_CHARS = 24_000
_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class GatewayResult:
    text: str
    data: dict[str, Any] | None
    usage: dict[str, int] | None
    request_id: str | None
    model: str
    reasoning_effort: str
    provider: str
    request_sent: bool
    cost_pending: bool


class GatewayError(Exception):
    """Sanitized failure. Unknown outcomes must be reconciled, never resent blindly."""

    def __init__(
        self, code: str, request_sent: bool = False, unknown_outcome: bool = False,
        usage: dict[str, int] | None = None, *, status_code: int = 503,
        request_id: str | None = None, model: str | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.request_sent = request_sent
        self.unknown_outcome = unknown_outcome
        self.usage = usage
        self.status_code = status_code
        self.request_id = request_id
        self.model = model
        self.reasoning_effort = "high"
        self.provider = provider
        self.cost_pending = request_sent and usage is None


@dataclass(slots=True)
class _CallState:
    sent: bool = False
    response_received: bool = False
    completed: bool = False
    usage: dict[str, int] | None = None
    request_id: str | None = None
    model: str | None = None
    provider: str | None = None

    def error(self, code: str, status: int = 502) -> GatewayError:
        return GatewayError(
            code, self.sent, self.sent and not self.completed, self.usage,
            status_code=status, request_id=self.request_id,
            model=self.model, provider=self.provider,
        )


def _strict_json(text: str) -> Any:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("non-finite JSON number")

    def number(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def _usage(value: Any, api_type: str = "chat_completions") -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    keys = ("input_tokens", "output_tokens") if api_type == "responses" else ("prompt_tokens", "completion_tokens")
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= 10**12 for key in keys):
        return None
    return dict(zip(("prompt_tokens", "completion_tokens"), (value[key] for key in keys)))


def _text(value: Any, *, maximum: int = _MAX_CONTEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise GatewayError("invalid_context", status_code=422)
    return value


def build_context(approved_blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Return only synthetic references and approved, already de-identified text.

    Input blocks need ``external_processing_permission is True`` or the exact
    string ``"approved"``, and ``text`` (or ``content``). Identity, source paths,
    uploader fields, original files, and all other metadata are deliberately
    excluded. Permission is not proof of de-identification: the authorized
    service must select and redact the text before calling this helper.
    """
    if not isinstance(approved_blocks, (list, tuple)) or len(approved_blocks) > 8:
        raise GatewayError("invalid_context", status_code=422)
    result = []
    length = 0
    for block in approved_blocks:
        if not isinstance(block, Mapping):
            raise GatewayError("invalid_context", status_code=422)
        permission = block.get("external_processing_permission")
        if permission is not True and not (type(permission) is str and permission == "approved"):
            raise GatewayError("external_processing_not_approved", status_code=403)
        text = _text(block.get("text", block.get("content")))
        length += len(text)
        if length > _MAX_CONTEXT_CHARS:
            raise GatewayError("context_too_large", status_code=422)
        result.append({"reference": f"B{len(result) + 1}", "text": text})
    return result


def _answer_context(value: Any) -> str | int | bool | list[int] | list[str]:
    if isinstance(value, str):
        return _text(value, maximum=8_000)
    if type(value) in (int, bool) and (type(value) is bool or abs(value) <= 10**12):
        return value
    if isinstance(value, list) and len(value) <= 32:
        if all(type(item) is int and 0 <= item < 32 for item in value):
            return list(value)
        if all(isinstance(item, str) and item.strip() and len(item) <= 1000 for item in value):
            return list(value)
    raise GatewayError("invalid_context", status_code=422)


def _question_requirements(question: Mapping[str, Any]) -> dict[str, Any]:
    """Copy bounded answer/mathematical requirements, never arbitrary metadata.

    Objects may name mathematical variables or explicit mathematical attributes.
    Unknown object keys fail closed instead of silently dropping a condition that
    might change grading. Text is selected/de-identified by the trusted caller,
    just like a question stem; this helper is not a free-text PII detector.
    """
    selected: dict[str, Any] = {}
    remaining = 128
    attributes = {
        "domain", "variables", "assumptions", "constraints", "type", "symbol",
        "expression", "relation", "value", "values", "min", "max", "lower", "upper",
        "inclusive", "lower_inclusive", "upper_inclusive", "excluded", "unit", "units",
        "real", "integer", "positive", "nonnegative", "negative", "nonzero", "finite",
    }

    def copy(value: Any, depth: int = 0) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 4:
            raise GatewayError("invalid_context", status_code=422)
        if value is None or type(value) is bool:
            return value
        if type(value) is int and abs(value) <= 10**12:
            return value
        if type(value) is float and math.isfinite(value) and abs(value) <= 10**12:
            return value
        if isinstance(value, str):
            return _text(value, maximum=2_000)
        if isinstance(value, (list, tuple)) and len(value) <= 32:
            return [copy(item, depth + 1) for item in value]
        if isinstance(value, Mapping) and len(value) <= 32:
            result = {}
            for key, item in value.items():
                if not isinstance(key, str) or (key not in attributes and not re.fullmatch(r"[A-Za-z](?:_[0-9]{1,2})?", key)):
                    raise GatewayError("invalid_context", status_code=422)
                result[key] = copy(item, depth + 1)
            return result
        raise GatewayError("invalid_context", status_code=422)

    if "answer_format" in question:
        selected["answer_format"] = _text(question["answer_format"], maximum=256)
    for key in ("domain", "variables", "assumptions", "constraints"):
        if key in question:
            # Empty/unrestricted markers are meaningful and must not be coerced
            # into different conditions. Nested strings still need real content.
            selected[key] = "" if question[key] == "" else copy(question[key])
    if len(json.dumps(selected, ensure_ascii=False, allow_nan=False)) > 8_000:
        raise GatewayError("context_too_large", status_code=422)
    return selected


def _question_maximum(question: Mapping[str, Any]) -> int | None:
    """Use exact integer score units, with bounded legacy point conversion."""
    units = []
    for key in ("score_units", "points_units", "max_score_units"):
        if key in question:
            value = question[key]
            if type(value) is not int or not 0 <= value <= 10**9:
                raise GatewayError("invalid_context", status_code=422)
            units.append(value)
    if units:
        if len(set(units)) != 1:
            raise GatewayError("invalid_context", status_code=422)
        return units[0]
    if "points" not in question:
        return None
    from decimal import Decimal, InvalidOperation
    from fractions import Fraction

    value = question["points"]
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 64:
        raise GatewayError("invalid_context", status_code=422)
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or abs(decimal.as_tuple().exponent) > 64:
            raise ValueError
        scaled = Fraction(decimal) * 100
        if scaled.denominator != 1 or not 0 <= scaled <= 10**9:
            raise ValueError
        return scaled.numerator
    except (InvalidOperation, ValueError, OverflowError):
        raise GatewayError("invalid_context", status_code=422) from None


def build_minimal_context(
    *, question: Mapping[str, Any] | None = None,
    approved_blocks: Sequence[Mapping[str, Any]] = (), answer: Any = None,
    hint: str | None = None, learning_impression: str | None = None,
    aggregate: Mapping[str, Any] | None = None, include_reference: bool = False,
) -> dict[str, Any]:
    """Explicit allowlist; never accepts users, rosters, or conversation history.

    Question fields: type, stem, options, knowledge_points, bounded answer format
    and mathematical requirements, and integer maximum score units. Mathematical
    conditions remain visible to independent review without exposing the key.
    References/rubric are opt-in for authorized grading/review callers. Aggregate
    fields are counts and integer score units only; user/class IDs are excluded.
    Free text must already be de-identified by the service that selected it.
    """
    context: dict[str, Any] = {}
    if type(include_reference) is not bool:
        raise GatewayError("invalid_context", status_code=422)
    if question is not None:
        if not isinstance(question, Mapping):
            raise GatewayError("invalid_context", status_code=422)
        selected: dict[str, Any] = {}
        for key in ("type", "stem"):
            if key in question:
                selected[key] = _text(question[key], maximum=8_000)
        for key in ("options", "knowledge_points"):
            if key in question:
                values = question[key]
                if not isinstance(values, (list, tuple)) or len(values) > 32:
                    raise GatewayError("invalid_context", status_code=422)
                selected[key] = [_text(value, maximum=2_000) for value in values]
        selected.update(_question_requirements(question))
        maximum = _question_maximum(question)
        if maximum is not None:
            selected["score_units"] = maximum
        if include_reference:
            for key in ("answer", "reference_answer"):
                if key in question and question[key] is not None:
                    selected[key] = _answer_context(question[key])
            if "rubric" in question:
                rubric = question["rubric"]
                if not isinstance(rubric, list) or len(rubric) > 32:
                    raise GatewayError("invalid_context", status_code=422)
                selected["rubric"] = []
                for point in rubric:
                    if not isinstance(point, Mapping) or type(point.get("score_units")) is not int or not 0 <= point["score_units"] <= 10**9:
                        raise GatewayError("invalid_context", status_code=422)
                    selected["rubric"].append({"point": _text(point.get("point"), maximum=1_000), "score_units": point["score_units"]})
        context["question"] = selected
    blocks = build_context(approved_blocks)
    if blocks:
        context["blocks"] = blocks
    if answer is not None:
        context["answer"] = _answer_context(answer)
    for key, value, maximum in (("hint", hint, 4_000), ("learning_impression", learning_impression, 2_000)):
        if value is not None:
            context[key] = _text(value, maximum=maximum)
    if aggregate is not None:
        if not isinstance(aggregate, Mapping):
            raise GatewayError("invalid_context", status_code=422)
        context["aggregate"] = {}
        for key in ("total_count", "submitted_count", "graded_count", "pending_count", "correct_count", "incorrect_count", "total_score_units", "max_score_units"):
            if key in aggregate:
                value = aggregate[key]
                if type(value) is not int or not 0 <= value <= 10**12:
                    raise GatewayError("invalid_context", status_code=422)
                context["aggregate"][key] = value
    if len(json.dumps(context, ensure_ascii=False)) > _MAX_INPUT_CHARS:
        raise GatewayError("context_too_large", status_code=422)
    return context


class ModelGateway:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._client_factory = client_factory or httpx.AsyncClient

    build_context = staticmethod(build_context)
    build_minimal_context = staticmethod(build_minimal_context)

    def provider_for(self, purpose: str) -> LLMProvider:
        """Select one exact channel. Failure never falls through to another key."""
        if not isinstance(purpose, str) or purpose not in _PURPOSES:
            raise GatewayError("invalid_purpose", status_code=422)
        channel, model, _ = _PURPOSES[purpose]
        providers = list(getattr(self.settings, "llm_providers", ()))
        candidates = [item for item in providers if item.channel == channel]
        if not candidates:
            raise GatewayError("not_configured", status_code=503)
        # Invalid entries in the selected group are configuration errors, not
        # invitations to silently use an alternate model or shared credential.
        if any(item.model != model for item in candidates):
            raise GatewayError("model_mismatch", status_code=503)
        if any(not item.name or not item.api_key or not item.base_url for item in candidates):
            raise GatewayError("invalid_configuration", status_code=503)
        if any(not isinstance(item.api_type, str) or item.api_type not in {"chat_completions", "responses"} for item in candidates):
            raise GatewayError("invalid_configuration", status_code=503)
        return min(candidates, key=lambda item: (item.priority, item.name))

    def describe(self, purpose: str) -> dict[str, Any]:
        """Read-only routing/limit preflight and explicit worker billing mode."""
        provider = self.provider_for(purpose)
        timeout, tokens = self._limits(purpose)
        unmetered = getattr(self.settings, "competition_unmetered", False) is True
        return {"provider": provider, "model": provider.model, "reasoning_effort": "high",
                "max_tokens": tokens, "timeout_seconds": timeout,
                "billing_mode": "unmetered_competition" if unmetered else "metered"}

    def configured_for(self, purpose: str) -> bool:
        try:
            self.provider_for(purpose)
        except GatewayError:
            return False
        return True

    @property
    def configured(self) -> bool:
        return any(self.configured_for(purpose) for purpose in _PURPOSES)

    def _limits(self, purpose: str) -> tuple[float, int]:
        group = _PURPOSES[purpose][2]
        timeout = getattr(self.settings, f"llm_{group}_timeout_seconds", 180.0 if group == "interaction" else 360.0)
        tokens = getattr(self.settings, f"llm_{group}_max_tokens", 4096 if group == "interaction" else 8192)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise GatewayError("invalid_configuration", status_code=503)
        if type(tokens) is not int or not 1 <= tokens <= 262144:
            raise GatewayError("invalid_configuration", status_code=503)
        return float(timeout), tokens

    @staticmethod
    def _messages(messages: Any) -> list[dict[str, str]]:
        if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 24:
            raise GatewayError("invalid_messages", status_code=422)
        result = []
        size = 0
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise GatewayError("invalid_messages", status_code=422)
            role, content = message["role"], message["content"]
            if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant"}:
                raise GatewayError("invalid_messages", status_code=422)
            if not isinstance(content, str) or not content.strip() or "\x00" in content:
                raise GatewayError("invalid_messages", status_code=422)
            size += len(content)
            if size > _MAX_INPUT_CHARS:
                raise GatewayError("input_too_large", status_code=422)
            result.append({"role": role, "content": content})
        return result

    @staticmethod
    def _url(provider: LLMProvider) -> str:
        base = provider.base_url.rstrip("/")
        try:
            parsed = urlsplit(base)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or any(char.isspace() for char in base):
                raise ValueError
            parsed.port
        except (TypeError, ValueError):
            raise GatewayError("unsafe_url", status_code=503) from None
        suffix = "/responses" if provider.api_type == "responses" else "/chat/completions"
        other = "/chat/completions" if provider.api_type == "responses" else "/responses"
        if parsed.path.endswith(other):
            raise GatewayError("invalid_configuration", status_code=503)
        return base if parsed.path.endswith(suffix) else base + suffix

    @staticmethod
    def _check_pin(original: str, pinned: str, host: str, sni: str) -> None:
        try:
            parsed, actual = urlsplit(original), urlsplit(pinned)
            address = ipaddress.ip_address(actual.hostname or "")
            if (
                actual.scheme != "https" or not address.is_global or address.is_multicast or address.is_reserved
                or actual.username is not None or actual.password is not None
                or actual.port != parsed.port or actual.path != parsed.path or actual.query != parsed.query
                or actual.fragment or host != parsed.netloc or sni != (parsed.hostname or "").encode("idna").decode("ascii")
            ):
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            raise GatewayError("unsafe_url", status_code=503) from None

    async def complete(
        self, purpose: str, messages: Sequence[dict[str, str]], *, call_id: str,
        json_mode: bool = False, on_delta: Callable[[str], Any] | None = None,
    ) -> GatewayResult:
        state = _CallState()
        try:
            provider = self.provider_for(purpose)
            state.model, state.provider = provider.model, provider.name
            validated = self._messages(messages)
            if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
                raise GatewayError("invalid_call_id", status_code=422)
            if type(json_mode) is not bool or (on_delta is not None and not callable(on_delta)):
                raise GatewayError("invalid_options", status_code=422)
            timeout, max_tokens = self._limits(purpose)
            original = self._url(provider)
            async with asyncio.timeout(timeout):
                try:
                    url, host, sni = await asyncio.to_thread(pin_remote_url, original)
                except (ValueError, OSError):
                    raise GatewayError("unsafe_url", status_code=503) from None
                self._check_pin(original, url, host, sni)
                if provider.api_type == "responses":
                    payload: dict[str, Any] = {
                        "model": provider.model, "input": validated, "max_output_tokens": max_tokens,
                        "reasoning": {"effort": "high"}, "store": False, "stream": on_delta is not None,
                    }
                    if json_mode:
                        payload["text"] = {"format": {"type": "json_object"}}
                else:
                    payload = {
                        "model": provider.model, "messages": validated, "temperature": 0.2 if json_mode else 0.3,
                        "max_tokens": max_tokens, "reasoning_effort": "high", "stream": on_delta is not None,
                    }
                    if json_mode:
                        payload["response_format"] = {"type": "json_object"}
                    if on_delta is not None:
                        payload["stream_options"] = {"include_usage": True}
                options = dict(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False, verify=True)
                if self._transport is not None:
                    options["transport"] = self._transport
                async with self._client_factory(**options) as client:
                    request = client.build_request(
                        "POST", url, json=payload,
                        headers={"Authorization": f"Bearer {provider.api_key}", "Host": host,
                                 "Idempotency-Key": call_id, "X-Client-Request-ID": call_id,
                                 "Accept": "text/event-stream" if on_delta is not None else "application/json"},
                    )
                    request.extensions["sni_hostname"] = sni
                    # Conservative while send is in progress; connect/pool
                    # failures below are known to precede application bytes.
                    state.sent = True
                    response = await client.send(request, stream=True)
                    state.response_received = True
                    state.request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    try:
                        if response.status_code != 200:
                            raw = await self._read(response, state)
                            state.completed = True
                            try:
                                body = _strict_json(raw)
                                if isinstance(body, dict):
                                    state.usage = _usage(body.get("usage"), provider.api_type)
                                    if not state.request_id and isinstance(body.get("id"), str):
                                        state.request_id = body["id"]
                            except (ValueError, RecursionError):
                                pass
                            code = "rate_limited" if response.status_code == 429 else "http_error"
                            raise state.error(code, 429 if response.status_code == 429 else 502)
                        if on_delta is not None:
                            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
                                await self._read(response, state)
                                state.completed = True
                                raise state.error("invalid_sse")
                            stream = self._responses_stream if provider.api_type == "responses" else self._stream
                            text = await stream(response, state, on_delta)
                        else:
                            raw = await self._read(response, state)
                            if provider.api_type != "responses":
                                state.completed = True
                            try:
                                body = _strict_json(raw)
                            except (ValueError, RecursionError):
                                raise state.error("invalid_response") from None
                            content = self._responses_content if provider.api_type == "responses" else self._content
                            text = content(body, state)
                    finally:
                        await response.aclose()
            data = None
            if json_mode:
                try:
                    data = _strict_json(text)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, RecursionError):
                    raise state.error("invalid_json") from None
            return GatewayResult(text, data, state.usage, state.request_id, provider.model, "high", provider.name, True, state.usage is None)
        except GatewayError as error:
            # Preserve deliberate preflight errors while enriching them with
            # safe metadata. Never include the provider's error body or key.
            if state.sent:
                error.request_sent = True
                error.unknown_outcome = not state.completed
                error.usage = state.usage
                error.cost_pending = state.usage is None
            error.request_id = state.request_id
            error.model = state.model
            error.provider = state.provider
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            if not state.response_received:
                state.sent = False
            raise state.error("timeout" if isinstance(error, httpx.TimeoutException) else "transport_error", 503) from None
        except (TimeoutError, httpx.TimeoutException):
            raise state.error("timeout", 504) from None
        except asyncio.CancelledError:
            raise state.error("cancelled", 499) from None
        except httpx.HTTPError:
            raise state.error("transport_error", 502) from None
        except (TypeError, ValueError, UnicodeError):
            # Header/URL encoding and injected-client validation can fail
            # before send. Preserve the same explicit accounting contract.
            raise state.error("invalid_request" if not state.sent else "invalid_response", 422 if not state.sent else 502) from None

    @staticmethod
    async def _read(response: httpx.Response, state: _CallState) -> str:
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes(chunk_size=16_384):
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise state.error("response_too_large")
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8-sig")
        except UnicodeError:
            state.completed = True
            raise state.error("invalid_response") from None

    @staticmethod
    def _metadata(body: Any, state: _CallState) -> None:
        if not isinstance(body, dict):
            raise state.error("invalid_response")
        if body.get("usage") is not None:
            state.usage = _usage(body["usage"])
        if not state.request_id and isinstance(body.get("id"), str):
            state.request_id = body["id"]
        if "model" in body and body["model"] != state.model:
            raise state.error("model_mismatch")
        if body.get("error") is not None:
            state.completed = True
            raise state.error("upstream_error")

    @staticmethod
    def _finish(reason: Any, state: _CallState) -> None:
        if reason is None:
            return
        if isinstance(reason, str) and reason in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
            # A terminal provider acknowledgement is a known outcome even
            # when its content is unusable or the following usage frame is lost.
            state.completed = True
        if reason == "stop":
            return
        code = {"length": "output_truncated", "content_filter": "content_filtered", "tool_calls": "unsupported_tool_call", "function_call": "unsupported_tool_call"}.get(reason, "invalid_response") if isinstance(reason, str) else "invalid_response"
        raise state.error(code)

    @classmethod
    def _content(cls, body: Any, state: _CallState) -> str:
        cls._metadata(body, state)
        try:
            choices = body["choices"]
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError
            choice = choices[0]
            cls._finish(choice.get("finish_reason"), state)
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError
            if message.get("tool_calls") or message.get("function_call"):
                raise state.error("unsupported_tool_call")
            text = message["content"]
            if isinstance(text, list):
                text = "".join(part["text"] for part in text if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            return text
        except (KeyError, TypeError, ValueError):
            raise state.error("invalid_response") from None

    @staticmethod
    def _responses_metadata(body: Any, state: _CallState) -> None:
        if not isinstance(body, dict):
            raise state.error("invalid_response")
        # Capture accounting and terminal acknowledgement before interpreting
        # output, model mismatches or upstream errors.
        usage = _usage(body.get("usage"), "responses")
        if usage is not None:
            state.usage = usage
        if not state.request_id and isinstance(body.get("id"), str):
            state.request_id = body["id"]
        status = body.get("status")
        if isinstance(status, str) and status in {"completed", "incomplete", "failed", "cancelled"}:
            state.completed = True
        if body.get("error") is not None:
            state.completed = True
        if "model" in body and body["model"] != state.model:
            raise state.error("model_mismatch")
        if body.get("error") is not None or status == "failed":
            raise state.error("upstream_error")
        if status == "cancelled":
            raise state.error("cancelled")
        if status == "incomplete":
            details = body.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            code = {"max_output_tokens": "output_truncated", "content_filter": "content_filtered"}.get(reason, "output_incomplete") if isinstance(reason, str) else "output_incomplete"
            raise state.error(code)

    @classmethod
    def _responses_content(cls, body: Any, state: _CallState) -> str:
        cls._responses_metadata(body, state)
        if body.get("status") != "completed":
            raise state.error("incomplete_response")
        output = body.get("output")
        if not isinstance(output, list):
            raise state.error("invalid_response")
        parts = []
        for item in output:
            if not isinstance(item, dict):
                raise state.error("invalid_response")
            kind = item.get("type")
            if kind == "reasoning":
                continue
            if isinstance(kind, str) and (kind.endswith("_call") or kind.startswith("mcp_")):
                raise state.error("unsupported_tool_call")
            if kind != "message" or item.get("role") != "assistant":
                raise state.error("invalid_response")
            if item.get("status", "completed") != "completed":
                raise state.error("output_incomplete")
            content = item.get("content")
            if not isinstance(content, list):
                raise state.error("invalid_response")
            for part in content:
                if not isinstance(part, dict):
                    raise state.error("invalid_response")
                if part.get("type") == "refusal":
                    raise state.error("refusal")
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise state.error("invalid_response")
                parts.append(part["text"])
        text = "".join(parts)
        if not text.strip():
            raise state.error("invalid_response")
        return text

    @staticmethod
    async def _events(response: httpx.Response, state: _CallState):
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        buffer = ""
        data: list[str] = []
        size = event_size = 0
        try:
            # Do not buffer to a fixed chunk size: a small live delta must be
            # delivered before the upstream sends its next chunk.
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise state.error("response_too_large")
                buffer += decoder.decode(chunk)
                while match := re.search(r"\r\n|\r|\n", buffer):
                    if match.group() == "\r" and match.end() == len(buffer):
                        break
                    line, buffer = buffer[:match.start()], buffer[match.end():]
                    event_size += len(line)
                    if event_size > _MAX_EVENT_CHARS:
                        raise state.error("response_too_large")
                    if not line:
                        if data:
                            yield "\n".join(data)
                        data, event_size = [], 0
                    elif line.startswith("data:"):
                        value = line[5:]
                        data.append(value[1:] if value.startswith(" ") else value)
                if len(buffer) + event_size > _MAX_EVENT_CHARS:
                    raise state.error("response_too_large")
            buffer += decoder.decode(b"", final=True)
        except UnicodeError:
            raise state.error("invalid_sse") from None
        # Some compatible servers omit the final empty line. The protocol
        # consumer still requires an explicit terminal acknowledgement.
        if buffer.endswith("\r"):
            buffer = buffer[:-1]
        if buffer.startswith("data:"):
            value = buffer[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        if data:
            yield "\n".join(data)

    @classmethod
    async def _responses_stream(cls, response: httpx.Response, state: _CallState, on_delta: Callable[[str], Any]) -> str:
        parts: list[str] = []
        blocked: str | None = None

        async def emit(text: str) -> None:
            try:
                result = on_delta(text)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                raise state.error("delta_callback_error") from None

        async for event in cls._events(response, state):
            # Responses terminates with response.completed/failed/incomplete,
            # never with Chat Completions' [DONE] marker alone.
            if event == "[DONE]":
                raise state.error("incomplete_stream")
            try:
                body = _strict_json(event)
            except (ValueError, RecursionError):
                raise state.error("invalid_sse") from None
            if not isinstance(body, dict) or not isinstance(body.get("type"), str):
                raise state.error("invalid_sse")
            kind = body["type"]
            if kind in {"response.completed", "response.failed", "response.incomplete"}:
                terminal = body.get("response")
                if not isinstance(terminal, dict):
                    raise state.error("invalid_sse")
                state.completed = True
                cls._responses_metadata(terminal, state)
                if terminal.get("status") != kind.removeprefix("response."):
                    raise state.error("invalid_sse")
                text = cls._responses_content(terminal, state)
                if blocked:
                    raise state.error(blocked)
                if parts and "".join(parts) != text:
                    raise state.error("invalid_sse")
                if not parts:
                    await emit(text)
                return text
            nested = body.get("response")
            if nested is not None:
                cls._responses_metadata(nested, state)
                if state.completed:
                    raise state.error("invalid_sse")
            if kind == "error":
                state.completed = True
                raise state.error("upstream_error")
            if kind in {"response.output_item.added", "response.output_item.done"}:
                item = body.get("item")
                if not isinstance(item, dict):
                    raise state.error("invalid_sse")
                item_type = item.get("type")
                if isinstance(item_type, str) and (item_type.endswith("_call") or item_type.startswith("mcp_")):
                    blocked = "unsupported_tool_call"
                elif item_type not in {"message", "reasoning"}:
                    raise state.error("invalid_sse")
                elif item_type == "message" and item.get("role") != "assistant":
                    raise state.error("invalid_sse")
            elif kind in {"response.refusal.delta", "response.refusal.done"}:
                blocked = "refusal"
            elif kind in {"response.content_part.added", "response.content_part.done"}:
                part = body.get("part")
                if not isinstance(part, dict):
                    raise state.error("invalid_sse")
                if part.get("type") == "refusal":
                    blocked = "refusal"
            elif kind == "response.output_text.delta":
                delta = body.get("delta")
                if not isinstance(delta, str):
                    raise state.error("invalid_sse")
                if delta and not blocked:
                    parts.append(delta)
                    await emit(delta)
            # Reasoning, summary and lifecycle events never reach on_delta.
        raise state.error("incomplete_stream")

    @classmethod
    async def _stream(cls, response: httpx.Response, state: _CallState, on_delta: Callable[[str], Any]) -> str:
        parts = []
        finished = False
        async for event in cls._events(response, state):
            if event == "[DONE]":
                state.completed = True
                if not finished or not parts or not "".join(parts).strip():
                    raise state.error("invalid_sse")
                return "".join(parts)
            try:
                body = _strict_json(event)
            except (ValueError, RecursionError):
                raise state.error("invalid_sse") from None
            cls._metadata(body, state)
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise state.error("invalid_sse")
            if not choices:
                if body.get("usage") is None:
                    raise state.error("invalid_sse")
                continue
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index", 0) != 0 or finished:
                raise state.error("invalid_sse")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant":
                raise state.error("invalid_sse")
            if delta.get("tool_calls") or delta.get("function_call"):
                raise state.error("unsupported_tool_call")
            cls._finish(choice.get("finish_reason"), state)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise state.error("invalid_sse")
                if content:
                    parts.append(content)
                    try:
                        result = on_delta(content)
                        if inspect.isawaitable(result):
                            await result
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise state.error("delta_callback_error") from None
            finished = choice.get("finish_reason") == "stop"
        raise state.error("incomplete_stream")
