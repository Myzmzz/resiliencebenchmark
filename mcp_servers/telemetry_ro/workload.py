"""Safe summaries of the configured Locust workload statistics endpoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def summarize_locust_stats(
    payload: Mapping[str, Any], *, target_stat_name: str
) -> dict[str, Any]:
    rows = [item for item in payload.get("stats") or () if isinstance(item, Mapping)]
    aggregate = next((item for item in rows if item.get("name") == "Aggregated"), {})
    target_rows = [item for item in rows if item.get("name") == target_stat_name]

    requests = sum(_integer(item.get("num_requests")) for item in target_rows)
    failures = sum(_integer(item.get("num_failures")) for item in target_rows)
    response_sum_ms = sum(
        _number(item.get("avg_response_time")) * _integer(item.get("num_requests"))
        for item in target_rows
    )
    latency_ms = response_sum_ms / requests if requests else 0.0
    success_rate = (requests - failures) / requests if requests else 0.0
    current_rps = sum(_number(item.get("current_rps")) for item in target_rows)
    current_fail_per_sec = sum(
        _number(item.get("current_fail_per_sec")) for item in target_rows
    )
    p95_values = [
        _number(item.get("response_time_percentile_0.95"))
        for item in target_rows
        if item.get("response_time_percentile_0.95") is not None
    ]
    state = str(payload.get("state") or "")
    user_count = _integer(payload.get("user_count"))
    total_rps = _number(aggregate.get("total_rps") or payload.get("total_rps"))
    sample_valid = state == "running" and user_count > 0 and requests > 0
    return {
        "target_stat_name": target_stat_name,
        "state": state,
        "user_count": user_count,
        "sample_status": "valid" if sample_valid else "insufficient",
        "target_requests": requests,
        "target_failures": failures,
        "target_response_sum_ms": response_sum_ms,
        "target_latency_ms": latency_ms,
        "target_p95_ms": max(p95_values) if p95_values else 0.0,
        "target_success_rate": success_rate,
        "target_current_rps": current_rps,
        "target_current_fail_per_sec": current_fail_per_sec,
        "total_requests": _integer(aggregate.get("num_requests")),
        "total_failures": _integer(aggregate.get("num_failures")),
        "total_rps": total_rps,
    }


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
