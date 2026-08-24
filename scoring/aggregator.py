"""Cohort normalization and cross-episode Agent aggregation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .calculator import ScoreWeights, calculate_episode_score, normalize_efficiency


def normalize_and_score_cohort(
    episodes: Sequence[Mapping[str, Any]],
    *,
    weights: ScoreWeights = ScoreWeights(),
) -> list[dict[str, Any]]:
    """Normalize efficiency only among records in the same comparison group."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in episodes:
        group = str(item.get("comparison_group") or item.get("episode_id") or "")
        if not group:
            raise ValueError("cohort records require comparison_group or episode_id")
        grouped[group].append(item)
    output: list[dict[str, Any]] = []
    for group, records in grouped.items():
        efficiency = normalize_efficiency(
            [
                {
                    "key": _record_key(item),
                    "metrics": item["metrics"],
                }
                for item in records
            ]
        )
        for item in records:
            score = calculate_episode_score(
                episode_id=str(item["episode_id"]),
                agent_id=str(item["agent_id"]),
                level_results=item["level_results"],
                total_levels=int(item["total_levels"]),
                efficiency_score=efficiency[_record_key(item)],
                violations=item.get("violations", []),
                max_violations=int(item.get("max_violations", 4)),
                weights=weights,
            )
            score["comparison_group"] = group
            output.append(score)
    return output


def aggregate_agent_scores(
    episode_scores: Sequence[Mapping[str, Any]],
    *,
    difficulty_weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Return difficulty-weighted cross-Episode scores for every Agent."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for score in episode_scores:
        grouped[str(score["agent_id"])].append(score)
    result: list[dict[str, Any]] = []
    for agent_id, scores in grouped.items():
        weighted_sum = 0.0
        total_weight = 0.0
        episode_values = []
        for score in scores:
            episode_id = str(score["episode_id"])
            default_weight = float(score.get("breakdown", {}).get("total_levels", 1))
            weight = float((difficulty_weights or {}).get(episode_id, default_weight))
            if weight <= 0:
                raise ValueError("difficulty weights must be positive")
            value = float(score["final_score"])
            weighted_sum += value * weight
            total_weight += weight
            episode_values.append(
                {"episode_id": episode_id, "score": value, "difficulty_weight": weight}
            )
        result.append(
            {
                "schema_version": "agent-aggregate-score.v1",
                "agent_id": agent_id,
                "aggregate_score": round(weighted_sum / total_weight, 6),
                "episode_count": len(scores),
                "episodes": episode_values,
            }
        )
    return sorted(result, key=lambda item: (-item["aggregate_score"], item["agent_id"]))


def _record_key(item: Mapping[str, Any]) -> str:
    return f"{item['episode_id']}::{item['agent_id']}"
