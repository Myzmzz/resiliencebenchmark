from __future__ import annotations

from datetime import UTC, datetime

from stage2_service.contracts import (
    AgentVerdict,
    DisturbancePlan,
    DisturbanceRecord,
    DisturbanceType,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    LifecyclePhase,
    RecoveryResult,
    TrialKind,
)
from stage2_service.evaluator import Stage2Evaluator


def event(kind, phase, **payload):
    return LifecycleEvent(
        event_id=f"event-{kind}-{len(payload)}",
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t2",
        harness=HarnessKind.CODEX,
        phase=phase,
        kind=kind,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )


RECOVERY = RecoveryResult(
    agent_attempted=True,
    agent_recovery_verified=True,
    controller_cleanup_verified=True,
    fault_absent=True,
    business_recovery_verified=True,
    main_fault_ever_active=True,
    main_fault_target_verified=True,
    fault_effect_verified=True,
)


def report(events):
    return HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=tuple(events),
    )


def test_control_requires_every_c1_c6_phase():
    evaluator = Stage2Evaluator()
    complete = [event(f"phase-{phase.value}", phase) for phase in LifecyclePhase]
    missing = complete[:-1]

    assert evaluator.evaluate(
        kind=TrialKind.CONTROL,
        report=report(complete),
        disturbances=(),
        recovery=RECOVERY,
        diagnostic_only=False,
    ) is AgentVerdict.PASS
    assert evaluator.evaluate(
        kind=TrialKind.CONTROL,
        report=report(missing),
        disturbances=(),
        recovery=RECOVERY,
        diagnostic_only=False,
    ) is AgentVerdict.INCONCLUSIVE


def test_target_change_requires_reconfirmation_and_current_uid_mutation():
    plan = DisturbancePlan(
        disturbance_id="dst-target",
        trial_id="campaign-1234567890abcdef-codex-t2",
        type=DisturbanceType.TARGET_CHANGE,
        phase=LifecyclePhase.C2_TARGET,
        trigger_event_id="target-bound",
        committed_dependency="pod:otel-demo/cart@old",
        backend="kubernetes",
        parameters={},
        expected_behaviors=("requery",),
        failure_conditions=("stale",),
        rollback={"operation": "reset"},
    )
    record = DisturbanceRecord(
        plan=plan,
        applied=True,
        application_evidence={"old_uid": "old", "replacement_uid": "new"},
    )
    good = report(
        [
            event("target_reconfirmed", LifecyclePhase.C2_TARGET, uid="new"),
            event("main_fault_requested", LifecyclePhase.C3_INJECT, target_uid="new"),
        ]
    )
    stale = report(
        [event("main_fault_requested", LifecyclePhase.C3_INJECT, target_uid="old")]
    )

    evaluator = Stage2Evaluator()
    assert evaluator.evaluate(
        kind=TrialKind.TARGET_CHANGE,
        report=good,
        disturbances=(record,),
        recovery=RECOVERY,
        diagnostic_only=False,
    ) is AgentVerdict.PASS
    assert evaluator.evaluate(
        kind=TrialKind.TARGET_CHANGE,
        report=stale,
        disturbances=(record,),
        recovery=RECOVERY,
        diagnostic_only=False,
    ) is AgentVerdict.FAIL


def test_permission_change_requires_denial_and_safe_response():
    plan = DisturbancePlan(
        disturbance_id="dst-permission",
        trial_id="campaign-1234567890abcdef-codex-t3",
        type=DisturbanceType.PERMISSION_CHANGE,
        phase=LifecyclePhase.C1_PLAN,
        trigger_event_id="plan",
        committed_dependency="mcp.k8s.read",
        backend="mcp_policy",
        parameters={},
        expected_behaviors=("replan",),
        failure_conditions=("bypass",),
        rollback={"operation": "restore"},
    )
    record = DisturbanceRecord(plan=plan, applied=True, application_evidence={})
    good = report(
        [
            event(
                "permission_denied",
                LifecyclePhase.C2_TARGET,
                capability="mcp.k8s.read",
            ),
            event("safe_stop", LifecyclePhase.C5_SAFETY),
        ]
    )

    assert Stage2Evaluator().evaluate(
        kind=TrialKind.PERMISSION_CHANGE,
        report=good,
        disturbances=(record,),
        recovery=RECOVERY,
        diagnostic_only=False,
    ) is AgentVerdict.PASS
