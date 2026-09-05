"""Per-Trial capability provisioning for the four native Harnesses."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from controller.scoped_kubeconfig import create_scoped_kubeconfig

from .contracts import (
    BladeAINativePermissions,
    CapabilityProfile,
    HarnessKind,
    KubernetesRule,
    McpServerName,
    PermissionProfile,
)
from .runtime_adapters import McpTokenStateRegistry


class PermissionBackend(Protocol):
    def provision_bladeai(
        self, trial_id: str, permissions: BladeAINativePermissions
    ) -> dict[str, Any]: ...

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]: ...

    def issue_kubeconfig(self, trial_id: str, output_path: Path) -> dict[str, Any]: ...


class NullPermissionBackend:
    def provision_bladeai(
        self, trial_id: str, permissions: BladeAINativePermissions
    ) -> dict[str, Any]:
        return {
            "service_account": f"resbench-{trial_id[-24:]}",
            "permissions": permissions.model_dump(mode="json"),
            "provisioned": True,
        }

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        return {"trial_id": trial_id, "verified": True}

    def issue_kubeconfig(self, trial_id: str, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_path.write_text("apiVersion: v1\n", encoding="utf-8")
        output_path.chmod(0o600)
        return {"trial_id": trial_id, "path": str(output_path), "mode": "0600"}


class Stage2PermissionManager:
    MCP_TOOLS_BY_SERVER = {
        McpServerName.K8S_RO: (
            "k8s_get_resource",
            "k8s_list_resources",
            "k8s_list_events",
            "k8s_pod_logs",
            "k8s_cluster_inventory",
        ),
        McpServerName.TELEMETRY_RO: (
            "telemetry_prom_metric_instant",
            "telemetry_prom_metric_range",
            "telemetry_prom_metric_series",
            "telemetry_prom_list_labels",
            "telemetry_jaeger_list_services",
            "telemetry_jaeger_list_operations",
            "telemetry_jaeger_find_traces",
            "telemetry_loki_list_labels",
            "telemetry_loki_logs",
            "telemetry_loki_logs_range",
        ),
        McpServerName.SOURCE_RO: (
            "source_list_repositories",
            "source_list_files",
            "source_search_text",
            "source_read_file",
            "source_show_commit",
        ),
        McpServerName.CHAOS_CONTROL: (
            "chaos_validate_plan",
            "chaos_inventory_run",
            "chaos_create_experiment",
            "chaos_get_experiment",
            "chaos_destroy_experiment",
            "chaos_recovery_status",
        ),
    }

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
        self,
        campaign_id,
        trial_id,
        harness,
        episode,
        runtime,
        permission_profile: PermissionProfile,
    ) -> CapabilityProfile:
        del campaign_id
        del runtime
        # BladeAI keeps its native ChaosBlade execution channel. Its current
        # target guard cannot safely classify dynamic mutating MCP tools, so
        # the shared chaos_control server is intentionally not placed in the
        # BladeAI tool graph during the static-profile phase.
        enabled_servers = tuple(
            server
            for server in permission_profile.mcp_servers
            if not (
                harness is HarnessKind.BLADEAI
                and server is McpServerName.CHAOS_CONTROL
            )
        )
        token = secrets.token_urlsafe(48)
        token_paths = self.token_registry.initialize(
            trial_id, {server.value: token for server in enabled_servers}
        )
        runtime: dict[str, Any] = {
            "mcp_token": token,
            "mcp_token_state_files": token_paths,
            "permission_profile": permission_profile.model_dump(mode="json"),
        }
        # Register cleanup state before any Kubernetes mutation so a partial
        # provisioning failure can still be revoked by the campaign finalizer.
        self._runtime[trial_id] = runtime
        direct_kubeconfig = harness is HarnessKind.BLADEAI
        native_permissions = (
            permission_profile.bladeai_native
            if direct_kubeconfig
            else BladeAINativePermissions(
                kubernetes_read=False,
                kubernetes_metrics=False,
                chaosblade_execute=False,
            )
        )
        kubernetes_rules: tuple[KubernetesRule, ...] = ()
        if direct_kubeconfig:
            provisioned = self.permission_backend.provision_bladeai(
                trial_id, native_permissions
            )
            service_account = str(provisioned["service_account"])
            kubeconfig = self.private_root / trial_id / "bladeai.kubeconfig"
            if self.admin_kubeconfig is not None:
                runtime["bladeai_kubeconfig"] = create_scoped_kubeconfig(
                    admin_kubeconfig=self.admin_kubeconfig,
                    service_account=service_account,
                    output_path=kubeconfig,
                    duration="2h",
                )["path"]
            else:
                runtime["bladeai_kubeconfig"] = self.permission_backend.issue_kubeconfig(
                    trial_id, kubeconfig
                )["path"]
            runtime["bladeai_service_account"] = service_account
            rules: list[KubernetesRule] = []
            if native_permissions.kubernetes_read:
                rules.extend(
                    [
                        KubernetesRule(
                            api_group="",
                            resource=resource,
                            verbs=("get", "list"),
                            namespace="otel-demo",
                        )
                        for resource in ("pods", "services", "endpoints", "events")
                    ]
                )
                rules.extend(
                    (
                        KubernetesRule(
                            api_group="",
                            resource="pods/log",
                            verbs=("get",),
                            namespace="otel-demo",
                        ),
                        KubernetesRule(
                            api_group="apps",
                            resource="deployments",
                            verbs=("get", "list"),
                            namespace="otel-demo",
                        ),
                        KubernetesRule(
                            api_group="apps",
                            resource="replicasets",
                            verbs=("get", "list"),
                            namespace="otel-demo",
                        ),
                    )
                )
            if native_permissions.kubernetes_metrics:
                rules.append(
                    KubernetesRule(
                        api_group="metrics.k8s.io",
                        resource="pods",
                        verbs=("get", "list"),
                        namespace="otel-demo",
                    )
                )
            if native_permissions.chaosblade_execute:
                rules.append(
                    KubernetesRule(
                        api_group="chaosblade.io",
                        resource="chaosblades",
                        verbs=("get", "list", "create", "delete"),
                    )
                )
            kubernetes_rules = tuple(rules)
        enabled_tools = tuple(
            tool
            for server in enabled_servers
            for tool in self.MCP_TOOLS_BY_SERVER[server]
        )
        return CapabilityProfile(
            permission_profile_id=permission_profile.profile_id,
            harness=harness,
            mcp_servers=enabled_servers,
            mcp_tools=enabled_tools,
            kubernetes_rules=kubernetes_rules,
            direct_kubeconfig=direct_kubeconfig,
            native_permissions=native_permissions,
            allowed_fault_types=(episode.internal.main_fault.fault_type,),
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
            return {"verified": False, "reason": "permission runtime is missing"}
        try:
            cleanup = (
                self.permission_backend.cleanup_trial(trial_id)
                if runtime.get("bladeai_service_account")
                else {
                    "trial_id": trial_id,
                    "verified": True,
                    "not_applicable": True,
                }
            )
        except Exception as exc:  # Cleanup must continue across independent layers.
            cleanup = {
                "trial_id": trial_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        try:
            token_cleanup = self.token_registry.cleanup(trial_id)
        except Exception as exc:  # Keep kubeconfig cleanup reachable.
            token_cleanup = {
                "trial_id": trial_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        kubeconfig = runtime.get("bladeai_kubeconfig")
        kubeconfig_cleanup: dict[str, Any] = {
            "path_present": bool(kubeconfig),
            "verified": True,
        }
        if kubeconfig:
            try:
                Path(kubeconfig).unlink(missing_ok=True)
                kubeconfig_cleanup["verified"] = not Path(kubeconfig).exists()
            except Exception as exc:
                kubeconfig_cleanup = {
                    "path_present": True,
                    "verified": False,
                    "error_type": type(exc).__name__,
                }
        self._runtime.pop(trial_id, None)
        return {
            "verified": (
                cleanup.get("verified") is True
                and token_cleanup.get("verified") is True
                and kubeconfig_cleanup.get("verified") is True
            ),
            "backend": cleanup,
            "mcp_tokens": token_cleanup,
            "kubeconfig": kubeconfig_cleanup,
        }
