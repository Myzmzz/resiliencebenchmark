"""Concrete runtime adapters used by the single Stage-2 service."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from disturbances.kubernetes_runtime import KubernetesDisturbanceClient

from .contracts import DisturbanceRecord, DisturbanceType


class RuntimeAdapterError(RuntimeError):
    pass


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(self, argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )


class KubernetesEnvironmentGate:
    """Read-only gate; the service never starts or scales the application load generator."""

    def __init__(self, kubeconfig: Path, *, runner: CommandRunner | None = None):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.runner = runner or SubprocessRunner()

    def qualify(self, episode) -> Mapping[str, Any]:
        namespace = episode.public.environment_snapshot.get("namespace", "")
        if namespace != "otel-demo":
            return {"qualified": False, "reason": "fixed Episode namespace is not otel-demo"}
        deployments = self._json(
            ["get", "deployments", "-n", namespace, "-o", "json"]
        )
        items = deployments.get("items") if isinstance(deployments, dict) else None
        if not isinstance(items, list):
            raise RuntimeAdapterError("Kubernetes deployment inventory is invalid")
        desired = sum(int(item.get("spec", {}).get("replicas") or 0) for item in items)
        ready = sum(int(item.get("status", {}).get("readyReplicas") or 0) for item in items)
        load = next(
            (
                item
                for item in items
                if item.get("metadata", {}).get("name") == "load-generator"
            ),
            None,
        )
        load_desired = int(load.get("spec", {}).get("replicas") or 0) if load else 0
        load_ready = int(load.get("status", {}).get("readyReplicas") or 0) if load else 0
        chaos = self._json(
            ["get", "chaosblades.chaosblade.io", "-A", "-o", "json"]
        )
        chaos_items = chaos.get("items") if isinstance(chaos, dict) else None
        if not isinstance(chaos_items, list):
            raise RuntimeAdapterError("ChaosBlade inventory is invalid")
        qualified = (
            load_desired >= 1
            and load_ready >= 1
            and desired == ready
            and len(chaos_items) == 0
        )
        return {
            "qualified": qualified,
            "application_namespace": namespace,
            "deployment_count": len(items),
            "desired_replicas": desired,
            "ready_replicas": ready,
            "built_in_load_generator_desired": load_desired,
            "built_in_load_generator_ready": load_ready,
            "active_chaosblade_count": len(chaos_items),
            "reason": (
                "ready"
                if qualified
                else "application, built-in load generator, or ChaosBlade inventory is not clean"
            ),
        }

    def _json(self, args: list[str]) -> Any:
        if not self.kubeconfig.is_file():
            raise RuntimeAdapterError("configured kubeconfig does not exist")
        completed = self.runner.run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), *args]
        )
        if completed.returncode:
            raise RuntimeAdapterError("Kubernetes read-only qualification command failed")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError("Kubernetes response is not JSON") from exc


class McpTokenStateRegistry:
    """Own per-server active tokens; rotating one file revokes only that MCP server."""

    SERVER_BY_CAPABILITY = {
        "mcp.chaos.create": "chaos_control",
        "mcp.chaos.destroy": "chaos_control",
        "mcp.k8s.read": "k8s_ro",
        "mcp.telemetry.read": "telemetry_ro",
        "mcp.source.read": "source_ro",
    }

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._original: dict[str, str] = {}

    def initialize(self, trial_id: str, tokens: Mapping[str, str]) -> dict[str, str]:
        paths: dict[str, str] = {}
        for server, token in tokens.items():
            _validate_token(token)
            key = f"{trial_id}:{server}"
            self._original[key] = token
            path = self._path(trial_id, server)
            _atomic_token(path, token)
            paths[server] = str(path)
        return paths

    def revoke(self, trial_id: str, capability: str) -> dict[str, Any]:
        server = self.SERVER_BY_CAPABILITY.get(capability)
        if not server:
            raise RuntimeAdapterError(f"no MCP server mapping for capability {capability}")
        path = self._path(trial_id, server)
        if not path.is_file():
            raise RuntimeAdapterError("MCP token state was not initialized")
        _atomic_token(path, secrets.token_urlsafe(48))
        return {"server": server, "capability": capability, "revoked": True}

    def restore(self, trial_id: str, capability: str) -> dict[str, Any]:
        server = self.SERVER_BY_CAPABILITY.get(capability)
        key = f"{trial_id}:{server}"
        token = self._original.get(key)
        if not server or token is None:
            raise RuntimeAdapterError("MCP permission restoration state is missing")
        _atomic_token(self._path(trial_id, server), token)
        return {"server": server, "capability": capability, "verified": True}

    def _path(self, trial_id: str, server: str) -> Path:
        if not trial_id.startswith("campaign-") or server not in {
            "k8s_ro",
            "telemetry_ro",
            "source_ro",
            "chaos_control",
        }:
            raise RuntimeAdapterError("invalid token-state identity")
        directory = self.root / trial_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / f"{server}.token"


class RbacPermissionBackend(Protocol):
    def revoke_metrics(self, trial_id: str) -> dict[str, Any]: ...

    def restore_metrics(self, trial_id: str) -> dict[str, Any]: ...


class TargetCapabilityRebinder(Protocol):
    def rebind(
        self,
        trial_id: str,
        *,
        namespace: str,
        target_name: str,
        target_uid: str,
    ) -> Mapping[str, Any]: ...


class McpTransportController(Protocol):
    def interrupt(self, names: tuple[str, ...]) -> Mapping[str, Any]: ...

    def restore(self, names: tuple[str, ...]) -> Mapping[str, Any]: ...


class CompositeDisturbanceExecutor:
    def __init__(
        self,
        *,
        kubernetes_client: KubernetesDisturbanceClient,
        mcp_tokens: McpTokenStateRegistry,
        rbac_permissions: RbacPermissionBackend | None = None,
        target_rebinder: TargetCapabilityRebinder | None = None,
        mcp_supervisor: McpTransportController | None = None,
        sleeper=time.sleep,
    ):
        self.kubernetes_client = kubernetes_client
        self.mcp_tokens = mcp_tokens
        self.rbac_permissions = rbac_permissions
        self.target_rebinder = target_rebinder
        self.mcp_supervisor = mcp_supervisor
        self.sleeper = sleeper

    def apply(self, plan) -> DisturbanceRecord:
        if plan.type is DisturbanceType.TARGET_CHANGE:
            target = plan.parameters["target"]
            replacement = self.kubernetes_client.restart_exact_pod(
                namespace=str(target["namespace"]),
                name=str(target["name"]),
                expected_uid=str(target["uid"]),
                timeout_seconds=int(plan.parameters["replacement_timeout_seconds"]),
                labels={"resiliencebenchmark.io/disturbance": plan.disturbance_id},
            )
            if self.target_rebinder is None:
                raise RuntimeAdapterError("target capability rebinder is unavailable")
            capability = self.target_rebinder.rebind(
                plan.trial_id,
                namespace=str(target["namespace"]),
                target_name=str(replacement["name"]),
                target_uid=str(replacement["uid"]),
            )
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "old_name": str(target["name"]),
                    "old_uid": str(target["uid"]),
                    "replacement_name": replacement["name"],
                    "replacement_uid": replacement["uid"],
                    "baseline_capability": dict(capability),
                },
            )
        if plan.type is DisturbanceType.PERMISSION_CHANGE:
            capability = str(plan.parameters["revoke_capability"])
            if plan.backend == "mcp_policy":
                evidence = self.mcp_tokens.revoke(plan.trial_id, capability)
            elif plan.backend == "kubernetes_rbac" and capability == "metrics.k8s.io":
                if self.rbac_permissions is None:
                    raise RuntimeAdapterError("Kubernetes RBAC backend is unavailable")
                evidence = self.rbac_permissions.revoke_metrics(plan.trial_id)
            else:
                raise RuntimeAdapterError("unsupported permission disturbance backend")
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence=evidence,
            )
        if plan.type is DisturbanceType.OBSERVABILITY_CHANGE:
            capabilities = tuple(str(item) for item in plan.parameters["revoke_capabilities"])
            evidence = [
                self.mcp_tokens.revoke(plan.trial_id, capability)
                for capability in capabilities
            ]
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "revoked": evidence,
                    "expected_signal": plan.parameters.get("expected_signal"),
                },
            )
        if plan.type in {
            DisturbanceType.TOOL_CHANNEL_INTERRUPTION,
            DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY,
        }:
            if self.mcp_supervisor is None:
                raise RuntimeAdapterError("MCP transport controller is unavailable")
            servers = tuple(str(item) for item in plan.parameters["servers"])
            duration = int(plan.parameters.get("duration_seconds") or 0)
            if not servers or not 1 <= duration <= 10:
                raise RuntimeAdapterError("MCP interruption must be bounded to 1-10 seconds")
            interrupted = dict(self.mcp_supervisor.interrupt(servers))
            if interrupted.get("verified") is not True:
                raise RuntimeAdapterError("MCP interruption was not independently verified")
            self.sleeper(duration)
            restored = dict(self.mcp_supervisor.restore(servers))
            if restored.get("verified") is not True:
                raise RuntimeAdapterError("MCP channel restoration was not verified")
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "servers": servers,
                    "duration_seconds": duration,
                    "interruption": interrupted,
                    "restoration": restored,
                    "verified": True,
                },
                rolled_back=True,
                rollback_evidence={"restored_during_apply": True, **restored},
            )
        raise RuntimeAdapterError("unsupported Stage-2 disturbance type")

    def rollback(self, record: DisturbanceRecord) -> DisturbanceRecord:
        if record.plan.type is DisturbanceType.PERMISSION_CHANGE:
            capability = str(record.plan.parameters["revoke_capability"])
            if record.plan.backend == "mcp_policy":
                evidence = self.mcp_tokens.restore(record.plan.trial_id, capability)
            elif record.plan.backend == "kubernetes_rbac" and capability == "metrics.k8s.io":
                if self.rbac_permissions is None:
                    raise RuntimeAdapterError("Kubernetes RBAC backend is unavailable")
                evidence = self.rbac_permissions.restore_metrics(record.plan.trial_id)
            else:
                raise RuntimeAdapterError("unsupported permission restoration backend")
            return record.model_copy(
                update={"rolled_back": True, "rollback_evidence": evidence}
            )
        if record.plan.type is DisturbanceType.OBSERVABILITY_CHANGE:
            capabilities = tuple(
                str(item) for item in record.plan.parameters["revoke_capabilities"]
            )
            evidence = [
                self.mcp_tokens.restore(record.plan.trial_id, capability)
                for capability in capabilities
            ]
            return record.model_copy(
                update={
                    "rolled_back": True,
                    "rollback_evidence": {"restored": evidence, "verified": True},
                }
            )
        if record.plan.type in {
            DisturbanceType.TOOL_CHANNEL_INTERRUPTION,
            DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY,
        }:
            return record
        return record.model_copy(
            update={
                "rolled_back": False,
                "rollback_evidence": {"deferred_to_environment_reset": True},
            }
        )


def _validate_token(token: str) -> None:
    if len(token) < 32 or any(character.isspace() for character in token):
        raise RuntimeAdapterError("MCP token must be at least 32 non-whitespace characters")


def _atomic_token(path: Path, token: str) -> None:
    _validate_token(token)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
