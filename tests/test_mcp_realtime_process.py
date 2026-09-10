"""Real local MCP-process regression for the Controller audit boundary.

This deliberately starts only ``source_ro`` against a local empty source root.
It does not contact Kubernetes, a model gateway, or a fault injector.  The
test covers the process/environment/HTTP boundary that unit tests of
``PolicyGate`` alone cannot cover.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from mcp_servers.audit_bridge import AuditBridgeConfig, AuditBridgeListener
from stage2_service.capability_policy import write_policy_file
from stage2_service.contracts import ServerPolicy
from stage2_service.harness_adapters.base import ToolCall, ToolResult
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.tool_event_pump import RealtimeToolEventPump


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40
TRIAL_ID = "trial-realtime-process"
AUTHORITY = "controller-realtime-process"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(process: subprocess.Popen[bytes], port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"source_ro exited before readiness: {output[-2000:]}")
        with socket.socket() as sock:
            sock.settimeout(0.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("source_ro did not bind its local MCP port")


@contextmanager
def _source_ro_process(tmp_path: Path, *, policy_file: Path, audit: AuditBridgeConfig) -> Iterator[str]:
    port = _free_port()
    source_root = tmp_path / "sources"
    source_root.mkdir(mode=0o700)
    log_path = tmp_path / "source-ro.log"
    environment = {
        **os.environ,
        "RESBENCH_MCP_TRANSPORT": "streamable-http",
        "RESBENCH_MCP_TOKEN": TOKEN,
        "RESBENCH_MCP_HTTP_HOST": "127.0.0.1",
        "RESBENCH_MCP_HTTP_PORT": str(port),
        "RESBENCH_MCP_HTTP_PATH": "/mcp",
        "RESBENCH_MCP_ISSUER_URL": "http://127.0.0.1:17999",
        "RESBENCH_MCP_RESOURCE_URL": f"http://127.0.0.1:{port}/mcp",
        "RESBENCH_MCP_SCOPE": "stage2:fixture:source_ro",
        "RESBENCH_MCP_POLICY_FILE": str(policy_file),
        "RESBENCH_PLATFORM_LEDGER_ROOT": str(tmp_path / "ledger"),
        "RESBENCH_AUTHORIZED_RUN_ID": audit.trial_id,
        "RESBENCH_MCP_AUDIT_SOCKET": str(audit.socket_path),
        "RESBENCH_MCP_AUDIT_AUTHORITY": audit.authority,
        "RESBENCH_MCP_AUDIT_TIMEOUT_SECONDS": "5",
        "RESBENCH_SOURCE_ROOT": str(source_root),
        "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": "otel-demo",
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "mcp_servers.source_ro"], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=environment,
        )
        try:
            _wait_ready(process, port, log_path)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def _call(endpoint: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("source_list_repositories", {})
    assert result.is_error is False
    structured = result.structured_content
    assert isinstance(structured, dict)
    return dict(structured)


@pytest.mark.parametrize("mode,expected", [
    ("disabled", "TOOL_DISABLED"),
    ("d5", "CHANNEL_UNAVAILABLE"),
])
def test_real_mcp_process_applies_controller_policy_on_the_same_call(
    tmp_path: Path, mode: str, expected: str,
) -> None:
    """The child process audits before its PolicyGate reads the policy file."""
    policy_file = tmp_path / "trial" / "tools.policy.json"
    write_policy_file(
        policy_file, trial_id=TRIAL_ID,
        servers={"source_ro": ServerPolicy(server_name="source_ro")},
    )
    ledger = PlatformLedger(tmp_path / "ledger")
    events: list[ToolCall | ToolResult] = []
    with tempfile.TemporaryDirectory(prefix="mcp-audit-", dir="/tmp") as socket_root:
        audit = AuditBridgeConfig(Path(socket_root) / "events.sock", TRIAL_ID, AUTHORITY, timeout_seconds=5)

        def controller(event: ToolCall | ToolResult, _source: str) -> dict[str, object]:
            events.append(event)
            if isinstance(event, ToolCall):
                policy = ServerPolicy(
                    server_name="source_ro",
                    state="disabled" if mode == "disabled" else "enabled",
                    channel_unavailable_until=(
                        datetime.now(UTC) + timedelta(seconds=10) if mode == "d5" else None
                    ),
                )
                write_policy_file(policy_file, trial_id=TRIAL_ID, servers={"source_ro": policy})
            return {"allowed": True}

        pump = RealtimeToolEventPump(TRIAL_ID, ledger, controller)
        with AuditBridgeListener(audit, pump), _source_ro_process(tmp_path, policy_file=policy_file, audit=audit) as endpoint:
            response = asyncio.run(_call(endpoint))

    assert response == {
        "ok": False,
        "error": {"code": expected, "message": "该工具已停用。" if expected == "TOOL_DISABLED" else "通道暂时不可用。"},
        "controller_call_id": events[0].call_id,
    }
    assert [type(event) for event in events] == [ToolCall, ToolResult]
    assert events[0].tool == "source_ro.source_list_repositories"
    assert events[1].payload == response
    assert [event.event_type for event in ledger.query(trial_id=TRIAL_ID)] == [
        "ToolCall",
        "TOOL_CALL_DENIED_DISABLED" if expected == "TOOL_DISABLED" else "CHANNEL_UNAVAILABLE_RETURNED",
        "ToolResult",
    ]
