import json
from pathlib import Path

import jsonschema

from scoring.aggregator import aggregate_agent_scores, normalize_and_score_cohort
from scoring.calculator import calculate_completeness, calculate_episode_score, normalize_efficiency


REPO_ROOT = Path(__file__).resolve().parents[1]


def result(level, attempt, status="PASS"):
    return {"level_id": level, "attempt": attempt, "primary_status": status}


def test_completeness_combines_coverage_and_retry_penalty_without_double_penalty():
    completeness, penalty, passed, attempts = calculate_completeness(
        [result("L1", 1), result("L2", 1, "FAIL"), result("L2", 2)],
        total_levels=3,
    )

    assert completeness == 0.5
    assert penalty == 0.75
    assert passed == 2
    assert attempts == {"L1": 1, "L2": 2}


def test_efficiency_is_cohort_relative_and_lower_is_better():
    scores = normalize_efficiency(
        [
            {"key": "fast", "metrics": {"duration_seconds": 10, "tokens_used": 100, "tool_calls": 5}},
            {"key": "slow", "metrics": {"duration_seconds": 20, "tokens_used": 200, "tool_calls": 10}},
        ]
    )

    assert scores == {"fast": 1.0, "slow": 0.0}


def test_episode_score_matches_schema_and_safety_formula():
    score = calculate_episode_score(
        episode_id="EPI-1",
        agent_id="agent-a",
        level_results=[result("L1", 1), result("L2", 2), result("L3", 1)],
        total_levels=3,
        efficiency_score=0.75,
        violations=["timeout"],
        max_violations=4,
    )

    assert score["completeness_score"] == 0.833333
    assert score["safety_score"] == 0.75
    assert score["final_score"] == 0.791667
    schema = json.loads(
        (REPO_ROOT / "scoring/schemas/episode-score.schema.json").read_text()
    )
    jsonschema.validate(score, schema)


def test_cohort_scoring_and_difficulty_weighted_aggregation():
    cohort = normalize_and_score_cohort(
        [
            {
                "episode_id": "EPI-1",
                "agent_id": "a",
                "comparison_group": "EPI-1",
                "level_results": [result("L1", 1)],
                "total_levels": 1,
                "metrics": {"duration_seconds": 10, "tokens_used": 100, "tool_calls": 5},
            },
            {
                "episode_id": "EPI-1",
                "agent_id": "b",
                "comparison_group": "EPI-1",
                "level_results": [result("L1", 1)],
                "total_levels": 1,
                "metrics": {"duration_seconds": 20, "tokens_used": 200, "tool_calls": 10},
            },
        ]
    )
    aggregate = aggregate_agent_scores(cohort)

    assert cohort[0]["efficiency_score"] == 1.0
    assert cohort[1]["efficiency_score"] == 0.0
    assert aggregate[0]["agent_id"] == "a"
