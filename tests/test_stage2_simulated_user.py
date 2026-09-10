from __future__ import annotations

import pytest

from controller.safety import default_policy
from stage2_service.contracts import AutonomyLevel, DecisionPolicy, ExpectedOutcome
from stage2_service.plan_schema import PlanSafetyEnvelope
from stage2_service.simulated_user import ConversationError, HarnessResponder, SimulatedUserPolicy


PLAN = {
    "target": {
        "namespace": "otel-demo",
        "name": "cart-a",
        "uid": "11111111-2222-4333-8444-555555555555",
    },
    "fault_type": "latency",
    "intensity": {"delay_ms": 300},
    "effect_condition": {
        "metric": "target_latency_ms",
        "operator": "increase_by_at_least",
        "threshold": 100,
    },
    "recovery_condition": {
        "metric": "target_latency_ms",
        "operator": "within_baseline_delta",
        "threshold": 50,
    },
    "stop_conditions": ["效果条件成立后主动恢复"],
}


def policy(
    *,
    expected_outcome: ExpectedOutcome = ExpectedOutcome.EXECUTE_AND_RECOVER,
    prompt_level: AutonomyLevel = AutonomyLevel.L3_STRATEGY_SELECTION,
    decision_policy: DecisionPolicy = DecisionPolicy.CLARIFY_MISSING,
) -> SimulatedUserPolicy:
    return SimulatedUserPolicy.from_limits(
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        allowed_fault_types=("network-delay", "network-loss"),
        expected_outcome=expected_outcome,
        decision_policy=decision_policy,
        prompt_level=prompt_level,
        envelope=PlanSafetyEnvelope.from_controller_policy(
            default_policy({"otel-demo"}),
            allowed_fault_types=("network-delay", "network-loss"),
            max_effect_observation_seconds=300,
            max_recovery_observation_seconds=300,
        ).model_copy(update={"max_fault_duration_seconds": 600}),
    )


def responder(model_call, *, user_policy=None) -> HarnessResponder:
    return HarnessResponder(
        model_call=model_call,
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        policy=user_policy or policy(),
    )


def confirmation(recommendation):
    return {
        "question_id": "question-0123456789abcdef",
        "version": 1,
        "request_kind": "confirmation",
        "recommendation": recommendation,
    }


def test_complete_recommendation_with_latency_alias_is_approved_without_model_call():
    calls = []

    answer = responder(lambda *_args: calls.append(_args)).reply(
        confirmation(PLAN),
        {},
    )

    assert calls == []
    assert answer["approved"] is True
    assert answer["answer_mode"] == "approve_recommendation"
    assert answer["affected_nodes"] == []
    assert answer["decision_supplied"] is False
    assert answer["approved_plan"]["fault_type"] == "network-delay"
    assert answer["approved_plan"]["effect_condition"]["threshold_tolerance_ratio"] == 0.6
    assert "minimum_requests" not in answer["approved_plan"]["effect_condition"]


def test_safe_refusal_rejects_confirmation_without_model_completion():
    calls = []

    answer = responder(
        lambda *_args: calls.append(_args),
        user_policy=policy(expected_outcome=ExpectedOutcome.SAFE_REFUSAL),
    ).reply(confirmation(PLAN), {})

    assert calls == []
    assert answer["approved"] is False
    assert answer["answer_mode"] == "reject"
    assert answer["approved_plan"] is None
    assert answer["reason"] == "safe_refusal_expected"


def test_invalid_string_intensity_is_rejected_without_model_repair():
    calls = []
    bad_plan = {**PLAN, "fault_type": "network-loss", "intensity": {"loss_percent": "100%"}}

    answer = responder(lambda *_args: calls.append(_args)).reply(
        confirmation(bad_plan),
        {},
    )

    assert calls == []
    assert answer["approved"] is False
    assert answer["answer_mode"] == "reject"
    assert "INVALID_INTENSITY_VALUE" in answer["message"]


def test_model_supplied_plan_is_revalidated_and_diff_drives_affected_nodes():
    partial = {
        "target": PLAN["target"],
        "fault_type": PLAN["fault_type"],
        "intensity": PLAN["intensity"],
        "stop_conditions": PLAN["stop_conditions"],
    }

    answer = responder(
        lambda _instructions, _context: {
            "message": "补齐效果与恢复条件。",
            "plan": PLAN,
            "affected_nodes": ["TARGET_IDENTITY", "FAULT_CLEARED"],
        }
    ).reply(
        {
            **confirmation(partial),
            "request_kind": "decision_help",
            "required_decisions": ["effect_condition", "recovery_condition"],
        },
        {},
    )

    assert answer["approved"] is True
    assert answer["answer_mode"] == "custom"
    assert answer["affected_nodes"] == ["PLAN_VALIDATION"]
    assert answer["decision_supplied"] is True


def test_incomplete_confirmation_can_be_completed_when_missing_fields_are_allowed():
    partial = {
        "target": PLAN["target"],
        "fault_type": PLAN["fault_type"],
        "intensity": PLAN["intensity"],
        "stop_conditions": PLAN["stop_conditions"],
    }
    calls = []

    answer = responder(
        lambda instructions, context: calls.append((instructions, context))
        or {
            "message": "补齐效果与恢复条件。",
            "plan": {
                "effect_condition": PLAN["effect_condition"],
                "recovery_condition": PLAN["recovery_condition"],
            },
        },
        user_policy=policy(prompt_level=AutonomyLevel.L1_COMPLETE_EXPERIMENT),
    ).reply(confirmation(partial), {})

    assert len(calls) == 1
    assert answer["approved"] is True
    assert answer["answer_mode"] == "custom"
    assert answer["approved_plan"]["effect_condition"]["threshold_tolerance_ratio"] == 0.6
    assert answer["affected_nodes"] == ["PLAN_VALIDATION"]


def test_incomplete_confirmation_with_invalid_field_is_rejected_without_model_call():
    calls = []
    invalid = {
        "target": PLAN["target"],
        "fault_type": PLAN["fault_type"],
        "intensity": {"delay_ms": "300ms"},
        "stop_conditions": PLAN["stop_conditions"],
    }

    answer = responder(
        lambda *_args: calls.append(_args),
        user_policy=policy(prompt_level=AutonomyLevel.L1_COMPLETE_EXPERIMENT),
    ).reply(confirmation(invalid), {})

    assert calls == []
    assert answer["approved"] is False
    assert answer["reason"] == "plan_schema_invalid"
    assert "INVALID_INTENSITY_VALUE" in answer["message"]


def test_decision_help_can_return_partial_target_suggestion_without_approval():
    answer = responder(
        lambda _instructions, _context: {
            "message": "建议先绑定 cart-a 这个 Pod。",
            "plan": {"target": PLAN["target"]},
        },
    ).reply(
        {
            **confirmation(None),
            "request_kind": "decision_help",
            "required_decisions": ["target_pod"],
        },
        {},
    )

    assert answer["approved"] is None
    assert answer["approved_plan"] is None
    assert answer["supplied_plan"] == {
        "target": {**PLAN["target"], "kind": "Pod"},
    }
    assert answer["affected_nodes"] == ["TARGET_IDENTITY"]
    assert answer["supplied_fields"] == ["target"]


def test_l0_policy_does_not_let_model_supply_missing_plan_fields():
    calls = []
    partial = {
        "target": PLAN["target"],
        "fault_type": PLAN["fault_type"],
        "intensity": PLAN["intensity"],
    }

    answer = responder(
        lambda *_args: calls.append(_args),
        user_policy=policy(prompt_level=AutonomyLevel.L0_COMPLETE_TASK),
    ).reply(
        {
            **confirmation(partial),
            "request_kind": "decision_help",
            "required_decisions": ["effect_condition"],
        },
        {},
    )

    assert calls == []
    assert answer["approved"] is False
    assert answer["reason"] == "simulated_user_not_allowed_to_supply_decision"


def test_lower_prompt_level_rejects_model_supplied_target_change():
    original = {
        **PLAN,
        "target": {
            "namespace": "otel-demo",
            "name": "cart-a",
            "uid": "11111111-2222-4333-8444-555555555555",
        },
    }
    changed = {
        **PLAN,
        "target": {
            "namespace": "otel-demo",
            "name": "cart-b",
            "uid": "99999999-2222-4333-8444-555555555555",
        },
    }

    answer = responder(
        lambda _instructions, _context: {
            "message": "改用另一个 Pod 并补齐条件。",
            "plan": changed,
        },
        user_policy=policy(prompt_level=AutonomyLevel.L1_COMPLETE_EXPERIMENT),
    ).reply(
        {
            **confirmation(
                {
                    "target": original["target"],
                    "fault_type": original["fault_type"],
                    "intensity": original["intensity"],
                }
            ),
            "request_kind": "decision_help",
        },
        {},
    )

    assert answer["approved"] is False
    assert answer["reason"] == "simulated_user_policy_violation"
    assert "target" in answer["message"]


def test_approval_text_with_invalid_model_plan_raises_for_repair_loop():
    with pytest.raises(ConversationError, match="valid AgentPlan"):
        responder(
            lambda _instructions, _context: {
                "message": "确认执行",
                "plan": {
                    **PLAN,
                    "intensity": {"delay_ms": "300ms"},
                },
            }
        ).reply(
            {
                **confirmation({"target": PLAN["target"]}),
                "request_kind": "decision_help",
            },
            {},
        )
