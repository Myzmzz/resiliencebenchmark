"""Map canonical live harness events into disturbance lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from disturbances.types import DisturbancePhase, LifecycleEvent
from stage2_service.harness_adapters import AgentMessage, CanonicalEvent, ToolCall, ToolResult


class HarnessStreamError(RuntimeError):
    pass


EventEmitter = Callable[[LifecycleEvent], list[dict[str, Any]]]


ALLOWED_SERVER_PREFIXES = frozenset(
    {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"}
)


class StreamingLifecycleBridge:
    """Convert tool events while the Agent process is still running.

    The bridge does not infer success from process exit. A main fault is marked
    applied only after a successful ``chaos_create_experiment`` result event.
    """

    def __init__(self, run_id: str, level_id: str, emit: EventEmitter):
        self.run_id = run_id
        self.level_id = level_id
        self.emit = emit
        self.main_fault_applied = False
        self.observation_started = False
        self._calls_by_id: dict[str, ToolCall] = {}

    def start(self) -> list[dict[str, Any]]:
        return self.emit(self._event(DisturbancePhase.EXECUTION, "trial_started"))

    def finish(self, status: str) -> list[dict[str, Any]]:
        return self.emit(
            self._event(
                DisturbancePhase.VERIFICATION,
                "trial_finished",
                payload={"status": status},
            )
        )

    def handle(self, event: CanonicalEvent) -> list[dict[str, Any]]:
        if isinstance(event, AgentMessage):
            return []
        if isinstance(event, ToolCall):
            self._validate_tool_call(event)
            self._calls_by_id[event.call_id] = event
            return self._handle_tool_event(
                kind="tool_call",
                tool=event.tool,
                payload=event.model_dump(mode="json"),
                success=False,
            )
        if isinstance(event, ToolResult):
            call = self._calls_by_id.get(event.call_id)
            if call is None:
                raise HarnessStreamError(
                    f"tool result {event.call_id!r} is missing a matching ToolCall"
                )
            return self._handle_tool_event(
                kind="tool_result",
                tool=call.tool,
                payload=event.model_dump(mode="json"),
                success=_successful_result(event),
            )
        return []

    def _handle_tool_event(
        self,
        *,
        kind: str,
        tool: str,
        payload: Mapping[str, Any],
        success: bool,
    ) -> list[dict[str, Any]]:
        phase = self._phase(tool)
        records: list[dict[str, Any]] = []
        if (
            phase is DisturbancePhase.OBSERVATION
            and kind == "tool_call"
            and not self.observation_started
        ):
            self.observation_started = True
            records.extend(
                self.emit(
                    self._event(
                        DisturbancePhase.OBSERVATION,
                        "observation_started",
                        tool=tool,
                    )
                )
            )
        records.extend(self.emit(self._event(phase, kind, tool=tool, payload=payload)))
        if kind == "tool_result" and tool.endswith("chaos_create_experiment") and success:
            self.main_fault_applied = True
            records.extend(
                self.emit(
                    self._event(
                        DisturbancePhase.EXECUTION,
                        "main_fault_applied",
                        tool=tool,
                    )
                )
            )
            if not self.observation_started:
                self.observation_started = True
                records.extend(
                    self.emit(
                        self._event(
                            DisturbancePhase.OBSERVATION,
                            "observation_started",
                        )
                    )
                )
        return records

    def _phase(self, tool: str) -> DisturbancePhase:
        if tool.startswith("chaos_control."):
            if tool.endswith(("chaos_destroy_experiment", "chaos_recovery_status")):
                return DisturbancePhase.VERIFICATION
            return DisturbancePhase.EXECUTION
        if self.main_fault_applied:
            return DisturbancePhase.OBSERVATION
        return DisturbancePhase.EXECUTION

    def _event(
        self,
        phase: DisturbancePhase,
        kind: str,
        *,
        tool: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> LifecycleEvent:
        return LifecycleEvent(
            run_id=self.run_id,
            level_id=self.level_id,
            phase=phase,
            kind=kind,
            tool=tool,
            payload=dict(payload or {}),
        )

    def _validate_tool_call(self, call: ToolCall) -> None:
        server = call.tool.split(".", 1)[0]
        if server not in ALLOWED_SERVER_PREFIXES:
            raise HarnessStreamError(
                f"non-MCP or unapproved tool event observed: {call.tool}"
            )

def _successful_result(result: ToolResult) -> bool:
    if result.status != "completed":
        return False
    if result.payload.get("ok") is False:
        return False
    if result.payload.get("error"):
        return False
    return True
