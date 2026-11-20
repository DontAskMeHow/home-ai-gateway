"""Проверочный прогон шлюза против живых эндпоинтов.

    python scripts/smoke.py --base http://localhost:8090 --token sk-gateway-scripts

Проверяет: здоровье, список моделей, чат без стрима и со стримом, объём токенов,
учёт обрыва стрима клиентом, отклонение по rpm, наличие букетов в /metrics,
отклонение неизвестного ключа.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--token", default=None)
    parser.add_argument("--model", default="next")
    parser.add_argument("--responses-model", default=None,
                        help="алиас апстрима с /v1/responses (например, локальный мост)")
    args = parser.parse_args()

    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    # trust_env=False: системный прокси ноутбука режет обращения к localhost,
    # а шлюз живёт именно на localhost.
    client = httpx.Client(base_url=args.base, headers=headers, timeout=120,
                          trust_env=False)
    results: list[bool] = []

    # Ждём, пока сервер поднимется и первый проход пробинга зафиксирует апстримы.
    health: dict = {}
    for _ in range(40):
        try:
            health = json.loads(client.get("/healthz").text or "{}")
        except httpx.HTTPError:
            health = {}
        if any(health.get("upstreams", {}).values()):
            break
        time.sleep(1)

    body = {"model": args.model, "max_tokens": 160,
            "messages": [{"role": "user",
                          "content": "Столица Франции? Ответь одним словом."}]}

    health_raw = client.get("/healthz").text
    health = json.loads(health_raw) if health_raw else health
    results.append(check("healthz отдаёт статусы апстримов", bool(health.get("upstreams")),
                         json.dumps(health.get("upstreams", {}), ensure_ascii=False)))

    models = json.loads(client.get("/v1/models").text)
    results.append(check("v1/models непустой", len(models.get("data", [])) > 0,
                         f"{len(models.get('data', []))} записей"))

    if args.token:
        # Неизвестный/пустой ключ отклоняется до апстрима (401), а не метрится литералом.
        anon = httpx.Client(base_url=args.base, timeout=30, trust_env=False)
        bad = anon.post("/v1/chat/completions", json=dict(body, max_tokens=8),
                        headers={"Authorization": "Bearer sk-neizvestnyy-klyuch"})
        empty = anon.post("/v1/chat/completions", json=dict(body, max_tokens=8))
        bad_responses = anon.post("/v1/responses",
                                  json={"model": args.model, "input": "привет"})
        results.append(check("неизвестный ключ отклоняется 401", bad.status_code == 401,
                             f"HTTP {bad.status_code}"))
        results.append(check("запрос без ключа отклоняется 401", empty.status_code == 401,
                             f"HTTP {empty.status_code}"))
        results.append(check("responses без ключа отклоняется 401",
                             bad_responses.status_code == 401,
                             f"HTTP {bad_responses.status_code}"))

    direct = json.loads(client.post("/v1/chat/completions", json=body).text)
    message = (direct.get("choices") or [{}])[0].get("message") or {}
    text = message.get("content") or message.get("reasoning") \
        or message.get("reasoning_content") or ""
    usage = direct.get("usage") or {}
    results.append(check("чат без стрима", bool(text), repr(text)[:60]))
    results.append(check("usage заполнен", bool(usage.get("prompt_tokens")),
                         json.dumps(usage)[:120]))

    stream_body = dict(body, stream=True, stream_options={"include_usage": True})
    pieces: list[str] = []
    final_usage: dict = {}
    with client.stream("POST", "/v1/chat/completions", json=stream_body) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            obj = json.loads(payload)
            if isinstance(obj.get("usage"), dict):
                final_usage = obj["usage"]
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content") or delta.get("reasoning") or ""
                if piece:
                    pieces.append(piece)
    results.append(check("стриминг собрал ответ", bool("".join(pieces)),
                         repr("".join(pieces))[:60]))
    results.append(check("usage пришёл в финальном чанке", bool(final_usage),
                         json.dumps(final_usage)[:120]))

    # Обрыв стрима клиентом на середине: учёт обязан писать error:client_aborted
    # (v1.4 — раньше обрыв не записывался вовсе, слепая зона метрик).
    def status_counter(metric_text: str) -> float:
        prefix = (f'gateway_model_requests_total{{model="{args.model}",'
                  f'status="error:client_aborted"}}')
        for line in metric_text.splitlines():
            if line.startswith(prefix):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    aborted_before = status_counter(client.get("/metrics").text)
    with client.stream("POST", "/v1/chat/completions", json=stream_body) as resp:
        seen = 0
        for line in resp.iter_lines():
            if line.startswith("data:"):
                seen += 1
                if seen >= 2:
                    break
    time.sleep(3)
    aborted_after = status_counter(client.get("/metrics").text)
    results.append(check("обрыв стрима клиентом пишется в учёт",
                         aborted_after > aborted_before,
                         f"error:client_aborted {aborted_before:.0f} → "
                         f"{aborted_after:.0f} (чанков прочитано {seen})"))

    if args.responses_model:
        responses_body = {"model": args.responses_model, "max_output_tokens": 160,
                          "input": [{"role": "user", "content": [
                              {"type": "input_text",
                               "text": "Столица Франции? Ответь одним словом."}]}]}
        raw = client.post("/v1/responses", json=responses_body)
        data = json.loads(raw.text) if raw.status_code == 200 else {}
        texts = [part.get("text") for item in (data.get("output") or [])
                 if item.get("type") == "message"
                 for part in item.get("content", []) if "text" in (part.get("type") or "")]
        answer = " ".join(t for t in texts if t)
        results.append(check("responses API без стрима", bool(answer),
                             f"HTTP {raw.status_code} {repr(answer)[:60]}"))
        results.append(check("responses usage заполнен",
                             bool((data.get("usage") or {}).get("input_tokens")),
                             json.dumps(data.get("usage"))[:120]))

    limiter = httpx.Client(base_url=args.base, headers={"Authorization": "Bearer rpm1"},
                           timeout=60, trust_env=False)
    # Окно rpm считается за последние 60 с, поэтому пару запросов мог съесть
    # предыдущий прогон: повторяем, пока не поймает чистую пару 200 → 429.
    first = second = None
    for _ in range(6):
        first = limiter.post("/v1/chat/completions", json=dict(body, max_tokens=8)).status_code
        second = limiter.post("/v1/chat/completions", json=dict(body, max_tokens=8))
        if first == 200 and second.status_code == 429:
            break
        time.sleep(11)
    results.append(check("ключ с rpm=1 получает 429 на втором запросе",
                         first == 200 and second.status_code == 429,
                         f"{first} / {second.status_code}"))
    results.append(check("хедера лимитов на месте", second.headers.get(
        "x-ratelimit-remaining-requests") is not None, str(dict(second.headers))[:100]))

    metrics = client.get("/metrics").text
    needed = ("gateway_requests_total", "gateway_tokens_total", "gateway_cost_usd",
              "gateway_model_tokens_total", "gateway_key_requests_total",
              "gateway_key_tokens_total", "gateway_key_cost_usd",
              "gateway_key_model_tokens_total", "gateway_model_bucket_tokens_total",
              "gateway_model_duration_seconds", "gateway_key_duration_seconds",
              "gateway_gpt_week_tokens", "gateway_gpt_week_limit_tokens",
              "gateway_upstream_up", "gateway_request_duration_seconds_bucket")
    missing = [name for name in needed if name not in metrics]
    results.append(check("метрики содержат нужные серии", not missing,
                         "нет: " + ", ".join(missing) if missing else f"{len(metrics)} строк"))

    usage_rows = json.loads(client.get("/v1/usage", params={"since": 2}).text).get("rows") or []
    results.append(check("отчёт учёта непустой", len(usage_rows) > 0,
                         json.dumps(usage_rows[:1], ensure_ascii=False)[:160]))

    failed = results.count(False)
    print(f"\nпроверок: {len(results)}, провалено: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
