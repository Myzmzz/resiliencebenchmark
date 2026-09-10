from __future__ import annotations

import asyncio
import importlib.metadata as metadata
import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


if Path(sys.executable) != Path("/opt/bladeai-venv/bin/python"):
    pytest.skip(
        "requires fixed BladeAI agent image /opt/bladeai-venv",
        allow_module_level=True,
    )

if importlib.util.find_spec("chaos_agent") is None:
    pytest.skip("requires real BladeAI SDK chaos_agent package", allow_module_level=True)


def test_worker_uses_real_bladeai_sdk_mcp_lifecycle_without_model_or_cluster(
    tmp_path, monkeypatch
):
    assert sys.executable == "/opt/bladeai-venv/bin/python"
    assert metadata.version("blade-ai") == "0.3.0"
    assert metadata.version("mcp") == "1.27.0"

    fake_server = tmp_path / "fake_mcp_server.py"
    calls_file = tmp_path / "mcp-calls.jsonl"
    fake_server.write_text(
        textwrap.dedent(
            """
            import anyio
            import json
            import sys
            from mcp import types
            from mcp.server import Server
            from mcp.server.stdio import stdio_server

            calls_file = sys.argv[1]
            server = Server("fake-resbench-mcp")

            @server.list_tools()
            async def list_tools():
                return [
                    types.Tool(
                        name="echo_probe",
                        description="local no-fault probe",
                        inputSchema={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    )
                ]

            @server.call_tool()
            async def call_tool(_tool_name, arguments):
                arguments = dict(arguments or {})
                with open(calls_file, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
                return types.CallToolResult(content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"ok": True, "value": arguments.get("value")},
                            separators=(",", ":"),
                        ),
                    )
                ])

            async def main():
                async with stdio_server() as streams:
                    await server.run(
                        streams[0],
                        streams[1],
                        server.create_initialization_options(),
                    )

            anyio.run(main)
            """
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / ".blade-ai" / "mcp.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake_ro": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(fake_server), str(calls_file)],
                        "attach_to": ["phase1"],
                        "timeout_seconds": 5,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("BLADE_AI_MCP_ENABLED", "true")
    monkeypatch.setenv("BLADE_AI_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BLADE_AI_MCP_CONNECT_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("BLADE_AI_MEMORY_DIR", str(tmp_path / "memory"))

    from chaos_agent.config.settings import settings

    settings.reload()

    import chaos_agent.agent.factory as factory
    import chaos_agent.skills.loader as skill_loader
    from chaos_agent.l4.agent import L4ResilienceAgent
    from stage2_service.bladeai_worker import _install_worker_sdk_runtime

    monkeypatch.setattr(skill_loader, "get_skills_dir", lambda: tmp_path / "missing-skills")
    observed = {}

    async def create_agent(_registry, checkpointer=None, *, mcp_manager=None):
        observed["create_loop"] = id(asyncio.get_running_loop())
        observed["manager_class"] = type(mcp_manager).__name__
        observed["client_classes"] = [
            type(client).__name__ for client in mcp_manager._clients
        ]
        phase1_tools = mcp_manager.tools_for_phase("phase1")
        observed["phase1_tool_names"] = [tool.name for tool in phase1_tools]
        observed["factory_probe"] = await phase1_tools[0].ainvoke({"value": "factory"})
        return {"inject": object(), "recover": object()}

    async def execute_without_model(self, pool, _runtime, task):
        observed["execute_loop"] = id(asyncio.get_running_loop())
        tool = pool._mcp_manager.tools_for_phase("phase1")[0]
        observed["execute_probe"] = await tool.ainvoke({"value": "execute"})
        return SimpleNamespace(
            status="passed",
            task_id=task.task_id,
            trajectory_id="trajectory",
            summary="done",
            error=None,
            extras={},
        )

    monkeypatch.setattr(factory, "create_agent", create_agent)
    monkeypatch.setattr(L4ResilienceAgent, "_async_execute", execute_without_model)

    emitted = []
    monkeypatch.setattr(
        "stage2_service.bladeai_worker.emit",
        lambda kind, payload: emitted.append((kind, payload)),
    )

    _install_worker_sdk_runtime(L4ResilienceAgent)
    agent = L4ResilienceAgent()
    task = SimpleNamespace(task_id="image-sdk-mcp-lifecycle")

    agent.prepare(None, task)
    result = agent.execute(None, task)

    assert result.status == "passed"
    assert observed["manager_class"] == "McpManager"
    assert observed["client_classes"] == ["McpClient"]
    assert observed["create_loop"] == observed["execute_loop"]
    assert observed["phase1_tool_names"] == ["fake_ro__echo_probe"]
    assert observed["factory_probe"] == '{"ok":true,"value":"factory"}'
    assert observed["execute_probe"] == '{"ok":true,"value":"execute"}'
    assert agent._pool._mcp_manager is None
    assert agent._pool._initialized is False
    assert calls_file.read_text(encoding="utf-8").splitlines() == [
        '{"value":"factory"}',
        '{"value":"execute"}',
    ]
    assert [payload for kind, payload in emitted if kind == "mcp_lifecycle"] == [
        {
            "configured_servers": ["fake_ro"],
            "connected_servers": ["fake_ro"],
            "phase_tool_counts": {
                "clarification": 0,
                "phase1": 1,
                "phase2": 0,
                "verifier": 0,
            },
        }
    ]

    import chaos_agent.mcp.manager as manager_module

    original_disconnect_all = manager_module.McpManager.disconnect_all
    disconnect_calls = {"count": 0}

    async def tracked_disconnect_all(self):
        disconnect_calls["count"] += 1
        await original_disconnect_all(self)

    async def missing_recover_graph(_registry, checkpointer=None, *, mcp_manager=None):
        assert [client.name for client in mcp_manager._clients] == ["fake_ro"]
        return {"inject": object()}

    monkeypatch.setattr(manager_module.McpManager, "disconnect_all", tracked_disconnect_all)
    monkeypatch.setattr(factory, "create_agent", missing_recover_graph)
    broken_agent = L4ResilienceAgent()
    with pytest.raises(KeyError):
        broken_agent.prepare(None, task)
        broken_agent.execute(None, SimpleNamespace(task_id="image-sdk-mcp-broken-graphs"))
    assert disconnect_calls["count"] == 1
