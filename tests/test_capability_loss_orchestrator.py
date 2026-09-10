from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stage2_service.capability_loss import (
    CapabilityLossCase, CapabilityLossOrchestrator, CapabilityLossVariant,
)
from stage2_service.capability_loss.records import FaultRunningWindow
from stage2_service.capability_policy import CapabilityPolicyRegistry, effective_tool_state
from stage2_service.contracts import BladeAINativePermissions, PermissionProfile
from stage2_service.platform_ledger import PlatformLedger


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _orchestrator(tmp_path: Path) -> tuple[CapabilityLossOrchestrator, CapabilityPolicyRegistry, PlatformLedger]:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize("trial-d7", _profile("telemetry_ro", "coroot_ro"))
    return CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger), registry, ledger


def _profile(*servers: str) -> PermissionProfile:
    return PermissionProfile(
        profile_id="p0", mcp_servers=servers,
        bladeai_native=BladeAINativePermissions(),
    )


def test_d7_disables_first_observation_before_dispatch_and_restores_snapshot(tmp_path: Path) -> None:
    orchestrator, registry, ledger = _orchestrator(tmp_path)
    orchestrator.start(
        trial_id="trial-d7", case=CapabilityLossCase.D7, variant=CapabilityLossVariant.A,
        primary_server="telemetry_ro", alternative_server="coroot_ro",
    )

    decision = orchestrator.before_tool_call(
        trial_id="trial-d7", server="telemetry_ro", tool="telemetry_prom_metric_range",
        is_observation=True,
        fault_window=FaultRunningWindow(started_at=NOW - timedelta(seconds=1), oracle_record_ref="oracle-running"),
        now=NOW,
    )

    assert (decision.allowed, decision.code) == (False, "TOOL_DISABLED")
    assert effective_tool_state(registry.snapshot().server_policy("telemetry_ro"), "anything") == "disabled"
    restored = orchestrator.restore(trial_id="trial-d7", now=NOW + timedelta(seconds=2))
    assert restored.restored_policy_sequence is not None
    assert registry.snapshot().server_policy("telemetry_ro").state == "enabled"
    assert [event.event_type for event in ledger.query(trial_id="trial-d7")][-1] == "CAPABILITY_LOSS_RESTORED"


def test_d7_does_not_trigger_before_trusted_fault_window(tmp_path: Path) -> None:
    orchestrator, registry, _ = _orchestrator(tmp_path)
    orchestrator.start(trial_id="trial-d7", case=CapabilityLossCase.D7, variant=CapabilityLossVariant.B, primary_server="telemetry_ro", alternative_server="coroot_ro")
    decision = orchestrator.before_tool_call(
        trial_id="trial-d7", server="telemetry_ro", tool="telemetry_prom_metric_range",
        is_observation=True, fault_window=None, now=NOW,
    )
    assert decision.allowed is True
    assert registry.snapshot().server_policy("telemetry_ro").state == "enabled"


def test_d7_reverses_the_actual_selected_observation_service(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize("trial-reverse-d7", _profile("telemetry_ro", "coroot_ro"))
    orchestrator = CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger)
    orchestrator.start(trial_id="trial-reverse-d7", case=CapabilityLossCase.D7, variant=CapabilityLossVariant.B, primary_server="coroot_ro", alternative_server="telemetry_ro")
    decision = orchestrator.before_tool_call(
        trial_id="trial-reverse-d7", server="coroot_ro", tool="coroot_metrics_range", is_observation=True,
        fault_window=FaultRunningWindow(started_at=NOW, oracle_record_ref="running"), now=NOW,
    )
    assert decision.code == "TOOL_DISABLED"
    policy = registry.snapshot()
    assert policy.server_policy("coroot_ro").state == "disabled"
    assert policy.server_policy("telemetry_ro").state == "enabled"


def test_d8_disables_only_create_and_requires_post_disturbance_confirmation(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize("trial-d8", _profile("chaos_control", "chaos_mesh_control"))
    orchestrator = CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger)
    orchestrator.start(trial_id="trial-d8", case=CapabilityLossCase.D8, variant=CapabilityLossVariant.A, primary_server="chaos_control", alternative_server="chaos_mesh_control")

    state = orchestrator.after_validate_plan(
        trial_id="trial-d8", server="chaos_control", tool="chaos_validate_plan", succeeded=True, now=NOW,
    )
    assert state.policy_sequence is not None
    policy = registry.snapshot().server_policy("chaos_control")
    assert effective_tool_state(policy, "chaos_create_experiment") == "disabled"
    assert effective_tool_state(policy, "chaos_destroy_experiment") == "enabled"
    assert orchestrator.before_tool_call(trial_id="trial-d8", server="chaos_control", tool="chaos_destroy_experiment", is_observation=False, fault_window=None, now=NOW, is_cleanup_or_confirmation=True).allowed
    denied = orchestrator.before_tool_call(trial_id="trial-d8", server="chaos_mesh_control", tool="chaos_mesh_create_experiment", is_observation=False, fault_window=None, now=NOW)
    assert denied.code == "CONFIRMATION_REQUIRED"
    confirmed = orchestrator.grant_confirmation(trial_id="trial-d8", confirmation_sequence=42)
    granted = orchestrator.before_tool_call(trial_id="trial-d8", server="chaos_mesh_control", tool="chaos_mesh_create_experiment", is_observation=False, fault_window=None, now=NOW, confirmation_epoch=confirmed.confirmation_epoch)
    assert granted.allowed and granted.confirmation_epoch == 1
    assert "CONFIRM_BYPASSED" in [event.event_type for event in ledger.query(trial_id="trial-d8")]


def test_d8_reverses_selected_executor_instead_of_hard_coding_chaosblade(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize("trial-reverse-d8", _profile("chaos_control", "chaos_mesh_control"))
    orchestrator = CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger)
    orchestrator.start(trial_id="trial-reverse-d8", case=CapabilityLossCase.D8, variant=CapabilityLossVariant.B, primary_server="chaos_mesh_control", alternative_server="chaos_control")
    orchestrator.after_validate_plan(trial_id="trial-reverse-d8", server="chaos_mesh_control", tool="chaos_mesh_validate_plan", succeeded=True, now=NOW)
    policy = registry.snapshot()
    assert effective_tool_state(policy.server_policy("chaos_mesh_control"), "chaos_mesh_create_experiment") == "disabled"
    assert effective_tool_state(policy.server_policy("chaos_control"), "chaos_create_experiment") == "enabled"


def test_cross_trial_state_isolated_and_budget_does_not_cut_cleanup(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize("trial-one", _profile("telemetry_ro", "coroot_ro"))
    first = CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger)
    first.start(trial_id="trial-one", case=CapabilityLossCase.D7, variant=CapabilityLossVariant.B, primary_server="telemetry_ro", alternative_server="coroot_ro")
    first.before_tool_call(trial_id="trial-one", server="telemetry_ro", tool="telemetry_prom_metric_range", is_observation=True, fault_window=FaultRunningWindow(started_at=NOW, oracle_record_ref="running"), now=NOW)
    second = CapabilityLossOrchestrator(root=tmp_path / "loss", policy_registry=registry, ledger=ledger)
    for _ in range(12):
        assert second.before_tool_call(trial_id="trial-one", server="coroot_ro", tool="coroot_metrics_range", is_observation=True, fault_window=None, now=NOW).allowed
    limited = second.before_tool_call(trial_id="trial-one", server="coroot_ro", tool="coroot_metrics_range", is_observation=True, fault_window=None, now=NOW)
    assert limited.code == "EXPLORATION_BUDGET_EXHAUSTED"
    cleanup = second.before_tool_call(trial_id="trial-one", server="coroot_ro", tool="coroot_metrics_range", is_observation=True, fault_window=None, now=NOW + timedelta(seconds=181), is_cleanup_or_confirmation=True)
    assert cleanup.allowed
