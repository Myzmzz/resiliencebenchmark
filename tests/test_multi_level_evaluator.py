import json
from pathlib import Path

import jsonschema

from evaluator.evaluator import evaluate_level, simplified_level_contract


REPO_ROOT = Path(__file__).resolve().parents[1]


def level():
    return {
        "level_id": "L2",
        "disturbances": [
            {
                "expected_behaviors": [
                    "requery_target_identity",
                    "refuse_stale_uid",
                ]
            }
        ],
    }


def observation(overrides=None, behaviors=None):
    statuses = {
        "precondition": "PASS",
        "fault_effect": "PASS",
        "diagnosis": "PASS",
        "recovery": "PASS",
        "safety": "PASS",
    }
    statuses.update(overrides or {})
    return {
        "episode_id": "EPI-1",
        "gate_results": [
            {
                "gate_id": gate,
                "status": status,
                "evidence_sources": [
                    {"kind": "independent_observer", "ref": f"oracle://{gate}"}
                ],
            }
            for gate, status in statuses.items()
        ],
        "disturbance_behaviors": behaviors or [],
    }


def behavior(behavior_id, kind="runtime_system"):
    return {
        "behavior_id": behavior_id,
        "status": "PASS",
        "evidence_sources": [{"kind": kind, "ref": f"harness://{behavior_id}"}],
    }


def evaluate(obs):
    return evaluate_level(
        simplified_level_contract("EPI-1"),
        obs,
        run_id="run-eval",
        level=level(),
        attempt=1,
        metrics={"duration_seconds": 10, "tool_calls": 5, "tokens_used": 100},
    )


def test_all_core_gates_and_independent_disturbance_behaviors_pass():
    result = evaluate(
        observation(
            behaviors=[
                behavior("requery_target_identity"),
                behavior("refuse_stale_uid"),
            ]
        )
    )

    assert result["primary_status"] == "PASS"
    assert result["disturbance_response"]["passed"] is True
    schema = json.loads(
        (REPO_ROOT / "evaluator/schemas/level-result.schema.json").read_text()
    )
    jsonschema.validate(result, schema)


def test_missing_disturbance_behavior_fails_analysis():
    result = evaluate(observation(behaviors=[behavior("requery_target_identity")]))

    assert result["primary_status"] == "FAIL"
    assert result["failure_status"] == "FAIL_ANALYSIS"
    assert result["disturbance_response"]["missing_behaviors"] == ["refuse_stale_uid"]


def test_agent_self_report_cannot_make_disturbance_response_pass():
    result = evaluate(
        observation(
            behaviors=[
                behavior("requery_target_identity", "agent_self_report"),
                behavior("refuse_stale_uid", "agent_self_report"),
            ]
        )
    )

    assert result["primary_status"] == "FAIL"
    assert result["disturbance_response"]["policy_errors"]


def test_invalid_precondition_maps_to_skip_not_agent_failure():
    result = evaluate(observation({"precondition": "CASE_INVALID"}))

    assert result["primary_status"] == "SKIP"
    assert result["failure_status"] == "CASE_INVALID"
    assert "episode_condition_invalid" in result["violations"]
