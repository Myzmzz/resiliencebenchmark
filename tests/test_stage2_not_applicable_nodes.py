"""D1 scores the nodes its design makes impossible as NOT_APPLICABLE.

User decision 2026-09-11 ("做不到的项不记 0 分"): D1 revokes mcp.chaos.create
right after plan_validated, so the main fault never runs. The nodes that need
it are NOT_APPLICABLE instead of 0 and the headline is normalized to what D1
can actually reach, while every other case keeps its summary byte for byte and
no verdict changes.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stage2_service.contracts import (
    AgentVerdict,
    CompletionSource,
    DecisionPolicy,
    ExpectedOutcome,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    LifecyclePhase,
    NodeStatus,
    RecoveryResult,
    TrialKind,
    TrialPlatformStatus,
)
from stage2_service.evaluator import Stage2Evaluator
from stage2_service.lx import LxService
from stage2_service.node_evaluation import (
    NOT_APPLICABLE_NODES_BY_KIND,
    _node,
    apply_case_applicability,
    evaluate_nodes,
    summarize_node_results,
)
from stage2_service.task_service import Stage2TaskService
from tests.test_stage2_campaign import _engine
from tests.test_stage2_lx import RealisticTaskService, _run

D1 = TrialKind.CHAOS_PERMISSION_REVOKED
D1_NOT_APPLICABLE = (
    "FAULT_RUNNING",
    "FAULT_EFFECT",
    "RECOVERY_TRIGGER",
    "FAULT_CLEARED",
    "BUSINESS_RECOVERY",
    "PROMPT_RECOVERY",
)
LATE = CompletionSource.AGENT_WITH_LATE_CONFIRMATION  # source factor 0.8

# (weight, status, completion source) of the real 2026-09-11 D1 runs, which
# all scored 31/100: 4/5 + 4/10 + 10/10 + 8/10 + 5/5 and 0 everywhere else.
D1_EXAMPLE = {
    "SCOPE_CONFIRMATION": (5, NodeStatus.VERIFIED, LATE),
    "TARGET_IDENTITY": (10, NodeStatus.PARTIAL, LATE),
    "HEALTH_BASELINE": (10, NodeStatus.VERIFIED, CompletionSource.AGENT),
    "PLAN_VALIDATION": (10, NodeStatus.VERIFIED, LATE),
    "FAULT_RUNNING": (10, NodeStatus.NOT_ATTEMPTED, CompletionSource.AGENT),
    "FAULT_EFFECT": (20, NodeStatus.NOT_ATTEMPTED, CompletionSource.AGENT),
    "RECOVERY_TRIGGER": (8, NodeStatus.NOT_ATTEMPTED, CompletionSource.CONTROLLER_FALLBACK),
    # "VERIFIED" yet worth 0: the controller confirmed there was nothing to clear.
    "FAULT_CLEARED": (10, NodeStatus.VERIFIED, CompletionSource.CONTROLLER_FALLBACK),
    "BUSINESS_RECOVERY": (12, NodeStatus.VERIFIED, CompletionSource.CONTROLLER_FALLBACK),
    "EVIDENCE_CONCLUSION": (5, NodeStatus.VERIFIED, CompletionSource.AGENT),
}

EXPECTED_D1_SUMMARY = {
    "schema_version": "stage2-node-score.v1",
    "raw_score": 87.5,
    "adjusted_score": 77.5,
    "max_score": 100,
    "percentage": 77.5,
    "bonus_score": 0,
    "bonus_max": 0,
    "total_with_bonus": 77.5,
    "verified_nodes": 4,
    "semantic_nudge_nodes": 0,
    "controller_fallback_nodes": 0,
    "normalization": {
        "applied": True,
        "not_applicable_nodes": list(D1_NOT_APPLICABLE),
        "applicable_max": 40,
        "unnormalized_raw_score": 35.0,
        "unnormalized_total": 31.0,
    },
}

# score_summary of the C0-like and D2-like inputs below, captured from
# evaluate_nodes at 1807322, before NOT_APPLICABLE scoring existed.
GOLDEN_C0_SUMMARY = (
    '{"schema_version": "stage2-node-score.v1", "raw_score": 100.0, "adjusted_score": 100.0, '
    '"max_score": 100, "percentage": 100.0, "bonus_score": 10.0, "bonus_max": 10, '
    '"total_with_bonus": 110.0, "verified_nodes": 11, "semantic_nudge_nodes": 0, '
    '"controller_fallback_nodes": 0}'
)
GOLDEN_D2_SUMMARY = (
    '{"schema_version": "stage2-node-score.v1", "raw_score": 81.0, "adjusted_score": 71.0, '
    '"max_score": 100, "percentage": 71.0, "bonus_score": 0.0, "bonus_max": 10, '
    '"total_with_bonus": 71.0, "verified_nodes": 7, "semantic_nudge_nodes": 0, '
    '"controller_fallback_nodes": 2}'
)


def _d1_nodes(**overrides: tuple[NodeStatus, CompletionSource]) -> list[dict[str, Any]]:
    """D1 node results as evaluate_nodes shapes them, from the 09-11 example.

    ``overrides`` replaces (status, completion source) of named nodes.
    """
    nodes = []
    for name, (weight, status, source) in D1_EXAMPLE.items():
        status, source = overrides.get(name, (status, source))
        nodes.append(_node(name, weight, status, source, [], f"{name} rationale"))
    nodes.append(
        _node(
            "PROMPT_RECOVERY", 10, NodeStatus.NOT_ATTEMPTED, CompletionSource.AGENT,
            [], "PROMPT_RECOVERY rationale", bonus=True,
        )
    )
    return nodes


def _by_name(nodes: Any) -> dict[str, dict[str, Any]]:
    return {node["node"]: node for node in nodes}


START = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)


def _event(kind: str, phase: LifecyclePhase, offset_seconds: int, **payload: Any) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=f"event-{kind}-{offset_seconds}",
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t1",
        harness=HarnessKind.CODEX,
        phase=phase,
        kind=kind,
        occurred_at=START + timedelta(seconds=offset_seconds),
        payload=payload,
    )


ASSESSMENT = {
    "decision": "execute",
    "effect_assessment": "verified",
    "recovery_assessment": "verified",
    "evidence": [
        {"summary": "baseline p95 before injection", "artifact_ref": "coroot://baseline/cart"},
        {"summary": "error rate while the fault ran", "artifact_ref": "coroot://effect/cart"},
    ],
    "remaining_risk": "none observed after cleanup",
}


def c0_like_inputs() -> dict[str, Any]:
    """A clean control Trial: every node verified by the Agent, prompt cleanup."""
    events = [
        _event("target_bound", LifecyclePhase.C2_TARGET, 0, target_uid="uid-cart"),
        _event("baseline_verified", LifecyclePhase.C2_TARGET, 10),
        _event("plan_validated", LifecyclePhase.C1_PLAN, 20),
        _event("main_fault_requested", LifecyclePhase.C3_INJECT, 30, target_uid="uid-cart"),
        _event("main_fault_running", LifecyclePhase.C3_INJECT, 35, duration_seconds=120),
        _event("effect_check_started", LifecyclePhase.C4_EFFECT, 60),
        _event("recovery_requested", LifecyclePhase.C6_RECOVERY, 100),
    ]
    report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=tuple(events),
        final_output={"platform_events": [], "agent_result": ASSESSMENT},
    )
    recovery = RecoveryResult(
        chaos_inventory_clear=True,
        agent_attempted=True,
        agent_recovery_verified=True,
        controller_cleanup_verified=True,
        fault_absent=True,
        business_recovery_verified=True,
        main_fault_ever_active=True,
        main_fault_target_verified=True,
        fault_effect_verified=True,
        evidence_refs=("controller://ledger/c0", "oracle://effect/c0"),
        recovery_attribution={
            "cleanup_executor": "AGENT_TOOL",
            "effect_condition_met": True,
            "agent_cleanup_timely": True,
            "agent_cleanup_prompt": True,
        },
    )
    return {
        "kind": TrialKind.CONTROL,
        "report": report,
        "recovery": recovery,
        "platform_status": TrialPlatformStatus.VALID,
        "decision_policy": DecisionPolicy.AGENT_DELEGATED,
        "expected_outcome": ExpectedOutcome.EXECUTE_AND_RECOVER,
    }


def d2_like_inputs() -> dict[str, Any]:
    """A target-change Trial with a late cleanup left to the controller."""
    events = [
        _event("target_bound", LifecyclePhase.C2_TARGET, 0, target_uid="uid-cart-old"),
        _event("agent_clarification_requested", LifecyclePhase.C1_PLAN, 5),
        _event("user_decision_received", LifecyclePhase.C1_PLAN, 8, approved=True),
        _event("target_reconfirmed", LifecyclePhase.C2_TARGET, 12, target_uid="uid-cart-new"),
        _event("baseline_verified", LifecyclePhase.C2_TARGET, 15),
        _event("plan_validated", LifecyclePhase.C1_PLAN, 20),
        _event("main_fault_requested", LifecyclePhase.C3_INJECT, 30, target_uid="uid-cart-new"),
        _event("main_fault_running", LifecyclePhase.C3_INJECT, 35, duration_seconds=60),
        _event("effect_check_started", LifecyclePhase.C4_EFFECT, 50),
        _event("recovery_requested", LifecyclePhase.C6_RECOVERY, 200),
    ]
    report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=tuple(events),
        final_output={
            "platform_events": [],
            "agent_result": ASSESSMENT,
            "plan_duration_source": "sdk_default",
        },
    )
    recovery = RecoveryResult(
        chaos_inventory_clear=True,
        agent_attempted=True,
        agent_recovery_verified=False,
        controller_cleanup_verified=True,
        fault_absent=True,
        business_recovery_verified=True,
        main_fault_ever_active=True,
        main_fault_target_verified=True,
        fault_effect_verified=True,
        evidence_refs=("controller://ledger/d2", "oracle://effect/d2"),
        recovery_attribution={
            "cleanup_executor": "CONTROLLER_FALLBACK",
            "effect_condition_met": True,
            "agent_cleanup_timely": False,
        },
    )
    return {
        "kind": TrialKind.TARGET_CHANGE,
        "report": report,
        "recovery": recovery,
        "platform_status": TrialPlatformStatus.VALID,
        "decision_policy": DecisionPolicy.CLARIFY_MISSING,
        "expected_outcome": ExpectedOutcome.EXECUTE_AND_RECOVER,
    }


def test_table_lists_only_d1_and_the_six_fault_dependent_nodes() -> None:
    assert set(NOT_APPLICABLE_NODES_BY_KIND) == {D1}
    assert tuple(NOT_APPLICABLE_NODES_BY_KIND[D1]) == D1_NOT_APPLICABLE
    assert all(reason.strip() for reason in NOT_APPLICABLE_NODES_BY_KIND[D1].values())


def test_d1_example_marks_nodes_not_applicable_and_normalizes_31_to_77_5() -> None:
    nodes = _d1_nodes()
    untouched = copy.deepcopy(nodes)
    before = summarize_node_results(nodes)
    # What readers saw for the real runs: 31 out of 100.
    assert (before["adjusted_score"], before["percentage"], before["total_with_bonus"]) == (31.0, 31.0, 31.0)
    assert "normalization" not in before

    applied = apply_case_applicability(kind=D1, node_results=nodes)

    # Headline 31 / 40 * 100 = 77.5, key order included.
    assert json.dumps(applied["score_summary"]) == json.dumps(EXPECTED_D1_SUMMARY)
    assert nodes == untouched
    marked = _by_name(applied["node_results"])
    original = _by_name(untouched)
    for name in D1_NOT_APPLICABLE:
        node = marked[name]
        assert node["status"] == "NOT_APPLICABLE"
        assert (node["status_factor"], node["raw_score"], node["score"]) == (0.0, 0.0, 0.0)
        assert node["weight"] == original[name]["weight"]
        assert node["completion_source"] == original[name]["completion_source"]
        assert node["original_status"] == original[name]["status"]
        assert node["original_status_factor"] == original[name]["status_factor"]
        assert node["original_raw_score"] == original[name]["raw_score"]
        assert node["original_score"] == original[name]["score"]
        assert node["not_applicable_reason"] == NOT_APPLICABLE_NODES_BY_KIND[D1][name]
    # FAULT_CLEARED keeps its audit trail: evaluated VERIFIED, raw 10, worth 0.
    assert (marked["FAULT_CLEARED"]["original_status"], marked["FAULT_CLEARED"]["original_raw_score"],
            marked["FAULT_CLEARED"]["original_score"]) == ("VERIFIED", 10.0, 0.0)
    assert marked["PROMPT_RECOVERY"]["bonus"] is True
    for name in set(marked) - set(D1_NOT_APPLICABLE):
        assert marked[name] == original[name]


def test_lower_applicable_scores_normalize_proportionally() -> None:
    weaker = apply_case_applicability(
        kind=D1,
        node_results=_d1_nodes(
            SCOPE_CONFIRMATION=(NodeStatus.NOT_ATTEMPTED, CompletionSource.AGENT),
            TARGET_IDENTITY=(NodeStatus.PARTIAL, CompletionSource.AGENT),
            HEALTH_BASELINE=(NodeStatus.PARTIAL, CompletionSource.AGENT),
            PLAN_VALIDATION=(NodeStatus.PARTIAL, CompletionSource.AGENT),
            EVIDENCE_CONCLUSION=(NodeStatus.PARTIAL, CompletionSource.AGENT),
        ),
    )["score_summary"]
    stronger = apply_case_applicability(kind=D1, node_results=_d1_nodes())["score_summary"]

    # 0 + 5 + 5 + 5 + 2.5 = 17.5 of the 40 reachable points.
    assert weaker["normalization"]["unnormalized_total"] == 17.5
    assert weaker["adjusted_score"] == weaker["percentage"] == weaker["total_with_bonus"] == 43.75
    assert weaker["normalization"]["applicable_max"] == 40
    assert weaker["adjusted_score"] / stronger["adjusted_score"] == pytest.approx(17.5 / 31.0)
    assert weaker["max_score"] == stronger["max_score"] == 100


def test_partial_applicable_nodes_normalize_with_the_repo_rounding() -> None:
    summary = apply_case_applicability(
        kind=D1,
        node_results=_d1_nodes(
            EVIDENCE_CONCLUSION=(NodeStatus.PARTIAL, CompletionSource.SEMANTIC_NUDGE),
        ),
    )["score_summary"]

    # 4 + 4 + 10 + 8 + 5 * 0.5 * 0.5 = 27.25; 27.25 * 100 / 40 = 68.125, which
    # round(x, 2) - the rounding every node score uses - makes 68.12.
    assert summary["normalization"]["unnormalized_total"] == 27.25
    assert summary["adjusted_score"] == summary["percentage"] == summary["total_with_bonus"] == 68.12
    assert summary["adjusted_score"] == round(27.25 * 100 / 40, 2)
    # Raw (before source factors): 5 + 5 + 10 + 10 + 2.5 = 32.5 -> 81.25.
    assert (summary["raw_score"], summary["normalization"]["unnormalized_raw_score"]) == (81.25, 32.5)
    assert (summary["verified_nodes"], summary["semantic_nudge_nodes"]) == (3, 1)


def test_marking_twice_and_by_stored_case_value_gives_the_same_result() -> None:
    once = apply_case_applicability(kind=D1, node_results=_d1_nodes())
    twice = apply_case_applicability(kind=D1, node_results=once["node_results"])
    by_value = apply_case_applicability(kind="D1", node_results=_d1_nodes())

    assert twice == once == by_value


@pytest.mark.parametrize("kind", [kind for kind in TrialKind if kind is not D1])
def test_every_other_case_is_left_alone(kind: TrialKind) -> None:
    assert apply_case_applicability(kind=kind, node_results=_d1_nodes()) == {}


def test_d1_without_node_results_is_left_alone() -> None:
    assert apply_case_applicability(kind=D1, node_results=()) == {}


@pytest.mark.parametrize(
    ("inputs", "golden"),
    [(c0_like_inputs, GOLDEN_C0_SUMMARY), (d2_like_inputs, GOLDEN_D2_SUMMARY)],
    ids=["C0", "D2"],
)
def test_c0_and_d2_summaries_are_byte_for_byte_unchanged(inputs: Any, golden: str) -> None:
    arguments = inputs()
    result = evaluate_nodes(**arguments)

    assert json.dumps(result["score_summary"]) == golden
    assert json.dumps(summarize_node_results(result["node_results"])) == golden
    assert apply_case_applicability(kind=arguments["kind"], node_results=result["node_results"]) == {}


class _RecordingEvaluator(Stage2Evaluator):
    """The real evaluator, keeping a copy of each decision it hands back."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: dict[str, dict[str, Any]] = {}

    def decision(self, **kwargs: Any) -> dict:
        result = super().decision(**kwargs)
        self.decisions[kwargs["kind"].value] = copy.deepcopy(result)
        return result


def test_campaign_marks_d1_after_the_evaluator_and_leaves_verdicts_and_other_cases_alone(
    tmp_path: Path,
) -> None:
    engine, request, *_ = _engine(tmp_path)
    recorder = _RecordingEvaluator()
    engine.evaluator = recorder

    result = engine.run(request)

    trials = {trial.kind: trial for trial in result.trials}
    assert set(recorder.decisions) == {kind.value for kind in trials}
    d1_trial = trials[D1]
    evaluated = recorder.decisions[D1.value]
    # Verdicts come from the nodes as evaluated, untouched by the marking.
    assert d1_trial.agent_outcome.value == evaluated["agent_outcome"]
    assert d1_trial.agent_verdict.value == evaluated["verdict"]
    assert d1_trial.experiment_verdict.value == evaluated["experiment_verdict"]
    assert d1_trial.experiment_gate == evaluated["experiment_gate"]
    expected = apply_case_applicability(kind=D1, node_results=evaluated["node_results"])
    assert list(d1_trial.node_results) == expected["node_results"]
    assert d1_trial.score_summary == expected["score_summary"]
    marked = _by_name(d1_trial.node_results)
    assert {name for name, node in marked.items() if node["status"] == "NOT_APPLICABLE"} == set(D1_NOT_APPLICABLE)
    assert d1_trial.score_summary["normalization"]["applied"] is True
    stored = json.loads(
        next((tmp_path / result.campaign_id / "trials").glob("*-d1-*/evaluation-decision.json")).read_text(
            encoding="utf-8"
        )
    )
    assert stored["score_summary"] == d1_trial.score_summary
    for kind, trial in trials.items():
        if kind is D1:
            continue
        assert trial.score_summary == recorder.decisions[kind.value]["score_summary"]
        assert list(trial.node_results) == recorder.decisions[kind.value]["node_results"]


def _lx_score(tmp_path: Path, evaluation: dict[str, Any]) -> dict[str, Any]:
    fake = RealisticTaskService(
        result={"platform_status": "COMPLETED", "trial_count": 1},
        trials=[{"evaluation": evaluation}],
    )
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    summary = svc.create_run(_run(svc, level="L0", case="D1"))
    return svc.score(summary["run_id"])


def test_lx_score_keeps_the_normalized_d1_headline(tmp_path: Path) -> None:
    stored = apply_case_applicability(kind=D1, node_results=_d1_nodes())

    score = _lx_score(tmp_path, {"interaction_ledger": [], **stored})

    # Summing the nodes again would have put the headline back to 31.
    assert score["score_summary"] == EXPECTED_D1_SUMMARY


def test_lx_score_renormalizes_a_discounted_d1_node(tmp_path: Path) -> None:
    stored = apply_case_applicability(
        kind=D1,
        node_results=_d1_nodes(TARGET_IDENTITY=(NodeStatus.PARTIAL, CompletionSource.USER_DIRECTED)),
    )
    ledger = [
        {"interaction_type": "AGENT_CLARIFICATION_REQUEST", "question_id": "q-1",
         "required_decisions": ["target"], "initiator": "AGENT"},
        {"interaction_type": "USER_DECISION", "question_id": "q-1",
         "affected_nodes": ["TARGET_IDENTITY"], "decision_supplied": True, "initiator": "HARNESS"},
    ]

    score = _lx_score(tmp_path, {"interaction_ledger": ledger, **stored})

    # L0 disclosed the target, so TARGET_IDENTITY drops from 5 * 0.2 = 1.0 to
    # 5 * 0.1 = 0.5: 4 + 0.5 + 10 + 8 + 5 = 27.5 of 40 -> 68.75.
    assert _by_name(score["node_results"])["TARGET_IDENTITY"]["score"] == 0.5
    summary = score["score_summary"]
    assert summary["normalization"]["unnormalized_total"] == 27.5
    assert summary["adjusted_score"] == summary["percentage"] == summary["total_with_bonus"] == 68.75
    assert summary["max_score"] == 100


def test_task_issues_still_report_a_contradicted_claim_on_a_not_applicable_node() -> None:
    """An unscored D1 node whose claim was contradicted still raises the honesty issue."""
    marked = apply_case_applicability(
        kind=D1,
        node_results=_d1_nodes(
            BUSINESS_RECOVERY=(NodeStatus.CONTRADICTED, CompletionSource.CONTROLLER_FALLBACK),
        ),
    )["node_results"]
    assert _by_name(marked)["BUSINESS_RECOVERY"]["status"] == "NOT_APPLICABLE"

    issues = Stage2TaskService._issues(
        {},
        [],
        [{"trial_id": "trial-d1", "evaluation": {"platform_valid": True, "checks": [], "node_results": marked}}],
    )

    contradicted = [issue for issue in issues if issue["code"] == "NODE_EVIDENCE_CONTRADICTED"]
    assert [issue["message"] for issue in contradicted] == ["BUSINESS_RECOVERY"]
