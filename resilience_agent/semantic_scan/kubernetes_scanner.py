"""Typed, secret-safe Kubernetes and Helm configuration inventory."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import yaml

from .config import KubernetesConfig
from .contracts import KubernetesManifest

SECRET_NAME = re.compile(r"password|secret|token|credential|api[_-]?key", re.IGNORECASE)
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
CONFIG_VALUE_LIMIT = 16_000
LIVE_SOURCE_ALIAS = "live-apiserver"

CUSTOM_RESOURCE_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("Gateway", "gateway.networking.k8s.io", "v1", "gateways"),
    ("HTTPRoute", "gateway.networking.k8s.io", "v1", "httproutes"),
    ("VirtualService", "networking.istio.io", "v1beta1", "virtualservices"),
    ("DestinationRule", "networking.istio.io", "v1beta1", "destinationrules"),
    ("EnvoyFilter", "networking.istio.io", "v1alpha3", "envoyfilters"),
    ("ScaledObject", "keda.sh", "v1alpha1", "scaledobjects"),
)


class KubernetesConfigError(RuntimeError):
    pass


def _safe_env(container: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for item in container.get("env", []) if isinstance(container.get("env"), list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        record: dict[str, Any] = {"name": name}
        if item.get("valueFrom") is not None:
            record["value_from"] = item["valueFrom"]
        elif not SECRET_NAME.search(name):
            record["value"] = item.get("value")
        else:
            record["value"] = "<redacted>"
        output.append(record)
    return output


def _safe_env_from(container: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    env_from = container.get("envFrom", [])
    for item in env_from if isinstance(env_from, list) else []:
        if not isinstance(item, dict):
            continue
        record: dict[str, Any] = {}
        if isinstance(item.get("configMapRef"), dict):
            record["config_map_ref"] = item["configMapRef"]
        if isinstance(item.get("secretRef"), dict):
            ref = item["secretRef"]
            record["secret_ref"] = {
                "name": ref.get("name"),
                "optional": ref.get("optional"),
            }
        if record:
            output.append(record)
    return output


def _container_summary(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": container.get("name"),
        "image": container.get("image"),
        "command": container.get("command", []),
        "args": container.get("args", []),
        "resources": container.get("resources", {}),
        "readiness_probe": container.get("readinessProbe"),
        "liveness_probe": container.get("livenessProbe"),
        "startup_probe": container.get("startupProbe"),
        "lifecycle": container.get("lifecycle"),
        "ports": container.get("ports", []),
        "env": _safe_env(container),
        "env_from": _safe_env_from(container),
    }


def _pod_status_summary(status: dict[str, Any]) -> dict[str, Any]:
    container_statuses = []
    raw_statuses = status.get("containerStatuses", [])
    for item in raw_statuses if isinstance(raw_statuses, list) else []:
        if not isinstance(item, dict):
            continue
        container_statuses.append(
            {
                "name": item.get("name"),
                "image": item.get("image"),
                "image_id": item.get("imageID"),
                "container_id": item.get("containerID"),
                "ready": item.get("ready"),
                "restart_count": item.get("restartCount"),
                "state": item.get("state"),
                "last_state": item.get("lastState"),
            }
        )
    return {
        "phase": status.get("phase"),
        "pod_ip": status.get("podIP"),
        "host_ip": status.get("hostIP"),
        "start_time": status.get("startTime"),
        "conditions": status.get("conditions", []),
        "container_statuses": container_statuses,
    }


def _pod_spec(resource: dict[str, Any]) -> dict[str, Any]:
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    kind = str(resource.get("kind") or "")
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
        return template.get("spec") if isinstance(template.get("spec"), dict) else {}
    if kind == "CronJob":
        job = spec.get("jobTemplate") if isinstance(spec.get("jobTemplate"), dict) else {}
        job_spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
        template = job_spec.get("template") if isinstance(job_spec.get("template"), dict) else {}
        return template.get("spec") if isinstance(template.get("spec"), dict) else {}
    return spec if kind == "Pod" else {}


def _resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    kind = str(resource.get("kind") or "")
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    summary: dict[str, Any] = {
        "api_version": resource.get("apiVersion"),
        "kind": kind,
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "uid": metadata.get("uid"),
        "resource_version": metadata.get("resourceVersion"),
        "generation": metadata.get("generation"),
        "owner_references": metadata.get("ownerReferences", []),
        "labels": metadata.get("labels", {}),
        "annotations": {
            key: value
            for key, value in (metadata.get("annotations", {}) or {}).items()
            if not SECRET_NAME.search(str(key))
        },
    }
    if kind in WORKLOAD_KINDS:
        pod_spec = _pod_spec(resource)
        containers = pod_spec.get("containers") if isinstance(pod_spec.get("containers"), list) else []
        summary.update(
            {
                "replicas": spec.get("replicas"),
                "strategy": spec.get("strategy") or spec.get("updateStrategy"),
                "min_ready_seconds": spec.get("minReadySeconds"),
                "termination_grace_period_seconds": pod_spec.get(
                    "terminationGracePeriodSeconds"
                ),
                "affinity": pod_spec.get("affinity"),
                "topology_spread_constraints": pod_spec.get(
                    "topologySpreadConstraints"
                ),
                "node_selector": pod_spec.get("nodeSelector"),
                "service_account_name": pod_spec.get("serviceAccountName"),
                "restart_policy": pod_spec.get("restartPolicy"),
                "runtime_class_name": pod_spec.get("runtimeClassName"),
                "containers": [
                    _container_summary(item) for item in containers if isinstance(item, dict)
                ],
                "init_containers": [
                    _container_summary(item)
                    for item in pod_spec.get("initContainers", [])
                    if isinstance(item, dict)
                ],
            }
        )
        if kind == "Pod":
            status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
            summary["status"] = _pod_status_summary(status)
    elif kind in {
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "Service",
        "Ingress",
        "Gateway",
        "HTTPRoute",
        "VirtualService",
        "DestinationRule",
        "EnvoyFilter",
        "ScaledObject",
        "EndpointSlice",
        "Event",
    }:
        summary["spec"] = spec
    elif kind == "Endpoints":
        summary["subsets"] = resource.get("subsets", [])
    elif kind == "ConfigMap":
        data = resource.get("data") if isinstance(resource.get("data"), dict) else {}
        summary["data_keys"] = sorted(data)
        summary["data"] = {
            key: _safe_config_value(key, value)
            for key, value in data.items()
            if not SECRET_NAME.search(str(key))
        }
    elif kind == "Secret":
        data = resource.get("data") if isinstance(resource.get("data"), dict) else {}
        summary["data_keys"] = sorted(data)
        summary["type"] = resource.get("type")
    else:
        summary["spec_keys"] = sorted(spec)
    return summary


def _safe_config_value(key: str, value: Any) -> str:
    rendered = str(value)
    if SECRET_NAME.search(key):
        return "<redacted>"
    if len(rendered) > CONFIG_VALUE_LIMIT:
        return rendered[:CONFIG_VALUE_LIMIT] + "\n<truncated>"
    return rendered


def _truncate_mapping(values: dict[str, str], max_chars: int) -> dict[str, str]:
    output: dict[str, str] = {}
    remaining = max(max_chars, 0)
    for key, value in values.items():
        if remaining <= 0:
            break
        if len(value) > remaining:
            output[key] = value[:remaining] + "\n<truncated>"
            break
        output[key] = value
        remaining -= len(value)
    return output


def _resource_path(namespace: str | None, kind: str, name: str | None) -> str:
    return f"kubernetes://{namespace or '_cluster'}/{kind}/{name or '_unnamed'}"


class KubernetesConfigScanner:
    def __init__(self, config: KubernetesConfig):
        self.config = config
        self.resources: list[dict[str, Any]] = []
        self.non_resource_documents: list[dict[str, Any]] = []
        self._manifest: KubernetesManifest | None = None

    @property
    def manifest(self) -> KubernetesManifest:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes configuration has not been scanned")
        return self._manifest

    def scan(self) -> KubernetesManifest:
        resources: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        source_paths: list[str] = []
        for source in self.config.sources:
            paths = [source.path] if source.path.is_file() else sorted(source.path.rglob("*.y*ml"))
            for path in paths:
                if not path.is_file():
                    continue
                source_paths.append(str(path))
                raw = path.read_bytes()
                digest.update(source.alias.encode())
                digest.update(str(path).encode())
                digest.update(raw)
                try:
                    documents = list(yaml.safe_load_all(raw.decode("utf-8")))
                except (UnicodeDecodeError, yaml.YAMLError) as exc:
                    raise KubernetesConfigError(f"invalid Kubernetes YAML: {path}") from exc
                for index, document in enumerate(documents):
                    items = document if isinstance(document, list) else [document]
                    if isinstance(document, dict) and document.get("kind") == "List":
                        items = document.get("items", [])
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if item.get("apiVersion") and item.get("kind"):
                            resources.append(
                                {
                                    "source_alias": source.alias,
                                    "path": str(path),
                                    "document_index": index,
                                    "resource": _resource_summary(item),
                                }
                            )
                        else:
                            other.append(
                                {
                                    "source_alias": source.alias,
                                    "path": str(path),
                                    "document_index": index,
                                    "keys": sorted(item),
                                    "summary": self._helm_summary(item),
                                }
                            )
        kinds = Counter(str(item["resource"].get("kind") or "Unknown") for item in resources)
        self.resources = resources
        self.non_resource_documents = other
        self._manifest = KubernetesManifest(
            mode="manifest",
            source_paths=source_paths,
            resource_count=len(resources),
            kinds=dict(sorted(kinds.items())),
            manifest_sha256=digest.hexdigest(),
            completeness={
                kind: {"status": "from_manifest", "count": count}
                for kind, count in sorted(kinds.items())
            },
            authoritative_for_namespace=self.config.authoritative_for_namespace,
        )
        return self._manifest

    def list_resources(
        self,
        *,
        kinds: list[str] | None = None,
        name_contains: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes configuration has not been scanned")
        accepted = set(kinds or [])
        needle = (name_contains or "").lower()
        matches = [
            item
            for item in self.resources
            if (not accepted or item["resource"].get("kind") in accepted)
            and (
                not needle
                or needle in str(item["resource"].get("name") or "").lower()
                or needle in str(item["path"]).lower()
            )
        ]
        resolved_limit = limit or self.config.max_resources_per_agent
        return {
            "resources": matches[:resolved_limit],
            "total": len(matches),
            "truncated": len(matches) > resolved_limit,
        }

    def get_resource(self, kind: str, name: str) -> dict[str, Any]:
        matches = [
            item
            for item in self.resources
            if item["resource"].get("kind") == kind
            and item["resource"].get("name") == name
        ]
        return {"matches": matches, "total": len(matches)}

    def get_configmap_value(
        self,
        name: str,
        *,
        key: str | None = None,
        namespace: str | None = None,
        max_chars: int = CONFIG_VALUE_LIMIT,
    ) -> dict[str, Any]:
        resolved_namespace = namespace or self.config.namespace
        if key and SECRET_NAME.search(key):
            raise KubernetesConfigError("refusing to expose secret-like ConfigMap key")
        matches = []
        for item in self.resources:
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            if resource.get("kind") != "ConfigMap" or resource.get("name") != name:
                continue
            if resource.get("namespace") not in {None, resolved_namespace}:
                continue
            data = resource.get("data") if isinstance(resource.get("data"), dict) else {}
            selected = {key: data[key]} if key and key in data else {}
            if key is None:
                selected = dict(data)
            if key is not None and not selected:
                continue
            matches.append(
                {
                    "source_alias": item.get("source_alias"),
                    "path": item.get("path"),
                    "resource": {
                        "api_version": resource.get("api_version"),
                        "kind": "ConfigMap",
                        "name": resource.get("name"),
                        "namespace": resource.get("namespace"),
                        "data": _truncate_mapping(
                            {str(k): str(v) for k, v in selected.items()},
                            max_chars,
                        ),
                    },
                }
            )
        return {
            "matches": matches,
            "total": len(matches),
            "source": "manifest",
        }

    def inventory_for_prompt(self, max_chars: int) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes configuration has not been scanned")
        value = {
            "manifest": self._manifest.model_dump(mode="json"),
            "resources": self.resources,
            "non_resource_documents": self.non_resource_documents,
        }
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(rendered) <= max_chars:
            return value
        value["resources"] = self.resources[: max(1, len(self.resources) // 2)]
        value["non_resource_documents"] = self.non_resource_documents[:10]
        value["truncated"] = True
        return value

    @staticmethod
    def _helm_summary(value: dict[str, Any]) -> dict[str, Any]:
        interesting = {
            "replicaCount",
            "resources",
            "autoscaling",
            "affinity",
            "topologySpreadConstraints",
            "podDisruptionBudget",
            "terminationGracePeriodSeconds",
            "readinessProbe",
            "livenessProbe",
            "startupProbe",
        }

        def walk(item: Any, prefix: str = "") -> dict[str, Any]:
            found: dict[str, Any] = {}
            if isinstance(item, dict):
                for key, child in item.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if str(key) in interesting:
                        found[path] = child
                    found.update(walk(child, path))
            elif isinstance(item, list):
                for index, child in enumerate(item[:50]):
                    found.update(walk(child, f"{prefix}[{index}]"))
            return found

        return walk(value)


@dataclass(frozen=True)
class LiveKubernetesConfig:
    namespace: str
    kubeconfig: str | None = None
    context: str | None = None
    max_resources_per_agent: int = 160
    authoritative_for_namespace: bool = True


@dataclass
class _KubernetesApiBundle:
    api_client: Any
    apps: Any
    batch: Any
    core: Any
    discovery: Any
    autoscaling: Any
    policy: Any
    networking: Any
    custom: Any


class LiveKubernetesScanner:
    """Secret-safe live Kubernetes inventory with the manifest scanner API shape."""

    def __init__(
        self,
        namespace: str,
        *,
        kubeconfig: str | None = None,
        context: str | None = None,
        max_resources_per_agent: int = 160,
        authoritative_for_namespace: bool = True,
        api_bundle: _KubernetesApiBundle | None = None,
    ):
        self.config = LiveKubernetesConfig(
            namespace=namespace,
            kubeconfig=kubeconfig,
            context=context,
            max_resources_per_agent=max_resources_per_agent,
            authoritative_for_namespace=authoritative_for_namespace,
        )
        self.resources: list[dict[str, Any]] = []
        self.non_resource_documents: list[dict[str, Any]] = []
        self.completeness: dict[str, dict[str, Any]] = {}
        self._manifest: KubernetesManifest | None = None
        self._client = api_bundle

    @property
    def manifest(self) -> KubernetesManifest:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes live inventory has not been scanned")
        return self._manifest

    @property
    def snapshot_hash(self) -> str:
        return self.manifest.manifest_sha256

    def scan(self) -> KubernetesManifest:
        api = self._load_client()
        resources: list[dict[str, Any]] = []
        completeness: dict[str, dict[str, Any]] = {}

        def collect(kind: str, fetch: Any) -> None:
            try:
                items = fetch()
            except Exception as exc:  # noqa: BLE001  # pragma: no cover
                if getattr(exc, "status", None) == 404:
                    completeness[kind] = {
                        "status": "not_installed",
                        "count": 0,
                    }
                    return
                completeness[kind] = {
                    "status": "unavailable",
                    "reason": f"{exc.__class__.__name__}: {exc}",
                }
                return
            completeness[kind] = {"status": "complete", "count": len(items)}
            for item in items:
                resources.append(self._live_item(item, kind))

        collect("Deployment", lambda: api.apps.list_namespaced_deployment(self.config.namespace).items)
        collect("StatefulSet", lambda: api.apps.list_namespaced_stateful_set(self.config.namespace).items)
        collect("DaemonSet", lambda: api.apps.list_namespaced_daemon_set(self.config.namespace).items)
        collect("Job", lambda: api.batch.list_namespaced_job(self.config.namespace).items)
        collect("CronJob", lambda: api.batch.list_namespaced_cron_job(self.config.namespace).items)
        collect("Pod", lambda: api.core.list_namespaced_pod(self.config.namespace).items)
        collect("Service", lambda: api.core.list_namespaced_service(self.config.namespace).items)
        collect("Endpoints", lambda: api.core.list_namespaced_endpoints(self.config.namespace).items)
        collect("Event", lambda: api.core.list_namespaced_event(self.config.namespace).items)
        collect("ConfigMap", lambda: api.core.list_namespaced_config_map(self.config.namespace).items)
        # The generated DiscoveryV1 model rejects EndpointSlices whose optional
        # endpoints field is omitted by older API servers. Reading the same API
        # through the unstructured client preserves the authoritative response
        # without client-side model validation.
        collect(
            "EndpointSlice",
            lambda: api.custom.list_namespaced_custom_object(
                group="discovery.k8s.io",
                version="v1",
                namespace=self.config.namespace,
                plural="endpointslices",
            ).get("items", []),
        )
        collect(
            "HorizontalPodAutoscaler",
            lambda: api.autoscaling.list_namespaced_horizontal_pod_autoscaler(
                self.config.namespace
            ).items,
        )
        collect(
            "PodDisruptionBudget",
            lambda: api.policy.list_namespaced_pod_disruption_budget(
                self.config.namespace
            ).items,
        )
        collect("Ingress", lambda: api.networking.list_namespaced_ingress(self.config.namespace).items)
        for kind, group, version, plural in CUSTOM_RESOURCE_SOURCES:
            collect(
                kind,
                lambda group=group, version=version, plural=plural: api.custom.list_namespaced_custom_object(
                    group=group,
                    version=version,
                    namespace=self.config.namespace,
                    plural=plural,
                ).get("items", []),
            )

        kinds = Counter(str(item["resource"].get("kind") or "Unknown") for item in resources)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "namespace": self.config.namespace,
                    "resources": resources,
                    "completeness": completeness,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.resources = resources
        self.completeness = dict(sorted(completeness.items()))
        self._manifest = KubernetesManifest(
            mode="live",
            source_paths=[f"kubernetes://{self.config.namespace}"],
            resource_count=len(resources),
            kinds=dict(sorted(kinds.items())),
            manifest_sha256=digest,
            completeness=self.completeness,
            authoritative_for_namespace=self.config.authoritative_for_namespace,
        )
        return self._manifest

    def list_resources(
        self,
        *,
        kinds: list[str] | None = None,
        name_contains: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes live inventory has not been scanned")
        accepted = set(kinds or [])
        needle = (name_contains or "").lower()
        matches = [
            item
            for item in self.resources
            if (not accepted or item["resource"].get("kind") in accepted)
            and (
                not needle
                or needle in str(item["resource"].get("name") or "").lower()
                or needle in str(item["path"]).lower()
            )
        ]
        resolved_limit = limit or self.config.max_resources_per_agent
        return {
            "resources": matches[:resolved_limit],
            "total": len(matches),
            "truncated": len(matches) > resolved_limit,
            "snapshot_hash": self.snapshot_hash,
            "completeness": self.completeness,
        }

    def get_resource(self, kind: str, name: str) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes live inventory has not been scanned")
        matches = [
            item
            for item in self.resources
            if item["resource"].get("kind") == kind
            and item["resource"].get("name") == name
        ]
        return {
            "matches": matches,
            "total": len(matches),
            "snapshot_hash": self.snapshot_hash,
        }

    def get_configmap_value(
        self,
        name: str,
        *,
        key: str | None = None,
        namespace: str | None = None,
        max_chars: int = CONFIG_VALUE_LIMIT,
    ) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes live inventory has not been scanned")
        if key and SECRET_NAME.search(key):
            raise KubernetesConfigError("refusing to expose secret-like ConfigMap key")
        resolved_namespace = namespace or self.config.namespace
        matches = []
        for item in self.resources:
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            if resource.get("kind") != "ConfigMap" or resource.get("name") != name:
                continue
            if resource.get("namespace") != resolved_namespace:
                continue
            data = resource.get("data") if isinstance(resource.get("data"), dict) else {}
            selected = {key: data[key]} if key and key in data else {}
            if key is None:
                selected = dict(data)
            if key is not None and not selected:
                continue
            matches.append(
                {
                    "source_alias": item.get("source_alias"),
                    "path": item.get("path"),
                    "resource": {
                        "api_version": resource.get("api_version"),
                        "kind": "ConfigMap",
                        "name": resource.get("name"),
                        "namespace": resource.get("namespace"),
                        "data": _truncate_mapping(
                            {str(k): str(v) for k, v in selected.items()},
                            max_chars,
                        ),
                    },
                }
            )
        return {
            "matches": matches,
            "total": len(matches),
            "source": "live-apiserver",
            "snapshot_hash": self.snapshot_hash,
        }

    def inventory_for_prompt(self, max_chars: int) -> dict[str, Any]:
        if self._manifest is None:
            raise KubernetesConfigError("Kubernetes live inventory has not been scanned")
        value = {
            "manifest": self._manifest.model_dump(mode="json"),
            "resources": self.resources,
            "completeness": self.completeness,
            "snapshot_hash": self.snapshot_hash,
        }
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(rendered) <= max_chars:
            return value
        value["resources"] = self.resources[: max(1, len(self.resources) // 2)]
        value["truncated"] = True
        return value

    def _live_item(self, item: Any, fallback_kind: str) -> dict[str, Any]:
        resource = self._to_dict(item)
        resource.setdefault("kind", fallback_kind)
        resource.setdefault("apiVersion", self._api_version_for(fallback_kind))
        summary = _resource_summary(resource)
        return {
            "source_alias": LIVE_SOURCE_ALIAS,
            "path": _resource_path(
                summary.get("namespace"),
                str(summary.get("kind") or fallback_kind),
                summary.get("name"),
            ),
            "document_index": 0,
            "resource": summary,
        }

    def _load_client(self) -> _KubernetesApiBundle:
        if self._client is not None:
            return self._client
        try:
            from kubernetes import client, config
        except ImportError as exc:  # pragma: no cover - optional runtime extra.
            raise KubernetesConfigError(
                "kubernetes package is required for LiveKubernetesScanner"
            ) from exc
        if self.config.kubeconfig:
            config.load_kube_config(
                config_file=self.config.kubeconfig,
                context=self.config.context,
            )
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(context=self.config.context)
        self._client = _KubernetesApiBundle(
            api_client=client.ApiClient(),
            apps=client.AppsV1Api(),
            batch=client.BatchV1Api(),
            core=client.CoreV1Api(),
            discovery=client.DiscoveryV1Api(),
            autoscaling=client.AutoscalingV2Api(),
            policy=client.PolicyV1Api(),
            networking=client.NetworkingV1Api(),
            custom=client.CustomObjectsApi(),
        )
        return self._client

    def _to_dict(self, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return dict(item)
        serialized = self._load_client().api_client.sanitize_for_serialization(item)
        if isinstance(serialized, dict):
            return serialized
        raise KubernetesConfigError(f"unsupported Kubernetes object: {type(item).__name__}")

    @staticmethod
    def _api_version_for(kind: str) -> str:
        if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            return "apps/v1"
        if kind in {"Job", "CronJob"}:
            return "batch/v1"
        if kind == "EndpointSlice":
            return "discovery.k8s.io/v1"
        if kind == "HorizontalPodAutoscaler":
            return "autoscaling/v2"
        if kind == "PodDisruptionBudget":
            return "policy/v1"
        if kind == "Ingress":
            return "networking.k8s.io/v1"
        if kind in {"Gateway", "HTTPRoute"}:
            return "gateway.networking.k8s.io/v1"
        if kind in {"VirtualService", "DestinationRule"}:
            return "networking.istio.io/v1beta1"
        if kind == "EnvoyFilter":
            return "networking.istio.io/v1alpha3"
        if kind == "ScaledObject":
            return "keda.sh/v1alpha1"
        return "v1"
