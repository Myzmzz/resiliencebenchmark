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
            "target_name": "cart",
            "namespace": "otel-demo",
            "fault_type": "network-delay",
        }

    def destroy(self, _handle):
        return {"verified_absent": True}

    def inventory(self, _namespace):
        return {"global_chaosblade_count": 0, "active_owned_count": 0}


class Traffic:
    def __init__(self):
        self.recovery_kwargs = {}

    def current(self):
        return {
            "application_owned": True,
            "load_generator_ready": True,
            "traffic_observed": True,
            "business_healthy": True,
        }

    def baseline(self, _trial_id):
        return {
            "target_latency_ms": 10.0,
            "target_requests": 10,
            "target_failures": 0,
            "target_response_sum_ms": 100.0,
        }

    def effect_since(self, _trial_id, _runtime, _approved_plan):
        return {"verified": True, "latency_delta_ms": 1200}

    def reset_and_wait_healthy(self, **_kwargs):
        self.recovery_kwargs = dict(_kwargs)
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
    from stage2_service.evaluator import Stage2Evaluator
    from stage2_service.contracts import TrialKind
    traffic = Traffic()
    result = Stage2Finalizer(Chaos(absent_before=False), traffic).finalize(
        "trial", object(), context(), report()
    )

    assert result.controller_cleanup_verified is True
    assert result.agent_attempted is True
    assert result.agent_recovery_verified is False
    assert result.recovery_attribution["cleanup_executor"] == "CONTROLLER_FALLBACK"
    decision = Stage2Evaluator().decision(kind=TrialKind.CONTROL, report=report(), disturbances=(), recovery=result, diagnostic_only=True)
    cleared = next(node for node in decision["node_results"] if node["node"] == "FAULT_CLEARED")
    assert cleared["completion_source"] == "CONTROLLER_FALLBACK"
    assert cleared["score"] == 0
    assert "minimum_requests" not in traffic.recovery_kwargs
    assert traffic.recovery_kwargs["stability_samples"] == 7


def test_agent_recovery_requires_agent_observation_not_only_oracle_health():
    result = Stage2Finalizer(Chaos(absent_before=True), Traffic()).finalize(
        "trial", object(), context(), report()
    )

    assert result.agent_recovery_verified is False
    assert result.main_fault_ever_active is True
    assert result.main_fault_target_verified is True
    assert result.fault_effect_verified is True


def test_cleanup_verification_is_independent_from_business_recovery():
    class UnrecoveredTraffic(Traffic):
        def reset_and_wait_healthy(self, **_kwargs):
            return {
                "application_owned": True,
                "load_generator_ready": True,
                "traffic_observed": True,
                "business_healthy": False,
            }

    result = Stage2Finalizer(
        Chaos(absent_before=True), UnrecoveredTraffic()
    ).finalize("trial", object(), context(), report())

    assert result.fault_absent is True
    assert result.controller_cleanup_verified is True
    assert result.business_recovery_verified is False


def test_condition_met_agent_cleanup_is_attributed_to_agent_not_timer():
    class AgentCleanupChaos(Chaos):
        def status(self, _handle):
            return {
                "resource_absent": True,
                "ever_active": True,
                "target_uid": "uid-current",
                "target_name": "cart",
                "namespace": "otel-demo",
                "fault_type": "network-delay",
                "ledger_state": "destroyed",
            }

    accepted = LifecycleEvent(
        event_id="accepted",
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t1",
        harness=HarnessKind.CODEX,
        phase=LifecyclePhase.C6_RECOVERY,
        kind="recovery_accepted",
    )
    harness_report = report().model_copy(
        update={
            "lifecycle_events": (*report().lifecycle_events, accepted),
            "final_output": {
                "condition_monitor": {
                    "armed": True,
                    "effect_condition_met": True,
                    "agent_cleanup_requested": True,
                    "agent_cleanup_timely": True,
                }
            },
        }
    )

    result = Stage2Finalizer(AgentCleanupChaos(), Traffic()).finalize(
        "trial", object(), context(), harness_report
    )

    assert result.recovery_attribution["cleanup_executor"] == "AGENT_TOOL"
    assert result.recovery_attribution["effect_condition_met"] is True
    assert result.recovery_attribution["agent_cleanup_timely"] is True


def test_failed_feedback_and_agent_self_report_do_not_create_assistance_credit():
    failed_feedback = LifecycleEvent(
        event_id="failed-feedback",
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t1",
        harness=HarnessKind.CODEX,
        phase=LifecyclePhase.C5_SAFETY,
        kind="harness_feedback_failed",
        payload={"category": "SEMANTIC_NUDGE"},
    )
    harness_report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=(failed_feedback,),
        final_output={
            "interaction_mode": "guided",
            "agent_result": {
                "interaction_mode": "autonomous",
                "assisted": True,
            },
        },
    )

    assistance = Stage2Finalizer._assistance_summary(harness_report)

    assert assistance["interaction_mode"] == "guided"
    assert assistance["reported_assisted"] is True
    assert assistance["semantic_nudge_used"] is False
    assert assistance["assisted"] is False


def test_bounded_timeout_is_observed_before_controller_cleanup():
    class TimeoutChaos(Chaos):
        def __init__(self):
            super().__init__(absent_before=False)
            self.status_calls = 0

        def status(self, _handle):
            self.status_calls += 1
            return {
                "resource_absent": self.status_calls >= 2,
                "ever_active": True,
                "target_uid": "uid-current",
                "fault_type": "network-delay",
            }

    no_explicit_recovery = report().model_copy(update={"lifecycle_events": ()})
    runtime = context().model_copy(
        update={
            "main_fault": {
                "fault_type": "network-delay",
                "duration_seconds": 1,
            }
        }
    )
    chaos = TimeoutChaos()

    result = Stage2Finalizer(
        chaos,
        Traffic(),
        poll_seconds=1,
        sleep=lambda _seconds: None,
    ).finalize("trial", object(), runtime, no_explicit_recovery)

    assert chaos.status_calls >= 2
    assert result.agent_attempted is False
    assert result.fault_absent is True
    assert result.fault_effect_evidence["timeout_recovery_observed"] is True


def test_unique_external_bladeai_fault_is_reconciled_by_target_and_type():
    class ExternalChaos(Chaos):
        def __init__(self):
            super().__init__()
            self.external_calls = 0

        def status(self, _handle):
            return {"error_type": "missing-ledger"}

        def external_status(self, runtime):
            self.external_calls += 1
            if self.external_calls >= 2:
                return {"resource_absent": True, "ever_active": False}
            return {
                "resource_absent": False,
                "ever_active": True,
                "external": True,
                "target_uid": runtime.target.uid,
                "target_name": runtime.target.name,
                "namespace": runtime.target.namespace,
                "fault_type": runtime.main_fault["fault_type"],
            }

        def cleanup_external(self, _runtime):
            return {"verified_absent": True, "external_experiment": "bladeai-cr"}

    runtime = context().model_copy(
        update={
            "main_fault": {
                "fault_type": "network-delay",
                "duration_seconds": 1,
            }
        }
    )
    no_explicit_recovery = report().model_copy(update={"lifecycle_events": ()})

    result = Stage2Finalizer(
        ExternalChaos(),
        Traffic(),
        poll_seconds=1,
        sleep=lambda _seconds: None,
    ).finalize("trial", object(), runtime, no_explicit_recovery)

    assert result.main_fault_ever_active is True
    assert result.main_fault_target_verified is True
    assert result.fault_absent is True
    assert result.fault_effect_evidence["external_chaos_reconciled"] is True
