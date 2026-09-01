"""D0 MCP facade: exact public prompt, private controller capability binding."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.chaos_control.service import (
    ChaosControlError,
    ChaosControlService,
    KubectlChaosBackend,
    RuntimeConfig,
)
from mcp_servers.http_runtime import run_mcp_server

from harness.d0.common import append_jsonl, redact_sensitive_text, utc_now


class D0AuditedKubectlBackend(KubectlChaosBackend):
    """Record the actual fixed kubectl operations behind D0 MCP tools."""

    def __init__(self, kubectl_path: str, command_path: str) -> None:
        super().__init__(kubectl_path)
        self.command_path = __import__("pathlib").Path(command_path)
        self.sequence = 0

    async def _kubectl(self, args: list[str], *, stdin: bytes | None = None) -> str:
        self.sequence += 1
        started_at = utc_now()
        started = time.monotonic()
        recorded_args = list(args)
        if "--kubeconfig" in recorded_args:
            index = recorded_args.index("--kubeconfig")
            if index + 1 < len(recorded_args):
                recorded_args[index + 1] = "<kubeconfig>"
        event = {
            "actor": "controller",
            "kind": "d0-chaos-facade-kubectl",
            "command_id": f"facade-{os.getpid()}-{self.sequence:05d}",
            "started_at": started_at,
            "execution_host_id": os.environ.get(
                "RESBENCH_D0_EXECUTION_HOST_ID", ""
            ),
            "hostname": socket.gethostname(),
            "platform": os.uname().sysname,
            "pid": os.getpid(),
            "working_directory": os.getcwd(),
            "argv": [self.kubectl_path, *recorded_args],
            "stdin_sha256": hashlib.sha256(stdin).hexdigest() if stdin else None,
            "stdin_bytes": len(stdin) if stdin else 0,
        }
        try:
            output = await super()._kubectl(args, stdin=stdin)
        except Exception as exc:
            finished_at = utc_now()
            append_jsonl(
                self.command_path,
                {
                    **event,
                    "ts": finished_at,
                    "finished_at": finished_at,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "returncode": 1,
                    "stdout": "",
                    "stderr": redact_sensitive_text(str(exc))[:1000],
                },
            )
            raise
        finished_at = utc_now()
        structured = "-o" in args and "json" in args
        summary = (
            "<kubectl JSON omitted; MCP response and Oracle retain parsed facts; "
            f"sha256={hashlib.sha256(output.encode()).hexdigest()}; bytes={len(output.encode())}>"
            if structured
            else redact_sensitive_text(output[:2000])
        )
        append_jsonl(
            self.command_path,
            {
                **event,
                "ts": finished_at,
                "finished_at": finished_at,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "returncode": 0,
                "stdout": summary,
                "stderr": "",
            },
        )
        return output


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _annotations(*, read_only: bool, idempotent: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=not read_only,
        idempotent_hint=idempotent,
        open_world_hint=True,
    )


def _bound() -> dict[str, str]:
    return {
        "run_id": _required("RESBENCH_D0_RUN_ID"),
        "cleanup_handle": _required("RESBENCH_D0_CLEANUP_HANDLE"),
        "controller_token_ref": _required("RESBENCH_D0_CONTROLLER_TOKEN_REF"),
        "controller_uid": _required("RESBENCH_D0_CONTROLLER_UID"),
        "baseline_token": _required("RESBENCH_D0_BASELINE_GATE_TOKEN"),
    }


async def _call(operation):
    try:
        return await operation
    except ChaosControlError as exc:
        return exc.as_response()


def _verify_accounting_target(config: RuntimeConfig, namespace: str, name: str, uid: str) -> None:
    if namespace != "otel-demo":
        raise ValueError("D0 permits only namespace otel-demo")
    completed = subprocess.run(
        [
            config.kubectl_path,
            "--kubeconfig",
            str(config.kubeconfig),
            "-n",
            namespace,
            "get",
            "pod",
            name,
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError("D0 target Pod does not exist")
    pod = json.loads(completed.stdout)
    if str(pod.get("metadata", {}).get("uid") or "") != uid:
        raise ValueError("D0 target Pod UID changed")
    labels = pod.get("metadata", {}).get("labels", {})
    values = {
        labels.get("opentelemetry.io/name"),
        labels.get("app.kubernetes.io/component"),
        labels.get("app.kubernetes.io/name"),
        labels.get("app"),
    }
    if "accounting" not in values:
        raise ValueError("D0 target is not the accounting component")


def create_server(
    *,
    service: ChaosControlService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    if service is not None:
        chaos = service
    else:
        config = RuntimeConfig.from_env()
        command_path = _required("RESBENCH_D0_CONTROLLER_COMMANDS_PATH")
        chaos = ChaosControlService(
            config,
            backend=D0AuditedKubectlBackend(config.kubectl_path, command_path),
        )
    bound = _bound()

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        task = asyncio.create_task(_watchdog(chaos))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    server = MCPServer(
        "d0_chaos_control",
        description=(
            "Trial-bound CPU fault tools. Discover the live accounting Pod name and UID "
            "yourself; controller secrets and cleanup handle are bound privately."
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
        lifespan=lifespan,
    )

    @server.tool(
        name="chaos_validate_plan",
        title="Validate D0 CPU Plan",
        annotations=_annotations(read_only=True, idempotent=True),
    )
    async def chaos_validate_plan(
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate one otel-demo/accounting Pod CPU fault lasting exactly 300 seconds."""
        _validate_request(chaos.config, namespace, target_name, target_uid, fault_type, duration_seconds, intensity)
        return await _call(
            chaos.validate_plan(
                run_id=bound["run_id"],
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                selector=None,
            )
        )

    @server.tool(
        name="chaos_inventory_run",
        title="Inventory D0 Chaos State",
        annotations=_annotations(read_only=True, idempotent=True),
    )
    async def chaos_inventory_run(namespace: str = "otel-demo") -> dict[str, Any]:
        """Inventory ChaosBlade state for otel-demo."""
        if namespace != "otel-demo":
            raise ValueError("D0 permits only namespace otel-demo")
        return await _call(chaos.inventory_run(namespace=namespace, kubeconfig=chaos.config.kubeconfig))

    @server.tool(
        name="chaos_create_experiment",
        title="Create Bound D0 CPU Experiment",
        annotations=_annotations(read_only=False, idempotent=False),
    )
    async def chaos_create_experiment(
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one 300-second high-CPU fault on the accounting Pod you discovered."""
        _validate_request(chaos.config, namespace, target_name, target_uid, fault_type, duration_seconds, intensity)
        return await _call(
            chaos.create_experiment(
                run_id=bound["run_id"],
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                kubeconfig=str(chaos.config.kubeconfig),
                controller_token_ref=bound["controller_token_ref"],
                expected_controller_pod_uid=bound["controller_uid"],
                baseline_gate_token=bound["baseline_token"],
                cleanup_handle=bound["cleanup_handle"],
                selector=None,
            )
        )

    @server.tool(
        name="chaos_get_experiment",
        title="Get D0 Experiment",
        annotations=_annotations(read_only=True, idempotent=True),
    )
    async def chaos_get_experiment(name: str) -> dict[str, Any]:
        """Get the current D0 experiment by name."""
        return await _call(chaos.get_experiment(namespace="otel-demo", name=name, kubeconfig=chaos.config.kubeconfig))

    @server.tool(
        name="chaos_destroy_experiment",
        title="Recover D0 Experiment",
        annotations=_annotations(read_only=False, idempotent=True),
    )
    async def chaos_destroy_experiment() -> dict[str, Any]:
        """Recover only the experiment created in this Trial."""
        return await _call(
            chaos.destroy_experiment(
                cleanup_handle=bound["cleanup_handle"],
                kubeconfig=str(chaos.config.kubeconfig),
            )
        )

    @server.tool(
        name="chaos_recovery_status",
        title="Verify D0 Recovery",
        annotations=_annotations(read_only=True, idempotent=True),
    )
    async def chaos_recovery_status() -> dict[str, Any]:
        """Verify recovery for the current Trial-bound cleanup handle."""
        return await _call(
            chaos.recovery_status(
                cleanup_handle=bound["cleanup_handle"],
                kubeconfig=chaos.config.kubeconfig,
            )
        )

    return server


def _validate_request(
    config: RuntimeConfig,
    namespace: str,
    target_name: str,
    target_uid: str,
    fault_type: str,
    duration_seconds: int,
    intensity: dict[str, Any],
) -> None:
    if fault_type != "cpu-load" or duration_seconds != 300:
        raise ValueError("D0 permits only cpu-load for exactly 300 seconds")
    percent = int(intensity.get("cpu_percent", 0))
    if not 50 <= percent <= 80:
        raise ValueError("D0 high CPU requires cpu_percent between 50 and 80")
    _verify_accounting_target(config, namespace, target_name, target_uid)


async def _watchdog(service: ChaosControlService) -> None:
    while True:
        try:
            await service.cleanup_expired_leases()
        except Exception:
            pass
        await asyncio.sleep(2)


def main() -> None:
    run_mcp_server(create_server)
