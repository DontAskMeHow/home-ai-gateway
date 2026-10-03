"""Проверка лимитов ключа: rpm, tpm и дневной бюджет.

Лимиты применяются до отправки запроса в апстрим: при превышении возвращается 429
с хедерами `x-ratelimit-*`, запрос до модели не идёт.
"""

from __future__ import annotations

from . import store

KIND_ORDER = ("requests", "tokens", "budget")


def check(conn, key_name: str, conf: dict) -> tuple[bool, dict]:
    """Возвращает (пройден_ли, заголовки с остатками)."""
    requests, tokens = store.window(conn, key_name, 60)
    spent = store.day_cost(conn, key_name)

    rpm, tpm = int(conf["rpm"]), int(conf["tpm"])
    budget = float(conf["daily_budget_usd"])
    remaining_rpm = max(rpm - requests, 0)
    remaining_tpm = max(tpm - tokens, 0)
    remaining_budget = max(budget - spent, 0.0)

    headers = {
        "x-ratelimit-limit-requests": str(rpm),
        "x-ratelimit-remaining-requests": str(remaining_rpm),
        "x-ratelimit-limit-tokens": str(tpm),
        "x-ratelimit-remaining-tokens": str(remaining_tpm),
        "x-ratelimit-limit-budget-usd": f"{budget:.2f}",
        "x-ratelimit-remaining-budget-usd": f"{remaining_budget:.2f}",
        "x-ratelimit-reset": "60",
    }

    blocked = None
    if remaining_rpm <= 0:
        blocked = f"rpm={rpm} исчерпан"
    elif remaining_tpm <= 0:
        blocked = f"tpm={tpm} исчерпан"
    elif remaining_budget <= 0:
        blocked = f"дневной бюджет {budget:.2f} USD исчерпан"

    if blocked:
        headers["retry-after"] = "1"
        store.record(conn, None, key=key_name, model="-", prompt=0, cached=0,
                     completion=0, reasoning=0, latency_ms=0.0, status="rate_limited",
                     cost_tco=0.0, cost_api=0.0)
        return False, {"headers": headers, "reason": blocked}
    return True, {"headers": headers, "reason": ""}


def allowed_models(conf: dict) -> list[str]:
    return list(conf.get("models", []))
