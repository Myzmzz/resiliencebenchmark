from __future__ import annotations

import asyncio
from pathlib import Path

from disturbances.file_telemetry_interceptor import (
    FileBackedTelemetryDisturbanceHook,
    FileTelemetryRuleClient,
)


def test_file_channel_applies_metric_gap_and_retains_controller_evidence(
    tmp_path: Path,
) -> None:
    client = FileTelemetryRuleClient(tmp_path / "runtime")
    rule_id = client.register_rule(
        run_id="run-1",
        level_id="L3",
        disturbance_id="gap-1",
        rule={
            "kind": "metric_data_gap",
            "tool": "telemetry_prom_metric_range",
            "missing_slots": [2, 4],
        },
    )
    hook = FileBackedTelemetryDisturbanceHook(tmp_path / "runtime")

    transformed = hook.after_tool(
        "telemetry_prom_metric_range",
        {"result": [{"values": [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]}]},
    )

    assert transformed["result"][0]["values"] == [[1, "a"], [3, "c"]]
    events = client.events()
    assert any(
        event["rule_id"] == rule_id
        and event["status"] == "response_transformed"
        and event["detail"]["removed_points"] == 2
        for event in events
    )

    assert client.remove_rule(rule_id) is True
    unchanged = hook.after_tool(
        "telemetry_prom_metric_range",
        {"result": [{"values": [[1, "a"], [2, "b"]]}]},
    )
    assert unchanged["result"][0]["values"] == [[1, "a"], [2, "b"]]


def test_file_channel_failure_schedule_raises_from_telemetry_process(tmp_path: Path) -> None:
    client = FileTelemetryRuleClient(tmp_path / "runtime")
    client.register_rule(
        run_id="run-1",
        level_id="L2",
        disturbance_id="failure-1",
        rule={
            "kind": "telemetry_failure_schedule",
            "tool": "telemetry_prom_metric_range",
            "slots": [1],
            "response_modes": ["http_503"],
            "timeout_milliseconds": 1,
        },
    )
    hook = FileBackedTelemetryDisturbanceHook(tmp_path / "runtime")

    try:
        asyncio.run(hook.before_tool("telemetry_prom_metric_range"))
    except Exception as exc:  # exact type is exercised by the in-process engine tests.
        assert getattr(exc, "http_status", None) == 503
    else:
        raise AssertionError("injected failure did not reach the telemetry process")
