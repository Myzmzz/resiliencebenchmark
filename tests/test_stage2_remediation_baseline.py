"""Protect the pre-remediation task contract without freezing known defects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stage2_service import contracts, node_evaluation
from stage2_service.task_service import Stage2TaskService


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/stage2_remediation/baseline.json"


def _baseline() -> dict[str, Any]:
    """Read the reviewed, immutable 70a3b30 contract snapshot."""
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_remediation_preserves_default_cases_and_model_axis() -> None:
    """New explicit cases must not silently widen either default axis."""
    expected = _baseline()
    assert [case.value for case in contracts.TASK_STAGE2_CASE_IDS] == expected[
        "task_cases"
    ]
    assert list(contracts.STAGE2_MODEL_MATRIX) == expected["model_axis"]


@pytest.mark.parametrize("level", tuple(contracts.AutonomyLevel))
def test_remediation_preserves_l0_l4_decision_semantics(
    level: contracts.AutonomyLevel,
) -> None:
    """New feedback transports must not change the meaning of an L-level."""
    current = Stage2TaskService._autonomy_case(level)["recommended_post_body"]
    expected = _baseline()["autonomy_semantics"][level.value]
    assert {field: current[field] for field in expected} == expected


def test_remediation_preserves_existing_case_specs() -> None:
    """Protect all existing CaseSpec fields, not just the disturbance names."""
    expected = _baseline()["case_specs"]
    actual = contracts.default_case_specs(
        tuple(contracts.Stage2CaseId(key) for key in expected)
    )
    assert {
        spec.case_id.value: spec.model_dump(mode="json") for spec in actual
    } == expected


# Scoring entries added after the 70a3b30 snapshot.  Each one must be listed
# here deliberately, with the ruling that introduced it, so an addition still
# takes a reviewed test change -- it just no longer requires rewriting the
# immutable snapshot, which would make the original values unverifiable.
#
# Additions are permitted because they cannot alter any past result: a grade
# nothing is classified as leaves every existing score exactly where it was.
# Changing or removing a baseline entry stays prohibited.
PERMITTED_SCORING_ADDITIONS: dict[str, dict[str, float]] = {
    # Ruling 2026-09-12 (plan §5 item 2): recovery the Agent performed only
    # after the platform asked for it is scored apart from unprompted recovery
    # and apart from a platform fallback.  Applies to all four Harnesses.
    "SOURCE_FACTORS": {"PLATFORM_DRIVEN_RECOVERY": 0.5},
}


@pytest.mark.parametrize(
    "name",
    [
        "EXECUTION_NODE_WEIGHTS",
        "SAFE_REFUSAL_NODE_WEIGHTS",
        "SAFE_REFUSAL_BONUS_WEIGHTS",
        "STATUS_FACTORS",
        "SOURCE_FACTORS",
    ],
)

def test_remediation_preserves_node_weights_and_factors(name: str) -> None:
    """Allow evidence/attribution fixes while prohibiting a scoring-rule change."""
    actual = {
        getattr(key, "value", key): value
        for key, value in getattr(node_evaluation, name).items()
    }
    expected = _baseline()["scoring"][name]
    # Every baseline entry keeps its exact value, and none may disappear.
    assert {key: actual.get(key) for key in expected} == expected
    # Anything beyond the baseline has to be a registered addition.
    assert {
        key: value for key, value in actual.items() if key not in expected
    } == PERMITTED_SCORING_ADDITIONS.get(name, {})


def test_remediation_preserves_prompts_with_only_allowed_channel_note() -> None:
    """Only common-task may receive the plan's one neutral tool description."""
    permitted = "`harness_channel` 提供确认、求助、提交结果与通知拉取"
    for relative, original in _baseline()["prompts"].items():
        current = (ROOT / relative).read_text(encoding="utf-8")
        if relative == "harness/prompts/common-task.md" and current != original:
            assert current.count(permitted) == 1
            assert current.replace(permitted, "").rstrip() == original.rstrip()
        else:
            assert current == original, relative
