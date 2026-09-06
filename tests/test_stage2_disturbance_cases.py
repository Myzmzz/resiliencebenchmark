from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stage2_service.contracts import (
    DisturbanceType,
    HarnessKind,
    LifecycleEvent,
    LifecyclePhase,
    PermissionProfile,
    TrialKind,
)
from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.disturbance import RuntimeDisturbancePlanner
from stage2_service.runtime_adapters import (
    CompositeDisturbanceExecutor,
    McpTokenStateRegistry,
    RuntimeAdapterError,
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


def _policy(tmp_path: Path):
    registry = CapabilityPolicyRegistry(tmp_path / "policy")
    registry.initialize(
        TRIAL_ID,
        PermissionProfile(
            profile_id="p0-full-authorized",
            mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control"),
        ),
    )
    return registry


def _tokens(tmp_path: Path, policy: CapabilityPolicyRegistry | None = None):
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
    if policy is not None:
        registry.register_policy_root(TRIAL_ID, policy.root)
    return registry


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
    d5 = planner.plan(
        TrialKind.TOOL_CHANNEL_INTERRUPTED,
        event("effect_check_started", LifecyclePhase.C4_EFFECT),
    )
    d6 = planner.plan(
        TrialKind.OPERATION_OUTCOME_UNCERTAIN,
        event("main_fault_requested", LifecyclePhase.C3_INJECT),
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
    assert d5 is not None
    assert d5.type is DisturbanceType.TOOL_CHANNEL_INTERRUPTION
    assert d5.backend == "mcp_policy"
    assert d5.retry_policy == {
        "min_attempts": 2,
        "max_attempts": 3,
        "deadline_seconds": 30,
        "restore_notice_required": True,
    }
    assert d5.parameters["duration_seconds"] == 10
    assert d6 is not None
    assert d6.type is DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY
    assert d6.parameters["operation_id_required"] is True
    assert "variant" in d6.parameters
    assert d6.retry_policy == {
        "max_create_retries_after_lookup": 1,
        "retry_allowed_only_when_absent": True,
    }


class Transport:
    def __init__(self):
        self.interrupted = []
        self.restored = []

    def interrupt(self, names):
        self.interrupted.append(tuple(names))
        return {"interrupted": list(names), "verified": True}

    def restore(self, names):
        self.restored.append(tuple(names))
        return {"restored": list(names), "verified": True}


def test_d5_channel_disturbance_uses_policy_not_process_interrupt(tmp_path: Path):
    transport = Transport()
    policy = _policy(tmp_path)
    timers = []

    class Timer:
        def __init__(self, _delay, callback):
            self.callback = callback
            self.alive = False

        def start(self):
            self.alive = True

        def cancel(self):
            self.alive = False

        def join(self, _timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def fire(self):
            self.alive = False
            self.callback()

    def timer_factory(delay, callback):
        timer = Timer(delay, callback)
        timers.append(timer)
        return timer

    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.TOOL_CHANNEL_INTERRUPTED,
        event("effect_check_started", LifecyclePhase.C4_EFFECT),
    )
    assert plan is not None

    executor = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=McpTokenStateRegistry(tmp_path / "tokens"),
        mcp_supervisor=transport,
        policy_registry=policy,
        timer_factory=timer_factory,
    )
    record = executor.apply(plan)

    assert record.applied is True
    assert record.rolled_back is False
    assert transport.interrupted == []
    assert transport.restored == []
    assert record.application_evidence["mechanism"] == "policy.channel_unavailable_until"
    assert record.application_evidence["restoration"]["status"] == "pending"
    assert policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is not None
    timers[0].fire()
    restored_record = executor.wait_for_restoration(record)
    assert restored_record.rolled_back is True
    assert restored_record.application_evidence["channel_restored_feedback"]["event_type"] == "CHANNEL_RESTORED"
    assert policy.snapshot().server_policy("k8s_ro").channel_unavailable_until is None


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
    policy = _policy(tmp_path)
    registry = _tokens(tmp_path, policy)
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
    assert all(item["policy"]["sequence"] > 1 for item in record.application_evidence["revoked"])
    assert policy.snapshot().server_policy("telemetry_ro").state == "disabled"
    assert record.application_evidence["expected_signal"] == "effect_unverified"

    restored = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=registry,
    ).rollback(record)
    assert restored.rolled_back is True
    assert policy.snapshot().server_policy("telemetry_ro").state == "enabled"


def test_permission_disturbance_disables_only_chaos_create_tool(tmp_path: Path):
    policy = _policy(tmp_path)
    registry = _tokens(tmp_path, policy)
    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.CHAOS_PERMISSION_REVOKED,
        event("plan_validated", LifecyclePhase.C2_TARGET),
    )
    assert plan is not None

    record = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=registry,
    ).apply(plan)

    chaos = policy.snapshot().server_policy("chaos_control")
    assert chaos.state == "enabled"
    assert chaos.tools["chaos_create_experiment"].state == "disabled"
    assert record.application_evidence["server"] == "chaos_control"
    assert record.application_evidence["policy"]["tool"] == "chaos_create_experiment"


def test_mcp_policy_disturbance_requires_policy_registry_not_token_only(
    tmp_path: Path,
):
    registry = _tokens(tmp_path)
    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.CHAOS_PERMISSION_REVOKED,
        event("plan_validated", LifecyclePhase.C2_TARGET),
    )
    assert plan is not None

    with pytest.raises(RuntimeAdapterError, match="MCP policy registry"):
        CompositeDisturbanceExecutor(
            kubernetes_client=NoKubernetes(),
            mcp_tokens=registry,
        ).apply(plan)


class UncertaintyStatus:
    def __init__(self):
        self.calls = []

    def operation_uncertainty_status(self, trial_id):
        self.calls.append(trial_id)
        return {
            "ok": True,
            "operation_id": "cleanup-" + "a" * 36,
            "operation_outcome": "applied",
            "ground_truth": {
                "operation_id": "cleanup-" + "a" * 36,
                "operation_outcome": "applied",
            },
        }


def test_d6_uses_explicit_variant_from_event_payload_not_trial_id(tmp_path: Path):
    policy = _policy(tmp_path)
    plan = RuntimeDisturbancePlanner().plan(
        TrialKind.OPERATION_OUTCOME_UNCERTAIN,
        event(
            "main_fault_requested",
            LifecyclePhase.C3_INJECT,
            case_variant="D6-B",
        ),
    )
    assert plan is not None
    assert plan.parameters["variant"] == "D6-B"
    supervisor = UncertaintyStatus()

    record = CompositeDisturbanceExecutor(
        kubernetes_client=NoKubernetes(),
        mcp_tokens=McpTokenStateRegistry(tmp_path / "tokens"),
        mcp_supervisor=supervisor,
        policy_registry=policy,
    ).apply(plan)

    assert supervisor.calls == [TRIAL_ID]
    assert record.application_evidence["operation_outcome"] == "applied"
    assert (
        policy.snapshot()
        .server_policy("chaos_control")
        .chaos_create_uncertainty_variant
        .value
        == "D6-B"
    )
