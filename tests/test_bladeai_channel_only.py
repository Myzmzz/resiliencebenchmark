from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from stage2_service.bladeai_worker import (
    CHANNEL_ONLY_MAX_TURNS,
    _run_channel_only,
)
from stage2_service.channel_qualification import ChannelQualificationRunner
from stage2_service.contracts import HarnessKind


def _install_fake_l4_result_types(monkeypatch: pytest.MonkeyPatch) -> None:
    schemas = types.ModuleType("chaos_agent.l4.schemas")

    class L4AgentError:
        def __init__(self, code: str, message: str = "", details: dict | None = None):
            self.code = code
            self.message = message
            self.details = details or {}

    class L4TaskResult:
        def __init__(self, task_id: str, status: str, summary: str = "", error=None, extras=None):
            self.task_id = task_id
            self.status = status
            self.summary = summary
            self.error = error
            self.extras = extras or {}

    schemas.L4AgentError = L4AgentError
    schemas.L4TaskResult = L4TaskResult
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.schemas", schemas)


class _Tool:
    def __init__(self, name: str, output: str = '{"ok":true}'):
        self.name = name
        self.output = output
        self.calls: list[dict] = []

    async def ainvoke(self, arguments: dict) -> str:
        self.calls.append(dict(arguments))
        return self.output


class _Manager:
    def __init__(self, tools):
        self.tools = tools

    def tools_for_phase(self, _phase: str):
        return list(self.tools)


class _BoundLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, _messages):
        return next(self.responses)


def test_only_bladeai_base_qualification_enables_channel_only_worker_path():
    supervisor = SimpleNamespace(base_environment={})
    harness_runner = SimpleNamespace(base_environment={}, mcp_supervisor=None)
    components = SimpleNamespace(supervisor=supervisor, harness_runner=harness_runner)
    runner = ChannelQualificationRunner(None)

    runner._prepare_no_fault_components(
        components, harness=HarnessKind.BLADEAI, profile="base"
    )
    assert supervisor.base_environment["RESBENCH_BLADEAI_CHANNEL_ONLY"] == "true"
    assert harness_runner.base_environment["RESBENCH_BLADEAI_CHANNEL_ONLY"] == "true"

    runner._prepare_no_fault_components(
        components, harness=HarnessKind.CODEX, profile="base"
    )
    assert "RESBENCH_BLADEAI_CHANNEL_ONLY" not in supervisor.base_environment
    assert "RESBENCH_BLADEAI_CHANNEL_ONLY" not in harness_runner.base_environment


def _tool_response(name: str, call_id: str, arguments: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "id": call_id, "args": arguments or {}}],
    )


def test_channel_only_probe_uses_only_connected_mcp_tools_and_stops_on_submit(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_l4_result_types(monkeypatch)
    tools = [
        _Tool("k8s_ro__k8s_get_resource"),
        _Tool("telemetry_ro__telemetry_workload_current"),
        _Tool("harness_channel__harness_confirm"),
        _Tool("harness_channel__harness_consult"),
        _Tool("harness_channel__harness_poll_notices"),
        _Tool("harness_channel__harness_submit_result", '{"ok":true,"valid":true}'),
    ]
    responses = [
        _tool_response(tools[0].name, "call-1"),
        _tool_response(tools[1].name, "call-2"),
        _tool_response(tools[2].name, "call-3"),
        _tool_response(tools[3].name, "call-4"),
        _tool_response(tools[4].name, "call-5"),
        _tool_response(tools[5].name, "call-6", {"result": {"status": "completed"}}),
    ]
    bound = _BoundLLM(responses)
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))

    result = asyncio.run(_run_channel_only(
        SimpleNamespace(_mcp_manager=_Manager(tools)),
        None,
        SimpleNamespace(task_id="trial-1", intent="run channel probe"),
        llm_factory=lambda: bound,
    ))

    assert result.status == "passed"
    assert result.extras["channel_only"] is True
    assert [tool.name for tool in bound.bound_tools] == [tool.name for tool in tools]
    assert [len(tool.calls) for tool in tools] == [1, 1, 1, 1, 1, 1]
    assert any(kind == "conclusion" and payload["status"] == "passed" for kind, payload in emitted)


def test_channel_only_probe_fails_fast_on_mutation_request(monkeypatch: pytest.MonkeyPatch):
    _install_fake_l4_result_types(monkeypatch)
    harmless = _Tool("k8s_ro__k8s_get_resource")
    bound = _BoundLLM([_tool_response("chaos_control__chaos_create_experiment", "bad-call")])
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))

    result = asyncio.run(_run_channel_only(
        SimpleNamespace(_mcp_manager=_Manager([harmless])),
        None,
        SimpleNamespace(task_id="trial-2", intent="run channel probe"),
        llm_factory=lambda: bound,
    ))

    assert result.status == "failed"
    assert result.error.code == "CHANNEL_QUALIFICATION_MUTATION_ATTEMPT"
    assert harmless.calls == []
    assert any(kind == "fatal" for kind, _payload in emitted)


def test_channel_only_probe_has_a_bounded_turn_budget(monkeypatch: pytest.MonkeyPatch):
    _install_fake_l4_result_types(monkeypatch)
    tool = _Tool("k8s_ro__k8s_get_resource")
    bound = _BoundLLM([_tool_response(tool.name, f"call-{idx}") for idx in range(CHANNEL_ONLY_MAX_TURNS)])

    result = asyncio.run(_run_channel_only(
        SimpleNamespace(_mcp_manager=_Manager([tool])),
        None,
        SimpleNamespace(task_id="trial-3", intent="run channel probe"),
        llm_factory=lambda: bound,
    ))

    assert result.status == "failed"
    assert result.error.code == "CHANNEL_QUALIFICATION_TURN_LIMIT"
    assert result.extras["turns"] == CHANNEL_ONLY_MAX_TURNS
