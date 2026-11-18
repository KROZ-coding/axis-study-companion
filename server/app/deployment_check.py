"""Offline configuration checks. No sockets, database opens, or paid calls.

A successful report validates configuration only. Budgets, provider billing,
HTTPS, device behavior and data permission still require acceptance evidence.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
CHANNELS = ("teacher", "student", "grading", "summary")
MODELS = {"teacher": "gpt-5.6-sol", "student": "gpt-5.6-sol",
          "grading": "gpt-5.6-terra", "summary": "gpt-5.6-terra"}


def check_settings(config: Any, *, production: bool = False, require_models: bool = False) -> dict:
    from .runtime import resolve_data_dir
    from .services.billing import PriceRegistry, PriceUnavailable
    from .services.model_gateway import GatewayError, ModelGateway

    checks = []

    def add(code: str, passed: bool, message: str, *, required: bool = True) -> None:
        checks.append({"code": code, "status": "pass" if passed else "fail" if required else "pending",
                       "message": message})

    add("formal_mode", not config.demo_enabled, "Formal deployment must disable STUDY_DEMO_ENABLED.")
    add("worker_enabled", config.worker_enabled, "The formal background worker must be enabled.")
    add("worker_concurrency", type(config.worker_concurrency) is int and 1 <= config.worker_concurrency <= 16,
        "Worker concurrency must be between 1 and 16.")
    if production:
        add("production_environment", config.app_env.strip().lower() == "production",
            "Set APP_ENV=production for this profile.")
        add("secure_cookie", config.cookie_secure is True, "HTTPS deployment requires COOKIE_SECURE=true.")
        hosts = config.allowed_hosts
        valid_hosts = bool(hosts) and all(
            isinstance(host, str) and host.strip() == host and host and "*" not in host
            and "/" not in host and ":" not in host and not any(char.isspace() for char in host)
            for host in hosts
        )
        add("explicit_hosts", valid_hosts, "ALLOWED_HOSTS must list exact hostnames, without schemes or ports.")
        origins_ok = True
        for origin in config.cors_origins:
            try:
                parsed = urlsplit(origin)
                origins_ok = origins_ok and (
                    parsed.scheme == "https" and parsed.hostname in hosts and parsed.path in {"", "/"}
                    and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                    and parsed.port in {None, 443}
                )
            except (ValueError, TypeError):
                origins_ok = False
        add("same_origin_configuration", origins_ok,
            "Leave CORS_ORIGINS empty or use an HTTPS origin on an allowed host; writes remain same-origin.")
        data_dir = resolve_data_dir(config)
        add("external_data_directory", Path(config.data_dir).is_absolute() and not data_dir.is_relative_to(ROOT),
            "Production DATA_DIR must be absolute and outside the source checkout.")

    gateway = ModelGateway(config)
    unmetered = getattr(config, "competition_unmetered", False) is True
    billing_mode = "unmetered_competition" if unmetered else "metered"
    add("billing_mode", True,
        "Competition mode records actual usage without monetary prices, caps or reservations."
        if unmetered else "Metered mode requires versioned prices and budget authorization.")
    prices = None
    if not unmetered:
        try:
            prices = PriceRegistry(config.llm_prices_json)
        except (PriceUnavailable, ValueError, TypeError):
            add("price_configuration", False, "LLM_PRICES_JSON must contain explicit versioned integer prices.")

    configured = bool(config.llm_providers)
    add("grouped_models", configured, "Configure all four explicit model channels before real-model acceptance.",
        required=require_models)
    if configured or require_models:
        add("reasoning_effort", config.llm_reasoning_effort == "high", "LLM_REASONING_EFFORT must be high.")
        if not unmetered:
            add("budget_currency", isinstance(config.budget_currency, str) and bool(config.budget_currency.strip()),
                "BUDGET_CURRENCY must name the agreed integer accounting unit.")
        for channel in CHANNELS:
            try:
                descriptor = gateway.describe(channel)
            except GatewayError:
                add(channel + "_route", False, "Configure the exact " + channel + " channel and " + MODELS[channel] + ".")
                continue
            add(channel + "_route", True, "Exact purpose route and request limits are configured.")
            candidates = [provider for provider in config.llm_providers if provider.channel == channel]
            for index, provider in enumerate(candidates, 1):
                prefix = channel + "_" + str(index)
                try:
                    url = gateway._url(provider)
                    host = urlsplit(url).hostname or ""
                    try:
                        address = ipaddress.ip_address(host)
                    except ValueError:
                        safe_host = host.lower() != "localhost" and not host.lower().endswith((".localhost", ".local"))
                    else:
                        safe_host = address.is_global and not address.is_multicast and not address.is_reserved
                    add(prefix + "_url", safe_host, "Provider URL must use HTTPS without credentials or a local address.")
                except (GatewayError, ValueError, TypeError):
                    add(prefix + "_url", False, "Provider URL must use HTTPS without userinfo, query or fragment.")
                if prices is not None:
                    try:
                        price = prices.get(provider.name, provider.model)
                        matching = price.currency == config.budget_currency
                    except PriceUnavailable:
                        matching = False
                    add(prefix + "_price", matching,
                        "Every configured provider/model needs a versioned price in BUDGET_CURRENCY.")
    if not unmetered:
        add("budget_limits", False, "Configure and verify project, teaching and applicable student daily limits before paid calls.",
            required=False)
    add("external_acceptance", False,
        "Real provider quality/bills, HTTPS, backup drills, devices and authorized competition data require separate evidence.",
        required=False)
    return {"schema": "axis.deployment-check.v1", "profile": "production" if production else "local",
            "billing_mode": billing_mode,
            "status": "configuration_valid" if not any(row["status"] == "fail" for row in checks) else "blocked",
            "network_requests": 0, "database_writes": 0, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "production"), default="local")
    parser.add_argument("--require-models", action="store_true", help="require all four routes and prices in metered mode")
    args = parser.parse_args(argv)
    try:
        from .config import Settings
        report = check_settings(Settings(), production=args.profile == "production", require_models=args.require_models)
    except (ValueError, TypeError, OverflowError):
        # Configuration errors can contain credential variable names or values.
        report = {"schema": "axis.deployment-check.v1", "profile": args.profile, "status": "blocked",
                  "network_requests": 0, "database_writes": 0,
                  "checks": [{"code": "configuration_invalid", "status": "fail",
                              "message": "Configuration could not be parsed; review field types and credential injection locally."}]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "configuration_valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
