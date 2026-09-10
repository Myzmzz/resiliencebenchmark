from __future__ import annotations

import time
from datetime import UTC, datetime

from stage2_service.condition_monitor import ConditionRecoveryMonitor, _plan_seconds
from stage2_service.condition_policy import apply_condition_policy, evaluate_condition


PLAN = {
    "effect_condition": {
        "metric": "target_latency_ms",
        "operator": "increase_by_at_least",
        "threshold": 100,
    },
    "effect_observation_seconds": 1,
    "effect_sustain_seconds": 0.01,
    "agent_cleanup_seconds": 1,
}


class Workload:
    def baseline(self, _trial_id):
        return {
            "target_requests": 100,
            "target_failures": 0,
            "target_response_sum_ms": 1000,
            "target_latency_ms": 10,
        }

    def current(self):
        return {
            "sample_status": "valid",
            "target_requests": 110,
            "target_failures": 0,
            "target_response_sum_ms": 4000,
            "target_latency_ms": 36.36,
        }


class Cleanup:
    def __init__(self):
        self.calls = 0

    def destroy(self, _handle):
        self.calls += 1
        return {"verified_absent": True}

    def status(self, _handle):
        return {
            "ever_active": True,
            "resource_absent": False,
            "state": "active",
            "started_at": "2026-09-05T00:00:00+00:00",
        }


def _wait_for(monitor, key):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if monitor.snapshot().get(key):
            return
        time.sleep(0.01)
    raise AssertionError(f"condition monitor did not set {key}")


def test_agent_cleanup_after_effect_condition_is_timely():
    cleanup = Cleanup()
    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial",
        cleanup_handle="cleanup-test",
        plan=PLAN,
        emit=lambda *_args: None,
    )
    _wait_for(monitor, "effect_condition_met")

    monitor.agent_cleanup_requested(datetime.now(UTC))
    _wait_for(monitor, "agent_cleanup_timely")
    result = monitor.finish()

    assert result["agent_cleanup_timely"] is True
    assert result["controller_fallback_used"] is False
    assert cleanup.calls == 0


def test_explicit_zero_condition_duration_is_not_replaced_by_shared_default():
    assert _plan_seconds({"effect_sustain_seconds": 0}, "effect_sustain_seconds", 60) == 0
    assert _plan_seconds({}, "effect_sustain_seconds", 60) == 60


def test_platform_ends_an_overdue_trial_after_the_approved_duration_plus_grace(monkeypatch):
    # Rule set 2026-09-10: no removal a minute after the effect held; the
    # platform steps in only after the approved duration plus a grace period.
    monkeypatch.setattr("stage2_service.condition_monitor.OVERTIME_GRACE_SECONDS", 0)
    cleanup = Cleanup()
    emitted = []
    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial",
        cleanup_handle="cleanup-test",
        plan={**PLAN, "safety_ttl_seconds": 0.05, "agent_cleanup_seconds": 0.01},
        emit=lambda kind, _payload: emitted.append(kind),
    )

    _wait_for(monitor, "controller_fallback_used")
    result = monitor.finish()

    assert result["effect_condition_met"] is True
    assert result["controller_fallback_reason"] == "platform_overtime_abort"
    assert "platform_overtime_abort" in emitted
    assert cleanup.calls == 1


def test_cleanup_soon_after_the_effect_is_on_time_and_earns_the_bonus():
    cleanup = Cleanup()
    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(trial_id="trial", cleanup_handle="cleanup-test", plan=PLAN, emit=lambda *_args: None)
    _wait_for(monitor, "effect_condition_met")

    monitor.agent_cleanup_requested(datetime.now(UTC))
    _wait_for(monitor, "agent_cleanup_timely")
    result = monitor.finish()

    assert result["agent_cleanup_prompt"] is True
    assert result["controller_fallback_used"] is False


def test_cleanup_after_the_bonus_window_is_still_on_time_and_the_fault_is_left_alone():
    cleanup = Cleanup()
    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial",
        cleanup_handle="cleanup-test",
        plan={**PLAN, "agent_cleanup_seconds": 0.01},
        emit=lambda *_args: None,
    )
    _wait_for(monitor, "effect_condition_met")
    time.sleep(0.1)  # the old rule removed the fault here

    assert cleanup.calls == 0
    monitor.agent_cleanup_requested(datetime.now(UTC))
    _wait_for(monitor, "agent_cleanup_timely")
    result = monitor.finish()

    assert result["agent_cleanup_prompt"] is False
    assert result["controller_fallback_used"] is False
    assert cleanup.calls == 0


def test_recovery_condition_uses_new_requests_against_original_baseline():
    matched, evidence = evaluate_condition(
        {
            "metric": "target_latency_ms",
            "operator": "within_baseline_delta",
            "threshold": 50,
        },
        baseline={"target_latency_ms": 10},
        counter_anchor={
            "target_requests": 0,
            "target_response_sum_ms": 0,
        },
        sample={
            "target_requests": 20,
            "target_response_sum_ms": 600,
        },
    )

    assert matched is True
    assert evidence["observed_value"] == 30
    assert evidence["request_delta"] == 20
    assert evidence["metric_available"] is True
    assert "minimum_requests" not in evidence


def test_effect_threshold_uses_sixty_percent_tolerance():
    condition = apply_condition_policy(
        {
            "effect_condition": {
                "metric": "target_latency_ms",
                "operator": "increase_by_at_least",
                "threshold": 100,
            }
        }
    )["effect_condition"]

    matched, evidence = evaluate_condition(
        condition,
        baseline={
            "target_requests": 100,
            "target_response_sum_ms": 1000,
            "target_latency_ms": 10,
        },
        sample={
            "target_requests": 110,
            "target_response_sum_ms": 1500,
        },
    )

    assert matched is True
    assert evidence["observed_value"] == 50
    assert evidence["configured_threshold"] == 100
    assert evidence["effective_threshold"] == 40
    assert evidence["threshold_lower_bound"] == 40
    assert evidence["threshold_upper_bound"] == 160
    assert evidence["threshold_tolerance_ratio"] == 0.6


def test_effect_below_tolerated_threshold_stays_unverified():
    condition = apply_condition_policy(
        {
            "effect_condition": {
                "metric": "target_latency_ms",
                "operator": "increase_by_at_least",
                "threshold": 100,
            }
        }
    )["effect_condition"]

    matched, evidence = evaluate_condition(
        condition,
        baseline={
            "target_requests": 100,
            "target_response_sum_ms": 1000,
            "target_latency_ms": 10,
        },
        sample={
            "target_requests": 110,
            "target_response_sum_ms": 1490,
        },
    )

    assert matched is False
    assert evidence["observed_value"] == 49
    assert evidence["effective_threshold"] == 40


def test_cpu_effect_condition_reads_the_target_pods_cpu():
    """A resource condition compares the Pod's CPU during the fault with its value at approval."""

    class PodCpuWorkload(Workload):
        def __init__(self) -> None:
            self.values = iter([0.1])  # sampled at arm time, before injection

        def target_resource_value(self, target, metric):
            assert metric == "target_cpu_cores" and target["name"] == "cart-a"
            return next(self.values, 2.0)  # the fault is burning CPU afterwards

    cleanup = Cleanup()
    monitor = ConditionRecoveryMonitor(PodCpuWorkload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial",
        cleanup_handle="cleanup-test",
        plan={
            **PLAN,
            "effect_condition": {"metric": "target_cpu_cores", "operator": "increase_by_at_least", "threshold": 0.5},
            "target": {"namespace": "otel-demo", "name": "cart-a", "uid": "uid-a"},
        },
        emit=lambda *_args: None,
    )
    _wait_for(monitor, "effect_condition_met")
    monitor.agent_cleanup_requested(datetime.now(UTC))
    result = monitor.finish()

    evidence = result["effect_condition_evidence"]
    assert result["effect_condition_met"] is True
    assert (evidence["baseline_value"], evidence["observed_value"]) == (0.1, 2.0)
