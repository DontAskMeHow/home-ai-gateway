"""Text-exposition для Prometheus: счётчики по моделям, букеты токенов, латентность."""

from __future__ import annotations

from . import store

LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)


def render(conn, health: dict, samples: list[float],
           week: dict | None = None) -> str:
    lines: list[str] = []
    totals = store.totals(conn)
    lines.append("# HELP gateway_requests_total Requests through the gateway")
    lines.append("# TYPE gateway_requests_total counter")
    lines.append(f"gateway_requests_total {totals['requests']}")

    lines.append("# HELP gateway_tokens_total Tokens by bucket")
    lines.append("# TYPE gateway_tokens_total counter")
    for bucket in ("prompt", "cached", "completion", "reasoning"):
        lines.append(f'gateway_tokens_total{{bucket="{bucket}"}} {totals[bucket]}')

    lines.append("# HELP gateway_cost_usd Cost in USD by price model")
    lines.append("# TYPE gateway_cost_usd counter")
    lines.append(f'gateway_cost_usd{{price="tco"}} {totals["cost_tco"]:.6f}')
    lines.append(f'gateway_cost_usd{{price="api"}} {totals["cost_api"]:.6f}')

    lines.append("# HELP gateway_model_requests_total Requests by model and status")
    lines.append("# TYPE gateway_model_requests_total counter")
    for model, status, hits in store.by_model_status(conn):
        lines.append(f'gateway_model_requests_total{{model="{model}",status="{status}"}} {hits}')
    lines.append("# HELP gateway_model_tokens_total Tokens by model")
    lines.append("# TYPE gateway_model_tokens_total counter")
    lines.append("# HELP gateway_model_cost_usd Cost in USD by model and price model")
    lines.append("# TYPE gateway_model_cost_usd counter")
    for model, tokens, tco, api in store.by_model(conn):
        lines.append(f'gateway_model_tokens_total{{model="{model}"}} {tokens}')
        lines.append(f'gateway_model_cost_usd{{model="{model}",price="tco"}} {tco:.6f}')
        lines.append(f'gateway_model_cost_usd{{model="{model}",price="api"}} {api:.6f}')

    lines.append("# HELP gateway_key_requests_total Requests by key and status")
    lines.append("# TYPE gateway_key_requests_total counter")
    for key, status, hits in store.by_key_status(conn):
        lines.append(f'gateway_key_requests_total{{key="{key}",status="{status}"}} {hits}')
    lines.append("# HELP gateway_key_tokens_total Tokens by key and bucket")
    lines.append("# TYPE gateway_key_tokens_total counter")
    lines.append("# HELP gateway_key_cost_usd Cost in USD by key and price model")
    lines.append("# TYPE gateway_key_cost_usd counter")
    for key, prompt, cached, completion, reasoning, tco, api in store.by_key(conn):
        for bucket, value in (("prompt", prompt), ("cached", cached),
                               ("completion", completion), ("reasoning", reasoning)):
            lines.append(f'gateway_key_tokens_total{{key="{key}",bucket="{bucket}"}} {value}')
        lines.append(f'gateway_key_cost_usd{{key="{key}",price="tco"}} {tco:.6f}')
        lines.append(f'gateway_key_cost_usd{{key="{key}",price="api"}} {api:.6f}')

    lines.append("# HELP gateway_key_model_tokens_total Tokens by key and model (prompt+completion)")
    lines.append("# TYPE gateway_key_model_tokens_total counter")
    for key, model, tokens in store.by_key_model(conn):
        lines.append(f'gateway_key_model_tokens_total{{key="{key}",model="{model}"}} {tokens}')

    lines.append("# HELP gateway_model_bucket_tokens_total Token buckets by model")
    lines.append("# TYPE gateway_model_bucket_tokens_total counter")
    for model, prompt, cached, completion, reasoning in store.by_model_buckets(conn):
        for bucket, value in (("prompt", prompt), ("cached", cached),
                              ("completion", completion), ("reasoning", reasoning)):
            lines.append(f'gateway_model_bucket_tokens_total{{model="{model}",bucket="{bucket}"}} {value}')

    lines.append("# HELP gateway_model_duration_seconds Request duration quantiles by model, 24h fixed window")
    lines.append("# TYPE gateway_model_duration_seconds gauge")
    for model, p50, p99 in store.duration_quantiles(conn, "model"):
        lines.append(f'gateway_model_duration_seconds{{model="{model}",quantile="0.5"}} {p50:.3f}')
        lines.append(f'gateway_model_duration_seconds{{model="{model}",quantile="0.99"}} {p99:.3f}')

    lines.append("# HELP gateway_key_duration_seconds Request duration p50 by key, 24h fixed window")
    lines.append("# TYPE gateway_key_duration_seconds gauge")
    for key, p50, _p99 in store.duration_quantiles(conn, "key"):
        lines.append(f'gateway_key_duration_seconds{{key="{key}",quantile="0.5"}} {p50:.3f}')

    if week:
        lines.append("# HELP gateway_gpt_week_tokens GPT tokens (in+out) since current weekly window start")
        lines.append("# TYPE gateway_gpt_week_tokens gauge")
        lines.append(f"gateway_gpt_week_tokens {week['tokens']}")
        lines.append("# HELP gateway_gpt_week_limit_tokens Configured weekly GPT token limit")
        lines.append("# TYPE gateway_gpt_week_limit_tokens gauge")
        lines.append(f"gateway_gpt_week_limit_tokens {week['limit']}")

    lines.append("# HELP gateway_upstream_up Upstream health from background probes")
    lines.append("# TYPE gateway_upstream_up gauge")
    for name, ok in sorted(health.items()):
        lines.append(f'gateway_upstream_up{{upstream="{name}"}} {1 if ok else 0}')

    lines.append("# HELP gateway_request_duration_seconds Request latency")
    lines.append("# TYPE gateway_request_duration_seconds histogram")
    counts = {b: 0 for b in LATENCY_BUCKETS}
    total_sum = 0.0
    for value in samples:
        total_sum += value
        for b in LATENCY_BUCKETS:
            if value <= b:
                counts[b] += 1
    for b in LATENCY_BUCKETS:
        lines.append(f'gateway_request_duration_seconds_bucket{{le="{b}"}} {counts[b]}')
    lines.append(f'gateway_request_duration_seconds_bucket{{le="+Inf"}} {len(samples)}')
    lines.append(f"gateway_request_duration_seconds_sum {total_sum:.3f}")
    lines.append(f"gateway_request_duration_seconds_count {len(samples)}")

    return "\n".join(lines) + "\n"
