"""Concrete runtime adapters used by the single Stage-2 service."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
from collections.abc import Mapping
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from disturbances.kubernetes_runtime import KubernetesDisturbanceClient

from .capability_policy import CapabilityPolicyDocument, CapabilityPolicyRegistry
from .contracts import DisturbanceRecord, DisturbanceType
from .platform_ledger import PlatformLedger
from .target_binding import current as current_target_binding


# The Controller stamps this label on every fault it creates.
CHAOSBLADE_NAMESPACE_LABEL = "benchmark.namespace"


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


def _chaosblade_target_namespace(item: Mapping[str, Any]) -> str | None:
    """The namespace a ChaosBlade CR aims at, from the platform label or its spec.

    The Controller labels every experiment it creates with
    ``benchmark.namespace``. An unlabelled CR is attributed through its first
    experiment's ``namespace`` matcher, which is how ChaosBlade itself scopes
    a Pod-level experiment. Returns ``None`` when neither is present.
    """
    metadata = item.get("metadata") if isinstance(item, Mapping) else None
    labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
    if isinstance(labels, Mapping):
        labelled = str(labels.get(CHAOSBLADE_NAMESPACE_LABEL) or "").strip()
        if labelled:
            return labelled
    spec = item.get("spec") if isinstance(item, Mapping) else None
    experiments = spec.get("experiments") if isinstance(spec, Mapping) else None
    if isinstance(experiments, list):
        for experiment in experiments:
            matchers = experiment.get("matchers") if isinstance(experiment, Mapping) else None
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, Mapping) or str(matcher.get("name") or "") != "namespace":
                    continue
                values = matcher.get("value")
                if isinstance(values, list) and values:
                    return str(values[0]).strip() or None
                if isinstance(values, str) and values.strip():
                    return values.strip()
    return None


def _partition_chaosblade_inventory(
    items: list[Any], namespace: str
) -> tuple[list[Any], list[Any], list[Any]]:
    """Split a cluster-wide ChaosBlade inventory into own, foreign and unattributed.

    Matching is exact equality, never a prefix test: ``otel-demo`` is a prefix
    of ``otel-demo-01`` and a ``startswith`` would treat one replica's fault as
    another's. An experiment with no discoverable target namespace stays in the
    blocking set, so an unattributable leftover still fails the gate.
    """
    own: list[Any] = []
    foreign: list[Any] = []
    unattributed: list[Any] = []
    for item in items:
        target = _chaosblade_target_namespace(item) if isinstance(item, Mapping) else None
        if target is None:
            unattributed.append(item)
            own.append(item)
        elif target == namespace:
            own.append(item)
        else:
            foreign.append(item)
    return own, foreign, unattributed


class KubernetesEnvironmentGate:
    """Read-only gate; the service never starts or scales the application load generator."""

    def __init__(self, kubeconfig: Path, *, runner: CommandRunner | None = None):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.runner = runner or SubprocessRunner()

    def qualify(self, episode) -> Mapping[str, Any]:
        # The Episode is frozen and hash-checked, so its snapshot names the
        # system it was written against -- the deployment bundle. A replica is
        # a copy of that system in its own namespace, so the Episode is
        # checked against the bundle and the cluster is read in the namespace
        # this instance is actually bound to. With the default binding the two
        # are the same name and this is the original check.
        snapshot = episode.public.environment_snapshot.get("namespace", "")
        binding = current_target_binding()
        # Exact equality only: ``otel-demo`` and ``otel-demo-01`` share a
        # prefix, and a prefix match would mix a replica with the full system.
        if snapshot != binding.bundle:
            return {
                "qualified": False,
                "reason": f"fixed Episode namespace is not {binding.bundle}",
            }
        namespace = binding.application_namespace
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
        # ChaosBlade CRs are cluster-scoped, so the inventory is listed across
        # namespaces and then attributed. Only experiments aimed at *this*
        # replica may block it; a sibling replica's injection must not.
        chaos = self._json(
            ["get", "chaosblades.chaosblade.io", "-A", "-o", "json"]
        )
        chaos_items = chaos.get("items") if isinstance(chaos, dict) else None
        if not isinstance(chaos_items, list):
            raise RuntimeAdapterError("ChaosBlade inventory is invalid")
        own, foreign, unattributed = _partition_chaosblade_inventory(chaos_items, namespace)
        qualified = (
            load_desired >= 1
            and load_ready >= 1
            and desired == ready
            and len(own) == 0
        )
        return {
            "qualified": qualified,
            "application_namespace": namespace,
            "deployment_count": len(items),
            "desired_replicas": desired,
            "ready_replicas": ready,
            "built_in_load_generator_desired": load_desired,
            "built_in_load_generator_ready": load_ready,
            "active_chaosblade_count": len(own),
            "cluster_chaosblade_count": len(chaos_items),
            "foreign_chaosblade_count": len(foreign),
            "unattributed_chaosblade_names": [
                str((item.get("metadata") or {}).get("name") or "") for item in unattributed
            ],
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

    POLICY_FILE_STATE_KEY = "__resbench_mcp_policy_file__"
    POLICY_ROOT_STATE_KEY = "__resbench_mcp_policy_root__"

    SERVER_BY_CAPABILITY = {
        "mcp.chaos.create": "chaos_control",
        "mcp.chaos.destroy": "chaos_control",
        "mcp.k8s.read": "k8s_ro",
        "mcp.telemetry.read": "telemetry_ro",
        "mcp.source.read": "source_ro",
    }
    TOOL_BY_CAPABILITY = {
        "mcp.chaos.create": ("chaos_control", "chaos_create_experiment"),
        "mcp.chaos.destroy": ("chaos_control", "chaos_destroy_experiment"),
    }

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._original: dict[str, str] = {}
        self._policy_roots: dict[str, Path] = {}
        self.platform_ledger = PlatformLedger(self.root.parent / "platform-ledger")

    def initialize(self, trial_id: str, tokens: Mapping[str, str]) -> dict[str, str]:
        _validate_trial_id(trial_id)
        paths: dict[str, str] = {}
        for server, token in tokens.items():
            _validate_token(token)
            key = f"{trial_id}:{server}"
            self._original[key] = token
            path = self._path(trial_id, server)
            _atomic_token(path, token)
            paths[server] = str(path)
        return paths

    def register_policy_root(self, trial_id: str, root: Path) -> None:
        _validate_trial_id(trial_id)
        policy_root = Path(root).resolve()
        if not policy_root.is_dir():
            raise RuntimeAdapterError("MCP policy registry root was not initialized")
        self._policy_roots[trial_id] = policy_root

    def policy_registry(self, trial_id: str) -> CapabilityPolicyRegistry:
        _validate_trial_id(trial_id)
        root = self._policy_roots.get(trial_id)
        if root is None:
            raise RuntimeAdapterError("MCP policy registry was not initialized")
        return CapabilityPolicyRegistry(root, ledger=self.platform_ledger)

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
        _validate_trial_id(trial_id)
        if server not in {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel",
                          "coroot_ro", "chaos_mesh_control", "code_sandbox"}:
            raise RuntimeAdapterError("invalid token-state identity")
        directory = self.root / trial_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / f"{server}.token"


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

    def operation_uncertainty_status(self, trial_id: str) -> Mapping[str, Any]: ...


class RestorationTimer(Protocol):
    """The small subset of ``threading.Timer`` used by D5.

    Keeping this protocol deliberately small lets tests deterministically fire a
    pending restoration without shortening the production interruption window.
    """

    def start(self) -> None: ...

    def cancel(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


@dataclass
class _D5Restoration:
    record: DisturbanceRecord
    snapshot: CapabilityPolicyDocument
    registry: CapabilityPolicyRegistry
    duration_seconds: int
    timer: RestorationTimer | None = None
    completion: DisturbanceRecord | None = None
    failed: BaseException | None = None
    cancelled: bool = False
    restoring: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class CompositeDisturbanceExecutor:
    def __init__(
        self,
        *,
        kubernetes_client: KubernetesDisturbanceClient,
        mcp_tokens: McpTokenStateRegistry,
        target_rebinder: TargetCapabilityRebinder | None = None,
        mcp_supervisor: McpTransportController | None = None,
        policy_registry: CapabilityPolicyRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        timer_factory: Callable[[float, Callable[[], None]], RestorationTimer] | None = None,
        restoration_observer: Callable[[DisturbanceRecord], None] | None = None,
    ):
        self.kubernetes_client = kubernetes_client
        self.mcp_tokens = mcp_tokens
        self.target_rebinder = target_rebinder
        self.mcp_supervisor = mcp_supervisor
        self.policy_registry = policy_registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timer_factory = timer_factory or _threading_timer
        self.restoration_observer = restoration_observer
        self._d5_restorations: dict[tuple[str, str], _D5Restoration] = {}
        self._d5_lock = threading.RLock()

    def set_restoration_observer(
        self,
        observer: Callable[[DisturbanceRecord], None] | None,
    ) -> None:
        """Set the D5 completion sink used by the campaign event bridge."""
        self.restoration_observer = observer

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
            if capability.get("baseline_capability_rebound") is not True:
                raise RuntimeAdapterError(
                    "target capability rebind was not independently verified"
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
                evidence = self._revoke_mcp_capability(plan.trial_id, capability)
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
                self._revoke_mcp_capability(plan.trial_id, capability)
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
        if plan.type is DisturbanceType.TOOL_CHANNEL_INTERRUPTION:
            servers = tuple(str(item) for item in plan.parameters["servers"])
            duration = int(plan.parameters.get("duration_seconds") or 0)
            if not servers or not 1 <= duration <= 10:
                raise RuntimeAdapterError("MCP interruption must be bounded to 1-10 seconds")
            registry = self._policy_registry_for(plan.trial_id)
            snapshot = registry.snapshot()
            started_at = _utc_datetime(self.clock())
            unavailable_until = started_at + timedelta(seconds=duration)
            applied_sequences: dict[str, int] = {}
            for server in servers:
                document = registry.set_server(
                    server,
                    channel_unavailable_until=unavailable_until,
                    reason=plan.disturbance_id,
                    source="d5-channel-unavailable",
                )
                applied_sequences[server] = document.sequence
            record = DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "servers": servers,
                    "duration_seconds": duration,
                    "mechanism": "policy.channel_unavailable_until",
                    "channel_unavailable_until": unavailable_until.isoformat(),
                    "interruption": {
                        "policy_sequences": applied_sequences,
                        "started_at": started_at.isoformat(),
                        "verified": True,
                    },
                    "restoration": {
                        "status": "pending",
                        "verified": False,
                    },
                    # The snapshot is retained so a process that has already
                    # joined the timer can still perform a safe final rollback.
                    "policy_snapshot": snapshot.model_dump(mode="json"),
                    "verified": True,
                },
                rolled_back=False,
            )
            state = _D5Restoration(
                record=record,
                snapshot=snapshot,
                registry=registry,
                duration_seconds=duration,
            )
            key = self._d5_key(record)
            with self._d5_lock:
                if key in self._d5_restorations:
                    raise RuntimeAdapterError("D5 restoration is already pending for this trial")
                self._d5_restorations[key] = state
            try:
                timer = self.timer_factory(
                    duration,
                    lambda: self._complete_d5(state, source="d5-channel-restored"),
                )
                state.timer = timer
                timer.start()
            except BaseException:
                with self._d5_lock:
                    self._d5_restorations.pop(key, None)
                try:
                    registry.restore(snapshot, source="d5-channel-apply-failed")
                except BaseException as rollback_error:
                    raise RuntimeAdapterError(
                        "D5 timer could not start and policy restoration failed"
                    ) from rollback_error
                raise
            return record
        if plan.type is DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY:
            if self.mcp_supervisor is None:
                raise RuntimeAdapterError("MCP transport controller is unavailable")
            if not hasattr(self.mcp_supervisor, "operation_uncertainty_status"):
                raise RuntimeAdapterError("operation outcome status backend is unavailable")
            variant = plan.parameters.get("variant")
            if variant:
                registry = self._policy_registry_for(plan.trial_id)
                registry.set_server(
                    "chaos_control",
                    chaos_create_uncertainty_variant=str(variant),
                    reason=plan.disturbance_id,
                    source="d6-operation-outcome-uncertain",
                )
            status = dict(self.mcp_supervisor.operation_uncertainty_status(plan.trial_id))
            outcome = str(status.get("operation_outcome") or "unknown")
            operation_id = str(status.get("operation_id") or "")
            if outcome not in {"absent", "applied", "unknown"}:
                outcome = "unknown"
            ground_truth = dict(status.get("ground_truth") or {})
            if not ground_truth:
                ground_truth = {
                    "operation_id": operation_id,
                    "operation_outcome": outcome,
                    "source": "chaos_control_operation_status",
                }
            return DisturbanceRecord(
                plan=plan,
                applied=True,
                application_evidence={
                    "verified": status.get("ok") is True,
                    "operation_id": operation_id,
                    "operation_outcome": outcome,
                    "status": status,
                },
                ground_truth=ground_truth,
                rolled_back=True,
                rollback_evidence={
                    "one_shot_response_policy_consumed": True,
                    "persistent_response_policy_absent": True,
                },
            )
        raise RuntimeAdapterError("unsupported Stage-2 disturbance type")

    def rollback(self, record: DisturbanceRecord) -> DisturbanceRecord:
        if record.plan.type is DisturbanceType.PERMISSION_CHANGE:
            capability = str(record.plan.parameters["revoke_capability"])
            if record.plan.backend == "mcp_policy":
                evidence = self._restore_mcp_capabilities(record, (capability,))
            else:
                raise RuntimeAdapterError("unsupported permission restoration backend")
            return record.model_copy(
                update={"rolled_back": True, "rollback_evidence": evidence}
            )
        if record.plan.type is DisturbanceType.OBSERVABILITY_CHANGE:
            capabilities = tuple(
                str(item) for item in record.plan.parameters["revoke_capabilities"]
            )
            evidence = self._restore_mcp_capabilities(record, capabilities)
            return record.model_copy(
                update={"rolled_back": True, "rollback_evidence": evidence}
            )
        if record.plan.type is DisturbanceType.TOOL_CHANNEL_INTERRUPTION:
            return self._rollback_d5(record)
        if record.plan.type is DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY:
            return record
        return record.model_copy(
            update={
                "rolled_back": False,
                "rollback_evidence": {"deferred_to_environment_reset": True},
            }
        )

    def wait_for_restoration(
        self,
        record: DisturbanceRecord,
        *,
        timeout: float | None = None,
    ) -> DisturbanceRecord:
        """Join D5's bounded restoration and return its final evidence record.

        Campaign finalization must call this before it judges a D5 trial.  It is
        intentionally a no-op for all other disturbance types.
        """
        if record.plan.type is not DisturbanceType.TOOL_CHANNEL_INTERRUPTION:
            return record
        state = self._d5_state(record)
        if state is None:
            return record
        timer = state.timer
        if timer is not None:
            timer.join(timeout if timeout is not None else state.duration_seconds + 1)
            if timer.is_alive():
                raise RuntimeAdapterError("D5 restoration timer did not finish within its bound")
        return self._completed_d5_record(state)

    def _rollback_d5(self, record: DisturbanceRecord) -> DisturbanceRecord:
        state = self._d5_state(record)
        if state is None:
            # This is only expected after an executor restart.  The original
            # snapshot makes rollback safe without claiming an unseen timer did
            # anything.
            snapshot_payload = record.application_evidence.get("policy_snapshot")
            if not isinstance(snapshot_payload, Mapping):
                raise RuntimeAdapterError("D5 policy restoration snapshot is missing")
            try:
                restored = self._policy_registry_for(record.plan.trial_id).restore(
                    CapabilityPolicyDocument(**snapshot_payload),
                    source="d5-channel-rollback-recovered",
                )
            except BaseException as exc:
                raise RuntimeAdapterError("D5 policy restoration failed") from exc
            return self._d5_completed_record(record, restored, cause="rollback-recovered")

        timer = state.timer
        with state.lock:
            state.cancelled = True
        if timer is not None:
            timer.cancel()
            timer.join(state.duration_seconds + 1)
            if timer.is_alive():
                raise RuntimeAdapterError("D5 restoration timer remained alive after cancellation")
        self._complete_d5(state, source="d5-channel-rollback")
        return self._completed_d5_record(state)

    def _complete_d5(self, state: _D5Restoration, *, source: str) -> None:
        """Restore exactly once.  Timer exceptions become observable evidence."""
        with state.lock:
            if state.completion is not None or state.failed is not None:
                return
            if state.restoring:
                return
            state.restoring = True
        try:
            restored = state.registry.restore(state.snapshot, source=source)
            completion = self._d5_completed_record(state.record, restored, cause=source)
        except BaseException as exc:  # the timer thread must not hide this failure
            failure = self._d5_failed_record(state.record, exc, source=source)
            with state.lock:
                state.failed = exc
                state.completion = failure
                state.restoring = False
            self._notify_d5_restoration(failure)
            return
        with state.lock:
            state.completion = completion
            state.restoring = False
        self._notify_d5_restoration(completion)

    def _completed_d5_record(self, state: _D5Restoration) -> DisturbanceRecord:
        with state.lock:
            completion = state.completion
        if completion is None:
            raise RuntimeAdapterError("D5 restoration has not completed")
        # Return a record with ``rolled_back=False`` and explicit error evidence
        # on failure.  Callers can persist the fact before deciding whether the
        # trial is invalid; throwing here would hide the timer's evidence.
        return completion

    def _d5_completed_record(
        self,
        record: DisturbanceRecord,
        restored: CapabilityPolicyDocument,
        *,
        cause: str,
    ) -> DisturbanceRecord:
        evidence = dict(record.application_evidence)
        duration = int(evidence["duration_seconds"])
        servers = list(evidence["servers"])
        restored_at = _utc_datetime(self.clock())
        evidence["restoration"] = {
            "status": "restored",
            "policy_sequence": restored.sequence,
            "restored_at": restored_at.isoformat(),
            "verified": True,
            "source": cause,
        }
        evidence["channel_restored_feedback"] = {
            "event_type": "CHANNEL_RESTORED",
            "servers": servers,
            "interruption_seconds": duration,
            "retryable": True,
            "restored_at": restored_at.isoformat(),
        }
        return record.model_copy(
            update={
                "application_evidence": evidence,
                "rolled_back": True,
                "rollback_evidence": {
                    "policy_sequence": restored.sequence,
                    "restored_at": restored_at.isoformat(),
                    "verified": True,
                    "source": cause,
                },
            }
        )

    def _d5_failed_record(
        self,
        record: DisturbanceRecord,
        error: BaseException,
        *,
        source: str,
    ) -> DisturbanceRecord:
        evidence = dict(record.application_evidence)
        evidence["restoration"] = {
            "status": "failed",
            "verified": False,
            "source": source,
            "error_type": type(error).__name__,
        }
        return record.model_copy(
            update={
                "application_evidence": evidence,
                "rolled_back": False,
                "rollback_evidence": {
                    "verified": False,
                    "error_type": type(error).__name__,
                    "source": source,
                },
            }
        )

    def _notify_d5_restoration(self, record: DisturbanceRecord) -> None:
        observer = self.restoration_observer
        if observer is None:
            return
        try:
            observer(record)
        except BaseException as exc:
            # A notification failure cannot undo a completed policy restoration,
            # but it must remain visible to the finalizer rather than escaping a
            # timer thread.
            state = self._d5_state(record)
            if state is None:
                return
            with state.lock:
                evidence = dict(record.application_evidence)
                evidence["restoration_observer"] = {
                    "verified": False,
                    "error_type": type(exc).__name__,
                }
                state.completion = record.model_copy(update={"application_evidence": evidence})

    def _d5_key(self, record: DisturbanceRecord) -> tuple[str, str]:
        return record.plan.trial_id, record.plan.disturbance_id

    def _d5_state(self, record: DisturbanceRecord) -> _D5Restoration | None:
        with self._d5_lock:
            return self._d5_restorations.get(self._d5_key(record))

    def _policy_registry_for(self, trial_id: str) -> CapabilityPolicyRegistry:
        if self.policy_registry is not None:
            return self.policy_registry
        return self.mcp_tokens.policy_registry(trial_id)

    def _revoke_mcp_capability(self, trial_id: str, capability: str) -> dict[str, Any]:
        registry = self._policy_registry_for(trial_id)
        snapshot = registry.snapshot()
        token_evidence = self.mcp_tokens.revoke(trial_id, capability)
        server, tool = self._policy_target_for_capability(capability)
        if tool is None:
            document = registry.set_server(
                server,
                state="disabled",
                reason=capability,
                source="disturbance-runtime",
            )
        else:
            document = registry.set_tool(
                server,
                tool,
                state="disabled",
                reason=capability,
                source="disturbance-runtime",
            )
        policy_evidence: dict[str, Any] = {
            "server": server,
            "tool": tool,
            "sequence": document.sequence,
            "snapshot": snapshot.model_dump(mode="json"),
        }
        return {
            **token_evidence,
            "policy": policy_evidence,
        }

    def _restore_mcp_capabilities(
        self,
        record: DisturbanceRecord,
        capabilities: tuple[str, ...],
    ) -> dict[str, Any]:
        """Undo ``_revoke_mcp_capability`` for D1 and D3/D4 alike.

        The pre-revocation policy snapshot and the policy registry are resolved
        before any token or policy is written, so a malformed record fails
        without leaving a half-restored capability behind.
        """
        policy_snapshot = _earliest_policy_snapshot(_mcp_revocation_entries(record))
        if policy_snapshot is None:
            raise RuntimeAdapterError("MCP policy restoration snapshot is missing")
        registry = self._policy_registry_for(record.plan.trial_id)
        restored_tokens = [
            self.mcp_tokens.restore(record.plan.trial_id, capability)
            for capability in capabilities
        ]
        restored_policy = registry.restore(
            policy_snapshot,
            source="disturbance-runtime-rollback",
        )
        return {
            "restored": restored_tokens,
            "policy": {
                "sequence": restored_policy.sequence,
                "restored": True,
            },
            "verified": True,
        }

    def _policy_target_for_capability(self, capability: str) -> tuple[str, str | None]:
        tool_target = self.mcp_tokens.TOOL_BY_CAPABILITY.get(capability)
        if tool_target is not None:
            return tool_target
        server = self.mcp_tokens.SERVER_BY_CAPABILITY.get(capability)
        if server is None:
            raise RuntimeAdapterError(f"no MCP policy mapping for capability {capability}")
        return server, None


def _validate_token(token: str) -> None:
    if len(token) < 32 or any(character.isspace() for character in token):
        raise RuntimeAdapterError("MCP token must be at least 32 non-whitespace characters")


def _validate_trial_id(trial_id: str) -> None:
    # D0 qualification trials are sealed under a ``d0-`` campaign directory,
    # while ordinary Stage-2 campaigns use ``campaign-``.  Both prefixes are
    # Controller-generated identities; keep the same component/path checks
    # below so accepting the D0 form cannot widen the file-system boundary.
    if not isinstance(trial_id, str) or not (
        trial_id.startswith("campaign-") or trial_id.startswith("d0-")
    ):
        raise RuntimeAdapterError("invalid token-state identity")
    if trial_id.startswith("/") or "\\" in trial_id:
        raise RuntimeAdapterError("invalid token-state identity")
    components = trial_id.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise RuntimeAdapterError("invalid token-state identity")
    safe = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(any(character not in safe for character in component) for component in components):
        raise RuntimeAdapterError("invalid token-state identity")


def _mcp_revocation_entries(record: DisturbanceRecord) -> tuple[Mapping[str, Any], ...]:
    """Return the per-capability entries ``_revoke_mcp_capability`` recorded.

    Every entry carries the token registry's ``"revoked": True`` flag.  D1
    stores its single entry as the whole application evidence, while D3/D4
    wrap theirs in a ``"revoked"`` list, so the key name alone cannot tell the
    two shapes apart.  Decode by disturbance type, exactly as ``apply`` wrote
    the evidence and as the evaluator reads it.
    """
    evidence = record.application_evidence
    if record.plan.type is DisturbanceType.PERMISSION_CHANGE:
        entries: list[Any] = [evidence]
    elif record.plan.type is DisturbanceType.OBSERVABILITY_CHANGE:
        revoked = evidence.get("revoked")
        entries = list(revoked) if isinstance(revoked, (list, tuple)) else []
    else:
        raise RuntimeAdapterError("disturbance type has no MCP revocation evidence")
    if not entries or not all(isinstance(entry, Mapping) for entry in entries):
        raise RuntimeAdapterError("MCP revocation evidence is malformed")
    return tuple(entries)


def _earliest_policy_snapshot(
    entries: tuple[Mapping[str, Any], ...],
) -> CapabilityPolicyDocument | None:
    """Return the policy snapshot taken before the disturbance's first revocation."""
    snapshots: list[CapabilityPolicyDocument] = []
    for entry in entries:
        policy = entry.get("policy")
        snapshot = policy.get("snapshot") if isinstance(policy, Mapping) else None
        if snapshot:
            snapshots.append(CapabilityPolicyDocument(**snapshot))
    if not snapshots:
        return None
    return min(snapshots, key=lambda document: document.sequence)


def _threading_timer(delay_seconds: float, callback: Callable[[], None]) -> RestorationTimer:
    return threading.Timer(delay_seconds, callback)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
