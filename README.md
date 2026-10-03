# Home AI Gateway

Лёгкий AI-шлюз для собственного парка моделей: один OpenAI-совместимый вход,
маршрутизация по алиасам, честный учёт токенов и стоимости, лимиты, которые
действительно работают. ~900 строк Python, FastAPI + httpx + SQLite, один контейнер.

Ниша, которую закрывает: хостинг нескольких self-hosted инференс-эндпоинтов для
нескольких потребителей (агент, скрипты, телефон) с учётом в токенах и деньгах.
Большие шлюзы (LiteLLM и подобные) для этого требуют БД и тянут за собой десятки
провайдеров; здесь — три апстрима, пять ключей и прозрачная смета.

## Что делает

| Возможность | Детали |
|---|---|
| OpenAI-совместимый вход | `POST /v1/chat/completions`, `GET /v1/models`; стриминг — прозрачный SSE-passthrough, usage читается из финального чанка |
| Каталог апстримов | алиас → базовый URL + имя модели + таймаут + ретраи + цепочка запасных; особенности наших моделей (отдельное поле `reasoning`, длинные грезы до 180 с) |
| Виртуальные ключи | имя потребителя → разрешённые модели, токены; выдача и отзыв из CLI |
| Учёт токенов | четыре букета на запрос: вход, из него кэш префилла, выход, из него размышление; накопление ключ × модель × день в SQLite |
| Две цены | смета из TCO железа (амортизация + электричество на измеренную пропускную способность) и рыночный аналог — рядом, для сравнения |
| Лимиты | rpm / tpm / дневной бюджет на ключ, ответ 429 + `x-ratelimit-*` хедера с остатками |
| Здоровье | фоновый опрос `/models` каждого апстрима, `GET /healthz` со статусами |
| Метрики | `GET /metrics` в формате Prometheus: запросы по моделям и статусам, гистограмма латентности, счётчики токенов по букетам, стоимость, отказы, срабатывания лимитов |
| Журнал | JSONL с метаданными каждого запроса (время, ключ, модель, букеты токенов, латентность, статус, обе стоимости, длина и хэш промпта); тело запроса не пишется. SQLite — только агрегаты |
| Отчёты | `cli report` текстом или JSON за N дней, по ключу |

## Структура

```
gateway/
├── app.py       # FastAPI-приложение: маршрутизация, стриминг, учёт, лимиты
├── config.py    # загрузка YAML, разрешения алиасов
├── store.py     # SQLite (WAL) + JSONL, стоимость, агрегаты, окно лимитов
├── limits.py    # rpm / tpm / бюджет, хедера x-ratelimit-*
├── metrics.py   # text exposition для Prometheus
└── cli.py       # keys / budget / report / upstreams / prune
run.py           # запуск uvicorn
scripts/smoke.py # проверочный прогон против живых эндпоинтов
config.example.yaml
```

## Быстрый старт

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.local.yaml          # свои адреса и токены
python run.py config.local.yaml                    # http://localhost:8090
python -m gateway.cli keys list
python scripts/smoke.py --base http://localhost:8090 --token sk-gateway-scripts
```

Путь до конфига задаётся либо первым аргументом, либо переменной окружения
`GATEWAY_CONFIG` (так сделано в контейнере). Без обоих подхватывается
`config.local.yaml` → `config.yaml` → `config.example.yaml` из текущей папки.
Приватная часть — адреса инференс-хостов и токены — живёт в `config.local.yaml`,
который исключён из git; в репозитории остаётся обезличенный `config.example.yaml`.

В контейнере:

```bash
docker build -t home-ai-gateway .
docker run -d --name gateway -p 8090:8090 \
  -v "$PWD/config.local.yaml:/etc/gateway/config.yaml:ro" \
  -v gateway-data:/app/data home-ai-gateway
```

Готовые манифесты и helm-чарт для этого образа — в репозитории `llm-stack`
(`charts/llm-stack`, плюс вариант на kustomize в `kustomize/`).

## Конфиг

```yaml
listen: {host: 0.0.0.0, port: 8090}
probe_interval_s: 30
retention_days: 90
db: ./data/gateway.db
log: ./data/usage.jsonl

upstreams:
  next:
    base_url: http://llm-a.internal:8040/mercury/next/v1
    model: Big-Next-Instruct
    timeout_s: 180
    retries: 1
    fallback: [mid]
    price: {input: 0.30, cached_input: 0.05, output: 1.20}   # $ за 1M токенов, из TCO
    api_price: {input: 0.60, cached_input: 0.10, output: 2.40}

keys:
  kimi: {token: sk-gateway-kimi, models: [next, mid], rpm: 30, tpm: 200000, daily_budget_usd: 5}
```

Значения в строках проходят `os.path.expandvars`, поэтому токен можно дать через
`api_key: ${EXTERNAL_API_KEY}`.

Апстримы по умолчанию опрашиваются напрямую (`trust_env: false`): системный прокси
рабочей машины нередко не пропускает внутренние адреса инференс-хостов. Если до
эндпоинтов надо идти через прокси — поставьте в конфиге `trust_env: true`.
Локальные проверки (`scripts/smoke.py`) прокси игнорируют всегда, иначе ответы
на localhost обрезаются до пустого тела.

## Как считается смета

На запрос из `usage` берутся четыре счётчика:

| Букет | Источник |
|---|---|
| `prompt` | `usage.prompt_tokens` |
| `cached` | `usage.prompt_tokens_details.cached_tokens` — часть входа, отданная из кэша префилла |
| `completion` | `usage.completion_tokens` |
| `reasoning` | `usage.completion_tokens_details.reasoning_tokens` — часть вывода на размышления |

Стоимость: непустая часть входа по `input`, кэшированная по `cached_input`, вывод по
`output`, отдельно по таблице TCO и по таблице рыночной цены. Если апстрим не вернул
`usage`, объём оценивается по длине текста (4 символа на токен), статус запроса при
этом остаётся `ok`.

KV-кеш — не часть сметы, а состояние движка: он остаётся в метриках инференса
(`vllm:kv_cache_usage_perc`, `vllm:prefix_cache_hits_total`), а в отчёте шлюза виден
как доля `cached`.

## Лимиты

Проверяются до обращения в модель: если rpm, tpm или дневной бюджет исчерпаны,
клиент получает 429 с `x-ratelimit-*` и `retry-after`, запрос до модели не идёт.
В отличие от схем «лимиты только в хедерах», здесь отклонение реальное: второй запрос
ключа с `rpm: 1` действительно получает 429.

## Метрики и наблюдение

`GET /metrics` отдаёт:

```
gateway_requests_total
gateway_tokens_total{bucket="prompt|cached|completion|reasoning"}
gateway_cost_usd{price="tco|api"}
gateway_model_requests_total{model,status}
gateway_model_tokens_total{model}
gateway_model_cost_usd{model}
gateway_upstream_up{upstream}
gateway_request_duration_seconds_bucket / _sum / _count
```

В существующий контур наблюдения шлюз подключается ServiceMonitor'ом с узким
selector по лейблу самого сервиса — широкий selector по `release` захватывает чужие
службы и их эндпоинты дают 404.

## Развёртывание

Образ собирается из этого репозитория (`docker build -t home-ai-gateway .`), конфиг
монтируется в `/etc/gateway/config.yaml`, данные — на volume в `/app/data`. На k3s
не хватает одного `docker build`: образ нужно импортировать в containerd самого k3s
(`docker save` + `k3s ctr images import`), иначе под падает в ErrImagePull.

Манифесты для кластера живут в репозитории `llm-stack`: helm-чарт `charts/llm-stack`
или вариант на kustomize в `kustomize/` — там Deployment с `Recreate` (диск у пода один),
readiness/liveness на `/healthz`, ConfigMap с каталогом моделей, Secret с токенами,
Service + ServiceMonitor с узким selector и PVC под SQLite. Если апстримы доступны
только через туннель от рабочей машины, релей вешается на машину, а шлюз ходит в
`http://127.0.0.1:<порт>`; при выключенном релее шлюз отдаёт 503 — это видимый отказ,
а не тишина.

## Проверки

`scripts/smoke.py` прогоняет по живому контуру: здоровье, список моделей, чат без
стрима и со стримом, совпадение `usage` с прямым прогоном, 429 на втором запросе ключа
с `rpm: 1`, наличие букетов в `/metrics`, непустой `cli report`.

## Известные ограничения

- При стриминге хедера `x-ratelimit-*` отражают состояние на момент входа в запрос
  (после последнего чанка их уже не отправить).
- Оценка токенов по длине текста — грубая; включается только когда апстрим не отдал
  `usage`.
- Приоритет одного процесса: шлюз рассчитан на десятки потребителей, не на сотни.
