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
from stage2_service.notices import attach_notices

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
        annotations=_annotations("Confirm Harness Plan"),
    )
    async def harness_confirm(plan: dict[str, Any]) -> dict[str, Any]:
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
