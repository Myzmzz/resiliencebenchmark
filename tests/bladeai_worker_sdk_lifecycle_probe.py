from __future__ import annotations

import asyncio
import importlib.metadata as metadata
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    if Path(sys.executable) != Path("/opt/bladeai-venv/bin/python"):
        raise AssertionError(f"unexpected executable: {sys.executable}")
    if metadata.version("blade-ai") != "0.3.0":
        raise AssertionError(f"unexpected blade-ai version: {metadata.version('blade-ai')}")
    if metadata.version("mcp") != "1.27.0":
        raise AssertionError(f"unexpected mcp version: {metadata.version('mcp')}")
    chaos_spec = importlib.util.find_spec("chaos_agent")
    if chaos_spec is None:
        raise AssertionError("chaos_agent is not importable")

    with tempfile.TemporaryDirectory(prefix="bladeai-sdk-probe-") as probe_dir:
        root = Path(probe_dir)
        fake_server = root / "fake_mcp_server.py"
        calls_file = root / "mcp-calls.jsonl"
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

        config_path = root / ".blade-ai" / "mcp.json"
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

        os.environ["BLADE_AI_MCP_ENABLED"] = "true"
        os.environ["BLADE_AI_MCP_CONFIG_PATH"] = str(config_path)
        os.environ["BLADE_AI_MCP_CONNECT_TIMEOUT_SECONDS"] = "5"
        os.environ["BLADE_AI_MEMORY_DIR"] = str(root / "memory")

        from chaos_agent.config.settings import settings

        settings.reload()

        import chaos_agent.agent.factory as factory
        import chaos_agent.skills.loader as skill_loader
        from chaos_agent.l4.agent import L4ResilienceAgent
        from stage2_service import bladeai_worker

        skill_loader.get_skills_dir = lambda: root / "missing-skills"
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

        factory.create_agent = create_agent
        L4ResilienceAgent._async_execute = execute_without_model
        events: list[tuple[str, dict]] = []
        bladeai_worker.emit = lambda kind, payload: events.append((kind, payload))

        bladeai_worker._install_worker_sdk_runtime(L4ResilienceAgent)
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
        lifecycle = [payload for kind, payload in events if kind == "mcp_lifecycle"]
        assert lifecycle == [
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

        async def failing_create_agent(_registry, checkpointer=None, *, mcp_manager=None):
            assert [client.name for client in mcp_manager._clients] == ["fake_ro"]
            raise RuntimeError("factory failed after MCP connect")

        manager_module.McpManager.disconnect_all = tracked_disconnect_all
        factory.create_agent = failing_create_agent
        failing_agent = L4ResilienceAgent()
        try:
            failing_agent.prepare(None, task)
            failing_agent.execute(None, SimpleNamespace(task_id="image-sdk-mcp-failure"))
        except RuntimeError as exc:
            assert str(exc) == "factory failed after MCP connect"
        else:
            raise AssertionError("factory failure was not propagated")
        finally:
            manager_module.McpManager.disconnect_all = original_disconnect_all

        assert disconnect_calls["count"] == 1

        graph_error_disconnects = {"count": 0}

        async def tracked_graph_disconnect_all(self):
            graph_error_disconnects["count"] += 1
            await original_disconnect_all(self)

        async def missing_recover_graph(_registry, checkpointer=None, *, mcp_manager=None):
            assert [client.name for client in mcp_manager._clients] == ["fake_ro"]
            return {"inject": object()}

        manager_module.McpManager.disconnect_all = tracked_graph_disconnect_all
        factory.create_agent = missing_recover_graph
        broken_graph_agent = L4ResilienceAgent()
        try:
            broken_graph_agent.prepare(None, task)
            broken_graph_agent.execute(None, SimpleNamespace(task_id="image-sdk-mcp-broken-graphs"))
        except KeyError:
            pass
        else:
            raise AssertionError("missing compiled graph did not fail")
        finally:
            manager_module.McpManager.disconnect_all = original_disconnect_all

        assert graph_error_disconnects["count"] == 1

        bad_config_path = root / ".blade-ai" / "mcp-bad.json"
        bad_config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fake_ro": {
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": [str(fake_server), str(calls_file)],
                            "attach_to": ["phase1"],
                            "timeout_seconds": 5,
                        },
                        "bad_ro": {
                            "transport": "stdio",
                            "command": "/no/such/mcp-server",
                            "args": [],
                            "attach_to": ["phase1"],
                            "timeout_seconds": 5,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["BLADE_AI_MCP_CONFIG_PATH"] = str(bad_config_path)
        settings.reload()
        factory_called = {"value": False}

        async def forbidden_create_agent(_registry, checkpointer=None, *, mcp_manager=None):
            factory_called["value"] = True
            raise AssertionError("create_agent must not run when a configured MCP server is missing")

        factory.create_agent = forbidden_create_agent
        missing_agent = L4ResilienceAgent()
        try:
            missing_agent.prepare(None, task)
            missing_agent.execute(None, SimpleNamespace(task_id="image-sdk-mcp-missing"))
        except bladeai_worker.BladeTaskError as exc:
            missing_server_error = str(exc)
            assert "bad_ro" in missing_server_error
        else:
            raise AssertionError("missing configured MCP server did not fail before factory")

        assert factory_called["value"] is False

        print(
            json.dumps(
                {
                    "status": "passed",
                    "executable": sys.executable,
                    "blade_ai": metadata.version("blade-ai"),
                    "mcp": metadata.version("mcp"),
                    "chaos_agent_origin": chaos_spec.origin,
                    "phase1_tool_names": observed["phase1_tool_names"],
                    "disconnect_on_factory_failure": disconnect_calls["count"],
                    "disconnect_on_graph_failure": graph_error_disconnects["count"],
                    "missing_server_error": missing_server_error,
                    "events": lifecycle,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
