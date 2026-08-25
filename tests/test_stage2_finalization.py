from __future__ import annotations

from stage2_service.contracts import (
    AgentVerdict,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    LifecyclePhase,
    RuntimeTarget,
    TrialRuntimeContext,
)
from stage2_service.finalization import Stage2Finalizer


class Chaos:
    def __init__(self, absent_before=False):
        self.absent_before = absent_before

    def status(self, _handle):
        return {
            "resource_absent": self.absent_before,
            "ever_active": True,
            "target_uid": "uid-current",
            "fault_type": "network-delay",
        }

    def destroy(self, _handle):
        return {"verified_absent": True}

    def inventory(self, _namespace):
        return {"global_chaosblade_count": 0, "active_owned_count": 0}


class Traffic:
    def current(self):
        return {
            "application_owned": True,
            "load_generator_ready": True,
            "traffic_observed": True,
            "business_healthy": True,
        }

    def effect_since(self, _trial_id):
        return {"verified": True, "latency_delta_ms": 1200}

    def reset_and_wait_healthy(self, **_kwargs):
        return self.current()


def context():
    return TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-codex-t1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )


def report():
    return HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=(
            LifecycleEvent(
                event_id="recovery",
                campaign_id="campaign-1234567890abcdef",
                trial_id="campaign-1234567890abcdef-codex-t1",
                harness=HarnessKind.CODEX,
                phase=LifecyclePhase.C6_RECOVERY,
                kind="recovery_requested",
            ),
        ),
    )


def test_controller_cleanup_does_not_credit_agent_when_fault_was_not_absent_before_fallback():
    result = Stage2Finalizer(Chaos(absent_before=False), Traffic()).finalize(
        "trial", object(), context(), report()
    )

    assert result.controller_cleanup_verified is True
    assert result.agent_attempted is True
    assert result.agent_recovery_verified is False


def test_agent_recovery_requires_preexisting_absence_and_business_recovery():
    result = Stage2Finalizer(Chaos(absent_before=True), Traffic()).finalize(
        "trial", object(), context(), report()
    )

    assert result.agent_recovery_verified is True
    assert result.main_fault_ever_active is True
    assert result.main_fault_target_verified is True
    assert result.fault_effect_verified is True
