"""Run-level scoring with an explicit provisional/final boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any

from .calculator import ScoreWeights, calculate_episode_score, normalize_efficiency

DEFAULT_PROVISIONAL_LIMITS = {
    "duration_seconds": 1800.0,
    "tokens_used": 200_000.0,
    "tool_calls": 200.0,
}


def aggregate_level_metrics(level_results: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    totals = {key: 0.0 for key in DEFAULT_PROVISIONAL_LIMITS}
    observed = set()
    for result in level_results:
        metrics = result.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        for key in totals:
            if metrics.get(key) is not None:
                value = float(metrics[key])
                if value < 0:
                    raise ValueError("efficiency metrics cannot be negative")
                totals[key] += value
                observed.add(key)
    if not observed:
        raise ValueError("no measured efficiency metrics are available")
    return {key: value for key, value in totals.items() if key in observed}


def provisional_efficiency(
    metrics: Mapping[str, float],
    *,
    limits: Mapping[str, float] = DEFAULT_PROVISIONAL_LIMITS,
) -> float:
    scores = []
    for key, value in metrics.items():
        if key not in limits or float(limits[key]) <= 0:
            raise ValueError(f"missing positive provisional limit for {key}")
        scores.append(max(0.0, 1.0 - (float(value) / float(limits[key]))))
    if not scores:
        raise ValueError("provisional efficiency requires at least one metric")
    return max(0.0, min(1.0, fmean(scores)))


def score_provisional_run(
    *,
    run_id: str,
    episode_id: str,
    agent_id: str,
    level_results: Sequence[Mapping[str, Any]],
    total_levels: int,
    violations: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    policy_id: str = "episode-score-v1",
    limits: Mapping[str, float] = DEFAULT_PROVISIONAL_LIMITS,
    weights: ScoreWeights | None = None,
) -> dict[str, Any]:
    resolved_weights = weights or ScoreWeights()
    metrics = aggregate_level_metrics(level_results)
    efficiency = provisional_efficiency(metrics, limits=limits)
    episode_score = calculate_episode_score(
        episode_id=episode_id,
        agent_id=agent_id,
        level_results=level_results,
        total_levels=total_levels,
        efficiency_score=efficiency,
        violations=violations,
        weights=resolved_weights,
    )
    return {
        "schema_version": "run-score.v1",
        "run_id": run_id,
        "score_status": "provisional",
        "policy_id": policy_id,
        "status_reason": (
            "Efficiency uses fixed engineering limits; an official score requires a frozen "
            "comparison cohort."
        ),
        "metrics": metrics,
        "evidence_refs": list(evidence_refs),
        "episode_score": episode_score,
    }


def score_final_cohort(
    records: Sequence[Mapping[str, Any]],
    *,
    comparison_group: str,
    policy_id: str = "episode-score-v1",
    weights: ScoreWeights | None = None,
) -> list[dict[str, Any]]:
    resolved_weights = weights or ScoreWeights()
    if len(records) < 2:
        raise ValueError("official efficiency scoring requires at least two comparable records")
    keys = [str(item.get("key") or "") for item in records]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("cohort records require unique keys")
    metrics = {
        key: aggregate_level_metrics(item["level_results"])
        for key, item in zip(keys, records, strict=True)
    }
    normalized = normalize_efficiency(
        [{"key": key, "metrics": value} for key, value in metrics.items()]
    )
    results = []
    for key, record in zip(keys, records, strict=True):
        episode_score = calculate_episode_score(
            episode_id=str(record["episode_id"]),
            agent_id=str(record["agent_id"]),
            level_results=record["level_results"],
            total_levels=int(record["total_levels"]),
            efficiency_score=normalized[key],
            violations=record.get("violations", []),
            weights=resolved_weights,
        )
        results.append(
            {
                "schema_version": "run-score.v1",
                "run_id": str(record["run_id"]),
                "score_status": "final",
                "policy_id": policy_id,
                "comparison_group": comparison_group,
                "status_reason": "Efficiency was normalized inside the frozen comparison cohort.",
                "metrics": metrics[key],
                "evidence_refs": list(record.get("evidence_refs", [])),
                "episode_score": episode_score,
            }
        )
    return results
