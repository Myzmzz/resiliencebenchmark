from __future__ import annotations

import math
from types import SimpleNamespace

from controller.safety import ControllerPolicy, FaultTypeContract, default_policy
from stage2_service.plan_schema import (
    AgentPlan,
    FaultType,
    FaultEnvelope,
    IntensityFieldEnvelope,
    PlanSafetyEnvelope,
    validate_agent_plan,
)


def envelope() -> PlanSafetyEnvelope:
    return PlanSafetyEnvelope.from_controller_policy(
        default_policy({"otel-demo"}),
        allowed_fault_types=("network-delay", "network-loss", "cpu-load", "memory-stress"),
        max_threshold_by_metric={
            "target_latency_ms": 5_000,
            "target_success_rate": 1.0,
            "target_current_rps": 10_000,
        },
    )


def valid_plan(**updates):
    plan = {
        "target": {
            "namespace": "otel-demo",
            "name": "cart-abc123",
            "uid": "11111111-2222-4333-8444-555555555555",
        },
        "fault_type": "latency",
        "intensity": {"delay_ms": 250},
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
        "stop_conditions": ["effect condition sustained"],
        "safety_ttl_seconds": 600,
        "effect_observation_seconds": 300,
        "effect_sustain_seconds": 60,
        "agent_cleanup_seconds": 60,
        "recovery_observation_seconds": 180,
        "recovery_sustain_seconds": 60,
    }
    plan.update(updates)
    return plan


def issue_codes(result):
    return {issue.code for issue in result.issues}


def test_canonicalizes_fault_alias_once_and_exports_controller_fault_contract():
    result = validate_agent_plan(valid_plan(), envelope())

    assert result.ok
    assert result.plan is not None
    assert result.plan.fault_type is FaultType.NETWORK_DELAY
    assert result.plan.model_dump()["fault_type"] == "network-delay"
    assert result.plan.main_fault_spec().fault_type == "network-delay"
    assert result.plan.main_fault_spec().duration_seconds == 600
    assert result.plan.main_fault_spec().intensity == {"delay_ms": 250}
    assert result.plan.chaos_create_arguments() == {
        "namespace": "otel-demo",
        "target_name": "cart-abc123",
        "target_uid": "11111111-2222-4333-8444-555555555555",
        "fault_type": "network-delay",
        "duration_seconds": 600,
        "intensity": {"delay_ms": 250},
    }


def test_fault_aliases_are_centralized():
    assert FaultType.canonical("latency") is FaultType.NETWORK_DELAY
    assert FaultType.canonical("network_delay") is FaultType.NETWORK_DELAY
    assert FaultType.canonical("延迟") is FaultType.NETWORK_DELAY
    assert FaultType.canonical("丢包率") is FaultType.NETWORK_LOSS
    assert FaultType.canonical("cpu load") is FaultType.CPU_LOAD
    assert FaultType.canonical("内存压力") is FaultType.MEMORY_STRESS


def test_rejects_string_intensity_even_when_controller_safety_could_coerce_it():
    bad_values = ("100%", "丢包率 100%", "丢包率100%")

    for bad in bad_values:
        result = validate_agent_plan(
            valid_plan(fault_type="network-loss", intensity={"loss_percent": bad}),
            envelope(),
        )

        assert not result.ok
        assert "INVALID_INTENSITY_VALUE" in issue_codes(result)
        assert result.plan is None


def test_rejects_bool_negative_and_non_finite_numeric_values():
    cases = [
        ({"delay_ms": True}, "INVALID_INTENSITY_VALUE"),
        ({"delay_ms": -1}, "INVALID_INTENSITY_VALUE"),
        ({"delay_ms": math.inf}, "INVALID_INTENSITY_VALUE"),
        ({"delay_ms": math.nan}, "INVALID_INTENSITY_VALUE"),
    ]

    for intensity, code in cases:
        result = validate_agent_plan(valid_plan(intensity=intensity), envelope())

        assert not result.ok
        assert code in issue_codes(result)


def test_rejects_fault_type_and_intensity_mismatch():
    result = validate_agent_plan(
        valid_plan(fault_type="cpu-load", intensity={"delay_ms": 250}),
        envelope(),
    )

    assert not result.ok
    assert {"MISSING_INTENSITY_FIELD", "UNKNOWN_INTENSITY_FIELD"} <= issue_codes(result)
    assert any(issue.path == "intensity.cpu_percent" for issue in result.issues)


def test_rejects_intensity_outside_caller_envelope():
    limited = envelope().model_copy(
        update={
            "fault_contracts": {
                "network-delay": FaultEnvelope(
                    intensity_fields={
                        "delay_ms": IntensityFieldEnvelope(
                            unit="milliseconds",
                            max_value=300,
                        )
                    }
                )
            },
            "allowed_fault_types": ("network-delay",),
        }
    )

    result = validate_agent_plan(valid_plan(intensity={"delay_ms": 500}), limited)

    assert not result.ok
    assert "INTENSITY_EXCEEDS_ENVELOPE" in issue_codes(result)


def test_controller_policy_intensity_bounds_are_preserved_when_present():
    policy = ControllerPolicy(
        namespace_allowlist=frozenset({"otel-demo"}),
        fault_type_contracts={
            "network-delay": FaultTypeContract(
                intensity_fields={
                    "delay_ms": SimpleNamespace(
                        unit="milliseconds",
                        min_value=10,
                        max_value=500,
                    )
                }
            )
        },
    )

    derived = PlanSafetyEnvelope.from_controller_policy(policy)

    field = derived.fault_contracts["network-delay"].intensity_fields["delay_ms"]
    assert field.min_value == 10
    assert field.max_value == 500


def test_rejects_unknown_fault_type_before_controller_execution():
    result = validate_agent_plan(valid_plan(fault_type="disk-fill"), envelope())

    assert not result.ok
    assert "FAULT_TYPE_NOT_ALLOWED" in issue_codes(result)


def test_rejects_target_outside_envelope_and_missing_uid():
    target = {"namespace": "kube-system", "name": "coredns-abc", "uid": ""}
    result = validate_agent_plan(valid_plan(target=target), envelope())

    assert not result.ok
    assert {"NAMESPACE_NOT_ALLOWED", "MISSING_TARGET_UID"} <= issue_codes(result)


def test_rejects_ttl_and_condition_threshold_outside_caller_envelope():
    result = validate_agent_plan(
        valid_plan(
            safety_ttl_seconds=1_201,
            effect_condition={
                "metric": "target_latency_ms",
                "operator": "increase_by_at_least",
                "threshold": 9_000,
            },
        ),
        envelope(),
    )

    assert not result.ok
    assert {"SAFETY_TTL_EXCEEDED", "THRESHOLD_EXCEEDS_ENVELOPE"} <= issue_codes(result)


def test_rejects_condition_operator_and_bool_threshold():
    result = validate_agent_plan(
        valid_plan(
            effect_condition={
                "metric": "target_latency_ms",
                "operator": "within_baseline_delta",
                "threshold": True,
            }
        ),
        envelope(),
    )

    assert not result.ok
    assert {"INVALID_EFFECT_OPERATOR", "INVALID_CONDITION_THRESHOLD"} <= issue_codes(result)


def test_agent_plan_model_validate_is_strict_when_caller_wants_exception_flow():
    plan = AgentPlan.model_validate(valid_plan(fault_type="network_delay"))

    assert plan.fault_type is FaultType.NETWORK_DELAY
    assert plan.intensity == {"delay_ms": 250}
