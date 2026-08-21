"""Official MCP Python SDK v2 server for read-only telemetry access."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import run_mcp_server
from .service import TelemetryROError, TelemetryROService, error_envelope


def _read_annotations(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


async def _call(operation) -> dict[str, Any]:
    try:
        return await operation
    except TelemetryROError as exc:
        return error_envelope(exc)


def create_server(
    *,
    service: TelemetryROService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    telemetry = service if service is not None else TelemetryROService()
    server = MCPServer(
        "telemetry_ro_mcp",
        description=(
            "Read-only Prometheus, Jaeger, and Loki telemetry tools for bounded benchmark episode windows. "
            "Endpoint URLs are runtime configuration only and are never accepted as tool parameters. "
            "Prometheus and Loki result queries must preserve a namespace, kubernetes_namespace, "
            "or exported_namespace label so shared-cluster scope filtering can fail closed."
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.tool(
        name="telemetry_prom_query_instant",
        title="Prometheus Instant Query",
        annotations=_read_annotations("Prometheus Instant Query"),
    )
    async def telemetry_prom_query_instant(query: str, time: int, limit: int = 50) -> dict[str, Any]:
        """Run a scoped read-only Prometheus instant query; results without allowed namespace labels are dropped."""

        return await _call(telemetry.prometheus_query_instant(query=query, time=time, limit=limit))

    @server.tool(
        name="telemetry_prom_query_range",
        title="Prometheus Range Query",
        annotations=_read_annotations("Prometheus Range Query"),
    )
    async def telemetry_prom_query_range(query: str, start: int, end: int, step: int, limit: int = 50) -> dict[str, Any]:
        """Run a scoped Prometheus range query; preserve namespace labels in PromQL aggregations."""

        return await _call(telemetry.prometheus_query_range(query=query, start=start, end=end, step=step, limit=limit))

    @server.tool(
        name="telemetry_prom_list_labels",
        title="Prometheus List Labels",
        annotations=_read_annotations("Prometheus List Labels"),
    )
    async def telemetry_prom_list_labels(
        start: int,
        end: int,
        match: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List Prometheus label names for a bounded time window; label names do not reveal cross-namespace series."""

        return await _call(telemetry.prometheus_list_labels(start=start, end=end, match=match, limit=limit, offset=offset))

    @server.tool(
        name="telemetry_prom_list_series",
        title="Prometheus List Series",
        annotations=_read_annotations("Prometheus List Series"),
    )
    async def telemetry_prom_list_series(
        match: list[str],
        start: int,
        end: int,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List only Prometheus series that carry an allowed namespace label."""

        return await _call(telemetry.prometheus_list_series(match=match, start=start, end=end, limit=limit, offset=offset))

    @server.tool(
        name="telemetry_jaeger_list_services",
        title="Jaeger List Services",
        annotations=_read_annotations("Jaeger List Services"),
    )
    async def telemetry_jaeger_list_services(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List only Jaeger services in the configured allowlist."""

        return await _call(telemetry.jaeger_list_services(limit=limit, offset=offset))

    @server.tool(
        name="telemetry_jaeger_list_operations",
        title="Jaeger List Operations",
        annotations=_read_annotations("Jaeger List Operations"),
    )
    async def telemetry_jaeger_list_operations(service: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List Jaeger operations only for an allowlisted service."""

        return await _call(telemetry.jaeger_list_operations(service=service, limit=limit, offset=offset))

    @server.tool(
        name="telemetry_jaeger_find_traces",
        title="Jaeger Find Traces",
        annotations=_read_annotations("Jaeger Find Traces"),
    )
    async def telemetry_jaeger_find_traces(
        service: str,
        start: int,
        end: int,
        operation: str | None = None,
        tags: str | None = None,
        min_duration: str | None = None,
        max_duration: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find Jaeger traces for an allowlisted service and drop traces that expose non-allowlisted services."""

        return await _call(
            telemetry.jaeger_find_traces(
                service=service,
                start=start,
                end=end,
                operation=operation,
                tags=tags,
                min_duration=min_duration,
                max_duration=max_duration,
                limit=limit,
            )
        )

    @server.tool(
        name="telemetry_jaeger_get_trace",
        title="Jaeger Get Trace",
        annotations=_read_annotations("Jaeger Get Trace"),
    )
    async def telemetry_jaeger_get_trace(trace_id: str) -> dict[str, Any]:
        """Fetch one Jaeger trace only if every discovered serviceName is in the allowlist."""

        return await _call(telemetry.jaeger_get_trace(trace_id=trace_id))

    @server.tool(
        name="telemetry_loki_list_labels",
        title="Loki List Labels",
        annotations=_read_annotations("Loki List Labels"),
    )
    async def telemetry_loki_list_labels(start: int, end: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List Loki label names for a bounded benchmark episode window."""

        return await _call(telemetry.loki_list_labels(start=start, end=end, limit=limit, offset=offset))

    @server.tool(
        name="telemetry_loki_query",
        title="Loki Instant Query",
        annotations=_read_annotations("Loki Instant Query"),
    )
    async def telemetry_loki_query(
        query: str,
        time: int,
        limit: int = 50,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Run a scoped Loki instant query; streams without allowed namespace labels are dropped."""

        return await _call(telemetry.loki_query(query=query, time=time, limit=limit, direction=direction))

    @server.tool(
        name="telemetry_loki_query_range",
        title="Loki Range Query",
        annotations=_read_annotations("Loki Range Query"),
    )
    async def telemetry_loki_query_range(
        query: str,
        start: int,
        end: int,
        step: int,
        limit: int = 50,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Run a scoped Loki range query; streams without allowed namespace labels are dropped."""

        return await _call(
            telemetry.loki_query_range(query=query, start=start, end=end, step=step, limit=limit, direction=direction)
        )

    return server


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
