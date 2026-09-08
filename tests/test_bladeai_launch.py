from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_servers.bladeai_k8s_proxy.service import ProxyConfig
from stage2_service.bladeai_launch import prepare_bladeai_launch


REPO_ROOT = Path(__file__).resolve().parents[1]


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "RESBENCH_LLM_BASE_URL": "http://127.0.0.1:18090/v1",
        "RESBENCH_LLM_API_KEY": "trial-relay-token",
        "RESBENCH_MCP_TOKEN": "trial-mcp-token",
        "RESBENCH_HARNESS_CHANNEL_TOKEN": "trial-channel-token",
        "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": "http://127.0.0.1:18185/sse",
        "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
        "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
        "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
        "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
        "RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18187/sse",
        "RESBENCH_BLADEAI_COROOT_MCP_SSE_URL": "http://127.0.0.1:18186/sse",
    }
    values.update(overrides)
    return values


def _launch(tmp_path: Path, environment: dict[str, str]):
    return prepare_bladeai_launch(
        repo_root=REPO_ROOT,
        trial_root=tmp_path / "trial",
        trial_id="campaign-1234567890abcdef-bladeai-d0-1",
        namespace="otel-demo",
        prompt="qualification prompt",
        model_alias="gpt-5.5",
        environment=environment,
        proxy_config=ProxyConfig(
            namespace="otel-demo",
            token="proxy-token-for-kubeconfig-only-0001",
        ),
        python_executable="/opt/bladeai-venv/bin/python",
    )


def test_agent_visible_mcp_excludes_execution_servers_but_shim_env_keeps_chaos_url(tmp_path: Path) -> None:
    _argv, _stdin, child_env = _launch(tmp_path, _env())

    mcp = json.loads(Path(child_env["BLADE_AI_MCP_CONFIG_PATH"]).read_text())["mcpServers"]
    template = json.loads((REPO_ROOT / "harness/bladeai/mcp.json.template").read_text())["mcpServers"]
    assert "chaos_control" not in mcp
    assert "chaos_mesh_control" not in mcp
    assert "coroot_ro" in mcp
    for name in ("k8s_ro", "telemetry_ro", "source_ro", "harness_channel", "coroot_ro"):
        assert mcp[name]["attach_to"] == template[name]["attach_to"]
    assert child_env["RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL"] == "http://127.0.0.1:18184/sse"
    assert child_env["RESBENCH_MCP_TOKEN"] == "trial-mcp-token"
    assert child_env["BLADE_AI_MCP_CONNECT_TIMEOUT_SECONDS"] == "120"


def test_wp8_launch_carries_only_controller_fault_contract(tmp_path: Path) -> None:
    _argv, _stdin, child_env = prepare_bladeai_launch(
        repo_root=REPO_ROOT,
        trial_root=tmp_path / "trial",
        trial_id="campaign-1234567890abcdef-bladeai-wp8-1",
        namespace="otel-demo",
        prompt="qualification prompt",
        model_alias="gpt-5.5",
        environment=_env(),
        proxy_config=ProxyConfig(
            namespace="otel-demo",
            token="proxy-token-for-kubeconfig-only-0001",
        ),
        python_executable="/opt/bladeai-venv/bin/python",
        qualification_fault={
            "qualification_type": "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION",
            "fault_type": "network-delay",
            "duration_seconds": 120,
            "intensity": {"delay_ms": 1},
        },
    )
    task = json.loads((Path(child_env["HOME"]) / "task.json").read_text())
    assert task["mode"] == "task"
    assert task.get("target") is None
    assert task["qualification_fault"]["fault_type"] == "network-delay"
    assert task["qualification_fault"]["duration_seconds"] == 120


def test_bladeai_worker_uses_trial_local_source_overlay(tmp_path: Path) -> None:
    _argv, _stdin, child_env = _launch(tmp_path, _env())

    overlay, image_root = child_env["PYTHONPATH"].split(":", 1)
    assert overlay.startswith(str(tmp_path / "trial"))
    assert image_root == str(REPO_ROOT)
    assert (Path(overlay) / "stage2_service" / "bladeai_worker.py").read_bytes() == (
        REPO_ROOT / "stage2_service" / "bladeai_worker.py"
    ).read_bytes()
    assert (Path(overlay) / "stage2_service" / "bladeai_events.py").read_bytes() == (
        REPO_ROOT / "stage2_service" / "bladeai_events.py"
    ).read_bytes()
    assert (Path(overlay) / "stage2_service" / "bladeai_shim.py").read_bytes() == (
        REPO_ROOT / "stage2_service" / "bladeai_shim.py"
    ).read_bytes()


def test_missing_required_shim_endpoint_fails_even_when_agent_mcp_is_read_only(tmp_path: Path) -> None:
    environment = _env(RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL="")

    with pytest.raises(ValueError, match="controlled blade shim"):
        _launch(tmp_path, environment)


def test_sandbox_is_only_exposed_when_provisioned_for_the_case(tmp_path: Path) -> None:
    environment = _env(RESBENCH_BLADEAI_CODE_SANDBOX_MCP_SSE_URL="http://127.0.0.1:18188/sse")
    _argv, _stdin, child_env = _launch(tmp_path, environment)
    mcp = json.loads(Path(child_env["BLADE_AI_MCP_CONFIG_PATH"]).read_text())["mcpServers"]
    assert mcp["code_sandbox"]["url"] == environment["RESBENCH_BLADEAI_CODE_SANDBOX_MCP_SSE_URL"]
    assert "chaos_control" not in mcp and "chaos_mesh_control" not in mcp


def test_missing_optional_bladeai_endpoints_do_not_render_placeholders(tmp_path: Path) -> None:
    environment = _env(
        RESBENCH_BLADEAI_COROOT_MCP_SSE_URL="",
        RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL="",
    )

    _argv, _stdin, child_env = _launch(tmp_path, environment)

    mcp = json.loads(Path(child_env["BLADE_AI_MCP_CONFIG_PATH"]).read_text())["mcpServers"]
    assert set(mcp) == {"k8s_ro", "telemetry_ro", "source_ro", "harness_channel"}
    assert "RESBENCH_BLADEAI_COROOT_MCP_SSE_URL" not in child_env
    assert "RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL" not in child_env
