"""Local contract tests for the agent-exec backed code sandbox.

These tests use a fake agent-exec client on macOS.  They prove request and
broker wiring only; Linux namespace/cgroup isolation remains a deployment
qualification requirement.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from harness.agent_exec.client import ExecutionResult
from harness.agent_exec.sandbox import (
    AGENT_EXEC_SANDBOX_PYTHON,
    AgentExecSandboxConfig,
    AgentExecSandboxExecutor,
)
from mcp_servers.code_sandbox.executor import (
    ControlledSandboxConfig,
    ControlledSandboxExecutor,
)
from mcp_servers.code_sandbox.service import CodeSandboxConfig, CodeSandboxError, CodeSandboxService
from stage2_service.platform_ledger import PlatformLedger


class FakeAgentExecClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = []

    def run(self, argv, stdin, env, timeout_seconds, **kwargs):
        self.calls.append(
            {
                "argv": tuple(argv),
                "stdin": bytes(stdin),
                "env": dict(env),
                "timeout_seconds": timeout_seconds,
                **kwargs,
            }
        )
        return ExecutionResult(
            returncode=0,
            stdout=b'{"answer": 42}\n',
            stderr=b"",
            output_truncated=False,
        )


class FakeBroker:
    instances = []

    def __init__(self, config, *, invoker, ledger):
        self.config = config
        self.invoker = invoker
        self.ledger = ledger
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _controlled(tmp_path: Path):
    trial_id = "campaign-1234567890abcdef-codex-d7-a-1"
    ledger = PlatformLedger(tmp_path / "ledger")
    client = FakeAgentExecClient()
    config = ControlledSandboxConfig(
        trial_id=trial_id,
        guest_uid=10003,
        guest_gid=10003,
        # The production layout also uses a short controller-generated cwd.
        # pytest's macOS temp root alone can exceed sockaddr_un's budget.
        broker_root=Path("/tmp") / f"rb-{os.getpid()}" / ".sandbox-tmp",
        agent_exec_cwd="s/1234abcd",
        allowed_tools=frozenset({"coroot_ro.coroot_metrics_range"}),
        endpoints={"coroot_ro": "http://127.0.0.1:18086/mcp"},
        token="t" * 40,
    )
    executor = ControlledSandboxExecutor(
        config,
        agent_exec=AgentExecSandboxExecutor(
            client,
            config=AgentExecSandboxConfig(cwd=config.agent_exec_cwd),
        ),
        ledger=ledger,
        invoker=lambda tool, args: {"tool": tool, "args": dict(args)},
        broker_factory=FakeBroker,
    )
    return executor, client, ledger, trial_id


def test_controlled_executor_uses_agent_exec_sandbox_without_guest_credentials(tmp_path: Path):
    FakeBroker.instances.clear()
    executor, client, _ledger, _trial_id = _controlled(tmp_path)

    result = executor.run("print(mcp_call('coroot_ro.coroot_metrics_range', {'metric': 'latency'}))", 5)

    assert result.exit_code == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["argv"] == (AGENT_EXEC_SANDBOX_PYTHON, "-I", "-")
    assert call["env"] == {}
    assert call["sandbox"] is True
    assert call["cwd"] == "s/1234abcd"
    guest_program = call["stdin"].decode("utf-8")
    assert ".sandbox-tmp/b-" in guest_program
    for forbidden in ("127.0.0.1:18086", "t" * 40, "campaign-1234567890abcdef"):
        assert forbidden not in guest_program
    assert len(FakeBroker.instances) == 1
    broker = FakeBroker.instances[0]
    assert broker.started is True and broker.stopped is True
    assert broker.config.allowed_tools == frozenset({"coroot_ro.coroot_metrics_range"})


def test_service_persists_private_code_and_output_artifacts_with_ledger_references(tmp_path: Path):
    FakeBroker.instances.clear()
    executor, _client, ledger, trial_id = _controlled(tmp_path)
    artifact_root = tmp_path / "artifacts"
    service = CodeSandboxService(
        CodeSandboxConfig(trial_id=trial_id, ledger_root=ledger.root, artifact_root=artifact_root),
        executor=executor,
        ledger=ledger,
    )
    source = "print('private sandbox source')"

    response = service.run_python(source, 5)

    code_ref, output_ref = response["artifact_refs"]
    code_path = artifact_root / code_ref
    output_path = artifact_root / output_ref
    assert code_path.read_text(encoding="utf-8") == source
    assert json.loads(output_path.read_text(encoding="utf-8"))["stdout"] == '{"answer": 42}\n'
    assert stat.S_IMODE(code_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    event = ledger.query()[0]
    assert event.payload["code_artifact_ref"] == code_ref
    assert event.payload["output_artifact_ref"] == output_ref
    assert source not in str(event.payload)


def test_env_configuration_rejects_non_loopback_mcp_endpoint_and_never_accepts_url_from_tool_input(tmp_path: Path):
    env = {
        "RESBENCH_AUTHORIZED_RUN_ID": "campaign-1234567890abcdef-codex-d7-a-1",
        "RESBENCH_CODE_SANDBOX_GUEST_UID": "10003",
        "RESBENCH_CODE_SANDBOX_GUEST_GID": "10003",
        "RESBENCH_CODE_SANDBOX_BROKER_ROOT": str(tmp_path / "trial" / ".sandbox-tmp"),
        "RESBENCH_CODE_SANDBOX_AGENT_EXEC_CWD": "s/1234abcd",
        "RESBENCH_CODE_SANDBOX_ALLOWED_TOOLS": json.dumps(["coroot_ro.coroot_metrics_range"]),
        "RESBENCH_CODE_SANDBOX_MCP_ENDPOINTS_JSON": json.dumps({"coroot_ro": "https://outside.example/mcp"}),
        "RESBENCH_CODE_SANDBOX_MCP_TOKEN": "t" * 40,
    }

    with pytest.raises(CodeSandboxError, match="loopback MCP URL"):
        ControlledSandboxConfig.from_env(env)


def test_env_executor_invokes_agent_exec_with_installed_absolute_python_and_empty_env(monkeypatch, tmp_path: Path):
    import mcp_servers.code_sandbox.executor as executor_module

    clients = []

    class ConstructedAgentExecClient(FakeAgentExecClient):
        def __init__(self, socket_path, *, expected_server_uid):
            super().__init__()
            self.socket_path = Path(socket_path)
            self.expected_server_uid = expected_server_uid
            clients.append(self)

    env = {
        "RESBENCH_AUTHORIZED_RUN_ID": "campaign-1234567890abcdef-codex-d7-a-1",
        "RESBENCH_CODE_SANDBOX_GUEST_UID": "10003",
        "RESBENCH_CODE_SANDBOX_GUEST_GID": "10003",
        "RESBENCH_CODE_SANDBOX_BROKER_ROOT": str(Path("/tmp") / f"rb-{os.getpid()}-env" / ".sandbox-tmp"),
        "RESBENCH_CODE_SANDBOX_AGENT_EXEC_CWD": "s/1234abcd",
        "RESBENCH_CODE_SANDBOX_ALLOWED_TOOLS": json.dumps(["coroot_ro.coroot_metrics_range"]),
        "RESBENCH_CODE_SANDBOX_MCP_ENDPOINTS_JSON": json.dumps({"coroot_ro": "http://127.0.0.1:18086/mcp"}),
        "RESBENCH_CODE_SANDBOX_MCP_TOKEN": "t" * 40,
        "RESBENCH_AGENT_EXEC_SOCKET": str(tmp_path / "agent-exec.sock"),
        "RESBENCH_AGENT_EXEC_SERVER_UID": "0",
    }
    monkeypatch.setattr(executor_module, "AgentExecClient", ConstructedAgentExecClient)
    executor = ControlledSandboxExecutor.from_env(
        ledger=PlatformLedger(tmp_path / "ledger"),
        env=env,
    )
    executor.broker_factory = FakeBroker
    executor.invoker = lambda tool, args: {"tool": tool, "args": dict(args)}

    executor.run("print(1)", 1)

    assert len(clients) == 1
    assert clients[0].socket_path == tmp_path / "agent-exec.sock"
    assert clients[0].expected_server_uid == 0
    assert clients[0].calls[0]["argv"] == (AGENT_EXEC_SANDBOX_PYTHON, "-I", "-")
    assert clients[0].calls[0]["env"] == {}
    assert clients[0].calls[0]["sandbox"] is True


def test_broker_socket_path_fails_before_bind_when_controller_cwd_layout_is_too_long(tmp_path: Path):
    executor, _client, _ledger, _trial_id = _controlled(tmp_path)
    executor.config = replace(
        executor.config,
        broker_root=tmp_path / ("x" * 110) / ".sandbox-tmp",
    )

    with pytest.raises(CodeSandboxError, match="Unix socket limit"):
        executor.run("print('x')", 1)
