from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from scoring.runtime import score_final_cohort, score_provisional_run

REPO_ROOT = Path(__file__).resolve().parents[1]


def _levels(duration: int, tokens: int, tools: int):
    return [
        {
            "level_id": "L1",
            "attempt": 1,
            "primary_status": "PASS",
            "metrics": {
                "duration_seconds": duration,
                "tokens_used": tokens,
                "tool_calls": tools,
            },
        }
    ]


def test_single_run_score_is_explicitly_provisional_and_schema_valid() -> None:
    result = score_provisional_run(
        run_id="run-1",
        episode_id="EPI-1",
        agent_id="codex-gpt-5.6",
        level_results=_levels(900, 100_000, 100),
        total_levels=1,
        evidence_refs=["oracle/level-L1.json"],
    )

    schema = json.loads(
        (REPO_ROOT / "scoring/schemas/run-score.schema.json").read_text()
    )
    episode_schema = json.loads(
        (REPO_ROOT / "scoring/schemas/episode-score.schema.json").read_text()
    )
    jsonschema.validate(result, schema)
    jsonschema.validate(result["episode_score"], episode_schema)
    assert result["score_status"] == "provisional"
    assert result["episode_score"]["efficiency_score"] == 0.5


def test_official_score_requires_a_real_comparison_cohort() -> None:
    with pytest.raises(ValueError, match="at least two"):
        score_final_cohort(
            [
                {
                    "key": "codex",
                    "run_id": "run-1",
                    "episode_id": "EPI-1",
                    "agent_id": "codex",
                    "level_results": _levels(100, 1000, 10),
                    "total_levels": 1,
                }
            ],
            comparison_group="group-1",
        )


def test_final_cohort_normalizes_only_within_the_frozen_group() -> None:
    records = [
        {
            "key": "codex",
            "run_id": "run-1",
            "episode_id": "EPI-1",
            "agent_id": "codex",
            "level_results": _levels(100, 1000, 10),
            "total_levels": 1,
        },
        {
            "key": "claude",
            "run_id": "run-2",
            "episode_id": "EPI-1",
            "agent_id": "claude",
            "level_results": _levels(200, 2000, 20),
            "total_levels": 1,
        },
    ]

    results = score_final_cohort(records, comparison_group="same-episode-policy")

    assert all(item["score_status"] == "final" for item in results)
    assert results[0]["episode_score"]["efficiency_score"] == 1.0
    assert results[1]["episode_score"]["efficiency_score"] == 0.0
