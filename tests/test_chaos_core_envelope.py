"""Unit tests for the Oracle-facing envelope judgement in chaos_core.gates.

``execution_parameters_within_envelope`` exists so an independent Oracle can ask
the same question the create gate asked, after the fact and without raising.  The
point of these tests is that it agrees with the gate on what is inside, and fails
closed -- never "inside" -- on anything it cannot actually read.
"""

from __future__ import annotations

import pytest

from controller.safety import default_policy
from mcp_servers.chaos_core.gates import execution_parameters_within_envelope
from stage2_service.plan_schema import PlanSafetyEnvelope


def _envelope(*, namespaces: tuple[str, ...] = ("otel-demo",)) -> PlanSafetyEnvelope:
    return PlanSafetyEnvelope.from_controller_policy(default_policy(set(namespaces)))


def _within(**overrides) -> bool:
    parameters = {
        "namespace": "otel-demo",
        "target_name": "cart-1",
        "target_uid": "uid-1",
        "fault_type": "network-delay",
        "duration_seconds": 60,
        "intensity": {"delay_ms": 300},
    }
    parameters.update(overrides)
    return execution_parameters_within_envelope(envelope=_envelope(), **parameters)


def test_parameters_the_create_gate_accepts_are_within_the_envelope():
    assert _within() is True


@pytest.mark.parametrize(
    "overrides",
    [
        # Over the controller's max_fault_duration_seconds.
        {"duration_seconds": 5000},
        # Under the intensity minimum.
        {"intensity": {"delay_ms": -5}},
        # Over the published maximum for a percent field.
        {"fault_type": "cpu-load", "intensity": {"cpu_percent": 200}},
        # A fault type the policy has no contract for.
        {"fault_type": "no-such-fault", "intensity": {"delay_ms": 300}},
        # Outside the envelope's namespace allowlist.
        {"namespace": "someone-elses-namespace"},
    ],
)
def test_parameters_outside_the_envelope_are_rejected(overrides):
    assert _within(**overrides) is False


@pytest.mark.parametrize(
    "overrides",
    [
        # Fail closed: anything unreadable is "not within", never "within".
        {"namespace": ""},
        {"target_name": ""},
        {"target_uid": ""},
        {"fault_type": ""},
        {"duration_seconds": None},
        {"duration_seconds": "60"},
        {"duration_seconds": 0},
        {"duration_seconds": True},
        {"intensity": {}},
        {"intensity": None},
        {"intensity": "delay_ms=300"},
    ],
)
def test_unreadable_parameters_fail_closed(overrides):
    assert _within(**overrides) is False


def test_envelope_namespace_allowlist_comes_from_the_caller_not_the_parameters():
    # The caller supplies the envelope, so a parameter set that names its own
    # namespace cannot widen the allowlist it is judged against.
    assert execution_parameters_within_envelope(
        namespace="otel-demo-02", target_name="cart-1", target_uid="uid-1",
        fault_type="network-delay", duration_seconds=60, intensity={"delay_ms": 300},
        envelope=_envelope(namespaces=("otel-demo-01",)),
    ) is False
    assert execution_parameters_within_envelope(
        namespace="otel-demo-01", target_name="cart-1", target_uid="uid-1",
        fault_type="network-delay", duration_seconds=60, intensity={"delay_ms": 300},
        envelope=_envelope(namespaces=("otel-demo-01",)),
    ) is True
