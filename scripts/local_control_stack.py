#!/usr/bin/env python3
"""Run a local authenticated MCP stack against the explicit test kubeconfig."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from controller.scoped_kubeconfig import create_scoped_kubeconfig
from scripts.qualify_mcp_endpoints import run_qualification
from scripts.run_harness_trial import run_trial
from scripts.smoke_mcp_data_path import run_smoke

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
ENDPOINTS = {
    "RESBENCH_K8S_MCP_URL": "http://127.0.0.1:18081/mcp",
    "RESBENCH_TELEMETRY_MCP_URL": "http://127.0.0.1:18082/mcp",
    "RESBENCH_SOURCE_MCP_URL": "http://127.0.0.1:18083/mcp",
    "RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:18084/mcp",
}
PORTS = (19090, 19086, 13100, 18081, 18082, 18083, 18084)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=WORKSPACE_ROOT / "benchmark-sources/materialized",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=REPO_ROOT / "runs/local-control-stack",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--qualify-and-exit", action="store_true")
    parser.add_argument("--smoke-data-path", action="store_true")
    parser.add_argument("--codex-auth-file", type=Path)
    parser.add_argument("--codex-smoke", action="store_true")
    parser.add_argument("--codex-model", default="gpt-5.6")
    parser.add_argument("--enable-chaos-writes", action="store_true")
    return parser


def build_stack_environment(
    *,
    k8s_kubeconfig: Path,
    chaos_kubeconfig: Path,
    source_root: Path,
    runtime_root: Path,
    token: str,
    enable_chaos_writes: bool = False,
) -> dict[str, dict[str, str]]:
    common = {
        "RESBENCH_MCP_TOKEN": token,
        "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
        "RESBENCH_MCP_SCOPE": "resbench:episode",
        "RESBENCH_MCP_TRANSPORT": "streamable-http",
        "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
        "RESBENCH_MCP_HTTP_PATH": "/mcp",
    }
    telemetry_dir = runtime_root / "telemetry-disturbance"
    active_ledger = runtime_root / "chaos-control/active"
    baseline_ledger = runtime_root / "chaos-control/baseline"
    controller_lease = runtime_root / "private/controller-lease.json"
    controller_id = "local-controller-supervisor"
    return {
        "k8s_ro": {
            **common,
            "RESBENCH_MCP_HTTP_PORT": "18081",
            "RESBENCH_MCP_RESOURCE_URL": ENDPOINTS["RESBENCH_K8S_MCP_URL"],
            "RESBENCH_K8S_RO_KUBECONFIG": str(k8s_kubeconfig),
            "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST": "otel-demo",
        },
        "telemetry_ro": {
            **common,
            "RESBENCH_MCP_HTTP_PORT": "18082",
            "RESBENCH_MCP_RESOURCE_URL": ENDPOINTS["RESBENCH_TELEMETRY_MCP_URL"],
            "RESBENCH_PROMETHEUS_URL": "http://127.0.0.1:19090",
            "RESBENCH_JAEGER_URL": "http://127.0.0.1:19086",
            "RESBENCH_LOKI_URL": "http://127.0.0.1:13100",
            "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": "otel-demo",
            "RESBENCH_JAEGER_ALLOWED_SERVICES": (
                "frontend,frontend-proxy,checkout,cart,payment,shipping"
            ),
            "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES": "false",
            "RESBENCH_TELEMETRY_DISTURBANCE_DIR": str(telemetry_dir),
        },
        "source_ro": {
            **common,
            "RESBENCH_MCP_HTTP_PORT": "18083",
            "RESBENCH_MCP_RESOURCE_URL": ENDPOINTS["RESBENCH_SOURCE_MCP_URL"],
            "RESBENCH_SOURCE_ROOT": str(source_root),
            "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": "otel-demo",
        },
        "chaos_control": {
            **common,
            "RESBENCH_MCP_HTTP_PORT": "18084",
            "RESBENCH_MCP_RESOURCE_URL": ENDPOINTS[
                "RESBENCH_CHAOS_CONTROL_MCP_URL"
            ],
            "RESBENCH_CHAOS_EXECUTE_ENABLED": (
                "true" if enable_chaos_writes else "false"
            ),
            "RESBENCH_CHAOS_KUBECONFIG": str(chaos_kubeconfig),
            "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": "otel-demo",
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "runtime://local-controller/token",
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": controller_id,
            "RESBENCH_CHAOS_CONTROLLER_LEASE_FILE": str(controller_lease),
            "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(baseline_ledger),
            "RESBENCH_CHAOS_LEDGER_DIR": str(active_ledger),
        },
    }


def redacted_plan(
    environments: Mapping[str, Mapping[str, str]],
    runtime_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "local-control-stack-plan.v1",
        "chaos_writes_enabled": (
            environments["chaos_control"]["RESBENCH_CHAOS_EXECUTE_ENABLED"] == "true"
        ),
        "runtime_root": str(runtime_root),
        "token": {"generated": True, "persisted_private": True},
        "port_forwards": [
            {"service": "prometheus", "local_port": 19090, "remote_port": 9090},
            {"service": "jaeger-query", "local_port": 19086, "remote_port": 16686},
            {"service": "loki", "local_port": 13100, "remote_port": 3100},
        ],
        "mcp_servers": [
            {
                "name": name,
                "url": ENDPOINTS[f"RESBENCH_{'K8S' if name == 'k8s_ro' else 'TELEMETRY' if name == 'telemetry_ro' else 'SOURCE' if name == 'source_ro' else 'CHAOS_CONTROL'}_MCP_URL"],
                "runtime_keys": sorted(key for key in env if key != "RESBENCH_MCP_TOKEN"),
            }
            for name, env in environments.items()
        ],
    }


class LocalStack:
    def __init__(
        self,
        *,
        port_forward_kubeconfig: Path,
        controller_kubeconfig: Path,
        codex_auth_file: Path | None,
        source_root: Path,
        runtime_root: Path,
        environments: dict[str, dict[str, str]],
    ):
        self.port_forward_kubeconfig = port_forward_kubeconfig
        self.controller_kubeconfig = controller_kubeconfig
        self.codex_auth_file = codex_auth_file
        self.source_root = source_root
        self.runtime_root = runtime_root
        self.environments = environments
        self.processes: list[subprocess.Popen[bytes]] = []
        self.log_handles: list[Any] = []
        self.lease_stop = threading.Event()
        self.lease_thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        try:
            self._start()
        except Exception:
            self._stop()
            raise
        return self

    def _start(self) -> None:
        for port in PORTS:
            if _port_open(port):
                raise RuntimeError(f"local stack port is already in use: {port}")
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_root, 0o700)
        for relative in (
            "logs",
            "telemetry-disturbance/rules",
            "telemetry-disturbance/events",
            "chaos-control/active",
            "chaos-control/baseline",
        ):
            path = self.runtime_root / relative
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        lease_raw = self.environments["chaos_control"].get(
            "RESBENCH_CHAOS_CONTROLLER_LEASE_FILE"
        )
        if (
            self.environments["chaos_control"]["RESBENCH_CHAOS_EXECUTE_ENABLED"]
            == "true"
        ):
            if not lease_raw:
                raise RuntimeError("write-enabled chaos service requires a Controller lease")
            lease_path = Path(lease_raw)
            self._renew_controller_lease(lease_path)

            def renew_lease() -> None:
                while not self.lease_stop.wait(30):
                    self._renew_controller_lease(lease_path)

            self.lease_thread = threading.Thread(target=renew_lease, daemon=True)
            self.lease_thread.start()
        forwards = (
            ("prometheus", 19090, 9090),
            ("jaeger-query", 19086, 16686),
            ("loki", 13100, 3100),
        )
        for service, local_port, remote_port in forwards:
            self._spawn(
                f"port-forward-{service}",
                [
                    "kubectl",
                    "--kubeconfig",
                    str(self.port_forward_kubeconfig),
                    "port-forward",
                    "-n",
                    "observability",
                    f"service/{service}",
                    f"{local_port}:{remote_port}",
                    "--address",
                    "127.0.0.1",
                ],
                os.environ,
            )
        for name, port in (
            ("k8s_ro", 18081),
            ("telemetry_ro", 18082),
            ("source_ro", 18083),
            ("chaos_control", 18084),
        ):
            env = {**os.environ, **self.environments[name]}
            self._spawn(name, [sys.executable, "-m", f"mcp_servers.{name}"], env)
        for port in PORTS:
            _wait_port(port, timeout_seconds=30)
        self._write_private_env()

    def __exit__(self, *_args: object) -> None:
        self._stop()

    def _stop(self) -> None:
        self.lease_stop.set()
        if self.lease_thread is not None:
            self.lease_thread.join(timeout=2)
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        for process in reversed(self.processes):
            remaining = max(0.1, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for handle in self.log_handles:
            handle.close()

    def _renew_controller_lease(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        payload = {
            "schema_version": "local-controller-lease.v1",
            "controller_id": self.environments["chaos_control"][
                "RESBENCH_CHAOS_CONTROLLER_POD_UID"
            ],
            "pid": os.getpid(),
            "issued_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
        }
        descriptor, raw_path = tempfile.mkstemp(prefix=".controller-lease-", dir=path.parent)
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _spawn(
        self,
        name: str,
        argv: list[str],
        env: Mapping[str, str],
    ) -> None:
        log = (self.runtime_root / "logs" / f"{name}.log").open("ab")
        self.log_handles.append(log)
        process = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(process)

    def _write_private_env(self) -> None:
        common = self.environments["k8s_ro"]
        values = {
            "RESBENCH_MCP_TOKEN": common["RESBENCH_MCP_TOKEN"],
            **ENDPOINTS,
            "RESBENCH_KUBECONFIG": str(self.controller_kubeconfig),
            "RESBENCH_TELEMETRY_DISTURBANCE_DIR": str(
                self.runtime_root / "telemetry-disturbance"
            ),
            "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(
                self.runtime_root / "chaos-control/baseline"
            ),
            "RESBENCH_PRIVATE_RUNTIME_ROOT": str(self.runtime_root / "private"),
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": self.environments[
                "chaos_control"
            ]["RESBENCH_CHAOS_CONTROLLER_TOKEN_REF"],
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": self.environments[
                "chaos_control"
            ]["RESBENCH_CHAOS_CONTROLLER_POD_UID"],
            "RESBENCH_CHAOS_CONTROLLER_LEASE_FILE": self.environments[
                "chaos_control"
            ]["RESBENCH_CHAOS_CONTROLLER_LEASE_FILE"],
        }
        if self.codex_auth_file is not None:
            values["RESBENCH_CODEX_AUTH_FILE"] = str(self.codex_auth_file)
        path = self.runtime_root / "stack.env"
        path.write_text(
            "".join(
                f"{key}={json.dumps(value, ensure_ascii=False)}\n"
                for key, value in sorted(values.items())
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_open(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"local stack port did not become ready: {port}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kubeconfig = args.kubeconfig.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    codex_auth_file = (
        args.codex_auth_file.expanduser().resolve() if args.codex_auth_file else None
    )
    if not kubeconfig.is_file():
        print(json.dumps({"status": "blocked", "message": "kubeconfig is missing"}))
        return 2
    if not source_root.is_dir():
        print(json.dumps({"status": "blocked", "message": "source root is missing"}))
        return 2
    if codex_auth_file is not None and (
        not codex_auth_file.is_file()
        or codex_auth_file.is_symlink()
        or codex_auth_file.stat().st_mode & 0o077
    ):
        print(json.dumps({"status": "blocked", "message": "Codex auth file is not private"}))
        return 2
    token = secrets.token_urlsafe(32)
    kubeconfig_root = runtime_root / "kubeconfigs"
    k8s_kubeconfig = kubeconfig_root / "k8s-ro.kubeconfig"
    chaos_kubeconfig = kubeconfig_root / "chaos-control.kubeconfig"
    controller_kubeconfig = kubeconfig_root / "controller.kubeconfig"
    environments = build_stack_environment(
        k8s_kubeconfig=k8s_kubeconfig,
        chaos_kubeconfig=chaos_kubeconfig,
        source_root=source_root,
        runtime_root=runtime_root,
        token=token,
        enable_chaos_writes=args.enable_chaos_writes,
    )
    plan = redacted_plan(environments, runtime_root)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    create_scoped_kubeconfig(
        admin_kubeconfig=kubeconfig,
        service_account="resbench-k8s-ro-otel-demo",
        output_path=k8s_kubeconfig,
    )
    create_scoped_kubeconfig(
        admin_kubeconfig=kubeconfig,
        service_account="resbench-chaos-control-otel-demo",
        output_path=chaos_kubeconfig,
    )
    create_scoped_kubeconfig(
        admin_kubeconfig=kubeconfig,
        service_account="resbench-controller-otel-demo",
        output_path=controller_kubeconfig,
    )
    try:
        with LocalStack(
            port_forward_kubeconfig=controller_kubeconfig,
            controller_kubeconfig=controller_kubeconfig,
            codex_auth_file=codex_auth_file,
            source_root=source_root,
            runtime_root=runtime_root,
            environments=environments,
        ):
            qualification_env = {"RESBENCH_MCP_TOKEN": token, **ENDPOINTS}
            report = run_qualification(
                qualification_env,
                execute=True,
                timeout=30,
            )
            report_path = runtime_root / "qualification.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(report_path, 0o600)
            smoke_report = None
            if args.smoke_data_path and report["status"] == "qualified":
                smoke_report = run_smoke(
                    qualification_env,
                    disturbance_dir=runtime_root / "telemetry-disturbance",
                )
                smoke_path = runtime_root / "data-path-smoke.json"
                smoke_path.write_text(
                    json.dumps(smoke_report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.chmod(smoke_path, 0o600)
            codex_report = None
            if args.codex_smoke and report["status"] == "qualified":
                if codex_auth_file is None:
                    raise RuntimeError("--codex-smoke requires --codex-auth-file")
                codex_report = run_trial(
                    REPO_ROOT,
                    "codex",
                    args.codex_model,
                    prompt_ref="connectivity_smoke",
                    execute=True,
                    artifact_root=runtime_root / "codex-smoke",
                    timeout_seconds=300,
                    parent_env={
                        **qualification_env,
                        "RESBENCH_CODEX_AUTH_FILE": str(codex_auth_file),
                    },
                    trial_id=f"local-codex-connectivity-{int(time.time())}",
                )
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "qualification_ref": str(report_path),
                        "stack_env_ref": str(runtime_root / "stack.env"),
                        "chaos_writes_enabled": (
                            environments["chaos_control"][
                                "RESBENCH_CHAOS_EXECUTE_ENABLED"
                            ]
                            == "true"
                        ),
                        "data_path_status": (
                            smoke_report["status"] if smoke_report is not None else "not_requested"
                        ),
                        "codex_status": (
                            codex_report["status"]
                            if codex_report is not None
                            else "not_requested"
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.qualify_and_exit:
                return 0 if (
                    report["status"] == "qualified"
                    and (smoke_report is None or smoke_report["status"] == "qualified")
                    and (codex_report is None or codex_report["status"] == "completed")
                ) else 2
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "message": str(exc)[:500]},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
