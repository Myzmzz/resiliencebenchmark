#!/usr/bin/env python3
"""Run one bounded CPU fault through the manual Codex capability and verify recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


def private_json(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    info = resolved.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("private input must be a regular mode-0600 file")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("private input must contain one JSON object")
    return value


def atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(resolved.parent, 0o700)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
        os.chmod(resolved, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


async def call_tool(
    *,
    url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    timeout = httpx2.Timeout(60, read=60)
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    ) as client, streamable_http_client(url, http_client=client) as (
        read_stream,
        write_stream,
    ), ClientSession(
        read_stream, write_stream, read_timeout_seconds=60
    ) as session:
        await session.initialize()
        result = await session.call_tool(name, arguments=arguments)
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"MCP tool returned no structured object: {name}")


def kubectl(
    kubeconfig: Path,
    arguments: list[str],
    *,
    timeout_seconds: int = 60,
) -> str:
    completed = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode:
        raise RuntimeError("scoped kubectl observation failed")
    return completed.stdout


def pod_sample(kubeconfig: Path, namespace: str, name: str) -> dict[str, Any]:
    pod = json.loads(kubectl(kubeconfig, ["-n", namespace, "get", "pod", name, "-o", "json"]))
    status = pod["status"]["containerStatuses"][0]
    ready = any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in pod["status"].get("conditions", [])
    )
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "uid": pod["metadata"]["uid"],
        "ready": ready,
        "restart_count": int(status.get("restartCount", 0)),
        "container_id": status.get("containerID"),
    }


def cpu_sample(kubeconfig: Path, namespace: str, name: str) -> dict[str, Any]:
    raw = kubectl(
        kubeconfig,
        ["top", "pod", "-n", namespace, name, "--containers", "--no-headers"],
    )
    fields = raw.split()
    if len(fields) < 4:
        raise RuntimeError("kubectl top returned an unexpected row")
    cpu = fields[-2]
    millicores = int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * 1000)
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "cpu": cpu,
        "cpu_millicores": millicores,
        "memory": fields[-1],
    }


def business_sample(kubeconfig: Path) -> dict[str, Any]:
    raw = kubectl(
        kubeconfig,
        [
            "get",
            "--raw",
            "/api/v1/namespaces/otel-demo/services/http:load-generator:8089/proxy/stats/requests",
        ],
    )
    payload = json.loads(raw)
    aggregate = next(item for item in payload["stats"] if item.get("name") == "Aggregated")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "state": payload.get("state"),
        "user_count": int(payload.get("user_count") or 0),
        "num_requests": int(aggregate.get("num_requests") or 0),
        "num_failures": int(aggregate.get("num_failures") or 0),
        "current_rps": float(aggregate.get("current_rps") or 0.0),
        "current_fail_per_sec": float(aggregate.get("current_fail_per_sec") or 0.0),
        "p95_ms": float(aggregate.get("response_time_percentile_0.95") or 0.0),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    session_path = args.session.expanduser().resolve()
    session = private_json(session_path)
    expires_at = datetime.fromisoformat(str(session["expires_at"]))
    if expires_at <= datetime.now(timezone.utc):
        raise RuntimeError("manual capability has expired")
    target = dict(session["target"])
    kubeconfig = args.kubeconfig.expanduser().resolve()
    token = os.environ.get(args.mcp_token_env, "")
    if len(token) < 32 or any(char.isspace() for char in token):
        raise RuntimeError("MCP bearer token is unavailable")
    create_arguments = {
        "run_id": session["run_id"],
        "namespace": target["namespace"],
        "target_name": target["name"],
        "target_uid": target["uid"],
        "fault_type": "cpu-load",
        "duration_seconds": args.duration_seconds,
        "intensity": {"cpu_percent": args.cpu_percent},
        "controller_token_ref": session["controller_token_ref"],
        "expected_controller_pod_uid": session["expected_controller_pod_uid"],
        "baseline_gate_token": session["baseline_gate_token"],
        "cleanup_handle": session["cleanup_handle"],
    }
    report: dict[str, Any] = {
        "schema_version": "manual-chaos-qualification.v1",
        "run_id": session["run_id"],
        "target": target,
        "fault": {
            "type": "cpu-load",
            "cpu_percent": args.cpu_percent,
            "duration_seconds": args.duration_seconds,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "samples": [],
    }
    report["preflight"] = {
        "pod": pod_sample(kubeconfig, target["namespace"], target["name"]),
        "cpu": cpu_sample(kubeconfig, target["namespace"], target["name"]),
        "business": business_sample(kubeconfig),
        "inventory": await call_tool(
            url=args.chaos_url,
            token=token,
            name="chaos_inventory_run",
            arguments={"namespace": target["namespace"]},
        ),
        "plan": await call_tool(
            url=args.chaos_url,
            token=token,
            name="chaos_validate_plan",
            arguments={
                key: value
                for key, value in create_arguments.items()
                if key
                in {
                    "run_id",
                    "namespace",
                    "target_name",
                    "target_uid",
                    "fault_type",
                    "duration_seconds",
                    "intensity",
                }
            },
        ),
    }
    if report["preflight"]["inventory"].get("global_chaosblade_count") != 0:
        raise RuntimeError("preflight found an existing ChaosBlade resource")
    if report["preflight"]["plan"].get("ok") is not True:
        raise RuntimeError("controller rejected the fault plan")

    created = False
    experiment_name = ""
    cleanup_error: str | None = None
    started = time.monotonic()
    try:
        create = await call_tool(
            url=args.chaos_url,
            token=token,
            name="chaos_create_experiment",
            arguments=create_arguments,
        )
        report["create"] = create
        if create.get("ok") is not True:
            error = create.get("error", {})
            raise RuntimeError(
                f"fault creation was rejected: {error.get('code', 'UNKNOWN')}"
            )
        created = True
        experiment_name = str(create["created"]["name"])
        for target_second in (15, 60, 180, args.duration_seconds - 5):
            remaining = target_second - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            report["samples"].append(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "experiment": await call_tool(
                        url=args.chaos_url,
                        token=token,
                        name="chaos_get_experiment",
                        arguments={
                            "namespace": target["namespace"],
                            "name": experiment_name,
                        },
                    ),
                    "pod": pod_sample(kubeconfig, target["namespace"], target["name"]),
                    "cpu": cpu_sample(kubeconfig, target["namespace"], target["name"]),
                    "business": business_sample(kubeconfig),
                }
            )
    finally:
        if created:
            try:
                report["destroy"] = await call_tool(
                    url=args.chaos_url,
                    token=token,
                    name="chaos_destroy_experiment",
                    arguments={"cleanup_handle": session["cleanup_handle"]},
                )
            except Exception as exc:  # noqa: BLE001 - final report must retain cleanup failure.
                cleanup_error = type(exc).__name__

    recovery_samples = []
    recovery_started = time.monotonic()
    for target_second in (15, 45, 75):
        remaining = target_second - (time.monotonic() - recovery_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        recovery_samples.append(
            {
                "elapsed_seconds": round(time.monotonic() - recovery_started, 1),
                "pod": pod_sample(kubeconfig, target["namespace"], target["name"]),
                "cpu": cpu_sample(kubeconfig, target["namespace"], target["name"]),
                "business": business_sample(kubeconfig),
            }
        )
    report["recovery"] = {
        "status": await call_tool(
            url=args.chaos_url,
            token=token,
            name="chaos_recovery_status",
            arguments={"cleanup_handle": session["cleanup_handle"]},
        ),
        "inventory": await call_tool(
            url=args.chaos_url,
            token=token,
            name="chaos_inventory_run",
            arguments={"namespace": target["namespace"]},
        ),
        "samples": recovery_samples,
        "cleanup_error": cleanup_error,
    }
    preflight = report["preflight"]
    max_fault_cpu = max(item["cpu"]["cpu_millicores"] for item in report["samples"])
    final_pod = recovery_samples[-1]["pod"]
    final_business = recovery_samples[-1]["business"]
    request_delta = final_business["num_requests"] - preflight["business"]["num_requests"]
    failure_delta = final_business["num_failures"] - preflight["business"]["num_failures"]
    report["gates"] = {
        "fault_effect_verified": max_fault_cpu
        >= preflight["cpu"]["cpu_millicores"] + 500,
        "controller_cleanup_verified": report["recovery"]["status"].get("resource_absent")
        is True,
        "global_inventory_zero": report["recovery"]["inventory"].get(
            "global_chaosblade_count"
        )
        == 0,
        "target_identity_stable": final_pod["uid"] == target["uid"],
        "target_ready_without_restart": final_pod["ready"] is True
        and final_pod["restart_count"] == preflight["pod"]["restart_count"],
        "business_recovery_verified": request_delta > 0
        and (failure_delta / request_delta if request_delta else 1.0) <= 0.05
        and final_business["current_fail_per_sec"] == 0
        and final_business["p95_ms"] <= 1000,
    }
    report["metrics"] = {
        "baseline_cpu_millicores": preflight["cpu"]["cpu_millicores"],
        "maximum_fault_cpu_millicores": max_fault_cpu,
        "final_cpu_millicores": recovery_samples[-1]["cpu"]["cpu_millicores"],
        "business_request_delta": request_delta,
        "business_failure_delta": failure_delta,
    }
    report["qualified"] = all(report["gates"].values())
    report["status"] = "qualified" if report["qualified"] else "failed"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    session_path.unlink(missing_ok=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chaos-url", default="http://127.0.0.1:18084/mcp")
    parser.add_argument("--mcp-token-env", default="RESBENCH_MCP_TOKEN")
    parser.add_argument("--cpu-percent", type=int, default=80)
    parser.add_argument("--duration-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.cpu_percent <= 80:
        raise SystemExit("cpu-percent must be between 1 and 80")
    if not 60 <= args.duration_seconds <= 300:
        raise SystemExit("duration-seconds must be between 60 and 300")
    report = asyncio.run(run(args))
    atomic_private_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "run_id": report["run_id"],
                "qualified": report["qualified"],
                "gates": report["gates"],
                "metrics": report["metrics"],
                "output": str(args.output.expanduser().resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["qualified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
