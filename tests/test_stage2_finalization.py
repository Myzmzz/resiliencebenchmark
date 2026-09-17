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

    def inventory_trial(self, _runtime):
        return {
            "qualified": True,
            "owned_resources_absent": self.absent_before,
            "inventory_clear": self.absent_before,
            "foreign_active_count": 0,
            "trial": {
                "resource_absent": self.absent_before,
                "ever_active": True,
                "target_uid": "uid-current",
                "target_name": "cart",
                "namespace": "otel-demo",
                "fault_type": "network-delay",
            },
        }

    def cleanup_owned(self, _runtime):
        self.absent_before = True
        return {"verified_absent": True, "principal": "CONTROLLER_FALLBACK"}


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


def test_approved_recovery_window_overrides_shared_policy_for_wp8():
    traffic = Traffic()
    harness_report = report().model_copy(
        update={
            "final_output": {
                "approved_plan": {
                    "recovery_observation_seconds": 60,
                    "recovery_sustain_seconds": 0,
                }
            }
        }
    )

    result = Stage2Finalizer(Chaos(absent_before=True), traffic).finalize(
        "trial", object(), context(), harness_report
    )

    assert result.controller_cleanup_verified is True
    assert traffic.recovery_kwargs["timeout_seconds"] == 60
    assert traffic.recovery_kwargs["stability_samples"] == 1


def test_agent_recovery_requires_agent_observation_not_only_oracle_health():
    result = Stage2Finalizer(Chaos(absent_before=True), Traffic()).finalize(
        "trial", object(), context(), report()
    )

    assert result.agent_recovery_verified is False
    assert result.main_fault_ever_active is True
    assert result.main_fault_target_verified is True
    assert result.fault_effect_verified is True
    assert result.recovery_attribution["trial_id"] == context().trial_id
    assert result.recovery_attribution["cleanup_handle"] == context().cleanup_handle
    assert result.recovery_attribution["target_uid"] == context().target.uid


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


def test_incomplete_inventory_never_turns_an_absent_resource_into_verified_cleanup():
    class IncompleteInventoryChaos(Chaos):
        def inventory_trial(self, runtime):
            result = super().inventory_trial(runtime)
            result.update(
                qualified=False,
                inventory_clear=False,
                owned_resources_absent=False,
                unavailable_executors=["chaos_mesh"],
            )
            return result

    result = Stage2Finalizer(
        IncompleteInventoryChaos(absent_before=True), Traffic()
    ).finalize("trial", object(), context(), report())

    assert result.fault_absent is False
    assert result.controller_cleanup_verified is False
    assert result.business_recovery_verified is True


def test_condition_met_agent_cleanup_is_attributed_to_agent_not_timer():
    class AgentCleanupChaos(Chaos):
        def inventory_trial(self, runtime):
            result = super().inventory_trial(runtime)
            result["trial"]["resource_absent"] = True
            result["owned_resources_absent"] = True
            result["inventory_clear"] = True
            result["trial"]["ledger_state"] = "destroyed"
            return result

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

        def inventory_trial(self, runtime):
            self.status_calls += 1
            self.absent_before = self.status_calls >= 2
            return super().inventory_trial(runtime)

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


def test_unified_inventory_reconciles_a_controller_owned_fault():
    class InventoryChaos(Chaos):
        def inventory_trial(self, runtime):
            result = super().inventory_trial(runtime)
            result["resources"] = [{
                "executor_id": "chaosblade",
                "name": "controller-cr",
                "owned_by_trial": True,
            }]
            return result

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
        InventoryChaos(),
        Traffic(),
        poll_seconds=1,
        sleep=lambda _seconds: None,
    ).finalize("trial", object(), runtime, no_explicit_recovery)

    assert result.main_fault_ever_active is True
    assert result.main_fault_target_verified is True
    assert result.fault_absent is True
    assert result.fault_effect_evidence["fault_inventory"]["qualified"] is True


class NeverActiveChaos(Chaos):
    """A Trial whose main fault never ran (every injection was refused)."""

    def inventory_trial(self, runtime):
        value = super().inventory_trial(runtime)
        value["trial"] = {**value["trial"], "ever_active": False, "resource_absent": True}
        value["owned_resources_absent"] = True
        value["inventory_clear"] = True
        return value


def test_no_fault_ever_ran_skips_the_recovery_wait_and_fails_only_on_the_missing_fault():
    from stage2_service.contracts import TrialKind
    from stage2_service.evaluator import Stage2Evaluator

    traffic = Traffic()
    result = Stage2Finalizer(NeverActiveChaos(absent_before=True), traffic).finalize(
        "trial", object(), context(), report()
    )

    assert traffic.recovery_kwargs == {}
    assert result.main_fault_ever_active is False
    assert result.fault_effect_evidence["business_recovery_observation"]["not_applicable"] is True
    decision = Stage2Evaluator().decision(
        kind=TrialKind.CONTROL, report=report(), disturbances=(), recovery=result, diagnostic_only=True
    )
    failed = {check["rule_id"] for check in decision["checks"] if not check["passed"]}
    assert failed == {"MAIN_FAULT_ACTIVE"}
    requirements = decision["experiment_gate"]["requirements"]
    assert requirements["main_fault_running"] is False
    assert "business_recovery_verified" not in requirements and "target_verified" not in requirements


def _bladeai_report(final_output):
    """A black-box Agent run: no recovery_requested, the fault came from its own client."""
    return HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=(),
        final_output=final_output,
    )


def test_an_agent_fault_the_platform_removed_for_overtime_is_a_controller_fallback():
    """Before 2026-09-17 this removal was indistinguishable from any other and scored UNATTRIBUTED."""
    foreign_fault = {
        "observed": True,
        "experiment_name": "blade-own",
        "approved_duration_seconds": 300,
        "grace_seconds": 120,
        "controller_fallback_used": True,
        "controller_fallback_at": "2026-09-17T10:07:00+00:00",
        "controller_fallback_reason": "approved_duration_exceeded",
        "controller_cleanup": {"verified_absent": True, "deleted_foreign_experiment": "blade-own"},
    }

    result = Stage2Finalizer(Chaos(absent_before=True), Traffic()).finalize(
        "trial", object(), context(), _bladeai_report({"foreign_fault": foreign_fault})
    )

    attribution = result.recovery_attribution
    assert attribution["cleanup_executor"] == "CONTROLLER_FALLBACK"
    assert attribution["controller_intervened"] is True
    assert attribution["foreign_overtime_cleanup"]["experiment_name"] == "blade-own"
    assert attribution["foreign_overtime_cleanup"]["approved_duration_seconds"] == 300


def test_an_unverified_overtime_removal_is_not_credited_to_the_platform():
    foreign_fault = {
        "controller_fallback_used": True,
        "controller_cleanup": {"verified_absent": False, "deleted_foreign_experiment": "blade-own"},
    }

    result = Stage2Finalizer(Chaos(absent_before=True), Traffic()).finalize(
        "trial", object(), context(), _bladeai_report({"foreign_fault": foreign_fault})
    )

    assert result.recovery_attribution["cleanup_executor"] == "UNATTRIBUTED"
    assert result.recovery_attribution["foreign_overtime_cleanup"] is None


class _ForeignChaos:
    """An Agent-created experiment first seen ``age_seconds`` ago and still running."""

    def __init__(self, age_seconds):
        from datetime import UTC, datetime, timedelta

        self.started_at = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        self.absent = False
        self.cleanups = 0

    def inventory_trial(self, _runtime):
        return {
            "qualified": True,
            "owned_resources_absent": self.absent,
            "inventory_clear": self.absent,
            "foreign_active_count": 0 if self.absent else 1,
            "trial": {
                "resource_absent": self.absent,
                "ever_active": True,
                "fault_attribution": "observed_foreign",
                "experiment_name": "blade-own",
                "started_at": self.started_at,
                "ended_at": None,
                "target_uid": "uid-current",
                "target_name": "cart",
                "namespace": "otel-demo",
                "fault_type": "network-delay",
            },
        }

    def cleanup_owned(self, _runtime):
        self.cleanups += 1
        self.absent = True
        return {"verified_absent": True, "principal": "CONTROLLER_FALLBACK", "deleted_foreign_experiment": "blade-own"}


def test_finalization_removes_an_agent_fault_the_session_left_running_past_its_approved_duration():
    """Round ten r3: the session ended six seconds before the observer's deadline.

    Finalization used to wait a fixed duration + 10 s from its own start and let
    the experiment run on to BladeAI's 600 s timer.  Now it counts from when the
    experiment was first seen and removes it once the approved duration is over.
    """
    chaos = _ForeignChaos(age_seconds=420)
    slept = []

    result = Stage2Finalizer(chaos, Traffic(), sleep=slept.append).finalize(
        "trial", object(), context(), _bladeai_report({"approved_plan": {"safety_ttl_seconds": 300}})
    )

    # Already past 300 s: only the fixed margin is waited, not 300 + 10 again.
    assert sum(slept) <= 10
    assert chaos.cleanups == 1
    attribution = result.recovery_attribution
    assert attribution["cleanup_executor"] == "CONTROLLER_FALLBACK"
    assert attribution["controller_intervened"] is True
    cleanup = attribution["foreign_overtime_cleanup"]
    assert cleanup["removed_by"] == "finalization"
    assert cleanup["experiment_name"] == "blade-own"
    assert cleanup["reason"] == "approved_duration_exceeded"
    assert cleanup["approved_duration_seconds"] == 300


def test_waiting_for_an_agent_fault_counts_from_when_it_was_first_seen():
    from datetime import UTC, datetime, timedelta

    runtime = context().model_copy(update={"main_fault": {"fault_type": "network-delay", "duration_seconds": 300}})
    seen_200_seconds_ago = {
        "fault_attribution": "observed_foreign",
        "started_at": (datetime.now(UTC) - timedelta(seconds=200)).isoformat(),
    }

    remaining = Stage2Finalizer._remaining_fault_seconds(seen_200_seconds_ago, runtime, {"safety_ttl_seconds": 300})

    assert 105 <= remaining <= 111
    # Without a first-seen time the old fixed wait still applies.
    assert Stage2Finalizer._remaining_fault_seconds({"fault_attribution": "observed_foreign"}, runtime, {}) == 310
