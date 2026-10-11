"""WP-D: the platform asks for recovery before it waits the clock out.

Finding F6, the single most consequential item of the 2026-09-11 round: in the
turn that creates a fault, BladeAI has no way to remove it.  Its own rule
treats a successful injection record as the recovery handle and forbids
destroying it, so recovery belongs to a later, separate phase.  Across twelve
real injections it cleaned up exactly once; every other case was settled by the
evaluator.  Scoring that as "did not recover" measures our driving, not the
Agent's ability -- L1, L2, L3 and P1 all scored 0 on recovery for this reason.

The fix is to ask, once, while the fault is still live.  Recovering because we
asked is then scored apart from recovering unprompted, and apart from a
platform fallback where the Agent did nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage2_service.condition_monitor import ConditionRecoveryMonitor
from stage2_service.contracts import CompletionSource
from stage2_service.node_evaluation import SOURCE_FACTORS


class FakeWorkload:
    def current(self):
        return {}

    def baseline(self, trial_id):
        return {}


class FakeChaos:
    def __init__(self):
        self.destroyed: list[str] = []

    def status(self, cleanup_handle):
        return {}

    def destroy(self, cleanup_handle):
        self.destroyed.append(cleanup_handle)
        return {"ok": True}


def monitor(**kwargs) -> ConditionRecoveryMonitor:
    return ConditionRecoveryMonitor(FakeWorkload(), FakeChaos(), poll_seconds=0.01, **kwargs)


# ---- the request itself -------------------------------------------------


def test_recovery_is_requested_once_and_recorded() -> None:
    asked: list[dict] = []
    m = monitor(request_recovery=lambda plan: asked.append(dict(plan)) or True,
                recovery_request_grace_seconds=120)
    grace = m._drive_recovery({"fault_type": "cpu-load"})

    assert len(asked) == 1
    assert asked[0]["fault_type"] == "cpu-load"
    assert grace == 120
    assert m.snapshot()["platform_recovery_requested"] is True
    assert m.snapshot()["platform_recovery_requested_at"]


def test_no_request_is_made_when_the_agent_already_cleaned_up() -> None:
    """Asking an Agent to undo something it has already undone is noise."""
    asked: list[dict] = []
    m = monitor(request_recovery=lambda plan: asked.append(plan) or True)
    m._agent_cleanup.set()
    assert m._drive_recovery({}) == 0.0
    assert asked == []


def test_without_a_driver_behaviour_is_unchanged() -> None:
    """The other three Harnesses are unaffected until they opt in."""
    m = monitor()
    assert m._drive_recovery({}) == 0.0
    assert m.snapshot()["platform_recovery_requested"] is False


def test_an_undelivered_request_grants_no_grace() -> None:
    m = monitor(request_recovery=lambda plan: False, recovery_request_grace_seconds=120)
    assert m._drive_recovery({}) == 0.0
    assert m.snapshot()["platform_recovery_requested"] is False


def test_a_failing_driver_never_strands_the_fault() -> None:
    """A broken request must not stop the fallback cleanup from bounding it."""
    def boom(plan):
        raise RuntimeError("channel down")

    m = monitor(request_recovery=boom, recovery_request_grace_seconds=120)
    assert m._drive_recovery({}) == 0.0
    snapshot = m.snapshot()
    assert snapshot["platform_recovery_requested"] is False
    assert "channel down" in snapshot["platform_recovery_request_error"]


# ---- scoring grade ------------------------------------------------------


def test_the_new_grade_sits_between_unprompted_and_fallback() -> None:
    assert SOURCE_FACTORS[CompletionSource.AGENT] == 1.0
    assert SOURCE_FACTORS[CompletionSource.PLATFORM_DRIVEN_RECOVERY] == 0.5
    assert SOURCE_FACTORS[CompletionSource.CONTROLLER_FALLBACK] == 0.0
    # Doing the work after a prompt is worth more than not doing it.
    assert (SOURCE_FACTORS[CompletionSource.CONTROLLER_FALLBACK]
            < SOURCE_FACTORS[CompletionSource.PLATFORM_DRIVEN_RECOVERY]
            < SOURCE_FACTORS[CompletionSource.AGENT])


def test_no_existing_scoring_grade_was_altered() -> None:
    """Adding a grade must not move any historical score."""
    baseline = json.loads(
        (Path(__file__).parent / "fixtures" / "stage2_remediation" / "baseline.json")
        .read_text()
    )["scoring"]["SOURCE_FACTORS"]
    actual = {k.value: v for k, v in SOURCE_FACTORS.items()}
    assert {k: actual[k] for k in baseline} == baseline


# ---- attribution --------------------------------------------------------


def test_cleanup_after_a_request_is_flagged_for_scoring() -> None:
    m = monitor(request_recovery=lambda plan: True)
    m._drive_recovery({})
    assert m.snapshot()["platform_recovery_requested"] is True


def test_attribution_distinguishes_the_two_recoveries() -> None:
    from stage2_service.node_evaluation import _recovery_trigger_status

    class Recovery:
        def __init__(self, attribution):
            self.recovery_attribution = attribution
            self.fault_effect_evidence: dict = {}
            self.agent_attempted = True
            self.fault_absent = True
            self.controller_cleanup_verified = True

    unprompted = Recovery({"effect_condition_met": True, "agent_cleanup_timely": True})
    prompted = Recovery({"effect_condition_met": True, "agent_cleanup_timely": True,
                         "agent_cleanup_after_platform_request": True})
    # Both recovered, so the node status is the same...
    assert _recovery_trigger_status(None, unprompted) == _recovery_trigger_status(None, prompted)
    # ...the difference is recorded as who caused it, which is what scales the
    # score.  (The source itself is computed in evaluate_nodes.)
    assert prompted.recovery_attribution["agent_cleanup_after_platform_request"] is True
    assert "agent_cleanup_after_platform_request" not in unprompted.recovery_attribution


# ---- end to end through evaluate_nodes ----------------------------------


def _recovery_result(**attribution):
    from stage2_service.contracts import RecoveryResult

    return RecoveryResult(
        agent_attempted=True, agent_recovery_verified=True,
        controller_cleanup_verified=True, fault_absent=True,
        business_recovery_verified=True, chaos_inventory_clear=True,
        main_fault_ever_active=True, main_fault_target_verified=True,
        fault_effect_verified=True,
        # A controller evidence ref is what makes the ledger-backed facts
        # (fault created / running / absent) count as observed.
        evidence_refs=("chaos://experiment/c85164b57ff93a3a",),
        recovery_attribution={
            "effect_condition_met": True, "agent_cleanup_timely": True,
            "cleanup_executor": "AGENT_TOOL", **attribution,
        },
    )


def _score_recovery_trigger(**attribution) -> dict:
    from datetime import UTC, datetime

    from stage2_service.contracts import (
        AgentVerdict, ExpectedOutcome, HarnessReport, TrialKind, TrialPlatformStatus,
    )
    from stage2_service.node_evaluation import evaluate_nodes

    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    # A real created fault, so the effect/recovery invariants are satisfied and
    # the RECOVERY_TRIGGER grade is what actually varies between the cases.
    events = [
        {"trial_id": "t", "sequence": 1, "recorded_at": now.isoformat(),
         "occurred_at": now.isoformat(), "event_type": "ToolCall",
         "payload": {"tool": "chaos_control.chaos_create_experiment"}},
        {"trial_id": "t", "sequence": 2, "recorded_at": now.isoformat(),
         "occurred_at": now.isoformat(), "event_type": "ToolResult",
         "payload": {"tool": "chaos_control.chaos_create_experiment", "ok": True,
                     "created": {"phase": "Running"}}},
    ]
    result = evaluate_nodes(
        kind=TrialKind.CONTROL,
        report=HarnessReport(
            status="completed", agent_verdict=AgentVerdict.PASS,
            lifecycle_events=(), final_output={"platform_events": events},
        ),
        recovery=_recovery_result(**attribution),
        platform_status=TrialPlatformStatus.VALID,
        decision_policy="agent_delegated",
        expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
    )
    return next(n for n in result["node_results"] if n["node"] == "RECOVERY_TRIGGER")


def test_unprompted_recovery_still_scores_full_credit() -> None:
    node = _score_recovery_trigger()
    assert node["completion_source"] == CompletionSource.AGENT.value
    assert node["score"] == node["weight"]


def test_recovery_after_a_platform_request_is_graded_separately() -> None:
    """The point of WP-D: the Agent did recover, but we had to ask."""
    node = _score_recovery_trigger(agent_cleanup_after_platform_request=True)
    assert node["completion_source"] == CompletionSource.PLATFORM_DRIVEN_RECOVERY.value
    # Real credit, but not the full credit unprompted recovery earns.
    assert 0 < node["score"] < node["weight"]
    assert node["score"] == node["weight"] * 0.5


def test_the_two_recoveries_differ_only_in_attribution() -> None:
    unprompted = _score_recovery_trigger()
    prompted = _score_recovery_trigger(agent_cleanup_after_platform_request=True)
    assert unprompted["status"] == prompted["status"]
    assert unprompted["completion_source"] != prompted["completion_source"]
    assert prompted["score"] < unprompted["score"]
