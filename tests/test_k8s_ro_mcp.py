from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from mcp_servers.k8s_ro.service import K8sROError, K8sROService, RuntimeConfig


def run(coro):
    return asyncio.run(coro)


@dataclass
class FakeRunner:
    responses: dict[tuple[str, ...], str]
    calls: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    async def run(self, argv: list[str], *, timeout_seconds: float) -> str:
        self.calls.append(argv)
        self.timeouts.append(timeout_seconds)
        return self.responses[tuple(argv)]


def service(responses: dict[tuple[str, ...], str]) -> tuple[K8sROService, FakeRunner]:
    runner = FakeRunner(responses)
    config = RuntimeConfig(
        kubeconfig="/fixed/kubeconfig",
        namespace_allowlist=frozenset({"train-ticket", "sock-shop", "otel-demo"}),
        kubectl_path="kubectl",
        timeout_seconds=5.0,
    )
    return K8sROService(config, runner), runner


def test_runtime_config_requires_env_fixed_kubeconfig(monkeypatch):
    monkeypatch.delenv("RESBENCH_K8S_RO_KUBECONFIG", raising=False)
    monkeypatch.setenv("RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST", "train-ticket")

    with pytest.raises(K8sROError) as exc:
        RuntimeConfig.from_env()

    assert exc.value.code == "missing_kubeconfig"


def test_runtime_config_requires_absolute_existing_kubeconfig(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_K8S_RO_KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST", "train-ticket")

    config = RuntimeConfig.from_env()

    assert config.kubeconfig == str(kubeconfig)

    monkeypatch.setenv("RESBENCH_K8S_RO_KUBECONFIG", "relative-config")
    with pytest.raises(K8sROError) as exc:
        RuntimeConfig.from_env()
    assert exc.value.code == "invalid_kubeconfig"


def test_list_resources_uses_fixed_argv_and_paginates():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "otel-demo",
        "get",
        "pods",
        "-o",
        "json",
        "-l",
        "app=checkoutservice",
    )
    svc, runner = service(
        {
            argv: '{"items":[{"metadata":{"name":"pod-a"}},{"metadata":{"name":"pod-b"}}]}'
        }
    )

    result = run(
        svc.list_resources(
            namespace="otel-demo",
            resource="pods",
            label_selector="app=checkoutservice",
            limit=1,
            offset=1,
        )
    )

    assert result["ok"] is True
    assert result["returned"] == 1
    assert result["items"][0]["metadata"]["name"] == "pod-b"
    assert runner.calls == [list(argv)]
    assert runner.timeouts == [5.0]


def test_get_configmap_redacts_content_and_last_applied():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "train-ticket",
        "get",
        "configmaps",
        "tt-config",
        "-o",
        "json",
    )
    svc, _ = service(
        {
            argv: (
                '{"metadata":{"name":"tt-config","managedFields":[{}],'
                '"annotations":{"kubectl.kubernetes.io/last-applied-configuration":"secret",'
                '"safe":"ok"}},"data":{"password":"x"},"binaryData":{"a":"b"}}'
            )
        }
    )

    result = run(svc.get_resource(namespace="train-ticket", resource="configmaps", name="tt-config"))

    obj = result["object"]
    assert "data" not in obj
    assert "binaryData" not in obj
    assert "managedFields" not in obj["metadata"]
    assert obj["metadata"]["annotations"] == {"safe": "ok"}


def test_pod_env_values_are_redacted():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "otel-demo",
        "get",
        "pods",
        "checkout-pod",
        "-o",
        "json",
    )
    svc, _ = service(
        {
            argv: (
                '{"metadata":{"name":"checkout-pod"},"spec":{"containers":[{"name":"app",'
                '"env":[{"name":"TOKEN","value":"raw-secret","valueFrom":{"secretKeyRef":{"name":"s","key":"k"}}}],'
                '"envFrom":[{"secretRef":{"name":"s"}}]}]}}'
            )
        }
    )

    result = run(svc.get_resource(namespace="otel-demo", resource="pods", name="checkout-pod"))

    container = result["object"]["spec"]["containers"][0]
    assert container["env"][0]["value"] == "<redacted-value>"
    assert container["env"][0]["valueFrom"] == "<redacted-reference>"
    assert container["envFrom"] == "<redacted-reference>"


def test_forbidden_secret_resource_rejected_before_runner_call():
    svc, runner = service({})

    with pytest.raises(K8sROError) as exc:
        run(svc.list_resources(namespace="otel-demo", resource="secrets"))

    assert exc.value.code == "resource_forbidden"
    assert runner.calls == []


def test_namespace_allowlist_blocks_cross_namespace_read():
    svc, runner = service({})

    with pytest.raises(K8sROError) as exc:
        run(svc.list_resources(namespace="kube-system", resource="pods"))

    assert exc.value.code == "namespace_not_allowed"
    assert runner.calls == []


def test_pod_logs_are_bounded_and_do_not_use_previous():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "otel-demo",
        "logs",
        "checkout-pod",
        "--since",
        "60s",
        "--tail",
        "10",
        "-c",
        "app",
    )
    svc, runner = service({argv: "line1 Bearer abcdefghijklmnop\npassword=raw\nsk-testsecret123\n"})

    result = run(svc.pod_logs(namespace="otel-demo", pod="checkout-pod", container="app", since_seconds=60, tail_lines=10))

    assert result["ok"] is True
    assert "Bearer <redacted>" in result["logs"]
    assert "password=<redacted>" in result["logs"]
    assert "sk-<redacted>" in result["logs"]
    assert "--previous" not in runner.calls[0]


def test_cluster_inventory_allows_only_small_allowlist():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "get",
        "nodes",
        "-o",
        "json",
    )
    svc, _ = service({argv: '{"items":[{"metadata":{"name":"node-a"},"status":{"conditions":[{"type":"Ready"}]}}]}'})

    result = run(svc.cluster_inventory(resource="nodes"))

    assert result["items"][0]["name"] == "node-a"

    with pytest.raises(K8sROError):
        run(svc.cluster_inventory(resource="clusterroles"))


def test_events_redact_message_credentials():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "otel-demo",
        "get",
        "events",
        "-o",
        "json",
    )
    svc, _ = service({argv: '{"items":[{"metadata":{"name":"e1"},"message":"token: raw-secret"}]}'})

    result = run(svc.list_events(namespace="otel-demo"))

    assert result["items"][0]["message"] == "token=<redacted>"


def test_namespace_cluster_inventory_is_limited_to_allowlist():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "get",
        "namespaces",
        "-o",
        "json",
    )
    svc, _ = service({argv: '{"items":[{"metadata":{"name":"otel-demo"}},{"metadata":{"name":"kube-system"}}]}'})

    result = run(svc.cluster_inventory(resource="namespaces"))

    assert [item["name"] for item in result["items"]] == ["otel-demo"]


def test_invalid_kubectl_json_is_structured():
    argv = (
        "kubectl",
        "--kubeconfig",
        "/fixed/kubeconfig",
        "--request-timeout=5s",
        "-n",
        "otel-demo",
        "get",
        "pods",
        "-o",
        "json",
    )
    svc, _ = service({argv: "not-json"})

    with pytest.raises(K8sROError) as exc:
        run(svc.list_resources(namespace="otel-demo", resource="pods"))

    assert exc.value.code == "invalid_kubectl_json"


def test_mcp_tools_are_all_read_only_and_do_not_accept_kubeconfig():
    from mcp_servers.k8s_ro.server import mcp

    tools = run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {
        "k8s_get_resource",
        "k8s_list_resources",
        "k8s_list_events",
        "k8s_pod_logs",
        "k8s_cluster_inventory",
    }
    for tool in by_name.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert "kubeconfig" not in tool.input_schema["properties"]


def test_create_server_accepts_http_auth_injection():
    from mcp.server.auth.settings import AuthSettings

    from mcp_servers.http_runtime import StaticBearerTokenVerifier
    from mcp_servers.k8s_ro.server import create_server

    svc, _ = service({})
    auth = AuthSettings(
        issuer_url="https://issuer.example.test",
        resource_server_url="https://mcp.example.test/k8s",
        required_scopes=["k8s_ro:read"],
    )
    verifier = StaticBearerTokenVerifier(
        token="t" * 40,
        scopes=["k8s_ro:read"],
        resource="https://mcp.example.test/k8s",
    )

    server = create_server(service=svc, auth=auth, token_verifier=verifier)

    assert server is not None
