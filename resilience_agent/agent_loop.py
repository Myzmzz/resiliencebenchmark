"""Auditable, bounded model/tool loop for internal analysis stages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from .analysis_tools import ProjectAnalysisTools, ToolInputError
from .model_client import ModelTransportError, ReasoningModel


class AgentLoopError(RuntimeError):
    """Raised when a model stage cannot finish safely within its contract."""

    def __init__(self, message: str, *, trace: dict[str, Any] | None = None):
        super().__init__(message)
        self.trace = trace


@dataclass
class AgentLoopResult:
    output: dict[str, Any]
    trace: dict[str, Any]


def _validate_output(value: Any, schema: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if not errors:
        return
    messages = []
    for error in errors[:10]:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"{location}: {error.message}")
    raise AgentLoopError("model structured output failed schema validation: " + "; ".join(messages))


def run_agent_loop(
    *,
    stage: str,
    model: ReasoningModel,
    tools: ProjectAnalysisTools,
    instructions: str,
    prompt: str,
    output_schema: dict[str, Any],
    schema_name: str,
) -> AgentLoopResult:
    input_items: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }
    ]
    trace: dict[str, Any] = {
        "stage": stage,
        "provider": model.config.provider,
        "protocol": model.config.protocol,
        "requested_model": model.config.model,
        "resolved_model": None,
        "rounds": [],
        "tool_call_count": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "status": "running",
        "transport_retries": [],
    }
    for round_number in range(1, model.config.max_tool_rounds + 1):
        turn = None
        for attempt in range(model.config.max_transport_retries + 1):
            try:
                turn = model.create_turn(
                    instructions=instructions,
                    input_items=input_items,
                    tools=tools.definitions,
                    output_schema=output_schema,
                    schema_name=schema_name,
                )
                break
            except ModelTransportError as exc:
                trace["transport_retries"].append(
                    {
                        "round": round_number,
                        "attempt": attempt + 1,
                        "error": str(exc)[:500],
                    }
                )
                if attempt >= model.config.max_transport_retries:
                    trace["status"] = "transport_failed"
                    raise AgentLoopError(str(exc), trace=trace) from exc
        if turn is None:
            raise AgentLoopError("model transport retry loop produced no turn", trace=trace)
        trace["resolved_model"] = turn.resolved_model or trace["resolved_model"]
        for key in trace["usage"]:
            trace["usage"][key] += turn.usage.get(key, 0)
        round_trace: dict[str, Any] = {
            "round": round_number,
            "response_id": turn.response_id,
            "response_status": turn.status,
            "tool_calls": [],
            "has_final_output": bool(turn.final_text),
        }
        if turn.tool_calls:
            input_items.extend(turn.output_items)
            for call in turn.tool_calls:
                try:
                    result = tools.execute(call.name, call.arguments)
                except ToolInputError as exc:
                    result = {"ok": False, "error": str(exc)}
                serialized = json.dumps(result, ensure_ascii=False)
                if len(serialized) > model.config.max_tool_result_chars:
                    serialized = serialized[: model.config.max_tool_result_chars]
                    result_for_model = {
                        "ok": False,
                        "error": "tool result exceeded the configured character limit",
                        "truncated_preview": serialized,
                    }
                    serialized = json.dumps(result_for_model, ensure_ascii=False)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": serialized,
                    }
                )
                round_trace["tool_calls"].append(
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": bool(result.get("ok")),
                        "result_preview": serialized[:1000],
                    }
                )
                trace["tool_call_count"] += 1
            trace["rounds"].append(round_trace)
            continue
        if not turn.final_text:
            trace["rounds"].append(round_trace)
            trace["status"] = "failed"
            raise AgentLoopError(
                "model returned neither tool calls nor final structured output",
                trace=trace,
            )
        try:
            final = json.loads(turn.final_text)
        except json.JSONDecodeError as exc:
            trace["rounds"].append(round_trace)
            trace["status"] = "failed"
            raise AgentLoopError("model final output is not valid JSON", trace=trace) from exc
        try:
            _validate_output(final, output_schema)
        except AgentLoopError as exc:
            exc.trace = trace
            raise
        trace["rounds"].append(round_trace)
        trace["status"] = "completed"
        return AgentLoopResult(output=final, trace=trace)
    trace["status"] = "max_rounds_exceeded"
    raise AgentLoopError(
        f"model stage '{stage}' exceeded {model.config.max_tool_rounds} tool rounds",
        trace=trace,
    )
