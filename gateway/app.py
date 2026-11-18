"""Домашний AI-шлюз: OpenAI-совместимый прокси над self-hosted парком моделей.

Слой отвечает за:
  - один вход для всех потребителей (чат, список моделей, здоровье, метрики, учёт);
  - маршрутизацию по алиасам с таймаутами, ретраями и запасными апстримами;
  - авторизацию по виртуальным ключам: неизвестный или пустой — 401;
  - учёт токенов по четырём букетам и стоимости по двум таблицам цен;
  - лимиты на ключ (rpm / tpm / дневной бюджет) с настоящим отклонением запроса;
  - честную деградацию: не стало апстрима — 503, без тихих повторов длинных запросов.

Адреса и токены живут в конфиге (`config.local.yaml`), здесь их нет.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config as cfg_mod
from . import limits as limits_mod
from . import metrics as metrics_mod
from . import store

DEFAULT_KEY = "default"


def _auth_key(request: Request, cfg: dict) -> str | None:
    """Имя ключа из Authorization; None — неизвестный или отсутствующий токен.

    Конфиг без ключей (локальные пробы) сохраняет прежнее поведение: default.
    """
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not cfg["keys"]:
        return DEFAULT_KEY
    for name, conf in cfg["keys"].items():
        expected = conf.get("token") or name
        if token and token == expected:
            return name
    return None


def _key_conf(cfg: dict, key_name: str) -> dict:
    if key_name in cfg["keys"]:
        return cfg["keys"][key_name]
    return {"models": list(cfg["upstreams"]), "rpm": 60, "tpm": 200_000,
            "daily_budget_usd": 10.0}


def _buckets(usage: dict, answer: str, prompt_text: str) -> tuple[int, int, int, int]:
    """Четыре букета из usage; при отсутствии usage — оценка по длине текста."""
    details = usage.get("prompt_tokens_details") or {}
    out_details = usage.get("completion_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0)
    reasoning = int(out_details.get("reasoning_tokens") or 0)
    if not prompt:
        prompt = store.estimate_tokens(prompt_text)
    if not completion:
        completion = store.estimate_tokens(answer)
    return prompt, cached, completion, reasoning


def _answer_text(payload: dict) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    parts = [message.get("content") or "",
             message.get("reasoning") or message.get("reasoning_content") or ""]
    return " ".join(part for part in parts if part)


def _prompt_text(payload: dict) -> str:
    parts = [str(message.get("content") or "")
             for message in payload.get("messages") or [] if isinstance(message, dict)]
    return " ".join(parts)


async def _body_json(request: Request) -> dict:
    """Толергантный разбор тела: консоль Windows умеет присылать OEM-кодировку."""
    raw = await request.body()
    for encoding in ("utf-8", "cp1251", "cp866"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {}


def _usage_from_sse(text: str) -> tuple[dict, str]:
    """Usage из финального чанка и склеенный текст ответа SSE-потока."""
    usage: dict = {}
    pieces: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            piece_source = choice.get("delta") or choice.get("message") or {}
            piece = (piece_source.get("content")
                     or piece_source.get("reasoning")
                     or piece_source.get("reasoning_content") or "")
            if piece:
                pieces.append(piece)
    return usage, "".join(pieces)


def _responses_prompt_text(payload: dict) -> str:
    """Текст промпта из тела responses API (`input` — строка или список частей)."""
    source = payload.get("input")
    if isinstance(source, str):
        return source
    parts: list[str] = []
    for item in source or []:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(str(part.get("text") or "")
                             for part in content if isinstance(part, dict))
        else:
            parts.append(str(item))
    return " ".join(part for part in parts if part)


def _responses_answer_text(payload: dict) -> str:
    """Текст ответа responses API (для оценки токенов при пустом usage)."""
    pieces: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and "text" in (part.get("type") or ""):
                pieces.append(str(part.get("text") or ""))
    return " ".join(pieces)


def _buckets_responses(usage: dict, answer: str,
                       prompt_text: str) -> tuple[int, int, int, int]:
    """Букеты из usage responses API: input/output-токены и их details."""
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = int(in_details.get("cached_tokens") or 0)
    reasoning = int(out_details.get("reasoning_tokens") or 0)
    if not prompt:
        prompt = store.estimate_tokens(prompt_text)
    if not completion:
        completion = store.estimate_tokens(answer)
    return prompt, cached, completion, reasoning


def _usage_from_sse_responses(text: str) -> tuple[dict, str]:
    """Usage из события response.completed и текст из дельт SSE responses API."""
    usage: dict = {}
    pieces: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        event = obj.get("type") or ""
        if event == "response.completed":
            response_obj = obj.get("response") or {}
            if isinstance(response_obj.get("usage"), dict):
                usage = response_obj["usage"]
        if event == "response.output_text.delta":
            delta = obj.get("delta")
            if delta:
                pieces.append(str(delta))
    return usage, "".join(pieces)


async def _probe_loop(client: httpx.AsyncClient, cfg: dict, health: dict,
                      errors: dict) -> None:
    interval = float(cfg.get("probe_interval_s", 30))
    while True:
        for name, up in cfg["upstreams"].items():
            url = up["base_url"].rstrip("/") + "/models"
            headers = {}
            if up.get("api_key"):
                headers["Authorization"] = f"Bearer {up['api_key']}"
            try:
                resp = await client.get(url, headers=headers, timeout=10.0)
                health[name] = resp.status_code < 400
                errors[name] = "" if health[name] else f"HTTP {resp.status_code}"
            except Exception as exc:
                health[name] = False
                errors[name] = f"{type(exc).__name__}: {exc}"[:160]
        await asyncio.sleep(interval)


def create_app(config_path: str | None = None) -> FastAPI:
    cfg = cfg_mod.load(config_path)
    conn = store.connect(cfg.get("db", "./data/gateway.db"))
    jsonl_path = cfg.get("log", "./data/usage.jsonl")
    health: dict[str, bool] = {name: False for name in cfg["upstreams"]}
    errors: dict[str, str] = {name: "не опрослен" for name in cfg["upstreams"]}
    samples: deque[float] = deque(maxlen=2000)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # До апстримов по умолчанию идём напрямую: системный прокси рабочих машин
        # часто не пропускает внутренние адреса. Включается ключом `trust_env: true`.
        app.state.client = httpx.AsyncClient(trust_env=bool(cfg.get("trust_env", False)))
        task = asyncio.create_task(_probe_loop(app.state.client, cfg, health, errors))
        store.prune(conn, int(cfg.get("retention_days", 90)))
        try:
            yield
        finally:
            task.cancel()
            await app.state.client.aclose()
            conn.close()

    app = FastAPI(title="home ai gateway", lifespan=lifespan)

    def chain_of(alias: str) -> list[str]:
        names = [alias]
        for extra in cfg["upstreams"][alias].get("fallback") or []:
            if extra not in names:
                names.append(extra)
        return names

    async def call_upstream(client: httpx.AsyncClient, name: str, payload: dict,
                            stream: bool, path: str = "/chat/completions"):
        up = cfg["upstreams"][name]
        url = up["base_url"].rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if up.get("api_key"):
            headers["Authorization"] = f"Bearer {up['api_key']}"
        body = dict(payload)
        body["model"] = up["model"]
        timeout = httpx.Timeout(float(up["timeout_s"]), connect=10.0)

        last_error = "апстрим не настроен"
        for _ in range(int(up["retries"]) + 1):
            try:
                if stream:
                    request = client.build_request("POST", url, json=body,
                                                   headers=headers, timeout=timeout)
                    resp = await client.send(request, stream=True)
                else:
                    resp = await client.post(url, json=body, headers=headers,
                                             timeout=timeout)
                if resp.status_code >= 500:
                    last_error = f"{name}: HTTP {resp.status_code}"
                    if stream:
                        await resp.aclose()
                    continue
                return resp
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"{name}: {type(exc).__name__}"
        raise RuntimeError(last_error)

    def finish(key_name: str, upstream: str, prompt: int, cached: int, completion: int,
               reasoning: int, started: float, status: str,
               prompt_text: str = "") -> dict:
        up = cfg["upstreams"][upstream]
        cost_tco = store.compute_cost(up["price"], prompt, cached, completion)
        cost_api = store.compute_cost(up["api_price"], prompt, cached, completion)
        latency_ms = (time.perf_counter() - started) * 1000
        samples.append(latency_ms / 1000)
        info = None
        if prompt_text:
            digest = hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()
            info = {"prompt_chars": len(prompt_text), "prompt_hash": digest[:12]}
        return store.record(conn, jsonl_path, key=key_name, model=upstream,
                            prompt=prompt, cached=cached, completion=completion,
                            reasoning=reasoning, latency_ms=latency_ms, status=status,
                            cost_tco=cost_tco, cost_api=cost_api, prompt_info=info)

    @app.get("/healthz")
    async def healthz():
        return {"config": os.path.basename(str(cfg.get("_path", ""))),
                "upstreams": health, "probe_errors": errors,
                "totals": store.totals(conn)}

    @app.get("/v1/models")
    async def models():
        data = [{"id": up["model"], "object": "model", "owned_by": name}
                for name, up in cfg["upstreams"].items()]
        data += [{"id": name, "object": "model", "owned_by": "alias"}
                 for name in cfg["upstreams"]]
        return {"object": "list", "data": data}

    @app.get("/v1/usage")
    async def usage(since: int = 7, key: str | None = None):
        return {"rows": store.report(conn, since_days=since, key=key)}

    @app.get("/metrics")
    async def metrics():
        text = metrics_mod.render(conn, health, list(samples))
        return StreamingResponse(iter([text]),
                                 media_type="text/plain; version=0.0.4")

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await _body_json(request)
        if not payload.get("messages"):
            return JSONResponse({"error": {"message": "нужны сообщения в `messages`",
                                           "type": "BadRequestError"}}, status_code=400)
        key_name = _auth_key(request, cfg)
        if key_name is None:
            return JSONResponse({"error": {"message": "неизвестный или отсутствующий ключ",
                                           "type": "AuthenticationError"}},
                                status_code=401)
        key_conf = _key_conf(cfg, key_name)

        alias = cfg_mod.resolve_upstream(cfg, payload.get("model"))
        if alias is None:
            return JSONResponse({"error": {"message": f"неизвестная модель: "
                                                       f"{payload.get('model')}",
                                           "type": "BadRequestError"}}, status_code=400)
        if key_conf.get("models") and alias not in key_conf["models"]:
            return JSONResponse({"error": {"message": f"ключу {key_name} недоступна "
                                                       f"модель {alias}",
                                           "type": "ForbiddenError"}}, status_code=403)

        ok, verdict = limits_mod.check(conn, key_name, key_conf)
        headers = dict(verdict["headers"])
        if not ok:
            return JSONResponse({"error": {"message": verdict["reason"],
                                           "type": "RateLimitError"}},
                                status_code=429, headers=headers)

        chain = chain_of(alias)
        stream = bool(payload.get("stream"))
        prompt_text = _prompt_text(payload)
        client: httpx.AsyncClient = request.app.state.client
        started = time.perf_counter()

        for name in chain:
            try:
                resp = await call_upstream(client, name, payload, stream)
            except RuntimeError as exc:
                finish(key_name, name, 0, 0, 0, 0, started, f"error:{exc}"[:80],
                       prompt_text)
                if name is chain[-1]:
                    return JSONResponse({"error": {"message": str(exc),
                                                   "type": "UpstreamUnavailable"}},
                                        status_code=503, headers=headers)
                continue

            if stream:
                headers["x-gateway-upstream"] = name

                async def relay(response=resp, upstream=name):
                    collected: list[str] = []
                    try:
                        async for chunk in response.aiter_text():
                            collected.append(chunk)
                            yield chunk
                    finally:
                        await response.aclose()
                    usage_obj, answer = _usage_from_sse("".join(collected))
                    prompt, cached, completion, reasoning = _buckets(
                        usage_obj, answer, prompt_text)
                    finish(key_name, upstream, prompt, cached, completion, reasoning,
                           started, "ok", prompt_text)

                return StreamingResponse(relay(), media_type="text/event-stream",
                                         headers=headers)

            data = resp.json()
            usage_obj = data.get("usage") or {}
            answer = _answer_text(data)
            prompt, cached, completion, reasoning = _buckets(usage_obj, answer,
                                                            prompt_text)
            finish(key_name, name, prompt, cached, completion, reasoning, started, "ok",
                   prompt_text)
            headers.update(limits_mod.check(conn, key_name, key_conf)[1]["headers"])
            headers["x-gateway-upstream"] = name
            return JSONResponse(data, headers=headers)

        return JSONResponse({"error": {"message": "все апстримы недоступны",
                                       "type": "UpstreamUnavailable"}},
                            status_code=503, headers=headers)

    @app.post("/v1/responses")
    async def responses(request: Request):
        payload = await _body_json(request)
        if not payload.get("model") or "input" not in payload:
            return JSONResponse({"error": {"message": "нужны `model` и `input`",
                                           "type": "BadRequestError"}}, status_code=400)
        key_name = _auth_key(request, cfg)
        if key_name is None:
            return JSONResponse({"error": {"message": "неизвестный или отсутствующий ключ",
                                           "type": "AuthenticationError"}},
                                status_code=401)
        key_conf = _key_conf(cfg, key_name)

        alias = cfg_mod.resolve_upstream(cfg, payload.get("model"))
        if alias is None:
            return JSONResponse({"error": {"message": f"неизвестная модель: "
                                                       f"{payload.get('model')}",
                                           "type": "BadRequestError"}}, status_code=400)
        if key_conf.get("models") and alias not in key_conf["models"]:
            return JSONResponse({"error": {"message": f"ключу {key_name} недоступна "
                                                       f"модель {alias}",
                                           "type": "ForbiddenError"}}, status_code=403)

        ok, verdict = limits_mod.check(conn, key_name, key_conf)
        headers = dict(verdict["headers"])
        if not ok:
            return JSONResponse({"error": {"message": verdict["reason"],
                                           "type": "RateLimitError"}},
                                status_code=429, headers=headers)

        chain = chain_of(alias)
        stream = bool(payload.get("stream"))
        prompt_text = _responses_prompt_text(payload)
        client: httpx.AsyncClient = request.app.state.client
        started = time.perf_counter()

        for name in chain:
            try:
                resp = await call_upstream(client, name, payload, stream,
                                           path="/responses")
            except RuntimeError as exc:
                finish(key_name, name, 0, 0, 0, 0, started, f"error:{exc}"[:80],
                       prompt_text)
                if name is chain[-1]:
                    return JSONResponse({"error": {"message": str(exc),
                                                   "type": "UpstreamUnavailable"}},
                                        status_code=503, headers=headers)
                continue

            if stream:
                headers["x-gateway-upstream"] = name

                async def relay(response=resp, upstream=name):
                    collected: list[str] = []
                    try:
                        async for chunk in response.aiter_text():
                            collected.append(chunk)
                            yield chunk
                    finally:
                        await response.aclose()
                    usage_obj, answer = _usage_from_sse_responses("".join(collected))
                    prompt, cached, completion, reasoning = _buckets_responses(
                        usage_obj, answer, prompt_text)
                    finish(key_name, upstream, prompt, cached, completion, reasoning,
                           started, "ok", prompt_text)

                return StreamingResponse(relay(), media_type="text/event-stream",
                                         headers=headers)

            data = resp.json()
            usage_obj = data.get("usage") or {}
            answer = _responses_answer_text(data)
            prompt, cached, completion, reasoning = _buckets_responses(
                usage_obj, answer, prompt_text)
            finish(key_name, name, prompt, cached, completion, reasoning, started, "ok",
                   prompt_text)
            headers.update(limits_mod.check(conn, key_name, key_conf)[1]["headers"])
            headers["x-gateway-upstream"] = name
            return JSONResponse(data, headers=headers)

        return JSONResponse({"error": {"message": "все апстримы недоступны",
                                       "type": "UpstreamUnavailable"}},
                            status_code=503, headers=headers)

    return app


def serve(config_path: str | None = None) -> None:
    """Запуск сервера: `python -m gateway.app`."""
    import uvicorn

    cfg = cfg_mod.load(config_path)
    app = create_app(config_path)
    uvicorn.run(app, host=cfg["listen"]["host"], port=int(cfg["listen"]["port"]))


if __name__ == "__main__":
    serve()
