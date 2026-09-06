"""ChaosBlade backend and resource-shape helpers for the shared chaos core."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from controller.safety import ChaosBladeAction
from mcp_servers.chaos_core.contracts import (
    FAULT_TYPE_LABEL,
    NAMESPACE_LABEL,
    OWNER_LABEL,
    OWNER_VALUE,
    RUN_ID_LABEL,
    TARGET_UID_LABEL,
    ChaosControlError,
    ExperimentRecord,
)


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

    def render_manifest(self, name: str, action: ChaosBladeAction) -> dict[str, Any]:
        """Render the existing cluster-scoped ChaosBlade resource."""
        return _manifest(name, action)

    async def prepare_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """ChaosBlade embeds the validated UID as an ownership label; no Pod mutation is needed."""

    async def clear_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """ChaosBlade has no target-Pod fence to remove."""

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
    """Fake backend for unit tests and local integration checks."""

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

    def render_manifest(self, name: str, action: ChaosBladeAction) -> dict[str, Any]:
        """Render the established ChaosBlade-shaped test manifest."""
        return _manifest(name, action)

    async def prepare_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """In-memory ChaosBlade tests need no target-Pod mutation."""

    async def clear_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """In-memory ChaosBlade tests need no target-Pod mutation."""


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


def _safe_kubectl_error(stderr: str) -> str:
    text = " ".join(stderr.split())
    return text[:500] if text else "no stderr"
