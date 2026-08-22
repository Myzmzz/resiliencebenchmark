"""Restricted read-only Kubernetes access for benchmark agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol


KUBECONFIG_ENV = "RESBENCH_K8S_RO_KUBECONFIG"
NAMESPACE_ALLOWLIST_ENV = "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST"
KUBECTL_ENV = "RESBENCH_KUBECTL"
TIMEOUT_ENV = "RESBENCH_K8S_RO_TIMEOUT_SECONDS"

MAX_RESPONSE_CHARS = 25_000
MAX_RAW_BYTES = 2_000_000
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_LOG_TAIL = 500
MAX_LOG_SINCE_SECONDS = 6 * 60 * 60

K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
RESOURCE_RE = re.compile(r"^[a-z][a-z0-9.-]{1,80}$")
LABEL_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_.\-/=!,()]+$")

NAMESPACED_RESOURCES: Mapping[str, str] = {
    "pods": "pods",
    "deployments": "deployments.apps",
    "statefulsets": "statefulsets.apps",
    "daemonsets": "daemonsets.apps",
    "replicasets": "replicasets.apps",
    "services": "services",
    "endpoints": "endpoints",
    "jobs": "jobs.batch",
    "configmaps": "configmaps",
}
CLUSTER_RESOURCES: Mapping[str, str] = {
    "nodes": "nodes",
    "namespaces": "namespaces",
    "crds": "customresourcedefinitions.apiextensions.k8s.io",
}
FORBIDDEN_RESOURCES = {"secret", "secrets", "serviceaccounts", "roles", "rolebindings", "clusterroles", "clusterrolebindings"}
SENSITIVE_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|credential)", re.IGNORECASE)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)
SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)


class K8sROError(ValueError):
    """Structured user-facing error for k8s_ro tools."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "action": self.action}


def error_envelope(exc: K8sROError) -> dict[str, Any]:
    return {"ok": False, "error": exc.to_dict()}


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_RESPONSE_CHARS:
        payload = dict(payload)
        payload["truncated"] = True
        payload["items"] = payload.get("items", [])[:10]
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(text) > MAX_RESPONSE_CHARS:
            raise K8sROError(
                "response_too_large",
                "Kubernetes response exceeded the k8s_ro size limit after truncation.",
                "Use a smaller limit, narrower namespace, resource name, or label selector.",
            )
    return {"ok": True, **payload}


@dataclass(frozen=True)
class RuntimeConfig:
    kubeconfig: str
    namespace_allowlist: frozenset[str]
    kubectl_path: str = "kubectl"
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        values = os.environ if env is None else env
        kubeconfig = values.get(KUBECONFIG_ENV)
        if not kubeconfig:
            raise K8sROError(
                "missing_kubeconfig",
                f"{KUBECONFIG_ENV} is not configured for the k8s_ro server.",
                "Set the kubeconfig path in the MCP service environment; tools never accept kubeconfig parameters.",
            )
        kubeconfig_path = Path(kubeconfig)
        if not kubeconfig_path.is_absolute() or not kubeconfig_path.is_file():
            raise K8sROError(
                "invalid_kubeconfig",
                f"{KUBECONFIG_ENV} must point to an absolute existing kubeconfig file.",
                "Configure a server-owned absolute kubeconfig path before starting k8s_ro.",
            )
        timeout = _parse_timeout(values.get(TIMEOUT_ENV, "5"))
        namespaces = frozenset(
            item.strip() for item in values.get(NAMESPACE_ALLOWLIST_ENV, "").split(",") if item.strip()
        )
        if not namespaces:
            raise K8sROError(
                "missing_namespace_allowlist",
                f"{NAMESPACE_ALLOWLIST_ENV} is empty.",
                "Set the single namespace assigned to this Episode, such as train-ticket.",
            )
        if len(namespaces) != 1:
            raise K8sROError(
                "invalid_namespace_scope",
                f"{NAMESPACE_ALLOWLIST_ENV} must contain exactly one Episode namespace.",
                "Start a separate k8s_ro process with a fresh token for each benchmark Episode.",
            )
        for namespace in namespaces:
            _validate_k8s_name(namespace, "namespace")
        return cls(
            kubeconfig=str(kubeconfig_path),
            namespace_allowlist=namespaces,
            kubectl_path=values.get(KUBECTL_ENV, "kubectl"),
            timeout_seconds=timeout,
        )


class KubectlRunner(Protocol):
    async def run(self, argv: list[str], *, timeout_seconds: float) -> str:
        """Run a fixed argv command and return stdout."""


class SubprocessKubectlRunner:
    async def run(self, argv: list[str], *, timeout_seconds: float) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise K8sROError(
                "kubectl_timeout",
                "kubectl read-only operation exceeded the k8s_ro timeout.",
                "Use a narrower query or check API server health from the MCP host.",
            ) from exc
        if len(stdout) > MAX_RAW_BYTES:
            raise K8sROError(
                "upstream_response_too_large",
                "kubectl returned more data than k8s_ro allows.",
                "Use a narrower selector or lower limit.",
            )
        if proc.returncode != 0:
            detail = _safe_error(stderr.decode(errors="replace"))
            if "notfound" in detail.lower() or "not found" in detail.lower():
                raise K8sROError("resource_not_found", "Kubernetes resource was not found.", "Refresh inventory and retry.")
            raise K8sROError(
                "kubectl_failed",
                f"kubectl failed for a fixed read-only operation: {detail}",
                "Verify server kubeconfig, RBAC, resource allowlist, and namespace allowlist.",
            )
        return stdout.decode()


class K8sROService:
    def __init__(self, config: RuntimeConfig, runner: KubectlRunner | None = None) -> None:
        self.config = config
        self.runner = runner or SubprocessKubectlRunner()

    async def get_resource(self, *, namespace: str, resource: str, name: str) -> dict[str, Any]:
        namespace = self._namespace(namespace)
        kubectl_resource = _namespaced_resource(resource)
        _validate_k8s_name(name, "name")
        raw = await self._run(self._base(namespace) + ["get", kubectl_resource, name, "-o", "json"])
        return envelope({"resource": resource, "namespace": namespace, "name": name, "object": _sanitize(_loads_json(raw))})

    async def list_resources(
        self,
        *,
        namespace: str,
        resource: str,
        label_selector: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        namespace = self._namespace(namespace)
        kubectl_resource = _namespaced_resource(resource)
        limit = _limit(limit)
        offset = _offset(offset)
        argv = self._base(namespace) + ["get", kubectl_resource, "-o", "json"]
        if label_selector:
            argv.extend(["-l", _label_selector(label_selector)])
        data = _loads_json(await self._run(argv))
        items = [_summarize_namespaced_item(item, resource) for item in data.get("items", [])]
        page = items[offset : offset + limit]
        return envelope(
            {
                "resource": resource,
                "namespace": namespace,
                "total": len(items),
                "returned": len(page),
                "limit": limit,
                "offset": offset,
                "hasMore": offset + limit < len(items),
                "items": page,
            }
        )

    async def list_events(
        self,
        *,
        namespace: str,
        involved_object_kind: str | None = None,
        involved_object_name: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        namespace = self._namespace(namespace)
        limit = _limit(limit)
        offset = _offset(offset)
        fields: list[str] = []
        if involved_object_kind:
            fields.append(f"involvedObject.kind={_simple_ref(involved_object_kind, 'involved_object_kind')}")
        if involved_object_name:
            fields.append(f"involvedObject.name={_simple_ref(involved_object_name, 'involved_object_name')}")
        argv = self._base(namespace) + ["get", "events", "-o", "json"]
        if fields:
            argv.extend(["--field-selector", ",".join(fields)])
        data = _loads_json(await self._run(argv))
        items = [_sanitize_event(item) for item in data.get("items", [])]
        page = items[offset : offset + limit]
        return envelope(
            {
                "namespace": namespace,
                "total": len(items),
                "returned": len(page),
                "limit": limit,
                "offset": offset,
                "hasMore": offset + limit < len(items),
                "items": page,
            }
        )

    async def pod_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str | None = None,
        since_seconds: int = 3600,
        tail_lines: int = 200,
    ) -> dict[str, Any]:
        namespace = self._namespace(namespace)
        _validate_k8s_name(pod, "pod")
        tail_lines = _bounded_int(tail_lines, 1, MAX_LOG_TAIL, "tail_lines")
        since_seconds = _bounded_int(since_seconds, 1, MAX_LOG_SINCE_SECONDS, "since_seconds")
        argv = self._base(namespace) + ["logs", pod, "--since", f"{since_seconds}s", "--tail", str(tail_lines)]
        if container:
            argv.extend(["-c", _simple_ref(container, "container")])
        raw = _redact_text(await self._run(argv))
        if len(raw) > MAX_RESPONSE_CHARS:
            raw = raw[:MAX_RESPONSE_CHARS]
            truncated = True
        else:
            truncated = False
        return envelope(
            {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "sinceSeconds": since_seconds,
                "tailLines": tail_lines,
                "truncated": truncated,
                "logs": raw,
            }
        )

    async def cluster_inventory(self, *, resource: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
        kubectl_resource = _cluster_resource(resource)
        limit = _limit(limit)
        offset = _offset(offset)
        raw = await self._run([self.config.kubectl_path, "--kubeconfig", self.config.kubeconfig, _request_timeout(self.config.timeout_seconds), "get", kubectl_resource, "-o", "json"])
        data = _loads_json(raw)
        items = [_sanitize_cluster_item(item, resource) for item in data.get("items", [])]
        if resource == "namespaces":
            items = [item for item in items if item.get("name") in self.config.namespace_allowlist]
        page = items[offset : offset + limit]
        return envelope(
            {
                "resource": resource,
                "total": len(items),
                "returned": len(page),
                "limit": limit,
                "offset": offset,
                "hasMore": offset + limit < len(items),
                "items": page,
            }
        )

    def _namespace(self, namespace: str) -> str:
        _validate_k8s_name(namespace, "namespace")
        if namespace not in self.config.namespace_allowlist:
            raise K8sROError(
                "namespace_not_allowed",
                "Namespace is outside the k8s_ro allowlist.",
                "Use one of the benchmark namespaces configured in the MCP service environment.",
            )
        return namespace

    def _base(self, namespace: str) -> list[str]:
        return [
            self.config.kubectl_path,
            "--kubeconfig",
            self.config.kubeconfig,
            _request_timeout(self.config.timeout_seconds),
            "-n",
            namespace,
        ]

    async def _run(self, argv: list[str]) -> str:
        return await self.runner.run(argv, timeout_seconds=self.config.timeout_seconds)


def _namespaced_resource(resource: str) -> str:
    key = _resource_key(resource)
    if key in FORBIDDEN_RESOURCES:
        raise K8sROError("resource_forbidden", "This Kubernetes resource is not exposed by k8s_ro.", "Use non-secret workload resources or telemetry/source tools.")
    if key not in NAMESPACED_RESOURCES:
        raise K8sROError("resource_not_allowed", "Resource is outside the k8s_ro namespaced allowlist.", f"Use one of: {', '.join(sorted(NAMESPACED_RESOURCES))}.")
    return NAMESPACED_RESOURCES[key]


def _cluster_resource(resource: str) -> str:
    key = _resource_key(resource)
    if key not in CLUSTER_RESOURCES:
        raise K8sROError("cluster_resource_not_allowed", "Cluster resource is outside the k8s_ro allowlist.", f"Use one of: {', '.join(sorted(CLUSTER_RESOURCES))}.")
    return CLUSTER_RESOURCES[key]


def _resource_key(resource: str) -> str:
    if not isinstance(resource, str) or not RESOURCE_RE.fullmatch(resource):
        raise K8sROError("invalid_resource", "Resource must be a simple allowlisted Kubernetes resource name.", "Do not pass kubectl flags, shell fragments, api paths, or shortcuts.")
    return resource.lower()


def _validate_k8s_name(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not K8S_NAME_RE.fullmatch(value):
        raise K8sROError("invalid_name", f"{field_name} must be a DNS-like Kubernetes name.", "Pass a concrete resource name, not a selector or command fragment.")


def _simple_ref(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"^[A-Za-z0-9_.:-]{1,128}$", value):
        raise K8sROError("invalid_ref", f"{field_name} contains unsupported characters.", "Use the exact value from a previous k8s_ro response.")
    return value


def _label_selector(value: str) -> str:
    if len(value) > 256 or not LABEL_SELECTOR_RE.fullmatch(value) or ".." in value:
        raise K8sROError("invalid_label_selector", "label selector is outside the accepted safe subset.", "Use simple exact or set-based Kubernetes label selectors without spaces.")
    return value


def _limit(value: int) -> int:
    return _bounded_int(value, 1, MAX_LIMIT, "limit")


def _offset(value: int) -> int:
    return _bounded_int(value, 0, 100_000, "offset")


def _bounded_int(value: int, minimum: int, maximum: int, field_name: str) -> int:
    if not isinstance(value, int) or value < minimum or value > maximum:
        raise K8sROError("invalid_integer", f"{field_name} must be between {minimum} and {maximum}.", "Use a bounded integer so responses remain small.")
    return value


def _parse_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise K8sROError(
            "invalid_timeout",
            f"{TIMEOUT_ENV} must be a number of seconds.",
            "Use a bounded kubectl timeout between 0.5 and 30 seconds.",
        ) from exc
    if timeout < 0.5 or timeout > 30:
        raise K8sROError(
            "invalid_timeout",
            f"{TIMEOUT_ENV} must be between 0.5 and 30 seconds.",
            "Use a bounded kubectl timeout between 0.5 and 30 seconds.",
        )
    return timeout


def _request_timeout(timeout_seconds: float) -> str:
    return f"--request-timeout={int(timeout_seconds)}s"


def _loads_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise K8sROError(
            "invalid_kubectl_json",
            "kubectl did not return valid JSON for a read-only operation.",
            "Verify the resource allowlist and API server behavior; raw output is intentionally hidden.",
        ) from exc


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_sanitize(item) for item in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if key in {"managedFields", "data", "binaryData", "stringData"}:
            continue
        if key == "annotations" and isinstance(value, dict):
            out[key] = {
                k: _sanitize(v)
                for k, v in value.items()
                if k != "kubectl.kubernetes.io/last-applied-configuration" and not SENSITIVE_RE.search(k)
            }
            continue
        if key == "env" and isinstance(value, list):
            out[key] = [_sanitize_env(item) for item in value]
            continue
        if key in {"envFrom", "imagePullSecrets"}:
            out[key] = "<redacted-reference>"
            continue
        out[key] = _sanitize(value)
    return out


def _sanitize_env(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    clean = dict(item)
    if "value" in clean:
        clean["value"] = "<redacted-value>"
    if "valueFrom" in clean:
        clean["valueFrom"] = "<redacted-reference>"
    return _sanitize(clean)


def _sanitize_event(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
    involved = item.get("involvedObject", {}) if isinstance(item.get("involvedObject"), dict) else {}
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "type": item.get("type"),
        "reason": item.get("reason"),
        "message": _redact_text(str(item.get("message") or "")),
        "count": item.get("count"),
        "firstTimestamp": item.get("firstTimestamp"),
        "lastTimestamp": item.get("lastTimestamp"),
        "involvedObject": {"kind": involved.get("kind"), "name": involved.get("name")},
    }


def _summarize_namespaced_item(item: Mapping[str, Any], resource: str) -> dict[str, Any]:
    metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
    spec = item.get("spec", {}) if isinstance(item.get("spec"), dict) else {}
    status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
    summary: dict[str, Any] = {
        "apiVersion": item.get("apiVersion"),
        "kind": item.get("kind"),
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "labels": _sanitize(metadata.get("labels", {})),
            "creationTimestamp": metadata.get("creationTimestamp"),
            "deletionTimestamp": metadata.get("deletionTimestamp"),
        },
    }
    key = _resource_key(resource)
    if key == "pods":
        summary["spec"] = {
            "nodeName": spec.get("nodeName"),
            "serviceAccountName": spec.get("serviceAccountName"),
            "containers": _container_specs(spec.get("containers")),
            "initContainers": _container_specs(spec.get("initContainers")),
        }
        summary["status"] = {
            "phase": status.get("phase"),
            "podIP": status.get("podIP"),
            "hostIP": status.get("hostIP"),
            "startTime": status.get("startTime"),
            "conditions": _condition_summaries(status.get("conditions")),
            "containerStatuses": _container_status_summaries(status.get("containerStatuses")),
            "initContainerStatuses": _container_status_summaries(status.get("initContainerStatuses")),
        }
    elif key in {"deployments", "statefulsets", "daemonsets", "replicasets"}:
        summary["spec"] = {
            "replicas": spec.get("replicas"),
            "selector": _sanitize(spec.get("selector", {})),
        }
        summary["status"] = {
            field: status.get(field)
            for field in (
                "observedGeneration",
                "replicas",
                "readyReplicas",
                "availableReplicas",
                "updatedReplicas",
                "currentReplicas",
                "desiredNumberScheduled",
                "numberReady",
                "numberAvailable",
                "numberUnavailable",
            )
            if field in status
        }
    elif key == "services":
        summary["spec"] = {
            "type": spec.get("type"),
            "clusterIP": spec.get("clusterIP"),
            "selector": _sanitize(spec.get("selector", {})),
            "ports": [
                {
                    field: port.get(field)
                    for field in ("name", "protocol", "port", "targetPort", "nodePort")
                    if field in port
                }
                for port in spec.get("ports", [])
                if isinstance(port, dict)
            ],
        }
    elif key == "endpoints":
        subsets = spec.get("subsets") if "subsets" in spec else item.get("subsets", [])
        summary["subsets"] = _endpoint_subset_summaries(subsets)
    elif key == "jobs":
        summary["spec"] = {field: spec.get(field) for field in ("parallelism", "completions", "backoffLimit")}
        summary["status"] = {
            field: status.get(field)
            for field in ("active", "succeeded", "failed", "startTime", "completionTime")
            if field in status
        }
    return summary


def _container_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {"name": item.get("name"), "image": item.get("image")}
        for item in value
        if isinstance(item, dict)
    ]


def _condition_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            field: item.get(field)
            for field in ("type", "status", "reason", "lastTransitionTime")
            if field in item
        }
        for item in value
        if isinstance(item, dict)
    ]


def _container_status_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    summaries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        state = item.get("state", {}) if isinstance(item.get("state"), dict) else {}
        state_summary = {
            state_name: {
                field: state_value.get(field)
                for field in ("reason", "exitCode", "startedAt", "finishedAt")
                if field in state_value
            }
            for state_name, state_value in state.items()
            if isinstance(state_value, dict)
        }
        summaries.append(
            {
                "name": item.get("name"),
                "ready": item.get("ready"),
                "restartCount": item.get("restartCount"),
                "image": item.get("image"),
                "imageID": item.get("imageID"),
                "state": state_summary,
            }
        )
    return summaries


def _endpoint_subset_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    summaries: list[dict[str, Any]] = []
    for subset in value:
        if not isinstance(subset, dict):
            continue
        addresses = subset.get("addresses", []) if isinstance(subset.get("addresses"), list) else []
        not_ready = subset.get("notReadyAddresses", []) if isinstance(subset.get("notReadyAddresses"), list) else []
        summaries.append(
            {
                "readyAddresses": [
                    {"ip": item.get("ip"), "nodeName": item.get("nodeName")}
                    for item in addresses[:50]
                    if isinstance(item, dict)
                ],
                "notReadyAddresses": [
                    {"ip": item.get("ip"), "nodeName": item.get("nodeName")}
                    for item in not_ready[:50]
                    if isinstance(item, dict)
                ],
                "ports": [
                    {field: port.get(field) for field in ("name", "port", "protocol") if field in port}
                    for port in subset.get("ports", [])
                    if isinstance(port, dict)
                ],
            }
        )
    return summaries


def _sanitize_cluster_item(item: Mapping[str, Any], resource: str) -> dict[str, Any]:
    metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
    if resource == "nodes":
        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        return {"name": metadata.get("name"), "labels": metadata.get("labels", {}), "conditions": status.get("conditions", [])}
    if resource == "namespaces":
        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        return {"name": metadata.get("name"), "labels": metadata.get("labels", {}), "phase": status.get("phase")}
    return {"name": metadata.get("name"), "group": item.get("spec", {}).get("group"), "scope": item.get("spec", {}).get("scope")}


def _safe_error(stderr: str) -> str:
    text = " ".join(stderr.split())
    if not text:
        return "no stderr"
    return _redact_text(text)[:500]


def _redact_text(text: str) -> str:
    redacted = BEARER_RE.sub("Bearer <redacted>", text)
    redacted = SK_RE.sub("sk-<redacted>", redacted)
    redacted = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return SENSITIVE_RE.sub(lambda match: match.group(0), redacted)
