"""Shared condition-driven recovery policy for Stage-2 Agent experiments."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
from typing import Any


CONDITION_POLICY = {
    "recovery_mode": "effect_condition",
    "safety_ttl_seconds": 600,
    "effect_observation_seconds": 300,
    "effect_sustain_seconds": 60,
    "agent_cleanup_seconds": 60,
    "recovery_observation_seconds": 180,
    "recovery_sustain_seconds": 60,
}

WORKLOAD_METRICS = frozenset(
    {
        "target_latency_ms",
        "target_success_rate",
        "target_current_rps",
    }
)
EFFECT_OPERATORS = frozenset(
    {
        "increase_by_at_least",
        "decrease_by_at_least",
        "at_or_above",
        "at_or_below",
    }
)
RECOVERY_OPERATORS = frozenset(
    {
        "within_baseline_delta",
        "at_or_above",
        "at_or_below",
    }
)


def apply_condition_policy(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """Attach the Controller-owned timing budget without changing Agent choices."""

    value = deepcopy(dict(plan or {}))
    value.pop("duration_seconds", None)
    value.pop("maximum_observation_seconds", None)
    value.update(CONDITION_POLICY)
    return value


def validate_condition_plan(plan: Mapping[str, Any]) -> list[str]:
    """Return missing or invalid Agent-owned condition fields."""

    missing: list[str] = []
    _validate_condition(
        plan.get("effect_condition"),
        name="effect_condition",
        operators=EFFECT_OPERATORS,
        missing=missing,
    )
    _validate_condition(
        plan.get("recovery_condition"),
        name="recovery_condition",
        operators=RECOVERY_OPERATORS,
        missing=missing,
    )
    return missing


def condition_plan_complete(plan: Mapping[str, Any]) -> bool:
    return not validate_condition_plan(plan)


def _validate_condition(
    raw: Any,
    *,
    name: str,
    operators: frozenset[str],
    missing: list[str],
) -> None:
    if not isinstance(raw, Mapping):
        missing.append(name)
        return
    metric = str(raw.get("metric") or "")
    if metric not in WORKLOAD_METRICS:
        missing.append(f"{name}.metric")
    operator = str(raw.get("operator") or "")
    if operator not in operators:
        missing.append(f"{name}.operator")
    threshold = raw.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) < 0
    ):
        missing.append(f"{name}.threshold")
    minimum_requests = raw.get("minimum_requests")
    if (
        isinstance(minimum_requests, bool)
        or not isinstance(minimum_requests, int)
        or minimum_requests < 1
    ):
        missing.append(f"{name}.minimum_requests")


def condition_policy_summary() -> dict[str, Any]:
    return dict(CONDITION_POLICY)


def evaluate_condition(
    condition: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    sample: Mapping[str, Any],
    counter_anchor: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Evaluate one approved condition against raw workload snapshots."""

    metric = str(condition["metric"])
    operator = str(condition["operator"])
    threshold = float(condition["threshold"])
    minimum_requests = int(condition["minimum_requests"])
    anchor = baseline if counter_anchor is None else counter_anchor
    request_delta = _counter_delta(
        sample.get("target_requests"), anchor.get("target_requests")
    )
    baseline_raw = baseline.get(metric)
    baseline_value = (
        float(baseline_raw)
        if isinstance(baseline_raw, (int, float)) and not isinstance(baseline_raw, bool)
        else None
    )
    sample_value = _metric_value(metric, anchor, sample)
    enough_requests = request_delta >= minimum_requests
    matched = False
    if sample_value is not None and baseline_value is not None and enough_requests:
        if operator == "increase_by_at_least":
            matched = sample_value - baseline_value >= threshold
        elif operator == "decrease_by_at_least":
            matched = baseline_value - sample_value >= threshold
        elif operator == "at_or_above":
            matched = sample_value >= threshold
        elif operator == "at_or_below":
            matched = sample_value <= threshold
        elif operator == "within_baseline_delta":
            matched = abs(sample_value - baseline_value) <= threshold
    return matched, {
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
        "minimum_requests": minimum_requests,
        "request_delta": request_delta,
        "enough_requests": enough_requests,
        "baseline_value": baseline_value,
        "observed_value": sample_value,
        "matched": matched,
        "sample_status": sample.get("sample_status") or (
            "valid" if sample.get("traffic_observed") is True else "insufficient"
        ),
    }


def _metric_value(
    metric: str,
    baseline: Mapping[str, Any],
    sample: Mapping[str, Any],
) -> float | None:
    if metric == "target_latency_ms":
        request_delta = _counter_delta(
            sample.get("target_requests"), baseline.get("target_requests")
        )
        response_delta = _number(sample.get("target_response_sum_ms")) - _number(
            baseline.get("target_response_sum_ms")
        )
        if request_delta > 0 and response_delta >= 0:
            return response_delta / request_delta
    if metric == "target_success_rate":
        request_delta = _counter_delta(
            sample.get("target_requests"), baseline.get("target_requests")
        )
        failure_delta = _counter_delta(
            sample.get("target_failures", sample.get("cart_failures")),
            baseline.get("target_failures", baseline.get("cart_failures")),
        )
        if request_delta > 0 and 0 <= failure_delta <= request_delta:
            return (request_delta - failure_delta) / request_delta
    value = sample.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _counter_delta(current: Any, baseline: Any) -> int:
    try:
        left = int(current or 0)
        right = int(baseline or 0)
    except (TypeError, ValueError):
        return 0
    return left - right if left >= right else left


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
