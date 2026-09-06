from __future__ import annotations

import json

from stage2_service.harness_adapters.base import (
    AgentMessage,
    Checkpoint,
    ToolCall,
    ToolResult,
)
from stage2_service.harness_adapters.bladeai import BladeAIHarnessAdapter


def _line(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _event(kind: str, payload: dict) -> bytes:
    return _line({"type": "stage2_bladeai_event", "kind": kind, "payload": payload})


def test_bladeai_control_events_remain_checkpoints_not_tool_evidence():
    adapter = BladeAIHarnessAdapter()

    events = []
    for kind in ("step_start", "approval", "finish"):
        events.extend(
            adapter.on_stream_line(
                _event(
                    kind,
                    {
                        "name": "planning",
                        "status": "passed",
                        "sdk_confirmation_id": "sdk-1",
                        "confirm_call_id": "controller-1",
                    },
                )
            )
        )

    assert [type(event) for event in events] == [Checkpoint, Checkpoint, Checkpoint]
    assert all(
        event.values["kind"] == "bladeai_control"
        and event.values["event"] in {"step_start", "approval", "finish"}
        for event in events
    )
    assert events[1].values["sdk_confirmation_id"] == "sdk-1"
    assert events[1].values["confirm_call_id"] == "controller-1"
    assert adapter.open_calls() == []


def test_bladeai_sdk_confirmation_proposal_is_control_checkpoint():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _event(
            "sdk_confirmation_proposed",
            {
                "sdk_confirmation_id": "sdk-1",
                "risk_level": "high",
                "proposal_fields": ["target", "fault_intent"],
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["kind"] == "bladeai_control"
    assert events[0].values["event"] == "sdk_confirmation_proposed"
    assert events[0].values["sdk_confirmation_id"] == "sdk-1"
    assert adapter.open_calls() == []


def test_bladeai_real_tool_events_pair_by_sdk_call_id_and_normalize_mcp_name():
    adapter = BladeAIHarnessAdapter()

    started = adapter.on_stream_line(
        _event(
            "tool_start",
            {
                "call_id": "run-1",
                "tool": "k8s_ro__k8s_get_resource",
                "input": {"namespace": "otel-demo", "resource": "pods", "name": "cart"},
            },
        )
    )
    completed = adapter.on_stream_line(
        _event(
            "tool_end",
            {
                "call_id": "run-1",
                "tool": "k8s_ro__k8s_get_resource",
                "summary": '{"ok":true,"object":{"kind":"Pod"}}',
                "level": "ok",
            },
        )
    )

    assert [type(event) for event in started + completed] == [ToolCall, ToolResult]
    assert started[0].call_id == completed[0].call_id == "run-1"
    assert started[0].tool == "k8s_ro.k8s_get_resource"
    assert started[0].arguments == {"namespace": "otel-demo", "resource": "pods", "name": "cart"}
    assert completed[0].status == "completed"
    assert completed[0].payload == {"ok": True, "object": {"kind": "Pod"}}
    assert adapter.open_calls() == []


def test_bladeai_tool_end_without_call_id_is_unpaired_checkpoint():
    adapter = BladeAIHarnessAdapter()

    first = adapter.on_stream_line(
        _event(
            "tool_start",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro.telemetry_prom_metric_range",
                "args": {"query": "a"},
            },
        )
    )[0]
    second = adapter.on_stream_line(
        _event(
            "tool_start",
            {
                "call_id": "run-2",
                "tool": "telemetry_ro.telemetry_prom_metric_range",
                "args": {"query": "b"},
            },
        )
    )[0]
    completed = adapter.on_stream_line(
        _event(
            "tool_end",
            {"tool": "telemetry_ro.telemetry_prom_metric_range", "content": '{"ok":true}'},
        )
    )[0]

    assert isinstance(completed, Checkpoint)
    assert completed.values["kind"] == "bladeai_tool_event_unpaired"
    assert completed.values["reason"] == "missing_call_id"
    assert [call.call_id for call in adapter.open_calls()] == [
        first.call_id,
        second.call_id,
    ]


def test_bladeai_runtime_tool_execute_is_audit_checkpoint_not_tool_success():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _event(
            "runtime_tool_execute",
            {"tool": "sls_write_logs", "params": {"task_id": "trial-1"}, "kwargs": {}},
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["kind"] == "bladeai_control"
    assert events[0].values["event"] == "runtime_tool_execute"
    assert events[0].values["payload"]["tool"] == "sls_write_logs"
    assert adapter.open_calls() == []


def test_bladeai_tool_start_without_call_id_is_checkpoint_not_synthetic_call():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _event(
            "tool_start",
            {"tool": "k8s_ro__k8s_get_resource", "input": {"resource": "pods"}},
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["kind"] == "bladeai_tool_event_unpaired"
    assert events[0].values["reason"] == "missing_call_id"
    assert adapter.open_calls() == []


def test_bladeai_mcp_adapter_text_errors_are_not_marked_successful():
    adapter = BladeAIHarnessAdapter()
    adapter.on_stream_line(
        _event(
            "tool_start",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
            },
        )
    )

    events = adapter.on_stream_line(
        _event(
            "tool_end",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "summary": (
                    "[tool timeout] telemetry_ro__telemetry_prom_metric_range "
                    "did not respond within 30s"
                ),
                "level": "ok",
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].status == "channel_error"
    assert events[0].payload["error"]["code"] == "timeout"


def test_bladeai_tool_error_closes_call_as_channel_error():
    adapter = BladeAIHarnessAdapter()
    adapter.on_stream_line(
        _event(
            "runtime_tool_start",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "input": {"query": "up"},
            },
        )
    )

    events = adapter.on_stream_line(
        _event(
            "runtime_tool_error",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "status": "failed",
                "error": {
                    "code": "timeout",
                    "type": "TimeoutError",
                    "message": "request timed out",
                },
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].call_id == "run-1"
    assert events[0].status == "channel_error"
    assert events[0].payload["error"]["code"] == "timeout"
    assert adapter.open_calls() == []


def test_bladeai_tool_result_parses_mcp_text_content_blocks():
    adapter = BladeAIHarnessAdapter()
    adapter.on_stream_line(
        _event(
            "runtime_tool_start",
            {
                "call_id": "run-1",
                "tool": "chaos_control__chaos_create_experiment",
                "input": {"target_uid": "pod-uid-1"},
            },
        )
    )

    events = adapter.on_stream_line(
        _event(
            "runtime_tool_end",
            {
                "call_id": "run-1",
                "tool": "chaos_control__chaos_create_experiment",
                "result": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": True,
                                "controller_call_id": "controller-call-1",
                                "controller_notices": ["accepted"],
                                "operation_id": "op-1",
                            }
                        ),
                    }
                ],
                "level": "ok",
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].status == "completed"
    assert events[0].payload == {
        "ok": True,
        "controller_call_id": "controller-call-1",
        "controller_notices": ["accepted"],
        "operation_id": "op-1",
    }


def test_bladeai_tool_result_preserves_mcp_content_block_error_code():
    adapter = BladeAIHarnessAdapter()
    adapter.on_stream_line(
        _event(
            "runtime_tool_start",
            {
                "call_id": "run-1",
                "tool": "chaos_control__chaos_create_experiment",
            },
        )
    )

    events = adapter.on_stream_line(
        _event(
            "runtime_tool_end",
            {
                "call_id": "run-1",
                "tool": "chaos_control__chaos_create_experiment",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": False,
                                "controller_call_id": "controller-call-2",
                                "error": {
                                    "code": "permission_denied",
                                    "message": "chaos create permission revoked",
                                },
                            }
                        ),
                    }
                ],
                "level": "ok",
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].status == "denied"
    assert events[0].payload["controller_call_id"] == "controller-call-2"
    assert events[0].payload["error"]["code"] == "permission_denied"


def test_bladeai_tool_result_text_content_block_error_is_not_success():
    adapter = BladeAIHarnessAdapter()
    adapter.on_stream_line(
        _event(
            "runtime_tool_start",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
            },
        )
    )

    events = adapter.on_stream_line(
        _event(
            "runtime_tool_end",
            {
                "call_id": "run-1",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[tool timeout] telemetry_ro__telemetry_prom_metric_range "
                            "did not respond within 30s"
                        ),
                    }
                ],
                "level": "ok",
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].status == "channel_error"
    assert events[0].payload["error"]["code"] == "timeout"


def test_bladeai_control_content_blocks_do_not_become_tool_success():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _event(
            "agent_progress",
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"ok":true,"controller_call_id":"not-a-tool"}',
                    }
                ],
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["kind"] == "bladeai_control"
    assert adapter.open_calls() == []


def test_bladeai_result_preserves_agent_result_json_as_final_message():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _line(
            {
                "type": "stage2_bladeai_result",
                "status": "passed",
                "summary": '{"final_status":"completed","effect_assessment":"verified"}',
                "task_id": "trial-1",
            }
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], AgentMessage)
    assert events[0].structured == {
        "final_status": "completed",
        "effect_assessment": "verified",
    }


def test_bladeai_plain_result_does_not_wrap_metadata_as_assessment():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _line(
            {
                "type": "stage2_bladeai_result",
                "status": "failed",
                "summary": "unable to verify effect",
                "task_id": "trial-1",
            }
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], AgentMessage)
    assert events[0].text == "unable to verify effect"
    assert events[0].structured is None


def test_bladeai_progress_and_thought_are_checkpoints_not_structured_assessments():
    adapter = BladeAIHarnessAdapter()

    events = [
        *adapter.on_stream_line(_event("agent_progress", {"message": "checking"})),
        *adapter.on_stream_line(_event("llm_thought", {"content": "reasoning"})),
    ]

    assert [type(event) for event in events] == [Checkpoint, Checkpoint]
    assert [event.values["event"] for event in events] == [
        "agent_progress",
        "llm_thought",
    ]


def test_bladeai_fatal_event_is_terminal_message_not_failed_tool_result():
    adapter = BladeAIHarnessAdapter()

    events = adapter.on_stream_line(
        _event(
            "fatal",
            {
                "error": "BladeAI import failed: ImportError",
                "integration_status": "incomplete",
            },
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["event"] == "fatal"
    assert events[0].values["payload"]["integration_status"] == "incomplete"
    assert adapter.open_calls() == []
