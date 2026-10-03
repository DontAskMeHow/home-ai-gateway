"""Запуск шлюза: `python run.py [путь до конфига]`.

Хост и порт берутся из секции `listen` конфига.
"""

from __future__ import annotations

import sys

from gateway.app import serve


def main() -> int:
    config = sys.argv[1] if len(sys.argv) > 1 else None
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
