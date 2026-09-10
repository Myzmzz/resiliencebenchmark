"""Every MCP server the supervisor can launch must run with ``python -m``."""

from __future__ import annotations

import importlib.util

import pytest

from stage2_service.mcp_supervisor import McpSupervisor

SUPERVISED_SERVERS = sorted(set(McpSupervisor.HTTP_PORTS) | set(McpSupervisor.SSE_PORTS))


@pytest.mark.parametrize("name", SUPERVISED_SERVERS)
def test_every_supervised_mcp_server_has_a_module_entrypoint(name: str) -> None:
    # The supervisor starts each server as `python -m mcp_servers.<name>`. On
    # 2026-09-10 chaos_mesh_control had no __main__.py, so every substitution
    # qualification (the Coroot checks for codex, claude-code and deepseek)
    # failed at startup with McpSupervisorError.
    assert importlib.util.find_spec(f"mcp_servers.{name}.__main__") is not None
