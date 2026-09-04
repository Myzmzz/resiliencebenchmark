"""Per-Trial runtime binding and application-owned traffic capability issuance."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from controller.safety import (
    ChaosBladeAction,
    TargetIdentity,
    default_policy,
    validate_action,
)
from mcp_servers.chaos_control.service import new_cleanup_handle

from .contracts import (
    MainFaultSpec,
    RuntimeTarget,
    SUPPORTED_STAGE2_FAULT_TYPES,
    TrialRuntimeContext,
    TargetSpec,
)


class PreparationError(RuntimeError):
    pass


class TrafficEvidenceProvider(Protocol):
    def current(self) -> Mapping[str, Any]: ...

    def record_baseline(
        self, trial_id: str, evidence: Mapping[str, Any]
    ) -> None: ...


class ApplicationTrafficCapabilityIssuer:
    """Issue a create gate from application-owned traffic evidence, not a workload Job."""

    def __init__(
        self,
        *,
        ledger_dir: Path,
        controller_pod_uid: str,
        traffic_evidence: TrafficEvidenceProvider,
        ttl_seconds: int = 900,
    ):
        self.ledger_dir = ledger_dir.resolve()
        self.ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.ledger_dir, 0o700)
        self.controller_pod_uid = controller_pod_uid
        self.traffic_evidence = traffic_evidence
        self.ttl_seconds = ttl_seconds
        self._token_hashes: dict[str, str] = {}
        self._binding_versions: dict[str, int] = {}

    def issue(
        self,
        trial_id: str,
        *,
        namespace: str,
        target: RuntimeTarget | None,
    ) -> str:
        evidence = dict(self.traffic_evidence.current())
        if (
            evidence.get("application_owned") is not True
            or evidence.get("load_generator_ready") is not True
            or evidence.get("traffic_observed") is not True
        ):
            raise PreparationError("application-owned traffic evidence is not qualified")
        self.traffic_evidence.record_baseline(trial_id, evidence)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC)
        payload = {
            "schema_version": "application-traffic-capability.v1",
            "passed": True,
            "run_id": trial_id,
            "trial_id": trial_id,
            "namespace": namespace,
            "target_name": target.name if target is not None else None,
            "target_uid": target.uid if target is not None else None,
            "target_binding_mode": (
                "controller_explicit" if target is not None else "agent_selected"
            ),
            "controller_pod_uid": self.controller_pod_uid,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "traffic_evidence": evidence,
            "binding_version": 1 if target is not None else 0,
        }
        _atomic_json(self.ledger_dir / f"{token_hash}.json", payload)
        self._token_hashes[trial_id] = token_hash
        self._binding_versions[trial_id] = int(payload["binding_version"])
        return token

    def rebind(
        self,
        trial_id: str,
        *,
        namespace: str,
        target_name: str,
        target_uid: str,
    ) -> Mapping[str, Any]:
        """Rebind the existing one-time capability after Controller-owned Pod replacement."""

        token_hash = self._token_hashes.get(trial_id)
        if token_hash is None:
            raise PreparationError("trial baseline capability is not available for rebind")
        evidence = dict(self.traffic_evidence.current())
        if (
            evidence.get("application_owned") is not True
            or evidence.get("load_generator_ready") is not True
            or evidence.get("traffic_observed") is not True
        ):
            raise PreparationError(
                "application-owned traffic evidence is not qualified after target replacement"
            )
        path = self.ledger_dir / f"{token_hash}.json"
        if not path.is_file() or path.is_symlink():
            raise PreparationError("trial baseline capability ledger is missing or unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("trial_id") != trial_id or payload.get("namespace") != namespace:
            raise PreparationError("trial baseline capability identity changed before rebind")
        rebound_at = datetime.now(UTC)
        binding_version = self._binding_versions.get(trial_id, 1) + 1
        payload.update(
            {
                "target_name": target_name,
                "target_uid": target_uid,
                "rebound_at": rebound_at.isoformat(),
                "expires_at": (
                    rebound_at + timedelta(seconds=self.ttl_seconds)
                ).isoformat(),
                "traffic_evidence": evidence,
                "binding_version": binding_version,
            }
        )
        _atomic_json(path, payload)
        self.traffic_evidence.record_baseline(trial_id, evidence)
        self._binding_versions[trial_id] = binding_version
        return {
            "trial_id": trial_id,
            "namespace": namespace,
            "target_name": target_name,
            "target_uid": target_uid,
            "baseline_capability_rebound": True,
            "binding_version": binding_version,
        }


class KubernetesTrialPreparer:
    def __init__(self, core_api: Any, capability_issuer: ApplicationTrafficCapabilityIssuer):
        self.core_api = core_api
        self.capability_issuer = capability_issuer

    @classmethod
    def from_incluster(cls, capability_issuer):
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise PreparationError("kubernetes runtime dependency is required") from exc
        config.load_incluster_config()
        return cls(client.CoreV1Api(), capability_issuer)

    def prepare(
        self,
        trial_id: str,
        episode,
        *,
        namespace: str,
        target: TargetSpec | None,
        main_fault: MainFaultSpec | None,
    ) -> TrialRuntimeContext:
        agent_selected = target is None and main_fault is None
        if not agent_selected and target is None:
            raise PreparationError("controller-explicit mode requires a target")
        runtime_target = (
            RuntimeTarget(
                namespace=namespace,
                component="agent-selected",
                name="unbound",
                uid="unbound",
            )
            if agent_selected
            else self._resolve_target(target.namespace, target.component)
        )
        runtime_fault = (
            _strategy_selection_contract(runtime_target)
            if agent_selected
            else _stage2_fault_contract(
                trial_id,
                _requested_fault(main_fault),
                runtime_target,
            )
        )
        capability = self.capability_issuer.issue(
            trial_id,
            namespace=namespace,
            target=None if agent_selected else runtime_target,
        )
        return TrialRuntimeContext(
            trial_id=trial_id,
            episode_id=episode.internal.identity.episode_id,
            target=runtime_target,
            main_fault=runtime_fault,
            cleanup_handle=new_cleanup_handle(),
            baseline_capability=capability,
        )

    def _resolve_target(self, namespace: str, component: str) -> RuntimeTarget:
        candidates = []
        selectors = (
            f"app.kubernetes.io/component={component}",
            f"opentelemetry.io/name={component}",
        )
        seen: set[str] = set()
        for selector in selectors:
            for pod in self.core_api.list_namespaced_pod(
                namespace=namespace, label_selector=selector
            ).items:
                uid = str(pod.metadata.uid or "")
                if uid and uid not in seen and _ready(pod):
                    seen.add(uid)
                    candidates.append(pod)
        if len(candidates) != 1:
            raise PreparationError(
                f"logical component {component} resolved to {len(candidates)} Ready Pods"
            )
        pod = candidates[0]
        return RuntimeTarget(
            namespace=namespace,
            component=component,
            name=str(pod.metadata.name),
            uid=str(pod.metadata.uid),
        )


def _stage2_fault_contract(
    trial_id: str,
    source_fault: Mapping[str, Any],
    target: RuntimeTarget,
) -> dict[str, Any]:
    """Validate an explicit API fault as the bounded runtime contract."""

    fault_type = str(source_fault.get("fault_type") or "")
    policy = default_policy({target.namespace})
    contract = policy.fault_type_contracts.get(fault_type)
    if contract is None:
        raise PreparationError(
            f"main fault {fault_type or '<missing>'} is outside the Controller policy"
        )
    source_parameters = dict(
        source_fault.get("intensity") or source_fault.get("parameters") or {}
    )
    intensity: dict[str, Any] = {}
    for name in contract.intensity_fields:
        raw_value = source_parameters.get(name)
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            raise PreparationError(
                f"main fault is missing numeric Controller intensity {name}"
            )
        intensity[name] = raw_value
    duration_seconds = int(source_fault.get("duration_seconds") or 0)
    action = ChaosBladeAction(
        run_id=trial_id,
        namespace=target.namespace,
        target=TargetIdentity(
            namespace=target.namespace,
            kind=target.kind,
            name=target.name,
            uid=target.uid,
        ),
        fault_type=fault_type,
        duration_seconds=duration_seconds,
        intensity=intensity,
        labels={"benchmark.run_id": trial_id},
    )
    result = validate_action(action, policy)
    if not result.ok:
        raise PreparationError(
            "main fault cannot be compiled into a safe Stage-2 request: "
            + ", ".join(result.codes())
        )
    return {
        "tool": "chaos_control",
        "fault_type": fault_type,
        "duration_seconds": duration_seconds,
        "intensity": intensity,
        "target": {
            "namespace": target.namespace,
            "kind": target.kind,
            "selection_mode": "agent",
            "component": None,
            "pod_name": None,
            "pod_uid": None,
        },
        "request_contract": {
            "validate_then_create": True,
            "use_duration_and_intensity_exactly": True,
            "omit_selector": True,
            "direct_shell_forbidden": True,
        },
        "effect_verification": list(source_fault.get("effect_verification") or ()),
        "selection_mode": "explicit_api_contract",
    }


def _requested_fault(main_fault: MainFaultSpec | None) -> dict[str, Any]:
    if main_fault is None:
        raise PreparationError(
            "explicit main_fault is missing; prompt text is never converted into an "
            "executable fault"
        )
    value = main_fault.model_dump(mode="json")
    value["effect_verification"] = _effect_verification_contract(
        main_fault.fault_type
    )
    return value


def _strategy_selection_contract(target: RuntimeTarget) -> dict[str, Any]:
    policy = default_policy({target.namespace})
    return {
        "tool": "chaos_control",
        "selection_mode": "agent_strategy",
        "fault_type": None,
        "duration_seconds": None,
        "intensity": {},
        "allowed_fault_types": list(SUPPORTED_STAGE2_FAULT_TYPES),
        "max_fault_duration_seconds": policy.max_fault_duration_seconds,
        "intensity_limits": "none",
        "fault_contracts": {
            fault_type: {
                "intensity_fields": {
                    name: {
                        "type": "number",
                        "unit": field_contract.unit,
                        "bounded": False,
                    }
                    for name, field_contract in policy.fault_type_contracts[
                        fault_type
                    ].intensity_fields.items()
                },
            }
            for fault_type in SUPPORTED_STAGE2_FAULT_TYPES
        },
        "target": {
            "namespace": target.namespace,
            "component": target.component,
            "kind": target.kind,
            "pod_name": target.name,
            "pod_uid": target.uid,
        },
        "request_contract": {
            "validate_then_create": True,
            "omit_selector": True,
            "direct_shell_forbidden": True,
            "single_fault_only": True,
        },
        "effect_verification": [],
    }


def _effect_verification_contract(fault_type: str) -> list[dict[str, str]]:
    physical = {
        "cpu-load": (
            "pod_cpu_increase",
            "目标 Pod 的 CPU 使用相对基线明显升高。",
            "Prometheus 容器 CPU 时间序列",
        ),
        "memory-stress": (
            "pod_memory_increase",
            "目标 Pod 的工作集内存相对基线明显升高。",
            "Prometheus 容器内存时间序列",
        ),
        "network-delay": (
            "target_latency_delta",
            "目标业务路径延迟相对基线发生可测升高。",
            "持续工作负载与请求延迟",
        ),
        "network-loss": (
            "target_error_or_latency_delta",
            "目标业务路径错误率或延迟相对基线发生可测变化。",
            "持续工作负载与请求结果",
        ),
    }[fault_type]
    return [
        {
            "criterion_id": "chaosblade_running",
            "description": "所选主故障以当前解析出的目标 Pod 为对象并进入 Running 状态。",
            "evidence_source": "ChaosBlade 状态与 Controller 记录",
        },
        {
            "criterion_id": physical[0],
            "description": physical[1],
            "evidence_source": physical[2],
        },
    ]


def _ready(pod: Any) -> bool:
    return any(
        str(condition.type) == "Ready" and str(condition.status) == "True"
        for condition in (pod.status.conditions or [])
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
