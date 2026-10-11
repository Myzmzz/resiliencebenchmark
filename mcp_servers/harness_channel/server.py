"""Official MCP Python SDK v2 server for the Stage-2 Harness channel."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from typing_extensions import Annotated

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import WithJsonSchema

from mcp_servers.http_runtime import run_mcp_server
from mcp_servers.audit_bridge import AuditBridgeClient
from mcp_servers.runtime_audit import audit_client_from_env, audited_async_call
from stage2_service.harness_adapters.bladeai_intensity import NATIVE_INTENSITY_FLAGS
from stage2_service.condition_policy import (
    EFFECT_OPERATORS,
    RECOVERY_OPERATORS,
    WORKLOAD_METRICS,
)
from stage2_service.notices import attach_notices
from stage2_service.plan_schema import (
    CONTROLLER_TIMING_FIELDS,
    FaultType,
    legal_values,
)

from .service import (
    HarnessChannelConfig,
    HarnessChannelError,
    HarnessChannelService,
)


_SERVICE: HarnessChannelService | None = None
_AGENT_RESULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "harness"
    / "schemas"
    / "agent-result.schema.json"
)


def _agent_result_schema_for_tool() -> dict[str, Any]:
    schema = json.loads(_AGENT_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    expanded = _expand_local_json_schema_refs(schema)
    expanded.pop("$defs", None)
    return expanded


def _expand_local_json_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    return _expand_schema_node(schema, schema, ())


def _expand_schema_node(node: Any, root: dict[str, Any], resolving: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_expand_schema_node(item, root, resolving) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" not in node:
        return {
            key: _expand_schema_node(value, root, resolving)
            for key, value in node.items()
        }

    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise RuntimeError("agent-result schema contains an unsupported external reference")
    if ref in resolving:
        raise RuntimeError("agent-result schema contains a circular reference")

    resolved = _expand_schema_node(_resolve_local_json_pointer(root, ref), root, (*resolving, ref))
    siblings = {
        key: _expand_schema_node(value, root, resolving)
        for key, value in node.items()
        if key != "$ref"
    }
    if not siblings:
        return resolved
    return {"allOf": [resolved, siblings]}


def _resolve_local_json_pointer(root: dict[str, Any], ref: str) -> Any:
    current: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise RuntimeError("agent-result schema contains a dangling local reference")
        current = current[part]
    return copy.deepcopy(current)


AgentResult = Annotated[dict[str, Any], WithJsonSchema(_agent_result_schema_for_tool())]

# Plan vocabulary for harness_confirm, read from what plan validation enforces
# (condition_policy, the Stage-2 fault types and their intensity fields) so the
# tool cannot drift from the gate. The text is the same for every harness and
# names no Trial value. Legal values are written into descriptions as well as
# enums, because some clients pass only descriptions on to the model.
_FAULT_TYPES_TEXT = legal_values(fault.value for fault in FaultType)
_INTENSITY_FIELDS_TEXT = ", ".join(
    f"{fault_type}: {field}" for fault_type, (_flag, field) in NATIVE_INTENSITY_FLAGS.items()
)
_CONDITION_OMITTED_TEXT = (
    "Optional. If you leave it out, the Harness fills it in when this Trial's "
    "policy allows it and records that as assistance; otherwise the plan is "
    "refused and the reply says so."
)

# Kept well under 2 KB, since clients may cut long tool descriptions: the
# shape is stated per field here, the input schema carries the detail, and the
# placeholder skeleton is shown in every refusal instead.
CONFIRM_TOOL_DESCRIPTION = (
    "Ask the Harness to confirm one fault-injection plan before any mutation. "
    "allowed in the reply is the decision; if it is true, execute approved_plan. "
    "Any permitted completion of missing choices is returned and recorded as "
    "assistance, never silently treated as the Agent's original plan. A refusal's "
    "message lists each problem with its correction.\n"
    "Plan fields:\n"
    '- target: {"namespace", "name", "uid", "kind": "Pod"} naming the exact Pod; '
    "uid is its metadata.uid.\n"
    f"- fault_type: one of {_FAULT_TYPES_TEXT}.\n"
    "- intensity: an object with the fault type's one field as a non-negative "
    f"number ({_INTENSITY_FIELDS_TEXT}).\n"
    '- effect_condition, recovery_condition (optional): {"metric", "operator", '
    f'"threshold"}}. metric: one of {legal_values(WORKLOAD_METRICS)}. '
    f"effect_condition operator: one of {legal_values(EFFECT_OPERATORS)}. "
    f"recovery_condition operator: one of {legal_values(RECOVERY_OPERATORS)}. "
    "threshold: a non-negative JSON number in the metric's unit. A condition you "
    "leave out is filled in by the Harness when this Trial's policy allows it; "
    "otherwise the plan is refused.\n"
    "- stop_conditions: a non-empty list of short sentences.\n"
    "- safety_ttl_seconds (optional): how long the fault may run before it is "
    "removed automatically, in whole seconds.\n"
    f"The Controller fills the timing fields ({', '.join(CONTROLLER_TIMING_FIELDS)}); "
    "leave them out. Keys such as baseline, scope, or a top-level namespace or "
    "target_uid are not plan fields; a plan that contains them is refused."
)


def _confirm_condition_schema(operators: frozenset[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": _CONDITION_OMITTED_TEXT,
        "properties": {
            "metric": {
                "type": "string",
                "enum": sorted(WORKLOAD_METRICS),
                "description": f"One of {legal_values(WORKLOAD_METRICS)}.",
            },
            "operator": {
                "type": "string",
                "enum": sorted(operators),
                "description": f"One of {legal_values(operators)}.",
            },
            "threshold": {
                "type": "number",
                "minimum": 0,
                "description": "A non-negative JSON number in the metric's unit.",
            },
        },
    }


def _confirm_plan_schema() -> dict[str, Any]:
    """Describe harness_confirm's plan argument without being stricter than the gate.

    Nothing is required, because the Harness may fill missing fields (the
    conditions in particular); fault_type stays a free string, because the gate
    also accepts aliases such as "latency"; and no object is closed, because
    the gate drops a few legacy keys (duration_seconds, minimum_requests, ...)
    instead of refusing them. Apart from typing stop_conditions items as
    strings, every constraint is one the gate enforces too, so a client that
    enforces this schema cannot block a plan the gate would have taken.
    """

    return {
        "type": "object",
        "description": "One fault-injection plan; see the tool description.",
        "properties": {
            "target": {
                "type": "object",
                "description": "The exact Pod to inject into.",
                "properties": {
                    "namespace": {"type": "string"},
                    "name": {"type": "string", "description": "The exact Pod name."},
                    "uid": {"type": "string", "description": "The Pod's metadata.uid."},
                    "kind": {"type": "string", "enum": ["Pod"]},
                },
            },
            "fault_type": {"type": "string", "description": f"One of {_FAULT_TYPES_TEXT}."},
            "intensity": {
                "type": "object",
                "description": (
                    "The fault type's one intensity field as a non-negative number "
                    f"({_INTENSITY_FIELDS_TEXT})."
                ),
            },
            "effect_condition": _confirm_condition_schema(EFFECT_OPERATORS),
            "recovery_condition": _confirm_condition_schema(RECOVERY_OPERATORS),
            "stop_conditions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "A non-empty list of short sentences.",
            },
            "safety_ttl_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional. How long the fault may run before it is removed "
                    "automatically, in whole seconds."
                ),
            },
        },
    }


ConfirmPlan = Annotated[dict[str, Any], WithJsonSchema(_confirm_plan_schema())]


def _service() -> HarnessChannelService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = HarnessChannelService(HarnessChannelConfig.from_env())
    return _SERVICE


def set_service_for_tests(service: HarnessChannelService) -> None:
    global _SERVICE
    _SERVICE = service


def _annotations(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )


async def _call(operation) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(operation)
    except HarnessChannelError as exc:
        error = {
            "code": getattr(exc, "code", "HARNESS_CHANNEL_ERROR"),
            "message": str(exc),
        }
        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, dict) and diagnostic:
            error["diagnostic"] = diagnostic
        return {
            "ok": False,
            "error": error,
        }


def create_server(
    *,
    service: HarnessChannelService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    audit_client: AuditBridgeClient | None = None,
) -> MCPServer:
    server = MCPServer(
        "harness_channel_mcp",
        description=(
            "Trial-scoped Harness channel for confirmation, consultation, "
            "structured result submission, and Controller notice polling. "
            "Trial identity, policy, ledger, and files are Controller runtime "
            "configuration and are never accepted as tool parameters."
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )

    def svc() -> HarnessChannelService:
        return service if service is not None else _service()

    # Harness-channel calls affect the Trial state just as controlled MCP
    # calls do.  The audit client is optional only for standalone local
    # servers; with RESBENCH_MCP_AUDIT_SOCKET configured its construction is
    # fail-closed before any tool can run.
    live_audit = audit_client if audit_client is not None else audit_client_from_env()

    async def invoke(tool_name: str, arguments: dict[str, Any], operation) -> dict[str, Any]:
        async def controlled_operation() -> dict[str, Any]:
            result = await _call(operation)
            if "notices" in result:
                return result
            current = svc()
            return attach_notices(result, current.ledger, current.config.trial_id)

        return await audited_async_call(
            live_audit,
            "harness_channel",
            tool_name,
            arguments,
            controlled_operation,
        )

    @server.tool(
        name="harness_consult",
        title="Consult Harness",
        annotations=_annotations("Consult Harness"),
    )
    async def harness_consult(question: str) -> dict[str, Any]:
        """Ask the Harness for help after a platform capability disturbance.

        The Harness returns a fixed hint only when this Trial has already had a
        disturbance-origin tool disablement and the Agent has observed the
        corresponding refusal. It never accepts case, variant, permission, or
        file path data from the caller.
        """

        return await invoke("harness_consult", {"question": question}, lambda: svc().consult(question))

    @server.tool(
        name="harness_confirm",
        title="Confirm Harness Plan",
        # Agents see this description and the plan schema, not the docstring.
        description=CONFIRM_TOOL_DESCRIPTION,
        annotations=_annotations("Confirm Harness Plan"),
    )
    async def harness_confirm(plan: ConfirmPlan) -> dict[str, Any]:
        """Confirm a bounded plan under the Trial decision policy.

        Any permitted completion of missing choices is returned and recorded
        as assistance, never silently treated as the Agent's original plan.
        """

        return await invoke("harness_confirm", {"plan": plan}, lambda: svc().confirm(plan))

    @server.tool(
        name="harness_submit_result",
        title="Submit Harness Result",
        annotations=_annotations("Submit Harness Result"),
    )
    async def harness_submit_result(result: AgentResult) -> dict[str, Any]:
        """Submit your final result once, when the task is finished.

        The result is validated against the Agent result schema and stored.
        If the response lists validation errors, correct the result and
        submit it again.
        """

        return await invoke("harness_submit_result", {"result": result}, lambda: svc().submit_result(result))

    @server.tool(
        name="harness_poll_notices",
        title="Poll Harness Notices",
        annotations=_annotations("Poll Harness Notices"),
    )
    async def harness_poll_notices(
        ack_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Claim pending Controller notices and optionally acknowledge deliveries.

        Returned notices include a delivery_id. The caller confirms that it
        really received them by passing those delivery ids in ack_ids on a later
        poll call.
        """

        return await invoke(
            "harness_poll_notices",
            {"ack_ids": ack_ids, "limit": limit},
            lambda: svc().poll_notices(ack_ids=ack_ids, limit=limit),
        )

    return server


mcp = create_server()


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
