"""WP-B acceptance: the recorded L0 session through the black-box adapter.

Replays ``bladeai_L0.sse`` (real session ``sess_8a686003edf5``) and checks the
CanonicalEvent sequence is structurally the same shape codex produces, and that
LifecycleMapper consumes it without any BladeAI-specific branch.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from harness.bladeai_http.protocol import frame_to_event, iter_sse_frames
from stage2_service.contracts import HarnessKind, LifecyclePhase
from stage2_service.harness_adapters import (
    AgentMessage, Checkpoint, Question, ToolCall, ToolResult, create_adapter,
)
from stage2_service.lifecycle_mapper import (
    LifecycleMapper, capability_for_tool, phase_for_tool, successful,
)

GOLDEN = Path(__file__).parent / "fixtures" / "harness_streams" / "golden"


@pytest.fixture(scope="module")
def l0_events() -> list:
    wire = (GOLDEN / "bladeai_L0.sse").read_bytes()
    lines = [
        json.dumps(frame_to_event(frame), ensure_ascii=False).encode()
        for frame in iter_sse_frames([wire])
    ]
    adapter = create_adapter(HarnessKind.BLADEAI)
    events = [event for line in lines for event in adapter.on_stream_line(line)]
    events += adapter.on_turn_end(Path("/tmp"))
    return events


@pytest.fixture(scope="module")
def l0_adapter() -> object:
    wire = (GOLDEN / "bladeai_L0.sse").read_bytes()
    adapter = create_adapter(HarnessKind.BLADEAI)
    for frame in iter_sse_frames([wire]):
        adapter.on_stream_line(json.dumps(frame_to_event(frame), ensure_ascii=False).encode())
    adapter.on_turn_end(Path("/tmp"))
    return adapter


def test_every_tool_call_is_closed_by_its_own_result(l0_events, l0_adapter) -> None:
    calls = [e for e in l0_events if isinstance(e, ToolCall)]
    results = [e for e in l0_events if isinstance(e, ToolResult)]
    assert len(calls) == 99
    assert len(results) == 99
    assert {c.call_id for c in calls} == {r.call_id for r in results}
    assert l0_adapter.open_calls() == []
    # L0 completed normally, so nothing is left in an unknown fault state.
    assert l0_adapter.unresolved_injection_calls() == []


def test_message_granularity_matches_an_utterance_not_a_character(l0_events) -> None:
    """1,905 raw ``token`` events must collapse to whole utterances."""
    messages = [e for e in l0_events if isinstance(e, AgentMessage)]
    assert len(messages) == 28
    # Every message is a real utterance, not a fragment.
    assert all(m.text.strip() for m in messages)
    assert max(len(m.text) for m in messages) > 200
    # The first utterance reassembles exactly.
    assert messages[0].text.startswith("我先确认当前环境类型")


def test_result_statuses_have_the_same_shape_codex_produces(l0_events) -> None:
    results = [e for e in l0_events if isinstance(e, ToolResult)]
    counts = collections.Counter(r.status for r in results)
    assert counts["completed"] == 97
    # A kubectl Forbidden must classify as denied, exactly as the MCP gateway's
    # structured error does for the other three Harnesses.
    assert counts["denied"] == 1
    assert counts["failed"] == 1
    # Success is explicit, never assumed.
    assert sum(1 for r in results if successful(r)) == 97


def test_injection_call_carries_the_experiment_uid(l0_events) -> None:
    calls = {c.call_id: c for c in l0_events if isinstance(c, ToolCall)}
    create = next(
        r for r in l0_events
        if isinstance(r, ToolResult) and calls[r.call_id].tool.endswith("blade_create")
    )
    assert create.status == "completed"
    assert create.payload["ok"] is True
    # The uid is what WP-D and WP-E need to verify and to recover the fault.
    assert create.payload["result"] == "c85164b57ff93a3a"


def test_native_injection_tools_are_placed_in_the_right_lifecycle_phase(l0_events) -> None:
    tools = {c.tool for c in l0_events if isinstance(c, ToolCall)}
    assert "bladeai.blade_create" in tools
    assert phase_for_tool("bladeai.blade_create") is LifecyclePhase.C3_INJECT
    assert phase_for_tool("bladeai.blade_destroy") is LifecyclePhase.C6_RECOVERY
    assert capability_for_tool("bladeai.blade_create") == "mcp.chaos.create"
    # A read tool is not mistaken for either.
    assert phase_for_tool("bladeai.kubectl_read") is LifecyclePhase.C2_TARGET


def test_all_three_gates_arrive_as_questions(l0_events) -> None:
    questions = [e for e in l0_events if isinstance(e, Question)]
    assert [q.request_kind for q in questions] == [
        "intent", "intent", "intent", "execution"
    ]
    # The id to answer with is the event's own task_id -- there is no
    # ``interrupt_id`` field anywhere in the corpus.
    assert all(q.question_id.startswith("turn-") for q in questions)


def test_terminal_result_is_captured(l0_events, l0_adapter) -> None:
    checkpoints = [e for e in l0_events if isinstance(e, Checkpoint)]
    assert [c.values["kind"] for c in checkpoints] == ["result"]
    assert l0_adapter.terminal_result["status"] == "success"
    data = l0_adapter.terminal_result["data"]
    assert data["experiment_uid"] == "c85164b57ff93a3a"
    assert data["recovery_handle"]["value"] == "c85164b57ff93a3a"


def test_lifecycle_mapper_consumes_the_stream_without_a_bladeai_branch(
    l0_events,
) -> None:
    mapper = LifecycleMapper("campaign-l0", "trial-l0", HarnessKind.BLADEAI, "cleanup-l0")
    lifecycle = [mapped for event in l0_events for mapped in mapper.consume(event)]
    kinds = collections.Counter(event.kind for event in lifecycle)
    # No result arrives without its request: correlation held across 99 pairs.
    assert kinds["tool_result_unmatched"] == 0
    # The one denied read is preserved as a denial, not flattened into a generic failure.
    assert kinds["permission_denied"] == 1
    assert all(event.harness is HarnessKind.BLADEAI for event in lifecycle)
