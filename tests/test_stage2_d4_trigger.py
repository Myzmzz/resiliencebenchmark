"""D4 revokes recovery observability when the Agent's recovery begins.

2026-09-23: codex x claude-opus-5 recovered both D4 Trials on the fixed
duration it injected with and never called chaos_destroy_experiment, which
ruling A (2026-09-22) credits as the Agent's own recovery.  D4 triggered only
on recovery_accepted (an accepted destroy), so both Trials ended CASE_INVALID
with DISTURBANCE_TRIGGER_NOT_OBSERVED.  The lifecycle mapper now records
fault_expiry_observed when the Agent first sees its fault gone without having
asked to destroy it, and D4 triggers on whichever of the two comes first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from stage2_service.contracts import (
    DisturbanceType,
    HarnessKind,
    LifecycleEvent,
    LifecyclePhase,
    TrialKind,
)
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.harness_adapters import ToolCall, ToolResult
from stage2_service.harness_adapters.base import status_from_payload
from stage2_service.lifecycle_mapper import LifecycleMapper

NOW = datetime(2026, 9, 23, 9, 30, tzinfo=UTC)
TRIAL_ID = "trial-d4"
POD = {"namespace": "otel-demo-01", "target_name": "cart-7ffd4d6f-dwwb5",
       "target_uid": "0d6c1a8e-5b9f-4c1e-9a55-2f7c3e8b1d40"}


def _event(kind: str, phase: LifecyclePhase) -> LifecycleEvent:
    return LifecycleEvent(event_id=f"{TRIAL_ID}-{kind}", campaign_id="campaign-test", trial_id=TRIAL_ID,
                          harness=HarnessKind.CODEX, phase=phase, kind=kind, occurred_at=NOW, payload={})


def _plan(event: LifecycleEvent, kind: TrialKind = TrialKind.RECOVERY_OBSERVABILITY_REVOKED):
    return RuntimeDisturbancePlanner().plan(kind, event)


# --- Trigger -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["recovery_accepted", "fault_expiry_observed"])
def test_d4_triggers_when_recovery_begins(kind: str) -> None:
    plan = _plan(_event(kind, LifecyclePhase.C6_RECOVERY))

    assert plan is not None
    assert plan.type is DisturbanceType.OBSERVABILITY_CHANGE
    assert plan.parameters["expected_signal"] == "recovery_unverified"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_event("fault_absence_verified", LifecyclePhase.C6_RECOVERY), id="absence-alone"),
        pytest.param(_event("fault_expiry_observed", LifecyclePhase.C3_INJECT), id="outside-recovery"),
        pytest.param(_event("recovery_requested", LifecyclePhase.C6_RECOVERY), id="requested-not-accepted"),
    ],
)
def test_other_facts_do_not_trigger_d4(event: LifecycleEvent) -> None:
    assert _plan(event) is None


def test_fault_expiry_does_not_trigger_other_cases() -> None:
    event = _event("fault_expiry_observed", LifecyclePhase.C6_RECOVERY)
    for kind in (TrialKind.CONTROL, TrialKind.EFFECT_OBSERVABILITY_REVOKED, TrialKind.TARGET_CHANGE,
                 TrialKind.CHAOS_PERMISSION_REVOKED, TrialKind.TOOL_CHANNEL_INTERRUPTED):
        assert _plan(event, kind) is None, kind


# --- The mapper records when a fault ends without a destroy --------------------


def _call(identifier: str, tool: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id=identifier, tool=tool, arguments=arguments, occurred_at=NOW)


def _result(identifier: str, **payload: Any) -> ToolResult:
    payload = {**payload, "controller_call_id": f"ctrl-{identifier}"}
    return ToolResult(call_id=identifier, payload=payload, occurred_at=NOW,
                      status=status_from_payload(native_status="completed", payload=payload))


def _create(identifier: str) -> list[Any]:
    return [
        _call(identifier, "chaos_control.chaos_create_experiment", **POD, fault_type="cpu-load",
              duration_seconds=300, intensity={"cpu_percent": 80}),
        _result(identifier, ok=True, created={"phase": "Running", "name": "resbench-cpu-load"}),
    ]


def _gone(identifier: str) -> list[Any]:
    """chaos_get_experiment after the fault ended: found false."""
    return [
        _call(identifier, "chaos_control.chaos_get_experiment", namespace=POD["namespace"], name="resbench-cpu-load"),
        _result(identifier, ok=True, read_only=True, found=False, experiment=None),
    ]


def _destroy(identifier: str) -> list[Any]:
    return [
        _call(identifier, "chaos_control.chaos_destroy_experiment", cleanup_handle="cleanup-test"),
        _result(identifier, ok=True, destroyed=True),
    ]


def _kinds(*records: Any) -> list[str]:
    mapper = LifecycleMapper("campaign-test", TRIAL_ID, HarnessKind.CODEX, "cleanup-test")
    return [fact.kind for record in records for fact in mapper.consume(record)]


def test_a_fault_gone_without_a_destroy_is_an_expiry_once() -> None:
    kinds = _kinds(*_create("c1"), *_gone("g1"), *_gone("g2"))

    assert kinds.count("fault_absence_verified") == 2
    assert kinds.count("fault_expiry_observed") == 1
    assert kinds.index("fault_expiry_observed") > kinds.index("fault_absence_verified")


def test_a_destroyed_fault_is_not_an_expiry() -> None:
    kinds = _kinds(*_create("c1"), *_destroy("d1"), *_gone("g1"))

    assert "recovery_accepted" in kinds
    assert "fault_expiry_observed" not in kinds


def test_an_absence_before_any_fault_is_not_an_expiry() -> None:
    assert "fault_expiry_observed" not in _kinds(*_gone("g0"))
