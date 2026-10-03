"""Загрузка конфигурации шлюза.

Порядок поиска файла: аргумент `--config`, затем `GATEWAY_CONFIG`, затем
`config.local.yaml` → `config.yaml` → `config.example.yaml` в текущей папке.

`config.local.yaml` держит реальные адреса и токены и не попадает в git;
`config.example.yaml` — публичный шаблон с заполнителями.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CANDIDATES = ("config.local.yaml", "config.yaml", "config.example.yaml")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def find_config(start: Path | None = None) -> Path:
    env_path = os.environ.get("GATEWAY_CONFIG")
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate
    base = start or Path.cwd()
    for name in CANDIDATES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    here = Path(__file__).resolve().parent.parent
    for name in CANDIDATES:
        candidate = here / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"не найден конфиг шлюза (GATEWAY_CONFIG, затем {', '.join(CANDIDATES)})"
    )


def load(path: str | os.PathLike[str] | None = None) -> dict:
    cfg_path = Path(path) if path else find_config()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _expand(raw)

    cfg.setdefault("listen", {})
    cfg["listen"].setdefault("host", "0.0.0.0")
    cfg["listen"].setdefault("port", 8090)
    cfg.setdefault("upstreams", {})
    cfg.setdefault("keys", {})
    cfg.setdefault("retention_days", 90)

    for name, up in cfg["upstreams"].items():
        up.setdefault("model", name)
        up.setdefault("timeout_s", 60)
        up.setdefault("retries", 1)
        up.setdefault("fallback", [])
        if isinstance(up["fallback"], str):
            up["fallback"] = [up["fallback"]]
        up.setdefault("price", {})
        up.setdefault("api_price", up["price"])
        for table in (up["price"], up["api_price"]):
            table.setdefault("input", 0.5)
            table.setdefault("cached_input", table["input"] / 4)
            table.setdefault("output", 1.5)

    for name, key in cfg["keys"].items():
        key.setdefault("models", list(cfg["upstreams"]))
        key.setdefault("rpm", 60)
        key.setdefault("tpm", 200_000)
        key.setdefault("daily_budget_usd", 10.0)

    cfg["_path"] = str(cfg_path)

    # Относительные пути данных считаем от папки конфига, а не от текущей директории:
    # сервер и CLI могут запускаться из разных мест.
    base = cfg_path.parent
    for field in ("db", "log"):
        value = cfg.get(field)
        if value and not Path(value).is_absolute():
            cfg[field] = str(base / value)
    return cfg


def upstream_names(cfg: dict) -> list[str]:
    return list(cfg["upstreams"])


def resolve_upstream(cfg: dict, requested: str | None) -> str | None:
    """Алиас из конфига, точное имя модели или единственной апстрим."""
    names = upstream_names(cfg)
    if requested:
        if requested in cfg["upstreams"]:
            return requested
        for name in names:
            if cfg["upstreams"][name].get("model") == requested:
                return name
    if len(names) == 1:
        return names[0]
    return None
