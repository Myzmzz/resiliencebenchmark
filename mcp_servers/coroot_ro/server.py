"""MCP v2 server exposing the Controller-scoped Coroot read projection."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import PolicyGate, run_mcp_server

from .service import CorootROError, CorootROService, ScopeError, error_envelope


def _annotations(title: str) -> types.ToolAnnotations:
    """Return uniform read-only MCP metadata."""

    return types.ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


async def _call(operation: Any) -> dict[str, Any]:
    """Convert expected service failures to structured MCP content."""

    try:
        return await operation
    except (CorootROError, ScopeError) as exc:
        return error_envelope(exc)


def create_server(
    *,
    service: CorootROService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Create a Coroot-only MCP server without exposing configuration knobs."""

    observer = service if service is not None else CorootROService()
    server = MCPServer(
        "coroot_ro_mcp",
        description=(
            "Read-only Coroot metrics, trace, and log queries for one Controller-scoped benchmark application. "
            "Coroot endpoint, project, application, namespace, service allowlist, and time ceiling are runtime configuration."
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )
    policy_gate = PolicyGate.from_env("coroot_ro")

    @server.tool(name="coroot_metrics_range", title="Coroot Scoped Metric Range", annotations=_annotations("Coroot Scoped Metric Range"))
    @policy_gate.guard("coroot_metrics_range")
    async def coroot_metrics_range(metric: str, start: int, end: int, labels: dict[str, str] | None = None) -> dict[str, Any]:
        """Read a bounded metric range after the server injects the Controller namespace matcher."""

        return await _call(observer.metrics_range(metric=metric, start=start, end=end, labels=labels))

    @server.tool(name="coroot_traces_find", title="Coroot Scoped Trace Search", annotations=_annotations("Coroot Scoped Trace Search"))
    @policy_gate.guard("coroot_traces_find")
    async def coroot_traces_find(service: str, start: int, end: int, min_duration_ms: int = 0) -> dict[str, Any]:
        """Read traces from the Controller application scope for one approved service."""

        return await _call(observer.traces_find(service=service, start=start, end=end, min_duration_ms=min_duration_ms))

    @server.tool(name="coroot_logs_range", title="Coroot Scoped Log Range", annotations=_annotations("Coroot Scoped Log Range"))
    @policy_gate.guard("coroot_logs_range")
    async def coroot_logs_range(service: str, start: int, end: int, pattern: str | None = None) -> dict[str, Any]:
        """Read bounded logs from the Controller application scope and optionally match message text."""

        return await _call(observer.logs_range(service=service, start=start, end=end, pattern=pattern))

    return server


# Do not instantiate at import time: the Controller injects required scope
# configuration only when it launches the per-Trial process.
mcp: MCPServer | None = None


def main() -> None:
    """Run the configured MCP transport."""

    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
