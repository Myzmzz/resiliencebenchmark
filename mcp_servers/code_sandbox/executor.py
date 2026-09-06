"""Production bridge from ``code_sandbox`` to agent-exec and controlled MCP.

All endpoint and token material stays in this control-plane process.  Guest
source receives only the stable relative Unix-socket name generated for its
own run.  The broker is destroyed before this method returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from harness.agent_exec.client import AgentExecClient
from harness.agent_exec.sandbox import AgentExecSandboxConfig, AgentExecSandboxExecutor
from stage2_service.platform_ledger import PlatformLedger

from .broker import SandboxBroker, SandboxBrokerConfig, ToolInvoker
from .service import CodeSandboxError, SandboxRunResult


_TOKEN_MINIMUM = 32
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,127}$")
_ENV_ENDPOINTS = "RESBENCH_CODE_SANDBOX_MCP_ENDPOINTS_JSON"
_MAX_UNIX_SOCKET_PATH_BYTES = 100


class ControlledMcpInvoker(Protocol):
    def __call__(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ControlledSandboxConfig:
    trial_id: str
    guest_uid: int
    guest_gid: int
    broker_root: Path
    agent_exec_cwd: str
    allowed_tools: frozenset[str]
    endpoints: dict[str, str]
    token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ControlledSandboxConfig":
        values = os.environ if env is None else env
        trial_id = _required(values, "RESBENCH_AUTHORIZED_RUN_ID")
        guest_uid = _positive_int(_required(values, "RESBENCH_CODE_SANDBOX_GUEST_UID"), "guest uid")
        guest_gid = _positive_int(_required(values, "RESBENCH_CODE_SANDBOX_GUEST_GID"), "guest gid")
        broker_root = Path(_required(values, "RESBENCH_CODE_SANDBOX_BROKER_ROOT"))
        if (
            not broker_root.is_absolute()
            or broker_root.is_symlink()
            or broker_root.name != ".sandbox-tmp"
        ):
            raise CodeSandboxError("sandbox broker root must be an absolute non-symlink path")
        agent_exec_cwd = _safe_sandbox_cwd(
            _required(values, "RESBENCH_CODE_SANDBOX_AGENT_EXEC_CWD")
        )
        allowed_tools = _tool_set(_required(values, "RESBENCH_CODE_SANDBOX_ALLOWED_TOOLS"))
        endpoints = _endpoint_map(_required(values, _ENV_ENDPOINTS), allowed_tools)
        token = _required(values, "RESBENCH_CODE_SANDBOX_MCP_TOKEN")
        if len(token) < _TOKEN_MINIMUM or token != token.strip() or any(char.isspace() for char in token):
            raise CodeSandboxError("sandbox MCP token is invalid")
        return cls(
            trial_id=trial_id,
            guest_uid=guest_uid,
            guest_gid=guest_gid,
            broker_root=broker_root,
            agent_exec_cwd=agent_exec_cwd,
            allowed_tools=allowed_tools,
            endpoints=endpoints,
            token=token,
        )


class HttpMcpBrokerInvoker:
    """Control-plane-only MCP client restricted to configured loopback servers."""

    def __init__(self, *, endpoints: Mapping[str, str], token: str):
        self.endpoints = dict(endpoints)
        self.token = token

    def __call__(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        server, tool_name = _split_tool(tool)
        endpoint = self.endpoints.get(server)
        if endpoint is None:
            raise CodeSandboxError("sandbox tool endpoint is not configured")
        return asyncio.run(self._call(endpoint, tool_name, dict(arguments)))

    async def _call(self, endpoint: str, tool_name: str, arguments: dict[str, Any]) -> Mapping[str, Any]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        headers = {"Authorization": f"Bearer {self.token}"}
        if urlparse(endpoint).path.endswith("/sse"):
            async with sse_client(endpoint, headers=headers) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
        else:
            async with create_mcp_http_client(headers=headers) as http_client:
                async with streamable_http_client(endpoint, http_client=http_client) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments)
        if result.isError:
            raise CodeSandboxError("authorized MCP tool rejected sandbox call")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, Mapping):
            return dict(structured)
        text = "".join(
            str(item.text)
            for item in result.content
            if getattr(item, "type", None) == "text"
        )
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CodeSandboxError("authorized MCP tool returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise CodeSandboxError("authorized MCP tool returned an invalid response")
        return dict(decoded)


class ControlledSandboxExecutor:
    """Create one broker, execute through agent-exec, then remove the broker."""

    def __init__(
        self,
        config: ControlledSandboxConfig,
        *,
        agent_exec: AgentExecSandboxExecutor,
        ledger: PlatformLedger,
        invoker: ControlledMcpInvoker | None = None,
        broker_factory=SandboxBroker,
    ) -> None:
        self.config = config
        self.agent_exec = agent_exec
        self.ledger = ledger
        self.invoker = invoker or HttpMcpBrokerInvoker(
            endpoints=config.endpoints,
            token=config.token,
        )
        self.broker_factory = broker_factory

    @classmethod
    def from_env(
        cls,
        *,
        ledger: PlatformLedger,
        env: Mapping[str, str] | None = None,
    ) -> "ControlledSandboxExecutor":
        values = os.environ if env is None else env
        config = ControlledSandboxConfig.from_env(values)
        socket_path = Path(_required(values, "RESBENCH_AGENT_EXEC_SOCKET"))
        if not socket_path.is_absolute():
            raise CodeSandboxError("agent-exec socket must be absolute")
        server_uid = _positive_int(_required(values, "RESBENCH_AGENT_EXEC_SERVER_UID"), "agent-exec server uid", zero_allowed=True)
        client = AgentExecClient(socket_path, expected_server_uid=server_uid)
        return cls(
            config,
            agent_exec=AgentExecSandboxExecutor(
                client,
                config=AgentExecSandboxConfig(cwd=config.agent_exec_cwd),
            ),
            ledger=ledger,
        )

    def run(self, code: str, timeout_seconds: int) -> SandboxRunResult:
        self.config.broker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.config.broker_root.is_symlink():
            raise CodeSandboxError("sandbox broker root is unsafe")
        nonce = secrets.token_hex(8)
        socket_path = self.config.broker_root / f"b-{nonce}.sock"
        if len(os.fsencode(socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise CodeSandboxError("sandbox broker socket path exceeds Unix socket limit")
        relative_path = f".sandbox-tmp/{socket_path.name}"
        broker = self.broker_factory(
            SandboxBrokerConfig(
                socket_path=socket_path,
                guest_uid=self.config.guest_uid,
                guest_gid=self.config.guest_gid,
                trial_id=self.config.trial_id,
                code_sha256=_sha256(code),
                allowed_tools=self.config.allowed_tools,
            ),
            invoker=self.invoker,
            ledger=self.ledger,
        )
        broker.start()
        try:
            return self.agent_exec.run(
                code,
                timeout_seconds,
                broker_socket_relative_path=relative_path,
            )
        finally:
            broker.stop()


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CodeSandboxError(f"{name} is required")
    return value


def _positive_int(value: str, label: str, *, zero_allowed: bool = False) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CodeSandboxError(f"{label} is invalid") from exc
    if parsed < 0 or (parsed == 0 and not zero_allowed):
        raise CodeSandboxError(f"{label} is invalid")
    return parsed


def _tool_set(raw: str) -> frozenset[str]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodeSandboxError("sandbox tool allowlist is invalid") from exc
    if not isinstance(decoded, list) or not decoded or len(decoded) > 128:
        raise CodeSandboxError("sandbox tool allowlist is invalid")
    tools = frozenset(item for item in decoded if isinstance(item, str) and _TOOL_NAME.fullmatch(item))
    if len(tools) != len(decoded):
        raise CodeSandboxError("sandbox tool allowlist is invalid")
    return tools


def _endpoint_map(raw: str, allowed_tools: frozenset[str]) -> dict[str, str]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodeSandboxError("sandbox MCP endpoint mapping is invalid") from exc
    if not isinstance(decoded, dict):
        raise CodeSandboxError("sandbox MCP endpoint mapping is invalid")
    required_servers = {tool.split(".", 1)[0] for tool in allowed_tools}
    if set(decoded) != required_servers:
        raise CodeSandboxError("sandbox MCP endpoint mapping does not match allowlist")
    endpoints: dict[str, str] = {}
    for server, raw_url in decoded.items():
        if not isinstance(server, str) or not isinstance(raw_url, str):
            raise CodeSandboxError("sandbox MCP endpoint mapping is invalid")
        parsed = urlparse(raw_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"/mcp", "/sse"}
        ):
            raise CodeSandboxError("sandbox MCP endpoint must be a loopback MCP URL")
        endpoints[server] = raw_url
    return endpoints


def _split_tool(tool: str) -> tuple[str, str]:
    if not _TOOL_NAME.fullmatch(tool):
        raise CodeSandboxError("sandbox tool name is invalid")
    return tool.split(".", 1)


def _safe_sandbox_cwd(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or len(value.encode("utf-8")) > 48
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise CodeSandboxError("sandbox agent-exec cwd must be a short safe relative path")
    return value


def _sha256(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode("utf-8")).hexdigest()
