"""Per-Trial capability provisioning for the four native Harnesses."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from controller.scoped_kubeconfig import create_scoped_kubeconfig
from controller.safety import default_policy

from .contracts import (
    CapabilityProfile,
    HarnessKind,
    KubernetesRule,
    SUPPORTED_STAGE2_FAULT_TYPES,
)
from .runtime_adapters import McpTokenStateRegistry


class PermissionBackend(Protocol):
    def provision_bladeai(self, trial_id: str) -> dict[str, Any]: ...

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]: ...

    def issue_kubeconfig(self, trial_id: str, output_path: Path) -> dict[str, Any]: ...


class NullPermissionBackend:
    def provision_bladeai(self, trial_id: str) -> dict[str, Any]:
        return {"service_account": f"resbench-{trial_id[-24:]}", "provisioned": True}

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        return {"trial_id": trial_id, "verified": True}

    def issue_kubeconfig(self, trial_id: str, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_path.write_text("apiVersion: v1\n", encoding="utf-8")
        output_path.chmod(0o600)
        return {"trial_id": trial_id, "path": str(output_path), "mode": "0600"}


class Stage2PermissionManager:
    MCP_SERVERS = ("k8s_ro", "telemetry_ro", "source_ro", "chaos_control")
    MCP_TOOLS = (
        "k8s_get_resource",
        "k8s_list_resources",
        "k8s_list_events",
        "k8s_pod_logs",
        "telemetry_prom_metric_range",
        "telemetry_jaeger_find_traces",
        "telemetry_loki_logs_range",
        "chaos_validate_plan",
        "chaos_inventory_run",
        "chaos_create_experiment",
        "chaos_get_experiment",
        "chaos_operation_status",
        "chaos_destroy_experiment",
        "chaos_recovery_status",
    )

    def __init__(
        self,
        *,
        private_root: Path,
        token_registry: McpTokenStateRegistry,
        permission_backend: PermissionBackend,
        admin_kubeconfig: Path | None = None,
    ):
        self.private_root = private_root.resolve()
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_registry = token_registry
        self.permission_backend = permission_backend
        self.admin_kubeconfig = admin_kubeconfig
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
        token_paths = self.token_registry.initialize(
            trial_id, {server: token for server in self.MCP_SERVERS}
        )
        permission_runtime: dict[str, Any] = {
            "mcp_token": token,
            "mcp_token_state_files": token_paths,
        }
        # Register cleanup state before any Kubernetes mutation so a partial
        # provisioning failure can still be revoked by the campaign finalizer.
        self._runtime[trial_id] = permission_runtime
        direct_kubeconfig = harness is HarnessKind.BLADEAI
        kubernetes_rules = (
            KubernetesRule(
                api_group="",
                resource="pods",
                verbs=("get", "list"),
                namespace=runtime.target.namespace,
            ),
            KubernetesRule(
                api_group="",
                resource="pods/log",
                verbs=("get",),
                namespace=runtime.target.namespace,
            ),
        )
        if direct_kubeconfig:
            provisioned = self.permission_backend.provision_bladeai(trial_id)
            service_account = str(provisioned["service_account"])
            kubeconfig = self.private_root / trial_id / "bladeai.kubeconfig"
            if self.admin_kubeconfig is not None:
                permission_runtime["bladeai_kubeconfig"] = create_scoped_kubeconfig(
                    admin_kubeconfig=self.admin_kubeconfig,
                    service_account=service_account,
                    output_path=kubeconfig,
                    duration="2h",
                )["path"]
            else:
                permission_runtime["bladeai_kubeconfig"] = self.permission_backend.issue_kubeconfig(
                    trial_id, kubeconfig
                )["path"]
            permission_runtime["bladeai_service_account"] = service_account
            kubernetes_rules = (
                *kubernetes_rules,
                KubernetesRule(
                    api_group="metrics.k8s.io",
                    resource="pods",
                    verbs=("get", "list"),
                    namespace=runtime.target.namespace,
                ),
            )
        del episode
        selected_fault_type = str(runtime.main_fault.get("fault_type") or "")
        allowed_fault_types = (
            SUPPORTED_STAGE2_FAULT_TYPES
            if runtime.main_fault.get("selection_mode") == "agent_strategy"
            else (selected_fault_type,)
        )
        if not all(
            fault_type
            in default_policy({runtime.target.namespace}).fault_type_budgets
            for fault_type in allowed_fault_types
        ):
            raise RuntimeError("runtime fault capability is outside Controller policy")
        return CapabilityProfile(
            harness=harness,
            mcp_servers=self.MCP_SERVERS,
            mcp_tools=self.MCP_TOOLS,
            kubernetes_rules=kubernetes_rules,
            direct_kubeconfig=direct_kubeconfig,
            allowed_fault_types=allowed_fault_types,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )

    def runtime_context(self, trial_id: str) -> dict[str, Any]:
        value = self._runtime.get(trial_id)
        if value is None:
            raise RuntimeError("trial permission runtime is missing")
        return dict(value)

    def restore(self, trial_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(trial_id)
        if runtime is None:
            cleanup = self.permission_backend.cleanup_trial(trial_id)
            token_root = self.token_registry.root / trial_id
            if token_root.is_dir():
                for path in token_root.iterdir():
                    if path.is_file():
                        path.unlink(missing_ok=True)
            return {
                "verified": cleanup.get("verified") is True,
                "already_released": True,
                "backend": cleanup,
            }
        cleanup = self.permission_backend.cleanup_trial(trial_id)
        verified = cleanup.get("verified") is True
        for path in runtime.get("mcp_token_state_files", {}).values():
            Path(path).unlink(missing_ok=True)
        kubeconfig = runtime.get("bladeai_kubeconfig")
        if kubeconfig:
            Path(kubeconfig).unlink(missing_ok=True)
        self._runtime.pop(trial_id, None)
        return {"verified": verified, "backend": cleanup}

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
        if runtime.get("bladeai_service_account"):
            restored.append(self.permission_backend.restore_metrics(trial_id))
        return {
            "verified": all(item.get("verified") is True for item in restored),
            "target_state": "BASELINE",
            "permissions": restored,
        }
