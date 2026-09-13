"""Codex JSONL stream adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stage2_service.contracts import HarnessKind

from .base import (
    AgentMessage,
    BaseHarnessAdapter,
    CanonicalEvent,
    HarnessCapability,
    ToolCall,
    ToolResult,
    decode_json_line,
    extract_agent_message,
    extract_arguments,
    extract_call_id,
    normalize_tool_name,
    tool_identity_fields,
    parse_occurred_at,
    payload_from_result,
    session_id_from_mapping,
    stable_call_id,
    status_from_payload,
)


class CodexHarnessAdapter(BaseHarnessAdapter):
    kind = HarnessKind.CODEX

    def __init__(self) -> None:
        super().__init__()
        self._turn_index = 0

    def capability(self) -> HarnessCapability:
        return HarnessCapability(
            kind=self.kind,
            execution_model="stream",
            streams_tool_results=True,
            post_hoc_trace=False,
            supports_resume=True,
            supports_mid_turn_feedback=True,
            feedback_channels=("resume",),
            code_execution="none",
        )

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]:
        raw_ref = self._raw_ref()
        value = decode_json_line(line)
        if value is None:
            return []
        if isinstance(value, str):
            return [extract_agent_message(value, parse_occurred_at({}))]
        if (session_id := session_id_from_mapping(value)) is not None:
            self.session_id = session_id
        events: list[CanonicalEvent] = []
        marker = str(value.get("type") or value.get("event") or value.get("kind") or "")
        if marker in {"turn.started", "turn/start", "turn_start"}:
            self._turn_index += 1
            return []
        item = value.get("item") if isinstance(value.get("item"), Mapping) else value
        if marker == "item.started":
            call = self._tool_call_from_item(item)
            if call is not None:
                recorded = self._record_call(call)
                return [recorded] if recorded is not None else []
            message = self._message_from_item(item)
            return [message] if message is not None else []
        if marker == "item.completed":
            call = self._tool_call_from_item(item)
            if call is not None and call.call_id not in self._seen_call_ids:
                recorded = self._record_call(call)
                if recorded is not None:
                    events.append(recorded)
            result = self._tool_result_from_item(item, raw_ref)
            if result is not None:
                events.append(self._record_result(result))
                return events
            message = self._message_from_item(item)
            if message is not None:
                events.append(message)
            return events
        if marker in {"agent_message", "assistant_message", "message"}:
            message = self._message_from_item(value)
            return [message] if message is not None else []
        if marker in {
            "command_execution",
            "file_change",
            "apply_patch",
            "web_search",
            "computer_use",
            "browser_use",
            "subagent_call",
        }:
            recorded = self._record_call(self._forbidden_tool_call(marker, value))
            return [recorded] if recorded is not None else []
        if marker in {"mcp_tool_call", "tool_call", "tool_use", "function_call"}:
            if self._looks_like_result(value):
                events = []
                call = self._tool_call_from_item(value)
                if call is not None and call.call_id not in self._seen_call_ids:
                    recorded = self._record_call(call)
                    if recorded is not None:
                        events.append(recorded)
                result = self._tool_result_from_item(value, raw_ref)
                if result is not None:
                    events.append(self._record_result(result))
                return events
            call = self._tool_call_from_item(value)
            if call is None:
                return []
            recorded = self._record_call(call)
            return [recorded] if recorded is not None else []
        if marker in {"tool_result", "function_result", "function_call_output"}:
            result = self._tool_result_from_item(value, raw_ref)
            return [self._record_result(result)] if result is not None else []
        return []

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        return []

    def _tool_call_from_item(self, item: Mapping[str, Any]) -> ToolCall | None:
        identity = tool_identity_fields(
            item.get("tool") or item.get("name"),
            item.get("server") or item.get("server_name"),
        )
        call_id = self._canonical_call_id(extract_call_id(item))
        if identity is None or not call_id:
            return None
        return ToolCall(
            call_id=call_id,
            **identity,
            arguments=extract_arguments(item),
            occurred_at=parse_occurred_at(item),
        )

    def _forbidden_tool_call(
        self, marker: str, item: Mapping[str, Any]
    ) -> ToolCall:
        call_id = self._canonical_call_id(extract_call_id(item)) or stable_call_id(
            "codex", marker, self._line_index
        )
        identity = tool_identity_fields(item.get("tool") or item.get("name")) or {
            "tool": marker,
            "raw_tool": marker,
            "tool_resolution": "unknown",
        }
        return ToolCall(
            call_id=call_id,
            **identity,
            arguments=dict(item),
            occurred_at=parse_occurred_at(item),
        )

    def _tool_result_from_item(
        self, item: Mapping[str, Any], raw_ref: str
    ) -> ToolResult | None:
        if not self._is_tool_result_item(item):
            return None
        call_id = self._canonical_call_id(extract_call_id(item))
        if not call_id:
            return None
        result_source = item.get("result")
        if isinstance(result_source, Mapping):
            payload = payload_from_result(result_source)
        else:
            payload = payload_from_result(item)
        return ToolResult(
            call_id=call_id,
            status=status_from_payload(
                native_status=item.get("status"),
                payload=payload,
                is_error=str(item.get("status") or "").lower() in {"failed", "error"},
            ),
            payload=payload,
            raw_ref=raw_ref,
            occurred_at=parse_occurred_at(item),
        )

    def _message_from_item(self, item: Mapping[str, Any]) -> AgentMessage | None:
        text = item.get("text") or item.get("content") or item.get("message")
        if not isinstance(text, str) or not text.strip():
            return None
        return extract_agent_message(text, parse_occurred_at(item))

    @staticmethod
    def _looks_like_result(item: Mapping[str, Any]) -> bool:
        status = str(item.get("status") or "").lower()
        return status in {"completed", "success", "succeeded", "accepted", "failed", "error"} or any(
            key in item for key in ("result", "error", "output")
        )

    @staticmethod
    def _is_tool_result_item(item: Mapping[str, Any]) -> bool:
        marker = str(item.get("type") or item.get("event") or item.get("kind") or "")
        if marker in {"tool_result", "function_result", "function_call_output"}:
            return True
        if marker in {"mcp_tool_call", "tool_call", "function_call"}:
            return True
        return normalize_tool_name(
            item.get("tool") or item.get("name"),
            item.get("server") or item.get("server_name"),
        ) is not None

    def _canonical_call_id(self, call_id: str | None) -> str | None:
        if call_id is None:
            return None
        if self._turn_index <= 1:
            return call_id
        return f"turn-{self._turn_index}:{call_id}"
