#!/usr/bin/env python3
"""Qualify authenticated loopback MCP endpoints without exposing runtime values."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


TOKEN_ENV = "RESBENCH_MCP_TOKEN"
MIN_TOKEN_CHARS = 32
DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    url_env: str
    tool_prefix: str
    minimum_tools: int
    expected_tools: tuple[str, ...]


ENDPOINTS = (
    EndpointSpec(
        "k8s_ro",
        "RESBENCH_K8S_MCP_URL",
        "k8s_",
        5,
        ("k8s_get_resource", "k8s_list_resources", "k8s_list_events", "k8s_pod_logs", "k8s_cluster_inventory"),
    ),
    EndpointSpec(
        "telemetry_ro",
        "RESBENCH_TELEMETRY_MCP_URL",
        "telemetry_",
        10,
        (
            "telemetry_prom_metric_instant",
            "telemetry_prom_metric_range",
            "telemetry_prom_metric_series",
            "telemetry_prom_list_labels",
            "telemetry_jaeger_list_services",
            "telemetry_jaeger_list_operations",
            "telemetry_jaeger_find_traces",
            "telemetry_loki_list_labels",
            "telemetry_loki_logs",
            "telemetry_loki_logs_range",
        ),
    ),
    EndpointSpec(
        "source_ro",
        "RESBENCH_SOURCE_MCP_URL",
        "source_",
        5,
        (
            "source_list_repositories",
            "source_list_files",
            "source_search_text",
            "source_read_file",
            "source_show_commit",
        ),
    ),
    EndpointSpec(
        "chaos_control",
        "RESBENCH_CHAOS_CONTROL_MCP_URL",
        "chaos_",
        6,
        (
            "chaos_validate_plan",
            "chaos_inventory_run",
            "chaos_create_experiment",
            "chaos_get_experiment",
            "chaos_destroy_experiment",
            "chaos_recovery_status",
        ),
    ),
)


class QualificationError(RuntimeError):
    """Expected endpoint qualification failure with no sensitive details."""


def validate_loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise QualificationError("MCP endpoint must be an explicit loopback HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise QualificationError("MCP endpoint must not contain credentials, query, or fragment")
    if parsed.path != "/mcp":
        raise QualificationError("MCP endpoint path must be /mcp")
    if parsed.port is None or not 1024 <= parsed.port <= 65535:
        raise QualificationError("MCP endpoint must use an explicit unprivileged port")
    return value


def validate_token(value: str) -> str:
    if len(value) < MIN_TOKEN_CHARS or value != value.strip() or any(char.isspace() for char in value):
        raise QualificationError(f"{TOKEN_ENV} must contain at least {MIN_TOKEN_CHARS} non-whitespace characters")
    return value


def dry_run_report(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schemaVersion": "resiliencebenchmark.mcp_endpoint_qualification/v1",
        "mode": "dry-run",
        "status": "not_executed",
        "token": {"source": f"env:{TOKEN_ENV}", "present": bool(env.get(TOKEN_ENV))},
        "endpoints": [
            {
                "name": spec.name,
                "urlSource": f"env:{spec.url_env}",
                "present": bool(env.get(spec.url_env)),
                "plannedChecks": ["unauthenticated-rejected", "initialize", "tools-list", "schema-boundary"],
            }
            for spec in ENDPOINTS
        ],
    }


async def qualify_endpoint(spec: EndpointSpec, url: str, token: str, timeout: float) -> dict[str, Any]:
    unauthenticated_rejected = False
    http_timeout = httpx2.Timeout(timeout, read=timeout)
    async with create_mcp_http_client(timeout=http_timeout) as unauthenticated:
        response = await unauthenticated.post(
            url,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "resbench-qualifier", "version": "1"},
                },
            },
        )
        unauthenticated_rejected = response.status_code in {401, 403}
    if not unauthenticated_rejected:
        raise QualificationError("MCP endpoint accepted an unauthenticated initialize request")

    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=http_timeout,
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
            ) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()

    tools = list(listed.tools)
    names = sorted(tool.name for tool in tools)
    if set(names) != set(spec.expected_tools) or any(not name.startswith(spec.tool_prefix) for name in names):
        raise QualificationError("MCP endpoint exposed an unexpected tool set")
    forbidden_schema_fields: dict[str, list[str]] = {}
    for tool in tools:
        properties = (tool.input_schema or {}).get("properties", {})
        forbidden = sorted(set(properties) & {"url", "base_url", "endpoint", "kubeconfig"})
        if forbidden:
            forbidden_schema_fields[tool.name] = forbidden
    if forbidden_schema_fields:
        raise QualificationError("MCP tool schema exposes a server-owned endpoint or kubeconfig")

    if spec.name in {"k8s_ro", "telemetry_ro", "source_ro"}:
        if any(not tool.annotations or not tool.annotations.read_only_hint for tool in tools):
            raise QualificationError("read-only MCP endpoint contains a tool without readOnlyHint")
        if any(tool.annotations and tool.annotations.destructive_hint for tool in tools):
            raise QualificationError("read-only MCP endpoint contains a destructive tool")
    if spec.name == "chaos_control":
        by_name = {tool.name: tool for tool in tools}
        for name in ("chaos_create_experiment", "chaos_destroy_experiment"):
            if name not in by_name or not by_name[name].annotations or not by_name[name].annotations.destructive_hint:
                raise QualificationError("chaos control destructive annotations are incomplete")

    return {
        "name": spec.name,
        "status": "qualified",
        "unauthenticatedRejected": True,
        "serverName": initialized.server_info.name,
        "protocolVersion": initialized.protocol_version,
        "toolCount": len(tools),
        "tools": names,
        "forbiddenSchemaFields": forbidden_schema_fields,
    }


EndpointChecker = Callable[[EndpointSpec, str, str, float], Awaitable[dict[str, Any]]]


async def run_qualification_async(
    env: Mapping[str, str],
    *,
    execute: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    checker: EndpointChecker = qualify_endpoint,
) -> dict[str, Any]:
    if not execute:
        return dry_run_report(env)
    if not 1 <= timeout <= 60:
        raise QualificationError("timeout must be between 1 and 60 seconds")
    token = validate_token(env.get(TOKEN_ENV, ""))
    resolved = [(spec, validate_loopback_url(env.get(spec.url_env, ""))) for spec in ENDPOINTS]
    results: list[dict[str, Any]] = []
    for spec, url in resolved:
        try:
            results.append(await checker(spec, url, token, timeout))
        except Exception as exc:  # noqa: BLE001 - output must remain redacted.
            results.append(
                {
                    "name": spec.name,
                    "status": "failed",
                    "errorType": type(exc).__name__,
                    "message": "endpoint qualification failed; inspect supervisor logs on the MCP host",
                }
            )
    qualified = all(item["status"] == "qualified" for item in results)
    return {
        "schemaVersion": "resiliencebenchmark.mcp_endpoint_qualification/v1",
        "mode": "execute",
        "status": "qualified" if qualified else "failed",
        "endpoints": results,
    }


def run_qualification(
    env: Mapping[str, str],
    *,
    execute: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    checker: EndpointChecker = qualify_endpoint,
) -> dict[str, Any]:
    return asyncio.run(run_qualification_async(env, execute=execute, timeout=timeout, checker=checker))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify four authenticated loopback BenchmarkFactory MCP endpoints")
    parser.add_argument("--execute", action="store_true", help="connect to endpoints; default is dry-run")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path, help="optional redacted JSON report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_qualification(os.environ, execute=args.execute, timeout=args.timeout)
    except QualificationError as exc:
        report = {
            "schemaVersion": "resiliencebenchmark.mcp_endpoint_qualification/v1",
            "mode": "execute" if args.execute else "dry-run",
            "status": "blocked",
            "message": str(exc),
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        if output == Path(output.anchor) or output == Path.home().resolve():
            print("qualify_mcp_endpoints: unsafe output path", file=sys.stderr)
            return 2
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["status"] in {"not_executed", "qualified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
