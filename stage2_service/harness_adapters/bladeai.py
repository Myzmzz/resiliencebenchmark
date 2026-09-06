"""BladeAI controller-driven event adapter."""

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
    normalize_tool_name,
    parse_occurred_at,
    stable_call_id,
    status_from_payload,
)


class BladeAIHarnessAdapter(BaseHarnessAdapter):
    kind = HarnessKind.BLADEAI

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
            text = value.get("summary")
            return [extract_agent_message(str(text), parse_occurred_at(value))] if isinstance(text, str) else []
        if value.get("type") != "stage2_bladeai_event":
            return []
        kind = str(value.get("kind") or "")
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        occurred_at = parse_occurred_at(value)
        if kind in {"step_start", "tool_start"}:
            call = self._call_from_payload(kind, payload, occurred_at)
            recorded = self._record_call(call)
            return [recorded] if recorded is not None else []
        if kind in {"step_end", "tool_end", "approval", "fatal", "finish"}:
            result = self._result_from_payload(kind, payload, raw_ref, occurred_at)
            return [self._record_result(result)]
        return []

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        return []

    def _call_from_payload(
        self, kind: str, payload: Mapping[str, Any], occurred_at
    ) -> ToolCall:
        name = str(payload.get("name") or payload.get("tool") or kind)
        call_id = stable_call_id("bladeai", name, len(self._seen_call_ids))
        tool = normalize_tool_name(name) or f"bladeai.{name}"
        if "." not in tool:
            tool = f"bladeai.{tool}"
        return ToolCall(
            call_id=call_id,
            tool=tool,
            arguments=dict(payload.get("attrs") if isinstance(payload.get("attrs"), Mapping) else payload),
            occurred_at=occurred_at,
        )

    def _result_from_payload(
        self, kind: str, payload: Mapping[str, Any], raw_ref: str, occurred_at
    ) -> ToolResult:
        name = str(payload.get("name") or payload.get("tool") or kind)
        call_id = self._matching_call_id(name)
        result_payload = {"bladeai_event": kind, **dict(payload)}
        native_status = "failed" if kind == "fatal" else "completed"
        return ToolResult(
            call_id=call_id,
            status=status_from_payload(native_status=native_status, payload=result_payload),
            payload=result_payload,
            raw_ref=raw_ref,
            occurred_at=occurred_at,
        )

    def _matching_call_id(self, name: str) -> str:
        suffix = f"bladeai-{name}"
        for call_id, call in reversed(list(self._open_calls.items())):
            if call.tool.endswith(f".{name}") or call_id.startswith(suffix):
                return call_id
        return stable_call_id("bladeai", name, "result", self._line_index)
