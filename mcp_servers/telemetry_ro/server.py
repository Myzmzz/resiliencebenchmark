"""Official MCP Python SDK v2 server for read-only telemetry access."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import run_mcp_server
from .service import ALLOW_RAW_QUERIES_ENV, TelemetryROError, TelemetryROService, error_envelope


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
    raw_notice = (
        f" Raw arbitrary PromQL/LogQL tools are registered because {ALLOW_RAW_QUERIES_ENV}=true; "
        "these tools are unqualified for shared-cluster production use."
        if telemetry.config.allow_raw_queries
        else " Raw arbitrary PromQL/LogQL tools are not registered in production strict mode."
    )
    server = MCPServer(
        "telemetry_ro_mcp",
        description=(
            "Read-only Prometheus, Jaeger, and Loki telemetry tools for one bounded benchmark Episode namespace. "
            "Endpoint URLs are runtime configuration only and are never accepted as tool parameters. "
            "Default Prometheus and Loki tools construct scoped queries server-side with namespace exact matchers."
            + raw_notice
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.tool(
        name="telemetry_prom_metric_instant",
        title="Prometheus Scoped Metric Instant Query",
        annotations=_read_annotations("Prometheus Scoped Metric Instant Query"),
    )
    async def telemetry_prom_metric_instant(
        metric: str,
        time: int,
        labels: dict[str, str] | None = None,
        transform: str | None = None,
        window: int = 60,
        group_by: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run a Prometheus instant query built by telemetry_ro for the configured Episode namespace.

        The caller supplies only a metric name, exact non-namespace label filters,
        optional transform (none, rate, increase), and an allowlisted group_by.
        telemetry_ro injects namespace="<episode namespace>" and preserves the
        namespace label when aggregating.
        """

        return await _call(
            telemetry.prometheus_metric_instant(
                metric=metric,
                time=time,
                labels=labels,
                transform=transform,
                window=window,
                group_by=group_by,
                limit=limit,
            )
        )

    @server.tool(
        name="telemetry_prom_metric_range",
        title="Prometheus Scoped Metric Range Query",
        annotations=_read_annotations("Prometheus Scoped Metric Range Query"),
    )
    async def telemetry_prom_metric_range(
        metric: str,
        start: int,
        end: int,
        step: int,
        labels: dict[str, str] | None = None,
        transform: str | None = None,
        window: int = 60,
        group_by: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run a Prometheus range query built by telemetry_ro for the configured Episode namespace."""

        return await _call(
            telemetry.prometheus_metric_range(
                metric=metric,
                start=start,
                end=end,
                step=step,
                labels=labels,
                transform=transform,
                window=window,
                group_by=group_by,
                limit=limit,
            )
        )

    @server.tool(
        name="telemetry_prom_metric_series",
        title="Prometheus Scoped Metric Series",
        annotations=_read_annotations("Prometheus Scoped Metric Series"),
    )
    async def telemetry_prom_metric_series(
        metric: str,
        start: int,
        end: int,
        labels: dict[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List Prometheus series for one metric after telemetry_ro injects the Episode namespace matcher."""

        return await _call(
            telemetry.prometheus_metric_series(
                metric=metric,
                start=start,
                end=end,
                labels=labels,
                limit=limit,
                offset=offset,
            )
        )

    @server.tool(
        name="telemetry_prom_list_labels",
        title="Prometheus List Labels",
        annotations=_read_annotations("Prometheus List Labels"),
    )
    async def telemetry_prom_list_labels(
        start: int,
        end: int,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List Prometheus label names for a bounded time window without caller-supplied match expressions."""

        return await _call(telemetry.prometheus_list_labels(start=start, end=end, match=None, limit=limit, offset=offset))

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
        name="telemetry_loki_list_labels",
        title="Loki List Labels",
        annotations=_read_annotations("Loki List Labels"),
    )
    async def telemetry_loki_list_labels(start: int, end: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List Loki label names for a bounded benchmark episode window."""

        return await _call(telemetry.loki_list_labels(start=start, end=end, limit=limit, offset=offset))

    @server.tool(
        name="telemetry_loki_logs",
        title="Loki Scoped Logs Instant Query",
        annotations=_read_annotations("Loki Scoped Logs Instant Query"),
    )
    async def telemetry_loki_logs(
        time: int,
        labels: dict[str, str] | None = None,
        contains: str | None = None,
        limit: int = 50,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Run a Loki instant log query built by telemetry_ro for the configured Episode namespace.

        The caller may add exact non-namespace label filters and one bounded
        literal contains string. Regex matchers and caller-supplied LogQL pipeline
        fragments are not accepted.
        """

        return await _call(
            telemetry.loki_logs(time=time, labels=labels, contains=contains, limit=limit, direction=direction)
        )

    @server.tool(
        name="telemetry_loki_logs_range",
        title="Loki Scoped Logs Range Query",
        annotations=_read_annotations("Loki Scoped Logs Range Query"),
    )
    async def telemetry_loki_logs_range(
        start: int,
        end: int,
        step: int,
        labels: dict[str, str] | None = None,
        contains: str | None = None,
        limit: int = 50,
        direction: str = "backward",
    ) -> dict[str, Any]:
        """Run a Loki range log query built by telemetry_ro for the configured Episode namespace."""

        return await _call(
            telemetry.loki_logs_range(
                start=start,
                end=end,
                step=step,
                labels=labels,
                contains=contains,
                limit=limit,
                direction=direction,
            )
        )

    if telemetry.config.allow_raw_queries:

        @server.tool(
            name="telemetry_prom_query_instant",
            title="Unqualified Raw Prometheus Instant Query",
            annotations=_read_annotations("Unqualified Raw Prometheus Instant Query"),
        )
        async def telemetry_prom_query_instant(query: str, time: int, limit: int = 50) -> dict[str, Any]:
            """Development-only raw PromQL instant query; unqualified for shared-cluster production use."""

            return await _call(telemetry.prometheus_query_instant(query=query, time=time, limit=limit))

        @server.tool(
            name="telemetry_prom_query_range",
            title="Unqualified Raw Prometheus Range Query",
            annotations=_read_annotations("Unqualified Raw Prometheus Range Query"),
        )
        async def telemetry_prom_query_range(query: str, start: int, end: int, step: int, limit: int = 50) -> dict[str, Any]:
            """Development-only raw PromQL range query; unqualified for shared-cluster production use."""

            return await _call(telemetry.prometheus_query_range(query=query, start=start, end=end, step=step, limit=limit))

        @server.tool(
            name="telemetry_prom_list_series",
            title="Unqualified Raw Prometheus List Series",
            annotations=_read_annotations("Unqualified Raw Prometheus List Series"),
        )
        async def telemetry_prom_list_series(
            match: list[str],
            start: int,
            end: int,
            limit: int = 50,
            offset: int = 0,
        ) -> dict[str, Any]:
            """Development-only raw Prometheus series matcher; unqualified for shared-cluster production use."""

            return await _call(telemetry.prometheus_list_series(match=match, start=start, end=end, limit=limit, offset=offset))

        @server.tool(
            name="telemetry_jaeger_get_trace",
            title="Unqualified Raw Jaeger Get Trace",
            annotations=_read_annotations("Unqualified Raw Jaeger Get Trace"),
        )
        async def telemetry_jaeger_get_trace(trace_id: str) -> dict[str, Any]:
            """Development-only Jaeger trace-id lookup; unqualified for shared-cluster production use."""

            return await _call(telemetry.jaeger_get_trace(trace_id=trace_id))

        @server.tool(
            name="telemetry_loki_query",
            title="Unqualified Raw Loki Instant Query",
            annotations=_read_annotations("Unqualified Raw Loki Instant Query"),
        )
        async def telemetry_loki_query(
            query: str,
            time: int,
            limit: int = 50,
            direction: str = "backward",
        ) -> dict[str, Any]:
            """Development-only raw LogQL instant query; unqualified for shared-cluster production use."""

            return await _call(telemetry.loki_query(query=query, time=time, limit=limit, direction=direction))

        @server.tool(
            name="telemetry_loki_query_range",
            title="Unqualified Raw Loki Range Query",
            annotations=_read_annotations("Unqualified Raw Loki Range Query"),
        )
        async def telemetry_loki_query_range(
            query: str,
            start: int,
            end: int,
            step: int,
            limit: int = 50,
            direction: str = "backward",
        ) -> dict[str, Any]:
            """Development-only raw LogQL range query; unqualified for shared-cluster production use."""

            return await _call(
                telemetry.loki_query_range(query=query, start=start, end=end, step=step, limit=limit, direction=direction)
            )

    return server


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
