from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage2_service.contracts import HarnessKind
from stage2_service.harness_adapters import (
    AgentMessage,
    Checkpoint,
    HarnessAdapterError,
    ToolCall,
    ToolResult,
    create_adapter,
)
from stage2_service.harness_adapters import deepseek
from stage2_service.harness_adapters.deepseek import DeepSeekTraceError


FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams" / "synthetic_canonical_adapters"


def _line(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def test_factory_exports_four_harnesses_without_claiming_qualification():
    expected = {
        HarnessKind.CODEX: "stream",
        HarnessKind.CLAUDE_CODE: "stream",
        HarnessKind.DEEPSEEK: "post_hoc",
        HarnessKind.BLADEAI: "controller_driven",
    }

    for kind, execution_model in expected.items():
        adapter = create_adapter(kind)
        capability = adapter.capability()
        assert capability.kind is kind
        assert capability.execution_model == execution_model
        assert capability.qualification_passed is False
        assert capability.code_execution == "none"
        assert isinstance(capability.feedback_channels, tuple)


def test_codex_item_started_and_completed_share_call_id_without_duplicate_call():
    adapter = create_adapter(HarnessKind.CODEX)

    started = adapter.on_stream_line(
        _line(
            {
                "type": "item.started",
                "session_id": "codex-session-1",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "call-1",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": {"namespace": "otel-demo", "resource": "pods"},
                },
            }
        )
    )
    completed = adapter.on_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "call-1",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "status": "completed",
                    "result": {"structured_content": {"ok": True, "kind": "PodList"}},
                },
            }
        )
    )

    assert adapter.session_id == "codex-session-1"
    assert [type(event) for event in started + completed] == [ToolCall, ToolResult]
    assert started[0].call_id == completed[0].call_id == "call-1"
    assert started[0].tool == "k8s_ro.k8s_get_resource"
    assert completed[0].status == "completed"
    assert adapter.open_calls() == []


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("TOOL_DISABLED", "failed"),
        ("PERMISSION_DENIED", "denied"),
        ("CHANNEL_UNAVAILABLE", "channel_error"),
    ],
)
def test_codex_error_classification_keeps_tool_disabled_out_of_permission_denied(
    error_code: str, expected: str
):
    adapter = create_adapter(HarnessKind.CODEX)
    events = adapter.on_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "id": f"call-{error_code}",
                    "name": "mcp__telemetry_ro__telemetry_prom_metric_range",
                    "status": "failed",
                    "result": {
                        "structured_content": {
                            "ok": False,
                            "error": {"code": error_code, "message": error_code},
                        }
                    },
                },
            }
        )
    )

    result = next(event for event in events if isinstance(event, ToolResult))
    assert result.status == expected


def test_codex_completed_mcp_call_with_arguments_emits_matchable_call_and_result():
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line(
            {
                "type": "mcp_tool_call",
                "id": "call-completed-only",
                "server": "chaos_control",
                "tool": "chaos_validate_plan",
                "arguments": {"run_id": "trial-1"},
                "status": "completed",
                "result": {"structured_content": {"ok": True}},
            }
        )
    )

    assert [type(event) for event in events] == [ToolCall, ToolResult]
    assert events[0].call_id == events[1].call_id == "call-completed-only"
    assert events[0].tool == "chaos_control.chaos_validate_plan"
    assert events[0].arguments == {"run_id": "trial-1"}


def test_codex_resume_turn_namespaces_restarted_native_item_ids():
    adapter = create_adapter(HarnessKind.CODEX)

    adapter.on_stream_line(_line({"type": "turn.started", "session_id": "session-1"}))
    first = adapter.on_stream_line(
        _line(
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "item_0",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": {"name": "cart-a"},
                },
            }
        )
    )
    first_done = adapter.on_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "item_0",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": {"name": "cart-a"},
                    "status": "completed",
                    "result": {"structured_content": {"ok": True}},
                },
            }
        )
    )
    adapter.on_stream_line(_line({"type": "turn.started", "session_id": "session-1"}))
    second = adapter.on_stream_line(
        _line(
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "item_0",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": {"name": "cart-b"},
                },
            }
        )
    )
    second_done = adapter.on_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "item_0",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": {"name": "cart-b"},
                    "status": "completed",
                    "result": {"structured_content": {"ok": True}},
                },
            }
        )
    )

    assert first[0].call_id == first_done[0].call_id == "item_0"
    assert second[0].call_id == second_done[0].call_id == "turn-2:item_0"
    assert adapter.open_calls() == []


def test_codex_thread_started_captures_thread_id_for_resume():
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line({"type": "thread.started", "thread_id": "codex-thread-123"})
    )

    assert events == []
    assert adapter.session_id == "codex-thread-123"


def test_duplicate_completed_call_does_not_reopen_open_calls():
    adapter = create_adapter(HarnessKind.CODEX)
    call = {
        "type": "mcp_tool_call",
        "id": "call-dup",
        "server": "k8s_ro",
        "tool": "k8s_get_resource",
        "arguments": {"name": "cart-a"},
    }

    assert len(adapter.on_stream_line(_line(call))) == 1
    done = {**call, "status": "completed", "result": {"structured_content": {"ok": True}}}
    assert len(adapter.on_stream_line(_line(done))) == 1
    assert adapter.open_calls() == []

    assert adapter.on_stream_line(_line(call)) == []
    assert adapter.open_calls() == []


def test_duplicate_call_id_with_different_arguments_fails_closed():
    adapter = create_adapter(HarnessKind.CODEX)
    adapter.on_stream_line(
        _line(
            {
                "type": "mcp_tool_call",
                "id": "call-conflict",
                "server": "k8s_ro",
                "tool": "k8s_get_resource",
                "arguments": {"name": "cart-a"},
            }
        )
    )

    with pytest.raises(HarnessAdapterError, match="conflicting ToolCall"):
        adapter.on_stream_line(
            _line(
                {
                    "type": "mcp_tool_call",
                    "id": "call-conflict",
                    "server": "k8s_ro",
                    "tool": "k8s_get_resource",
                    "arguments": {"name": "cart-b"},
                }
            )
        )


def test_codex_completed_agent_message_is_not_treated_as_tool_result():
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "id": "message-1",
                    "text": "{\"status\":\"blocked\"}",
                },
            }
        )
    )

    assert [type(event) for event in events] == [AgentMessage]
    assert events[0].structured == {"status": "blocked"}


@pytest.mark.parametrize("error_code", ["invalid_token", "token_expired", "mcp_auth_required"])
def test_codex_runtime_permission_error_codes_remain_denied(error_code: str):
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line(
            {
                "type": "tool_result",
                "call_id": f"call-{error_code}",
                "status": "failed",
                "error": {"code": error_code, "message": "token problem"},
            }
        )
    )

    assert len(events) == 1
    assert events[0].status == "denied"


def test_failed_payload_top_level_auth_message_is_permission_denied():
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line(
            {
                "type": "tool_result",
                "call_id": "call-auth-message",
                "status": "failed",
                "result": {
                    "structured_content": {
                        "ok": False,
                        "message": "Transport error: Auth required",
                    }
                },
            }
        )
    )

    assert events[0].status == "denied"


def test_http_503_service_unavailable_is_channel_error():
    adapter = create_adapter(HarnessKind.CODEX)

    events = adapter.on_stream_line(
        _line(
            {
                "type": "tool_result",
                "call_id": "call-service-unavailable",
                "status": "failed",
                "result": {
                    "structured_content": {
                        "ok": False,
                        "error": {
                            "code": "service_unavailable",
                            "message": "backend unavailable",
                            "http_status": 503,
                        },
                    }
                },
            }
        )
    )

    assert events[0].status == "channel_error"


def test_claude_code_pairs_assistant_tool_use_with_user_tool_result():
    adapter = create_adapter(HarnessKind.CLAUDE_CODE)

    calls = adapter.on_stream_line(
        _line(
            {
                "type": "message",
                "sessionId": "claude-session-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "mcp__chaos_control__chaos_validate_plan",
                            "input": {"run_id": "trial-1"},
                        }
                    ],
                },
            }
        )
    )
    results = adapter.on_stream_line(
        _line(
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": [{"type": "text", "text": "{\"ok\":true}"}],
                            "is_error": False,
                        }
                    ],
                },
            }
        )
    )

    assert adapter.session_id == "claude-session-1"
    assert len(calls) == 1
    assert calls[0].tool == "chaos_control.chaos_validate_plan"
    assert calls[0].arguments == {"run_id": "trial-1"}
    assert results == [
        ToolResult(
            call_id="toolu-1",
            status="completed",
            payload={"ok": True},
            raw_ref="stream:2",
            occurred_at=results[0].occurred_at,
        )
    ]
    assert adapter.open_calls() == []


def test_claude_tool_disabled_is_failed_not_denied():
    adapter = create_adapter(HarnessKind.CLAUDE_CODE)
    adapter.on_stream_line(
        _line(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-disabled",
                            "name": "mcp__telemetry_ro__telemetry_prom_metric_range",
                            "input": {},
                        }
                    ],
                }
            }
        )
    )
    results = adapter.on_stream_line(
        _line(
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-disabled",
                            "content": "{\"ok\":false,\"error\":{\"code\":\"TOOL_DISABLED\"}}",
                            "is_error": True,
                        }
                    ],
                }
            }
        )
    )

    assert len(results) == 1
    assert results[0].status == "failed"


def test_deepseek_post_hoc_replays_multiframe_zstd_session(tmp_path: Path):
    zstd = pytest.importorskip("zstandard")
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    target = artifact_dir / "dsh-session-00.jsonl.zstd"
    compressor = zstd.ZstdCompressor()
    frames = [
        compressor.compress(line.encode() + b"\n")
        for line in (FIXTURES / "deepseek_session.jsonl").read_text().splitlines()
    ]
    target.write_bytes(b"".join(frames))

    adapter = create_adapter(HarnessKind.DEEPSEEK)
    events = adapter.on_turn_end(artifact_dir)

    assert adapter.session_id == "session-synthetic-dsh"
    assert [type(event) for event in events] == [ToolCall, ToolResult, AgentMessage]
    assert events[0].call_id == events[1].call_id == "call-dsh-1"
    assert events[0].tool == "telemetry_ro.telemetry_prom_metric_range"
    assert events[0].occurred_at.year == 2026
    assert events[1].payload == {"ok": True, "series": [1, 2, 3]}
    assert events[1].raw_ref == "dsh-session-00.jsonl.zstd:line:3"
    assert events[2].structured["effect_assessment"] == "verified"
    assert adapter.open_calls() == []


def test_deepseek_stream_line_preserves_headless_stdout_agent_messages():
    adapter = create_adapter(HarnessKind.DEEPSEEK)

    plain = adapter.on_stream_line(b"final answer from stdout\n")
    structured = adapter.on_stream_line(
        _line({"status": "blocked", "effect_assessment": "unverified"})
    )
    ignored_tool = adapter.on_stream_line(
        _line(
            {
                "type": "tool/call",
                "data": {
                    "callId": "call-ignored",
                    "name": "mcp__k8s_ro__k8s_get_resource",
                    "arguments": "{}",
                },
            }
        )
    )

    assert [type(event) for event in plain + structured] == [AgentMessage, AgentMessage]
    assert plain[0].text == "final answer from stdout"
    assert structured[0].structured == {
        "status": "blocked",
        "effect_assessment": "unverified",
    }
    assert ignored_tool == []


def test_deepseek_compressed_session_size_limit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("zstandard")
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "dsh-session-00.jsonl.zstd").write_bytes(b"x" * 11)
    monkeypatch.setattr(deepseek, "MAX_COMPRESSED_SESSION_BYTES", 10)

    with pytest.raises(DeepSeekTraceError, match="compressed session limit"):
        create_adapter(HarnessKind.DEEPSEEK).on_turn_end(artifact_dir)


def test_deepseek_truncated_zstd_session_fails_closed(tmp_path: Path):
    zstd = pytest.importorskip("zstandard")
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    compressed = zstd.ZstdCompressor().compress(
        (FIXTURES / "deepseek_session.jsonl").read_bytes()
    )
    (artifact_dir / "dsh-session-00.jsonl.zstd").write_bytes(compressed[:-8])

    adapter = create_adapter(HarnessKind.DEEPSEEK)

    with pytest.raises(DeepSeekTraceError, match="zstd JSONL stream"):
        adapter.on_turn_end(artifact_dir)


def test_deepseek_result_without_tool_call_remains_unclosed_evidence(tmp_path: Path):
    zstd = pytest.importorskip("zstandard")
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    orphan = {
        "type": "tool/result",
        "time": 1788627002000,
        "data": {
            "message": {
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "missing-call",
                        "content": [{"type": "text", "text": "{\"ok\":true}"}],
                        "isError": False,
                    }
                ]
            }
        },
    }
    (artifact_dir / "dsh-session-00.jsonl.zstd").write_bytes(
        zstd.ZstdCompressor().compress(json.dumps(orphan).encode() + b"\n")
    )

    events = create_adapter(HarnessKind.DEEPSEEK).on_turn_end(artifact_dir)

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)
    assert events[0].call_id == "missing-call"


def test_bladeai_preserves_steps_without_inventing_tool_calls():
    adapter = create_adapter(HarnessKind.BLADEAI)

    calls = adapter.on_stream_line(
        _line(
            {
                "type": "stage2_bladeai_event",
                "kind": "step_start",
                "payload": {"name": "planning", "attrs": {"phase": "C1"}},
            }
        )
    )
    results = adapter.on_stream_line(
        _line(
            {
                "type": "stage2_bladeai_event",
                "kind": "step_end",
                "payload": {"name": "planning", "attrs": {"phase": "C1"}},
            }
        )
    )

    assert len(calls) == 1
    assert isinstance(calls[0], Checkpoint)
    assert calls[0].values["event"] == "step_start"
    assert len(results) == 1
    assert isinstance(results[0], Checkpoint)
    assert results[0].values["event"] == "step_end"
    assert adapter.open_calls() == []
