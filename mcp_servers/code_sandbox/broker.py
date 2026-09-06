"""Narrow Unix IPC broker used by sandboxed Python, never direct MCP secrets."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stage2_service.platform_ledger import PlatformLedger

from harness.agent_exec.protocol import ProtocolError, recv_frame, send_frame


MAX_BROKER_ARGUMENT_BYTES = 65_536


class BrokerError(RuntimeError):
    """Sandbox code made an invalid broker request."""


ToolInvoker = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class SandboxBrokerConfig:
    socket_path: Path
    guest_uid: int
    guest_gid: int
    trial_id: str
    code_sha256: str
    allowed_tools: frozenset[str]


class SandboxBroker:
    """Serve allow-listed `{tool,args}` calls and record code-linked evidence.

    The injected ``invoker`` is control-plane-owned.  Its implementation must
    call the already configured MCP service endpoint; hence PolicyGate remains
    the final authorizer and the sandbox process never receives endpoint URLs
    or bearer tokens.
    """

    def __init__(self, config: SandboxBrokerConfig, *, invoker: ToolInvoker, ledger: PlatformLedger) -> None:
        self.config = config
        self.invoker = invoker
        self.ledger = ledger
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self.config.socket_path.exists() or self.config.socket_path.is_symlink():
            raise BrokerError("broker socket already exists")
        parent = self.config.socket_path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise BrokerError("sandbox broker parent must be pre-created and private")
        mode = parent.stat().st_mode & 0o777
        if mode & 0o007:
            raise BrokerError("sandbox broker parent must not be accessible to other users")
        if parent.stat().st_gid != self.config.guest_gid:
            raise BrokerError("sandbox broker parent has the wrong sandbox group")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.config.socket_path))
        try:
            # Controller owns the socket; its supplemental sandbox group grants
            # exactly the sandbox UID's primary group connect permission.
            os.chown(self.config.socket_path, -1, self.config.guest_gid)
            os.chmod(self.config.socket_path, 0o660)
        except OSError as exc:
            listener.close()
            self.config.socket_path.unlink(missing_ok=True)
            raise BrokerError("controller lacks required sandbox socket group") from exc
        listener.listen(8)
        self._listener = listener
        self._thread = threading.Thread(target=self._loop, name="sandbox-broker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self.config.socket_path.exists():
            self.config.socket_path.unlink()

    def _loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            try:
                _assert_guest_peer(connection, self.config.guest_uid)
                payload = recv_frame(connection)
                if payload is None:
                    return
                tool, args = _parse_call(payload, self.config.allowed_tools)
                response = dict(self.invoker(tool, args))
                self.ledger.append(
                    trial_id=self.config.trial_id,
                    event_type="SANDBOX_TOOL_CALL",
                    occurred_at=_utc_now(),
                    payload={"code_sha256": self.config.code_sha256, "tool": tool, "allowed": True},
                )
                send_frame(connection, {"ok": True, "result": response})
            except (BrokerError, ProtocolError, OSError, ValueError) as exc:
                self.ledger.append(
                    trial_id=self.config.trial_id,
                    event_type="SANDBOX_TOOL_CALL",
                    occurred_at=_utc_now(),
                    payload={"code_sha256": self.config.code_sha256, "tool": None, "allowed": False, "error_type": type(exc).__name__},
                )
                try:
                    send_frame(connection, {"ok": False, "error": "broker request rejected"})
                except OSError:
                    pass


def _parse_call(payload: Mapping[str, Any], allowed: frozenset[str]) -> tuple[str, dict[str, Any]]:
    if payload.get("type") != "tool_call":
        raise BrokerError("broker accepts only tool_call")
    tool = payload.get("tool")
    args = payload.get("args")
    if not isinstance(tool, str) or tool not in allowed:
        raise BrokerError("tool is not authorized for this sandbox Trial")
    if not isinstance(args, dict):
        raise BrokerError("tool args must be an object")
    raw = json.dumps(args, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_BROKER_ARGUMENT_BYTES:
        raise BrokerError("tool args exceed broker budget")
    return tool, dict(args)


def _assert_guest_peer(connection: socket.socket, expected_uid: int) -> None:
    if os.uname().sysname.lower() != "linux":
        raise BrokerError("sandbox broker peer validation requires Linux SO_PEERCRED")
    import struct

    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    if uid != expected_uid:
        raise BrokerError("sandbox broker peer is not the configured guest UID")


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
