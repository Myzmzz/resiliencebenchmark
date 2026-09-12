from __future__ import annotations

import re

import pytest

from controller.safety import default_policy
from stage2_service.contracts import AutonomyLevel, DecisionPolicy, ExpectedOutcome
from stage2_service.plan_schema import AGENT_PLAN_SKELETON, PlanSafetyEnvelope
from stage2_service.bladeai_shim import NATIVE_INTENSITY_FLAGS, parse_create
from stage2_service.condition_policy import (
    CONDITION_POLICY,
    EFFECT_OPERATORS,
    RECOVERY_OPERATORS,
    WORKLOAD_METRICS,
    WP8_CONDITION_POLICY,
    apply_condition_policy,
)
from stage2_service.contracts import STAGE2_PLATFORM_MODEL
from stage2_service.plan_schema import validate_agent_plan
from stage2_service.simulated_user import (
    EXAMPLE_PLAN_FIELDS,
    METRIC_MEANINGS,
    OPERATOR_MEANINGS,
    PLATFORM_MODEL_ENV,
    ConversationError,
    HarnessResponder,
    SimulatedUserPolicy,
    _attach_condition_policy,
    _uses_responses_api,
    plan_vocabulary,
    resolve_platform_model,
)


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


ALL_FAULT_TYPES = ("network-delay", "network-loss", "cpu-load", "memory-stress")
CPU_PARTIAL_PLAN = {
    "target": PLAN["target"],
    "fault_type": "cpu-load",
    "intensity": {"cpu_percent": 80},
}


def delegated_policy(fault_types=ALL_FAULT_TYPES) -> SimulatedUserPolicy:
    """The policy a BladeAI L0 Trial runs under: the Harness may fill any node."""

    return SimulatedUserPolicy.from_limits(
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        allowed_fault_types=fault_types,
        decision_policy=DecisionPolicy.AGENT_DELEGATED,
        prompt_level=AutonomyLevel.L0_COMPLETE_TASK,
    )


def test_vocabulary_lists_exactly_the_metrics_and_operators_validation_accepts():
    conditions = plan_vocabulary(delegated_policy())["conditions"]

    assert set(METRIC_MEANINGS) == WORKLOAD_METRICS
    assert set(OPERATOR_MEANINGS) == EFFECT_OPERATORS | RECOVERY_OPERATORS
    assert set(conditions["metrics"]) == WORKLOAD_METRICS
    assert set(conditions["effect_operators"]) == EFFECT_OPERATORS
    assert set(conditions["recovery_operators"]) == RECOVERY_OPERATORS


@pytest.mark.parametrize("fault_type", ALL_FAULT_TYPES)
def test_vocabulary_example_plan_passes_validation_once_a_real_target_is_copied(fault_type):
    user_policy = delegated_policy((fault_type,))
    example = plan_vocabulary(user_policy)["example_plan"]

    result = validate_agent_plan(
        _attach_condition_policy({**example, "target": PLAN["target"]}),
        user_policy.envelope,
    )

    assert example["fault_type"] == fault_type
    assert result.ok, result.issues


def test_vocabulary_example_target_is_a_placeholder_that_cannot_pass_validation():
    user_policy = delegated_policy(("cpu-load",))
    example = plan_vocabulary(user_policy)["example_plan"]

    result = validate_agent_plan(_attach_condition_policy(example), user_policy.envelope)

    assert not result.ok


@pytest.mark.parametrize("fault_type", ALL_FAULT_TYPES)
def test_vocabulary_chaosblade_command_is_one_the_blade_shim_accepts(fault_type):
    entry = plan_vocabulary(delegated_policy((fault_type,)))["fault_types"][fault_type]
    value = entry["example_plan_fields"]["intensity"][entry["intensity_field"]]
    command = (
        entry["chaosblade_command"]
        .replace(f"<{entry['intensity_field']}>", str(value))
        .replace("<target.name>", "cart-a")
        .replace("<target.namespace>", "otel-demo")
        .replace("<safety_ttl_seconds>", "120")
    )

    created = parse_create(command.split()[1:], namespace="otel-demo", max_duration_seconds=600)

    assert created.fault_type == fault_type
    assert created.intensity == {entry["intensity_field"]: value}
    assert created.duration_seconds == 120
    assert (entry["chaosblade_flag"], entry["intensity_field"]) == NATIVE_INTENSITY_FLAGS[fault_type]


def test_vocabulary_limits_come_from_the_controller_and_the_trial():
    vocabulary = plan_vocabulary(delegated_policy())

    assert list(vocabulary["fault_types"]) == list(ALL_FAULT_TYPES)
    assert vocabulary["fault_types"]["cpu-load"]["maximum"] == 100
    assert vocabulary["fault_types"]["memory-stress"]["maximum"] == 100
    assert vocabulary["fault_types"]["network-delay"]["maximum"] is None
    assert vocabulary["fault_types"]["network-delay"]["fixed_flags"] == {
        "--interface": "eth0",
        "--offset": "0",
    }
    assert vocabulary["plan_fields"]["safety_ttl_seconds"]["maximum"] == 600


def test_vocabulary_only_describes_the_trials_fault_types():
    vocabulary = plan_vocabulary(delegated_policy(("network-delay",)))

    assert list(vocabulary["fault_types"]) == ["network-delay"]
    assert vocabulary["plan_fields"]["fault_type"]["allowed"] == ["network-delay"]


def test_model_gets_the_vocabulary_and_its_valid_completion_is_approved():
    contexts = []

    answer = responder(
        lambda _instructions, context: contexts.append(context)
        or {
            "message": "补齐效果条件、恢复条件和停止条件。",
            "plan": {
                "effect_condition": EXAMPLE_PLAN_FIELDS["cpu-load"]["effect_condition"],
                "recovery_condition": EXAMPLE_PLAN_FIELDS["cpu-load"]["recovery_condition"],
                "stop_conditions": ["目标服务成功率低于 0.95"],
            },
        },
        user_policy=delegated_policy(),
    ).reply(confirmation(CPU_PARTIAL_PLAN), {})

    assert "cpu-load" in contexts[0]["policy"]["plan_vocabulary"]["fault_types"]
    assert answer["approved"] is True
    assert answer["decision_supplied"] is True


def test_invalid_platform_completion_is_a_failed_completion_not_an_agent_rejection():
    user = responder(
        lambda _instructions, _context: {
            "message": "补齐条件。",
            "plan": {
                "effect_condition": {"metric": "cpu_usage", "operator": "above", "threshold": 50},
                "recovery_condition": EXAMPLE_PLAN_FIELDS["cpu-load"]["recovery_condition"],
                "stop_conditions": ["目标服务成功率低于 0.95"],
            },
        },
        user_policy=delegated_policy(),
    )

    with pytest.raises(ConversationError, match="failed validation"):
        user.reply(confirmation(CPU_PARTIAL_PLAN), {})
    # The retry sends the model what was wrong and where the valid names are.
    assert "plan_vocabulary" in user.reply_errors["question-0123456789abcdef"]


def test_empty_platform_completion_fails_when_only_decisions_were_missing():
    user = responder(
        lambda _instructions, _context: {"message": "请补充效果条件。", "plan": None},
        user_policy=delegated_policy(),
    )

    with pytest.raises(ConversationError):
        user.reply(confirmation(CPU_PARTIAL_PLAN), {})


def test_empty_completion_for_a_plan_without_target_stays_an_agent_rejection():
    answer = responder(
        lambda _instructions, _context: {"message": "请先告诉我要注入的目标 Pod。", "plan": None},
        user_policy=delegated_policy(),
    ).reply(confirmation({"fault_type": "cpu-load", "intensity": {"cpu_percent": 80}}), {})

    assert answer["approved"] is False
    assert answer["reason"] == "plan_schema_invalid"


def test_platform_model_is_fixed_unless_a_supported_override_is_set():
    assert STAGE2_PLATFORM_MODEL == "deepseek-v4-pro-0813"
    assert resolve_platform_model({}) == STAGE2_PLATFORM_MODEL
    assert resolve_platform_model({PLATFORM_MODEL_ENV: "gpt-5.5"}) == "gpt-5.5"
    with pytest.raises(ValueError, match=PLATFORM_MODEL_ENV):
        resolve_platform_model({PLATFORM_MODEL_ENV: "not-a-gateway-alias"})


def test_only_gpt_gateway_routes_use_the_responses_api():
    assert _uses_responses_api("gpt-5.5")
    assert not _uses_responses_api("deepseek-v4-pro-0813")
    assert not _uses_responses_api("qwen3.8-max")


CPU_CONDITIONS = {
    "effect_condition": EXAMPLE_PLAN_FIELDS["cpu-load"]["effect_condition"],
    "recovery_condition": EXAMPLE_PLAN_FIELDS["cpu-load"]["recovery_condition"],
    "stop_conditions": ["目标服务成功率低于 0.95"],
}


def completing_responder(user_policy: SimulatedUserPolicy) -> HarnessResponder:
    """A responder whose model supplies only the conditions."""

    return HarnessResponder(
        model_call=lambda _instructions, _context: {"message": "补齐条件。", "plan": dict(CPU_CONDITIONS)},
        namespace="otel-demo",
        max_fault_seconds=user_policy.envelope.max_fault_duration_seconds,
        max_observation_seconds=300,
        policy=user_policy,
    )


def test_approved_plan_keeps_the_agents_own_fault_duration():
    answer = completing_responder(delegated_policy()).reply(
        confirmation({**CPU_PARTIAL_PLAN, "safety_ttl_seconds": 300}), {}
    )

    assert answer["approved"] is True
    # The create gate compares this with the Agent's --timeout exactly.
    assert answer["approved_plan"]["safety_ttl_seconds"] == 300


def test_missing_fault_duration_falls_back_to_1200_seconds_capped_by_the_trial():
    long_trial = SimulatedUserPolicy.from_limits(
        namespace="otel-demo",
        max_fault_seconds=1200,
        max_observation_seconds=300,
        allowed_fault_types=ALL_FAULT_TYPES,
        decision_policy=DecisionPolicy.AGENT_DELEGATED,
        prompt_level=AutonomyLevel.L0_COMPLETE_TASK,
    )

    uncapped = completing_responder(long_trial).reply(confirmation(CPU_PARTIAL_PLAN), {})
    capped = completing_responder(delegated_policy()).reply(confirmation(CPU_PARTIAL_PLAN), {})

    assert CONDITION_POLICY["safety_ttl_seconds"] == 1200
    assert uncapped["approved_plan"]["safety_ttl_seconds"] == 1200
    # delegated_policy() caps faults at 600 seconds.
    assert capped["approved_plan"]["safety_ttl_seconds"] == 600
    assert (
        plan_vocabulary(delegated_policy())["plan_fields"]["safety_ttl_seconds"]["default_when_absent"]
        == 600
    )


def test_apply_condition_policy_keeps_an_agent_ttl_and_fills_a_missing_one():
    assert apply_condition_policy({"safety_ttl_seconds": 300})["safety_ttl_seconds"] == 300
    assert apply_condition_policy({})["safety_ttl_seconds"] == 1200


def test_wp8_qualification_policy_still_fixes_its_own_fault_duration():
    complete = {**CPU_PARTIAL_PLAN, **CPU_CONDITIONS, "safety_ttl_seconds": 300}

    answer = HarnessResponder(
        model_call=lambda *_args: (_ for _ in ()).throw(AssertionError("model must not be called")),
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        policy=delegated_policy(),
        condition_policy=WP8_CONDITION_POLICY,
    ).reply(confirmation(complete), {})

    assert answer["approved"] is True
    assert answer["approved_plan"]["safety_ttl_seconds"] == WP8_CONDITION_POLICY["safety_ttl_seconds"]


def _no_model(*_args):
    raise AssertionError("model must not be called")


# The wording codex + qwen3.8-max kept sending: conditions in its own words.
CODEX_STYLE_PLAN = {
    "target": PLAN["target"],
    "fault_type": "cpu-load",
    "intensity": {"cpu_percent": 60},
    "effect_condition": {"metric": "cpu_usage", "operator": ">=", "threshold": 0.5},
    "recovery_condition": {"metric": "cpu_usage", "operator": "<=", "threshold": 0.2},
    "stop_conditions": ["目标 Pod 重启"],
}


def test_codex_style_conditions_are_still_rejected_with_every_legal_value_and_a_skeleton():
    plan = {**CODEX_STYLE_PLAN, "target_uid": PLAN["target"]["uid"]}

    answer = responder(_no_model, user_policy=delegated_policy()).reply(confirmation(plan), {})

    # Still a rule rejection without a platform completion, as before.
    assert answer["approved"] is False
    assert answer["answer_mode"] == "reject"
    assert answer["reason"] == "plan_schema_invalid"
    message = answer["message"]
    assert message.startswith("不批准执行：计划未通过类型化校验。")
    for path, code in (
        ("effect_condition.metric", "INVALID_CONDITION_METRIC"),
        ("effect_condition.operator", "INVALID_EFFECT_OPERATOR"),
        ("recovery_condition.metric", "INVALID_CONDITION_METRIC"),
        ("recovery_condition.operator", "INVALID_RECOVERY_OPERATOR"),
        ("target_uid", "PLAN_UNKNOWN_FIELD"),
    ):
        assert f"- {path}: {code} — " in message
    assert "MISSING_TARGET_UID" not in message
    assert "<root>" not in message
    assert f"Use one of these metrics: {', '.join(sorted(WORKLOAD_METRICS))}." in message
    assert (
        "Use one of these effect_condition operators: "
        f"{', '.join(sorted(EFFECT_OPERATORS))}." in message
    )
    assert (
        "Use one of these recovery_condition operators: "
        f"{', '.join(sorted(RECOVERY_OPERATORS))}." in message
    )
    assert "effect_condition 和 recovery_condition 可以省略" in message
    assert AGENT_PLAN_SKELETON in message


def test_misplaced_and_unknown_keys_are_named_as_such_not_as_a_missing_uid():
    plan = {
        **CPU_PARTIAL_PLAN,
        "stop_conditions": ["目标 Pod 重启"],
        "target_uid": PLAN["target"]["uid"],
        "namespace": "otel-demo",
        "baseline": {"target_cpu_cores": 0.1},
        "scope": "single pod",
        "scope_decision": "target only",
    }

    answer = responder(_no_model, user_policy=delegated_policy()).reply(confirmation(plan), {})

    message = answer["message"]
    assert answer["reason"] == "plan_schema_invalid"
    assert "MISSING_TARGET_UID" not in message
    assert (
        "- target_uid: PLAN_UNKNOWN_FIELD — target_uid is not an AgentPlan field. "
        "Move its value to target.uid." in message
    )
    assert (
        "- namespace: PLAN_UNKNOWN_FIELD — namespace is not an AgentPlan field. "
        "Move its value to target.namespace." in message
    )
    for key in ("baseline", "scope", "scope_decision"):
        assert (
            f"- {key}: PLAN_UNKNOWN_FIELD — {key} is not an AgentPlan field. "
            "Remove it; it is not part of the plan." in message
        )
    # Under this policy the omitted conditions are not something to fix.
    assert (
        "- effect_condition: MISSING_PLAN_FIELD — Field required. effect_condition "
        "is optional in this Trial: leave it out and the platform fills it in, or "
        "send a complete one." in message
    )


def test_a_genuinely_missing_target_uid_is_still_missing_target_uid():
    plan = {**CPU_PARTIAL_PLAN, "target": {"namespace": "otel-demo", "name": "cart-a"}}

    answer = responder(_no_model, user_policy=delegated_policy()).reply(confirmation(plan), {})

    assert answer["approved"] is False
    assert "- target.uid: MISSING_TARGET_UID — " in answer["message"]
    assert "PLAN_UNKNOWN_FIELD" not in answer["message"]


def test_plan_without_conditions_is_still_completed_and_approved_as_before():
    # Expected values recorded at 35c9e2c, before the refusal text changed:
    # the completion path must give the same decision and plan.
    contexts = []
    supplied = {
        "effect_condition": {
            "metric": "target_cpu_cores",
            "operator": "increase_by_at_least",
            "threshold": 0.5,
        },
        "recovery_condition": {
            "metric": "target_cpu_cores",
            "operator": "within_baseline_delta",
            "threshold": 0.3,
        },
    }
    plan = {**CPU_PARTIAL_PLAN, "intensity": {"cpu_percent": 60}, "stop_conditions": ["目标 Pod 重启"]}

    answer = responder(
        lambda _instructions, context: contexts.append(context)
        or {"message": "补齐效果条件和恢复条件。", "plan": supplied},
        user_policy=delegated_policy(),
    ).reply(confirmation(plan), {})

    expected_plan = {
        "target": {**PLAN["target"], "kind": "Pod"},
        "fault_type": "cpu-load",
        "intensity": {"cpu_percent": 60.0},
        "effect_condition": {**supplied["effect_condition"], "threshold_tolerance_ratio": 0.6},
        "recovery_condition": supplied["recovery_condition"],
        "stop_conditions": ["目标 Pod 重启"],
        "recovery_mode": "effect_condition",
        "safety_ttl_seconds": 600,
        "effect_observation_seconds": 300,
        "effect_sustain_seconds": 60,
        "agent_cleanup_seconds": 60,
        "recovery_observation_seconds": 180,
        "recovery_sustain_seconds": 60,
    }
    assert len(contexts) == 1
    assert contexts[0]["correction"] is None
    assert {
        key: answer[key]
        for key in ("approved", "answer_mode", "decision_supplied", "reason", "affected_nodes")
    } == {
        "approved": True,
        "answer_mode": "custom",
        "decision_supplied": True,
        "reason": "harness_supplied_decision",
        "affected_nodes": ["PLAN_VALIDATION"],
    }
    assert answer["approved_plan"] == expected_plan
    assert answer["supplied_plan"] == expected_plan
    assert answer["message"].startswith(
        "补齐效果条件和恢复条件。 执行时将 chaos_control 的 duration_seconds 设为 600，"
    )


def test_stricter_policy_refusals_do_not_suggest_leaving_conditions_out():
    strict = policy(prompt_level=AutonomyLevel.L0_COMPLETE_TASK)
    own_wording = {
        **PLAN,
        "effect_condition": {"metric": "latency", "operator": ">=", "threshold": 100},
    }
    without_conditions = {
        key: value
        for key, value in PLAN.items()
        if key not in {"effect_condition", "recovery_condition"}
    }

    invalid = responder(_no_model, user_policy=strict).reply(confirmation(own_wording), {})
    missing = responder(_no_model, user_policy=strict).reply(confirmation(without_conditions), {})

    assert invalid["reason"] == "plan_schema_invalid"
    assert "可以省略" not in invalid["message"]
    # Missing choices under a policy that lets the Harness supply nothing keep
    # their old refusal: who fills them in stays the level's decision.
    assert missing["reason"] == "simulated_user_not_allowed_to_supply_decision"
    assert missing["message"] == "不批准执行：当前提示等级不允许 Harness 代替 Agent 补全关键实验计划。"


# An Lx Trial's hidden execution contract. The channel cuts its envelope from
# it: the only allowed fault type is the contract's, and the fault-duration cap
# is the contract's duration.
HIDDEN_CONTRACT = {
    "target": {
        "namespace": "otel-demo",
        "name": "checkout-7f9c4-hidden",
        "uid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    },
    "fault_type": "cpu-load",
    "intensity": {"cpu_percent": 80},
    "duration_seconds": 300,
}


def lx_policy() -> SimulatedUserPolicy:
    return SimulatedUserPolicy.from_limits(
        namespace="otel-demo",
        max_fault_seconds=HIDDEN_CONTRACT["duration_seconds"],
        max_observation_seconds=300,
        allowed_fault_types=(HIDDEN_CONTRACT["fault_type"],),
        decision_policy=DecisionPolicy.AGENT_DELEGATED,
        prompt_level=AutonomyLevel.L3_STRATEGY_SELECTION,
    )


def test_refusal_text_quotes_no_value_from_the_hidden_contract():
    user = HarnessResponder(
        model_call=_no_model,
        namespace="otel-demo",
        max_fault_seconds=HIDDEN_CONTRACT["duration_seconds"],
        max_observation_seconds=300,
        policy=lx_policy(),
        context={"hidden_execution_contract": HIDDEN_CONTRACT},
    )
    # The Agent found the real target and intensity, but wrote the
    # conditions in its own words and added a baseline.
    plan = {
        **CODEX_STYLE_PLAN,
        "target": HIDDEN_CONTRACT["target"],
        "intensity": HIDDEN_CONTRACT["intensity"],
        "baseline": {"target_cpu_cores": 0.1},
    }

    message = user.reply(confirmation(plan), {})["message"]

    assert "- baseline: PLAN_UNKNOWN_FIELD — " in message
    for value in (
        HIDDEN_CONTRACT["target"]["name"],
        HIDDEN_CONTRACT["target"]["uid"],
        "otel-demo",
        "cpu_percent",
    ):
        assert value not in message
    assert set(re.findall(r"\d+", message)).isdisjoint({"80", "300"})


def test_refusal_text_does_not_reveal_the_trials_fault_type_or_duration_cap():
    plan = {
        "target": PLAN["target"],
        "fault_type": "memory-stress",
        "intensity": {"mem_percent": 50},
        "effect_condition": {
            "metric": "target_memory_mib",
            "operator": "increase_by_at_least",
            "threshold": 64,
        },
        "recovery_condition": {
            "metric": "target_memory_mib",
            "operator": "within_baseline_delta",
            "threshold": 64,
        },
        "stop_conditions": ["目标 Pod 重启"],
        "safety_ttl_seconds": 900,
    }

    answer = responder(_no_model, user_policy=lx_policy()).reply(confirmation(plan), {})

    message = answer["message"]
    assert answer["reason"] == "plan_schema_invalid"
    assert "- fault_type: FAULT_TYPE_NOT_ALLOWED — " in message
    assert "- safety_ttl_seconds: SAFETY_TTL_EXCEEDED — " in message
    # plan_schema's corrections would say "Choose one of: cpu-load" and
    # "Use safety_ttl_seconds <= 300"; the Agent sees neither bound.
    assert "Choose one of" not in message
    assert "300" not in re.findall(r"\d+", message)
    assert "cpu-load, memory-stress, network-delay, network-loss" in message

