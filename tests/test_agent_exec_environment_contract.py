from __future__ import annotations

from pathlib import Path

import pytest

from harness.agent_exec.environment import AGENT_ENV_ALLOWLIST
from harness.agent_exec.protocol import StartRequest
from harness.agent_exec.server import AgentExecServer, AgentExecServerConfig
from mcp_servers.bladeai_k8s_proxy.service import ProxyConfig
from scripts.run_harness_trial import child_env_for_harness
from stage2_service.bladeai_launch import prepare_bladeai_launch


REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL_RELAY_TOKEN = "trial-relay-token-for-agent-only-0001"
GATEWAY_MASTER_KEY = "gateway-master-key-must-not-reach-agent"
TRIAL_MCP_TOKEN = "trial-mcp-token-for-agent-only-000001"


def _parent_env() -> dict[str, str]:
    return {
        "RESBENCH_LLM_BASE_URL": "http://127.0.0.1:18090/v1",
        "RESBENCH_LLM_API_KEY": TRIAL_RELAY_TOKEN,
        "RESBENCH_MCP_TOKEN": TRIAL_MCP_TOKEN,
        "RESBENCH_HARNESS_CHANNEL_TOKEN": "trial-channel-token-for-agent-only-001",
        "RESBENCH_HARNESS_CHANNEL_MCP_URL": "http://127.0.0.1:18085/mcp",
        "RESBENCH_K8S_MCP_URL": "http://127.0.0.1:18081/mcp",
        "RESBENCH_TELEMETRY_MCP_URL": "http://127.0.0.1:18082/mcp",
        "RESBENCH_SOURCE_MCP_URL": "http://127.0.0.1:18083/mcp",
        "RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:18084/mcp",
        "RESBENCH_COROOT_MCP_URL": "http://127.0.0.1:18086/mcp",
        "RESBENCH_CHAOS_MESH_CONTROL_MCP_URL": "http://127.0.0.1:18087/mcp",
        "RESBENCH_CODE_SANDBOX_MCP_URL": "http://127.0.0.1:18088/mcp",
        "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": "http://127.0.0.1:18185/sse",
        "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
        "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
        "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
        "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
        "RESBENCH_BLADEAI_COROOT_MCP_SSE_URL": "http://127.0.0.1:18186/sse",
        "RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18187/sse",
        "RESBENCH_BLADEAI_CODE_SANDBOX_MCP_SSE_URL": "http://127.0.0.1:18188/sse",
        "RESBENCH_AGENT_RELAY_TOKEN": TRIAL_RELAY_TOKEN,
        "RESBENCH_GATEWAY_MASTER_KEY": GATEWAY_MASTER_KEY,
        "RESBENCH_MCP_AUDIT_AUTHORITY": "controller-audit-authority",
        "RESBENCH_BLADEAI_PROXY_TOKEN": "controller-proxy-token",
        "RESBENCH_COROOT_SESSION_COOKIE": "controller-coroot-session",
        "RESBENCH_BASELINE_GATE_TOKEN": "controller-baseline-gate-token",
        "RESBENCH_CLEANUP_HANDLE": "controller-cleanup-handle",
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "controller-token-ref",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": "controller-pod-uid",
        "RESBENCH_AUTHORIZED_TARGET_JSON": '{"controller":"target"}',
        "RESBENCH_MAIN_FAULT_JSON": '{"controller":"fault"}',
        "RESBENCH_AUTHORIZED_RUN_ID": "campaign-controller-run",
        "RESBENCH_CODEX_AUTH_FILE": "/controller/codex/auth.json",
    }


def _homes(trial_root: Path) -> dict[str, str]:
    values = {
        "CODEX_HOME": str(trial_root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(trial_root / "claude-home"),
        "DSH_HOME": str(trial_root / "dsh-home"),
    }
    for value in values.values():
        Path(value).mkdir(parents=True)
    return values


def _daemon() -> AgentExecServer:
    return AgentExecServer(
        AgentExecServerConfig(
            socket_path=Path("/unused/agent-exec.sock"),
            trial_root=Path.cwd(),
            controller_uid=10000,
            agent_uid=10001,
            allowed_env=set(AGENT_ENV_ALLOWLIST),
        )
    )


def _assert_default_daemon_accepts(child_env: dict[str, str]) -> None:
    _daemon()._validate_request(
        StartRequest("run", ("agent",), child_env, b"", ".", 1)
    )


def _assert_controller_private_env_absent(child_env: dict[str, str]) -> None:
    for key in (
        "RESBENCH_AGENT_RELAY_TOKEN",
        "RESBENCH_GATEWAY_MASTER_KEY",
        "RESBENCH_MCP_AUDIT_AUTHORITY",
        "RESBENCH_BLADEAI_PROXY_TOKEN",
        "RESBENCH_COROOT_SESSION_COOKIE",
        "RESBENCH_BASELINE_GATE_TOKEN",
        "RESBENCH_CLEANUP_HANDLE",
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID",
        "RESBENCH_AUTHORIZED_TARGET_JSON",
        "RESBENCH_MAIN_FAULT_JSON",
        "RESBENCH_AUTHORIZED_RUN_ID",
        "RESBENCH_CODEX_AUTH_FILE",
    ):
        assert key not in child_env
        assert GATEWAY_MASTER_KEY not in child_env.values()


@pytest.mark.parametrize(
    ("harness_name", "key_name"),
    [
        ("codex", "OPENAI_API_KEY"),
        ("claude-code", "ANTHROPIC_API_KEY"),
        ("deepseek-harness", "RESBENCH_LLM_API_KEY"),
    ],
)
def test_default_daemon_allowlist_accepts_real_child_env_for_formal_harnesses(
    tmp_path: Path,
    harness_name: str,
    key_name: str,
) -> None:
    child_env = child_env_for_harness(
        harness_name,
        _parent_env(),
        _homes(tmp_path / harness_name),
    )

    _assert_default_daemon_accepts(child_env)
    _assert_controller_private_env_absent(child_env)
    assert child_env[key_name] == TRIAL_RELAY_TOKEN
    assert child_env["USER"] == "resbench"
    assert child_env["LOGNAME"] == "resbench"
    if harness_name == "deepseek-harness":
        assert child_env["DSH_TOOLS_MODE"] == "native"


def test_default_daemon_allowlist_accepts_real_bladeai_env_with_optional_servers(
    tmp_path: Path,
) -> None:
    _argv, _stdin, child_env = prepare_bladeai_launch(
        repo_root=REPO_ROOT,
        trial_root=tmp_path / "bladeai-trial",
        trial_id="campaign-1234567890abcdef-bladeai-d0-1",
        namespace="otel-demo",
        prompt="qualification prompt",
        model_alias="gpt-5.5",
        environment=_parent_env(),
        proxy_config=ProxyConfig(namespace="otel-demo", token="proxy-token-for-kubeconfig-only-0001"),
        python_executable="/opt/bladeai-venv/bin/python",
    )

    _assert_default_daemon_accepts(child_env)
    _assert_controller_private_env_absent(child_env)
    assert child_env["BLADE_AI_LLM_API_KEY"] == TRIAL_RELAY_TOKEN
    assert child_env["BLADE_AI_API_BASE_URL"] == "http://127.0.0.1:18090/v1"
    assert child_env["BLADE_AI_MODEL_NAME"] == "gpt-5.5"
    assert child_env["BLADE_AI_MCP_ENABLED"] == "true"
    assert child_env["RESBENCH_BLADEAI_COROOT_MCP_SSE_URL"]
    assert child_env["RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL"]
    assert child_env["RESBENCH_BLADEAI_CODE_SANDBOX_MCP_SSE_URL"]
