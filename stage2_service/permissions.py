"""Per-Trial capability provisioning for the four native Harnesses."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from controller.safety import default_policy

from .capability_policy import CapabilityPolicyDocument, CapabilityPolicyRegistry
from .contracts import (
    BladeAINativePermissions,
    CapabilityProfile,
    HarnessKind,
    PermissionProfile,
    SUPPORTED_STAGE2_FAULT_TYPES,
)
from .runtime_adapters import McpTokenStateRegistry


class Stage2PermissionManager:
    # Coroot is registered for every Trial as a backup observation source
    # (2026-09-10); D7/D8 add only the remaining substitution tools.
    MCP_SERVERS = ("k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro")
    OPTIONAL_SUBSTITUTION_SERVERS = ("chaos_mesh_control", "code_sandbox")
    MCP_TOOLS = (
        "harness_consult", "harness_confirm", "harness_submit_result", "harness_poll_notices",
        "k8s_get_resource",
        "k8s_list_resources",
        "k8s_list_events",
        "k8s_pod_logs",
        "telemetry_prom_metric_range",
        "telemetry_workload_current",
        "telemetry_jaeger_find_traces",
        "telemetry_loki_logs_range",
        "chaos_validate_plan",
        "chaos_inventory_run",
        "chaos_create_experiment",
        "chaos_get_experiment",
        "chaos_operation_status",
        "chaos_destroy_experiment",
        "chaos_recovery_status",
        "coroot_metrics_range", "coroot_traces_find", "coroot_logs_range",
    )
    OPTIONAL_SUBSTITUTION_TOOLS = (
        "chaos_mesh_validate_plan", "chaos_mesh_inventory_run", "chaos_mesh_create_experiment",
        "chaos_mesh_get_experiment", "chaos_mesh_operation_status", "chaos_mesh_destroy_experiment",
        "chaos_mesh_recovery_status", "run_python",
    )

    def __init__(
        self,
        *,
        private_root: Path,
        token_registry: McpTokenStateRegistry,
    ):
        self.private_root = private_root.resolve()
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_registry = token_registry
        self.platform_ledger = token_registry.platform_ledger
        self._runtime: dict[str, dict[str, Any]] = {}

    def provision(
        self, campaign_id, trial_id, harness, episode, runtime
    ) -> CapabilityProfile:
        try:
            return self._provision(
                campaign_id, trial_id, harness, episode, runtime
            )
        except Exception:
            try:
                self.restore(trial_id)
            except Exception:
                pass
            raise

    def _provision(
        self, campaign_id, trial_id, harness, episode, runtime
    ) -> CapabilityProfile:
        del campaign_id
        token = secrets.token_urlsafe(48)
        channel_token = secrets.token_urlsafe(48)
        optional = bool(getattr(runtime, "tool_substitution_variant", None))
        servers = self.MCP_SERVERS + (self.OPTIONAL_SUBSTITUTION_SERVERS if optional else ())
        token_paths = self.token_registry.initialize(
            trial_id, {server: channel_token if server == "harness_channel" else token
                       for server in servers}
        )
        policy_registry = CapabilityPolicyRegistry(
            self.private_root / trial_id / "mcp-policy", ledger=self.platform_ledger,
        )
        policy_document = policy_registry.initialize(
            trial_id,
            self._default_permission_profile(servers),
            source="permission-provision",
        )
        self.token_registry.register_policy_root(trial_id, policy_registry.root)
        d6_variant = getattr(runtime, "d6_variant", None)
        if d6_variant is not None:
            policy_document = policy_registry.set_server(
                "chaos_control", chaos_create_uncertainty_variant=d6_variant,
                source="controller", reason="explicit D6 Trial variant",
            )
        supervisor_token_state_files = {
            **token_paths,
            McpTokenStateRegistry.POLICY_FILE_STATE_KEY: str(
                policy_registry.policy_path
            ),
            McpTokenStateRegistry.POLICY_ROOT_STATE_KEY: str(policy_registry.root),
        }
        permission_runtime: dict[str, Any] = {
            "platform_ledger_root": str(self.platform_ledger.root),
            "mcp_token": token,
            "harness_channel_token": channel_token,
            "mcp_token_state_files": supervisor_token_state_files,
            "mcp_token_files": token_paths,
            "mcp_policy_file": str(policy_registry.policy_path),
            "mcp_policy_root": str(policy_registry.root),
            "mcp_policy_baseline": policy_document.model_dump(mode="json"),
            "tool_substitution_enabled": optional,
        }
        # Register cleanup state before any Kubernetes mutation so a partial
        # provisioning failure can still be revoked by the campaign finalizer.
        self._runtime[trial_id] = permission_runtime
        del episode
        selected_fault_type = str(runtime.main_fault.get("fault_type") or "")
        allowed_fault_types = (
            SUPPORTED_STAGE2_FAULT_TYPES
            if runtime.main_fault.get("selection_mode") == "agent_strategy"
            else (selected_fault_type,)
        )
        if not all(
            fault_type
            in default_policy({runtime.target.namespace}).fault_type_contracts
            for fault_type in allowed_fault_types
        ):
            raise RuntimeError("runtime fault capability is outside Controller policy")
        return CapabilityProfile(
            harness=harness,
            mcp_servers=servers,
            mcp_tools=self.MCP_TOOLS + (self.OPTIONAL_SUBSTITUTION_TOOLS if optional else ()),
            kubernetes_rules=(),
            direct_kubeconfig=False,
            allowed_fault_types=allowed_fault_types,
            # Same 30-day lifetime as the baseline capability (user decision
            # 2026-09-10); the Trial's own time cap still ends every run.
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )

    def _default_permission_profile(self, servers: tuple[str, ...] | None = None) -> PermissionProfile:
        servers = servers or self.MCP_SERVERS
        return PermissionProfile(
            profile_id="p0-full-authorized",
            mcp_servers=tuple(name for name in servers if name != "harness_channel"),
            bladeai_native=BladeAINativePermissions(
                kubernetes_read=False,
                kubernetes_metrics=False,
                chaosblade_execute=False,
            ),
        )

    def runtime_context(self, trial_id: str) -> dict[str, Any]:
        value = self._runtime.get(trial_id)
        if value is None:
            raise RuntimeError("trial permission runtime is missing")
        return dict(value)

    def restore(self, trial_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(trial_id)
        if runtime is None:
            self._remove_token_files(trial_id)
            return {
                "verified": True,
                "already_released": True,
                "cleanup": "trial_tokens_and_policy_absent",
            }
        for path in runtime.get("mcp_token_files", {}).values():
            Path(path).unlink(missing_ok=True)
        self._remove_token_files(trial_id)
        policy_root = runtime.get("mcp_policy_root")
        if policy_root:
            _remove_private_directory(Path(policy_root))
        self._runtime.pop(trial_id, None)
        return {
            "verified": True,
            "cleanup": "trial_tokens_and_policy_revoked",
        }

    def restore_baseline(self, trial_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(trial_id)
        if runtime is None:
            return {
                "verified": False,
                "reason": "active Trial permission runtime is missing",
            }
        capabilities = (
            "mcp.k8s.read",
            "mcp.telemetry.read",
            "mcp.source.read",
            "mcp.chaos.create",
        )
        restored = []
        for capability in capabilities:
            restored.append(self.token_registry.restore(trial_id, capability))
        if runtime.get("mcp_policy_root") and runtime.get("mcp_policy_baseline"):
            registry = CapabilityPolicyRegistry(Path(runtime["mcp_policy_root"]))
            restored_policy = registry.restore(
                CapabilityPolicyDocument(**runtime["mcp_policy_baseline"]),
                source="permission-baseline-restore",
            )
            restored.append(
                {
                    "server": "mcp_policy",
                    "verified": True,
                    "sequence": restored_policy.sequence,
                }
            )
        return {
            "verified": all(item.get("verified") is True for item in restored),
            "target_state": "BASELINE",
            "permissions": restored,
        }

    def _remove_token_files(self, trial_id: str) -> None:
        token_root = self.token_registry.root / trial_id
        if not token_root.is_dir():
            return
        for path in token_root.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        try:
            token_root.rmdir()
        except OSError:
            pass


def _remove_private_directory(path: Path) -> None:
    """Delete only manager-created 0600 policy files in one Trial directory."""
    if not path.is_dir() or path.is_symlink():
        return
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
    try:
        path.rmdir()
    except OSError:
        pass
