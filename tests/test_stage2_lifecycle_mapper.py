"""Lifecycle facts must derive from matched canonical calls and results."""

from datetime import UTC, datetime

import pytest

from stage2_service.contracts import HarnessKind
from stage2_service.harness_adapters.base import ToolCall, ToolResult
from stage2_service.lifecycle_mapper import LifecycleMapper


NOW = datetime(2026, 9, 5, tzinfo=UTC)


def mapper(harness: HarnessKind = HarnessKind.CODEX) -> LifecycleMapper:
    """Create a mapper without a real Agent or any cluster access."""
    return LifecycleMapper("campaign-test", "trial-test", harness, "cleanup-test")


def call(identifier: str, tool: str, **arguments: object) -> ToolCall:
    """Build a canonical tool call with a stable correlation identifier."""
    return ToolCall(call_id=identifier, tool=tool, arguments=arguments, occurred_at=NOW)


def result(identifier: str, **payload: object) -> ToolResult:
    """Build a transport-completed response, which may still reject the request."""
    return ToolResult(call_id=identifier, status="completed", payload=payload, occurred_at=NOW)


@pytest.mark.parametrize("harness", list(HarnessKind))
def test_all_harnesses_have_identical_lifecycle_mapping(harness: HarnessKind) -> None:
    subject = mapper(harness)
    assert subject.consume(call("v", "chaos_control.chaos_validate_plan", namespace="otel-demo", target_name="cart", target_uid="uid")) == []
    events = subject.consume(result("v", ok=True))
    assert [event.kind for event in events] == ["target_bound", "plan_validated"]
    assert events[0].payload["target"]["uid"] == "uid"
    assert all(event.occurred_at == NOW for event in events)


def test_nested_ok_is_not_a_successful_tool_response() -> None:
    subject = mapper()
    subject.consume(call("v", "chaos_control.chaos_validate_plan", namespace="otel-demo", target_name="cart", target_uid="uid"))
    assert not subject.consume(result("v", object={"ok": True}))


def test_effect_trigger_is_first_query_call_after_fault_running() -> None:
    subject = mapper()
    assert subject.consume(call("baseline", "telemetry_ro.telemetry_prom_metric_range")) == []
    subject.consume(result("baseline", ok=True, result=[]))
    requested = subject.consume(call("create", "chaos_control.chaos_create_experiment", target_uid="uid"))
    assert [event.kind for event in requested] == ["injection_intent_committed", "main_fault_requested"]
    subject.consume(result("create", ok=True, created={"phase": "Running"}))
    events = subject.consume(call("effect", "telemetry_ro.telemetry_prom_metric_range"))
    assert [event.kind for event in events] == ["effect_check_started"]
    assert not subject.consume(result("effect", ok=True, result=[]))


def test_disabled_tool_is_not_a_revoked_task_authorization() -> None:
    subject = mapper()
    subject.consume(call("query", "coroot_ro.coroot_metrics_range"))
    events = subject.consume(result("query", ok=False, error={"code": "TOOL_DISABLED"}))
    assert [event.kind for event in events] == ["tool_unavailable"]


def test_result_without_call_cannot_create_an_execution_fact() -> None:
    subject = mapper()
    events = subject.consume(result("unknown", ok=True, created={"phase": "Running"}))
    assert [event.kind for event in events] == ["tool_result_unmatched"]
    assert not subject.fault_running


def test_replay_does_not_duplicate_fault_creation_or_results() -> None:
    subject = mapper()
    request = call("create", "chaos_control.chaos_create_experiment", intensity="100%")
    response = result("create", ok=True, created={"phase": "Running"})
    assert len(subject.consume(request)) == 2
    assert subject.consume(request) == []
    assert [event.kind for event in subject.consume(response)] == ["main_fault_running"]
    assert subject.consume(response) == []
