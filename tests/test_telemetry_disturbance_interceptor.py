import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from disturbances.telemetry_interceptor import (
    TelemetryDisturbanceRuleEngine,
    TelemetryInjectedFailure,
)
from mcp_servers.telemetry_ro.service import (
    HttpResponse,
    RuntimeConfig,
    TelemetryROService,
)


@dataclass
class FakeTransport:
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
        self.calls.append({"base_url": base_url, "path": path, "params": dict(params)})
        return HttpResponse(
            200,
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"namespace": "otel-demo", "pod": "checkout"},
                            "values": [[1, "1"], [2, "2"], [3, "3"], [4, "4"]],
                        }
                    ],
                },
            },
        )


def make_service(engine):
    transport = FakeTransport()
    service = TelemetryROService(
        RuntimeConfig(
            prometheus_url="http://prometheus.monitoring.svc:9090",
            namespace_allowlist=frozenset({"otel-demo"}),
            jaeger_service_allowlist=frozenset({"checkoutservice"}),
            allow_raw_queries=False,
        ),
        transport,
        disturbance_hook=engine,
    )
    return service, transport


def metric_range(service):
    return service.prometheus_metric_range(
        metric="http_requests_total",
        start=1,
        end=4,
        step=1,
    )


def test_failure_rule_interrupts_exact_call_slot_before_upstream_request():
    engine = TelemetryDisturbanceRuleEngine()
    engine.register_rule(
        run_id="run-1",
        level_id="L3",
        disturbance_id="telemetry-failure",
        rule={
            "kind": "telemetry_failure_schedule",
            "tool": "telemetry_prom_metric_range",
            "slots": [2],
            "response_modes": ["http_503"],
            "timeout_milliseconds": 1,
        },
    )
    service, transport = make_service(engine)

    first = asyncio.run(metric_range(service))
    with pytest.raises(TelemetryInjectedFailure) as exc:
        asyncio.run(metric_range(service))

    assert first["ok"] is True
    assert exc.value.http_status == 503
    assert len(transport.calls) == 1
    assert any(item["status"] == "matched" for item in engine.events())


def test_metric_gap_rule_removes_deterministic_points_and_records_count():
    engine = TelemetryDisturbanceRuleEngine()
    engine.register_rule(
        run_id="run-2",
        level_id="L3",
        disturbance_id="metric-gap",
        rule={
            "kind": "metric_data_gap",
            "tool": "telemetry_prom_metric_range",
            "missing_slots": [2, 4],
        },
    )
    service, _transport = make_service(engine)

    result = asyncio.run(metric_range(service))

    assert result["result"][0]["values"] == [[1, "1"], [3, "3"]]
    transformed = [item for item in engine.events() if item["status"] == "response_transformed"]
    assert transformed[-1]["detail"]["removed_points"] == 2


def test_rule_ids_are_stable_and_duplicate_registration_is_rejected():
    engine = TelemetryDisturbanceRuleEngine()
    kwargs = {
        "run_id": "run-3",
        "level_id": "L2",
        "disturbance_id": "gap",
        "rule": {"kind": "metric_data_gap", "missing_slots": [1]},
    }
    rule_id = engine.register_rule(**kwargs)

    with pytest.raises(ValueError, match="already exists"):
        engine.register_rule(**kwargs)

    assert TelemetryDisturbanceRuleEngine().register_rule(**kwargs) == rule_id
