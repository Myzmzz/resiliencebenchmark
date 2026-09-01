"""Per-Trial D0 chaos_control facade process and private capability issuance."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from mcp_servers.chaos_control.service import new_cleanup_handle

from .common import utc_now, write_json


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class D0ChaosFacade:
    def __init__(
        self,
        *,
        repo_root: Path,
        kubeconfig: Path,
        trial_dir: Path,
        trial_id: str,
        target: Mapping[str, Any],
        environment: Mapping[str, str],
    ):
        self.repo_root = repo_root
        self.kubeconfig = kubeconfig
        self.trial_dir = trial_dir
        self.trial_id = trial_id
        self.target = dict(target)
        self.environment = dict(environment)
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle = None
        self.port = _free_port()
        self.private = trial_dir / ".controller-private"
        self.private.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.private, 0o700)
        self.ledger_dir = self.private / "cleanup-ledger"
        self.baseline_dir = self.private / "baseline-ledger"
        self.ledger_dir.mkdir(mode=0o700)
        self.baseline_dir.mkdir(mode=0o700)
        self.cleanup_handle = new_cleanup_handle()
        self.controller_id = f"d0-controller-{hashlib.sha256(trial_id.encode()).hexdigest()[:12]}"
        self.controller_token_ref = f"runtime://d0/{trial_id}/controller"
        self.baseline_token = secrets.token_urlsafe(32)
        self._issue_capability()

    def _issue_capability(self) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=900)
        write_json(
            self.baseline_dir / f"{hashlib.sha256(self.baseline_token.encode()).hexdigest()}.json",
            {
                "schema_version": "d0-minimal-capability.v1",
                "passed": True,
                "run_id": self.trial_id,
                "namespace": str(self.target["namespace"]),
                "target_name": str(self.target["name"]),
                "target_uid": str(self.target["uid"]),
                "controller_pod_uid": self.controller_id,
                "issued_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "evidence": "minimal Pod/CPU/ChaosBlade before sample; no formal business baseline",
            },
        )
        os.chmod(next(self.baseline_dir.glob("*.json")), 0o600)
        write_json(
            self.private / "controller-lease.json",
            {
                "schema_version": "local-controller-lease.v1",
                "controller_id": self.controller_id,
                "pid": os.getpid(),
                "issued_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            },
        )
        os.chmod(self.private / "controller-lease.json", 0o600)

    def start(self) -> dict[str, str]:
        if self.process is not None:
            raise RuntimeError("D0 facade already started")
        token = self.environment.get("RESBENCH_MCP_TOKEN", "")
        if len(token) < 32 or any(char.isspace() for char in token):
            raise RuntimeError("RESBENCH_MCP_TOKEN is required for the D0 facade")
        url = f"http://127.0.0.1:{self.port}/mcp"
        env = {
            **os.environ,
            **self.environment,
            "PYTHONPATH": str(self.repo_root),
            "RESBENCH_CHAOS_EXECUTE_ENABLED": "true",
            "RESBENCH_CHAOS_KUBECONFIG": str(self.kubeconfig),
            "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": "otel-demo",
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": self.controller_token_ref,
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": self.controller_id,
            "RESBENCH_CHAOS_CONTROLLER_LEASE_FILE": str(self.private / "controller-lease.json"),
            "RESBENCH_CHAOS_LEDGER_DIR": str(self.ledger_dir),
            "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(self.baseline_dir),
            "RESBENCH_D0_RUN_ID": self.trial_id,
            "RESBENCH_D0_CLEANUP_HANDLE": self.cleanup_handle,
            "RESBENCH_D0_CONTROLLER_TOKEN_REF": self.controller_token_ref,
            "RESBENCH_D0_CONTROLLER_UID": self.controller_id,
            "RESBENCH_D0_BASELINE_GATE_TOKEN": self.baseline_token,
            "RESBENCH_D0_CONTROLLER_COMMANDS_PATH": str(
                self.trial_dir / "controller-commands.jsonl"
            ),
            "RESBENCH_MCP_TRANSPORT": "streamable-http",
            "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
            "RESBENCH_MCP_HTTP_PORT": str(self.port),
            "RESBENCH_MCP_HTTP_PATH": "/mcp",
            "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
            "RESBENCH_MCP_RESOURCE_URL": url,
            "RESBENCH_MCP_SCOPE": f"d0:{self.trial_id}:chaos_control",
        }
        self.log_handle = (self.trial_dir / "d0-chaos-facade.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mcp_servers.d0_chaos_control"],
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("D0 chaos facade exited before becoming ready")
            if _port_open(self.port):
                return {
                    "RESBENCH_CHAOS_CONTROL_MCP_URL": url,
                    "RESBENCH_D0_CLEANUP_HANDLE": self.cleanup_handle,
                }
            time.sleep(0.1)
        raise RuntimeError("D0 chaos facade did not become ready")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
        shutil.rmtree(self.private, ignore_errors=True)

    def public_context(self) -> dict[str, Any]:
        return {
            "started_at": utc_now(),
            "url": f"http://127.0.0.1:{self.port}/mcp",
            "cleanup_handle_ref": "<trial-private>",
            "target_scope": "otel-demo/accounting/one-pod",
        }


class D0ReadOnlyMcpStack:
    """Campaign-scoped read-only MCP servers for native Headless Harnesses."""

    PORTS = {"k8s_ro": 18081, "telemetry_ro": 18082, "source_ro": 18083}

    def __init__(
        self,
        *,
        repo_root: Path,
        kubeconfig: Path,
        campaign_dir: Path,
        environment: Mapping[str, str],
    ):
        self.repo_root = repo_root
        self.kubeconfig = kubeconfig
        self.campaign_dir = campaign_dir
        self.environment = dict(environment)
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []

    def start(self) -> dict[str, str]:
        token = self.environment.get("RESBENCH_MCP_TOKEN", "")
        if len(token) < 32 or any(char.isspace() for char in token):
            raise RuntimeError("RESBENCH_MCP_TOKEN is required for D0 read-only MCP")
        log_root = self.campaign_dir / "d0-readonly-mcp"
        log_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        source_root = self.environment.get(
            "STAGE2_SOURCE_ROOT", "/opt/resiliencebenchmark/sources"
        )
        common = {
            **os.environ,
            **self.environment,
            "PYTHONPATH": str(self.repo_root),
            "RESBENCH_MCP_TOKEN": token,
            "RESBENCH_MCP_TRANSPORT": "streamable-http",
            "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
            "RESBENCH_MCP_HTTP_PATH": "/mcp",
            "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
            "RESBENCH_MCP_SCOPE": "d0:readonly",
        }
        per_server = {
            "k8s_ro": {
                "RESBENCH_K8S_RO_KUBECONFIG": str(self.kubeconfig),
                "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST": "otel-demo",
            },
            "telemetry_ro": {
                "RESBENCH_PROMETHEUS_URL": "http://prometheus.observability.svc:9090",
                "RESBENCH_JAEGER_URL": "http://jaeger-query.observability.svc:16686",
                "RESBENCH_LOKI_URL": "http://loki.observability.svc:3100",
                "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": "otel-demo",
                "RESBENCH_JAEGER_ALLOWED_SERVICES": (
                    "accounting,frontend,frontend-proxy,checkout,cart,payment,shipping"
                ),
                "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES": "false",
                "RESBENCH_TELEMETRY_DISTURBANCE_DIR": str(
                    self.campaign_dir / ".telemetry"
                ),
            },
            "source_ro": {
                "RESBENCH_SOURCE_ROOT": source_root,
                "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": "otel-demo",
            },
        }
        urls = {}
        try:
            for name, port in self.PORTS.items():
                if _port_open(port):
                    raise RuntimeError(f"D0 read-only MCP port is in use: {port}")
                resource = f"http://127.0.0.1:{port}/mcp"
                log = (log_root / f"{name}.log").open("ab")
                env = {
                    **common,
                    **per_server[name],
                    "RESBENCH_MCP_HTTP_PORT": str(port),
                    "RESBENCH_MCP_RESOURCE_URL": resource,
                }
                process = subprocess.Popen(
                    [sys.executable, "-m", f"mcp_servers.{name}"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                self.logs.append(log)
                self.processes.append(process)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"D0 read-only MCP exited: {name}")
                    if _port_open(port):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(f"D0 read-only MCP did not become ready: {name}")
                urls[name] = resource
        except Exception:
            self.stop()
            raise
        return {
            "RESBENCH_K8S_MCP_URL": urls["k8s_ro"],
            "RESBENCH_TELEMETRY_MCP_URL": urls["telemetry_ro"],
            "RESBENCH_SOURCE_MCP_URL": urls["source_ro"],
        }

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.processes.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()


class BladeAIServerProcess:
    """Start the native BladeAI API only when the configured endpoint is absent."""

    def __init__(self, *, base_url: str, artifact_root: Path, environment: Mapping[str, str]):
        self.base_url = base_url.rstrip("/")
        self.artifact_root = artifact_root
        self.environment = dict(environment)
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle = None
        self.owned = False
        self.runtime_root: Path | None = None

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/v1/health", timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def start(self) -> None:
        if self._healthy():
            return
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise RuntimeError("D0 may auto-start BladeAI only on a loopback endpoint")
        command = self.environment.get("RESBENCH_D0_BLADEAI_COMMAND", "blade-ai")
        resolved = shutil.which(command) if not Path(command).is_absolute() else command
        if not resolved:
            raise RuntimeError("BladeAI command is unavailable")
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root = self.artifact_root / ".bladeai-runtime"
        self.runtime_root.mkdir(mode=0o700, exist_ok=False)
        self.log_handle = (self.artifact_root / "bladeai-server.log").open("ab")
        self.process = subprocess.Popen(
            [
                str(resolved),
                "__embedded_server__",
                "--host",
                "127.0.0.1",
                "--port",
                str(parsed.port),
                "--ready-stdout",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            env={**os.environ, **self.environment},
            cwd=self.runtime_root,
        )
        self.owned = True
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("BladeAI server exited before health became ready")
            if self._healthy():
                return
            time.sleep(0.25)
        raise RuntimeError("BladeAI server did not become healthy")

    def stop(self) -> None:
        if self.owned and self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
        if self.runtime_root is not None:
            shutil.rmtree(self.runtime_root, ignore_errors=True)
            self.runtime_root = None
