from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from stage2_service.capability_policy import (
    MCP_POLICY_FILE_ENV,
    CapabilityPolicyRegistry,
)
from stage2_service.contracts import HarnessKind, PermissionProfile
from stage2_service.mcp_supervisor import (
    BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS,
    DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS,
    McpSupervisor,
    _chaos_control_runtime_environment,
)
from stage2_service.runtime_adapters import McpTokenStateRegistry


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _policy(tmp_path: Path):
    registry = CapabilityPolicyRegistry(tmp_path / "policy")
    registry.initialize(
        "campaign-1234567890abcdef-codex-d5-1",
        PermissionProfile(
            profile_id="p0-full-authorized",
            mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control"),
        ),
    )
    return registry


def test_start_trial_injects_policy_file_env_into_all_mcp_servers_and_restart_keeps_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    launched: list[dict[str, str]] = []
    monkeypatch.setattr("stage2_service.mcp_supervisor._port_open", lambda _port: False)
    monkeypatch.setattr(
        "stage2_service.mcp_supervisor._wait_process_port",
        lambda _process, _port, timeout: None,
    )

    def fake_popen(*_args, **kwargs):
        launched.append(dict(kwargs["env"]))
        return FakeProcess()

    monkeypatch.setattr("stage2_service.mcp_supervisor.subprocess.Popen", fake_popen)
    policy = _policy(tmp_path)
    token_files = {
        "k8s_ro": str(tmp_path / "k8s.token"),
        "telemetry_ro": str(tmp_path / "telemetry.token"),
        "source_ro": str(tmp_path / "source.token"),
        "chaos_control": str(tmp_path / "chaos.token"),
        "harness_channel": str(tmp_path / "harness.token"),
        "coroot_ro": str(tmp_path / "coroot.token"),
        McpTokenStateRegistry.POLICY_FILE_STATE_KEY: str(policy.policy_path),
    }
    supervisor = McpSupervisor(
        private_root=tmp_path / "mcp",
        base_environment={"RESBENCH_K8S_RO_KUBECONFIG": str(tmp_path / "controller.kubeconfig")},
    )

    supervisor.start_trial(
        trial_id="campaign-1234567890abcdef-codex-d5-1",
        harness=HarnessKind.CODEX,
        token="t" * 48,
        token_state_files=token_files,
        runtime_environment={
            "RESBENCH_AUTHORIZED_RUN_ID": "campaign-1234567890abcdef-codex-d5-1",
            "RESBENCH_PLATFORM_LEDGER_ROOT": str(tmp_path / "ledger"),
            "RESBENCH_HARNESS_CHANNEL_TOKEN": "h" * 48,
        },
    )

    assert len(launched) == 6
    assert {env[MCP_POLICY_FILE_ENV] for env in launched} == {str(policy.policy_path)}
    assert {env["RESBENCH_PLATFORM_LEDGER_ROOT"] for env in launched} == {
        str(tmp_path / "ledger")
    }

    launched.clear()
    supervisor.interrupt(("telemetry_ro",))
    supervisor.restore(("telemetry_ro",))

    assert len(launched) == 1
    assert launched[0][MCP_POLICY_FILE_ENV] == str(policy.policy_path)


def test_bladeai_uses_two_minute_startup_window_but_codex_keeps_thirty_seconds(
    monkeypatch, tmp_path: Path,
) -> None:
    waits: list[int] = []
    monkeypatch.setattr("stage2_service.mcp_supervisor._port_open", lambda _port: False)
    monkeypatch.setattr(
        "stage2_service.mcp_supervisor._wait_process_port",
        lambda _process, _port, timeout: waits.append(timeout),
    )
    monkeypatch.setattr(
        "stage2_service.mcp_supervisor.subprocess.Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    token_files = {
        name: str(tmp_path / f"{name}.token")
        for name in ("k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro")
    }
    supervisor = McpSupervisor(
        private_root=tmp_path / "mcp",
        base_environment={"RESBENCH_K8S_RO_KUBECONFIG": str(tmp_path / "controller.kubeconfig")},
    )
    supervisor.start_trial(
        trial_id="campaign-1234567890abcdef-bladeai-d0-1",
        harness=HarnessKind.BLADEAI,
        token="t" * 48,
        token_state_files=token_files,
        runtime_environment={
            "RESBENCH_HARNESS_CHANNEL_TOKEN": "h" * 48,
            "RESBENCH_BLADEAI_PROXY_TOKEN": "p" * 48,
            "RESBENCH_BLADEAI_PROXY_NAMESPACE": "otel-demo",
            "RESBENCH_BLADEAI_PROXY_KUBECONFIG": str(tmp_path / "proxy.kubeconfig"),
        },
    )
    assert waits == [BLADEAI_MCP_STARTUP_TIMEOUT_SECONDS] * 7

    waits.clear()
    supervisor.stop()
    supervisor.start_trial(
        trial_id="campaign-1234567890abcdef-codex-d0-1",
        harness=HarnessKind.CODEX,
        token="t" * 48,
        token_state_files=token_files,
        runtime_environment={"RESBENCH_HARNESS_CHANNEL_TOKEN": "h" * 48},
    )
    assert waits == [DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS] * 6


def test_chaos_runtime_environment_does_not_infer_d6_variant_from_trial_id() -> None:
    env = _chaos_control_runtime_environment(
        "campaign-1234567890abcdef-codex-d6-b-1",
        {},
    )

    assert env["RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT"] == ""


def test_chaos_runtime_environment_preserves_explicit_d6_variant() -> None:
    env = _chaos_control_runtime_environment(
        "campaign-1234567890abcdef-codex-d6-a-1",
        {"RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT": "D6-A"},
    )

    assert env["RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT"] == "D6-A"


def test_substitution_tokens_start_all_optional_servers_with_private_sandbox_config(monkeypatch, tmp_path: Path) -> None:
    launched: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr("stage2_service.mcp_supervisor._port_open", lambda _port: False)
    monkeypatch.setattr("stage2_service.mcp_supervisor._wait_process_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("stage2_service.mcp_supervisor.subprocess.Popen", lambda args, **kwargs: launched.append((args, dict(kwargs["env"]))) or FakeProcess())
    policy = _policy(tmp_path)
    token_files = {name: str(tmp_path / f"{name}.token") for name in (
        "k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro", "chaos_mesh_control", "code_sandbox")}
    token_files[McpTokenStateRegistry.POLICY_FILE_STATE_KEY] = str(policy.policy_path)
    supervisor = McpSupervisor(private_root=tmp_path / "mcp", base_environment={})
    urls = supervisor.start_trial(trial_id="campaign-1234567890abcdef-codex-d7-1", harness=HarnessKind.CODEX,
        token="t" * 48, token_state_files=token_files, runtime_environment={"RESBENCH_HARNESS_CHANNEL_TOKEN": "h" * 48,
        "RESBENCH_AUTHORIZED_RUN_ID": "campaign-1234567890abcdef-codex-d7-1", "RESBENCH_PLATFORM_LEDGER_ROOT": str(tmp_path / "ledger"),
        "RESBENCH_CODE_SANDBOX_ARTIFACT_ROOT": str(tmp_path / "sandbox")})
    names = {args[-1].removeprefix("mcp_servers.") for args, _env in launched}
    assert names == {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro", "chaos_mesh_control", "code_sandbox"}
    assert "RESBENCH_COROOT_MCP_URL" in urls and "RESBENCH_CHAOS_MESH_CONTROL_MCP_URL" in urls
    mesh_env = next(env for args, env in launched if args[-1].endswith("chaos_mesh_control"))
    assert mesh_env[MCP_POLICY_FILE_ENV] == str(policy.policy_path)


def test_substitution_token_set_without_sandbox_private_config_fails_closed(tmp_path: Path) -> None:
    token_files = {name: str(tmp_path / f"{name}.token") for name in (
        "k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel", "coroot_ro", "chaos_mesh_control", "code_sandbox")}
    with __import__("pytest").raises(Exception, match="private artifact root"):
        McpSupervisor(private_root=tmp_path / "mcp", base_environment={}).start_trial(
            trial_id="campaign-1234567890abcdef-codex-d7-1", harness=HarnessKind.CODEX, token="t" * 48,
            token_state_files=token_files, runtime_environment={"RESBENCH_HARNESS_CHANNEL_TOKEN": "h" * 48},
        )


def test_interrupt_terminates_owned_server_process_group_including_child(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 3
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text())
    supervisor = McpSupervisor(private_root=tmp_path / "mcp", base_environment={})
    supervisor.processes["test-server"] = process

    result = supervisor.interrupt(("test-server",))

    assert result["verified"] is True
    assert process.poll() is not None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("MCP child process survived owned process-group shutdown")
