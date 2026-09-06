"""Narrow Kubernetes API proxy boundary used by the BladeAI SDK.

The SDK insists on a kubeconfig path.  The generated kubeconfig points only
at a loopback proxy with a per-Trial proxy token.  This module holds the
controller-side backend credential; the Agent never receives it.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from mcp_servers.http_runtime import PolicyGate


class ProxyError(ValueError):
    """Safe rejection returned by the loopback proxy."""


class KubernetesReadBackend(Protocol):
    async def request(self, method: str, path: str, headers: Mapping[str, str]) -> tuple[int, bytes, Mapping[str, str]]:
        """Perform a Controller-owned Kubernetes read request."""


@dataclass(frozen=True)
class ProxyConfig:
    namespace: str
    token: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 18481

    def __post_init__(self) -> None:
        if self.listen_host != "127.0.0.1":
            raise ProxyError("BladeAI Kubernetes proxy must bind loopback only")
        if not self.namespace or "/" in self.namespace:
            raise ProxyError("proxy namespace is invalid")
        if len(self.token) < 32 or self.token != self.token.strip():
            raise ProxyError("proxy token must be a non-empty Trial secret")

    @classmethod
    def new(cls, namespace: str, *, listen_port: int = 18481) -> "ProxyConfig":
        return cls(namespace=namespace, token=secrets.token_urlsafe(32), listen_port=listen_port)


class BladeAIKubernetesProxy:
    """Authorizes only namespaced GETs and forwards through ``k8s_ro`` policy."""

    def __init__(self, config: ProxyConfig, backend: KubernetesReadBackend, policy_gate: PolicyGate) -> None:
        self.config = config
        self.backend = backend
        self.policy_gate = policy_gate

    def kubeconfig(self) -> dict[str, Any]:
        """Return an Agent-visible kubeconfig with no cluster/SA credential."""
        return proxy_kubeconfig(self.config)

    def write_kubeconfig(self, path: Path) -> None:
        write_proxy_kubeconfig(self.config, path)

    async def forward(self, *, method: str, target: str, token: str) -> tuple[int, bytes, Mapping[str, str]]:
        if not secrets.compare_digest(token, self.config.token):
            raise ProxyError("unauthorized proxy token")
        path, policy_tool = self._allowed_path(method, target)
        if "/nodes/" in path:
            node = path.rsplit("/", 1)[1].split("?", 1)[0]
            await self._verify_namespace_node(node)

        async def invoke(*, requested_path: str):
            return await self.backend.request("GET", path, {"Accept": "application/json"})

        # D3/D4's k8s_ro policy must close this route at exactly the same time.
        # Keeping the validated path as an explicit argument means the realtime
        # audit event is a normal k8s_ro ToolCall with inspectable scope, rather
        # than an opaque proxy operation.
        gated: Callable[[], Awaitable[tuple[int, bytes, Mapping[str, str]]] | Mapping[str, Any]] = self.policy_gate.guard(policy_tool)(invoke)
        result = gated(requested_path=path)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, Mapping):
            payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
            return 403, payload, {"Content-Type": "application/json"}
        return result

    async def _verify_namespace_node(self, node: str) -> None:
        pods = f"/api/v1/namespaces/{self.config.namespace}/pods"
        status, body, _headers = await self.backend.request("GET", pods, {"Accept": "application/json"})
        if status != 200:
            raise ProxyError("cannot verify requested node against Trial namespace Pods")
        try:
            values = json.loads(body).get("items", [])
            nodes = {str(item.get("spec", {}).get("nodeName") or "") for item in values if isinstance(item, Mapping)}
        except (ValueError, AttributeError) as exc:
            raise ProxyError("namespace Pod inventory is invalid") from exc
        if node not in nodes:
            raise ProxyError("node is not referenced by a Pod in the Trial namespace")

    def _allowed_path(self, method: str, target: str) -> tuple[str, str]:
        if method.upper() != "GET":
            raise ProxyError("only GET is permitted through the BladeAI Kubernetes proxy")
        if not target.startswith("/") or "\\" in target:
            raise ProxyError("proxy target must be an absolute Kubernetes API path")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or ";" in parsed.path:
            raise ProxyError("proxy target must be a Kubernetes API path without URL components")
        path = parsed.path
        if "%" in path:
            raise ProxyError("encoded path components are forbidden")
        if "/../" in f"/{path.strip('/')}/" or path.endswith("/..") or path.endswith("/."):
            raise ProxyError("proxy path traversal is forbidden")
        pod_base = f"/api/v1/namespaces/{self.config.namespace}/pods"
        endpoints_base = f"/api/v1/namespaces/{self.config.namespace}/endpoints"
        events_base = f"/api/v1/namespaces/{self.config.namespace}/events"
        node_base = "/api/v1/nodes/"
        node_metrics_base = "/apis/metrics.k8s.io/v1beta1/nodes/"
        metric_base = f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.config.namespace}/pods"
        if path == pod_base:
            return _with_allowed_query(path, parsed.query, kind="pod_list"), "k8s_list_resources"
        if path == metric_base:
            return _with_allowed_query(path, parsed.query, kind="metrics_list"), "k8s_list_resources"
        if path in {endpoints_base, events_base}:
            return _with_allowed_query(path, parsed.query, kind="pod_list"), "k8s_list_resources"
        node_name_pattern = _POD_NAME.pattern.removeprefix("^").removesuffix("$")
        if re.fullmatch(node_base + node_name_pattern, path) or re.fullmatch(node_metrics_base + node_name_pattern, path):
            if parsed.query:
                raise ProxyError("node reads do not accept query parameters")
            return path, "k8s_get_resource"
        if re.fullmatch(re.escape(pod_base) + _POD_NAME_PATH, path):
            return _with_allowed_query(path, parsed.query, kind="pod_get"), "k8s_get_resource"
        if re.fullmatch(re.escape(metric_base) + _POD_NAME_PATH, path):
            return _with_allowed_query(path, parsed.query, kind="metrics_get"), "k8s_get_resource"
        if re.fullmatch(re.escape(pod_base) + _POD_NAME_PATH + r"/log", path):
            return _with_allowed_query(path, parsed.query, kind="pod_log"), "k8s_pod_logs"
        raise ProxyError("requested Kubernetes API route is outside the exact Trial read allowlist")


def proxy_kubeconfig(config: ProxyConfig) -> dict[str, Any]:
    """Build the Agent-visible loopback kubeconfig without a backend instance.

    The Controller calls this before the HTTP process is launched.  It must
    never read or reference the Controller kubeconfig used by that process.
    """
    endpoint = f"http://{config.listen_host}:{config.listen_port}"
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "bladeai-loopback", "cluster": {"server": endpoint}}],
        "users": [{"name": "bladeai-trial", "user": {"token": config.token}}],
        "contexts": [{"name": "bladeai-trial", "context": {
            "cluster": "bladeai-loopback", "user": "bladeai-trial", "namespace": config.namespace,
        }}],
        "current-context": "bladeai-trial",
    }


def write_proxy_kubeconfig(config: ProxyConfig, path: Path) -> None:
    """Write only the Trial proxy token and loopback endpoint with mode 0600."""
    if not path.is_absolute():
        raise ProxyError("Agent kubeconfig path must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(proxy_kubeconfig(config), separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


_POD_NAME_PATH = r"/[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?"
_LABEL_SELECTOR = re.compile(r"^[A-Za-z0-9_.\-/=!,()]+$")
_FIELD_SELECTOR = re.compile(r"^(?:metadata\.name|status\.phase|spec\.nodeName)=[A-Za-z0-9_.-]+(?:,(?:metadata\.name|status\.phase|spec\.nodeName)=[A-Za-z0-9_.-]+)*$")
_POSITIVE_INT = re.compile(r"^[1-9][0-9]{0,5}$")
_POD_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def _with_allowed_query(path: str, query: str, *, kind: str) -> str:
    if not query:
        return path
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ProxyError("Kubernetes query is malformed") from exc
    if len(pairs) > 6 or len({key for key, _ in pairs}) != len(pairs):
        raise ProxyError("query parameters are duplicate or exceed the proxy limit")
    allowed = {
        "pod_list": {"labelSelector", "fieldSelector", "limit", "continue", "resourceVersion", "timeoutSeconds"},
        "metrics_list": {"labelSelector", "fieldSelector", "limit", "continue", "resourceVersion", "timeoutSeconds"},
        "pod_get": {"resourceVersion"},
        "metrics_get": {"resourceVersion"},
        "pod_log": {"container", "tailLines", "sinceSeconds", "sinceTime", "previous", "timestamps", "limitBytes"},
    }[kind]
    validated: list[tuple[str, str]] = []
    for key, value in pairs:
        if key not in allowed:
            raise ProxyError("query parameter is not authorized for this Kubernetes read route")
        if key == "labelSelector" and (not value or not _LABEL_SELECTOR.fullmatch(value)):
            raise ProxyError("labelSelector is outside the proxy-safe syntax")
        if key == "fieldSelector" and (not value or not _FIELD_SELECTOR.fullmatch(value)):
            raise ProxyError("fieldSelector is outside the proxy-safe syntax")
        if key in {"limit", "timeoutSeconds", "tailLines", "sinceSeconds", "limitBytes"}:
            if not _POSITIVE_INT.fullmatch(value) or int(value) > 100_000:
                raise ProxyError("numeric query parameter is outside the proxy safety limit")
        if key == "sinceTime" and (not value or len(value) > 64 or "T" not in value):
            raise ProxyError("sinceTime must be a bounded RFC3339 value")
        if key in {"previous", "timestamps"} and value not in {"true", "false"}:
            raise ProxyError(f"{key} must be true or false")
        if key == "container" and not _POD_NAME.fullmatch(value):
            raise ProxyError("container name is invalid")
        if key == "continue" and (not value or len(value) > 1024 or any(char.isspace() for char in value)):
            raise ProxyError("continue token is invalid")
        if key == "resourceVersion" and (not value or len(value) > 128 or not value.isdigit()):
            raise ProxyError("resourceVersion is invalid")
        validated.append((key, value))
    return path + "?" + urlencode(validated)
