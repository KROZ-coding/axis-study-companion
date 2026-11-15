"""OpenAI-compatible adapter using upstream settings and response conventions.

The original pool implementation is not carried over: this local adapter keeps
channel selection and ordered failover, with validated public endpoints only.
"""

import asyncio
import json
from typing import Any

import httpx

from ..config import LLMProvider, Settings
from ..utils.url_security import pin_remote_url


def _extract_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class LLMClient:
    def __init__(self, settings: Settings, channel: str = "student"):
        self.settings = settings
        self.channel = channel
        self._reset()

    def _reset(self) -> None:
        self.last_error = None
        self.last_model = None
        self.last_provider = None
        self.last_request_id = None
        self.last_request_sent = False
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def _providers(self) -> list[LLMProvider]:
        providers = list(self.settings.llm_providers)
        if not providers and self.settings.llm_configured:
            providers = [LLMProvider(
                "legacy-shared", "shared", self.settings.llm_base_url, self.settings.llm_api_key,
                self.settings.llm_model, 1, 0, 100, "legacy-shared",
            )]
        channels = {self.channel, "shared"}
        if self.channel == "grading" and not any(item.channel == "grading" for item in providers):
            channels.add("student")
        return sorted((item for item in providers if item.channel in channels), key=lambda item: (item.priority, item.name))

    @property
    def configured(self) -> bool:
        return bool(self._providers())

    def _url(self, provider: LLMProvider) -> str:
        base = provider.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    def _payload(self, provider: LLMProvider, messages: list[dict[str, str]], temperature: float, json_mode: bool) -> dict:
        payload = {"model": provider.model, "messages": messages, "temperature": temperature,
                   "max_tokens": self.settings.llm_max_tokens or 1200}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.llm_reasoning_effort:
            payload["reasoning_effort"] = self.settings.llm_reasoning_effort
        return payload

    def _content(self, response: httpx.Response, provider: LLMProvider) -> str | None:
        self.last_request_id = response.headers.get("x-request-id")
        if response.status_code != 200:
            self.last_error = "rate_limited" if response.status_code == 429 else "http_error"
            return None
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            usage = data.get("usage") or {}
            self.last_usage = {key: max(0, int(usage.get(key) or 0)) for key in self.last_usage}
        except (KeyError, IndexError, TypeError, ValueError):
            self.last_error = "invalid_response"
            return None
        self.last_model = provider.model
        self.last_provider = provider.name
        self.last_error = None
        return text

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.3, json_mode: bool = False) -> str | None:
        self._reset()
        providers = self._providers()
        if not providers:
            self.last_error = "not_configured"
            return None
        for provider in providers[:self.settings.llm_max_attempts]:
            try:
                url, host_header, sni_hostname = pin_remote_url(self._url(provider))
                with httpx.Client(timeout=self.settings.llm_timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    self.last_request_sent = True
                    request = client.build_request(
                        "POST", url, headers={"Authorization": f"Bearer {provider.api_key}", "Host": host_header},
                        json=self._payload(provider, messages, temperature, json_mode),
                    )
                    request.extensions["sni_hostname"] = sni_hostname
                    response = client.send(request)
                text = self._content(response, provider)
                if text is not None:
                    return text
            except ValueError:
                self.last_error = "unsafe_url"
            except httpx.HTTPError:
                self.last_error = "transport_error"
        return None

    async def complete_async(self, messages: list[dict[str, str]], temperature: float = 0.3, json_mode: bool = False) -> str | None:
        self._reset()
        providers = self._providers()
        if not providers:
            self.last_error = "not_configured"
            return None
        for provider in providers[:self.settings.llm_max_attempts]:
            try:
                url, host_header, sni_hostname = await asyncio.to_thread(pin_remote_url, self._url(provider))
                async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    self.last_request_sent = True
                    request = client.build_request(
                        "POST", url, headers={"Authorization": f"Bearer {provider.api_key}", "Host": host_header},
                        json=self._payload(provider, messages, temperature, json_mode),
                    )
                    request.extensions["sni_hostname"] = sni_hostname
                    response = await client.send(request)
                text = self._content(response, provider)
                if text is not None:
                    return text
            except ValueError:
                self.last_error = "unsafe_url"
            except httpx.HTTPError:
                self.last_error = "transport_error"
        return None

    def complete_json(self, messages: list[dict[str, str]], temperature: float = 0.2) -> dict | None:
        text = self.complete(messages, temperature, json_mode=True)
        result = _extract_json(text) if text else None
        if text and result is None:
            self.last_error = "invalid_json"
        return result

    async def complete_json_async(self, messages: list[dict[str, str]], temperature: float = 0.2) -> dict | None:
        text = await self.complete_async(messages, temperature, json_mode=True)
        result = _extract_json(text) if text else None
        if text and result is None:
            self.last_error = "invalid_json"
        return result

    async def stream_json_async(self, messages: list[dict[str, str]], temperature: float = 0.2, on_delta=None) -> dict | None:
        # Compatibility with retained upstream services; this adapter is non-streaming.
        return await self.complete_json_async(messages, temperature)
