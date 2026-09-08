"""Bridge raw BladeAI LangGraph events into Stage-2 worker events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import merge_configs


EmitFunc = Callable[[str, dict[str, Any]], None]


class BladeAIToolCallbackHandler(BaseCallbackHandler):
    """Collect LangChain tool callbacks without controlling execution."""

    def __init__(self, *, emit: EmitFunc, operation: str) -> None:
        super().__init__()
        self._emit = emit
        self._operation = operation
        self._tools_by_run: dict[str, dict[str, Any]] = {}

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        tool = _callback_tool_name(serialized, kwargs)
        call_id = str(run_id or "")
        if not tool or not call_id:
            return None
        payload = _base_payload(
            call_id=call_id,
            tool=tool,
            operation=self._operation,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
        )
        payload["input"] = _json_safe(inputs if inputs is not None else input_str)
        self._tools_by_run[call_id] = payload
        self._emit("runtime_tool_start", payload)
        return None

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        payload = self._end_payload(
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            error=None,
        )
        payload["result"] = _tool_output(output)
        self._emit("runtime_tool_end", payload)
        return None

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        payload = self._end_payload(
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            error=error,
        )
        payload["error"] = _error_payload(error)
        self._emit("runtime_tool_error", payload)
        return None

    def _end_payload(
        self,
        *,
        run_id: Any,
        parent_run_id: Any,
        tags: list[str] | None,
        error: BaseException | None,
    ) -> dict[str, Any]:
        call_id = str(run_id or "")
        started = self._tools_by_run.pop(call_id, {})
        tool = str(started.get("tool") or "unknown")
        metadata = started.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else None
        payload = _base_payload(
            call_id=call_id,
            tool=tool,
            operation=self._operation,
            parent_run_id=parent_run_id or started.get("parent_run_id"),
            tags=tags or started.get("tags"),
            metadata=metadata,
        )
        if error is not None:
            payload["status"] = "failed"
        return payload


class BladeAIStage2EventGraph:
    """Proxy a compiled BladeAI graph while preserving raw tool call identity."""

    def __init__(self, graph: Any, *, emit: EmitFunc, operation: str) -> None:
        self._graph = graph
        self._emit = emit
        self._operation = operation

    def set_emit(self, emit: EmitFunc) -> None:
        """Bind event delivery to the current Trial runtime."""
        self._emit = emit

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    async def astream_events(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for raw_event in self._graph.astream_events(*args, **kwargs):
            event = stage2_tool_event(raw_event, operation=self._operation)
            if event is not None:
                kind, payload = event
                self._emit(kind, payload)
            yield raw_event

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        handler = BladeAIToolCallbackHandler(emit=self._emit, operation=self._operation)
        return await self._graph.ainvoke(
            input,
            _merge_callback_config(config, handler),
            **kwargs,
        )


def stage2_tool_event(
    raw_event: Mapping[str, Any], *, operation: str
) -> tuple[str, dict[str, Any]] | None:
    """Return a Stage-2 tool event from one LangGraph ``astream_events`` item."""

    event_name = str(raw_event.get("event") or "")
    if event_name not in {"on_tool_start", "on_tool_end", "on_tool_error"}:
        return None
    tool_name = str(raw_event.get("name") or "")
    call_id = str(raw_event.get("run_id") or "")
    if not tool_name or not call_id:
        return None
    node = _node_name(raw_event)
    payload: dict[str, Any] = {
        "call_id": call_id,
        "tool": tool_name,
        "operation": operation,
        "node": node,
    }
    data = raw_event.get("data")
    data = data if isinstance(data, Mapping) else {}
    if event_name == "on_tool_start":
        payload["input"] = _json_safe(data.get("input", {}))
        return "runtime_tool_start", payload
    if event_name == "on_tool_error":
        payload["status"] = "failed"
        error = data.get("error")
        payload["error"] = (
            _error_payload(error) if isinstance(error, BaseException)
            else {"code": "tool_error", "message": str(error or "tool error")}
        )
        return "runtime_tool_error", payload
    payload["result"] = _tool_output(data.get("output"))
    return "runtime_tool_end", payload


def _merge_callback_config(config: Any, handler: BladeAIToolCallbackHandler) -> Any:
    return merge_configs(config, {"callbacks": [handler]})


def _base_payload(
    *,
    call_id: str,
    tool: str,
    operation: str,
    parent_run_id: Any = None,
    tags: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "call_id": call_id,
        "tool": tool,
        "operation": operation,
        "node": _node_from_parts(tags=tags, metadata=metadata),
    }
    if parent_run_id:
        payload["parent_run_id"] = str(parent_run_id)
    if tags:
        payload["tags"] = _json_safe(tags)
    if metadata:
        payload["metadata"] = _json_safe(dict(metadata))
    return payload


def _callback_tool_name(serialized: Any, kwargs: Mapping[str, Any]) -> str:
    source = serialized if isinstance(serialized, Mapping) else {}
    name = source.get("name") or kwargs.get("name") or kwargs.get("tool_name")
    return str(name or "")


def _tool_output(output: Any) -> Any:
    content = getattr(output, "content", None)
    if content is None and isinstance(output, Mapping):
        content = output.get("content")
    if content is not None:
        return _json_safe(content)
    return _json_safe(output)


def _error_payload(error: BaseException) -> dict[str, str]:
    code = "timeout" if isinstance(error, TimeoutError) else "tool_error"
    return {
        "code": code,
        "type": type(error).__name__,
        "message": str(error),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


def _node_name(raw_event: Mapping[str, Any]) -> str:
    metadata = raw_event.get("metadata")
    if isinstance(metadata, Mapping):
        node = metadata.get("langgraph_node")
        if isinstance(node, str):
            return node
    tags = raw_event.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("langsmith:nodes:"):
                return tag.rsplit(":", 1)[-1]
    return ""


def _node_from_parts(
    *, tags: list[str] | None, metadata: Mapping[str, Any] | None
) -> str:
    raw_event: dict[str, Any] = {}
    if tags is not None:
        raw_event["tags"] = tags
    if metadata is not None:
        raw_event["metadata"] = metadata
    return _node_name(raw_event)
