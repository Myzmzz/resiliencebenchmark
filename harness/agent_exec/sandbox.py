"""Narrow ``agent_exec`` adapter for controlled Python sandbox requests.

This module is deliberately not a general subprocess helper.  The only
request it emits is ``python3 -I -`` in agent-exec's ``sandbox`` mode with an
empty environment.  The source receives a tiny ``mcp_call`` function that can
reach the per-run Unix broker; it never receives a URL, bearer token, Trial
identifier, or other controller configuration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from mcp_servers.code_sandbox.service import SandboxRunResult

from .client import AgentExecClient


class AgentExecSandboxError(RuntimeError):
    """The isolated agent-exec invocation could not be completed."""


@dataclass(frozen=True)
class AgentExecSandboxConfig:
    cwd: str = "."
    interpreter: str = "python3"
    request_id_prefix: str = "code-sandbox"


class AgentExecSandboxExecutor:
    """Execute one source string through the Linux agent-exec sandbox mode."""

    def __init__(self, client: AgentExecClient, *, config: AgentExecSandboxConfig | None = None):
        self.client = client
        self.config = config or AgentExecSandboxConfig()
        self._sequence = 0

    def run(
        self,
        code: str,
        timeout_seconds: int,
        *,
        broker_socket_relative_path: str,
    ) -> SandboxRunResult:
        relative_socket = _broker_socket_path(broker_socket_relative_path)
        self._sequence += 1
        started = time.monotonic()
        try:
            result = self.client.run(
                (self.config.interpreter, "-I", "-"),
                _guest_program(code, relative_socket).encode("utf-8"),
                {},
                timeout_seconds,
                cwd=self.config.cwd,
                request_id=f"{self.config.request_id_prefix}-{self._sequence}",
                sandbox=True,
            )
        except Exception as exc:
            raise AgentExecSandboxError("agent-exec sandbox request failed") from exc
        return SandboxRunResult(
            exit_code=result.returncode,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            truncated=result.output_truncated,
        )


def _broker_socket_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise AgentExecSandboxError("sandbox broker socket must be a safe relative path")
    if path.parts[0] != ".sandbox-tmp" or path.suffix != ".sock":
        raise AgentExecSandboxError("sandbox broker socket must be below .sandbox-tmp")
    return str(path)


def _guest_program(code: str, broker_socket_relative_path: str) -> str:
    """Return a self-contained interpreter program with one AF_UNIX capability."""
    encoded = repr(code)
    socket_path = repr(broker_socket_relative_path)
    return f'''import json as _json
import socket as _socket
import struct as _struct

_BROKER_SOCKET = {socket_path}
_SOURCE = {encoded}

def _recv_exact(_sock, _count):
    _chunks = []
    while _count:
        _chunk = _sock.recv(_count)
        if not _chunk:
            raise RuntimeError("sandbox broker disconnected")
        _chunks.append(_chunk)
        _count -= len(_chunk)
    return b"".join(_chunks)

def mcp_call(tool, args):
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise TypeError("mcp_call requires (tool: str, args: dict)")
    _payload = _json.dumps({{"type": "tool_call", "tool": tool, "args": args}}, separators=(",", ":")).encode("utf-8")
    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as _sock:
        _sock.connect(_BROKER_SOCKET)
        _sock.sendall(_struct.pack("!I", len(_payload)) + _payload)
        _size = _struct.unpack("!I", _recv_exact(_sock, 4))[0]
        if _size > 1048576:
            raise RuntimeError("sandbox broker response exceeds limit")
        _response = _json.loads(_recv_exact(_sock, _size).decode("utf-8"))
    if _response.get("ok") is not True:
        raise RuntimeError("sandbox broker rejected tool call")
    return _response["result"]

exec(compile(_SOURCE, "<code_sandbox>", "exec"), {{"__name__": "__main__", "mcp_call": mcp_call}})
'''
