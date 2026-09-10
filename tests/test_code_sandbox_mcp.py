"""Contract tests for the controlled code-sandbox MCP service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcp_servers.code_sandbox.server import create_server
from mcp_servers.code_sandbox.broker import BrokerError, _parse_call
from mcp_servers.code_sandbox.service import (
    MAX_CODE_BYTES,
    MAX_OUTPUT_BYTES,
    CodeSandboxConfig,
    CodeSandboxError,
    CodeSandboxService,
    SandboxRunResult,
)
from stage2_service.platform_ledger import PlatformLedger


def run(awaitable):
    return asyncio.run(awaitable)


@dataclass
class FakeExecutor:
    result: SandboxRunResult
    calls: list[tuple[str, int]]

    def run(self, code: str, timeout_seconds: int) -> SandboxRunResult:
        self.calls.append((code, timeout_seconds))
        return self.result


def service(tmp_path: Path, result: SandboxRunResult | None = None):
    ledger = PlatformLedger(tmp_path / "ledger")
    executor = FakeExecutor(result or SandboxRunResult(0, "ok\n", "", 4, False), [])
    return CodeSandboxService(
        CodeSandboxConfig(trial_id="trial-1", ledger_root=ledger.root),
        executor=executor,
        ledger=ledger,
    ), executor, ledger


def test_mcp_exposes_only_bounded_run_python(tmp_path: Path) -> None:
    sandbox, _executor, _ledger = service(tmp_path)
    tools = {tool.name: tool for tool in run(create_server(service=sandbox).list_tools())}
    assert set(tools) == {"run_python"}
    schema = tools["run_python"].input_schema
    assert set(schema["properties"]) == {"code", "timeout_seconds"}
    assert schema["properties"]["timeout_seconds"]["maximum"] == 60
    for forbidden in ("url", "path", "env", "token", "trial_id"):
        assert forbidden not in schema["properties"]


def test_run_records_code_hash_not_source_or_secrets(tmp_path: Path) -> None:
    sandbox, executor, ledger = service(tmp_path)
    source = "print('sk-secret-must-not-enter-ledger')"
    response = sandbox.run_python(source, 5)

    assert response["ok"] is True
    assert executor.calls == [(source, 5)]
    event = ledger.query()[0]
    assert event.event_type == "SANDBOX_RUN"
    assert event.payload["code_sha256"]
    assert "source" not in event.payload
    assert "sk-secret" not in str(event.payload)


def test_output_is_capped_at_64_kib_and_marked(tmp_path: Path) -> None:
    sandbox, _executor, ledger = service(
        tmp_path,
        SandboxRunResult(0, "a" * MAX_OUTPUT_BYTES, "b" * 32, 1, False),
    )
    response = sandbox.run_python("print('x')", 1)
    assert len((response["stdout"] + response["stderr"]).encode()) <= MAX_OUTPUT_BYTES
    assert response["truncated"] is True
    assert ledger.query()[0].payload["truncated"] is True


@pytest.mark.parametrize("timeout", [0, 61])
def test_rejects_timeout_outside_contract(tmp_path: Path, timeout: int) -> None:
    sandbox, executor, _ledger = service(tmp_path)
    with pytest.raises(CodeSandboxError, match="timeout_seconds"):
        sandbox.run_python("print(1)", timeout)
    assert executor.calls == []


def test_rejects_code_over_limit_before_executor(tmp_path: Path) -> None:
    sandbox, executor, _ledger = service(tmp_path)
    with pytest.raises(CodeSandboxError, match="code exceeds"):
        sandbox.run_python("x" * (MAX_CODE_BYTES + 1), 1)
    assert executor.calls == []


def test_mcp_schema_rejects_timeout_above_public_maximum(tmp_path: Path) -> None:
    sandbox, _executor, _ledger = service(tmp_path)
    with pytest.raises(Exception, match="less than or equal to 60"):
        run(create_server(service=sandbox).call_tool("run_python", {"code": "print(1)", "timeout_seconds": 61}))


def test_broker_accepts_only_authorized_tool_and_object_args() -> None:
    allowed = frozenset({"coroot_ro.coroot_metrics_range"})
    assert _parse_call(
        {"type": "tool_call", "tool": "coroot_ro.coroot_metrics_range", "args": {"metric": "latency"}}, allowed
    ) == ("coroot_ro.coroot_metrics_range", {"metric": "latency"})
    with pytest.raises(BrokerError, match="not authorized"):
        _parse_call({"type": "tool_call", "tool": "http://evil", "args": {}}, allowed)
    with pytest.raises(BrokerError, match="args"):
        _parse_call({"type": "tool_call", "tool": "coroot_ro.coroot_metrics_range", "args": "url=/etc/passwd"}, allowed)
