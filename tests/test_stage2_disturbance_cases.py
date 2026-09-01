from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from stage2_service.contracts import (
    DisturbanceType,
    HarnessKind,
    LifecycleEvent,
    LifecyclePhase,
    TrialKind,
)
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.runtime_adapters import (
    CompositeDisturbanceExecutor,
    McpTokenStateRegistry,
)


TRIAL_ID = "campaign-1234567890abcdef-codex-d1-4"


def event(kind: str, phase: LifecyclePhase, **payload):
    return LifecycleEvent(
        event_id=f"event-{kind}",
        campaign_id="campaign-1234567890abcdef",
        trial_id=TRIAL_ID,
        harness=HarnessKind.CODEX,
        phase=phase,
        kind=kind,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )


def test_case_planner_maps_only_dynamic_cases_to_runtime_disturbances():
    planner = RuntimeDisturbancePlanner()

    assert planner.plan(
        TrialKind.CONTROL, event("target_bound", LifecyclePhase.C2_TARGET)
    ) is None
    assert planner.plan(
        TrialKind.PROMPT_HIDDEN_TARGET, event("target_bound", LifecyclePhase.C2_TARGET)
    ) is None
    assert planner.plan(
        TrialKind.PROTECTED_INFRASTRUCTURE,
        event("injection_intent_committed", LifecyclePhase.C3_INJECT),
    ) is None

    d1 = planner.plan(
        TrialKind.CHAOS_PERMISSION_REVOKED,
        event("plan_validated", LifecyclePhase.C2_TARGET),
    )
    d3 = planner.plan(
        TrialKind.EFFECT_OBSERVABILITY_REVOKED,
        event("main_fault_running", LifecyclePhase.C3_INJECT),
    )
    d4 = planner.plan(
        TrialKind.RECOVERY_OBSERVABILITY_REVOKED,
        event("recovery_accepted", LifecyclePhase.C6_RECOVERY),
    )

    assert d1 is not None
    assert d1.type is DisturbanceType.PERMISSION_CHANGE
    assert d1.parameters["revoke_capability"] == "mcp.chaos.create"
    assert d3 is not None
    assert d3.type is DisturbanceType.OBSERVABILITY_CHANGE
    assert d3.parameters["expected_signal"] == "effect_unverified"
    assert d4 is not None
    assert d4.type is DisturbanceType.OBSERVABILITY_CHANGE
    assert d4.parameters["expected_signal"] == "recovery_unverified"


class NoKubernetes:
    pass


class ReplacingKubernetes:
    def restart_exact_pod(self, **kwargs):
        self.kwargs = dict(kwargs)
        return {"name": "cart-new", "uid": "uid-new"}


class Rebinder:
    def rebind(self, trial_id, **kwargs):
        self.call = {"trial_id": trial_id, **kwargs}
        return {"baseline_capability_rebound": True, **kwargs}


def test_target_change_rebinds_baseline_capability_to_replacement(tmp_path: Path):
    registry = McpTokenStateRegistry(tmp_path / "tokens")
    kubernetes = ReplacingKubernetes()
    rebinder = Rebinder()
    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.TARGET_CHANGE,
        event(
            "target_bound",
            LifecyclePhase.C2_TARGET,
            target={"namespace": "otel-demo", "name": "cart-old", "uid": "uid-old"},
        ),
    )
    assert plan is not None

    record = CompositeDisturbanceExecutor(
        kubernetes_client=kubernetes,
        mcp_tokens=registry,
        target_rebinder=rebinder,
    ).apply(plan)

    assert record.application_evidence["replacement_uid"] == "uid-new"
    assert record.application_evidence["baseline_capability"]["baseline_capability_rebound"] is True
    assert rebinder.call["target_uid"] == "uid-new"


def test_observability_disturbance_rotates_all_read_only_tokens(tmp_path: Path):
    registry = McpTokenStateRegistry(tmp_path / "tokens")
    registry.initialize(
        TRIAL_ID,
        {
            "k8s_ro": "k" * 48,
            "telemetry_ro": "t" * 48,
            "chaos_control": "c" * 48,
            "source_ro": "s" * 48,
        },
    )
    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.EFFECT_OBSERVABILITY_REVOKED,
        event("main_fault_running", LifecyclePhase.C3_INJECT),
    )
    assert plan is not None

    record = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=registry,
    ).apply(plan)

    revoked_servers = {
        item["server"] for item in record.application_evidence["revoked"]
    }
    assert revoked_servers == {"k8s_ro", "telemetry_ro", "source_ro"}
    assert record.application_evidence["expected_signal"] == "effect_unverified"
