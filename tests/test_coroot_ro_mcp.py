"""Unit and MCP contract tests for the Coroot read-only projection."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import pytest

from mcp_servers.common.scope import ObservationScope
from mcp_servers.coroot_ro.server import create_server
from mcp_servers.coroot_ro.service import (
    CorootROError,
    CorootROService,
    HttpResponse,
    RuntimeConfig,
)


class FakeCorootTransport:
    """Records only GET-shaped calls and returns deterministic endpoint payloads."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        self.calls.append({"base_url": base_url, "path": path, "params": dict(params), "headers": dict(headers)})
        if path.endswith("query_range"):
            return HttpResponse(200, {"status": "success", "data": {"resultType": "matrix", "result": []}})
        if path.endswith("tracing"):
            return HttpResponse(
                200,
                {
                    "spans": [
                        {"service": "cartservice", "duration": 250, "trace_id": "in-scope"},
                        {"service": "paymentservice", "duration": 900, "trace_id": "other-service"},
                    ]
                },
            )
        if path.endswith("logs"):
            return HttpResponse(
                200,
                {"entries": [{"message": "cart timeout", "timestamp": 1}, {"message": "payment ok", "timestamp": 2}]},
            )
        return HttpResponse(404, {})


def run(awaitable: Any) -> Any:
    """Synchronously execute MCP server coroutines in unit tests."""

    return asyncio.run(awaitable)


def service() -> tuple[CorootROService, FakeCorootTransport]:
    """Create a Controller-scoped service backed by an inspectable fake."""

    transport = FakeCorootTransport()
    config = RuntimeConfig(
        base_url="http://coroot.example.test",
        project_id="cluster-a",
        scope=ObservationScope(namespace="otel-demo", application="cluster-a:otel-demo:Deployment:cartservice"),
        allowed_services=frozenset({"cartservice"}),
    )
    return CorootROService(config=config, transport=transport), transport


def test_production_coroot_config_requires_identity_instead_of_anonymous_admin(monkeypatch):
    for key, value in {
        "RESBENCH_COROOT_URL": "http://coroot.example.test",
        "RESBENCH_COROOT_PROJECT_ID": "project",
        "RESBENCH_COROOT_APPLICATION_ID": "otel-demo:Deployment:cart",
        "RESBENCH_COROOT_ALLOWED_NAMESPACE": "otel-demo",
        "RESBENCH_COROOT_ALLOWED_SERVICES": "cartservice",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RESBENCH_COROOT_BEARER_TOKEN", raising=False)
    with pytest.raises(CorootROError) as failure:
        RuntimeConfig.from_env()
    assert failure.value.code == "missing_readonly_identity"
    monkeypatch.setenv("RESBENCH_COROOT_BEARER_TOKEN", "controller-readonly-token")
    assert RuntimeConfig.from_env().bearer_token == "controller-readonly-token"
    monkeypatch.setenv("RESBENCH_COROOT_URL", "http://admin:password@coroot.example.test")
    with pytest.raises(CorootROError) as failure:
        RuntimeConfig.from_env()
    assert failure.value.code == "invalid_coroot_url"


def test_coroot_transport_rejects_redirect_before_forwarding_identity():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from mcp_servers.coroot_ro.service import UrlLibCorootTransport

    paths = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/unexpected-destination")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = run(UrlLibCorootTransport().get_json(
            base_url=f"http://127.0.0.1:{server.server_port}", path="/original",
            params={}, headers={"Authorization": "Bearer readonly-test-token"},
            timeout_seconds=1, max_bytes=1000,
        ))
        assert response.status_code == 302
        assert paths == ["/original"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_metrics_injects_namespace_into_downstream_prometheus_query() -> None:
    observer, transport = service()

    result = run(observer.metrics_range(metric="http_requests_total", start=100, end=160, labels={"pod": "cart-1"}))

    assert result["ok"] is True
    call = transport.calls[-1]
    assert call["path"] == "/api/project/cluster-a/prom/api/v1/query_range"
    assert call["params"]["query"] == 'http_requests_total{namespace="otel-demo",pod="cart-1"}'
    assert call["params"]["start"] == 100
    assert result["query_scope"] == {"namespace_matcher_injected": True, "application_path_scope": False}


def test_metrics_rejects_namespace_override_and_overlong_window() -> None:
    observer, _ = service()

    with pytest.raises(CorootROError, match="Controller-owned"):
        run(observer.metrics_range(metric="up", start=100, end=120, labels={"namespace": "other"}))
    with pytest.raises(CorootROError, match="exceeds"):
        run(observer.metrics_range(metric="up", start=1, end=6 * 60 * 60 + 2))


def test_trace_and_log_paths_are_bound_to_controller_application() -> None:
    observer, transport = service()

    traces = run(observer.traces_find(service="cartservice", start=100, end=200, min_duration_ms=200))
    logs = run(observer.logs_range(service="cartservice", start=100, end=200, pattern="timeout"))

    assert traces["traces"] == [{"service": "cartservice", "duration": 250, "trace_id": "in-scope"}]
    assert logs["entries"] == [{"message": "cart timeout", "timestamp": 1}]
    assert transport.calls[0]["path"] == "/api/project/cluster-a/app/cluster-a%3Aotel-demo%3ADeployment%3Acartservice/tracing"
    assert transport.calls[0]["params"]["from"] == 100_000
    assert transport.calls[0]["params"]["trace"] == "otel::::-0.2"
    assert transport.calls[1]["path"].endswith("/logs")
    assert "query" in transport.calls[1]["params"]


def test_service_scope_rejects_unknown_service_before_backend_call() -> None:
    observer, transport = service()

    with pytest.raises(CorootROError, match="outside"):
        run(observer.logs_range(service="paymentservice", start=100, end=200))

    assert transport.calls == []


def test_actual_mcp_list_and_calls_are_read_only_and_descriptions_are_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESBENCH_MCP_POLICY_FILE", raising=False)
    observer, _ = service()
    server = create_server(service=observer)
    tools = {tool.name: tool for tool in run(server.list_tools())}

    assert set(tools) == {"coroot_metrics_range", "coroot_traces_find", "coroot_logs_range"}
    forbidden = ("alternative", "fallback", "backup", "test", "替代", "备用", "测试")
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert not any(word in tool.description.lower() for word in forbidden)

    successful = run(server.call_tool("coroot_metrics_range", {"metric": "up", "start": 100, "end": 200}))
    denied = run(server.call_tool("coroot_metrics_range", {"metric": "up", "start": 100, "end": 200, "labels": {"namespace": "x"}}))
    assert successful.structured_content["ok"] is True
    assert denied.structured_content["error"]["code"] == "namespace_override"
