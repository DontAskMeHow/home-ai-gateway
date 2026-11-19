"""Хранилище учёта: SQLite (WAL) для агрегатов и JSONL для сырых рекордов.

Букеты токенов на один запрос:
  prompt      — входные токены (`usage.prompt_tokens`)
  cached      — из них взятые из кэша префилла (`prompt_tokens_details.cached_tokens`)
  completion  — выходные токены (`usage.completion_tokens`)
  reasoning   — из них токены размышления (`completion_tokens_details.reasoning_tokens`)

`reasoning` входит в `completion` и не суммируется дважды: он нужен, чтобы видно было,
какую часть вывода съедают рассуждения.

Стоимость считается по двум таблицам: собственная (TCO железа) и рыночный аналог.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    key        TEXT NOT NULL,
    model      TEXT NOT NULL,
    prompt     INTEGER NOT NULL DEFAULT 0,
    cached     INTEGER NOT NULL DEFAULT 0,
    completion INTEGER NOT NULL DEFAULT 0,
    reasoning  INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL,
    cost_tco   REAL NOT NULL DEFAULT 0,
    cost_api   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_key ON events(key, ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def estimate_tokens(text: str) -> int:
    """Грубая оценка, когда апстрим не вернул usage."""
    return max(1, len(text) // 4) if text else 0


def compute_cost(price: dict, prompt: int, cached: int, completion: int) -> float:
    billable_input = max(prompt - cached, 0)
    return (billable_input * price["input"] + cached * price["cached_input"]
            + completion * price["output"]) / 1_000_000


def record(conn: sqlite3.Connection, jsonl_path: str | None, *, key: str, model: str,
           prompt: int, cached: int, completion: int, reasoning: int,
           latency_ms: float, status: str, cost_tco: float, cost_api: float,
           prompt_info: dict | None = None, ts: str | None = None) -> dict:
    """Один рекорд: строка в SQLite (агрегаты) + строка в JSONL (метаданные).

    `prompt_info` — что сохранить о самом запросе, не вскрывая его: длина и хэш
    (`prompt_chars`, `prompt_hash`). Тело запроса в журнал не пишется.
    """
    ts = ts or now_iso()
    conn.execute(
        "INSERT INTO events (ts, key, model, prompt, cached, completion, reasoning,"
        " latency_ms, status, cost_tco, cost_api) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, key, model, prompt, cached, completion, reasoning, latency_ms, status,
         cost_tco, cost_api),
    )
    conn.commit()
    row = {
        "ts": ts, "key": key, "model": model,
        "tokens": {"prompt": prompt, "cached": cached, "completion": completion,
                   "reasoning": reasoning},
        "latency_ms": round(latency_ms, 1), "status": status,
        "cost": {"tco": round(cost_tco, 6), "api": round(cost_api, 6)},
    }
    if prompt_info:
        row.update(prompt_info)
    if jsonl_path:
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(jsonl_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def window(conn: sqlite3.Connection, key: str, seconds: int) -> tuple[int, int]:
    """Число запросов и сумма токенов ключа за последние `seconds` секунд."""
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(prompt + completion), 0) FROM events"
        " WHERE key = ? AND ts >= ? AND ts <= ?",
        (key, _ago(seconds), cutoff),
    ).fetchone()
    return int(row[0]), int(row[1])


def day_cost(conn: sqlite3.Connection, key: str) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_tco), 0) FROM events WHERE key = ? AND ts LIKE ?",
        (key, today + "%"),
    ).fetchone()
    return float(row[0])


def report(conn: sqlite3.Connection, since_days: int = 7,
           key: str | None = None) -> list[dict]:
    cutoff = _ago(since_days * 86400)
    where = "WHERE ts >= ?"
    args: list = [cutoff]
    if key:
        where += " AND key = ?"
        args.append(key)
    cur = conn.execute(
        f"SELECT substr(ts, 1, 10) AS day, key, model,"
        f" SUM(prompt), SUM(cached), SUM(completion), SUM(reasoning),"
        f" COUNT(*), SUM(cost_tco), SUM(cost_api), AVG(latency_ms)"
        f" FROM events {where}"
        f" GROUP BY day, key, model ORDER BY day, key, model",
        args,
    )
    out = []
    for day, k, model, prompt, cached, completion, reasoning, hits, tco, api, lat in cur:
        out.append({
            "day": day, "key": k, "model": model,
            "tokens": {"prompt": prompt, "cached": cached, "completion": completion,
                       "reasoning": reasoning},
            "requests": hits,
            "cost": {"tco": round(tco or 0, 6), "api": round(api or 0, 6)},
            "avg_latency_ms": round(lat or 0, 1),
        })
    return out


def totals(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
        " COALESCE(SUM(completion),0), COALESCE(SUM(reasoning),0),"
        " COALESCE(SUM(cost_tco),0), COALESCE(SUM(cost_api),0) FROM events",
    ).fetchone()
    return {
        "requests": row[0], "prompt": row[1], "cached": row[2],
        "completion": row[3], "reasoning": row[4], "cost_tco": row[5], "cost_api": row[6],
    }


def by_model_status(conn: sqlite3.Connection) -> list[tuple]:
    """Запросы по (модель, статус) — уникальные серии для счётчика запросов."""
    return conn.execute(
        "SELECT model, status, COUNT(*) FROM events GROUP BY model, status",
    ).fetchall()


def by_model(conn: sqlite3.Connection) -> list[tuple]:
    """Токены и стоимость по модели (без статуса — иначе дубли серий в /metrics)."""
    return conn.execute(
        "SELECT model, COALESCE(SUM(prompt + completion),0), COALESCE(SUM(cost_tco),0)"
        " FROM events GROUP BY model",
    ).fetchall()


def by_key_status(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT key, status, COUNT(*) FROM events GROUP BY key, status",
    ).fetchall()


def by_key(conn: sqlite3.Connection) -> list[tuple]:
    """Букеты токенов и обе стоимости по ключу-потребителю."""
    return conn.execute(
        "SELECT key, COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
        " COALESCE(SUM(completion),0), COALESCE(SUM(reasoning),0),"
        " COALESCE(SUM(cost_tco),0), COALESCE(SUM(cost_api),0)"
        " FROM events GROUP BY key",
    ).fetchall()


def tokens_since(conn: sqlite3.Connection, since_z: str,
                 model_prefix: str = "gpt") -> int:
    """Токены (prompt+completion, кэш и reasoning уже внутри) по моделям
    с префиксом с момента `since_z` в формате now_iso — база недельной квоты."""
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt + completion), 0) FROM events"
        " WHERE ts >= ? AND model LIKE ?",
        (since_z, model_prefix + "%"),
    ).fetchone()
    return int(row[0])


def by_key_model(conn: sqlite3.Connection) -> list[tuple]:
    """Токены по паре (ключ, модель) — кто какой моделью пользовался."""
    return conn.execute(
        "SELECT key, model, COALESCE(SUM(prompt + completion), 0) FROM events"
        " GROUP BY key, model",
    ).fetchall()


def by_model_buckets(conn: sqlite3.Connection) -> list[tuple]:
    """Букеты токенов по модели — состав потребления и кэш-доля."""
    return conn.execute(
        "SELECT model, COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
        " COALESCE(SUM(completion),0), COALESCE(SUM(reasoning),0)"
        " FROM events GROUP BY model",
    ).fetchall()


def duration_quantiles(conn: sqlite3.Connection, group: str = "model",
                       window_hours: int = 24) -> list[tuple]:
    """p50/p99 длительности запросов (сек) за окно, группировка model|key."""
    cutoff = _ago(window_hours * 3600)
    rows = conn.execute(
        f"SELECT {group}, latency_ms FROM events"
        f" WHERE ts >= ? AND latency_ms > 0",
        (cutoff,),
    ).fetchall()
    groups: dict[str, list[float]] = {}
    for name, ms in rows:
        groups.setdefault(name, []).append(ms / 1000)
    out = []
    for name in sorted(groups):
        values = sorted(groups[name])
        n = len(values)
        p50 = values[min(n - 1, max(0, round(0.5 * n) - 1))]
        p99 = values[min(n - 1, max(0, round(0.99 * n) - 1))]
        out.append((name, p50, p99))
    return out


def prune(conn: sqlite3.Connection, retention_days: int) -> int:
    cutoff = _ago(retention_days * 86400)
    cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def _ago(seconds: int) -> str:
    stamp = datetime.now(timezone.utc)
    from datetime import timedelta

    return (stamp - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
