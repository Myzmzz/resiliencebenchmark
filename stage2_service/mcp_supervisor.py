"""Run all MCP modules as loopback child processes of the single Stage-2 service."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .contracts import HarnessKind, McpServerName


class McpSupervisorError(RuntimeError):
    pass


class McpSupervisor:
    HTTP_PORTS = {
        "k8s_ro": 18081,
        "telemetry_ro": 18082,
        "source_ro": 18083,
        "chaos_control": 18084,
    }
    SSE_PORTS = {
        "k8s_ro": 18181,
        "telemetry_ro": 18182,
        "source_ro": 18183,
        "chaos_control": 18184,
    }

    def __init__(self, *, private_root: Path, base_environment: Mapping[str, str]):
        self.private_root = private_root.resolve()
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.base_environment = dict(base_environment)
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []

    def start_trial(
        self,
        *,
        trial_id: str,
        harness: HarnessKind,
        token: str,
        token_state_files: Mapping[str, str],
        servers: tuple[McpServerName, ...],
    ) -> dict[str, str]:
        self.stop()
        transport = "sse" if harness is HarnessKind.BLADEAI else "streamable-http"
        ports = self.SSE_PORTS if transport == "sse" else self.HTTP_PORTS
        log_root = self.private_root / trial_id / "mcp-logs"
        log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        urls: dict[str, str] = {}
        selected = tuple(server.value for server in servers)
        try:
            for name in selected:
                port = ports[name]
                token_state_file = token_state_files.get(name)
                if not token_state_file:
                    raise McpSupervisorError(
                        f"MCP token state was not provisioned for server: {name}"
                    )
                if _port_open(port):
                    raise McpSupervisorError(
                        f"MCP loopback port is already in use: {port}"
                    )
                path = "/sse" if transport == "sse" else "/mcp"
                resource = f"http://127.0.0.1:{port}{path}"
                env = {
                    **os.environ,
                    **self.base_environment,
                    "RESBENCH_MCP_TOKEN": token,
                    "RESBENCH_MCP_TOKEN_STATE_FILE": str(token_state_file),
                    "RESBENCH_MCP_TRANSPORT": transport,
                    "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
                    "RESBENCH_MCP_HTTP_PORT": str(port),
                    "RESBENCH_MCP_HTTP_PATH": path,
                    "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
                    "RESBENCH_MCP_RESOURCE_URL": resource,
                    "RESBENCH_MCP_SCOPE": f"stage2:{trial_id}:{name}",
                }
                log = (log_root / f"{name}.log").open("ab")
                self.logs.append(log)
                process = subprocess.Popen(
                    [sys.executable, "-m", f"mcp_servers.{name}"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                self.processes.append(process)
                _wait_process_port(process, port, timeout=30)
                urls[name] = resource
        except Exception:
            self.stop()
            raise

        environment_names = {
            "k8s_ro": (
                "RESBENCH_K8S_MCP_URL",
                "RESBENCH_BLADEAI_K8S_MCP_SSE_URL",
            ),
            "telemetry_ro": (
                "RESBENCH_TELEMETRY_MCP_URL",
                "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL",
            ),
            "source_ro": (
                "RESBENCH_SOURCE_MCP_URL",
                "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL",
            ),
            "chaos_control": (
                "RESBENCH_CHAOS_CONTROL_MCP_URL",
                "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL",
            ),
        }
        return {
            env_name: urls[name]
            for name in selected
            for env_name in environment_names[name]
        }

    def stop(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        for process in reversed(self.processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except OSError:
                    pass
            except OSError:
                pass
        self.processes.clear()
        for log in self.logs:
            try:
                log.close()
            except OSError:
                pass
        self.logs.clear()


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_process_port(process: subprocess.Popen[bytes], port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise McpSupervisorError(f"MCP process exited before port {port} became ready")
        if _port_open(port):
            return
        time.sleep(0.1)
    raise McpSupervisorError(f"MCP port did not become ready: {port}")
