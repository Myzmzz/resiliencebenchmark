from __future__ import annotations

from evaluator.runtime_oracle import (
    RuntimeLevelEvaluator,
    RuntimeRunOracle,
    _workload_summary_effect,
)
from progression.controller import TrialTicket


def _ticket() -> TrialTicket:
    return TrialTicket(
        trial_id="run-1-L1-a1",
        run_id="run-1",
        episode_id="EPI-1",
        level_id="L1",
        attempt=1,
    )


def _trial_report():
    return {
        "status": "completed",
        "preparation": {"status": "qualified"},
        "finalization": {
            "verified": True,
            "evidence_refs": ["recovery.json"],
        },
        "lifecycleEvents": [
            {
                "kind": "trial_started",
                "occurred_at": "2026-08-23T00:00:00Z",
            },
            {
                "kind": "main_fault_applied",
                "occurred_at": "2026-08-23T00:00:10Z",
            },
            {
                "kind": "trial_finished",
                "occurred_at": "2026-08-23T00:01:00Z",
            },
        ],
    }


def test_level_oracle_passes_only_with_independent_effect_source_and_source_diagnosis() -> None:
    evaluator = RuntimeLevelEvaluator(
        episode_id="EPI-1",
        expected_diagnosis_terms=["timeout", "abortsignal"],
        source_evidence_refs=["source://frontend/Shipping.gateway.ts:31"],
        effect_observer=lambda *_args: {
            "verified": True,
            "evidence_refs": ["prometheus://effect"],
        },
        agent_result_loader=lambda _report: {
            "suspected_defect": "Missing timeout and AbortSignal on downstream fetch",
            "evidence": [{"source": "source_ro", "summary": "fetch has no signal"}],
        },
    )

    result = evaluator(_ticket(), {"level_id": "L1", "disturbances": []}, _trial_report(), [])

    assert result["primary_status"] == "PASS"
    assert result["metrics"]["tokens_used"] == 200_000


def test_level_oracle_does_not_accept_agent_claim_when_effect_is_unverified() -> None:
    evaluator = RuntimeLevelEvaluator(
        episode_id="EPI-1",
        expected_diagnosis_terms=["timeout"],
        source_evidence_refs=["source://evidence"],
        effect_observer=lambda *_args: {"verified": False, "evidence_refs": []},
        agent_result_loader=lambda _report: {
            "suspected_defect": "timeout",
            "evidence": [{"source": "source_ro"}],
        },
    )

    result = evaluator(_ticket(), {"level_id": "L1", "disturbances": []}, _trial_report(), [])

    assert result["primary_status"] == "FAIL"
    assert result["failure_status"] == "INCONCLUSIVE"


def test_run_oracle_preserves_level_results_and_violations() -> None:
    level = {
        "level_id": "L1",
        "primary_status": "PASS",
        "violations": [],
        "result_ref": "level-L1.json",
    }
    result = RuntimeRunOracle()(
        object(),
        {"episode_id": "EPI-1"},
        {"status": "PASS", "level_results": [level]},
        {"verified": True},
    )

    assert result["independent"] is True
    assert result["status"] == "PASS"
    assert result["level_results"] == [level]


def test_disturbed_level_reuses_only_a_previously_verified_l1_fault_effect() -> None:
    calls = []

    def effect(ticket, *_args):
        calls.append(ticket.level_id)
        return {
            "verified": ticket.level_id == "L1",
            "evidence_refs": [f"prometheus://{ticket.level_id}/effect"],
        }

    evaluator = RuntimeLevelEvaluator(
        episode_id="EPI-1",
        expected_diagnosis_terms=["timeout"],
        source_evidence_refs=["source://evidence"],
        effect_observer=effect,
        agent_result_loader=lambda _report: {
            "suspected_defect": "timeout",
            "evidence": [{"source": "source_ro"}],
        },
    )
    l1 = _trial_report()
    l1["mainFaultAppliedObserved"] = True
    l1["finalization"]["fault_provenance"] = {"ever_active": True}
    first = evaluator(_ticket(), {"level_id": "L1", "disturbances": []}, l1, [])
    l2_ticket = TrialTicket(
        trial_id="run-1-L2-a1",
        run_id="run-1",
        episode_id="EPI-1",
        level_id="L2",
        attempt=1,
    )
    l2 = _trial_report()
    l2["mainFaultAppliedObserved"] = True
    l2["finalization"]["fault_provenance"] = {"ever_active": True}
    second = evaluator(
        l2_ticket,
        {"level_id": "L2", "disturbances": [{"expected_behaviors": []}]},
        l2,
        [],
    )

    assert first["primary_status"] == "PASS"
    assert next(
        gate for gate in second["gate_results"] if gate["gate_id"] == "fault_effect"
    )["status"] == "PASS"


def test_client_workload_summary_is_an_independent_fault_effect_observer() -> None:
    result = _workload_summary_effect(
        {
            "baseline_summary": {
                "p95LatencyMs": 27,
                "errorRate": 0.0,
                "throughputRps": 7.62,
                "minimumThroughputRps": 7.24,
            }
        },
        {
            "experiment_summary": {
                "requests": 1300,
                "p95LatencyMs": 128,
                "errorRate": 0.0,
                "throughputRps": 7.55,
            },
            "evidence_refs": ["kubernetes://otel-demo/pvc/workload-results"],
        },
    )

    assert result is not None
    assert result["verified"] is True
    assert result["observer"] == "deterministic-client-workload"
    assert result["latency_delta_ms"] == 101
