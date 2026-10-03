"""Text-exposition для Prometheus: счётчики по моделям, букеты токенов, латентность."""

from __future__ import annotations

from . import store

LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)


def render(conn, health: dict, samples: list[float]) -> str:
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
    for model, status, hits, tokens, cost in store.by_model(conn):
        lines.append(f'gateway_model_requests_total{{model="{model}",status="{status}"}} {hits}')
        lines.append(f'gateway_model_tokens_total{{model="{model}"}} {tokens}')
        lines.append(f'gateway_model_cost_usd{{model="{model}"}} {cost:.6f}')

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
