# Home AI Gateway

AI-шлюз: один OpenAI-совместимый вход перед несколькими инференс-эндпоинтами.
Модели по алиасам, виртуальные ключи, учёт токенов (prompt/cached/completion/reasoning)
и стоимости, лимиты rpm/tpm/дневной бюджет с реальным 429. Python: FastAPI + httpx +
SQLite, один контейнер.

## Как запустить

```bash
pip install -r requirements.txt
cp config.example.yaml config.local.yaml   # свои адреса и токены
python run.py config.local.yaml            # http://localhost:8090
```

В контейнере:

```bash
docker build -t home-ai-gateway .
docker run -d -p 8090:8090 \
  -v "$PWD/config.local.yaml:/etc/gateway/config.yaml:ro" \
  home-ai-gateway
```

Манифесты для кластера — в репозитории `llm-stack`.

## Терпеливый режим (ретраи без фолбэка)

По умолчанию отказ апстрима отдаётся клиенту сразу (503) — фолбэк-цепочки
держат потребители. Ключу можно включить `patient: true`: транзиентные 5xx
(502/503/504) ретраятся той же моделью с бэк-оффом `retry_backoff_s` апстрима
(например `[60, 120]`), пока апстрим жив по фоновым пробам (4+ подряд неудачных
проб — ретраи отключаются). Сетевые отказы (ConnectTimeout/ConnectError) и
read-таймауты не ретраятся никогда. Метрика `gateway_upstream_retries_total`
показывает число ретраев по апстримам.
