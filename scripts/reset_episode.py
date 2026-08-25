#!/usr/bin/env python3
"""Reset one benchmark Episode without deleting its application namespace.

The reset reuses the chaos_control MCP cleanup ledger, application-owned
workload cleanup behavior, Kubernetes readiness, and the deterministic workload
SLO verdict. It deliberately does not inspect tc, iptables, database snapshots,
or Nacos registry contents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx2
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from scripts.deploy_application import DeployError, wait_ready
from scripts.deploy_application import SubprocessRunner as DeployRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_APPLICATIONS = ("train-ticket", "sock-shop", "otel-demo")
HANDLE_RE = re.compile(r"^cleanup-[a-z0-9][a-z0-9._-]{6,120}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
TOKEN_ENV = "RESBENCH_MCP_TOKEN"
CHAOS_URL_ENV = "RESBENCH_CHAOS_CONTROL_MCP_URL"
TRAIN_IMAGE_ENV = "RESBENCH_TRAIN_TICKET_WORKLOAD_IMAGE"
LOCUST_IMAGE_ENV = "RESBENCH_LOCUST_WORKLOAD_IMAGE"


class ResetError(RuntimeError):
    """Expected reset failure safe to print."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300) -> CommandResult:
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ResetError("reset command timed out") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def safe_error(value: str) -> str:
    text = " ".join(value.strip().split())
    text = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text[:1200] or "no diagnostic output"


def run_checked(runner: Runner, argv: list[str], *, stdin: str | None = None, timeout: int = 300) -> str:
    result = runner.run(argv, stdin=stdin, timeout=timeout)
    if result.returncode != 0:
        raise ResetError(f"command failed: {' '.join(argv[:4])}: {safe_error(result.stderr or result.stdout)}")
    return result.stdout


def validate_chaos_endpoint(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ResetError("chaos_control endpoint must be an explicit loopback HTTP URL")
    if parsed.path != "/mcp" or parsed.port is None or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResetError("chaos_control endpoint must be a clean loopback /mcp URL with an explicit port")
    return url


def validate_token(value: str) -> str:
    if len(value) < 32 or value != value.strip() or any(char.isspace() for char in value):
        raise ResetError(f"{TOKEN_ENV} must contain at least 32 non-whitespace characters")
    return value


def extract_tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
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
    raise ResetError("chaos_control returned no structured JSON payload")


async def call_chaos_tool(url: str, token: str, tool: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    http_timeout = httpx2.Timeout(timeout, read=timeout)
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=http_timeout,
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=timeout) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise ResetError(f"chaos_control tool {tool} returned an error")
    payload = extract_tool_payload(result)
    if payload.get("ok") is False:
        code = payload.get("error", {}).get("code", "CHAOS_CONTROL_ERROR")
        raise ResetError(f"chaos_control tool {tool} failed: {code}")
    return payload


ChaosCaller = Callable[[str, str, str, dict[str, Any], float], Awaitable[dict[str, Any]]]


async def cleanup_fault(
    url: str,
    token: str,
    cleanup_handle: str,
    *,
    timeout: float,
    caller: ChaosCaller = call_chaos_tool,
) -> dict[str, Any]:
    destroyed = await caller(url, token, "chaos_destroy_experiment", {"cleanup_handle": cleanup_handle}, timeout)
    status = await caller(url, token, "chaos_recovery_status", {"cleanup_handle": cleanup_handle}, timeout)
    absent = bool(destroyed.get("verified_absent")) or str(status.get("state", "")).lower() in {
        "destroyed",
        "expired_cleaned",
        "absent",
    }
    if not absent:
        raise ResetError("chaos_control did not verify absence of the ledger-owned experiment")
    return {
        "cleanupHandle": cleanup_handle,
        "verifiedAbsent": True,
        "destroyed": destroyed.get("destroyed"),
        "state": status.get("state"),
    }


def workload_contract(application: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = yaml.safe_load((REPO_ROOT / "environment/workloads/deterministic-profiles.yaml").read_text(encoding="utf-8"))
    defaults = data["spec"]["defaults"]
    app = next(item for item in data["spec"]["applications"] if item["id"] == application)
    return defaults, app


def baseline_gate(application: str) -> dict[str, Any]:
    defaults, app = workload_contract(application)
    slo = app["entrySlo"]
    gate = {
        "minimumSuccessRate": slo["minimumSuccessRate"],
        "maximumErrorRate": slo["maximumErrorRate"],
        "maximumP95LatencyMs": slo["p95LatencyMs"],
    }
    if application == "otel-demo":
        calibrated = yaml.safe_load((REPO_ROOT / "environment/workloads/calibrated-baselines.yaml").read_text(encoding="utf-8"))
        baseline = next(item for item in calibrated["spec"]["applications"] if item["id"] == "otel-demo")
        gate["minimumThroughputRps"] = baseline["minimumThroughputRps"]
        gate["measurementWindowSeconds"] = calibrated["spec"]["method"]["measurementWindowSeconds"]
    else:
        gate["minimumThroughputRps"] = None
        gate["measurementWindowSeconds"] = defaults["evaluationWindowSeconds"]
    return gate


def workload_command(
    application: str,
    run_id: str,
    kubeconfig: Path,
    env: Mapping[str, str],
    duration_seconds: int = 60,
) -> list[str]:
    if not 60 <= duration_seconds <= 21_600:
        raise ResetError("workload duration_seconds must be between 60 and 21600")
    python = str(Path(sys.executable))
    if application == "train-ticket":
        image = env.get(TRAIN_IMAGE_ENV, "")
        if not image:
            raise ResetError(f"{TRAIN_IMAGE_ENV} is required for Train-Ticket reset qualification")
        return [
            python,
            str(REPO_ROOT / "scripts/train_ticket_workload.py"),
            "start",
            "--profile",
            "baseline",
            "--fixture",
            str(REPO_ROOT / "environment/workloads/train-ticket/runtime-fixture.example.yaml"),
            "--run-id",
            run_id,
            "--namespace",
            "train-ticket",
            "--duration-seconds",
            str(duration_seconds),
            "--image",
            image,
            "--kubeconfig",
            str(kubeconfig),
            "--execute",
        ]
    image = env.get(LOCUST_IMAGE_ENV, "")
    if not image:
        raise ResetError(f"{LOCUST_IMAGE_ENV} is required for {application} reset qualification")
    return [
        python,
        str(REPO_ROOT / "scripts/locust_workload.py"),
        "start",
        "--application",
        application,
        "--fixture",
        str(REPO_ROOT / f"environment/workloads/{application}/runtime-fixture.example.yaml"),
        "--run-id",
        run_id,
        "--duration-seconds",
        str(duration_seconds),
        "--image",
        image,
        "--kubeconfig",
        str(kubeconfig),
        "--execute",
    ]


def wait_cleanup_workload(
    runner: Runner,
    application: str,
    run_id: str,
    kubeconfig: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    duration_seconds: int = 60,
    start_job: bool = True,
) -> dict[str, Any]:
    if start_job:
        command = workload_command(
            application,
            run_id,
            kubeconfig,
            env,
            duration_seconds=duration_seconds,
        )
        run_checked(runner, command, timeout=120)
    selector = f"resiliencebenchmark.io/workload={application},resiliencebenchmark.io/run-id={run_id}"
    run_checked(
        runner,
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "wait",
            "--for=condition=complete",
            "job",
            "-n",
            application,
            "-l",
            selector,
            f"--timeout={timeout_seconds}s",
        ],
        timeout=timeout_seconds + 30,
    )
    summary: dict[str, Any] | None = None
    if application in {"sock-shop", "otel-demo"}:
        image = env.get(LOCUST_IMAGE_ENV, "")
        reader_name = f"rb-summary-{run_id}"[:63].rstrip("-")
        summary_path = f"/results/{application}-summary.json"
        reader = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": reader_name,
                "namespace": application,
                "labels": {
                    "resiliencebenchmark.io/workload": application,
                    "resiliencebenchmark.io/run-id": run_id,
                    "resiliencebenchmark.io/summary-reader": "true",
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "summary-reader",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c", f"cat {summary_path}"],
                        "volumeMounts": [{"name": "results", "mountPath": "/results", "readOnly": True}],
                    }
                ],
                "volumes": [
                    {
                        "name": "results",
                        "persistentVolumeClaim": {"claimName": f"{application}-workload-results"},
                    }
                ],
            },
        }
        run_checked(
            runner,
            ["kubectl", "--kubeconfig", str(kubeconfig), "apply", "-f", "-"],
            stdin=yaml.safe_dump(reader, sort_keys=False),
            timeout=60,
        )
        run_checked(
            runner,
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{reader_name}",
                "-n",
                application,
                "--timeout=90s",
            ],
            timeout=120,
        )
        summary_raw = run_checked(
            runner,
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "logs",
                "-n",
                application,
                reader_name,
            ],
            timeout=30,
        )
        try:
            summary = json.loads(summary_raw)
        except json.JSONDecodeError as exc:
            raise ResetError("workload summary is not valid JSON") from exc
        if not summary.get("qualified"):
            raise ResetError("workload summary did not return to the configured baseline gate")
        run_checked(
            runner,
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "delete",
                f"pod/{reader_name}",
                "-n",
                application,
                "--ignore-not-found=true",
                "--wait=true",
            ],
            timeout=60,
        )
    run_checked(
        runner,
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "delete",
            "job,configmap",
            "-n",
            application,
            "-l",
            selector,
            "--ignore-not-found=true",
        ],
        timeout=60,
    )
    result = {"runId": run_id, "selector": selector, "jobComplete": True, "objectsRemoved": True}
    if summary is not None:
        result["summary"] = {
            key: summary.get(key)
            for key in (
                "requests",
                "failures",
                "successRate",
                "errorRate",
                "p95LatencyMs",
                "throughputRps",
                "minimumThroughputRps",
                "qualified",
                "measurementWindow",
                "checks",
                "randomSeed",
                "trafficMix",
            )
        }
    return result


def wait_application_ready(application: str, kubeconfig: Path, timeout_seconds: int) -> None:
    try:
        wait_ready(DeployRunner(), kubeconfig, application, timeout_seconds, {"load-generator"} if application == "otel-demo" else set())
    except DeployError as exc:
        raise ResetError(str(exc)) from exc


def plan(application: str, namespace: str, cleanup_handle: str | None) -> dict[str, Any]:
    return {
        "schemaVersion": "resiliencebenchmark.episode_reset/v1",
        "phase": "plan",
        "application": application,
        "namespace": namespace,
        "cleanupHandlePresent": bool(cleanup_handle),
        "steps": [
            "chaos_control destroy ledger-owned experiment and verify CR absence",
            "run deterministic workload cleanup/qualification job",
            "wait for all non-intentionally-standby application controllers Ready",
            "require workload SLO verdict inside the application baseline gate",
        ],
        "residualChecks": ["ledger-owned ChaosBlade CR absent", "workload metrics inside baseline gate"],
        "excludedChecks": ["tc qdisc", "iptables", "database snapshot", "Nacos registry"],
        "baselineGate": baseline_gate(application),
    }


def run_reset(
    args: argparse.Namespace,
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
    chaos_caller: ChaosCaller = call_chaos_tool,
) -> dict[str, Any]:
    runner = runner or SubprocessCommandRunner()
    values = os.environ if env is None else env
    report = plan(args.application, args.namespace, args.cleanup_handle)
    report["mode"] = "execute" if args.execute else "dry-run"
    if not args.execute:
        return report
    if args.namespace != args.application:
        raise ResetError("reset currently supports the live application namespace only")
    if not args.kubeconfig or not args.kubeconfig.expanduser().resolve().is_file():
        raise ResetError("--execute requires an explicit existing kubeconfig")
    if not args.cleanup_handle or not HANDLE_RE.fullmatch(args.cleanup_handle):
        raise ResetError("--execute requires a valid chaos cleanup handle")
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise ResetError("run id must be a safe lowercase identifier")
    url = validate_chaos_endpoint(values.get(CHAOS_URL_ENV, ""))
    token = validate_token(values.get(TOKEN_ENV, ""))
    cleanup = asyncio.run(
        cleanup_fault(url, token, args.cleanup_handle, timeout=float(args.mcp_timeout), caller=chaos_caller)
    )
    kubeconfig = args.kubeconfig.expanduser().resolve()
    workload = wait_cleanup_workload(runner, args.application, args.run_id, kubeconfig, values, args.timeout)
    wait_application_ready(args.application, kubeconfig, args.timeout)
    report.update(
        {
            "phase": "complete",
            "cleanup": cleanup,
            "workload": workload,
            "readiness": "ready",
            "baseline": {"status": "qualified-by-workload-job", **baseline_gate(args.application)},
        }
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset one benchmark Episode without deleting its namespace")
    parser.add_argument("--application", choices=SUPPORTED_APPLICATIONS, required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--cleanup-handle")
    parser.add_argument("--run-id", default="episode-reset-check")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--mcp-timeout", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.namespace = args.namespace or args.application
    if not 60 <= args.timeout <= 3600 or not 1 <= args.mcp_timeout <= 60:
        print("reset_episode: invalid timeout", file=sys.stderr)
        return 2
    try:
        report = run_reset(args)
    except ResetError as exc:
        print(json.dumps({"schemaVersion": "resiliencebenchmark.episode_reset/v1", "phase": "failed", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
