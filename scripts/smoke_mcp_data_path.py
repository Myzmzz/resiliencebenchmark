#!/usr/bin/env python3
"""Exercise real read-only MCP tools and the cross-process metric-gap channel."""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from disturbances.file_telemetry_interceptor import FileTelemetryRuleClient

TOKEN_ENV = "RESBENCH_MCP_TOKEN"
ENDPOINT_KEYS = (
    "RESBENCH_K8S_MCP_URL",
    "RESBENCH_TELEMETRY_MCP_URL",
    "RESBENCH_SOURCE_MCP_URL",
    "RESBENCH_CHAOS_CONTROL_MCP_URL",
)


def load_private_env(path: Path) -> dict[str, str]:
    resolved = path.expanduser().resolve()
    info = resolved.lstat()
    if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("stack env must be a regular mode-0600 file")
    values = {}
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        value = json.loads(raw)
        if isinstance(value, str):
            values[key] = value
    return values


async def call_tool(
    url: str,
    token: str,
    tool: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    timeout = httpx2.Timeout(30, read=30)
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as client, streamable_http_client(url, http_client=client) as (
        read_stream,
        write_stream,
    ), ClientSession(
        read_stream,
        write_stream,
        read_timeout_seconds=30,
    ) as session:
        await session.initialize()
        result = await session.call_tool(tool, arguments=dict(arguments))
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool returned an error: {tool}")
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"MCP tool returned no structured object: {tool}")


def point_count(payload: Mapping[str, Any]) -> int:
    result = payload.get("result")
    if not isinstance(result, list):
        return 0
    return sum(
        len(series.get("values", []))
        for series in result
        if isinstance(series, Mapping) and isinstance(series.get("values"), list)
    )


async def run_smoke_async(
    env: Mapping[str, str],
    *,
    disturbance_dir: Path,
) -> dict[str, Any]:
    token = env.get(TOKEN_ENV, "")
    if len(token) < 32:
        raise RuntimeError("MCP token is missing or too short")
    missing = [key for key in ENDPOINT_KEYS if not env.get(key)]
    if missing:
        raise RuntimeError("MCP endpoint settings are missing")
    k8s = await call_tool(
        env["RESBENCH_K8S_MCP_URL"],
        token,
        "k8s_list_resources",
        {"namespace": "otel-demo", "resource": "pods", "limit": 5, "offset": 0},
    )
    source = await call_tool(
        env["RESBENCH_SOURCE_MCP_URL"],
        token,
        "source_search_text",
        {
            "repo_id": "otel-demo-2.2.0",
            "query": "AbortSignal",
            "path": "src/frontend",
            "case_sensitive": True,
            "limit": 10,
            "offset": 0,
            "format": "json",
        },
    )
    chaos = await call_tool(
        env["RESBENCH_CHAOS_CONTROL_MCP_URL"],
        token,
        "chaos_inventory_run",
        {"namespace": "otel-demo"},
    )
    now = int(time.time())
    metric_args = {
        "metric": "kube_pod_status_ready",
        "start": now - 300,
        "end": now,
        "step": 60,
        "labels": {"condition": "true"},
        "group_by": ["pod"],
        "limit": 50,
    }
    telemetry_url = env["RESBENCH_TELEMETRY_MCP_URL"]
    baseline = await call_tool(
        telemetry_url,
        token,
        "telemetry_prom_metric_range",
        metric_args,
    )
    baseline_points = point_count(baseline)
    if baseline_points < 2:
        raise RuntimeError("baseline telemetry range has insufficient points")

    rule_client = FileTelemetryRuleClient(disturbance_dir)
    rule_id = rule_client.register_rule(
        run_id=f"mcp-data-path-smoke-{now}",
        level_id="L3",
        disturbance_id="metric-gap-smoke",
        rule={
            "kind": "metric_data_gap",
            "tool": "telemetry_prom_metric_range",
            "missing_slots": [2],
        },
    )
    try:
        disturbed = await call_tool(
            telemetry_url,
            token,
            "telemetry_prom_metric_range",
            metric_args,
        )
    finally:
        rule_client.remove_rule(rule_id)
    recovered = await call_tool(
        telemetry_url,
        token,
        "telemetry_prom_metric_range",
        metric_args,
    )
    disturbed_points = point_count(disturbed)
    recovered_points = point_count(recovered)
    events = [event for event in rule_client.events() if event.get("rule_id") == rule_id]
    k8s_items = (
        len(k8s.get("items", [])) if isinstance(k8s.get("items"), list) else 0
    )
    source_evidence_found = "AbortSignal" in json.dumps(source, ensure_ascii=False)
    global_chaos_count = chaos.get("global_chaosblade_count")
    active_owned_count = chaos.get("active_owned_count")
    metric_gap_verified = (
        disturbed_points < baseline_points
        and recovered_points == baseline_points
        and any(
            event.get("status") == "response_transformed"
            and int(event.get("detail", {}).get("removed_points", 0)) > 0
            for event in events
        )
    )
    all_qualified = (
        k8s.get("ok") is not False
        and k8s_items > 0
        and source.get("ok") is not False
        and source_evidence_found
        and chaos.get("ok") is not False
        and global_chaos_count == 0
        and active_owned_count == 0
        and metric_gap_verified
    )
    return {
        "schema_version": "mcp-data-path-smoke.v1",
        "status": "qualified" if all_qualified else "failed",
        "k8s": {
            "ok": k8s.get("ok") is not False,
            "returned_items": k8s_items,
        },
        "source": {
            "ok": source.get("ok") is not False,
            "evidence_found": source_evidence_found,
        },
        "chaos": {
            "ok": chaos.get("ok") is not False,
            "global_count": global_chaos_count,
            "active_owned_count": active_owned_count,
        },
        "metric_gap": {
            "rule_id": rule_id,
            "baseline_points": baseline_points,
            "disturbed_points": disturbed_points,
            "recovered_points": recovered_points,
            "evidence_event_count": len(events),
            "verified": metric_gap_verified,
        },
    }


def run_smoke(env: Mapping[str, str], *, disturbance_dir: Path) -> dict[str, Any]:
    return asyncio.run(run_smoke_async(env, disturbance_dir=disturbance_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-env", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        env = load_private_env(args.stack_env)
        disturbance_dir = Path(env["RESBENCH_TELEMETRY_DISTURBANCE_DIR"])
        report = run_smoke(env, disturbance_dir=disturbance_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "mcp-data-path-smoke.v1",
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        path = args.output.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["status"] == "qualified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
