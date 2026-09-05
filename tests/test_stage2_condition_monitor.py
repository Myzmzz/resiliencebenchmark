from __future__ import annotations

import time
from datetime import UTC, datetime

from stage2_service.condition_monitor import ConditionRecoveryMonitor
from stage2_service.condition_policy import evaluate_condition


PLAN = {
    "effect_condition": {
        "metric": "target_latency_ms",
        "operator": "increase_by_at_least",
        "threshold": 100,
        "minimum_requests": 10,
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


def test_controller_fallback_runs_when_agent_cleanup_budget_expires():
    cleanup = Cleanup()
    monitor = ConditionRecoveryMonitor(Workload(), cleanup, poll_seconds=0.01)
    monitor.arm(
        trial_id="trial",
        cleanup_handle="cleanup-test",
        plan={**PLAN, "agent_cleanup_seconds": 0.01},
        emit=lambda *_args: None,
    )

    _wait_for(monitor, "controller_fallback_used")
    result = monitor.finish()

    assert result["effect_condition_met"] is True
    assert result["controller_fallback_reason"] == "agent_cleanup_deadline_exceeded"
    assert cleanup.calls == 1


def test_recovery_condition_uses_new_requests_against_original_baseline():
    matched, evidence = evaluate_condition(
        {
            "metric": "target_latency_ms",
            "operator": "within_baseline_delta",
            "threshold": 50,
            "minimum_requests": 10,
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
