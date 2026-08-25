"""Normalize live Codex/Claude JSONL output into disturbance lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from disturbances.types import DisturbancePhase, LifecycleEvent


class HarnessStreamError(RuntimeError):
    pass


EventEmitter = Callable[[LifecycleEvent], list[dict[str, Any]]]


ALLOWED_SERVER_PREFIXES = frozenset(
    {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control"}
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

    def handle(self, raw_event: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = _trace_kind(raw_event)
        if kind not in {"tool_call", "tool_result"}:
            return []
        tool = _tool_name(raw_event)
        if not tool:
            raise HarnessStreamError("tool event is missing a stable MCP tool name")
        server = tool.split(".", 1)[0]
        if server not in ALLOWED_SERVER_PREFIXES:
            raise HarnessStreamError(f"non-MCP or unapproved tool event observed: {tool}")

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
        records.extend(
            self.emit(self._event(phase, kind, tool=tool, payload=dict(raw_event)))
        )
        if (
            kind == "tool_result"
            and tool.endswith("chaos_create_experiment")
            and _successful(raw_event)
        ):
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


def _trace_kind(event: Mapping[str, Any]) -> str | None:
    marker = str(event.get("type") or event.get("event") or event.get("kind") or "")
    if marker == "mcp_tool_call":
        completed = str(event.get("status") or "").lower() in {"completed", "failed"}
        has_result = any(
            event.get(key) is not None for key in ("result", "error", "output")
        )
        return "tool_result" if completed or has_result else "tool_call"
    if marker in {"tool_call", "tool_use", "function_call"}:
        return "tool_call"
    if marker in {"tool_result", "function_call_output"}:
        return "tool_result"
    return None


def _tool_name(event: Mapping[str, Any]) -> str | None:
    tool = event.get("tool") or event.get("name")
    server = event.get("server") or event.get("server_name")
    if not isinstance(tool, str) or not tool:
        return None
    normalized = tool.removeprefix("mcp__").replace("__", ".")
    if isinstance(server, str) and server and "." not in normalized:
        normalized = f"{server}.{normalized}"
    return normalized


def _successful(event: Mapping[str, Any]) -> bool:
    if event.get("error"):
        return False
    return str(event.get("status") or "completed").lower() not in {"failed", "error"}
