"""Run all MCP modules as loopback child processes of the single Stage-2 service."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from .capability_policy import (
    MCP_POLICY_FILE_ENV,
    CapabilityPolicyError,
    read_policy_file,
)
from .contracts import HarnessKind
from .runtime_adapters import McpTokenStateRegistry


class McpSupervisorError(RuntimeError):
    pass


# MCP Python services normally bind quickly.  BladeAI's SSE worker starts
# several clients under the same old-cluster I/O budget, so give only that
# transport a wider port-readiness window; Codex/Claude/DeepSeek retain the
# existing 30-second startup contract.
DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS = 30
BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS = 120


class McpSupervisor:
    HTTP_PORTS = {
        "k8s_ro": 18081,
        "telemetry_ro": 18082,
        "source_ro": 18083,
        "chaos_control": 18084,
        "harness_channel": 18085,
        "coroot_ro": 18086,
        "chaos_mesh_control": 18087,
        "code_sandbox": 18088,
    }
    SSE_PORTS = {
        "k8s_ro": 18181,
        "telemetry_ro": 18182,
        "source_ro": 18183,
        "chaos_control": 18184,
        "harness_channel": 18185,
        "coroot_ro": 18186,
        "chaos_mesh_control": 18187,
        "code_sandbox": 18188,
    }

    def __init__(self, *, private_root: Path, base_environment: Mapping[str, str]):
        self.private_root = private_root.resolve()
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.base_environment = dict(base_environment)
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: dict[str, Any] = {}
        self.specs: dict[str, tuple[int, dict[str, str], Path]] = {}

    def start_trial(
        self,
        *,
        trial_id: str,
        harness: HarnessKind,
        token: str,
        token_state_files: Mapping[str, str],
        runtime_environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        self.stop()
        transport = "sse" if harness is HarnessKind.BLADEAI else "streamable-http"
        ports = self.SSE_PORTS if transport == "sse" else self.HTTP_PORTS
        log_root = self.private_root / trial_id / "mcp-logs"
        log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        urls: dict[str, str] = {}
        policy_file = _policy_file_from_token_state_files(token_state_files)
        base_names = ("k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel")
        missing_base = [name for name in base_names if name not in token_state_files]
        if missing_base:
            raise McpSupervisorError("required MCP token state is missing: " + ", ".join(missing_base))
        optional_names = ("coroot_ro", "chaos_mesh_control", "code_sandbox")
        optional_present = [name for name in optional_names if name in token_state_files]
        if optional_present and len(optional_present) != len(optional_names):
            raise McpSupervisorError("optional substitution MCP token state must be all-or-none")
        server_names = list(base_names)
        if len(optional_present) == len(optional_names):
            if not (runtime_environment or {}).get("RESBENCH_CODE_SANDBOX_ARTIFACT_ROOT"):
                raise McpSupervisorError(
                    "substitution Trial provisioned code_sandbox but its private artifact root is missing"
                )
            server_names.extend(optional_names)
        for name in server_names:
            port = ports[name]
            runtime_env = dict(runtime_environment or {})
            server_token = runtime_env.get("RESBENCH_HARNESS_CHANNEL_TOKEN") if name == "harness_channel" else token
            if not server_token:
                raise McpSupervisorError(f"independent MCP token missing: {name}")
            if _port_open(port):
                raise McpSupervisorError(f"MCP loopback port is already in use: {port}")
            path = "/sse" if transport == "sse" else "/mcp"
            resource = f"http://127.0.0.1:{port}{path}"
            env = {
                **os.environ,
                **self.base_environment,
                **_shared_runtime_environment(runtime_environment),
                **(
                    _chaos_control_runtime_environment(trial_id, runtime_environment)
                    if name in {"chaos_control", "chaos_mesh_control"}
                    else {}
                ),
                **({key: value for key, value in runtime_env.items()
                    if key.startswith("RESBENCH_CODE_SANDBOX_") or key.startswith("RESBENCH_AGENT_EXEC_")}
                   if name == "code_sandbox" else {}),
                **({key: value for key, value in runtime_env.items() if key in {
                    "RESBENCH_HARNESS_CHANNEL_CONTEXT_FILE", "RESBENCH_HARNESS_CHANNEL_ROOT",
                    "RESBENCH_HARNESS_TRIAL_ID", "RESBENCH_USER_DECISION_FILE",
                    "RESBENCH_LLM_BASE_URL", "RESBENCH_LLM_API_KEY",
                }} if name == "harness_channel" else {}),
                "RESBENCH_MCP_TOKEN": server_token,
                "RESBENCH_MCP_TOKEN_STATE_FILE": str(token_state_files[name]),
                "RESBENCH_MCP_TRANSPORT": transport,
                "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
                "RESBENCH_MCP_HTTP_PORT": str(port),
                "RESBENCH_MCP_HTTP_PATH": path,
                "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
                "RESBENCH_MCP_RESOURCE_URL": resource,
                "RESBENCH_MCP_SCOPE": f"stage2:{trial_id}:{name}",
            }
            if policy_file is not None:
                env[MCP_POLICY_FILE_ENV] = policy_file
            self.specs[name] = (port, env, log_root / f"{name}.log")
            self._start_server(
                name,
                startup_timeout=(
                    BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS
                    if harness is HarnessKind.BLADEAI
                    else DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
                ),
            )
            urls[name] = resource
        if harness is HarnessKind.BLADEAI:
            runtime_env = dict(runtime_environment or {})
            for key in ("RESBENCH_BLADEAI_PROXY_TOKEN", "RESBENCH_BLADEAI_PROXY_NAMESPACE"):
                if not runtime_env.get(key):
                    raise McpSupervisorError(f"BladeAI loopback proxy configuration is missing: {key}")
            kubeconfig = self.base_environment.get("RESBENCH_K8S_RO_KUBECONFIG")
            if not kubeconfig:
                raise McpSupervisorError("BladeAI proxy requires the Controller's K8s read configuration")
            proxy_env = {
                **os.environ, **self.base_environment, **_shared_runtime_environment(runtime_environment),
                **{key: value for key, value in runtime_env.items() if key.startswith("RESBENCH_BLADEAI_PROXY_")},
                "RESBENCH_BLADEAI_PROXY_KUBECONFIG": kubeconfig,
                "RESBENCH_MCP_TOKEN_STATE_FILE": str(token_state_files["k8s_ro"]),
                "RESBENCH_MCP_TOKEN": token,
            }
            if policy_file is not None:
                proxy_env[MCP_POLICY_FILE_ENV] = policy_file
            proxy_port = int(runtime_env.get("RESBENCH_BLADEAI_PROXY_PORT", "18481"))
            self.specs["bladeai_k8s_proxy"] = (proxy_port, proxy_env, log_root / "bladeai_k8s_proxy.log")
            self._start_server(
                "bladeai_k8s_proxy",
                startup_timeout=BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS,
            )
        return {
            "RESBENCH_K8S_MCP_URL": urls["k8s_ro"],
            "RESBENCH_TELEMETRY_MCP_URL": urls["telemetry_ro"],
            "RESBENCH_SOURCE_MCP_URL": urls["source_ro"],
            "RESBENCH_CHAOS_CONTROL_MCP_URL": urls["chaos_control"],
            "RESBENCH_HARNESS_CHANNEL_MCP_URL": urls["harness_channel"],
            "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": urls["harness_channel"],
            "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": urls["k8s_ro"],
            "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": urls["telemetry_ro"],
            "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": urls["source_ro"],
            "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": urls["chaos_control"],
            **({"RESBENCH_COROOT_MCP_URL": urls["coroot_ro"],
                 "RESBENCH_BLADEAI_COROOT_MCP_SSE_URL": urls["coroot_ro"]} if "coroot_ro" in urls else {}),
            **({"RESBENCH_CHAOS_MESH_CONTROL_MCP_URL": urls["chaos_mesh_control"],
                 "RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL": urls["chaos_mesh_control"]} if "chaos_mesh_control" in urls else {}),
            **({"RESBENCH_CODE_SANDBOX_MCP_URL": urls["code_sandbox"],
                 "RESBENCH_BLADEAI_CODE_SANDBOX_MCP_SSE_URL": urls["code_sandbox"]} if "code_sandbox" in urls else {}),
        }

    def stop(self) -> None:
        self.interrupt(tuple(self.processes))
        self.specs.clear()

    def interrupt(self, names: tuple[str, ...]) -> dict[str, Any]:
        stopped = []
        for name in names:
            process = self.processes.get(name)
            if process is not None and process.poll() is None:
                _signal_known_process_group(process, signal.SIGTERM)
        for name in names:
            process = self.processes.pop(name, None)
            if process is None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_known_process_group(process, signal.SIGKILL)
                process.wait(timeout=5)
            _verify_known_process_group_exited(process)
            log = self.logs.pop(name, None)
            if log is not None:
                log.close()
            stopped.append(name)
        return {
            "interrupted": sorted(stopped),
            "verified": all(name not in self.processes for name in names),
        }

    def restore(self, names: tuple[str, ...]) -> dict[str, Any]:
        restored = []
        for name in names:
            if name in self.processes:
                continue
            if name not in self.specs:
                raise McpSupervisorError(f"MCP server has no restart specification: {name}")
            _port, env, _log_path = self.specs[name]
            self._start_server(
                name,
                startup_timeout=(
                    BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS
                    if env.get("RESBENCH_MCP_TRANSPORT") == "sse"
                    or name == "bladeai_k8s_proxy"
                    else DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
                ),
            )
            restored.append(name)
        return {
            "restored": sorted(restored),
            "verified": all(name in self.processes for name in names),
        }

    def operation_uncertainty_status(self, trial_id: str) -> Mapping[str, Any]:
        if "chaos_control" not in self.specs:
            raise McpSupervisorError("chaos_control MCP server has no active Trial specification")
        _port, env, _log_path = self.specs["chaos_control"]
        if env.get("RESBENCH_AUTHORIZED_RUN_ID") != trial_id:
            raise McpSupervisorError("chaos_control Trial identity does not match the requested operation status")
        cleanup_handle = env.get("RESBENCH_CLEANUP_HANDLE")
        if not cleanup_handle:
            raise McpSupervisorError("chaos_control cleanup handle is missing from the Trial runtime")
        from mcp_servers.chaos_control.service import ChaosControlService, RuntimeConfig

        service = ChaosControlService(RuntimeConfig.from_env(_env_with_policy_d6_variant(env)))
        deadline = time.monotonic() + 15
        latest: Mapping[str, Any] = {}
        while time.monotonic() < deadline:
            latest = asyncio.run(
                service.operation_status(
                    operation_id=cleanup_handle,
                    cleanup_handle=cleanup_handle,
                    include_ground_truth=True,
                )
            )
            if latest.get("operation_outcome") in {"absent", "applied"}:
                return latest
            time.sleep(0.25)
        return latest

    def _start_server(self, name: str, *, startup_timeout: int = DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS) -> None:
        port, env, log_path = self.specs[name]
        if _port_open(port):
            raise McpSupervisorError(f"MCP loopback port is already in use: {port}")
        log = log_path.open("ab")
        process = subprocess.Popen(
            [sys.executable, "-m", f"mcp_servers.{name}"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        self.logs[name] = log
        self.processes[name] = process
        try:
            _wait_process_port(process, port, timeout=startup_timeout)
        except Exception:
            self.processes.pop(name, None)
            self.logs.pop(name, None)
            if process.poll() is None:
                _signal_known_process_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _signal_known_process_group(process, signal.SIGKILL)
                    process.wait(timeout=5)
                _verify_known_process_group_exited(process)
            log.close()
            raise


def _signal_known_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal only a child session led by this exact known server PID."""
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        # Test doubles have no OS identity; production Popen instances always do.
        process.terminate() if sig == signal.SIGTERM else process.kill()
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != pid:
        raise McpSupervisorError("refusing to signal an MCP process outside its owned session")
    os.killpg(pgid, sig)


def _verify_known_process_group_exited(process: subprocess.Popen[bytes]) -> None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid == pid:
        raise McpSupervisorError("MCP process group still exists after bounded shutdown")


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


def _chaos_control_runtime_environment(
    trial_id: str,
    runtime_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    del trial_id
    env = dict(runtime_environment or {})
    if "RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT" not in env:
        env["RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT"] = ""
    return env


def _shared_runtime_environment(
    runtime_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    env = dict(runtime_environment or {})
    return {
        key: value
        for key, value in env.items()
        if key in {"RESBENCH_PLATFORM_LEDGER_ROOT", "RESBENCH_AUTHORIZED_RUN_ID",
                   "RESBENCH_MCP_AUDIT_SOCKET", "RESBENCH_MCP_AUDIT_AUTHORITY",
                   "RESBENCH_MCP_AUDIT_TIMEOUT_SECONDS"}
    }


def _policy_file_from_token_state_files(
    token_state_files: Mapping[str, str],
) -> str | None:
    raw = token_state_files.get(McpTokenStateRegistry.POLICY_FILE_STATE_KEY)
    if raw is None or not str(raw).strip():
        return None
    return str(raw)


def _env_with_policy_d6_variant(env: Mapping[str, str]) -> dict[str, str]:
    updated = dict(env)
    if updated.get("RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT"):
        return updated
    policy_file = updated.get(MCP_POLICY_FILE_ENV)
    if not policy_file:
        return updated
    try:
        document = read_policy_file(Path(policy_file))
        policy = document.server_policy("chaos_control")
    except CapabilityPolicyError:
        return updated
    if policy is not None and policy.chaos_create_uncertainty_variant is not None:
        updated["RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT"] = (
            policy.chaos_create_uncertainty_variant.value
        )
    return updated
