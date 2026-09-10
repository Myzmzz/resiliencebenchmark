"""Public, bounded interaction records derived solely from canonical events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .harness_adapters.base import AgentMessage, CanonicalEvent, ToolCall, ToolResult


def public_interaction(
    event: CanonicalEvent, request: ToolCall | None = None,
) -> dict[str, Any]:
    """Render a transcript record without exposing private reasoning fields."""
    payload = _public(event.model_dump(mode="json"))
    tool = event.tool if isinstance(event, ToolCall) else request.tool if request else None
    status = event.status if isinstance(event, ToolResult) else "in_progress" if isinstance(event, ToolCall) else "completed"
    native_type = "tool_call" if isinstance(event, ToolCall) else "tool_result" if isinstance(event, ToolResult) else "agent_message" if isinstance(event, AgentMessage) else "checkpoint"
    if isinstance(event, ToolResult) and request:
        payload.update(tool=request.tool, arguments=_public(request.arguments), result=payload.pop("payload"))
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > 20000:
        payload = {"truncated": True, "preview": encoded[:20000], "tool": tool}
    return {
        "actor": "AGENT", "peer": "HARNESS",
        "event_type": "TOOL_INTERACTION" if tool else "AGENT_MESSAGE",
        "native_type": native_type, "tool": tool, "status": status,
        "occurred_at": event.occurred_at.isoformat(), "payload": payload,
    }


def public_tool_evidence(request: ToolCall, result: ToolResult) -> dict[str, Any]:
    """Provide bounded tool arguments and data to the simulated user."""
    data = dict(result.payload)
    if isinstance(data.get("object"), Mapping):
        obj = data["object"]
        data = {"ok": data.get("ok"), "object": {
            key: obj.get(key) for key in ("kind", "metadata", "status")
        }}
    elif isinstance(data.get("items"), list):
        data["items"] = [
            {key: row.get(key) for key in ("kind", "metadata", "status")}
            if isinstance(row, Mapping) and "metadata" in row else row
            for row in data["items"][:30]
        ]
    safe = _public({"tool": request.tool, "arguments": request.arguments, "result": data})
    encoded = json.dumps(safe["result"], ensure_ascii=False)
    if len(encoded) > 8000:
        safe["result"] = {"truncated": True, "text": encoded[:8000]}
    return safe


def _public(value: Any) -> Any:
    """Drop private reasoning containers while preserving external tool data."""
    if isinstance(value, Mapping):
        return {key: _public(item) for key, item in value.items()
                if str(key).lower() not in {"reasoning", "thinking", "chain_of_thought", "analysis"}}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value
