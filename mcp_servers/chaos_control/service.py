"""Safety-gated ChaosBlade control service for the chaos_control MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Mapping, Protocol

from controller.safety import ChaosBladeAction, TargetIdentity, default_policy, validate_action


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

    def __init__(self, code: str, message: str, *, next_step: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_step = next_step

    def as_response(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "next_step": self.next_step}}


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime gates for destructive ChaosBlade operations."""

    execute_enabled: bool = False
    kubeconfig: str | None = None
    namespace_allowlist: frozenset[str] = frozenset()
    controller_token_ref: str | None = None
    controller_pod_uid: str | None = None
    controller_pod_namespace: str | None = None
    controller_pod_name: str | None = None
    controller_lease_file: Path | None = None
    authorized_run_id: str | None = None
    baseline_gate_token: str | None = None
    cleanup_handle: str | None = None
    ledger_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "resbench-chaos-control-ledger")
    baseline_ledger_dir: Path | None = None
    kubectl_path: str = "kubectl"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        values = os.environ if env is None else env
        namespaces = frozenset(
            item.strip()
            for item in values.get("RESBENCH_CHAOS_NAMESPACE_ALLOWLIST", "").split(",")
            if item.strip()
        )
        ledger_raw = values.get("RESBENCH_CHAOS_LEDGER_DIR")
        baseline_raw = values.get("RESBENCH_CHAOS_BASELINE_LEDGER_DIR")
        controller_lease_raw = values.get("RESBENCH_CHAOS_CONTROLLER_LEASE_FILE")
        return cls(
            execute_enabled=values.get("RESBENCH_CHAOS_EXECUTE_ENABLED", "").lower() == "true",
            kubeconfig=values.get("RESBENCH_CHAOS_KUBECONFIG"),
            namespace_allowlist=namespaces,
            controller_token_ref=values.get("RESBENCH_CHAOS_CONTROLLER_TOKEN_REF"),
            controller_pod_uid=values.get("RESBENCH_CHAOS_CONTROLLER_POD_UID"),
            controller_pod_namespace=values.get("RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE"),
            controller_pod_name=values.get("RESBENCH_CHAOS_CONTROLLER_POD_NAME"),
            authorized_run_id=values.get("RESBENCH_AUTHORIZED_RUN_ID"),
            baseline_gate_token=values.get("RESBENCH_BASELINE_GATE_TOKEN"),
            cleanup_handle=values.get("RESBENCH_CLEANUP_HANDLE"),
            controller_lease_file=(
                Path(controller_lease_raw) if controller_lease_raw else None
            ),
            ledger_dir=Path(ledger_raw) if ledger_raw else cls().ledger_dir,
            baseline_ledger_dir=Path(baseline_raw) if baseline_raw else None,
            kubectl_path=values.get("RESBENCH_KUBECTL", "kubectl"),
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
        """List cluster-scoped ChaosBlade resources, optionally filtered by benchmark namespace."""

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        """Get one ChaosBlade resource by benchmark namespace and name."""

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        """Read the current UID of a Kubernetes Pod target."""

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        """Create a ChaosBlade resource from a validated manifest."""

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        """Delete one ledger-owned ChaosBlade resource by name."""


class KubectlChaosBackend:
    """ChaosBackend implementation using fixed-argv kubectl subprocess calls."""

    def __init__(self, kubectl_path: str = "kubectl") -> None:
        self.kubectl_path = kubectl_path

    async def list_experiments(self, kubeconfig: str, namespace: str | None = None) -> list[ExperimentRecord]:
        output = await self._kubectl(["--kubeconfig", kubeconfig, "get", "chaosblades.chaosblade.io", "-o", "json"])
        data = json.loads(output or "{}")
        records = [_record_from_resource(item) for item in data.get("items", [])]
        if namespace is None:
            return records
        return [record for record in records if record.namespace == namespace]

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        try:
            output = await self._kubectl(["--kubeconfig", kubeconfig, "get", "chaosblades.chaosblade.io", name, "-o", "json"])
        except ChaosControlError as exc:
            if exc.code == "KUBECTL_NOT_FOUND":
                return None
            raise
        record = _record_from_resource(json.loads(output or "{}"))
        if record.namespace != namespace:
            return None
        return record

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        try:
            output = await self._kubectl(
                ["--kubeconfig", kubeconfig, "-n", namespace, "get", "pod", name, "-o", "json"]
            )
        except ChaosControlError as exc:
            if exc.code == "KUBECTL_NOT_FOUND":
                return None
            raise
        uid = json.loads(output or "{}").get("metadata", {}).get("uid")
        return str(uid) if uid else None

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        payload = json.dumps(manifest, separators=(",", ":")).encode()
        namespace = str(manifest["metadata"]["labels"][NAMESPACE_LABEL])
        name = str(manifest["metadata"]["name"])
        existing = await self._get_experiment_by_name(name, kubeconfig)
        if existing is not None:
            raise ChaosControlError(
                "CHAOSBLADE_NAME_ALREADY_EXISTS",
                "A cluster-scoped ChaosBlade resource with the deterministic experiment name already exists.",
                next_step="Do not overwrite terminal or historical CRs. Use a fresh run_id or reconcile the existing resource manually.",
            )
        await self._kubectl(["--kubeconfig", kubeconfig, "create", "-f", "-"], stdin=payload)
        created = await self.get_experiment(namespace, name, kubeconfig)
        if created is None:
            raise ChaosControlError(
                "CREATE_NOT_OBSERVABLE",
                "ChaosBlade create returned but the resource could not be read back.",
                next_step="Run chaos_inventory_run and check the ChaosBlade operator event stream.",
            )
        return created

    async def _get_experiment_by_name(self, name: str, kubeconfig: str) -> ExperimentRecord | None:
        try:
            output = await self._kubectl(["--kubeconfig", kubeconfig, "get", "chaosblades.chaosblade.io", name, "-o", "json"])
        except ChaosControlError as exc:
            if exc.code == "KUBECTL_NOT_FOUND":
                return None
            raise
        return _record_from_resource(json.loads(output or "{}"))

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        await self._kubectl(["--kubeconfig", kubeconfig, "delete", "chaosblades.chaosblade.io", name, "--ignore-not-found=true"])

    async def _kubectl(self, args: list[str], *, stdin: bytes | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.kubectl_path,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(stdin)
        if proc.returncode != 0:
            detail = _safe_kubectl_error(stderr.decode(errors="replace"))
            lowered = detail.lower()
            if "notfound" in lowered or "not found" in lowered:
                raise ChaosControlError(
                    "KUBECTL_NOT_FOUND",
                    "Kubernetes resource was not found.",
                    next_step="Refresh inventory and retry with a current resource name.",
                )
            raise ChaosControlError(
                "KUBECTL_FAILED",
                f"kubectl failed for a fixed ChaosBlade operation: {detail}",
                next_step="Verify kubeconfig path, RBAC for ChaosBlade CRs, and the namespace allowlist.",
            )
        return stdout.decode()


class InMemoryChaosBackend:
    """Fake backend for unit tests and dry integration checks."""

    def __init__(
        self,
        experiments: list[ExperimentRecord] | None = None,
        pod_uids: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self.experiments: dict[tuple[str, str], ExperimentRecord] = {}
        self.pod_uids: dict[tuple[str, str], str] = dict(pod_uids or {})
        self.created_manifests: list[Mapping[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        for record in experiments or []:
            self.experiments[(record.namespace, record.name)] = record

    async def list_experiments(self, kubeconfig: str, namespace: str | None = None) -> list[ExperimentRecord]:
        records = list(self.experiments.values())
        if namespace is None:
            return records
        return [record for record in records if record.namespace == namespace]

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        return self.experiments.get((namespace, name))

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        return self.pod_uids.get((namespace, name))

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        self.created_manifests.append(manifest)
        labels = manifest["metadata"]["labels"]
        experiment = manifest["spec"]["experiments"][0]
        record = ExperimentRecord(
            name=manifest["metadata"]["name"],
            namespace=labels[NAMESPACE_LABEL],
            run_id=labels[RUN_ID_LABEL],
            target_name=_matcher_value(experiment, "names"),
            target_uid=labels[TARGET_UID_LABEL],
            fault_type=labels[FAULT_TYPE_LABEL],
            phase="Running",
            owner=labels[OWNER_LABEL],
            labels=dict(labels),
            raw=dict(manifest),
        )
        self.experiments[(record.namespace, record.name)] = record
        return record

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        self.deleted.append((namespace, name))
        self.experiments.pop((namespace, name), None)


class ChaosControlService:
    """Implements safety-gated chaos_control workflows independently of MCP transport."""

    def __init__(self, config: RuntimeConfig, backend: ChaosBackend | None = None) -> None:
        self.config = config
        self.backend = backend or KubectlChaosBackend(config.kubectl_path)
        self._mutation_lock = asyncio.Lock()

    async def validate_plan(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        policy = default_policy(set(self.config.namespace_allowlist))
        action = _action(run_id, namespace, target_name, target_uid, fault_type, duration_seconds, intensity, selector)
        result = validate_action(action, policy, active_action_count=0)
        return {
            "ok": result.ok,
            "read_only": True,
            "findings": [_finding_payload(item.code, item.message) for item in result.findings],
            "policy": _policy_payload(policy),
        }

    async def inventory_run(self, *, namespace: str, kubeconfig: str | None = None) -> dict[str, Any]:
        cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
        self._validate_namespace(namespace)
        all_records = await self.backend.list_experiments(cfg_kubeconfig)
        records = [record for record in all_records if record.namespace == namespace]
        unsafe_unowned = [item for item in all_records if not item.terminal and not item.owned]
        return {
            "ok": True,
            "read_only": True,
            "namespace": namespace,
            "cluster_scoped": True,
            "experiments": [_record_payload(record) for record in sorted(records, key=lambda item: item.name)],
            "global_chaosblade_count": len(all_records),
            "global_unsafe_unowned_count": len(unsafe_unowned),
            "nonterminal_unowned_count": len([item for item in records if not item.terminal and not item.owned]),
            "active_owned_count": len([item for item in all_records if not item.terminal and item.owned]),
        }

    async def create_experiment(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        kubeconfig: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            return await self._create_experiment_locked(
                run_id=run_id,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                kubeconfig=kubeconfig,
                controller_token_ref=controller_token_ref,
                expected_controller_pod_uid=expected_controller_pod_uid,
                baseline_gate_token=baseline_gate_token,
                cleanup_handle=cleanup_handle,
                selector=selector,
            )

    async def _create_experiment_locked(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        kubeconfig: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self._assert_create_runtime_gates(
            kubeconfig=kubeconfig,
            namespace=namespace,
            controller_token_ref=controller_token_ref,
            expected_controller_pod_uid=expected_controller_pod_uid,
            baseline_gate_token=baseline_gate_token,
            cleanup_handle=cleanup_handle,
        )
        await self._verify_controller_identity(kubeconfig)
        self._verify_baseline_gate(
            baseline_gate_token=baseline_gate_token,
            run_id=run_id,
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
        )
        baseline_token_hash = _sha256(baseline_gate_token)
        self._assert_baseline_token_unused(baseline_token_hash, cleanup_handle)
        current_uid = await self.backend.get_pod_uid(namespace, target_name, kubeconfig)
        if current_uid is None:
            raise ChaosControlError(
                "TARGET_POD_NOT_FOUND",
                "The target Pod was not found during server-side identity verification.",
                next_step="Refresh target inventory and retry with a currently running Pod name and UID.",
            )
        if current_uid != target_uid:
            raise ChaosControlError(
                "TARGET_UID_MISMATCH",
                "The target Pod UID no longer matches the request.",
                next_step="Refresh the target Pod identity before creating a ChaosBlade experiment.",
            )

        all_records = await self.backend.list_experiments(kubeconfig)
        active_owned_count = len([item for item in all_records if not item.terminal and item.owned])
        unowned = [item for item in all_records if not item.terminal and not item.owned]
        if unowned:
            raise ChaosControlError(
                "UNSAFE_UNOWNED_CHAOSBLADE_PRESENT",
                "A non-terminal ChaosBlade resource not owned by this server already exists in the cluster.",
                next_step="Reconcile external ChaosBlade resources before retrying create.",
            )

        policy = default_policy(set(self.config.namespace_allowlist))
        action = _action(run_id, namespace, target_name, target_uid, fault_type, duration_seconds, intensity, selector)
        result = validate_action(action, policy, active_action_count=active_owned_count)
        if not result.ok:
            raise ChaosControlError(
                "PLAN_REJECTED_BY_SAFETY_POLICY",
                "The requested ChaosBlade action violates the controller safety policy.",
                next_step=f"Fix these validation codes before retrying: {', '.join(result.codes())}.",
            )

        name = _experiment_name(run_id, fault_type)
        manifest = _manifest(name, action)
        created_at = _now_utc()
        deadline_at = created_at + _duration_delta(duration_seconds)
        ledger = {
            "version": LEDGER_VERSION,
            "created_at": _datetime_iso(created_at),
            "updated_at": _datetime_iso(created_at),
            "state": "pending_apply",
            "experiment_name": name,
            "namespace": namespace,
            "run_id": run_id,
            "target_name": target_name,
            "target_uid": target_uid,
            "fault_type": fault_type,
            "duration_seconds": duration_seconds,
            "deadline_at": _datetime_iso(deadline_at),
            "controller_token_ref": controller_token_ref,
            "baseline_gate_token_sha256": baseline_token_hash,
            "cleanup_handle": cleanup_handle,
        }
        self._write_ledger(cleanup_handle, ledger)
        try:
            record = await self.backend.create_experiment(manifest, kubeconfig)
        except Exception:
            failed_ledger = {**ledger, "state": "create_failed", "updated_at": _now_iso()}
            self._write_ledger(cleanup_handle, failed_ledger)
            raise
        ledger = {
            **ledger,
            "state": "active",
            "ever_active": True,
            "updated_at": _now_iso(),
            "experiment_name": record.name,
        }
        self._write_ledger(cleanup_handle, ledger)
        return {
            "ok": True,
            "created": _record_payload(record),
            "cleanup_handle": cleanup_handle,
            "safety": {
                "active_owned_count_before_create": active_owned_count,
                "unowned_active_count": 0,
                "target_uid_verified": True,
            },
        }

    async def get_experiment(self, *, namespace: str, name: str, kubeconfig: str | None = None) -> dict[str, Any]:
        cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
        self._validate_namespace(namespace)
        _validate_resource_name(name, "name")
        record = await self.backend.get_experiment(namespace, name, cfg_kubeconfig)
        return {"ok": True, "read_only": True, "found": record is not None, "experiment": _record_payload(record) if record else None}

    async def destroy_experiment(self, *, cleanup_handle: str, kubeconfig: str) -> dict[str, Any]:
        async with self._mutation_lock:
            return await self._destroy_experiment_locked(cleanup_handle=cleanup_handle, kubeconfig=kubeconfig)

    async def _destroy_experiment_locked(self, *, cleanup_handle: str, kubeconfig: str) -> dict[str, Any]:
        self._assert_destroy_runtime_gates(kubeconfig=kubeconfig, cleanup_handle=cleanup_handle)
        ledger = self._read_ledger(cleanup_handle)
        namespace = ledger["namespace"]
        name = ledger["experiment_name"]
        await self._delete_and_verify_from_ledger(ledger, kubeconfig)
        self._write_ledger(
            cleanup_handle,
            {
                **ledger,
                "state": "destroyed",
                "cleanup_error": None,
                "updated_at": _now_iso(),
            },
        )
        return {"ok": True, "destroyed": name, "namespace": namespace, "verified_absent": True, "idempotent": True}

    async def cleanup_expired_leases(self, *, now: datetime | None = None) -> dict[str, Any]:
        async with self._mutation_lock:
            return await self._cleanup_expired_leases_locked(now=now)

    async def _cleanup_expired_leases_locked(self, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = _as_utc(now or _now_utc())
        kubeconfig = self._resolve_kubeconfig(None)
        inspected = 0
        cleaned: list[str] = []
        errors: list[dict[str, str]] = []
        for path in self._iter_cleanup_ledger_paths():
            payload: dict[str, Any] | None = None
            try:
                payload = _read_private_json_file(
                    path,
                    label="cleanup ledger entry",
                    missing_code="CLEANUP_LEDGER_UNREADABLE",
                    missing_message="A cleanup ledger entry disappeared while scanning expired leases.",
                    missing_next_step="Retry cleanup after checking the private cleanup ledger directory.",
                )
                state = str(payload.get("state", ""))
                if state not in {"active", "pending", "pending_apply", "create_failed", "cleanup_error"}:
                    continue
                deadline_at = _parse_datetime(payload.get("deadline_at"))
                if deadline_at is None or deadline_at > current_time:
                    continue
                inspected += 1
                await self._delete_and_verify_from_ledger(payload, kubeconfig)
                updated = {**payload, "state": "expired_cleaned", "cleanup_error": None, "updated_at": _now_iso()}
                self._write_ledger(str(payload["cleanup_handle"]), updated)
                cleaned.append(str(payload["cleanup_handle"]))
            except Exception as exc:  # noqa: BLE001 - watchdog cleanup must keep scanning
                handle = path.stem if path.name.endswith(".json") else path.name
                errors.append({"cleanup_handle": handle, "code": _error_code(exc)})
                try:
                    if payload and payload.get("cleanup_handle"):
                        updated = {
                            **payload,
                            "state": "cleanup_error",
                            "cleanup_error": _error_code(exc),
                            "updated_at": _now_iso(),
                        }
                        self._write_ledger(str(payload["cleanup_handle"]), updated)
                except Exception:
                    pass
        return {"ok": not errors, "inspected": inspected, "cleaned": cleaned, "errors": errors}

    async def recovery_status(self, *, cleanup_handle: str, kubeconfig: str | None = None) -> dict[str, Any]:
        cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
        ledger = self._read_ledger(cleanup_handle)
        record = await self.backend.get_experiment(ledger["namespace"], ledger["experiment_name"], cfg_kubeconfig)
        return {
            "ok": True,
            "read_only": True,
            "cleanup_handle": cleanup_handle,
            "namespace": ledger["namespace"],
            "experiment_name": ledger["experiment_name"],
            "resource_absent": record is None,
            "terminal": True if record is None else record.terminal,
            "phase": "Absent" if record is None else record.phase,
            "ledger_state": str(ledger.get("state", "unknown")),
            "run_id": str(ledger.get("run_id", "")),
            "target_uid": str(ledger.get("target_uid", "")),
            "fault_type": str(ledger.get("fault_type", "")),
            "created_at": ledger.get("created_at"),
            "deadline_at": ledger.get("deadline_at"),
            "ever_active": bool(ledger.get("ever_active")),
        }

    async def _delete_and_verify_from_ledger(self, ledger: Mapping[str, Any], kubeconfig: str) -> None:
        namespace = str(ledger["namespace"])
        name = str(ledger["experiment_name"])
        existing = await self.backend.get_experiment(namespace, name, kubeconfig)
        if existing and not _record_matches_ledger(existing, ledger):
            raise ChaosControlError(
                "LEDGER_TARGET_MISMATCH",
                "The live ChaosBlade resource no longer matches this server ledger handle.",
                next_step="Do not delete it through this handle. Run chaos_inventory_run and reconcile ownership manually.",
            )
        await self.backend.delete_experiment(namespace, name, kubeconfig)
        after = await self.backend.get_experiment(namespace, name, kubeconfig)
        if after is not None:
            raise ChaosControlError(
                "DESTROY_VERIFY_ABSENCE_FAILED",
                "ChaosBlade delete was issued but the resource is still present.",
                next_step="Wait for the operator to settle, then retry cleanup.",
            )

    def _assert_create_runtime_gates(
        self,
        *,
        kubeconfig: str,
        namespace: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
    ) -> None:
        if not self.config.execute_enabled:
            raise ChaosControlError(
                "EXECUTION_DISABLED",
                "Chaos creation is disabled by default.",
                next_step="Enable RESBENCH_CHAOS_EXECUTE_ENABLED only in the controller runtime after baseline checks pass.",
            )
        if not kubeconfig or kubeconfig != self.config.kubeconfig:
            raise ChaosControlError(
                "EXPLICIT_KUBECONFIG_REQUIRED",
                "Create requires the explicit kubeconfig path configured for this server.",
                next_step="Pass the exact configured kubeconfig path; do not rely on ambient Kubernetes context.",
            )
        self._validate_namespace(namespace)
        if not self.config.controller_token_ref or controller_token_ref != self.config.controller_token_ref:
            raise ChaosControlError(
                "CONTROLLER_TOKEN_REF_REQUIRED",
                "Create requires the configured controller token reference, not a raw token.",
                next_step="Pass the configured token reference name; never pass a token value.",
            )
        if not SAFE_REF_RE.fullmatch(controller_token_ref):
            raise ChaosControlError(
                "INVALID_CONTROLLER_TOKEN_REF",
                "Controller token reference contains unsupported characters.",
                next_step="Use a non-secret reference such as k8s://namespace/secret/name#key.",
            )
        if not self.config.controller_pod_uid or expected_controller_pod_uid != self.config.controller_pod_uid:
            raise ChaosControlError(
                "CONTROLLER_POD_UID_MISMATCH",
                "The request does not match the controller Pod UID injected when this MCP process started.",
                next_step="Refresh controller identity from the owning controller Pod and restart the MCP server if the Pod changed.",
            )
        if not baseline_gate_token:
            raise ChaosControlError(
                "BASELINE_GATE_REQUIRED",
                "Create requires a baseline gate token proving the healthy baseline completed.",
                next_step="Run the baseline gate first and pass its opaque token.",
            )
        self._validate_handle(cleanup_handle)
        if self._ledger_path(cleanup_handle).exists():
            raise ChaosControlError(
                "CLEANUP_HANDLE_ALREADY_USED",
                "The cleanup handle already exists in this server ledger.",
                next_step="Generate a fresh cleanup handle, or destroy the existing handle first.",
            )

    async def _verify_controller_identity(self, kubeconfig: str) -> None:
        if self.config.controller_lease_file is not None:
            payload = _read_private_json_file(
                self.config.controller_lease_file,
                label="controller process lease",
                missing_code="CONTROLLER_LEASE_MISSING",
                missing_message="The local Controller process lease is missing or unsafe.",
                missing_next_step="Restart the local Controller supervisor before enabling writes.",
            )
            controller_id = str(payload.get("controller_id") or "")
            expires_at = _parse_datetime(payload.get("expires_at"))
            try:
                pid = int(payload.get("pid"))
                process_alive = pid > 1
                if process_alive:
                    os.kill(pid, 0)
            except (OSError, TypeError, ValueError):
                process_alive = False
            if (
                controller_id != self.config.controller_pod_uid
                or expires_at is None
                or expires_at <= datetime.now(timezone.utc)
                or not process_alive
            ):
                raise ChaosControlError(
                    "CONTROLLER_LEASE_INVALID",
                    "The local Controller process lease is expired or does not match the configured identity.",
                    next_step="Renew the private Controller lease from the live supervisor before retrying.",
                )
            return
        if not (self.config.controller_pod_namespace and self.config.controller_pod_name):
            return
        live_uid = await self.backend.get_pod_uid(self.config.controller_pod_namespace, self.config.controller_pod_name, kubeconfig)
        if live_uid != self.config.controller_pod_uid:
            raise ChaosControlError(
                "CONTROLLER_LIVE_UID_MISMATCH",
                "The controller Pod UID injected at process start no longer matches the live controller Pod.",
                next_step="Restart the MCP server from the current controller Pod before enabling ChaosBlade writes.",
            )

    def _verify_baseline_gate(
        self,
        *,
        baseline_gate_token: str,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
    ) -> None:
        if not baseline_gate_token:
            raise ChaosControlError(
                "BASELINE_GATE_REQUIRED",
                "Create requires an opaque baseline gate capability token.",
                next_step="Run the controller-owned baseline gate first and pass its opaque token.",
            )
        if self.config.baseline_ledger_dir is None:
            raise ChaosControlError(
                "BASELINE_LEDGER_REQUIRED",
                "Execution is enabled but no controller-owned baseline ledger directory is configured.",
                next_step="Configure RESBENCH_CHAOS_BASELINE_LEDGER_DIR; until then create remains unavailable.",
            )
        _assert_private_directory(self.config.baseline_ledger_dir, "baseline ledger")
        token_hash = _sha256(baseline_gate_token)
        path = self.config.baseline_ledger_dir / f"{token_hash}.json"
        payload = _read_private_json_file(
            path,
            label="baseline ledger capability",
            missing_code="BASELINE_TOKEN_NOT_FOUND",
            missing_message="No baseline ledger capability matches the provided token.",
            missing_next_step="Re-run baseline through the controller and pass the returned opaque capability token.",
        )
        expected = {
            "passed": True,
            "run_id": run_id,
            "namespace": namespace,
            "target_name": target_name,
            "target_uid": target_uid,
            "controller_pod_uid": self.config.controller_pod_uid,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ChaosControlError(
                    "BASELINE_LEDGER_MISMATCH",
                    "Baseline capability does not match the requested run, target, or controller identity.",
                    next_step="Discard this token, re-run baseline for the exact live target, and retry create.",
                )
        expires_at = _parse_datetime(payload.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise ChaosControlError(
                "BASELINE_TOKEN_EXPIRED",
                "Baseline capability is missing a valid future expires_at timestamp.",
                next_step="Re-run baseline to obtain a fresh capability token.",
            )

    def _assert_baseline_token_unused(self, token_hash: str, cleanup_handle: str) -> None:
        if not self.config.ledger_dir.exists():
            return
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        for path in sorted(self.config.ledger_dir.glob("*.json")):
            payload = _read_private_json_file(
                path,
                label="cleanup ledger entry",
                missing_code="CLEANUP_LEDGER_UNREADABLE",
                missing_message="A cleanup ledger entry disappeared while checking baseline token replay.",
                missing_next_step="Pause chaos writes and inspect the cleanup ledger directory.",
            )
            if payload.get("cleanup_handle") == cleanup_handle:
                continue
            if payload.get("baseline_gate_token_sha256") == token_hash:
                raise ChaosControlError(
                    "BASELINE_TOKEN_REPLAYED",
                    "Baseline capability token was already consumed by a prior create attempt.",
                    next_step="Run a fresh baseline and use the new controller-issued capability token.",
                )

    def _iter_cleanup_ledger_paths(self) -> list[Path]:
        if not self.config.ledger_dir.exists():
            return []
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        return sorted(self.config.ledger_dir.glob("*.json"))

    def _assert_destroy_runtime_gates(self, *, kubeconfig: str, cleanup_handle: str) -> None:
        if not kubeconfig or kubeconfig != self.config.kubeconfig:
            raise ChaosControlError(
                "EXPLICIT_KUBECONFIG_REQUIRED",
                "Destroy requires the explicit kubeconfig path configured for this server.",
                next_step="Pass the exact configured kubeconfig path; do not rely on ambient Kubernetes context.",
            )
        self._validate_handle(cleanup_handle)

    def _resolve_kubeconfig(self, kubeconfig: str | None) -> str:
        selected = kubeconfig or self.config.kubeconfig
        if not selected:
            raise ChaosControlError(
                "KUBECONFIG_REQUIRED",
                "This operation needs an explicit kubeconfig path.",
                next_step="Pass kubeconfig or configure RESBENCH_CHAOS_KUBECONFIG for this MCP server.",
            )
        return selected

    def _validate_namespace(self, namespace: str) -> None:
        _validate_resource_name(namespace, "namespace")
        if namespace not in self.config.namespace_allowlist:
            raise ChaosControlError(
                "NAMESPACE_NOT_ALLOWED",
                "Namespace is outside the configured chaos control allowlist.",
                next_step="Use an allowlisted benchmark namespace or update RESBENCH_CHAOS_NAMESPACE_ALLOWLIST.",
            )

    def _ensure_ledger_dir(self) -> None:
        if self.config.ledger_dir.exists():
            _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        else:
            self.config.ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.config.ledger_dir, 0o700)

    def _ledger_path(self, cleanup_handle: str) -> Path:
        return self.config.ledger_dir / f"{cleanup_handle}.json"

    def _validate_handle(self, cleanup_handle: str) -> None:
        if not HANDLE_RE.fullmatch(cleanup_handle):
            raise ChaosControlError(
                "INVALID_CLEANUP_HANDLE",
                "Cleanup handle must start with cleanup- and contain only safe identifier characters.",
                next_step="Generate a fresh handle with the controller, for example cleanup- plus a random token.",
            )

    def _write_ledger(self, cleanup_handle: str, payload: Mapping[str, Any]) -> None:
        self._ensure_ledger_dir()
        final_path = self._ledger_path(cleanup_handle)
        fd, temp_name = tempfile.mkstemp(prefix=f".{cleanup_handle}.", suffix=".tmp", dir=self.config.ledger_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, final_path)
            os.chmod(final_path, 0o600)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _read_ledger(self, cleanup_handle: str) -> dict[str, Any]:
        self._validate_handle(cleanup_handle)
        path = self._ledger_path(cleanup_handle)
        if not self.config.ledger_dir.exists():
            raise ChaosControlError(
                "UNKNOWN_CLEANUP_HANDLE",
                "This server has no ledger entry for the cleanup handle.",
                next_step="Use the cleanup handle returned by chaos_create_experiment on this same server instance.",
            )
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        payload = _read_private_json_file(
            path,
            label="cleanup ledger entry",
            missing_code="UNKNOWN_CLEANUP_HANDLE",
            missing_message="This server has no ledger entry for the cleanup handle.",
            missing_next_step="Use the cleanup handle returned by chaos_create_experiment on this same server instance.",
        )
        required = {"experiment_name", "namespace", "run_id", "target_uid", "cleanup_handle"}
        missing = required - set(payload)
        if missing:
            raise ChaosControlError(
                "CORRUPT_LEDGER_ENTRY",
                "The cleanup ledger entry is missing required fields.",
                next_step="Stop automated cleanup and reconcile this experiment manually from inventory.",
            )
        return payload


def new_cleanup_handle() -> str:
    """Return a cleanup handle suitable for passing to chaos_create_experiment."""

    return "cleanup-" + secrets.token_hex(18)


def _action(
    run_id: str,
    namespace: str,
    target_name: str,
    target_uid: str,
    fault_type: str,
    duration_seconds: int,
    intensity: Mapping[str, Any],
    selector: Mapping[str, str] | None,
) -> ChaosBladeAction:
    labels = {
        RUN_ID_LABEL: run_id,
        TARGET_UID_LABEL: target_uid,
        NAMESPACE_LABEL: namespace,
        FAULT_TYPE_LABEL: fault_type,
        OWNER_LABEL: OWNER_VALUE,
    }
    target = TargetIdentity(namespace=namespace, kind="Pod", name=target_name, uid=target_uid, selector=selector)
    return ChaosBladeAction(
        run_id=run_id,
        namespace=namespace,
        target=target,
        fault_type=fault_type,
        duration_seconds=duration_seconds,
        intensity=dict(intensity),
        labels=labels,
    )


def _manifest(name: str, action: ChaosBladeAction) -> dict[str, Any]:
    return {
        "apiVersion": "chaosblade.io/v1alpha1",
        "kind": "ChaosBlade",
        "metadata": {
            "name": name,
            "labels": {
                RUN_ID_LABEL: action.run_id,
                TARGET_UID_LABEL: action.target.uid,
                NAMESPACE_LABEL: action.namespace,
                FAULT_TYPE_LABEL: action.fault_type,
                OWNER_LABEL: OWNER_VALUE,
            },
        },
        "spec": {"experiments": [_experiment_spec(action)]},
    }


def _experiment_spec(action: ChaosBladeAction) -> dict[str, Any]:
    matchers = [
        {"name": "names", "value": [action.target.name]},
        {"name": "namespace", "value": [action.namespace]},
    ]
    if action.fault_type == "cpu-load":
        matchers.append({"name": "cpu-percent", "value": [str(action.intensity["cpu_percent"])]})
        return {"scope": "pod", "target": "cpu", "action": "fullload", "matchers": matchers}
    if action.fault_type == "memory-stress":
        matchers.append({"name": "mem-percent", "value": [str(action.intensity["mem_percent"])]})
        return {"scope": "pod", "target": "mem", "action": "load", "matchers": matchers}
    if action.fault_type == "network-delay":
        matchers.extend([
            {"name": "time", "value": [str(action.intensity["delay_ms"])]},
            {"name": "interface", "value": ["eth0"]},
        ])
        return {"scope": "pod", "target": "network", "action": "delay", "matchers": matchers}
    if action.fault_type == "network-loss":
        matchers.extend([
            {"name": "percent", "value": [str(action.intensity["loss_percent"])]},
            {"name": "interface", "value": ["eth0"]},
        ])
        return {"scope": "pod", "target": "network", "action": "loss", "matchers": matchers}
    if action.fault_type == "pod-kill":
        return {"scope": "pod", "target": "pod", "action": "delete", "matchers": matchers}
    raise ChaosControlError(
        "FAULT_TYPE_NOT_ALLOWED",
        "Fault type is not supported by the controller policy.",
        next_step="Call chaos_validate_plan and choose one of the allowed policy fault types.",
    )


def _experiment_name(run_id: str, fault_type: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{fault_type}".encode()).hexdigest()[:10]
    base = re.sub(r"[^a-z0-9-]+", "-", f"cc-{run_id}-{fault_type}".lower()).strip("-")
    return f"{base[:45]}-{digest}"


def _record_from_resource(resource: Mapping[str, Any]) -> ExperimentRecord:
    metadata = resource.get("metadata", {})
    labels = metadata.get("labels", {}) or {}
    spec = resource.get("spec", {}) or {}
    status = resource.get("status", {}) or {}
    experiment = _first_experiment(spec)
    phase = str(status.get("phase") or status.get("status") or status.get("state") or "Unknown")
    return ExperimentRecord(
        name=str(metadata.get("name", "")),
        namespace=str(labels.get(NAMESPACE_LABEL) or _matcher_value(experiment, "namespace")),
        run_id=str(labels.get(RUN_ID_LABEL, "")),
        target_name=str(_matcher_value(experiment, "names")),
        target_uid=str(labels.get(TARGET_UID_LABEL, "")),
        fault_type=str(labels.get(FAULT_TYPE_LABEL) or _fault_type_from_experiment(experiment)),
        phase=phase,
        owner=labels.get(OWNER_LABEL),
        labels=dict(labels),
        raw=resource,
    )


def _record_payload(record: ExperimentRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "namespace": record.namespace,
        "run_id": record.run_id,
        "target_name": record.target_name,
        "target_uid": record.target_uid,
        "fault_type": record.fault_type,
        "phase": record.phase,
        "owned": record.owned,
        "terminal": record.terminal,
        "labels": {
            RUN_ID_LABEL: record.labels.get(RUN_ID_LABEL),
            TARGET_UID_LABEL: record.labels.get(TARGET_UID_LABEL),
            NAMESPACE_LABEL: record.labels.get(NAMESPACE_LABEL),
            FAULT_TYPE_LABEL: record.labels.get(FAULT_TYPE_LABEL),
            OWNER_LABEL: record.labels.get(OWNER_LABEL),
        },
    }


def _first_experiment(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    experiments = spec.get("experiments")
    if isinstance(experiments, list) and experiments and isinstance(experiments[0], Mapping):
        return experiments[0]
    return {}


def _matcher_value(experiment: Mapping[str, Any], name: str) -> str:
    matchers = experiment.get("matchers")
    if not isinstance(matchers, list):
        return ""
    for matcher in matchers:
        if not isinstance(matcher, Mapping) or matcher.get("name") != name:
            continue
        values = matcher.get("value")
        if isinstance(values, list) and values:
            return str(values[0])
    return ""


def _fault_type_from_experiment(experiment: Mapping[str, Any]) -> str:
    target = experiment.get("target")
    action = experiment.get("action")
    if target == "cpu" and action == "fullload":
        return "cpu-load"
    if target == "mem" and action == "load":
        return "memory-stress"
    if target == "network" and action == "delay":
        return "network-delay"
    if target == "network" and action == "loss":
        return "network-loss"
    if target == "pod" and action == "delete":
        return "pod-kill"
    return ""


def _finding_payload(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _policy_payload(policy: Any) -> dict[str, Any]:
    return {
        "namespace_allowlist": sorted(policy.namespace_allowlist),
        "max_concurrent_actions": policy.max_concurrent_actions,
        "allowed_fault_types": sorted(policy.fault_type_budgets),
    }


def _record_matches_ledger(record: ExperimentRecord, ledger: Mapping[str, Any]) -> bool:
    return (
        record.owned
        and record.run_id == ledger["run_id"]
        and record.target_uid == ledger["target_uid"]
        and record.namespace == ledger["namespace"]
        and record.name == ledger["experiment_name"]
    )


def _validate_resource_name(value: str, field_name: str) -> None:
    if not SAFE_NAME_RE.fullmatch(value):
        raise ChaosControlError(
            "INVALID_RESOURCE_NAME",
            f"{field_name} must be a Kubernetes DNS-like resource name.",
            next_step=f"Pass a concrete {field_name}; selectors and shell fragments are not accepted.",
        )


def _assert_private_directory(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ChaosControlError(
            "LEDGER_DIRECTORY_MISSING",
            f"{label} directory does not exist.",
            next_step=f"Create the controller-owned {label} directory with mode 0700 before enabling writes.",
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ChaosControlError(
            "LEDGER_DIRECTORY_UNSAFE",
            f"{label} path must be a real directory, not a symlink or special file.",
            next_step=f"Replace the {label} path with a controller-owned directory with mode 0700.",
        )
    if stat.S_IMODE(mode) != 0o700:
        raise ChaosControlError(
            "LEDGER_DIRECTORY_MODE_UNSAFE",
            f"{label} directory must have mode 0700.",
            next_step=f"Fix {label} directory permissions before enabling writes.",
        )


def _read_private_json_file(
    path: Path,
    *,
    label: str,
    missing_code: str,
    missing_message: str,
    missing_next_step: str,
) -> dict[str, Any]:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ChaosControlError(missing_code, missing_message, next_step=missing_next_step) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ChaosControlError(
            "LEDGER_FILE_UNSAFE",
            f"{label} must be a regular file, not a symlink or special file.",
            next_step="Pause chaos writes and replace the ledger entry with a controller-owned regular file.",
        )
    if stat.S_IMODE(mode) != 0o600:
        raise ChaosControlError(
            "LEDGER_FILE_MODE_UNSAFE",
            f"{label} must have mode 0600.",
            next_step="Fix ledger file permissions before using this capability or cleanup handle.",
        )
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ChaosControlError(
            "LEDGER_FILE_UNREADABLE",
            f"{label} is not valid JSON.",
            next_step="Pause chaos writes and reconcile the controller ledger manually.",
        ) from exc
    if not isinstance(payload, dict):
        raise ChaosControlError(
            "LEDGER_FILE_UNREADABLE",
            f"{label} JSON must be an object.",
            next_step="Pause chaos writes and reconcile the controller ledger manually.",
        )
    return payload


def _safe_kubectl_error(stderr: str) -> str:
    text = " ".join(stderr.split())
    return text[:500] if text else "no stderr"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration_delta(duration_seconds: int) -> timedelta:
    return timedelta(seconds=duration_seconds)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ChaosControlError):
        return exc.code
    return type(exc).__name__


def _now_iso() -> str:
    return _now_utc().isoformat()
