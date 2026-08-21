from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from mcp_servers.telemetry_ro.service import (
    JAEGER_SERVICE_ALLOWLIST_ENV,
    JAEGER_URL_ENV,
    LOKI_URL_ENV,
    NAMESPACE_ALLOWLIST_ENV,
    PROMETHEUS_URL_ENV,
    MAX_TIME_WINDOW_SECONDS,
    HttpResponse,
    RuntimeConfig,
    TelemetryROError,
    TelemetryROService,
)


def run(coro):
    return asyncio.run(coro)


@dataclass
class FakeTransport:
    responses: dict[tuple[str, str], Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        self.calls.append(
            {
                "base_url": base_url,
                "path": path,
                "params": dict(params),
                "timeout_seconds": timeout_seconds,
                "max_bytes": max_bytes,
            }
        )
        return HttpResponse(200, self.responses[(base_url, path)])


def service(responses: dict[tuple[str, str], Any]) -> tuple[TelemetryROService, FakeTransport]:
    transport = FakeTransport(responses)
    config = RuntimeConfig(
        prometheus_url="http://prometheus.monitoring.svc:9090",
        jaeger_url="http://jaeger.monitoring.svc:16686",
        loki_url="http://loki.monitoring.svc:3100",
        namespace_allowlist=frozenset({"otel-demo"}),
        jaeger_service_allowlist=frozenset({"checkoutservice", "paymentservice"}),
        timeout_seconds=2.0,
    )
    return TelemetryROService(config, transport), transport


def test_runtime_config_rejects_userinfo_endpoint(monkeypatch):
    monkeypatch.setenv(PROMETHEUS_URL_ENV, "http://user:pass@prometheus.example")
    monkeypatch.setenv(NAMESPACE_ALLOWLIST_ENV, "otel-demo")
    monkeypatch.setenv(JAEGER_SERVICE_ALLOWLIST_ENV, "checkoutservice")

    with pytest.raises(TelemetryROError) as exc:
        RuntimeConfig.from_env()

    assert exc.value.code == "invalid_telemetry_endpoint"
    assert "credentials" in exc.value.action
    assert "user:pass" not in exc.value.message


def test_runtime_config_requires_scope_allowlists(monkeypatch):
    monkeypatch.setenv(PROMETHEUS_URL_ENV, "http://prometheus.example")
    monkeypatch.setenv(JAEGER_URL_ENV, "http://jaeger.example")
    monkeypatch.setenv(LOKI_URL_ENV, "http://loki.example")
    monkeypatch.delenv(NAMESPACE_ALLOWLIST_ENV, raising=False)
    monkeypatch.setenv(JAEGER_SERVICE_ALLOWLIST_ENV, "checkoutservice")

    with pytest.raises(TelemetryROError) as exc:
        RuntimeConfig.from_env()

    assert exc.value.code == "missing_scope_allowlist"
    assert NAMESPACE_ALLOWLIST_ENV in exc.value.message


def test_missing_endpoint_error_does_not_accept_or_echo_urls():
    svc = TelemetryROService(RuntimeConfig(prometheus_url=None))

    with pytest.raises(TelemetryROError) as exc:
        run(svc.prometheus_query_instant(query="up", time=1700000000))

    assert exc.value.code == "missing_telemetry_endpoint"
    assert PROMETHEUS_URL_ENV in exc.value.action
    assert "http://" not in exc.value.message
    assert "http://" not in exc.value.action


def test_prometheus_instant_query_uses_get_path_time_and_limit():
    svc, transport = service(
        {
            ("http://prometheus.monitoring.svc:9090", "/api/v1/query"): {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"job": "a", "namespace": "otel-demo"}},
                        {"metric": {"job": "b", "namespace": "other"}},
                        {"metric": {"job": "c", "kubernetes_namespace": "otel-demo"}},
                    ],
                },
            }
        }
    )

    result = run(svc.prometheus_query_instant(query="up", time=1700000000, limit=2))

    assert result["ok"] is True
    assert result["returned"] == 2
    assert result["hasMore"] is False
    assert result["scopedOutCount"] == 1
    assert transport.calls[0]["path"] == "/api/v1/query"
    assert transport.calls[0]["params"] == {"query": "up", "time": 1700000000}


def test_prometheus_metric_labels_are_redacted_recursively():
    svc, _transport = service(
        {
            ("http://prometheus.monitoring.svc:9090", "/api/v1/query"): {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {
                                "namespace": "otel-demo",
                                "token": "raw-token-value",
                                "component": "sk-prometheusSecret123",
                            },
                            "value": [1700000000, "1"],
                        }
                    ],
                },
            }
        }
    )

    result = run(svc.prometheus_query_instant(query="up", time=1700000000))
    serialized = str(result)

    assert result["result"][0]["metric"]["token"] == "<redacted>"
    assert "raw-token-value" not in serialized
    assert "sk-prometheusSecret123" not in serialized
    assert "sk-<redacted>" in serialized


def test_prometheus_range_rejects_oversized_window_before_network_call():
    svc, transport = service({})

    with pytest.raises(TelemetryROError) as exc:
        run(
            svc.prometheus_query_range(
                query="rate(http_requests_total[5m])",
                start=10,
                end=10 + MAX_TIME_WINDOW_SECONDS + 1,
                step=30,
            )
        )

    assert exc.value.code == "time_window_too_large"
    assert transport.calls == []


def test_prometheus_series_uses_repeated_match_parameter_and_paginates():
    svc, transport = service(
        {
            ("http://prometheus.monitoring.svc:9090", "/api/v1/series"): {
                "status": "success",
                    "data": [
                        {"__name__": "up", "pod": "a", "namespace": "other"},
                        {"__name__": "up", "pod": "a0", "namespace": "otel-demo"},
                        {"__name__": "up", "pod": "b", "namespace": "otel-demo"},
                        {"__name__": "up", "pod": "c"},
                    ],
            }
        }
    )

    result = run(svc.prometheus_list_series(match=["up{job=\"otel\"}"], start=100, end=200, limit=1, offset=1))

    assert result["items"] == [{"__name__": "up", "pod": "b", "namespace": "otel-demo"}]
    assert result["total"] == 2
    assert result["scopedOutCount"] == 2
    assert transport.calls[0]["params"]["match[]"] == ["up{job=\"otel\"}"]


def test_prometheus_aggregate_without_namespace_is_dropped_fail_closed():
    svc, _transport = service(
        {
            ("http://prometheus.monitoring.svc:9090", "/api/v1/query"): {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"job": "checkout"}, "value": [1700000000, "9"]}],
                },
            }
        }
    )

    result = run(svc.prometheus_query_instant(query="sum(rate(http_requests_total[1m]))", time=1700000000))

    assert result["ok"] is True
    assert result["returned"] == 0
    assert result["scopedOutCount"] == 1
    assert "namespace" in result["scopeWarning"]


def test_jaeger_trace_search_converts_unix_seconds_to_microseconds():
    svc, transport = service(
        {
            ("http://jaeger.monitoring.svc:16686", "/api/traces"): {
                "data": [
                    {"traceID": "abc123", "processes": {"p1": {"serviceName": "checkoutservice"}}},
                    {"traceID": "def456", "processes": {"p1": {"serviceName": "inventoryservice"}}},
                ]
            }
        }
    )

    result = run(
        svc.jaeger_find_traces(
            service="checkoutservice",
            operation="POST /checkout",
            tags='{"error":"true"}',
            start=1700000000,
            end=1700000300,
            limit=10,
        )
    )

    params = transport.calls[0]["params"]
    assert result["returned"] == 1
    assert result["scopedOutCount"] == 1
    assert params["start"] == 1700000000 * 1_000_000
    assert params["end"] == 1700000300 * 1_000_000
    assert params["service"] == "checkoutservice"
    assert params["operation"] == "POST /checkout"
    assert params["tags"] == '{"error":"true"}'


def test_jaeger_rejects_service_outside_allowlist_before_network_call():
    svc, transport = service({})

    with pytest.raises(TelemetryROError) as exc:
        run(svc.jaeger_find_traces(service="inventoryservice", start=100, end=200))

    assert exc.value.code == "jaeger_service_outside_scope"
    assert transport.calls == []


def test_jaeger_get_trace_rejects_trace_with_non_allowlisted_service():
    svc, _transport = service(
        {
            ("http://jaeger.monitoring.svc:16686", "/api/traces/abc123"): {
                "data": [
                    {
                        "traceID": "abc123",
                        "processes": {
                            "p1": {"serviceName": "checkoutservice"},
                            "p2": {"serviceName": "inventoryservice"},
                        },
                        "spans": [],
                    }
                ]
            }
        }
    )

    with pytest.raises(TelemetryROError) as exc:
        run(svc.jaeger_get_trace(trace_id="abc123"))

    assert exc.value.code == "trace_outside_service_scope"


def test_jaeger_trace_tags_are_redacted_recursively():
    svc, _transport = service(
        {
            ("http://jaeger.monitoring.svc:16686", "/api/traces/abc123"): {
                "data": [
                    {
                        "traceID": "abc123",
                        "processes": {"p1": {"serviceName": "checkoutservice"}},
                        "spans": [
                            {
                                "processID": "p1",
                                "tags": [
                                    {"key": "http.authorization", "value": "Bearer jaegerBearerSecret123"},
                                    {"key": "config", "api_key": "raw-jaeger-api-key"},
                                    {"key": "db.statement", "value": "token: raw-jaeger-token"},
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    )

    result = run(svc.jaeger_get_trace(trace_id="abc123"))
    serialized = str(result)

    assert "jaegerBearerSecret123" not in serialized
    assert "raw-jaeger-api-key" not in serialized
    assert "raw-jaeger-token" not in serialized
    assert "Bearer <redacted>" in serialized
    assert "'api_key': '<redacted>'" in serialized
    assert "token: <redacted>" in serialized


def test_jaeger_get_trace_rejects_non_hex_id():
    svc, transport = service({})

    with pytest.raises(TelemetryROError) as exc:
        run(svc.jaeger_get_trace(trace_id="../../etc/passwd"))

    assert exc.value.code == "invalid_trace_id"
    assert transport.calls == []


def test_loki_query_range_converts_unix_seconds_to_nanoseconds():
    svc, transport = service(
        {
            ("http://loki.monitoring.svc:3100", "/loki/api/v1/query_range"): {
                "data": {
                    "resultType": "streams",
                    "result": [
                        {"stream": {"pod": "a", "namespace": "otel-demo"}, "values": []},
                        {"stream": {"pod": "b", "namespace": "other"}, "values": []},
                        {"stream": {"pod": "c"}, "values": []},
                    ],
                }
            }
        }
    )

    result = run(svc.loki_query_range(query='{namespace="otel-demo"}', start=100, end=130, step=5, limit=20))

    params = transport.calls[0]["params"]
    assert result["ok"] is True
    assert result["returned"] == 1
    assert result["scopedOutCount"] == 2
    assert params["start"] == "100000000000"
    assert params["end"] == "130000000000"
    assert params["limit"] == 20
    assert params["direction"] == "backward"


def test_loki_log_lines_are_redacted_recursively():
    svc, _transport = service(
        {
            ("http://loki.monitoring.svc:3100", "/loki/api/v1/query"): {
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"namespace": "otel-demo", "pod": "checkout"},
                            "values": [
                                [
                                    "1700000000000000000",
                                    "Authorization: Bearer lokiBearerSecret123 password = raw-loki-password token: raw-loki-token sk-lokiSecret123",
                                ]
                            ],
                        }
                    ],
                }
            }
        }
    )

    result = run(svc.loki_query(query='{namespace="otel-demo"}', time=1700000000))
    line = result["result"][0]["values"][0][1]

    assert "lokiBearerSecret123" not in line
    assert "raw-loki-password" not in line
    assert "raw-loki-token" not in line
    assert "sk-lokiSecret123" not in line
    assert "Bearer <redacted>" in line
    assert "password = <redacted>" in line
    assert "token: <redacted>" in line
    assert "sk-<redacted>" in line


def test_credential_like_queries_are_rejected_before_network_call():
    svc, transport = service({})

    with pytest.raises(TelemetryROError) as exc:
        run(svc.loki_query(query='{app="api"} |= "password"', time=100))

    assert exc.value.code == "sensitive_query_rejected"
    assert transport.calls == []


def test_mcp_tools_are_all_read_only_and_url_free():
    from mcp_servers.telemetry_ro.server import create_server

    svc, _transport = service({})
    server = create_server(service=svc)
    tools = run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert len(by_name) == 11
    for name, tool in by_name.items():
        assert name.startswith("telemetry_")
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert "url" not in tool.input_schema["properties"]
    assert "telemetry_prom_query_range" in by_name
    assert "telemetry_jaeger_find_traces" in by_name
    assert "telemetry_loki_query_range" in by_name


def test_create_server_accepts_http_auth_injection():
    from mcp.server.auth.settings import AuthSettings

    from mcp_servers.http_runtime import StaticBearerTokenVerifier
    from mcp_servers.telemetry_ro.server import create_server

    svc, _transport = service({})
    auth = AuthSettings(
        issuer_url="https://issuer.example.test",
        resource_server_url="https://mcp.example.test/telemetry",
        required_scopes=["telemetry_ro:read"],
    )
    verifier = StaticBearerTokenVerifier(
        token="t" * 40,
        scopes=["telemetry_ro:read"],
        resource="https://mcp.example.test/telemetry",
    )

    server = create_server(service=svc, auth=auth, token_verifier=verifier)

    assert server is not None
