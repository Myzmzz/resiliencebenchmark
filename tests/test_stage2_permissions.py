from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stage2_service.contracts import HarnessKind
from stage2_service.capability_policy import read_policy_file
from stage2_service.permissions import Stage2PermissionManager
from stage2_service.runtime_adapters import McpTokenStateRegistry


class MainFault:
    fault_type = "network-delay"


class Internal:
    main_fault = MainFault()


class Episode:
    internal = Internal()


class Runtime:
    main_fault = {"fault_type": "network-delay"}
    target = type("Target", (), {"namespace": "otel-demo"})()


class SubstitutionRuntime(Runtime):
    tool_substitution_variant = "A"


def test_permission_manager_gives_all_harnesses_one_mcp_only_profile(tmp_path: Path):
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
    )
    campaign = "campaign-1234567890abcdef"
    codex_trial = f"{campaign}-codex-t1"
    blade_trial = f"{campaign}-bladeai-t1"

    codex = manager.provision(
        campaign, codex_trial, HarnessKind.CODEX, Episode(), Runtime()
    )
    blade = manager.provision(
        campaign, blade_trial, HarnessKind.BLADEAI, Episode(), Runtime()
    )

    assert codex.direct_kubeconfig is False
    assert blade.direct_kubeconfig is False
    assert codex.kubernetes_rules == blade.kubernetes_rules == ()
    assert codex.mcp_servers == blade.mcp_servers
    assert codex.mcp_tools == blade.mcp_tools
    # Every Agent's permission lasts 30 days (user decision 2026-09-10), so no
    # Trial can outlive it; the Trial's own time cap still ends every run.
    for profile in (codex, blade):
        assert profile.expires_at - datetime.now(UTC) > timedelta(days=29)
    runtime_context = manager.runtime_context(codex_trial)
    assert runtime_context["mcp_token"]
    assert runtime_context["mcp_policy_file"]
    assert (
        runtime_context["mcp_token_state_files"][McpTokenStateRegistry.POLICY_FILE_STATE_KEY]
        == runtime_context["mcp_policy_file"]
    )
    policy = read_policy_file(Path(runtime_context["mcp_policy_file"]))
    assert set(policy.servers) == {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "coroot_ro"}
    # Coroot is a base backup source for every Trial (2026-09-10).
    assert "coroot_ro" in codex.mcp_servers and "coroot_metrics_range" in codex.mcp_tools
    assert all(server.state == "enabled" for server in policy.servers.values())
    native = manager._default_permission_profile().bladeai_native
    assert native.kubernetes_read is False
    assert native.kubernetes_metrics is False
    assert native.chaosblade_execute is False
    blade_runtime = manager.runtime_context(blade_trial)
    assert not any(key.startswith("bladeai_") for key in blade_runtime)
    assert manager.restore(codex_trial)["verified"] is True
    assert manager.restore(blade_trial)["verified"] is True


def test_permission_manager_baseline_restore_restores_mcp_policy(tmp_path: Path):
    token_registry = McpTokenStateRegistry(tmp_path / "tokens")
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=token_registry,
    )
    trial_id = "campaign-1234567890abcdef-codex-t1"
    manager.provision(
        "campaign-1234567890abcdef",
        trial_id,
        HarnessKind.CODEX,
        Episode(),
        Runtime(),
    )
    registry = token_registry.policy_registry(trial_id)
    registry.set_server("telemetry_ro", state="disabled")

    restored = manager.restore_baseline(trial_id)

    assert restored["verified"] is True
    assert registry.snapshot().server_policy("telemetry_ro").state == "enabled"


def test_substitution_runtime_provisions_only_the_three_optional_servers(tmp_path: Path):
    manager = Stage2PermissionManager(private_root=tmp_path / "private", token_registry=McpTokenStateRegistry(tmp_path / "tokens"))
    trial_id = "campaign-1234567890abcdef-codex-d7-1"
    profile = manager.provision("campaign-1234567890abcdef", trial_id, HarnessKind.CODEX, Episode(), SubstitutionRuntime())
    runtime = manager.runtime_context(trial_id)
    assert set(profile.mcp_servers) == {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro", "chaos_mesh_control", "code_sandbox"}
    assert {"coroot_ro", "chaos_mesh_control", "code_sandbox"} <= set(runtime["mcp_token_files"])
    assert {"coroot_ro", "chaos_mesh_control", "code_sandbox"} <= set(read_policy_file(Path(runtime["mcp_policy_file"])).servers)


def test_restore_removes_only_trial_token_and_policy_files(tmp_path: Path):
    token_registry = McpTokenStateRegistry(tmp_path / "tokens")
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private", token_registry=token_registry
    )
    trial_id = "campaign-1234567890abcdef-claude-code-t1"
    manager.provision("campaign-1234567890abcdef", trial_id, HarnessKind.CLAUDE_CODE, Episode(), Runtime())
    context = manager.runtime_context(trial_id)
    token_files = tuple(Path(path) for path in context["mcp_token_files"].values())
    policy_root = Path(context["mcp_policy_root"])

    restored = manager.restore(trial_id)

    assert restored == {"verified": True, "cleanup": "trial_tokens_and_policy_revoked"}
    assert all(not path.exists() for path in token_files)
    assert not policy_root.exists()
