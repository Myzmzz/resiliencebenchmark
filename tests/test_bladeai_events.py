from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langchain_core")

from stage2_service.bladeai_events import (
    BladeAIStage2EventGraph,
    BladeAIToolCallbackHandler,
    stage2_tool_event,
)


def test_stage2_tool_event_preserves_langgraph_run_id_and_raw_input():
    event = stage2_tool_event(
        {
            "event": "on_tool_start",
            "name": "k8s_ro__k8s_get_resource",
            "run_id": "run-123",
            "metadata": {"langgraph_node": "agent_loop"},
            "data": {
                "input": {
                    "namespace": "otel-demo",
                    "resource": "pods",
                    "name": "cart",
                },
            },
        },
        operation="inject",
    )

    assert event == (
        "runtime_tool_start",
        {
            "call_id": "run-123",
            "tool": "k8s_ro__k8s_get_resource",
            "operation": "inject",
            "node": "agent_loop",
            "input": {
                "namespace": "otel-demo",
                "resource": "pods",
                "name": "cart",
            },
        },
    )


def test_stage2_tool_event_preserves_tool_message_content_as_result():
    event = stage2_tool_event(
        {
            "event": "on_tool_end",
            "name": "telemetry_ro__telemetry_prom_metric_range",
            "run_id": "run-456",
            "tags": ["langsmith:nodes:verifier_loop"],
            "data": {"output": SimpleNamespace(content='{"ok":true,"series":[]}')},
        },
        operation="recover",
    )

    assert event == (
        "runtime_tool_end",
        {
            "call_id": "run-456",
            "tool": "telemetry_ro__telemetry_prom_metric_range",
            "operation": "recover",
            "node": "verifier_loop",
            "result": '{"ok":true,"series":[]}',
        },
    )


def test_stage2_tool_event_closes_tool_error():
    event = stage2_tool_event(
        {
            "event": "on_tool_error",
            "name": "telemetry_ro__telemetry_prom_metric_range",
            "run_id": "run-error",
            "data": {"error": TimeoutError("request timed out")},
        },
        operation="inject",
    )

    assert event == (
        "runtime_tool_error",
        {
            "call_id": "run-error",
            "tool": "telemetry_ro__telemetry_prom_metric_range",
            "operation": "inject",
            "node": "",
            "status": "failed",
            "error": {
                "code": "timeout",
                "type": "TimeoutError",
                "message": "request timed out",
            },
        },
    )


def test_stage2_tool_event_ignores_sdk_events_without_tool_identity():
    assert (
        stage2_tool_event({"event": "on_tool_end", "name": "kubectl"}, operation="inject")
        is None
    )
    assert (
        stage2_tool_event({"event": "on_chain_end", "run_id": "run"}, operation="inject")
        is None
    )


@pytest.mark.asyncio
async def test_stage2_event_graph_emits_tool_events_during_astream():
    emitted = []
    graph = _FakeGraph(
        [
            {
                "event": "on_tool_start",
                "name": "source_ro__source_search",
                "run_id": "run-1",
                "data": {"input": {"query": "cart"}},
            },
            {"event": "on_chain_end", "run_id": "chain-1"},
        ],
        values={"done": True},
    )
    wrapped = BladeAIStage2EventGraph(
        graph,
        emit=lambda kind, payload: emitted.append((kind, payload)),
        operation="inject",
    )

    seen = [event async for event in wrapped.astream_events({}, {}, version="v2")]

    assert seen == graph.events
    assert emitted == [
        (
            "runtime_tool_start",
            {
                "call_id": "run-1",
                "tool": "source_ro__source_search",
                "operation": "inject",
                "node": "",
                "input": {"query": "cart"},
            },
        )
    ]


def test_tool_callback_handler_emits_start_end_and_error_with_same_run_id():
    emitted = []
    handler = BladeAIToolCallbackHandler(
        emit=lambda kind, payload: emitted.append((kind, payload)),
        operation="recover",
    )

    handler.on_tool_start(
        {"name": "chaos_control__chaos_destroy_experiment"},
        "{}",
        run_id="run-2",
        parent_run_id="parent-1",
        tags=["langsmith:nodes:recover"],
        metadata={},
        inputs={"operation_id": "cleanup"},
    )
    handler.on_tool_end(SimpleNamespace(content='{"ok":true}'), run_id="run-2")
    handler.on_tool_start(
        {"name": "telemetry_ro__telemetry_prom_metric_range"},
        "{}",
        run_id="run-3",
    )
    handler.on_tool_error(TimeoutError("downstream timeout"), run_id="run-3")

    assert emitted == [
        (
            "runtime_tool_start",
            {
                "call_id": "run-2",
                "tool": "chaos_control__chaos_destroy_experiment",
                "operation": "recover",
                "node": "recover",
                "parent_run_id": "parent-1",
                "tags": ["langsmith:nodes:recover"],
                "input": {"operation_id": "cleanup"},
            },
        ),
        (
            "runtime_tool_end",
            {
                "call_id": "run-2",
                "tool": "chaos_control__chaos_destroy_experiment",
                "operation": "recover",
                "node": "recover",
                "parent_run_id": "parent-1",
                "tags": ["langsmith:nodes:recover"],
                "result": '{"ok":true}',
            },
        ),
        (
            "runtime_tool_start",
            {
                "call_id": "run-3",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "operation": "recover",
                "node": "",
                "input": {},
            },
        ),
        (
            "runtime_tool_error",
            {
                "call_id": "run-3",
                "tool": "telemetry_ro__telemetry_prom_metric_range",
                "operation": "recover",
                "node": "",
                "status": "failed",
                "error": {
                    "code": "timeout",
                    "type": "TimeoutError",
                    "message": "downstream timeout",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_stage2_event_graph_ainvoke_preserves_graph_output_without_state():
    graph = _FakeInvokeGraph(output={"actual": "invoke-result"})
    wrapped = BladeAIStage2EventGraph(
        graph,
        emit=lambda _kind, _payload: None,
        operation="recover",
    )

    result = await wrapped.ainvoke({"state": "value"}, {"configurable": {}})

    assert result == {"actual": "invoke-result"}
    assert graph.aget_state_called is False


@pytest.mark.asyncio
async def test_stage2_event_graph_ainvoke_keeps_output_that_differs_from_checkpoint():
    graph = _FakeInvokeGraph(
        output={"actual": "invoke-result"},
        checkpoint={"checkpoint": "different"},
    )
    wrapped = BladeAIStage2EventGraph(
        graph,
        emit=lambda _kind, _payload: None,
        operation="recover",
    )

    result = await wrapped.ainvoke({"state": "value"}, None)

    assert result == {"actual": "invoke-result"}
    assert graph.aget_state_called is False


@pytest.mark.asyncio
async def test_stage2_event_graph_ainvoke_propagates_graph_exception():
    graph = _FakeInvokeGraph(error=RuntimeError("recover failed"))
    wrapped = BladeAIStage2EventGraph(
        graph,
        emit=lambda _kind, _payload: None,
        operation="recover",
    )

    with pytest.raises(RuntimeError, match="recover failed"):
        await wrapped.ainvoke({"state": "value"}, {"configurable": {}})

    assert graph.aget_state_called is False


@pytest.mark.asyncio
async def test_stage2_event_graph_ainvoke_appends_callback_without_overwriting():
    existing = object()
    graph = _FakeInvokeGraph(output={"ok": True})
    wrapped = BladeAIStage2EventGraph(
        graph,
        emit=lambda _kind, _payload: None,
        operation="recover",
    )

    await wrapped.ainvoke({"state": "value"}, {"callbacks": [existing]})

    callbacks = graph.config["callbacks"]
    assert callbacks[0] is existing
    assert any(isinstance(item, BladeAIToolCallbackHandler) for item in callbacks)


class _FakeGraph:
    def __init__(self, events, *, values):
        self.events = events
        self.values = values

    async def astream_events(self, *_args, **_kwargs):
        for event in self.events:
            yield event

    async def aget_state(self, _config):
        return SimpleNamespace(values=self.values)


class _FakeInvokeGraph:
    def __init__(self, *, output=None, checkpoint=None, error=None):
        self.output = output
        self.checkpoint = checkpoint or {}
        self.error = error
        self.config = None
        self.aget_state_called = False

    async def ainvoke(self, _input, config=None, **_kwargs):
        self.config = config
        if self.error is not None:
            raise self.error
        return self.output

    async def aget_state(self, _config):
        self.aget_state_called = True
        return SimpleNamespace(values=self.checkpoint)
