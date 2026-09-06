"""Chaos Mesh backend with an executable Pod-UID fence.

Chaos Mesh selectors normally target a Pod name or labels, neither of which is
stable across a same-name Pod recreation.  Before creation this backend writes
a controller-owned, UID-derived label to the already UID-verified Pod and the
Chaos Mesh selector requires that label.  The label is removed only after the
ledger-owned resource is verified absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Mapping

from controller.safety import ChaosBladeAction
from mcp_servers.chaos_core.backends.chaosblade import _safe_kubectl_error
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


UID_FENCE_LABEL = "resbench.io/target-uid-fence"
TARGET_NAME_LABEL = "benchmark.target_name"
_CRDS = ("networkchaos.chaos-mesh.org", "podchaos.chaos-mesh.org", "stresschaos.chaos-mesh.org")


class ChaosMeshBackend:
    """Executor backend using fixed-argv kubectl calls for three Chaos Mesh CRDs."""

    def __init__(self, kubectl_path: str = "kubectl") -> None:
        self.kubectl_path = kubectl_path

    async def list_experiments(self, kubeconfig: str, namespace: str | None = None) -> list[ExperimentRecord]:
        records: list[ExperimentRecord] = []
        for crd in _CRDS:
            args = ["--kubeconfig", kubeconfig]
            if namespace:
                args.extend(["-n", namespace])
            args.extend(["get", crd, "-o", "json"])
            output = await self._kubectl(args)
            records.extend(_record_from_resource(item) for item in json.loads(output or "{}").get("items", []))
        return records

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        for crd in _CRDS:
            try:
                output = await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "get", crd, name, "-o", "json"])
            except ChaosControlError as exc:
                if exc.code == "KUBECTL_NOT_FOUND":
                    continue
                raise
            return _record_from_resource(json.loads(output or "{}"))
        return None

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        try:
            output = await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "get", "pod", name, "-o", "json"])
        except ChaosControlError as exc:
            if exc.code == "KUBECTL_NOT_FOUND":
                return None
            raise
        uid = json.loads(output or "{}").get("metadata", {}).get("uid")
        return str(uid) if uid else None

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        namespace = str(manifest["metadata"]["namespace"])
        name = str(manifest["metadata"]["name"])
        crd = _crd_for_manifest(manifest)
        existing = await self.get_experiment(namespace, name, kubeconfig)
        if existing is not None:
            raise ChaosControlError(
                "CHAOS_MESH_NAME_ALREADY_EXISTS",
                "A Chaos Mesh resource with this deterministic experiment name already exists.",
                next_step="Do not overwrite historical resources; use a fresh run_id or reconcile the existing resource.",
            )
        await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "create", "-f", "-"], stdin=json.dumps(manifest).encode())
        created = await self.get_experiment(namespace, name, kubeconfig)
        if created is None:
            raise ChaosControlError(
                "CREATE_NOT_OBSERVABLE",
                "Chaos Mesh create returned but the resource could not be read back.",
                next_step="Run chaos_mesh_inventory_run and inspect the Chaos Mesh controller event stream.",
            )
        return created

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        """Delete exactly one matching CRD, never every kind sharing a name."""
        matches: list[str] = []
        for crd in _CRDS:
            try:
                await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "get", crd, name, "-o", "json"])
            except ChaosControlError as exc:
                if exc.code == "KUBECTL_NOT_FOUND":
                    continue
                raise
            matches.append(crd)
        if len(matches) > 1:
            raise ChaosControlError(
                "AMBIGUOUS_CHAOS_MESH_RESOURCE",
                "More than one Chaos Mesh resource kind has this cleanup name.",
                next_step="Stop automatic cleanup and reconcile the ledger-owned resource manually.",
            )
        if matches:
            await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "delete", matches[0], name, "--ignore-not-found=true"])

    def render_manifest(self, name: str, action: ChaosBladeAction) -> dict[str, Any]:
        """Render only the supported official Chaos Mesh CRD forms."""
        labels = {
            OWNER_LABEL: OWNER_VALUE,
            RUN_ID_LABEL: action.run_id,
            TARGET_UID_LABEL: action.target.uid,
            TARGET_NAME_LABEL: action.target.name,
            NAMESPACE_LABEL: action.namespace,
            FAULT_TYPE_LABEL: action.fault_type,
        }
        selector = {
            "namespaces": [action.namespace],
            "labelSelectors": {UID_FENCE_LABEL: _fence_value(action.target.uid)},
        }
        metadata = {"name": name, "namespace": action.namespace, "labels": labels}
        if action.fault_type == "network-delay":
            return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "NetworkChaos", "metadata": metadata,
                    "spec": {"action": "delay", "mode": "one", "selector": selector, "direction": "to",
                             "delay": {"latency": f"{int(action.intensity['delay_ms'])}ms"}, "duration": f"{action.duration_seconds}s"}}
        if action.fault_type == "network-loss":
            return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "NetworkChaos", "metadata": metadata,
                    "spec": {"action": "loss", "mode": "one", "selector": selector, "direction": "to",
                             "loss": {"loss": str(action.intensity['loss_percent'])}, "duration": f"{action.duration_seconds}s"}}
        if action.fault_type in {"cpu-load", "memory-stress"}:
            stressors: dict[str, Any]
            if action.fault_type == "cpu-load":
                stressors = {"cpu": {"workers": 1, "load": int(action.intensity["cpu_percent"])}}
            else:
                stressors = {"memory": {"workers": 1, "size": f"{int(action.intensity['mem_percent'])}%"}}
            return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "StressChaos", "metadata": metadata,
                    "spec": {"mode": "one", "selector": selector, "stressors": stressors, "duration": f"{action.duration_seconds}s"}}
        if action.fault_type == "pod-kill":
            return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodChaos", "metadata": metadata,
                    "spec": {"action": "pod-kill", "mode": "one", "selector": selector, "duration": f"{action.duration_seconds}s"}}
        raise ChaosControlError("FAULT_TYPE_NOT_SUPPORTED_BY_EXECUTOR", "The requested fault type is not supported by Chaos Mesh.", next_step="Choose network-delay, network-loss, cpu-load, memory-stress, or pod-kill.")

    async def prepare_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """Atomically test the target UID and install the selector fence label.

        Chaos Mesh v2.8's ``selector.Pods`` path short-circuits generic
        selectors.  Therefore this backend deliberately uses only namespace
        plus labelSelectors and never emits ``pods``.
        """
        pod = await self._get_pod(namespace, name, kubeconfig)
        if pod is None or str((pod.get("metadata") or {}).get("uid") or "") != uid:
            raise ChaosControlError("TARGET_UID_MISMATCH", "The target Pod changed before the Chaos Mesh UID fence could be installed.", next_step="Refresh target identity and re-plan.")
        labels = dict((pod.get("metadata") or {}).get("labels") or {})
        value = _fence_value(uid)
        existing = labels.get(UID_FENCE_LABEL)
        if existing not in (None, value):
            raise ChaosControlError("TARGET_FENCE_CONFLICT", "The target Pod already has a conflicting controller fence label.", next_step="Stop and reconcile the conflicting target fence before creating a fault.")
        if existing == value:
            return
        path = "/metadata/labels/" + UID_FENCE_LABEL.replace("~", "~0").replace("/", "~1")
        operations: list[dict[str, Any]] = [{"op": "test", "path": "/metadata/uid", "value": uid}]
        if labels:
            operations.append({"op": "add", "path": path, "value": value})
        else:
            operations.append({"op": "add", "path": "/metadata/labels", "value": {UID_FENCE_LABEL: value}})
        await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "patch", "pod", name, "--type=json", "-p", json.dumps(operations, separators=(",", ":"))])

    async def clear_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        """Atomically remove only this UID's fence; a recreated Pod is untouched."""
        pod = await self._get_pod(namespace, name, kubeconfig)
        if pod is None:
            return
        metadata = pod.get("metadata") or {}
        if str(metadata.get("uid") or "") != uid:
            return
        labels = dict(metadata.get("labels") or {})
        value = _fence_value(uid)
        if labels.get(UID_FENCE_LABEL) != value:
            return
        path = "/metadata/labels/" + UID_FENCE_LABEL.replace("~", "~0").replace("/", "~1")
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": path, "value": value},
            {"op": "remove", "path": path},
        ]
        try:
            await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "patch", "pod", name, "--type=json", "-p", json.dumps(operations, separators=(",", ":"))])
        except ChaosControlError:
            # A target recreated after the read is safe to leave alone; any
            # other patch failure is still material and must be surfaced.
            latest = await self._get_pod(namespace, name, kubeconfig)
            if latest is None or str((latest.get("metadata") or {}).get("uid") or "") != uid:
                return
            raise

    async def _get_pod(self, namespace: str, name: str, kubeconfig: str) -> Mapping[str, Any] | None:
        try:
            output = await self._kubectl(["--kubeconfig", kubeconfig, "-n", namespace, "get", "pod", name, "-o", "json"])
        except ChaosControlError as exc:
            if exc.code == "KUBECTL_NOT_FOUND":
                return None
            raise
        return json.loads(output or "{}")

    async def _kubectl(self, args: list[str], *, stdin: bytes | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(self.kubectl_path, *args, stdin=asyncio.subprocess.PIPE if stdin else None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate(stdin)
        if proc.returncode:
            detail = _safe_kubectl_error(stderr.decode(errors="replace"))
            if "notfound" in detail.lower() or "not found" in detail.lower():
                raise ChaosControlError("KUBECTL_NOT_FOUND", "Kubernetes resource was not found.", next_step="Refresh inventory and retry with a current resource name.")
            raise ChaosControlError("KUBECTL_FAILED", f"kubectl failed for a fixed Chaos Mesh operation: {detail}", next_step="Verify kubeconfig path, RBAC for Chaos Mesh CRs, and the namespace allowlist.")
        return stdout.decode()


class InMemoryChaosMeshBackend:
    """In-memory Chaos Mesh backend used for no-cluster unit tests."""

    def __init__(self, *, pod_uids: Mapping[tuple[str, str], str] | None = None) -> None:
        self.experiments: dict[tuple[str, str], ExperimentRecord] = {}
        self.pod_uids = dict(pod_uids or {})
        self.pod_labels: dict[tuple[str, str], dict[str, str]] = {}
        self.json_patches: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.created_manifests: list[Mapping[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    async def list_experiments(self, kubeconfig: str, namespace: str | None = None) -> list[ExperimentRecord]:
        values = list(self.experiments.values())
        return values if namespace is None else [item for item in values if item.namespace == namespace]

    async def get_experiment(self, namespace: str, name: str, kubeconfig: str) -> ExperimentRecord | None:
        return self.experiments.get((namespace, name))

    async def get_pod_uid(self, namespace: str, name: str, kubeconfig: str) -> str | None:
        return self.pod_uids.get((namespace, name))

    async def create_experiment(self, manifest: Mapping[str, Any], kubeconfig: str) -> ExperimentRecord:
        self.created_manifests.append(manifest)
        labels = dict(manifest["metadata"]["labels"])
        namespace = str(manifest["metadata"]["namespace"])
        target_name = labels[TARGET_NAME_LABEL]
        selector = manifest["spec"]["selector"]
        if "pods" in selector or not _selector_matches_pod(selector, namespace, self.pod_labels.get((namespace, target_name), {})):
            raise ChaosControlError("TARGET_FENCE_NOT_MATCHED", "Chaos Mesh selector does not match the UID-fenced target Pod.", next_step="Refresh target binding and recreate the controlled fence.")
        record = ExperimentRecord(name=str(manifest["metadata"]["name"]), namespace=namespace, run_id=labels[RUN_ID_LABEL], target_name=target_name, target_uid=labels[TARGET_UID_LABEL], fault_type=labels[FAULT_TYPE_LABEL], phase="Running", owner=labels[OWNER_LABEL], labels=labels, raw=dict(manifest))
        self.experiments[(record.namespace, record.name)] = record
        return record

    async def delete_experiment(self, namespace: str, name: str, kubeconfig: str) -> None:
        self.deleted.append((namespace, name))
        self.experiments.pop((namespace, name), None)

    def render_manifest(self, name: str, action: ChaosBladeAction) -> dict[str, Any]:
        return ChaosMeshBackend().render_manifest(name, action)

    async def prepare_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        if self.pod_uids.get((namespace, name)) != uid:
            raise ChaosControlError("TARGET_UID_MISMATCH", "The target Pod changed before the Chaos Mesh UID fence could be installed.", next_step="Refresh target identity and re-plan.")
        labels = self.pod_labels.setdefault((namespace, name), {})
        existing = labels.get(UID_FENCE_LABEL)
        if existing not in (None, _fence_value(uid)):
            raise ChaosControlError("TARGET_FENCE_CONFLICT", "The target Pod already has a conflicting controller fence label.", next_step="Stop and reconcile the conflicting target fence before creating a fault.")
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "add", "path": "/metadata/labels/" + UID_FENCE_LABEL.replace("/", "~1"), "value": _fence_value(uid)},
        ]
        self.json_patches.append((namespace, name, operations))
        labels[UID_FENCE_LABEL] = _fence_value(uid)

    async def clear_target_fence(self, namespace: str, name: str, uid: str, kubeconfig: str) -> None:
        labels = self.pod_labels.setdefault((namespace, name), {})
        if self.pod_uids.get((namespace, name)) == uid and labels.get(UID_FENCE_LABEL) == _fence_value(uid):
            self.json_patches.append((namespace, name, [{"op": "test", "path": "/metadata/uid", "value": uid}, {"op": "test", "path": "/metadata/labels/" + UID_FENCE_LABEL.replace("/", "~1"), "value": _fence_value(uid)}, {"op": "remove", "path": "/metadata/labels/" + UID_FENCE_LABEL.replace("/", "~1")}]))
            labels.pop(UID_FENCE_LABEL, None)


def _record_from_resource(resource: Mapping[str, Any]) -> ExperimentRecord:
    metadata = resource.get("metadata") or {}
    labels = dict(metadata.get("labels") or {})
    status = resource.get("status") or {}
    experiment = status.get("experiment") or {}
    phase = str(experiment.get("desiredPhase") or experiment.get("phase") or status.get("phase") or "Unknown")
    return ExperimentRecord(name=str(metadata.get("name") or ""), namespace=str(metadata.get("namespace") or labels.get(NAMESPACE_LABEL) or ""), run_id=str(labels.get(RUN_ID_LABEL) or ""), target_name=str(labels.get(TARGET_NAME_LABEL) or ""), target_uid=str(labels.get(TARGET_UID_LABEL) or ""), fault_type=str(labels.get(FAULT_TYPE_LABEL) or ""), phase=phase, owner=labels.get(OWNER_LABEL), labels=labels, raw=dict(resource))


def _crd_for_manifest(manifest: Mapping[str, Any]) -> str:
    return {"NetworkChaos": _CRDS[0], "PodChaos": _CRDS[1], "StressChaos": _CRDS[2]}[str(manifest["kind"])]


def _fence_value(uid: str) -> str:
    """Return a valid fixed-length label value for any accepted Kubernetes UID."""
    return "uid-" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:40]


def _selector_matches_pod(selector: Mapping[str, Any], namespace: str, labels: Mapping[str, str]) -> bool:
    """Model Chaos Mesh's generic-selector path used when ``pods`` is absent."""
    namespaces = selector.get("namespaces") or []
    required = selector.get("labelSelectors") or {}
    return namespace in namespaces and all(labels.get(key) == value for key, value in required.items())
