"""Админ-CLI шлюза: ключи, бюджеты, отчёты.

    python -m gateway.cli keys list
    python -m gateway.cli keys add phone --models gb10-next --rpm 20 --tpm 100000
    python -m gateway.cli keys revoke phone
    python -m gateway.cli budget set phone --usd 3
    python -m gateway.cli report --since 7 [--key kimi] [--json]
    python -m gateway.cli upstreams
    python -m gateway.cli prune

Ключи и бюджеты живут в YAML-конфиге, агрегаты — в SQLite.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

import yaml

from . import config as cfg_mod
from . import store


def _save(cfg: dict, path: str) -> None:
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    for name, up in payload.get("upstreams", {}).items():
        if up.get("model") == name:
            up.pop("model", None)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)


def _conn(cfg: dict):
    return store.connect(cfg.get("db", "./data/gateway.db"))


def cmd_keys_list(cfg: dict) -> int:
    for name, conf in cfg["keys"].items():
        print(f"{name:12s} models={','.join(conf.get('models', [])) or '-'} "
              f"rpm={conf['rpm']} tpm={conf['tpm']} budget={conf['daily_budget_usd']} USD "
              f"token={conf.get('token', '')}")
    if not cfg["keys"]:
        print("ключей нет — все запросы идут как default")
    return 0


def cmd_keys_add(cfg: dict, args) -> int:
    key = {"models": args.models or list(cfg["upstreams"]),
           "rpm": args.rpm, "tpm": args.tpm,
           "daily_budget_usd": args.usd,
           "token": args.token or secrets.token_urlsafe(12)}
    cfg["keys"][args.name] = key
    _save(cfg, cfg["_path"])
    print(f"{args.name}: {key['token']}")
    return 0


def cmd_keys_revoke(cfg: dict, args) -> int:
    removed = cfg["keys"].pop(args.name, None)
    _save(cfg, cfg["_path"])
    print("убран" if removed else "не найден")
    return 0


def cmd_budget_set(cfg: dict, args) -> int:
    conf = cfg["keys"].setdefault(args.name, {"models": list(cfg["upstreams"]),
                                              "rpm": 60, "tpm": 200_000})
    conf["daily_budget_usd"] = args.usd
    if args.rpm:
        conf["rpm"] = args.rpm
    if args.tpm:
        conf["tpm"] = args.tpm
    _save(cfg, cfg["_path"])
    print(f"{args.name}: бюджет {args.usd} USD, rpm {conf['rpm']}, tpm {conf['tpm']}")
    return 0


def cmd_report(cfg: dict, args) -> int:
    conn = _conn(cfg)
    rows = store.report(conn, since_days=args.since, key=args.key)
    conn.close()
    if args.json:
        print(json.dumps({"rows": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("нет данных за период")
        return 0
    header = (f"{'день':10s} {'ключ':10s} {'модель':16s} "
              f"{'вход':>7s} {'кэш':>7s} {'выход':>7s} {'мысль':>7s} {'запр':>5s} "
              f"{'TCO $':>8s} {'API $':>8s}")
    print(header)
    for row in rows:
        tokens = row["tokens"]
        print(f"{row['day']:10s} {row['key']:10s} {row['model']:16s} "
              f"{tokens['prompt']:7d} {tokens['cached']:7d} {tokens['completion']:7d} "
              f"{tokens['reasoning']:7d} {row['requests']:5d} "
              f"{row['cost']['tco']:8.4f} {row['cost']['api']:8.4f}")
    totals = {}
    for row in rows:
        for bucket, value in row["tokens"].items():
            totals[bucket] = totals.get(bucket, 0) + value
    print(f"итого: {totals}, TCO {sum(r['cost']['tco'] for r in rows):.4f} USD, "
          f"API {sum(r['cost']['api'] for r in rows):.4f} USD")
    return 0


def cmd_upstreams(cfg: dict, args) -> int:
    conn = _conn(cfg)
    totals = store.totals(conn)
    conn.close()
    for name, up in cfg["upstreams"].items():
        price = up["price"]
        print(f"{name:16s} -> {up['base_url']} ({up['model']}), "
              f"timeout {up['timeout_s']}s, retries {up['retries']}, "
              f"fallback {','.join(up['fallback']) or '-'}, "
              f"цена {price['input']}/{price['cached_input']}/{price['output']} $/1M")
    print("всего:", totals)
    return 0


def cmd_prune(cfg: dict, args) -> int:
    conn = _conn(cfg)
    removed = store.prune(conn, int(cfg.get("retention_days", 90)))
    conn.close()
    print(f"удалено рекордов: {removed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway.cli", description=__doc__)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    keys = sub.add_parser("keys", help="управление ключами")
    keys_sub = keys.add_subparsers(dest="action", required=True)
    keys_list = keys_sub.add_parser("list")
    keys_list.set_defaults(func=cmd_keys_list)
    keys_add = keys_sub.add_parser("add")
    keys_add.add_argument("name")
    keys_add.add_argument("--models", nargs="*")
    keys_add.add_argument("--rpm", type=int, default=60)
    keys_add.add_argument("--tpm", type=int, default=200_000)
    keys_add.add_argument("--usd", type=float, default=10.0)
    keys_add.add_argument("--token", default=None)
    keys_add.set_defaults(func=cmd_keys_add)
    keys_revoke = keys_sub.add_parser("revoke")
    keys_revoke.add_argument("name")
    keys_revoke.set_defaults(func=cmd_keys_revoke)

    budget = sub.add_parser("budget", help="бюджет и лимиты ключа")
    budget_sub = budget.add_subparsers(dest="action", required=True)
    budget_set = budget_sub.add_parser("set")
    budget_set.add_argument("name")
    budget_set.add_argument("--usd", type=float, required=True)
    budget_set.add_argument("--rpm", type=int, default=0)
    budget_set.add_argument("--tpm", type=int, default=0)
    budget_set.set_defaults(func=cmd_budget_set)

    report = sub.add_parser("report", help="отчёт потребления")
    report.add_argument("--since", type=int, default=7)
    report.add_argument("--key", default=None)
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    upstreams = sub.add_parser("upstreams", help="каталог апстримов и счётчики")
    upstreams.set_defaults(func=cmd_upstreams)

    prune = sub.add_parser("prune", help="срезать старые рекорды")
    prune.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = cfg_mod.load(args.config)
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
