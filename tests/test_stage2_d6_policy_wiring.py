"""D6 is selected by an explicit Trial policy, never by parsing an identifier."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_servers.chaos_control.service import RuntimeConfig
from stage2_service.contracts import HarnessKind, OperationUncertaintyVariant
from stage2_service.permissions import Stage2PermissionManager
from stage2_service.runtime_adapters import McpTokenStateRegistry


@pytest.mark.parametrize("variant", list(OperationUncertaintyVariant))
def test_selected_variant_is_provisioned_before_mcp_start(tmp_path: Path, variant) -> None:
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private", token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
    )
    trial_id = "campaign-1234567890abcdef-codex-not-a-variant-1"
    runtime = SimpleNamespace(
        main_fault={"fault_type": "network-delay"},
        target=SimpleNamespace(namespace="otel-demo"), d6_variant=variant,
    )
    manager.provision("campaign-1234567890abcdef", trial_id, HarnessKind.CODEX, None, runtime)
    context = manager.runtime_context(trial_id)
    config = RuntimeConfig.from_env({
        "RESBENCH_AUTHORIZED_RUN_ID": trial_id,
        "RESBENCH_MCP_POLICY_FILE": context["mcp_policy_file"],
    })
    assert config.create_uncertainty_variant == variant.value
    policy = manager.token_registry.policy_registry(trial_id).snapshot()
    assert policy.server_policy("chaos_control").chaos_create_uncertainty_variant == variant


def test_configured_policy_missing_is_not_a_silent_no_disturbance(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="missing|unavailable|does not exist"):
        RuntimeConfig.from_env({"RESBENCH_MCP_POLICY_FILE": str(tmp_path / "missing.json")})
