"""Versioned integer-unit prices; an absent price never means a free model call."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from typing import Any, Mapping


class PriceUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    version: str
    currency: str
    input_units_per_million: int
    output_units_per_million: int

    def snapshot(self) -> dict:
        return asdict(self)

    def cost(self, usage: Mapping[str, Any] | None) -> int | None:
        if usage is None:
            return None
        values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        if any(type(value) is not int or value < 0 for value in values):
            return None
        amount = (Decimal(values[0]) * self.input_units_per_million
                  + Decimal(values[1]) * self.output_units_per_million) / Decimal(1_000_000)
        return int(amount.to_integral_value(rounding=ROUND_CEILING))

    def reserve(self, messages: list[dict[str, str]], max_output_tokens: int) -> int:
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        # A byte-count bound includes message framing; actual usage is settled later.
        input_bound = 64 + sum(len(message["content"].encode("utf-8")) + 32 for message in messages)
        return self.cost({"prompt_tokens": input_bound, "completion_tokens": max_output_tokens}) or 0

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "Price":
        strings = ("provider", "model", "version", "currency")
        rates = ("input_units_per_million", "output_units_per_million")
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in strings):
            raise PriceUnavailable("price identity and currency must be explicit")
        if any(type(value.get(key)) is not int or not 0 <= value[key] <= 10**15 for key in rates):
            raise PriceUnavailable("prices must be non-negative integer units per million tokens")
        return cls(**{key: value[key] for key in strings + rates})


class PriceRegistry:
    def __init__(self, raw_json: str = "") -> None:
        self._prices: dict[tuple[str, str], Price] = {}
        if not raw_json.strip():
            return
        try:
            values = json.loads(raw_json, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError) as error:
            raise PriceUnavailable("LLM_PRICES_JSON must be a JSON array of explicit prices") from error
        if not isinstance(values, list):
            raise PriceUnavailable("LLM_PRICES_JSON must be an array")
        for value in values:
            if not isinstance(value, dict):
                raise PriceUnavailable("each price must be an object")
            price = Price.from_snapshot(value)
            key = (price.provider, price.model)
            if key in self._prices:
                raise PriceUnavailable("a provider/model may have only one active price")
            self._prices[key] = price

    def get(self, provider: str, model: str) -> Price:
        try:
            return self._prices[(provider, model)]
        except KeyError:
            raise PriceUnavailable("configure a versioned price before enabling paid calls") from None
