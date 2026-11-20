"""Локальный тест терпеливого режима шлюза (без живых апстримов).

    docker run --rm --network host -v "$PWD:/app" -w /app \
        home-ai-gateway:<tag> python scripts/test_retries.py

Поднимает мок-LLM (stdlib http.server) и шлюз на соседних портах, проверяет
классификацию ошибок: транзиентные 5xx ретраятся только у patient-ключей и
только пока апстрим жив по пробам; connect-фейлы и read-таймауты не ретраятся.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

MOCK_PORT = 18081
GW_PORT = 18082
GW_BASE = f"http://127.0.0.1:{GW_PORT}"

# управляемое поведение мока
state = {"fail_504": 0, "hang_s": 0.0, "probe_500": False}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тише в выводе теста
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if state["probe_500"]:
                self._send(500, {"error": "probe fail"})
            else:
                self._send(200, {"data": [{"id": "mock"}]})
            return
        self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            if state["hang_s"]:
                time.sleep(state["hang_s"])
            if state["fail_504"] > 0:
                state["fail_504"] -= 1
                self._send(504, {"error": "upstream timeout (mock)"})
                return
            self._send(200, {
                "choices": [{"message": {"content": "ок"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            })
            return
        self._send(404, {})


def write_config(tmp: Path) -> Path:
    cfg = f"""
listen:
  host: 127.0.0.1
  port: {GW_PORT}
probe_interval_s: 0.2
retention_days: 1
db: {tmp}/gw.db
log: {tmp}/usage.jsonl
upstreams:
  mock:
    base_url: http://127.0.0.1:{MOCK_PORT}/mock/v1
    model: Mock-LLM
    timeout_s: 2
    retries: 0
    retry_backoff_s: [0.2, 0.2]
    fallback: []
    price: {{input: 0.1, cached_input: 0.02, output: 0.5}}
  dead:
    base_url: http://127.0.0.1:1/dead/v1
    model: Dead-LLM
    timeout_s: 2
    retries: 0
    retry_backoff_s: [0.2, 0.2]
    fallback: []
    price: {{input: 0.1, cached_input: 0.02, output: 0.5}}
keys:
  patient:
    token: sk-test-patient
    patient: true
  hasty:
    token: sk-test-hasty
    patient: false
"""
    path = tmp / "config.yaml"
    path.write_text(cfg, encoding="utf-8")
    return path


def wait_up(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            if client.get(f"{GW_BASE}/healthz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("шлюз не поднялся")


def chat(client: httpx.Client, token: str, model: str = "mock",
         timeout: float = 30.0) -> httpx.Response:
    return client.post(
        f"{GW_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": model, "max_tokens": 8,
              "messages": [{"role": "user", "content": "скажи ок"}]},
        timeout=timeout)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return ok


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gw-retry-test-"))
    server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    gw = subprocess.Popen([sys.executable, "run.py", str(write_config(tmp))],
                          cwd=str(Path(__file__).resolve().parent.parent))
    results: list[bool] = []
    try:
        client = httpx.Client(trust_env=False)
        wait_up(client)

        # T1: patient + две 504 → успех на третьей попытке, латентность с паузами
        state.update(fail_504=2, hang_s=0.0, probe_500=False)
        t0 = time.perf_counter()
        r = chat(client, "sk-test-patient")
        dt = time.perf_counter() - t0
        results.append(check("T1 patient: 504,504,200 → 200",
                             r.status_code == 200, f"HTTP {r.status_code}"))
        results.append(check("T1 латентность включает бэк-офф (>=0.4с)",
                             dt >= 0.4, f"{dt:.2f}с"))
        metrics = client.get(f"{GW_BASE}/metrics").text
        results.append(check("T1 метрика ретраев >= 2",
                             'gateway_upstream_retries_total{upstream="mock"} 2'
                             in metrics, "gateway_upstream_retries_total"))

        # T2: не-patient + 504 → сразу 503, без пауз
        state.update(fail_504=99)
        t0 = time.perf_counter()
        r = chat(client, "sk-test-hasty")
        dt = time.perf_counter() - t0
        results.append(check("T2 hasty: 504 → 503 сразу",
                             r.status_code == 503 and dt < 0.3,
                             f"HTTP {r.status_code}, {dt:.2f}с"))
        state.update(fail_504=0)

        # T3: connect-фейл (порт закрыт) — не ретраится даже у patient
        t0 = time.perf_counter()
        r = chat(client, "sk-test-patient", model="dead")
        dt = time.perf_counter() - t0
        results.append(check("T3 connect-фейл → 503 без ретраев",
                             r.status_code == 503 and dt < 0.5,
                             f"HTTP {r.status_code}, {dt:.2f}с"))

        # T4: апстрим мёртв по пробам (4+ подряд) → ретраи отключены у patient
        state.update(probe_500=True)
        time.sleep(1.2)  # >= 4 проб по 0.2 с + запас
        state.update(fail_504=99)
        t0 = time.perf_counter()
        r = chat(client, "sk-test-patient")
        dt = time.perf_counter() - t0
        results.append(check("T4 мёртв по пробам: 504 → 503 без пауз",
                             r.status_code == 503 and dt < 0.3,
                             f"HTTP {r.status_code}, {dt:.2f}с"))
        state.update(probe_500=False, fail_504=0)
        time.sleep(1.0)  # пробы снова зелёные

        # T5: read-таймаут (мок висит дольше timeout_s=2) — не ретрается
        state.update(hang_s=3.0)
        t0 = time.perf_counter()
        r = chat(client, "sk-test-patient", timeout=15)
        dt = time.perf_counter() - t0
        results.append(check("T5 read-таймаут → 503 за ~timeout_s, без ретраев",
                             r.status_code == 503 and 2.0 <= dt < 3.5,
                             f"HTTP {r.status_code}, {dt:.2f}с"))
        state.update(hang_s=0.0)

        # T6: после восстановления мок жив, patient снова проходит с ретраями
        state.update(fail_504=1)
        r = chat(client, "sk-test-patient")
        results.append(check("T6 восстановление: 504,200 → 200",
                             r.status_code == 200, f"HTTP {r.status_code}"))

        rows = [json.loads(l) for l in (tmp / "usage.jsonl").read_text().splitlines() if l]
        statuses = [row["status"] for row in rows]
        results.append(check("T7 журнал: ошибки записаны честно",
                             any("HTTP 504" in s for s in statuses)
                             and any("ConnectError" in s for s in statuses)
                             and any("ReadTimeout" in s for s in statuses),
                             "; ".join(statuses)))
    finally:
        gw.terminate()
        gw.wait(timeout=10)
        server.shutdown()

    print(f"\n{'OK' if all(results) else 'ЕСТЬ ПАДЕНИЯ'}: "
          f"{sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())