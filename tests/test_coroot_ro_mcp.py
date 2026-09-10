"""Unit and MCP contract tests for the Coroot read-only projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
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
from stage2_service.capability_loss.factory import _metric_result_covers_window
from stage2_service.capability_loss.records import FaultRunningWindow


SESSION = "eyJJZCI6MX0=.signature_value="


class FakeCorootTransport:
    """Records GET-shaped calls and returns native Coroot endpoint payloads."""

    def __init__(
        self,
        *,
        user: Mapping[str, Any] | None = None,
        panel: Mapping[str, Any] | None = None,
        series: Mapping[str, Any] | None = None,
        trace_view: Mapping[str, Any] | None = None,
        log_view: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.user = dict(user or {"name": "stage2", "email": "stage2@example.invalid", "anonymous": False, "role": "Viewer", "readonly": True})
        self.panel = dict(panel) if panel is not None else _native_panel_response()
        self.series = dict(series) if series is not None else _native_series_response()
        self.trace_view = dict(trace_view) if trace_view is not None else _native_trace_view()
        self.log_view = dict(log_view) if log_view is not None else _native_log_view()

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
        if headers != {"Cookie": f"coroot_session={SESSION}"}:
            return HttpResponse(401, {})
        if path == "/api/user":
            return HttpResponse(200, self.user)
        if path == "/api/project/cluster-a/panel/data":
            return HttpResponse(200, self.panel)
        if path == "/api/project/cluster-a/prom/api/v1/series":
            return HttpResponse(200, self.series)
        if path == "/api/project/cluster-a/prom/api/v1/query_range":
            raise AssertionError("coroot_ro must not call the unsupported Auth prom/query_range route")
        if path.endswith("/tracing"):
            return HttpResponse(200, {"context": {"status": {"status": "ok"}}, "data": self.trace_view})
        if path.endswith("/logs"):
            return HttpResponse(200, {"context": {"status": {"status": "ok"}}, "data": self.log_view})
        return HttpResponse(404, {})


def _native_series_labels(metric: str = "kube_pod_info") -> dict[str, str]:
    return {
        "__name__": metric,
        "coroot_project_id": "9auios5b",
        "created_by_kind": "ReplicaSet",
        "created_by_name": "cart-7c58f6bb56",
        "host_ip": "192.168.0.118",
        "host_network": "false",
        "instance": "127.0.0.1:10303",
        "job": "coroot-cluster-agent",
        "namespace": "otel-demo",
        "node": "tcse-v100-03",
        "pod": "cart-7c58f6bb56-8wwsb",
        "pod_ip": "10.0.2.195",
        "uid": "52f278d8-d2c0-41e7-b37e-2ad9f4a2ed4f",
    }


def _coroot_series_name(labels: Mapping[str, str]) -> str:
    return "{" + ",".join(f"{key}={labels[key]}" for key in sorted(labels)) + "}"


def _native_series_response(metric: str = "kube_pod_info") -> dict[str, Any]:
    return {"status": "success", "data": [_native_series_labels(metric)]}


def _native_panel_response() -> dict[str, Any]:
    labels = _native_series_labels()
    return {
        "chart": {
            "ctx": {
                "from": 100_000,
                "to": 130_000,
                "step": 15_000,
                "raw_step": 15_000,
                "truncated": False,
            },
            "series": [
                {
                    "name": _coroot_series_name(labels),
                    "data": [1, 2, 2.5],
                    "value": "",
                }
            ],
        }
    }


def _native_trace_view() -> dict[str, Any]:
    return {
        "status": "ok",
        "message": "",
        "spans": [
            {"service": "cartservice", "duration": 250, "trace_id": "in-scope", "timestamp": 1_000},
            {"service": "paymentservice", "duration": 900, "trace_id": "other-service", "timestamp": 2_000},
        ],
        "limit": 100,
    }


def _native_log_view() -> dict[str, Any]:
    return {
        "status": "ok",
        "source": "otel",
        "service": "cartservice",
        "entries": [
            {"message": "cart timeout", "timestamp": 1_000, "attributes": {"severity": "error"}},
            {"message": "payment ok", "timestamp": 2_000, "attributes": {"severity": "info"}},
        ],
        "limit": 100,
    }


def run(awaitable: Any) -> Any:
    """Synchronously execute MCP server coroutines in unit tests."""

    return asyncio.run(awaitable)


def service(
    *,
    user: Mapping[str, Any] | None = None,
    panel: Mapping[str, Any] | None = None,
    series: Mapping[str, Any] | None = None,
    trace_view: Mapping[str, Any] | None = None,
    log_view: Mapping[str, Any] | None = None,
) -> tuple[CorootROService, FakeCorootTransport]:
    """Create a Controller-scoped service backed by an inspectable fake."""

    transport = FakeCorootTransport(user=user, panel=panel, series=series, trace_view=trace_view, log_view=log_view)
    config = RuntimeConfig(
        base_url="http://coroot.example.test",
        project_id="cluster-a",
        scope=ObservationScope(namespace="otel-demo", application="cluster-a:otel-demo:Deployment:cartservice"),
        allowed_services=frozenset({"cartservice"}),
        session_cookie=SESSION,
    )
    return CorootROService(config=config, transport=transport), transport


def test_production_coroot_config_requires_native_viewer_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "RESBENCH_COROOT_URL": "http://coroot.example.test",
        "RESBENCH_COROOT_PROJECT_ID": "project",
        "RESBENCH_COROOT_APPLICATION_ID": "otel-demo:Deployment:cart",
        "RESBENCH_COROOT_ALLOWED_NAMESPACE": "otel-demo",
        "RESBENCH_COROOT_ALLOWED_SERVICES": "cartservice",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RESBENCH_COROOT_SESSION_COOKIE", raising=False)
    monkeypatch.setenv("RESBENCH_COROOT_BEARER_TOKEN", "ignored-old-token")
    with pytest.raises(CorootROError) as failure:
        RuntimeConfig.from_env()
    assert failure.value.code == "missing_readonly_identity"

    monkeypatch.setenv("RESBENCH_COROOT_SESSION_COOKIE", SESSION)
    config = RuntimeConfig.from_env()
    assert config.session_cookie == SESSION
    assert not hasattr(config, "bearer_token")

    for bad in ("coroot_session=a.b", "a.b; other=c", "a.b\nother=c", "a b.c", f" {SESSION}", f"{SESSION}\n"):
        monkeypatch.setenv("RESBENCH_COROOT_SESSION_COOKIE", bad)
        with pytest.raises(CorootROError) as failure:
            RuntimeConfig.from_env()
        assert failure.value.code == "invalid_readonly_identity"

    monkeypatch.setenv("RESBENCH_COROOT_SESSION_COOKIE", SESSION)
    monkeypatch.setenv("RESBENCH_COROOT_URL", "http://admin:password@coroot.example.test")
    with pytest.raises(CorootROError) as failure:
        RuntimeConfig.from_env()
    assert failure.value.code == "invalid_coroot_url"


def test_coroot_transport_rejects_redirect_before_forwarding_identity() -> None:
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
            params={}, headers={"Cookie": f"coroot_session={SESSION}"},
            timeout_seconds=1, max_bytes=1000,
        ))
        assert response.status_code == 302
        assert paths == ["/original"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "user,code",
    [
        ({"name": "anon", "role": "Admin", "anonymous": True}, "anonymous_identity_forbidden"),
        ({"name": "missing-anonymous", "role": "Viewer", "readonly": True}, "anonymous_identity_forbidden"),
        ({"name": "null-anonymous", "role": "Viewer", "anonymous": None, "readonly": True}, "anonymous_identity_forbidden"),
        ({"name": "admin", "role": "Admin", "anonymous": False, "readonly": False}, "readonly_identity_unqualified"),
        ({"name": "editor", "role": "Editor", "anonymous": False, "readonly": True}, "readonly_identity_unqualified"),
    ],
)
def test_every_observation_requires_non_anonymous_viewer_session(user: Mapping[str, Any], code: str) -> None:
    observer, transport = service(user=user)

    with pytest.raises(CorootROError) as failure:
        run(observer.metrics_range(metric="up", start=100, end=160))

    assert failure.value.code == code
    assert [call["path"] for call in transport.calls] == ["/api/user"]


@pytest.mark.parametrize(
    "session_cookie,code",
    [
        (None, "missing_readonly_identity"),
        (f" {SESSION}", "invalid_readonly_identity"),
        (f"{SESSION}\n", "invalid_readonly_identity"),
        ("coroot_session=a.b", "invalid_readonly_identity"),
    ],
)
def test_direct_runtime_config_with_missing_or_dirty_session_cookie_fails_closed_before_backend_call(session_cookie: str | None, code: str) -> None:
    transport = FakeCorootTransport()
    observer = CorootROService(
        config=RuntimeConfig(
            base_url="http://coroot.example.test",
            project_id="cluster-a",
            scope=ObservationScope(namespace="otel-demo", application="cluster-a:otel-demo:Deployment:cartservice"),
            allowed_services=frozenset({"cartservice"}),
            session_cookie=session_cookie,
        ),
        transport=transport,
    )

    with pytest.raises(CorootROError) as failure:
        run(observer.metrics_range(metric="up", start=100, end=160))

    assert failure.value.code == code
    assert transport.calls == []


def test_metrics_uses_native_panel_data_and_reconstructs_chart_timestamps_without_synthetic_labels() -> None:
    observer, transport = service()

    result = run(observer.metrics_range(metric="kube_pod_info", start=100, end=160, labels={"pod": "cart-1"}))

    assert result["ok"] is True
    assert [call["path"] for call in transport.calls] == [
        "/api/user",
        "/api/project/cluster-a/panel/data",
        "/api/user",
        "/api/project/cluster-a/prom/api/v1/series",
    ]
    assert transport.calls[0]["headers"] == {"Cookie": f"coroot_session={SESSION}"}
    call = transport.calls[1]
    assert call["params"]["from"] == 100_000
    assert call["params"]["to"] == 160_000
    panel = json.loads(call["params"]["query"])
    assert panel["source"]["metrics"]["queries"][0]["query"] == 'kube_pod_info{namespace="otel-demo",pod="cart-1"}'
    assert transport.calls[3]["params"] == {
        "match[]": 'kube_pod_info{namespace="otel-demo",pod="cart-1"}',
        "start": 100,
        "end": 160,
    }
    assert result["query_scope"] == {
        "namespace_matcher_injected": True,
        "application_path_scope": False,
        "backend": "coroot_panel_data_with_prom_series_metadata",
    }
    matrix = result["data"]
    assert matrix["resultType"] == "matrix"
    assert matrix["context"] == {"from": 100, "to": 130, "step": 15, "raw_step": 15}
    assert matrix["truncated"] is False
    assert matrix["result"][0]["metric"] == _native_series_labels()
    assert matrix["result"][0]["coroot_series"] == {"name": _coroot_series_name(_native_series_labels())}
    assert matrix["result"][0]["values"] == [[100, 1], [115, 2], [130, 2.5]]
    assert "pod_uid" not in json.dumps(matrix)


def test_metrics_does_not_bind_uid_when_chart_name_has_no_unique_series_metadata_match() -> None:
    duplicate = {"status": "success", "data": [_native_series_labels(), _native_series_labels()]}
    observer, _ = service(series=duplicate)

    result = run(observer.metrics_range(metric="kube_pod_info", start=100, end=160, labels={"pod": "cart-1"}))

    assert result["data"]["result"][0]["metric"] == {}
    assert result["data"]["result"][0]["coroot_series"]["name"] == _coroot_series_name(_native_series_labels())


@pytest.mark.parametrize(
    "panel,error_text",
    [
        ({"chart": {"ctx": {"from": float("nan"), "to": 130_000, "step": 15_000}, "series": []}}, "not finite"),
        ({"chart": {"ctx": {"from": 100_000, "to": 115_000, "step": 15_000}, "series": [{"name": _coroot_series_name(_native_series_labels()), "data": [1, 2, 3]}]}}, "extends past"),
        ({"chart": {"ctx": {"from": 100_000, "to": 130_000, "step": 15_000}, "series": [{"name": _coroot_series_name(_native_series_labels()), "data": [1, float("inf")]}]}}, "sample value is not finite"),
    ],
)
def test_metrics_rejects_non_finite_chart_context_and_samples_after_ctx_to(panel: Mapping[str, Any], error_text: str) -> None:
    observer, _ = service(panel=panel)

    with pytest.raises(CorootROError, match=error_text):
        run(observer.metrics_range(metric="kube_pod_info", start=100, end=160))


def test_metrics_accepts_empty_panel_as_normal_empty_matrix_result() -> None:
    observer, _ = service(panel={})

    result = run(observer.metrics_range(metric="kube_pod_info", start=100, end=160))

    assert result["data"] == {"resultType": "matrix", "result": [], "coroot_chart_context": None, "truncated": False}


def test_coroot_matrix_output_remains_usable_by_d7_factory_relevance_gate() -> None:
    labels = _native_series_labels("http_server_duration_milliseconds_bucket")
    panel = _native_panel_response()
    panel["chart"]["series"][0]["name"] = _coroot_series_name(labels)
    series = {"status": "success", "data": [labels]}
    observer, _ = service(panel=panel, series=series)

    result = run(observer.metrics_range(metric="http_server_duration_milliseconds_bucket", start=100, end=160))

    assert _metric_result_covers_window(
        "coroot_metrics_range",
        {"metric": "http_server_duration_milliseconds_bucket", "start": 100, "end": 160},
        result,
        "52f278d8-d2c0-41e7-b37e-2ad9f4a2ed4f",
        FaultRunningWindow(
            started_at=datetime.fromtimestamp(105, UTC),
            ended_at=datetime.fromtimestamp(125, UTC),
            oracle_record_ref="private://oracle",
        ),
        "network-delay",
    )


def test_metrics_rejects_namespace_override_and_overlong_window_before_backend_call() -> None:
    observer, transport = service()

    with pytest.raises(CorootROError, match="Controller-owned"):
        run(observer.metrics_range(metric="up", start=100, end=120, labels={"namespace": "other"}))
    with pytest.raises(CorootROError, match="exceeds"):
        run(observer.metrics_range(metric="up", start=1, end=6 * 60 * 60 + 2))

    assert transport.calls == []


def test_trace_and_log_paths_use_native_coroot_wrapped_views() -> None:
    observer, transport = service()

    traces = run(observer.traces_find(service="cartservice", start=100, end=200, min_duration_ms=200))
    logs = run(observer.logs_range(service="cartservice", start=100, end=200, pattern="timeout"))

    assert traces["traces"] == [{"service": "cartservice", "duration": 250, "trace_id": "in-scope", "timestamp": 1_000}]
    assert logs["entries"] == [{"message": "cart timeout", "timestamp": 1_000, "attributes": {"severity": "error"}}]
    assert [call["path"] for call in transport.calls] == [
        "/api/user",
        "/api/project/cluster-a/app/cluster-a%3Aotel-demo%3ADeployment%3Acartservice/tracing",
        "/api/user",
        "/api/project/cluster-a/app/cluster-a%3Aotel-demo%3ADeployment%3Acartservice/logs",
    ]
    assert transport.calls[1]["params"]["from"] == 100_000
    assert transport.calls[1]["params"]["trace"] == "::100000-200000:0.2-"
    assert json.loads(transport.calls[3]["params"]["query"]) == {"view": "messages", "limit": 100}


def test_trace_and_log_native_null_lists_are_honest_empty_results() -> None:
    observer, _ = service(
        trace_view={"status": "ok", "message": "Using traces of <i></i>", "sources": [{"type": "otel"}], "spans": None},
        log_view={"status": "ok", "message": "Using OpenTelemetry logs of <i></i>", "source": "", "sources": ["agent"], "entries": None},
    )

    traces = run(observer.traces_find(service="cartservice", start=100, end=200, min_duration_ms=200))
    logs = run(observer.logs_range(service="cartservice", start=100, end=200))

    assert traces["traces"] == []
    assert traces["backend_message"] == "Using traces of <i></i>"
    assert traces["backend_sources"] == [{"type": "otel"}]
    assert logs["entries"] == []
    assert logs["backend_message"] == "Using OpenTelemetry logs of <i></i>"
    assert logs["backend_source"] == ""
    assert logs["backend_sources"] == ["agent"]


def test_raw_legacy_trace_or_log_shapes_are_not_treated_as_native_coroot_api() -> None:
    class RawShapeTransport(FakeCorootTransport):
        async def get_json(self, **kwargs: Any) -> HttpResponse:
            response = await super().get_json(**kwargs)
            path = kwargs["path"]
            if path.endswith("/tracing"):
                return HttpResponse(200, {"spans": [{"service": "cartservice", "duration": 1}]})
            if path.endswith("/logs"):
                return HttpResponse(200, {"entries": [{"message": "legacy"}]})
            return response

    config = RuntimeConfig(
        base_url="http://coroot.example.test",
        project_id="cluster-a",
        scope=ObservationScope(namespace="otel-demo", application="cluster-a:otel-demo:Deployment:cartservice"),
        allowed_services=frozenset({"cartservice"}),
        session_cookie=SESSION,
    )
    observer = CorootROService(config=config, transport=RawShapeTransport())

    with pytest.raises(CorootROError, match="native Coroot trace view"):
        run(observer.traces_find(service="cartservice", start=100, end=200))
    with pytest.raises(CorootROError, match="native Coroot log view"):
        run(observer.logs_range(service="cartservice", start=100, end=200))


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


def _panel_promql(transport: FakeCorootTransport) -> str:
    """The PromQL coroot_ro placed in its dashboard-panel request."""

    import json

    call = next(call for call in transport.calls if call["path"].endswith("/panel/data"))
    return json.loads(call["params"]["query"])["source"]["metrics"]["queries"][0]["query"]


class AnonymousCorootTransport(FakeCorootTransport):
    """Coroot without login: a request must carry no session cookie at all."""

    async def get_json(self, *, base_url, path, params, headers, timeout_seconds, max_bytes):
        assert headers == {}, "anonymous read must not send a session cookie"
        return await super().get_json(
            base_url=base_url,
            path=path,
            params=params,
            headers={"Cookie": f"coroot_session={SESSION}"},
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )


def anonymous_service() -> tuple[CorootROService, FakeCorootTransport]:
    transport = AnonymousCorootTransport()
    config = RuntimeConfig(
        base_url="http://coroot.example.test",
        project_id="cluster-a",
        scope=ObservationScope(namespace="otel-demo", application="cluster-a:otel-demo:Deployment:cartservice"),
        allowed_services=frozenset({"cartservice"}),
        session_cookie=None,
        allow_anonymous_read=True,
    )
    return CorootROService(config=config, transport=transport), transport


def test_anonymous_read_mode_queries_without_cookie_or_identity_check() -> None:
    observer, transport = anonymous_service()

    run(observer.metrics_range(metric="container_resources_cpu_usage_seconds_total", start=1_000, end=1_060))

    assert "/api/user" not in [call["path"] for call in transport.calls]


def test_container_metrics_are_scoped_by_container_id_not_namespace() -> None:
    observer, transport = anonymous_service()

    run(observer.metrics_range(metric="container_resources_cpu_usage_seconds_total", start=1_000, end=1_060))

    promql = _panel_promql(transport)
    assert 'container_id=~"/k8s/otel-demo/.*"' in promql
    assert "namespace=" not in promql


def test_non_container_metrics_keep_the_namespace_matcher() -> None:
    observer, transport = anonymous_service()

    run(observer.metrics_range(metric="kube_pod_status_ready", start=1_000, end=1_060))

    assert 'namespace="otel-demo"' in _panel_promql(transport)


def test_config_from_env_allows_anonymous_read_without_a_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "RESBENCH_COROOT_URL": "http://coroot.example.test",
        "RESBENCH_COROOT_PROJECT_ID": "cluster-a",
        "RESBENCH_COROOT_APPLICATION_ID": "cluster-a:otel-demo:Deployment:cart",
        "RESBENCH_COROOT_ALLOWED_NAMESPACE": "otel-demo",
        "RESBENCH_COROOT_ALLOWED_SERVICES": "cart",
        "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RESBENCH_COROOT_SESSION_COOKIE", raising=False)

    config = RuntimeConfig.from_env()

    assert config.session_cookie is None
    assert config.allow_anonymous_read is True


def test_missing_configuration_fails_the_call_not_the_server_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_servers.coroot_ro.service import CorootROError

    for name in ("RESBENCH_COROOT_URL", "RESBENCH_COROOT_PROJECT_ID", "RESBENCH_COROOT_APPLICATION_ID"):
        monkeypatch.delenv(name, raising=False)

    observer = CorootROService(transport=FakeCorootTransport())

    with pytest.raises(CorootROError):
        observer.config
