"""BladeAI 0.7.0 black-box SSE adapter.

Replaces the pre-black-box suite, which asserted against the
``stage2_bladeai_event`` / ``stage2_bladeai_result`` envelopes minted by our own
in-process worker.  Those envelopes do not exist once BladeAI is driven as a
service, so every case here is stated against its real published event
vocabulary and is cross-checked in ``test_bladeai_adapter_golden.py`` against
the recorded L0 session.
"""

from __future__ import annotations

import json

from stage2_service.contracts import HarnessKind
from stage2_service.harness_adapters import (
    AgentMessage,
    Checkpoint,
    Question,
    ToolCall,
    ToolResult,
    create_adapter,
)


def line(**value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def adapter():
    return create_adapter(HarnessKind.BLADEAI)


# ---- capability ---------------------------------------------------------


def test_capability_matches_a_streaming_harness() -> None:
    capability = adapter().capability()
    assert capability.kind is HarnessKind.BLADEAI
    assert capability.execution_model == "stream"
    assert capability.streams_tool_results is True
    assert capability.post_hoc_trace is False
    # One session id serves many turns: L0 used five.
    assert capability.supports_resume is True
    assert capability.code_execution == "none"
    assert capability.qualification_passed is False


# ---- tool calls ---------------------------------------------------------


def test_tool_start_becomes_a_tool_call_with_no_invented_arguments() -> None:
    a = adapter()
    events = a.on_stream_line(line(
        type="tool_start", tool_name="kubectl_read", call_id="c-1",
        node="clarification_tools", task_id="turn-1",
        timestamp="2026-09-11T16:26:39.639662+08:00",
    ))
    assert len(events) == 1
    call = events[0]
    assert isinstance(call, ToolCall)
    assert call.call_id == "c-1"
    assert call.tool == "bladeai.kubectl_read"
    # ``tool_start`` publishes no arguments; inventing them would fabricate
    # evidence about what was actually run.
    assert call.arguments == {}
    assert a.open_calls() == [call]


def test_tool_end_closes_the_call_by_call_id() -> None:
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="kubectl_read", call_id="c-1"))
    events = a.on_stream_line(line(
        type="tool_end", call_id="c-1", tool_name="kubectl_read",
        content='{"ok": true, "pods": 1}',
    ))
    assert len(events) == 1
    result = events[0]
    assert isinstance(result, ToolResult)
    assert result.call_id == "c-1"
    assert result.status == "completed"
    assert result.payload.get("pods") == 1
    assert a.open_calls() == []


def test_tool_end_with_plain_text_content_keeps_the_text() -> None:
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="blade_help", call_id="c-9"))
    result = a.on_stream_line(line(type="tool_end", call_id="c-9", content="usage: blade ..."))[0]
    assert result.payload == {"ok": True, "text": "usage: blade ..."}


def test_forbidden_error_content_is_not_reported_as_success() -> None:
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="kubectl", call_id="c-2"))
    result = a.on_stream_line(line(
        type="tool_end", call_id="c-2",
        content='{"ok": false, "error": {"code": "forbidden", '
                '"message": "pods/exec is forbidden"}}',
    ))[0]
    assert result.status != "completed"


# ---- orphan injection calls (D6-B) --------------------------------------


def test_unclosed_injection_call_is_reported_as_state_unknown() -> None:
    """D6-B: the SSE stream was cut right after ``blade_create``'s start.

    No ``tool_end`` ever arrived, yet the independent observer measured the
    target going from 3m to ~783m CPU -- the fault was real.  An orphan like
    this must surface as "state unknown", never as "nothing was injected".
    """
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="blade_create", call_id="inject-1"))
    assert [c.call_id for c in a.unresolved_injection_calls()] == ["inject-1"]
    assert [c.call_id for c in a.open_calls()] == ["inject-1"]


def test_closed_injection_call_is_no_longer_unresolved() -> None:
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="blade_create", call_id="inject-1"))
    a.on_stream_line(line(type="tool_end", call_id="inject-1", content='{"ok": true}'))
    assert a.unresolved_injection_calls() == []


def test_read_only_tools_are_not_tracked_as_injection_risk() -> None:
    a = adapter()
    a.on_stream_line(line(type="tool_start", tool_name="kubectl_read", call_id="r-1"))
    assert a.unresolved_injection_calls() == []


# ---- token coalescing ---------------------------------------------------


def test_consecutive_tokens_coalesce_into_one_message() -> None:
    """1,905 ``token`` events in L0 must not become 1,905 AgentMessages."""
    a = adapter()
    for part in ("我先确认", "当前环境", "类型。"):
        assert a.on_stream_line(line(
            type="token", content=part, node="intent_clarification", task_id="turn-1"
        )) == []
    events = a.on_stream_line(line(type="usage", input_tokens=10))
    assert len(events) == 1
    message = events[0]
    assert isinstance(message, AgentMessage)
    assert message.text == "我先确认当前环境类型。"
    assert message.structured["node"] == "intent_clarification"


def test_thinking_does_not_emit_and_does_not_split_an_utterance() -> None:
    """Private reasoning is not an utterance, and must not chop one in half."""
    a = adapter()
    a.on_stream_line(line(type="token", content="前半", node="agent_loop"))
    assert a.on_stream_line(line(type="thinking", content="The user wants")) == []
    a.on_stream_line(line(type="token", content="后半", node="agent_loop"))
    message = a.on_stream_line(line(type="usage"))[0]
    assert message.text == "前半后半"


def test_token_run_is_flushed_before_the_event_that_ends_it() -> None:
    a = adapter()
    a.on_stream_line(line(type="token", content="说完了"))
    events = a.on_stream_line(line(type="tool_start", tool_name="kubectl_read", call_id="c-3"))
    assert [type(e).__name__ for e in events] == ["AgentMessage", "ToolCall"]


def test_trailing_tokens_are_flushed_at_turn_end(tmp_path) -> None:
    a = adapter()
    a.on_stream_line(line(type="token", content="末尾未收束"))
    events = a.on_turn_end(tmp_path)
    assert len(events) == 1 and events[0].text == "末尾未收束"
    assert a.on_turn_end(tmp_path) == []


def test_whitespace_only_token_run_emits_nothing() -> None:
    a = adapter()
    a.on_stream_line(line(type="token", content="   \n"))
    assert a.on_stream_line(line(type="usage")) == []


def test_node_message_is_a_complete_message_not_a_token_run() -> None:
    a = adapter()
    events = a.on_stream_line(line(
        type="node_message", content="Pre-task probes done (1 ok, 1 warning).",
        node="preplan_probe",
    ))
    assert len(events) == 1
    assert isinstance(events[0], AgentMessage)
    assert events[0].structured["node"] == "preplan_probe"


# ---- the three confirmation gates ---------------------------------------


def test_intent_gate_becomes_a_question_keyed_by_task_id() -> None:
    """There is no ``interrupt_id`` field: the id to answer with is task_id."""
    a = adapter()
    events = a.on_stream_line(line(
        type="confirm", node="intent_confirm", task_id="turn-1b4f6902e392",
        content="Fault type: pod-cpu-load\nDuration: 600s",
        payload={"type": "intent_confirm", "fault_intent": {"fault_type": "pod-cpu-load"}},
    ))
    assert len(events) == 1
    question = events[0]
    assert isinstance(question, Question)
    assert question.question_id == "turn-1b4f6902e392"
    assert question.request_kind == "intent"
    assert question.version == 1
    assert question.recommendation["gate_node"] == "intent_confirm"
    assert "Duration: 600s" in question.recommendation["card_text"]


def test_execution_gate_is_a_distinct_request_kind() -> None:
    a = adapter()
    question = a.on_stream_line(line(
        type="confirm", node="confirmation_gate", task_id="turn-e708412252a4",
        payload={"params": {"cpu-percent": "80"}, "duration_seconds": 600},
    ))[0]
    assert question.request_kind == "execution"
    # The execution gate's payload carries no ``type`` field, so dispatch must
    # key on ``node`` rather than on ``payload.type``.
    assert "type" not in question.recommendation
    assert question.recommendation["duration_seconds"] == 600


def test_target_change_gate_is_recognised() -> None:
    """The third gate the original handoff never mentioned (D8-B)."""
    a = adapter()
    question = a.on_stream_line(line(
        type="confirm", node="tool_screener", task_id="turn-17d0472480aa",
        content="Target change detected: scope drift: approved=pod effective=chaosblade",
        payload={"type": "target_change",
                 "original": {"scope": "pod", "names": ["cart-x"]},
                 "proposed": {"scope": "chaosblade", "names": ["b1f4bbf51e82d051"]}},
    ))[0]
    assert question.request_kind == "target_change"
    assert question.recommendation["original"]["scope"] == "pod"
    assert question.recommendation["proposed"]["scope"] == "chaosblade"


def test_unknown_gate_node_stays_identifiable() -> None:
    a = adapter()
    question = a.on_stream_line(line(
        type="confirm", node="some_future_gate", task_id="turn-9"
    ))[0]
    assert question.request_kind == "unknown:some_future_gate"


def test_re_emitted_gate_increments_version() -> None:
    """The corpus shows the same gate arriving more than once."""
    a = adapter()
    first = a.on_stream_line(line(type="confirm", node="intent_confirm", task_id="turn-1"))[0]
    second = a.on_stream_line(line(type="confirm", node="intent_confirm", task_id="turn-1"))[0]
    assert (first.version, second.version) == (1, 2)
    assert first.question_id == second.question_id


# ---- result / error / unknown -------------------------------------------


def test_result_becomes_a_checkpoint_and_is_kept_as_the_terminal_envelope() -> None:
    a = adapter()
    payload = {"status": "success", "data": {
        "experiment_uid": "c85164b57ff93a3a", "task_state": "injected",
        "recovery_handle": {"kind": "experiment_uid", "value": "c85164b57ff93a3a"}}}
    events = a.on_stream_line(line(
        type="result", content=json.dumps(payload), task_id="turn-1"
    ))
    assert len(events) == 1
    checkpoint = events[0]
    assert isinstance(checkpoint, Checkpoint)
    assert checkpoint.values["kind"] == "result"
    assert checkpoint.values["result"]["data"]["experiment_uid"] == "c85164b57ff93a3a"
    assert a.terminal_result == payload


def test_error_is_a_checkpoint_not_an_agent_message() -> None:
    """Every ``error`` in the corpus was the platform's own ``/cancel``.

    AgentMessage counts as evidence the Agent was active, so routing an error
    there would turn our own intervention into the Agent's activity.
    """
    a = adapter()
    events = a.on_stream_line(line(type="error", content="Turn cancelled", task_id="turn-1"))
    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values == {"kind": "error", "message": "Turn cancelled", "task_id": "turn-1"}
    assert not any(isinstance(e, AgentMessage) for e in events)


def test_structural_events_emit_nothing() -> None:
    a = adapter()
    for kind in ("node_start", "node_end", "llm_start", "context_size", "usage", "done"):
        assert a.on_stream_line(line(type=kind, node="agent_loop")) == [], kind


def test_unknown_event_kind_is_recorded_rather_than_dropped() -> None:
    """Upstream adding an event kind must not silently lose evidence."""
    a = adapter()
    events = a.on_stream_line(line(type="some_new_event_kind", detail="x"))
    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert events[0].values["kind"] == "unknown_event"
    assert events[0].values["event"]["detail"] == "x"


def test_blank_and_non_json_lines_are_handled() -> None:
    a = adapter()
    assert a.on_stream_line(b"") == []
    assert a.on_stream_line(b"   \n") == []
    plain = a.on_stream_line(b"not json at all")
    assert len(plain) == 1 and isinstance(plain[0], AgentMessage)
