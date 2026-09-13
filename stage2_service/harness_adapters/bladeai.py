"""BladeAI controller-driven event adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stage2_service.contracts import HarnessKind

from .base import (
    AgentMessage,
    BaseHarnessAdapter,
    CanonicalEvent,
    Checkpoint,
    HarnessCapability,
    ToolCall,
    ToolResult,
    decode_json_line,
    extract_agent_message,
    extract_arguments,
    extract_call_id,
    normalize_tool_name,
    resolve_tool_identity,
    parse_occurred_at,
    payload_from_result,
    stable_call_id,
    status_from_payload,
)


_CONTROL_EVENTS = frozenset({
    "agent_progress",
    "approval",
    "cleanup_error",
    "conclusion",
    "fatal",
    "finish",
    "llm_thought",
    "mcp_lifecycle",
    "runtime_tool_execute",
    "sdk_confirmation_proposed",
    "step_end",
    "step_start",
    "task_started",
})
_MESSAGE_EVENTS = frozenset({"conclusion"})
_TOOL_START_EVENTS = frozenset({"tool_start", "runtime_tool_start"})
_TOOL_END_EVENTS = frozenset({"tool_end", "runtime_tool_end", "runtime_tool_error"})


class BladeAIHarnessAdapter(BaseHarnessAdapter):
    kind = HarnessKind.BLADEAI

    def __init__(self) -> None:
        super().__init__()
        # Keep the SDK's terminal envelope separate from the Agent's optional
        # structured assessment.  In particular, a model/provider failure can
        # be emitted with an empty summary; dropping that envelope makes the
        # subsequent qualification failure impossible to diagnose.
        self.terminal_result: dict[str, Any] | None = None

    def capability(self) -> HarnessCapability:
        return HarnessCapability(
            kind=self.kind,
            execution_model="controller_driven",
            streams_tool_results=True,
            post_hoc_trace=False,
            supports_resume=False,
            supports_mid_turn_feedback=False,
            feedback_channels=(),
            code_execution="none",
        )

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]:
        raw_ref = self._raw_ref()
        value = decode_json_line(line)
        if value is None:
            return []
        if isinstance(value, str):
            return [extract_agent_message(value, parse_occurred_at({}))]
        if value.get("type") == "stage2_bladeai_result":
            self.terminal_result = dict(value)
            return [self._result_message(value)]
        if value.get("type") != "stage2_bladeai_event":
            return []
        kind = str(value.get("kind") or "")
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        occurred_at = parse_occurred_at(value)
        if kind in _TOOL_START_EVENTS:
            if extract_call_id(payload) is None:
                return [
                    self._unpaired_tool_checkpoint(
                        kind, payload, occurred_at, reason="missing_call_id"
                    )
                ]
            call = self._tool_call_from_payload(kind, payload, occurred_at)
            recorded = self._record_call(call)
            return [recorded] if recorded is not None else []
        if kind in _TOOL_END_EVENTS:
            result = self._tool_result_from_payload(
                kind, payload, raw_ref, occurred_at
            )
            if result is None:
                return [
                    self._unpaired_tool_checkpoint(
                        kind, payload, occurred_at, reason="missing_call_id"
                    )
                ]
            return [self._record_result(result)]
        if kind in _CONTROL_EVENTS:
            if kind in _MESSAGE_EVENTS:
                return [self._message_event(kind, payload, occurred_at)]
            return [self._control_checkpoint(kind, payload, occurred_at)]
        return []

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        return []

    def _tool_call_from_payload(
        self, kind: str, payload: Mapping[str, Any], occurred_at
    ) -> ToolCall:
        name = self._tool_name(kind, payload)
        call_id = self._call_id(kind, payload, name)
        tool = self._canonical_tool_name(name)
        return ToolCall(
            call_id=call_id,
            tool=tool,
            raw_tool=name,
            tool_resolution=resolve_tool_identity(name).resolution,
            arguments=self._arguments_from_payload(payload),
            occurred_at=occurred_at,
        )

    def _tool_result_from_payload(
        self, kind: str, payload: Mapping[str, Any], raw_ref: str, occurred_at
    ) -> ToolResult | None:
        name = self._tool_name(kind, payload)
        call_id = extract_call_id(payload)
        if call_id is None:
            return None
        result_payload = self._result_payload(kind, payload)
        return ToolResult(
            call_id=call_id,
            status=status_from_payload(
                native_status=self._native_status(payload),
                payload=result_payload,
                is_error=self._is_error_result(payload),
            ),
            payload=result_payload,
            raw_ref=raw_ref,
            occurred_at=occurred_at,
        )

    def _tool_name(self, kind: str, payload: Mapping[str, Any]) -> str:
        name = (
            payload.get("tool")
            or payload.get("tool_name")
            or payload.get("name")
            or kind
        )
        return str(name)

    def _call_id(self, kind: str, payload: Mapping[str, Any], name: str) -> str:
        explicit = extract_call_id(payload)
        if explicit is not None:
            return explicit
        return stable_call_id("bladeai", name, len(self._seen_call_ids))

    def _canonical_tool_name(self, name: str) -> str:
        tool = normalize_tool_name(name)
        if not tool:
            return f"bladeai.{name}"
        if "__" in tool and "." not in tool:
            server, _, tool_name = tool.partition("__")
            tool = normalize_tool_name(tool_name, server) or tool
        if "." not in tool:
            tool = f"bladeai.{tool}"
        return tool

    def _arguments_from_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = extract_arguments(payload)
        if arguments:
            return arguments
        params = payload.get("params")
        if isinstance(params, Mapping):
            return dict(params)
        attrs = payload.get("attrs")
        if isinstance(attrs, Mapping):
            return dict(attrs)
        return {
            key: value
            for key, value in dict(payload).items()
            if key not in {"call_id", "callId", "id", "name", "tool", "tool_name"}
        }

    def _result_payload(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        for key in ("result", "output", "summary", "content"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                parsed = payload_from_result(value)
                return self._normalize_parsed_payload(parsed) if parsed else dict(value)
            if isinstance(value, list):
                parsed = payload_from_result({"content": value})
                return self._normalize_parsed_payload(parsed) if parsed else {"content": value}
            if isinstance(value, str) and value.strip():
                return self._payload_from_text(value)
        return {"bladeai_event": kind, **dict(payload)}

    def _normalize_parsed_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        text = parsed.get("text")
        if isinstance(text, str) and len(parsed) == 1:
            return self._payload_from_text(text)
        return parsed

    def _payload_from_text(self, value: str) -> dict[str, Any]:
        text = value.strip()
        lowered = text.lower()
        if lowered.startswith("[tool timeout]"):
            return {"error": {"code": "timeout", "message": text}}
        if lowered.startswith("[tool error]"):
            return {"error": {"code": "tool_error", "message": text}}
        parsed = payload_from_result({"content": text})
        return parsed if parsed else {"text": text}

    def _is_error_result(self, payload: Mapping[str, Any]) -> bool:
        if payload.get("is_error") is True or payload.get("error") is not None:
            return True
        for key in ("result", "output", "summary", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip().lower().startswith(
                ("[tool error]", "[tool timeout]")
            ):
                return True
        status = str(payload.get("status") or payload.get("level") or "").lower()
        return status in {"failed", "failure", "error"}

    def _native_status(self, payload: Mapping[str, Any]) -> str:
        status = str(payload.get("status") or payload.get("level") or "").lower()
        if status == "ok":
            return "completed"
        return status or "completed"

    def _message_event(
        self, kind: str, payload: Mapping[str, Any], occurred_at
    ) -> AgentMessage:
        text = str(
            payload.get("message")
            or payload.get("content")
            or payload.get("summary")
            or payload.get("error")
            or kind
        )
        return extract_agent_message(text, occurred_at)

    def _control_checkpoint(
        self, kind: str, payload: Mapping[str, Any], occurred_at
    ) -> Checkpoint:
        values: dict[str, Any] = {
            "kind": "bladeai_control",
            "event": kind,
            "payload": dict(payload),
        }
        for key in (
            "sdk_confirmation_id",
            "confirm_call_id",
            "status",
            "integration_status",
        ):
            if key in payload:
                values[key] = payload[key]
        return Checkpoint(
            values=values,
            occurred_at=occurred_at,
        )

    def _unpaired_tool_checkpoint(
        self, kind: str, payload: Mapping[str, Any], occurred_at, *, reason: str
    ) -> Checkpoint:
        return Checkpoint(
            values={
                "kind": "bladeai_tool_event_unpaired",
                "event": kind,
                "tool": self._canonical_tool_name(self._tool_name(kind, payload)),
                "reason": reason,
                "payload": dict(payload),
            },
            occurred_at=occurred_at,
        )

    def _result_message(self, value: Mapping[str, Any]) -> AgentMessage:
        text = value.get("summary")
        if not isinstance(text, str) or not text.strip():
            text = f"BladeAI result: {value.get('status') or 'completed'}"
        message = extract_agent_message(str(text), parse_occurred_at(value))
        if message.structured is not None:
            return message
        return message
