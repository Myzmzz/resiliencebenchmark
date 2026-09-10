from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_servers.audit_bridge import AuditBridgeClient, AuditBridgeConfig, AuditBridgeListener
from mcp_servers.bladeai_k8s_proxy.service import (
    BladeAIKubernetesProxy,
    ProxyConfig,
    ProxyError,
    proxy_kubeconfig,
    write_proxy_kubeconfig,
)
from mcp_servers.http_runtime import PolicyGate
from mcp_servers.bladeai_k8s_proxy.server import (
    KubernetesBackendError,
    ProxyRuntimeConfig,
    ProxyRuntimeError,
    create_app,
)
from stage2_service.harness_adapters.base import ToolCall, ToolResult


@contextmanager
def _audit_config(trial_id: str):
    with tempfile.TemporaryDirectory(prefix="bp-", dir="/tmp") as root:
        yield AuditBridgeConfig(Path(root) / "audit.sock", trial_id, "controller")


class _Backend:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, headers):
        self.calls.append((method, path, dict(headers)))
        return 200, b'{"items":[]}', {"Content-Type": "application/json"}


class _UnavailableBackend:
    async def request(self, method, path, headers):
        raise KubernetesBackendError("Controller Kubernetes API is unavailable")


class _DeniedGate:
    def guard(self, _tool):
        def decorate(_function):
            async def rejected(*_args, **_kwargs):
                return {"ok": False, "error": {"code": "TOOL_DISABLED"}}
            return rejected
        return decorate


def _proxy(backend, gate=None):
    config = ProxyConfig(namespace="otel-demo", token="x" * 32)
    return BladeAIKubernetesProxy(config, backend, gate or PolicyGate(server_name="k8s_ro", policy_file=None))


def _runtime_config(tmp_path: Path) -> ProxyRuntimeConfig:
    kubeconfig = tmp_path / "controller-kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    kubeconfig.chmod(0o600)
    return ProxyRuntimeConfig(
        namespace="otel-demo", token="x" * 32, kubeconfig=kubeconfig,
    )


def test_proxy_allows_only_nodes_referenced_by_namespace_pods():
    class Nodes:
        async def request(self, method, path, headers):
            if path.endswith("/pods"):
                return 200, b'{"items":[{"spec":{"nodeName":"worker-a"}}]}', {}
            return 200, b'{"kind":"Node"}', {}

    proxy = _proxy(Nodes())
    allowed = asyncio.run(proxy.forward(method="GET", target="/apis/metrics.k8s.io/v1beta1/nodes/worker-a", token="x" * 32))
    assert allowed[0] == 200
    with pytest.raises(ProxyError, match="not referenced"):
        asyncio.run(proxy.forward(method="GET", target="/api/v1/nodes/worker-b", token="x" * 32))


def test_proxy_kubeconfig_contains_only_loopback_trial_token(tmp_path):
    proxy = _proxy(_Backend())
    path = tmp_path / "kubeconfig"
    proxy.write_kubeconfig(path)
    text = path.read_text()

    assert "127.0.0.1" in text
    assert "serviceaccount" not in text.lower()
    assert "kubernetes.default.svc" not in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_controller_can_write_agent_loopback_kubeconfig_without_backend(tmp_path):
    config = ProxyConfig(namespace="otel-demo", token="x" * 32)
    visible = proxy_kubeconfig(config)
    path = tmp_path / "agent" / "kubeconfig"
    write_proxy_kubeconfig(config, path)

    assert visible == json.loads(path.read_text())
    assert visible["clusters"][0]["cluster"]["server"] == "http://127.0.0.1:18481"
    assert visible["users"][0]["user"]["token"] == "x" * 32
    assert "certificate-authority" not in path.read_text()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_proxy_forwards_only_trial_namespaced_pod_reads():
    backend = _Backend()
    proxy = _proxy(backend)

    result = asyncio.run(proxy.forward(method="GET", target="/api/v1/namespaces/otel-demo/pods/cart", token="x" * 32))

    assert result[0] == 200
    assert backend.calls[0][0:2] == ("GET", "/api/v1/namespaces/otel-demo/pods/cart")


def test_proxy_allows_bounded_kubernetes_list_filters_and_pod_log_only():
    backend = _Backend()
    proxy = _proxy(backend)

    asyncio.run(proxy.forward(
        method="GET",
        target="/api/v1/namespaces/otel-demo/pods?labelSelector=app%3Dcart&limit=20",
        token="x" * 32,
    ))

    asyncio.run(proxy.forward(
        method="GET",
        target="/api/v1/namespaces/otel-demo/pods?labelSelector=app=cart&limit=20",
        token="x" * 32,
    ))
    asyncio.run(proxy.forward(
        method="GET",
        target="/api/v1/namespaces/otel-demo/pods/cart/log?tailLines=10&timestamps=true",
        token="x" * 32,
    ))
    assert backend.calls[0][1] == "/api/v1/namespaces/otel-demo/pods?labelSelector=app%3Dcart&limit=20"
    assert backend.calls[1][1] == "/api/v1/namespaces/otel-demo/pods?labelSelector=app%3Dcart&limit=20"
    assert backend.calls[2][1] == "/api/v1/namespaces/otel-demo/pods/cart/log?tailLines=10&timestamps=true"


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/namespaces/otel-demo/pods/cart/exec"),
    ("GET", "/api/v1/namespaces/otel-demo/secrets"),
    ("GET", "/api/v1/namespaces/other/pods/cart"),
    ("GET", "https://example.test/api/v1/namespaces/otel-demo/pods/cart"),
    ("GET", "/api/v1/namespaces/otel-demo/pods/cart/proxy/https://example.test"),
    ("GET", "/api/v1/namespaces/otel-demo/pods/cart/exec"),
    ("GET", "/api/v1/namespaces/otel-demo/pods/cart/attach"),
    ("GET", "/api/v1/namespaces/otel-demo/pods/cart/ephemeralcontainers"),
    ("GET", "/api/v1/namespaces/otel-demo/pods/../secrets"),
    ("GET", "/api/v1/namespaces/otel-demo/pods%2f..%2fsecrets"),
    ("GET", "/api/v1/namespaces/otel-demo/pods?watch=true"),
])
def test_proxy_rejects_method_and_scope_escape(method, path):
    with pytest.raises(ProxyError):
        asyncio.run(_proxy(_Backend()).forward(method=method, target=path, token="x" * 32))


def test_proxy_obeys_same_policy_gate_as_k8s_ro():
    backend = _Backend()
    response = asyncio.run(
        _proxy(backend, _DeniedGate()).forward(
            method="GET", target="/api/v1/namespaces/otel-demo/pods/cart", token="x" * 32
        )
    )
    assert response[0] == 403
    assert json.loads(response[1])["error"]["code"] == "TOOL_DISABLED"
    assert backend.calls == []


def test_proxy_audits_as_k8s_tool_and_preserves_kubernetes_body(tmp_path):
    backend = _Backend()
    events: list[ToolCall | ToolResult] = []

    def controller(event, _source):
        events.append(event)
        return {"allowed": True}

    with _audit_config("trial-proxy") as config, AuditBridgeListener(config, controller):
        gate = PolicyGate(
            server_name="k8s_ro",
            policy_file=None,
            audit_client=AuditBridgeClient(config),
        )
        proxy = _proxy(backend, gate)
        response = asyncio.run(proxy.forward(
            method="GET", target="/api/v1/namespaces/otel-demo/pods/cart", token="x" * 32,
        ))

    assert response[0] == 200
    assert json.loads(response[1]) == {"items": []}
    assert response[2]["X-Resbench-Controller-Call-Id"]
    assert [type(event) for event in events] == [ToolCall, ToolResult]
    assert events[0].tool == "k8s_ro.k8s_get_resource"
    assert events[0].arguments == {"requested_path": "/api/v1/namespaces/otel-demo/pods/cart"}
    assert events[1].payload["controller_call_id"] == response[2]["X-Resbench-Controller-Call-Id"]


def test_http_entrypoint_requires_proxy_token_and_preserves_upstream_status(tmp_path):
    backend = _Backend()
    app = create_app(config=_runtime_config(tmp_path), backend=backend,
                     policy_gate=PolicyGate(server_name="k8s_ro", policy_file=None))
    with TestClient(app) as client:
        rejected = client.get("/api/v1/namespaces/otel-demo/pods")
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "UNAUTHORIZED"
        allowed = client.get(
            "/api/v1/namespaces/otel-demo/pods?limit=20",
            headers={"Authorization": "Bearer " + "x" * 32},
        )
        assert allowed.status_code == 200
        assert allowed.json() == {"items": []}
        escaped = client.get(
            "/api/v1/namespaces/otel-demo/secrets",
            headers={"Authorization": "Bearer " + "x" * 32},
        )
        assert escaped.status_code == 400
        assert escaped.json()["error"]["code"] == "PROXY_REJECTED"
        assert client.post("/api/v1/namespaces/otel-demo/pods", headers={"Authorization": "Bearer " + "x" * 32}).status_code == 405
    assert len(backend.calls) == 1
    assert backend.calls[0][1].endswith("?limit=20")


def test_http_entrypoint_never_turns_controller_failure_into_200(tmp_path):
    app = create_app(config=_runtime_config(tmp_path), backend=_UnavailableBackend(),
                     policy_gate=PolicyGate(server_name="k8s_ro", policy_file=None))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/namespaces/otel-demo/pods",
            headers={"Authorization": "Bearer " + "x" * 32},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "KUBERNETES_UNAVAILABLE"


def test_runtime_config_rejects_non_loopback_and_world_readable_kubeconfig(tmp_path):
    kubeconfig = tmp_path / "controller-kubeconfig"
    kubeconfig.write_text("apiVersion: v1", encoding="utf-8")
    kubeconfig.chmod(0o644)
    with pytest.raises(ProxyRuntimeError, match="must not be group/world accessible"):
        ProxyRuntimeConfig(namespace="otel-demo", token="x" * 32, kubeconfig=kubeconfig)
    kubeconfig.chmod(0o600)
    with pytest.raises(ProxyRuntimeError, match="bind 127.0.0.1"):
        ProxyRuntimeConfig(namespace="otel-demo", token="x" * 32, kubeconfig=kubeconfig, host="0.0.0.0")
