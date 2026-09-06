"""Use real pinned MCP v2 result models, not v1-shaped mocks."""
from contextlib import asynccontextmanager
import socket

import pytest
from mcp import types
import mcp
from mcp.client import streamable_http

from harness.agent_exec.protocol import recv_frame, send_frame
from mcp_servers.code_sandbox import broker
from mcp_servers.code_sandbox.executor import HttpMcpBrokerInvoker
from mcp_servers.code_sandbox.service import CodeSandboxError
from stage2_service.platform_ledger import PlatformLedger


@pytest.mark.parametrize("shape", ["structured", "text", "error"])
def test_http_invoker_reads_real_sdk_result_fields(monkeypatch, shape):
    response = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"ok": true, "source": "text"}')],
        structured_content={"ok": True, "source": "structured"} if shape == "structured" else None,
        is_error=shape == "error",
    )
    @asynccontextmanager
    async def http_client(**kwargs):
        assert kwargs["headers"] == {"Authorization": "Bearer controller-only-token"}
        yield object()
    @asynccontextmanager
    async def transport(endpoint, **kwargs):
        assert endpoint == "http://127.0.0.1:18085/mcp"
        yield (object(), object())
    class Session:
        def __init__(self, *args):
            assert len(args) == 2
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def initialize(self): pass
        async def call_tool(self, name, arguments):
            assert name == "harness_poll_notices" and arguments == {}
            return response
    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    invoker = HttpMcpBrokerInvoker(endpoints={"harness_channel": "http://127.0.0.1:18085/mcp"}, token="controller-only-token")
    if shape == "error":
        with pytest.raises(CodeSandboxError, match="rejected"):
            invoker("harness_channel.harness_poll_notices", {})
    else:
        assert invoker("harness_channel.harness_poll_notices", {}) == {"ok": True, "source": shape}


def test_broker_records_authorized_sdk_failure_and_returns_redacted_error(monkeypatch, tmp_path):
    ledger = PlatformLedger(tmp_path / "ledger")
    service = broker.SandboxBroker(
        broker.SandboxBrokerConfig(tmp_path / "test.sock", 10003, 10003, "trial", "code", frozenset({"harness_channel.harness_poll_notices"})),
        invoker=lambda *_args: (_ for _ in ()).throw(AttributeError("secret must never escape")),
        ledger=ledger,
    )
    monkeypatch.setattr(broker, "_assert_guest_peer", lambda *_args: None)
    server, client = socket.socketpair()
    try:
        send_frame(client, {"type": "tool_call", "tool": "harness_channel.harness_poll_notices", "args": {}})
        service._serve(server)
        response = recv_frame(client)
    finally:
        server.close()
        client.close()
    assert response == {"ok": False, "error": "authorized MCP call failed"}
    events = ledger.query(trial_id="trial",limit=10)
    assert len(events) == 1
    assert events[0].payload == {"code_sha256": "code", "tool": "harness_channel.harness_poll_notices", "allowed": True, "status": "failed", "error_type": "AttributeError"}
    assert "secret" not in str(events[0].payload)
