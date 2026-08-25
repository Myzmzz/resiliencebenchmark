from __future__ import annotations

import os
import sys
import time

import pytest

from disturbances.types import DisturbancePhase
from harness.streaming import HarnessStreamError, StreamingLifecycleBridge
from scripts.run_harness_trial import subprocess_streaming_runner


def test_subprocess_streams_json_line_before_process_exit() -> None:
    observed_at: list[float] = []
    command = [
        sys.executable,
        "-c",
        "import json,time; print(json.dumps({'type':'tool_call'}),flush=True); time.sleep(0.3)",
    ]
    started = time.monotonic()

    result = subprocess_streaming_runner(
        command,
        b"",
        os.environ,
        5,
        lambda _line: observed_at.append(time.monotonic()),
    )
    finished = time.monotonic()

    assert result.returncode == 0
    assert observed_at
    assert observed_at[0] - started < finished - observed_at[0]


def test_stream_observer_failure_terminates_child() -> None:
    command = [
        sys.executable,
        "-c",
        "import json,time; print(json.dumps({'type':'tool_call'}),flush=True); time.sleep(10)",
    ]
    started = time.monotonic()

    with pytest.raises(HarnessStreamError, match="unsafe event"):
        subprocess_streaming_runner(
            command,
            b"",
            os.environ,
            15,
            lambda _line: (_ for _ in ()).throw(HarnessStreamError("unsafe event")),
        )

    assert time.monotonic() - started < 3


def test_bridge_emits_main_fault_only_after_successful_create_result() -> None:
    lifecycle = []

    def emit(event):
        lifecycle.append(event)
        return []

    bridge = StreamingLifecycleBridge("run-1", "L2", emit)
    bridge.start()
    bridge.handle(
        {
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "in_progress",
        }
    )
    assert all(event.kind != "main_fault_applied" for event in lifecycle)

    bridge.handle(
        {
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "result": {"state": "Running"},
        }
    )
    bridge.handle(
        {
            "type": "mcp_tool_call",
            "server": "telemetry_ro",
            "tool": "telemetry_prom_metric_range",
            "status": "in_progress",
        }
    )

    main_fault = next(event for event in lifecycle if event.kind == "main_fault_applied")
    telemetry = lifecycle[-1]
    assert main_fault.phase is DisturbancePhase.EXECUTION
    assert telemetry.phase is DisturbancePhase.OBSERVATION
    assert telemetry.tool == "telemetry_ro.telemetry_prom_metric_range"
    assert any(event.kind == "observation_started" for event in lifecycle)


def test_bridge_rejects_non_mcp_tool_immediately() -> None:
    bridge = StreamingLifecycleBridge("run-1", "L1", lambda _event: [])

    with pytest.raises(HarnessStreamError, match="unapproved"):
        bridge.handle({"type": "tool_call", "tool": "shell_tool"})
