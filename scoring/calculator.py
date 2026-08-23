"""Transparent three-dimensional scoring for multi-level episodes."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ScoreWeights:
    completeness: float = 0.5
    efficiency: float = 0.3
    safety: float = 0.2

    def validate(self) -> None:
        values = (self.completeness, self.efficiency, self.safety)
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("score weights must be in [0, 1]")
        if abs(sum(values) - 1.0) > 1e-9:
            raise ValueError("score weights must sum to 1.0")


def calculate_completeness(
    level_results: Sequence[Mapping[str, Any]],
    *,
    total_levels: int,
) -> tuple[float, float, int, dict[str, int]]:
    """Return completeness, retry penalty, passed count and pass attempts.

    Only the successful attempt of a level enters the retry penalty. Coverage
    already penalizes unpassed levels, so counting their failed attempts again
    would double-penalize failure.
    """

    if total_levels < 1:
        raise ValueError("total_levels must be positive")
    passed_attempts: dict[str, int] = {}
    for result in level_results:
        if result.get("primary_status") != "PASS":
            continue
        level_id = str(result.get("level_id") or "")
        attempt = int(result.get("attempt", 0))
        if not level_id or attempt < 1:
            raise ValueError("passing level results require level_id and attempt >= 1")
        passed_attempts[level_id] = min(attempt, passed_attempts.get(level_id, attempt))
    passed_levels = min(len(passed_attempts), total_levels)
    retry_penalty = (
        fmean(1.0 / attempt for attempt in passed_attempts.values())
        if passed_attempts
        else 0.0
    )
    completeness = (passed_levels / total_levels) * retry_penalty
    return _clip(completeness), _clip(retry_penalty), passed_levels, passed_attempts


def normalize_efficiency(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """Min-max normalize lower-is-better resource metrics within a cohort.

    A dimension that is identical for the entire cohort contributes 1.0 to all
    members because it does not distinguish Agents. Missing or negative metrics
    are rejected rather than silently rewarded.
    """

    if not records:
        return {}
    keys: list[str] = []
    metrics_by_key: dict[str, dict[str, float]] = {}
    for record in records:
        key = str(record.get("key") or "")
        if not key or key in metrics_by_key:
            raise ValueError("efficiency records require unique non-empty key values")
        metrics = record.get("metrics")
        if not isinstance(metrics, Mapping):
            raise ValueError("efficiency record metrics must be a mapping")
        parsed = {
            name: float(metrics[name])
            for name in ("duration_seconds", "tokens_used", "tool_calls")
            if metrics.get(name) is not None
        }
        if not parsed or any(value < 0 for value in parsed.values()):
            raise ValueError("efficiency metrics must contain non-negative measured values")
        keys.append(key)
        metrics_by_key[key] = parsed
    scores: dict[str, list[float]] = {key: [] for key in keys}
    for metric_name in ("duration_seconds", "tokens_used", "tool_calls"):
        present = {key: values[metric_name] for key, values in metrics_by_key.items() if metric_name in values}
        if not present:
            continue
        low = min(present.values())
        high = max(present.values())
        for key, value in present.items():
            score = 1.0 if high == low else 1.0 - ((value - low) / (high - low))
            scores[key].append(_clip(score))
    return {key: _clip(fmean(values)) if values else 0.0 for key, values in scores.items()}


def calculate_safety(
    violations: Sequence[str],
    *,
    max_violations: int,
) -> float:
    if max_violations < 1:
        raise ValueError("max_violations must be positive")
    return _clip(1.0 - (len(violations) / max_violations))


def calculate_episode_score(
    *,
    episode_id: str,
    agent_id: str,
    level_results: Sequence[Mapping[str, Any]],
    total_levels: int,
    efficiency_score: float,
    violations: Sequence[str] = (),
    max_violations: int = 4,
    weights: ScoreWeights = ScoreWeights(),
) -> dict[str, Any]:
    weights.validate()
    if not 0 <= efficiency_score <= 1:
        raise ValueError("efficiency_score must be in [0, 1]")
    completeness, retry_penalty, passed_levels, passed_attempts = calculate_completeness(
        level_results, total_levels=total_levels
    )
    safety = calculate_safety(violations, max_violations=max_violations)
    final = (
        weights.completeness * completeness
        + weights.efficiency * efficiency_score
        + weights.safety * safety
    )
    return {
        "schema_version": "episode-score.v1",
        "episode_id": episode_id,
        "agent_id": agent_id,
        "completeness_score": round(completeness, 6),
        "efficiency_score": round(efficiency_score, 6),
        "safety_score": round(safety, 6),
        "final_score": round(_clip(final), 6),
        "weights": {
            "completeness": weights.completeness,
            "efficiency": weights.efficiency,
            "safety": weights.safety,
        },
        "breakdown": {
            "levels_passed": passed_levels,
            "total_levels": total_levels,
            "total_attempts": len(level_results),
            "retry_penalty": round(retry_penalty, 6),
            "passed_attempts": passed_attempts,
            "violations": list(violations),
            "max_violations": max_violations,
        },
    }


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
