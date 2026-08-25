import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from disturbances.types import DisturbanceType
from progression.builder import build_multi_level_episode, wrap_single_level_episode
from progression.controller import (
    EpisodeProgressStatus,
    JsonFileProgressionStore,
    ProgressionController,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def qualified_plan(budget=6, fault="latency"):
    return {
        "episode_id": "EPI-MULTI-001",
        "defect_ref": "RBD-001",
        "application_snapshot": {
            "application": "otel-demo",
            "namespace": "otel-demo",
            "runtime_target": {
                "kind": "Pod",
                "name": "checkout-abc",
                "uid": "uid-1",
            },
        },
        "action_space": {
            "allowed_trigger_classes": [fault],
            "selected_actuator": "network-delay",
            "parameters": [{"name": "delay_ms", "value": 100}],
        },
        "budget": {"max_experiments": budget},
    }


def test_builder_creates_baseline_then_relevant_progressive_disturbances():
    episode = build_multi_level_episode(qualified_plan())

    assert episode["levels"][0]["disturbances"] == []
    assert [len(level["disturbances"]) for level in episode["levels"]] == [0, 1, 2]
    assert episode["levels"][1]["disturbances"][0]["type"] == DisturbanceType.FAULT_EFFECT_DEVIATION.value
    assert sum(level["retry_budget"] for level in episode["levels"]) == 6
    schema = json.loads(
        (REPO_ROOT / "tasks/schemas/multi-level-episode.schema.json").read_text()
    )
    jsonschema.validate(episode, schema)


def test_builder_refuses_budget_that_cannot_reach_every_level():
    with pytest.raises(ValueError, match="cannot fund"):
        build_multi_level_episode(qualified_plan(budget=2), level_count=3)


def test_existing_public_episode_wraps_as_l1_only_without_changing_public_contract():
    public_episode = yaml.safe_load(
        (REPO_ROOT / "tasks/examples/public/episode.timeout-missing.v0.1.yaml").read_text()
    )
    wrapped = wrap_single_level_episode(
        public_episode,
        target={
            "application": "otel-demo",
            "namespace": "otel-demo",
            "kind": "Pod",
            "name": "checkout-abc",
            "uid": "uid-qualified",
        },
        main_fault={
            "type": "network-delay",
            "actuator": "chaosblade-network-delay",
            "parameters": {"delay_ms": 100},
        },
    )

    assert len(wrapped["levels"]) == 1
    assert wrapped["levels"][0]["disturbances"] == []
    assert wrapped["base_task"]["agent_visible_task"] == public_episode
    assert wrapped["total_retry_budget"] == public_episode["budget"]["max_experiments"]


def test_builder_rejects_hidden_answer_keys_in_agent_visible_task():
    with pytest.raises(ValueError, match="forbidden key"):
        build_multi_level_episode(
            qualified_plan(),
            agent_visible_task={"task": "public", "hidden_truth": {"answer": "RBD-001"}},
        )


def test_progression_retries_same_level_and_advances_only_after_pass():
    episode = build_multi_level_episode(qualified_plan())
    controller = ProgressionController(episode, run_id="run-p1", agent_id="agent-a")

    l1 = controller.start_trial()
    controller.record_result(l1.trial_id, primary_status="PASS", result_ref="l1.json")
    first_l2 = controller.start_trial()
    controller.record_result(
        first_l2.trial_id,
        primary_status="FAIL",
        failure_status="FAIL_ANALYSIS",
        result_ref="l2-a1.json",
    )
    second_l2 = controller.start_trial()
    controller.record_result(second_l2.trial_id, primary_status="PASS", result_ref="l2-a2.json")
    l3 = controller.start_trial()
    controller.record_result(l3.trial_id, primary_status="PASS", result_ref="l3.json")

    assert first_l2.level_id == second_l2.level_id == "L2"
    assert second_l2.attempt == 2
    assert controller.status is EpisodeProgressStatus.PASS
    assert controller.snapshot()["total_attempts"] == 4


def test_skip_is_terminal_but_not_fail():
    episode = build_multi_level_episode(qualified_plan())
    controller = ProgressionController(episode, run_id="run-skip", agent_id="agent-a")
    ticket = controller.start_trial()

    controller.record_result(
        ticket.trial_id,
        primary_status="SKIP",
        failure_status="CASE_INVALID",
        result_ref="skip.json",
    )

    assert controller.status is EpisodeProgressStatus.SKIP
    assert controller.snapshot()["terminal_reason"] == "CASE_INVALID"


def test_engineering_progression_executes_all_levels_after_a_scored_failure():
    episode = build_multi_level_episode(qualified_plan(budget=3))
    controller = ProgressionController(
        episode,
        run_id="run-engineering",
        agent_id="agent-a",
        continue_after_failure=True,
    )

    l1 = controller.start_trial()
    controller.record_result(
        l1.trial_id,
        primary_status="FAIL",
        failure_status="INCONCLUSIVE",
        result_ref="l1.json",
    )
    l2 = controller.start_trial()
    controller.record_result(l2.trial_id, primary_status="PASS", result_ref="l2.json")
    l3 = controller.start_trial()
    controller.record_result(l3.trial_id, primary_status="PASS", result_ref="l3.json")

    state = controller.snapshot()
    assert [l1.level_id, l2.level_id, l3.level_id] == ["L1", "L2", "L3"]
    assert state["level_statuses"] == {"L1": "FAIL", "L2": "PASS", "L3": "PASS"}
    assert state["terminal_reason"] == "all_levels_executed_with_failures"
    assert controller.status is EpisodeProgressStatus.FAIL


def test_checkpoint_resume_preserves_attempt_budget(tmp_path):
    episode = build_multi_level_episode(qualified_plan())
    store = JsonFileProgressionStore(tmp_path / "state.json")
    first = ProgressionController(
        episode, run_id="run-resume", agent_id="agent-a", store=store
    )
    ticket = first.start_trial()
    first.record_result(
        ticket.trial_id,
        primary_status="FAIL",
        failure_status="FAIL_EXECUTION",
        result_ref="failed.json",
    )

    resumed = ProgressionController(
        episode,
        run_id="run-resume",
        agent_id="agent-a",
        store=store,
        resume=True,
    )

    assert resumed.start_trial().attempt == 2
