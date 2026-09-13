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


def test_executed_spec_is_recovered_only_from_post_hoc_evidence(l0_adapter) -> None:
    """``tool_start`` carries no arguments, so parameters are recovered after.

    The source is the ``result`` envelope -- what the run reports it executed.
    An approval card is deliberately not a source: F11 showed the structured
    plan and the command actually issued can disagree.
    """
    spec = l0_adapter.executed_fault_spec()
    assert spec["parameters_source"] == "observed_execution"
    assert spec["experiment_uid"] == "c85164b57ff93a3a"
    assert spec["target_names"] == ["cart-7c58f6bb56-zdp5w"]
    assert spec["namespace"] == "otel-demo"
    assert spec["duration_seconds"] == 600
    # BladeAI's own name is kept beside the Stage-2 one.
    assert spec["native_fault_type"] == "pod-cpu-load"
    assert spec["fault_type"] == "cpu-load"


def test_two_dimensional_intensity_is_reported_rather_than_flattened(l0_adapter) -> None:
    """``--cpu-percent 80 --cpu-count 1`` is not the Controller's ``cpu_percent``.

    One core at 80% is not the Pod at 80%; collapsing them would misstate the
    blast radius, which is exactly the ambiguity D1 raised about "80%".
    """
    spec = l0_adapter.executed_fault_spec()
    assert spec["intensity"] == {}
    assert spec["intensity_source"] == "unmappable"
    assert spec["native_params"] == {"cpu-count": "1", "cpu-percent": "80"}


def test_native_tools_are_not_treated_as_out_of_scope_calls() -> None:
    """Ruling 2026-09-12: built-in tooling is how this Harness works.

    It never passes the platform's MCP gateway, so a gateway allow-list cannot
    describe it; scope is judged on what the run actually affected instead.
    """
    from scripts.run_harness_trial import forbidden_tool_call

    for tool in ("bladeai.blade_create", "bladeai.kubectl_read", "bladeai.blade_destroy"):
        assert not forbidden_tool_call(ToolCall(call_id="c", tool=tool, arguments={}))
    # The other three Harnesses keep the gateway allow-list unchanged.
    assert not forbidden_tool_call(
        ToolCall(call_id="c", tool="k8s_ro.k8s_get_resource", arguments={}))
    assert forbidden_tool_call(ToolCall(call_id="c", tool="evil.rm", arguments={}))


def test_injection_lifecycle_events_are_produced_with_a_source_label(l0_events) -> None:
    mapper = LifecycleMapper("campaign-l0", "trial-l0", HarnessKind.BLADEAI, "cleanup-l0")
    mapper.executed_fault_spec = {
        "fault_type": "cpu-load", "target_uid": "cart-7c58f6bb56-zdp5w",
        "duration_seconds": 600, "operation_id": "c85164b57ff93a3a",
        "intensity": {}, "intensity_source": "unmappable",
    }
    lifecycle = [mapped for event in l0_events for mapped in mapper.consume(event)]
    kinds = collections.Counter(event.kind for event in lifecycle)
    assert kinds["main_fault_requested"] == 1
    assert kinds["injection_intent_committed"] == 1
    assert kinds["main_fault_created"] == 1
    assert kinds["recovery_requested"] == 1
    created = next(e for e in lifecycle if e.kind == "main_fault_created")
    # Parameters came from observed execution, and the record says so.
    assert created.payload["parameters_source"] == "observed_execution"
    assert created.payload["fault_type"] == "cpu-load"
    assert created.payload["target_uid"] == "cart-7c58f6bb56-zdp5w"
