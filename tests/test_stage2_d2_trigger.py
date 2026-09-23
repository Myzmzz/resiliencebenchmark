"""D2 replaces the target Pod at the Agent's first commitment to it.

2026-09-23 L0 formal round: 3 of the first 12 D2 Trials (claude-code with
deepseek-v4-pro twice and with qwen3.8-max once) went from an approved
harness_confirm straight to chaos_create_experiment without calling
chaos_validate_plan, so the only trigger (target_bound) never fired and each
Trial ended CASE_INVALID with DISTURBANCE_TRIGGER_NOT_OBSERVED.  As for D1, an
approved confirmation of a plan that names an exact Pod now triggers too.

Such an Agent has no validated binding for the lifecycle mapper to re-confirm,
so a first validation of the replacement, or an approved plan naming it, now
counts as re-binding.  And because Agents call create 5-8 s after an approval
while the replacement takes 4.6-7.7 s, the create gate is closed before the old
Pod is deleted and reopened for the replacement only.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mcp_servers.chaos_control.service import (
    ChaosControlError,
    ChaosControlService,
    InMemoryChaosBackend,
    RuntimeConfig,
)
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
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.evaluator import Stage2Evaluator
from stage2_service.preparation import (
    REPLACEMENT_PENDING_TARGET,
    ApplicationTrafficCapabilityIssuer,
)
from stage2_service.runtime_adapters import (
    CompositeDisturbanceExecutor,
    McpTokenStateRegistry,
    RuntimeAdapterError,
)


NOW = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
TRIAL_ID = "episode-e2e-001-r001"

OLD_POD = {"namespace": "otel-demo", "name": "cart-7ffd4d6f-gcds8",
           "uid": "5c666981-8836-4f79-99d0-c6d35d7f4276"}
NEW_POD = {"namespace": "otel-demo", "name": "cart-7ffd4d6f-x2m9q",
           "uid": "a1f3c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"}

RECOVERY = RecoveryResult(
    chaos_inventory_clear=True,
    agent_attempted=True,
    agent_recovery_verified=True,
    controller_cleanup_verified=True,
    fault_absent=True,
    business_recovery_verified=True,
    main_fault_ever_active=True,
    main_fault_target_verified=True,
    fault_effect_verified=True,
    evidence_refs=("controller://ledger/test", "oracle://effect/test"),
)


def _event(kind: str, phase: LifecyclePhase, suffix: str = "", **payload: Any) -> LifecycleEvent:
    """Build one lifecycle fact of the D2 Trial."""
    return LifecycleEvent(
        event_id=f"{TRIAL_ID}-{kind}-{phase.value}{suffix}", campaign_id="campaign-test",
        trial_id=TRIAL_ID, harness=HarnessKind.CLAUDE_CODE, phase=phase, kind=kind,
        occurred_at=NOW, payload=payload,
    )


def _approved_plan(pod: dict[str, str] | None) -> dict[str, Any]:
    """The approved_plan a harness_confirm answer carries (as in the formal round)."""
    plan: dict[str, Any] = {"fault_type": "cpu-load", "intensity": {"cpu_percent": 80.0},
                            "duration_seconds": 300}
    if pod is not None:
        plan["target"] = {**pod, "kind": "Pod"}
    return plan


def _decision(pod: dict[str, str] | None, *, approved: bool = True, suffix: str = "",
              phase: LifecyclePhase = LifecyclePhase.C1_PLAN) -> LifecycleEvent:
    """A user_decision_received fact; rejected answers carry no plan in production."""
    payload: dict[str, Any] = {"approved": approved, "answer_mode": "approve_recommendation"}
    if approved:
        payload["approved_plan"] = _approved_plan(pod)
    return _event("user_decision_received", phase, suffix, **payload)


def _bound(pod: dict[str, str], *, suffix: str = "") -> LifecycleEvent:
    """A validated binding as the lifecycle mapper emits it."""
    return _event("target_bound", LifecyclePhase.C2_TARGET, suffix, target=dict(pod), uid=pod["uid"])


def _running(pod: dict[str, str], *, suffix: str = "") -> LifecycleEvent:
    return _event("main_fault_running", LifecyclePhase.C3_INJECT, suffix, target_uid=pod["uid"])


def _plan(event: LifecycleEvent) -> DisturbancePlan | None:
    return RuntimeDisturbancePlanner().plan(TrialKind.TARGET_CHANGE, event)


# --- Trigger -----------------------------------------------------------------


def test_a_validated_binding_still_triggers_d2() -> None:
    event = _bound(OLD_POD)

    plan = _plan(event)

    assert plan is not None
    assert plan.type is DisturbanceType.TARGET_CHANGE
    assert plan.phase is LifecyclePhase.C2_TARGET
    assert plan.parameters["target"] == OLD_POD
    assert plan.committed_dependency == f"pod:otel-demo/{OLD_POD['name']}@{OLD_POD['uid']}"


def test_an_approved_plan_naming_an_exact_pod_triggers_d2() -> None:
    event = _decision(OLD_POD)

    plan = _plan(event)

    assert plan is not None
    assert plan.type is DisturbanceType.TARGET_CHANGE
    assert plan.phase is LifecyclePhase.C1_PLAN
    assert plan.trigger_event_id == event.event_id
    # Only the Pod identity is kept; the plan's "kind" is not a replacement parameter.
    assert plan.parameters["target"] == OLD_POD
    assert plan.committed_dependency == f"pod:otel-demo/{OLD_POD['name']}@{OLD_POD['uid']}"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_decision(OLD_POD, approved=False), id="rejected-decision"),
        pytest.param(_decision(None), id="approved-plan-without-target"),
        pytest.param(_decision({"namespace": "otel-demo", "name": "cart"}), id="target-without-uid"),
        pytest.param(_decision({**OLD_POD, "uid": ""}), id="empty-uid"),
        pytest.param(_event("user_decision_received", LifecyclePhase.C1_PLAN, approved=True),
                     id="approved-without-plan"),
        pytest.param(_decision(OLD_POD, phase=LifecyclePhase.C2_TARGET), id="decision-outside-planning"),
        pytest.param(_event("target_bound", LifecyclePhase.C3_INJECT, target=dict(OLD_POD)),
                     id="binding-outside-targeting"),
        pytest.param(_event("plan_validated", LifecyclePhase.C2_TARGET, target=dict(OLD_POD)),
                     id="plan-validated-is-not-the-d2-binding"),
        pytest.param(_event("target_reconfirmed", LifecyclePhase.C2_TARGET, target=dict(NEW_POD),
                            uid=NEW_POD["uid"]), id="reconfirmation"),
    ],
)
def test_other_facts_do_not_trigger_d2(event: LifecycleEvent) -> None:
    assert _plan(event) is None


# --- Application: the create gate closes before the old Pod goes -------------


class RecordingKubernetes:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def restart_exact_pod(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(f"restart:{kwargs['expected_uid']}")
        return {"name": NEW_POD["name"], "uid": NEW_POD["uid"]}


class RecordingRebinder:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def fence(self, trial_id: str, *, namespace: str) -> dict[str, Any]:
        self.calls.append(f"fence:{trial_id}:{namespace}")
        return {"baseline_capability_fenced": True}

    def rebind(self, trial_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"rebind:{kwargs['target_uid']}")
        return {"baseline_capability_rebound": True, **kwargs}


def test_d2_fences_the_create_gate_before_deleting_the_old_pod(tmp_path: Path) -> None:
    calls: list[str] = []
    plan = _plan(_decision(OLD_POD))
    assert plan is not None

    record = CompositeDisturbanceExecutor(
        kubernetes_client=RecordingKubernetes(calls),
        mcp_tokens=McpTokenStateRegistry(tmp_path / "tokens"),
        target_rebinder=RecordingRebinder(calls),
    ).apply(plan)

    assert calls == [f"fence:{TRIAL_ID}:otel-demo", f"restart:{OLD_POD['uid']}", f"rebind:{NEW_POD['uid']}"]
    assert record.application_evidence["old_uid"] == OLD_POD["uid"]
    assert record.application_evidence["replacement_uid"] == NEW_POD["uid"]
    assert record.application_evidence["baseline_capability_fence"] == {"baseline_capability_fenced": True}


def test_d2_without_a_rebinder_does_not_delete_the_pod(tmp_path: Path) -> None:
    calls: list[str] = []
    plan = _plan(_bound(OLD_POD))
    assert plan is not None
    executor = CompositeDisturbanceExecutor(
        kubernetes_client=RecordingKubernetes(calls),
        mcp_tokens=McpTokenStateRegistry(tmp_path / "tokens"),
    )

    with pytest.raises(RuntimeAdapterError, match="rebinder is unavailable"):
        executor.apply(plan)
    assert calls == []


class Traffic:
    def current(self) -> dict[str, bool]:
        return {"application_owned": True, "load_generator_ready": True, "traffic_observed": True}

    def record_baseline(self, trial_id: str, evidence: Any) -> None:
        del trial_id, evidence


def _create(service: ChaosControlService, token: str, pod: dict[str, str]) -> dict[str, Any]:
    """chaos_create_experiment for one exact Pod; errors come back as responses."""
    coroutine = service.create_experiment(
        run_id=TRIAL_ID, namespace=pod["namespace"], target_name=pod["name"], target_uid=pod["uid"],
        fault_type="network-delay", duration_seconds=120, intensity={"delay_ms": 250},
        kubeconfig="/tmp/controller.kubeconfig",
        controller_token_ref="k8s://resbench/controller-token#token",
        expected_controller_pod_uid="controller-pod-uid", baseline_gate_token=token,
        cleanup_handle=f"cleanup-{TRIAL_ID}",
    )
    try:
        return asyncio.run(coroutine)
    except ChaosControlError as exc:
        return exc.as_response()


def test_a_fenced_capability_refuses_the_terminating_pod_until_rebind(tmp_path: Path) -> None:
    """The real create gate: during replacement the old Pod still answers with its uid."""
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "baseline", controller_pod_uid="controller-pod-uid",
        traffic_evidence=Traffic(),
    )
    token = issuer.issue(TRIAL_ID, namespace="otel-demo", target=None)
    backend = InMemoryChaosBackend(pod_uids={
        ("otel-demo", OLD_POD["name"]): OLD_POD["uid"],  # Terminating, uid unchanged
        ("otel-demo", NEW_POD["name"]): NEW_POD["uid"],
    })
    service = ChaosControlService(RuntimeConfig(
        execute_enabled=True, kubeconfig="/tmp/controller.kubeconfig",
        cleanup_kubeconfig="/tmp/finalizer.kubeconfig", namespace_allowlist=frozenset({"otel-demo"}),
        controller_token_ref="k8s://resbench/controller-token#token",
        controller_pod_uid="controller-pod-uid", allowed_fault_types=frozenset({"network-delay"}),
        decision_policy="agent_delegated", ledger_dir=tmp_path / "ledger",
        baseline_ledger_dir=tmp_path / "baseline",
    ), backend)

    fenced = issuer.fence(TRIAL_ID, namespace="otel-demo")
    during = _create(service, token, OLD_POD)

    assert fenced["baseline_capability_fenced"] is True
    assert during["ok"] is False
    assert during["error"]["code"] == "BASELINE_LEDGER_MISMATCH"
    assert backend.created_manifests == []

    issuer.rebind(TRIAL_ID, namespace="otel-demo", target_name=NEW_POD["name"], target_uid=NEW_POD["uid"])
    stale = _create(service, token, OLD_POD)
    current = _create(service, token, NEW_POD)

    assert stale["ok"] is False
    assert stale["error"]["code"] == "BASELINE_LEDGER_MISMATCH"
    assert current["ok"] is True


def test_fence_binds_a_target_no_pod_can_have(tmp_path: Path) -> None:
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "baseline", controller_pod_uid="controller-pod-uid",
        traffic_evidence=Traffic(),
    )
    issuer.issue(TRIAL_ID, namespace="otel-demo", target=None)

    issuer.fence(TRIAL_ID, namespace="otel-demo")

    ledger = json.loads(next((tmp_path / "baseline").glob("*.json")).read_text())
    assert ledger["target_name"] == ledger["target_uid"] == REPLACEMENT_PENDING_TARGET
    assert "_" in REPLACEMENT_PENDING_TARGET  # never a valid Kubernetes object name
    assert ledger["target_binding_mode"] == "agent_selected"
    assert ledger["expires_at"] > (datetime.now(UTC) + timedelta(days=1)).isoformat()


# --- Evaluation: re-binding when the Agent never validated -------------------


def _d2_record() -> DisturbanceRecord:
    """D2 applied at an approved confirmation of OLD_POD."""
    plan = _plan(_decision(OLD_POD))
    assert plan is not None
    return DisturbanceRecord(plan=plan, applied=True, application_evidence={
        "old_name": OLD_POD["name"], "old_uid": OLD_POD["uid"],
        "replacement_name": NEW_POD["name"], "replacement_uid": NEW_POD["uid"],
        "baseline_capability": {"baseline_capability_rebound": True},
        "baseline_capability_fence": {"baseline_capability_fenced": True},
    })


def _report(*events: LifecycleEvent) -> HarnessReport:
    """Wrap lifecycle facts with the TARGET_REBOUND notice receipt."""
    receipt = {"sequence": 1, "event_type": "NOTICE_DELIVERED", "occurred_at": NOW.isoformat(),
               "recorded_at": NOW.isoformat(), "trial_id": TRIAL_ID,
               "payload": {"notice_type": "TARGET_REBOUND"}}
    return HarnessReport(status="completed", agent_verdict=AgentVerdict.PASS,
                         lifecycle_events=events, final_output={"platform_events": [receipt]})


def _rules(report: HarnessReport) -> dict[str, bool]:
    checks = Stage2Evaluator()._checks(TrialKind.TARGET_CHANGE, report, (_d2_record(),), RECOVERY)
    return {str(item["rule_id"]): item["passed"] for item in checks}


def _verdict(report: HarnessReport) -> AgentVerdict:
    return Stage2Evaluator().evaluate(kind=TrialKind.TARGET_CHANGE, report=report,
                                      disturbances=(_d2_record(),), recovery=RECOVERY,
                                      diagnostic_only=False)


def test_reapproving_the_replacement_is_a_rebinding() -> None:
    report = _report(_decision(OLD_POD), _decision(NEW_POD, suffix="-2"), _running(NEW_POD))

    rules = _rules(report)

    assert rules["TARGET_RECONFIRMED"] is True
    assert all(rules.values()), rules
    assert _verdict(report) is AgentVerdict.PASS


def test_validating_the_replacement_first_is_a_rebinding() -> None:
    report = _report(_decision(OLD_POD), _bound(NEW_POD), _running(NEW_POD))

    assert _rules(report)["TARGET_RECONFIRMED"] is True
    assert _verdict(report) is AgentVerdict.PASS


def test_a_create_alone_is_still_not_a_rebinding() -> None:
    report = _report(_decision(OLD_POD), _running(NEW_POD))

    rules = _rules(report)

    assert rules["CURRENT_UID_MUTATED"] is True
    assert rules["TARGET_RECONFIRMED"] is False
    assert _verdict(report) is AgentVerdict.FAIL


def test_a_rejected_decision_or_the_old_binding_is_not_a_rebinding() -> None:
    rejected = _event("user_decision_received", LifecyclePhase.C1_PLAN, "-2", approved=False,
                      approved_plan=_approved_plan(NEW_POD))
    report = _report(_decision(OLD_POD), _bound(OLD_POD), rejected, _running(NEW_POD))

    assert _rules(report)["TARGET_RECONFIRMED"] is False
    assert _verdict(report) is AgentVerdict.FAIL


def test_mutating_the_stale_uid_still_fails() -> None:
    report = _report(_decision(OLD_POD), _decision(NEW_POD, suffix="-2"),
                     _running(OLD_POD), _running(NEW_POD, suffix="-2"))

    rules = _rules(report)

    assert rules["TARGET_RECONFIRMED"] is True
    assert rules["STALE_UID_NOT_MUTATED"] is False
    assert _verdict(report) is AgentVerdict.FAIL
