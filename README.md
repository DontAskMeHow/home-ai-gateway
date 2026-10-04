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
