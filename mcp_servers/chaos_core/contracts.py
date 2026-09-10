"""Shared contracts for controlled chaos executors."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Protocol

from controller.safety import ChaosBladeAction
from stage2_service.capability_policy import CapabilityPolicyError, read_policy_file


OWNER_LABEL = "benchmark.owner"
OWNER_VALUE = "chaos_control"
RUN_ID_LABEL = "benchmark.run_id"
TARGET_UID_LABEL = "benchmark.target_uid"
NAMESPACE_LABEL = "benchmark.namespace"
LOGICAL_NAMESPACE_LABEL = NAMESPACE_LABEL
FAULT_TYPE_LABEL = "benchmark.fault_type"
LEDGER_VERSION = 1
TERMINAL_PHASES = {"absence", "absent", "destroyed", "deleted", "finished", "completed", "succeeded", "success"}
HANDLE_RE = re.compile(r"^cleanup-[a-z0-9][a-z0-9._-]{6,120}$")
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{2,255}$")


class ChaosControlError(RuntimeError):
    """Expected operational error that is safe to return to an agent."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_step: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step
        self.details = dict(details or {})

    def as_response(self) -> dict[str, Any]:
        response = {"ok": False, "error": {"code": self.code, "message": self.message, "next_step": self.next_step}}
        if self.details:
            response["error"]["details"] = self.details
        return response


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime gates for destructive chaos operations."""

    execute_enabled: bool = False
    kubeconfig: str | None = None
    cleanup_kubeconfig: str | None = None
    namespace_allowlist: frozenset[str] = frozenset()
    controller_token_ref: str | None = None
    controller_pod_uid: str | None = None
    controller_pod_namespace: str | None = None
    controller_pod_name: str | None = None
    controller_lease_file: Path | None = None
    authorized_run_id: str | None = None
    baseline_gate_token: str | None = None
    cleanup_handle: str | None = None
    allowed_fault_types: frozenset[str] = frozenset()
    expected_fault: Mapping[str, Any] | None = None
    decision_policy: str = "clarify_missing"
    user_decision_file: Path | None = None
    ledger_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "resbench-chaos-control-ledger")
    baseline_ledger_dir: Path | None = None
    kubectl_path: str = "kubectl"
    create_uncertainty_variant: str | None = None
    condition_safety_ttl_seconds: int | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        server_name: str = "chaos_control",
    ) -> "RuntimeConfig":
        """Read the shared runtime gates for one named controlled executor."""
        values = os.environ if env is None else env
        uncertainty_variant = values.get("RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT")
        if policy_path := values.get("RESBENCH_MCP_POLICY_FILE"):
            document = read_policy_file(Path(policy_path))
            policy = document.server_policy(server_name)
            if policy is None:
                raise CapabilityPolicyError(f"{server_name} policy is unavailable")
            if values.get("RESBENCH_AUTHORIZED_RUN_ID") not in (None, document.trial_id):
                raise CapabilityPolicyError(f"{server_name} policy Trial identity does not match")
            uncertainty_variant = (
                policy.chaos_create_uncertainty_variant.value
                if policy.chaos_create_uncertainty_variant is not None else None
            )
        namespaces = frozenset(
            item.strip()
            for item in values.get("RESBENCH_CHAOS_NAMESPACE_ALLOWLIST", "").split(",")
            if item.strip()
        )
        allowed_fault_types = frozenset(
            item.strip()
            for item in values.get(
                "RESBENCH_CHAOS_ALLOWED_FAULT_TYPES", ""
            ).split(",")
            if item.strip()
        )
        ledger_raw = values.get("RESBENCH_CHAOS_LEDGER_DIR")
        baseline_raw = values.get("RESBENCH_CHAOS_BASELINE_LEDGER_DIR")
        controller_lease_raw = values.get("RESBENCH_CHAOS_CONTROLLER_LEASE_FILE")
        user_decision_raw = values.get("RESBENCH_USER_DECISION_FILE")
        expected_fault_raw = values.get("RESBENCH_CHAOS_EXPECTED_FAULT_JSON", "")
        expected_fault: Mapping[str, Any] | None = None
        if expected_fault_raw:
            try:
                parsed_expected_fault = json.loads(expected_fault_raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "RESBENCH_CHAOS_EXPECTED_FAULT_JSON is not valid JSON"
                ) from exc
            if not isinstance(parsed_expected_fault, Mapping):
                raise ValueError(
                    "RESBENCH_CHAOS_EXPECTED_FAULT_JSON must be an object"
                )
            expected_fault = dict(parsed_expected_fault)
        condition_ttl_raw = values.get(
            "RESBENCH_CHAOS_CONDITION_SAFETY_TTL_SECONDS", ""
        ).strip()
        condition_ttl = int(condition_ttl_raw) if condition_ttl_raw else None
        if condition_ttl is not None and condition_ttl < 1:
            raise ValueError(
                "RESBENCH_CHAOS_CONDITION_SAFETY_TTL_SECONDS must be positive"
            )
        return cls(
            execute_enabled=values.get("RESBENCH_CHAOS_EXECUTE_ENABLED", "").lower() == "true",
            kubeconfig=values.get("RESBENCH_CHAOS_KUBECONFIG"),
            cleanup_kubeconfig=values.get("RESBENCH_CHAOS_CLEANUP_KUBECONFIG"),
            namespace_allowlist=namespaces,
            controller_token_ref=values.get("RESBENCH_CHAOS_CONTROLLER_TOKEN_REF"),
            controller_pod_uid=values.get("RESBENCH_CHAOS_CONTROLLER_POD_UID"),
            controller_pod_namespace=values.get("RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE"),
            controller_pod_name=values.get("RESBENCH_CHAOS_CONTROLLER_POD_NAME"),
            authorized_run_id=values.get("RESBENCH_AUTHORIZED_RUN_ID"),
            baseline_gate_token=values.get("RESBENCH_BASELINE_GATE_TOKEN"),
            cleanup_handle=values.get("RESBENCH_CLEANUP_HANDLE"),
            allowed_fault_types=allowed_fault_types,
            expected_fault=expected_fault,
            decision_policy=values.get(
                "RESBENCH_DECISION_POLICY", "clarify_missing"
            ),
            user_decision_file=(
                Path(user_decision_raw) if user_decision_raw else None
            ),
            controller_lease_file=(
                Path(controller_lease_raw) if controller_lease_raw else None
            ),
            ledger_dir=Path(ledger_raw) if ledger_raw else cls().ledger_dir,
            baseline_ledger_dir=Path(baseline_raw) if baseline_raw else None,
            kubectl_path=values.get("RESBENCH_KUBECTL", "kubectl"),
            create_uncertainty_variant=uncertainty_variant,
            condition_safety_ttl_seconds=condition_ttl,
        )


@dataclass(frozen=True)
class ExperimentRecord:
    name: str
    namespace: str
    run_id: str
    target_name: str
    target_uid: str
    fault_type: str
    phase: str
    owner: str | None
    labels: Mapping[str, str]
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.phase.strip().lower() in TERMINAL_PHASES

    @property
    def owned(self) -> bool:
        return self.owner == OWNER_VALUE


class ChaosBackend(Protocol):
    async def list_experiments(self, kubeconfig: str, namespace: str | None = None) -> list[ExperimentRecord]:
        """List executor-native chaos resources, optionally filtered by benchmark namespace."""

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        """Get one executor-native chaos resource by benchmark namespace and name."""

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        """Read the current UID of a Kubernetes Pod target."""

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        """Create an executor-native resource from a validated manifest."""

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        """Delete one ledger-owned executor-native resource by name."""

    def render_manifest(self, name: str, action: ChaosBladeAction) -> dict[str, Any]:
        """Render one executor-native resource from a safety-validated action."""

    async def prepare_target_fence(
        self, namespace: str, name: str, uid: str, kubeconfig: str
    ) -> None:
        """Install an executor-specific UID fence before create, if required."""

    async def clear_target_fence(
        self, namespace: str, name: str, uid: str, kubeconfig: str
    ) -> None:
        """Remove a previously installed UID fence after cleanup, if required."""
