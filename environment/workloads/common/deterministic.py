"""Deterministic scheduling helpers shared by benchmark Locust profiles."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Iterable, Sequence, TypeVar


T = TypeVar("T")


def exact_percent_schedule(seed: int, weighted_flows: Iterable[tuple[str, int]]) -> tuple[str, ...]:
    if not isinstance(seed, int) or isinstance(seed, bool) or seed <= 0:
        raise ValueError("seed must be a positive integer")
    schedule: list[str] = []
    seen: set[str] = set()
    for flow, weight in weighted_flows:
        if not isinstance(flow, str) or not flow or flow in seen:
            raise ValueError("flow ids must be unique non-empty strings")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError("flow weights must be positive integers")
        seen.add(flow)
        schedule.extend([flow] * weight)
    if len(schedule) != 100:
        raise ValueError("flow weights must sum to 100")
    for index in range(len(schedule) - 1, 0, -1):
        digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
        selected = int.from_bytes(digest[:8], "big") % (index + 1)
        schedule[index], schedule[selected] = schedule[selected], schedule[index]
    return tuple(schedule)


def deterministic_index(seed: int, user_index: int, iteration: int, label: str, size: int) -> int:
    if size <= 0:
        raise ValueError("cannot choose from an empty sequence")
    payload = f"{seed}:{user_index}:{iteration}:{label}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % size


def deterministic_choice(values: Sequence[T], seed: int, user_index: int, iteration: int, label: str) -> T:
    return values[deterministic_index(seed, user_index, iteration, label, len(values))]


def deterministic_uuid(seed: int, user_index: int, iteration: int, label: str) -> str:
    digest = bytearray(hashlib.sha256(f"{seed}:{user_index}:{iteration}:{label}".encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def evaluation_window_plan(duration_seconds: int, warmup_seconds: int, evaluation_window_seconds: int) -> dict[str, Any]:
    """Resolve the cumulative-stat reset used to measure the final SLO window."""
    for name, value in (
        ("duration_seconds", duration_seconds),
        ("warmup_seconds", warmup_seconds),
        ("evaluation_window_seconds", evaluation_window_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if evaluation_window_seconds <= 0:
        raise ValueError("evaluation_window_seconds must be positive")
    effective_window = min(duration_seconds, evaluation_window_seconds)
    reset_after = duration_seconds - effective_window
    return {
        "durationSeconds": duration_seconds,
        "configuredWarmupSeconds": warmup_seconds,
        "appliedWarmupSeconds": min(warmup_seconds, reset_after),
        "configuredEvaluationWindowSeconds": evaluation_window_seconds,
        "measurementWindowSeconds": effective_window,
        "resetAfterSeconds": reset_after,
        "calibrationWindowEligible": reset_after >= warmup_seconds and effective_window == evaluation_window_seconds,
    }


def install_locust_evaluation_window(events: Any) -> dict[str, Any]:
    """Reset Locust cumulative statistics before the final evaluation window."""
    plan = evaluation_window_plan(
        int(os.environ.get("RESBENCH_DURATION_SECONDS", "600")),
        int(os.environ.get("RESBENCH_WARMUP_SECONDS", "60")),
        int(os.environ.get("RESBENCH_EVALUATION_WINDOW_SECONDS", "300")),
    )

    @events.test_start.add_listener
    def schedule_stats_reset(environment: Any, **_: Any) -> None:
        delay = int(plan["resetAfterSeconds"])
        if delay <= 0:
            return
        import gevent

        def reset() -> None:
            environment.stats.reset_all()
            print(
                json.dumps(
                    {
                        "event": "resiliencebenchmark_evaluation_window_started",
                        "measurementWindowSeconds": plan["measurementWindowSeconds"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        gevent.spawn_later(delay, reset)

    return plan
