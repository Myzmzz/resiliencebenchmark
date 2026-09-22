"""Recovery credit for a fault that ended on the timer the Agent armed.

Ruling, 2026-09-22: within the approved duration plus the recovery grace
window, a fault the ledger expired at the duration the Agent requested is the
Agent's own recovery.  It earns the same RECOVERY_TRIGGER and FAULT_CLEARED
credit as an explicit destroy.  Condition-driven tasks, where hitting the
timer is late, and the platform's own cleanup still earn nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from stage2_service.contracts import (
    AgentVerdict,
    CompletionSource,
    ExpectedOutcome,
    HarnessReport,
    NodeStatus,
    RecoveryResult,
    TrialKind,
    TrialPlatformStatus,
)
from stage2_service.node_evaluation import _recovery_trigger_status, evaluate_nodes


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

# The fault really ran and the Agent never asked for a destroy; only the
# attribution below decides who ended it.
TIMER_ATTRIBUTION: dict[str, Any] = {
    "effect_condition_met": True,
    "agent_cleanup_timely": False,
    "cleanup_executor": "CONTROLLER_TIMER",
    "planned_automatic_recovery": True,
}


def _recovery_result(agent_attempted: bool, **attribution: Any) -> RecoveryResult:
    """A recovered fault whose attribution is the only thing each test varies."""
    return RecoveryResult(
        agent_attempted=agent_attempted,
        agent_recovery_verified=True,
        controller_cleanup_verified=True,
        fault_absent=True,
        business_recovery_verified=True,
        chaos_inventory_clear=True,
        main_fault_ever_active=True,
        main_fault_target_verified=True,
        fault_effect_verified=True,
        # A controller evidence ref makes the ledger-backed facts count as observed.
        evidence_refs=("chaos://experiment/c85164b57ff93a3a",),
        recovery_attribution=dict(attribution),
    )


def _recovery_nodes(agent_attempted: bool, **attribution: Any) -> dict[str, dict[str, Any]]:
    """Score one C0 execution Trial and return its two recovery nodes by name."""
    events = [
        {"trial_id": "t", "sequence": 1, "recorded_at": NOW.isoformat(),
         "occurred_at": NOW.isoformat(), "event_type": "ToolCall",
         "payload": {"tool": "chaos_control.chaos_create_experiment"}},
        {"trial_id": "t", "sequence": 2, "recorded_at": NOW.isoformat(),
         "occurred_at": NOW.isoformat(), "event_type": "ToolResult",
         "payload": {"tool": "chaos_control.chaos_create_experiment", "ok": True,
                     "created": {"phase": "Running"}}},
    ]
    result = evaluate_nodes(
        kind=TrialKind.CONTROL,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.PASS,
            lifecycle_events=(),
            final_output={"platform_events": events},
        ),
        recovery=_recovery_result(agent_attempted, **attribution),
        platform_status=TrialPlatformStatus.VALID,
        decision_policy="agent_delegated",
        expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
    )
    return {
        node["node"]: node
        for node in result["node_results"]
        if node["node"] in {"RECOVERY_TRIGGER", "FAULT_CLEARED"}
    }


def test_agent_timer_earns_full_recovery_credit() -> None:
    nodes = _recovery_nodes(False, **TIMER_ATTRIBUTION)
    trigger, cleared = nodes["RECOVERY_TRIGGER"], nodes["FAULT_CLEARED"]
    assert trigger["status"] == NodeStatus.VERIFIED.value
    assert trigger["completion_source"] == CompletionSource.AGENT.value
    assert trigger["score"] == trigger["weight"] == 8
    assert cleared["completion_source"] == CompletionSource.AGENT.value
    assert cleared["score"] == cleared["weight"] == 10


def test_agent_timer_scores_the_same_as_an_explicit_destroy() -> None:
    timer = _recovery_nodes(False, **TIMER_ATTRIBUTION)
    destroy = _recovery_nodes(
        True,
        effect_condition_met=True,
        agent_cleanup_timely=True,
        cleanup_executor="AGENT_TOOL",
    )
    for name in ("RECOVERY_TRIGGER", "FAULT_CLEARED"):
        assert timer[name]["score"] == destroy[name]["score"]
        assert timer[name]["completion_source"] == destroy[name]["completion_source"]


def test_timer_on_a_condition_driven_task_earns_nothing() -> None:
    # The Finalizer leaves planned_automatic_recovery False for prompts such
    # as "效果确认后立即恢复": reaching the timer there means recovering late.
    nodes = _recovery_nodes(False, **{**TIMER_ATTRIBUTION, "planned_automatic_recovery": False})
    assert nodes["RECOVERY_TRIGGER"]["score"] == 0
    assert nodes["FAULT_CLEARED"]["completion_source"] == CompletionSource.CONTROLLER_FALLBACK.value
    assert nodes["FAULT_CLEARED"]["score"] == 0


def test_platform_overtime_cleanup_earns_nothing_even_with_a_planned_timer() -> None:
    # Only the ledger timer is the Agent's; the platform's overtime or
    # session-end cleanup is recorded as CONTROLLER_FALLBACK and stays unpaid.
    nodes = _recovery_nodes(
        False, **{**TIMER_ATTRIBUTION, "cleanup_executor": "CONTROLLER_FALLBACK"}
    )
    assert nodes["RECOVERY_TRIGGER"]["score"] == 0
    assert nodes["FAULT_CLEARED"]["completion_source"] == CompletionSource.CONTROLLER_FALLBACK.value
    assert nodes["FAULT_CLEARED"]["score"] == 0


def test_unmonitored_trial_without_a_destroy_request_credits_the_timer() -> None:
    """Without a condition monitor the status falls back to lifecycle events."""
    report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=(),
        final_output={},
    )
    unmonitored = {
        "cleanup_executor": "CONTROLLER_TIMER",
        "planned_automatic_recovery": True,
    }
    assert (
        _recovery_trigger_status(report, _recovery_result(False, **unmonitored))
        is NodeStatus.VERIFIED
    )
    assert (
        _recovery_trigger_status(
            report,
            _recovery_result(False, **{**unmonitored, "planned_automatic_recovery": False}),
        )
        is NodeStatus.NOT_ATTEMPTED
    )
