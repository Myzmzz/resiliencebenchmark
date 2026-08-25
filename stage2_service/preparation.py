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

from mcp_servers.chaos_control.service import new_cleanup_handle

from .contracts import RuntimeTarget, TrialRuntimeContext


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

    def issue(self, trial_id: str, target: RuntimeTarget) -> str:
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
            "namespace": target.namespace,
            "target_name": target.name,
            "target_uid": target.uid,
            "controller_pod_uid": self.controller_pod_uid,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "traffic_evidence": evidence,
        }
        _atomic_json(self.ledger_dir / f"{token_hash}.json", payload)
        return token


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

    def prepare(self, trial_id: str, episode) -> TrialRuntimeContext:
        component = episode.internal.runtime_binding.component
        target = self._resolve_target("otel-demo", component)
        main_fault = episode.internal.main_fault.model_dump(mode="json")
        main_fault["target"] = {
            **dict(main_fault.get("target") or {}),
            "namespace": target.namespace,
            "component": target.component,
            "pod_name": target.name,
            "pod_uid": target.uid,
        }
        capability = self.capability_issuer.issue(trial_id, target)
        return TrialRuntimeContext(
            trial_id=trial_id,
            episode_id=episode.internal.identity.episode_id,
            target=target,
            main_fault=main_fault,
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
