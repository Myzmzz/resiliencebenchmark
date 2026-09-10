"""Claude Code message stream adapter."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stage2_service.contracts import HarnessKind

from .base import (
    BaseHarnessAdapter,
    CanonicalEvent,
    HarnessCapability,
    ToolCall,
    ToolResult,
    decode_json_line,
    extract_agent_message,
    extract_arguments,
    normalize_tool_name,
    parse_occurred_at,
    payload_from_result,
    session_id_from_mapping,
    status_from_payload,
)


class ClaudeCodeHarnessAdapter(BaseHarnessAdapter):
    kind = HarnessKind.CLAUDE_CODE

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
        message = value.get("message") if isinstance(value.get("message"), Mapping) else value
        role = str(message.get("role") or "")
        occurred_at = parse_occurred_at(value) if value is not message else parse_occurred_at(message)
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        output: list[CanonicalEvent] = []
        if role == "assistant":
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    output.append(
                        extract_agent_message(str(block["text"]), occurred_at)
                    )
                elif block.get("type") == "tool_use":
                    call = self._tool_call_from_block(block, occurred_at)
                    if call is not None:
                        recorded = self._record_call(call)
                        if recorded is not None:
                            output.append(recorded)
        elif role == "user":
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                result = self._tool_result_from_block(block, raw_ref, occurred_at)
                if result is not None:
                    output.append(self._record_result(result))
        elif str(value.get("type") or "") in {"tool_result", "function_result"}:
            result = self._tool_result_from_block(value, raw_ref, occurred_at)
            if result is not None:
                output.append(self._record_result(result))
        return output

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        return []

    def _tool_call_from_block(
        self, block: Mapping[str, Any], occurred_at
    ) -> ToolCall | None:
        call_id = block.get("id")
        tool = normalize_tool_name(block.get("name"))
        if not isinstance(call_id, str) or not tool:
            return None
        return ToolCall(
            call_id=call_id,
            tool=tool,
            arguments=extract_arguments(block),
            occurred_at=occurred_at,
        )

    def _tool_result_from_block(
        self, block: Mapping[str, Any], raw_ref: str, occurred_at
    ) -> ToolResult | None:
        call_id = block.get("tool_use_id") or block.get("toolUseId") or block.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            return None
        payload = payload_from_result(block)
        return ToolResult(
            call_id=call_id.strip(),
            status=status_from_payload(
                native_status="failed" if block.get("is_error") else "completed",
                payload=payload,
                is_error=bool(block.get("is_error")),
            ),
            payload=payload,
            raw_ref=raw_ref,
            occurred_at=occurred_at,
        )
