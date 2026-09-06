from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_servers.bladeai_k8s_proxy.service import BladeAIKubernetesProxy, ProxyConfig
from mcp_servers.http_runtime import PolicyGate
from stage2_service import bladeai_read_cli
from stage2_service.bladeai_read_cli import HTTPProxyTransport, ReadCliError, main


class FakeTransport:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, token: str) -> bytes:
        self.calls.append((path, token))
        value = self.responses[path]
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return json.dumps(value).encode("utf-8")


class ProxyValidatingTransport:
    def __init__(self, responses: dict[str, object]):
        self.backend = ProxyBackend(responses)
        self.proxy = BladeAIKubernetesProxy(
            ProxyConfig(namespace="otel-demo", token="x" * 32),
            self.backend,
            PolicyGate(server_name="k8s_ro", policy_file=None),
        )

    def get(self, path: str, token: str) -> bytes:
        import asyncio

        status, body, _headers = asyncio.run(self.proxy.forward(method="GET", target=path, token=token))
        if status >= 400:
            raise ReadCliError(f"proxy rejected Kubernetes read with status {status}")
        return body


class ProxyBackend:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, headers: dict[str, str]):
        self.calls.append((method, path))
        value = self.responses[path]
        if isinstance(value, bytes):
            body = value
        elif isinstance(value, str):
            body = value.encode("utf-8")
        else:
            body = json.dumps(value).encode("utf-8")
        return 200, body, {"Content-Type": "application/json"}


@pytest.fixture
def kubeconfig_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bladeai-kubeconfig.json"
    path.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "bladeai-loopback",
                        "cluster": {"server": "http://127.0.0.1:18481"},
                    }
                ],
                "users": [{"name": "bladeai-trial", "user": {"token": "x" * 32}}],
                "contexts": [
                    {
                        "name": "bladeai-trial",
                        "context": {
                            "cluster": "bladeai-loopback",
                            "user": "bladeai-trial",
                            "namespace": "otel-demo",
                        },
                    }
                ],
                "current-context": "bladeai-trial",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLADE_AI_KUBECONFIG_PATH", str(path))
    return path


def test_config_current_context_uses_injected_kubeconfig(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["--kubeconfig", str(kubeconfig_path), "config", "current-context"]) == 0
    assert capsys.readouterr().out == "bladeai-trial\n"


def test_get_sdk_json_resources_and_pluralizes_named_pod(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    pod = {
        "kind": "Pod",
        "metadata": {"name": "cart-abc", "uid": "uid-1"},
        "spec": {"nodeName": "worker-a"},
        "status": {"phase": "Running", "podIP": "10.0.0.7"},
    }
    pods = {"kind": "PodList", "items": [pod]}
    transport = FakeTransport(
        {
            "/api/v1/namespaces/otel-demo/pods": pods,
            "/api/v1/namespaces/otel-demo/endpoints": {"kind": "EndpointsList", "items": []},
            "/api/v1/namespaces/otel-demo/events": {"kind": "EventList", "items": []},
            "/api/v1/namespaces/otel-demo/pods/cart-abc": pod,
        }
    )

    main(["get", "pods", "-n", "otel-demo", "-o", "json"], transport=transport)
    assert json.loads(capsys.readouterr().out)["items"][0]["metadata"]["name"] == "cart-abc"
    main(["get", "endpoints", "-n", "otel-demo", "-o", "json"], transport=transport)
    assert json.loads(capsys.readouterr().out)["kind"] == "EndpointsList"
    main(["get", "events", "-n", "otel-demo", "-o", "json"], transport=transport)
    assert json.loads(capsys.readouterr().out)["kind"] == "EventList"

    main(["get", "pod", "cart-abc", "-n", "otel-demo", "-o", "jsonpath={.status.podIP}"], transport=transport)
    assert capsys.readouterr().out == "10.0.0.7\n"
    main(["get", "pod", "cart-abc", "-o", "jsonpath={.spec.nodeName}", "-n", "otel-demo"], transport=transport)
    assert capsys.readouterr().out == "worker-a\n"
    main(["get", "pod", "cart-abc", "-n", "otel-demo", "-o", "jsonpath={.metadata.uid}"], transport=transport)
    assert capsys.readouterr().out == "uid-1\n"

    assert ("/api/v1/namespaces/otel-demo/pods/cart-abc", "x" * 32) in transport.calls
    assert all("/pod/" not in path for path, _token in transport.calls)


def test_get_labels_field_selectors_and_table_formatting(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    pods = {
        "kind": "PodList",
        "items": [
            {
                "kind": "Pod",
                "metadata": {"name": "cart-abc"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True, "restartCount": 2}],
                },
            }
        ],
    }
    path = "/api/v1/namespaces/otel-demo/pods?labelSelector=app%3Dcart&fieldSelector=status.phase%3DRunning"
    transport = FakeTransport({path: pods})

    main(
        ["get", "pods", "-l", "app=cart", "--field-selector=status.phase=Running", "-n", "otel-demo"],
        transport=transport,
    )

    output = capsys.readouterr().out
    assert output.splitlines()[0] == "NAME\tREADY\tSTATUS\tRESTARTS\tAGE"
    assert "cart-abc\t1/1\tRunning\t2\t<unknown>" in output
    assert transport.calls == [(path, "x" * 32)]


def test_cli_generated_paths_are_accepted_by_current_proxy(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    pods = {
        "kind": "PodList",
        "items": [
            {
                "kind": "Pod",
                "metadata": {"name": "cart-abc"},
                "spec": {"nodeName": "worker-a"},
                "status": {"phase": "Running"},
            }
        ],
    }
    transport = ProxyValidatingTransport(
        {
            "/api/v1/namespaces/otel-demo/pods?labelSelector=app%3Dcart&fieldSelector=status.phase%3DRunning": pods,
            "/api/v1/namespaces/otel-demo/pods/cart-abc/log?tailLines=200&sinceTime=2026-09-05T01%3A02%3A03Z": "ok\n",
            "/api/v1/namespaces/otel-demo/pods": pods,
            "/apis/metrics.k8s.io/v1beta1/nodes/worker-a": {
                "kind": "NodeMetrics",
                "metadata": {"name": "worker-a"},
                "usage": {"cpu": "100m", "memory": "128Mi"},
            },
        }
    )

    main(
        ["get", "pods", "-n", "otel-demo", "-l", "app=cart", "--field-selector=status.phase=Running"],
        transport=transport,
    )
    assert "cart-abc" in capsys.readouterr().out
    main(
        ["logs", "cart-abc", "-n", "otel-demo", "--since-time=2026-09-05T01:02:03Z", "--tail=200"],
        transport=transport,
    )
    assert capsys.readouterr().out == "ok\n\n"
    main(["top", "node", "worker-a", "--no-headers"], transport=transport)
    assert capsys.readouterr().out == "worker-a\t100m\t128Mi\n"
    assert ("GET", "/apis/metrics.k8s.io/v1beta1/nodes/worker-a") in transport.backend.calls


def test_sdk_node_running_pod_selector_stays_namespace_scoped(kubeconfig_path, capsys):
    path = "/api/v1/namespaces/otel-demo/pods?fieldSelector=spec.nodeName%3Dworker-a%2Cstatus.phase%3DRunning"
    transport = ProxyValidatingTransport({path: {"kind": "PodList", "items": []}})
    main(
        ["get", "pod", "-n", "otel-demo", "--field-selector=spec.nodeName=worker-a,status.phase=Running", "-o", "json"],
        transport=transport,
    )
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert transport.backend.calls == [("GET", path)]


def test_top_pod_node_and_logs_do_not_reference_unbound_format(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    transport = FakeTransport(
        {
            "/apis/metrics.k8s.io/v1beta1/namespaces/otel-demo/pods/cart-abc": {
                "kind": "PodMetrics",
                "metadata": {"name": "cart-abc"},
                "containers": [{"usage": {"cpu": "25m", "memory": "64Mi"}}],
            },
            "/apis/metrics.k8s.io/v1beta1/nodes/worker-a": {
                "kind": "NodeMetrics",
                "metadata": {"name": "worker-a"},
                "usage": {"cpu": "120m", "memory": "512Mi"},
            },
            "/api/v1/namespaces/otel-demo/pods/cart-abc/log?tailLines=200&sinceTime=2026-09-05T01%3A02%3A03Z": "line 1\nline 2\n",
        }
    )

    main(["top", "pod", "cart-abc", "-n", "otel-demo", "--no-headers"], transport=transport)
    assert capsys.readouterr().out == "cart-abc\t25m\t65536Ki\n"
    main(["top", "node", "worker-a", "--no-headers"], transport=transport)
    assert capsys.readouterr().out == "worker-a\t120m\t512Mi\n"
    main(
        ["logs", "cart-abc", "-n", "otel-demo", "--since-time=2026-09-05T01:02:03Z", "--tail=200"],
        transport=transport,
    )
    assert capsys.readouterr().out == "line 1\nline 2\n\n"


def test_top_node_without_name_derives_namespace_nodes(kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]):
    transport = FakeTransport(
        {
            "/api/v1/namespaces/otel-demo/pods": {
                "items": [
                    {"metadata": {"name": "cart-a"}, "spec": {"nodeName": "worker-b"}},
                    {"metadata": {"name": "cart-b"}, "spec": {"nodeName": "worker-a"}},
                    {"metadata": {"name": "cart-c"}, "spec": {"nodeName": "worker-a"}},
                ]
            },
            "/apis/metrics.k8s.io/v1beta1/nodes/worker-a": {
                "metadata": {"name": "worker-a"},
                "usage": {"cpu": "10m", "memory": "100Mi"},
            },
            "/apis/metrics.k8s.io/v1beta1/nodes/worker-b": {
                "metadata": {"name": "worker-b"},
                "usage": {"cpu": "20m", "memory": "200Mi"},
            },
        }
    )

    main(["top", "node", "--no-headers"], transport=transport)

    assert capsys.readouterr().out == "worker-a\t10m\t100Mi\nworker-b\t20m\t200Mi\n"
    assert transport.calls == [
        ("/api/v1/namespaces/otel-demo/pods", "x" * 32),
        ("/apis/metrics.k8s.io/v1beta1/nodes/worker-a", "x" * 32),
        ("/apis/metrics.k8s.io/v1beta1/nodes/worker-b", "x" * 32),
    ]


def test_rejects_unknown_flags_mutating_commands_and_kubeconfig_mismatch(kubeconfig_path: Path, tmp_path: Path):
    with pytest.raises(ReadCliError, match="unsupported"):
        main(["get", "pods", "--watch"], transport=FakeTransport({}))
    with pytest.raises(ReadCliError, match="read-only"):
        main(["exec", "cart-abc", "--", "sh"], transport=FakeTransport({}))
    with pytest.raises(ReadCliError, match="does not match"):
        main(["--kubeconfig", str(tmp_path / "other.json"), "get", "pods"], transport=FakeTransport({}))


def test_rejects_raw_prefix_address_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "bad-kubeconfig.json"
    data = {
        "clusters": [{"name": "c", "cluster": {"server": "http://127.0.0.1:18481.evil"}}],
        "users": [{"name": "u", "user": {"token": "secret-token"}}],
        "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u", "namespace": "otel-demo"}}],
        "current-context": "ctx",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("BLADE_AI_KUBECONFIG_PATH", str(path))

    with pytest.raises(ReadCliError, match="exact loopback"):
        main(["get", "pods"], transport=FakeTransport({}))


def test_http_transport_rejects_redirects_without_following(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        status = 302

        def read(self, _size: int) -> bytes:
            return b"redirect"

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: int):
            self.host = host
            self.port = port
            self.timeout = timeout

        def request(self, method: str, path: str, headers: dict[str, str]):
            self.requested = (method, path, headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(bladeai_read_cli.http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(ReadCliError, match="redirects") as exc_info:
        HTTPProxyTransport().get("/api/v1/namespaces/otel-demo/pods", "token-that-must-not-print")
    assert "token-that-must-not-print" not in str(exc_info.value)


def test_http_transport_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        status = 200

        def read(self, size: int) -> bytes:
            return b"x" * size

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: int):
            self.host = host
            self.port = port
            self.timeout = timeout

        def request(self, method: str, path: str, headers: dict[str, str]):
            self.requested = (method, path, headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(bladeai_read_cli.http.client, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(bladeai_read_cli, "MAX_RESPONSE_BYTES", 4)

    with pytest.raises(ReadCliError, match="bounded output"):
        HTTPProxyTransport().get("/api/v1/namespaces/otel-demo/pods", "token-that-must-not-print")
