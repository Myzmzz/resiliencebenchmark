"""Fail-closed realtime audit bridge for controlled MCP tool boundaries.

The bridge deliberately lives below every Harness adapter.  An MCP server asks
the Controller for permission *before* it invokes a tool, and reports the
result immediately afterwards.  It is not a transcript importer: the
Controller is online for both messages and creates the authoritative call id.

The Unix socket is a Controller-owned capability.  On Linux peer credentials
are checked with ``SO_PEERCRED``; other platforms retain the protected socket
directory protocol for local tests but are not a Linux identity qualification.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from stage2_service.harness_adapters.base import ToolCall, ToolResult


# Tool results can contain real trace/log/metric windows.  The bridge carries
# them losslessly up to this explicit bound; MCP services must use their normal
# pagination rather than silently truncating evidence above it.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 2.0
_PEERCRED = getattr(socket, "SO_PEERCRED", None)


class AuditBridgeError(RuntimeError):
    """The audit bridge cannot prove that the Controller accepted an event."""


class AuditBridgeDenied(AuditBridgeError):
    """The Controller denied the requested tool invocation."""


@dataclass(frozen=True)
class AuditBridgeConfig:
    socket_path: Path
    trial_id: str
    authority: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    controller_uid: int | None = None
    client_uid: int | None = None

    def __post_init__(self) -> None:
        if not self.trial_id.strip() or not self.authority.strip():
            raise ValueError("trial_id and authority must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name, value in (("controller_uid", self.controller_uid), ("client_uid", self.client_uid)):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative uid")


@dataclass(frozen=True)
class AuditDecision:
    allowed: bool
    call_id: str
    reason: str | None = None
    payload: dict[str, Any] | None = None


ControllerCallback = Callable[[ToolCall | ToolResult, str], Mapping[str, Any] | None]


@dataclass
class _CallState:
    owner: tuple[str, str]
    result_fingerprint: str | None = None
    acknowledged: bool = False
    delivering: bool = False


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuditBridgeError("message has no occurred_at")
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _safe_mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuditBridgeError(f"{field} must be an object")
    return dict(value)


def _send_json(connection: socket.socket, value: Mapping[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise AuditBridgeError("audit bridge response exceeds size limit")
    connection.sendall(raw)


def _recv_json(connection: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while True:
        chunk = connection.recv(min(64 * 1024, MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            raise AuditBridgeError("audit bridge closed before acknowledgement")
        data.extend(chunk)
        if len(data) > MAX_MESSAGE_BYTES:
            raise AuditBridgeError("audit bridge message exceeds size limit")
        newline = chunk.find(b"\n")
        if newline >= 0:
            absolute_newline = len(data) - len(chunk) + newline
            if absolute_newline != len(data) - 1:
                raise AuditBridgeError("audit bridge accepts exactly one message per connection")
            try:
                value = json.loads(bytes(data[:absolute_newline]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditBridgeError("invalid audit bridge JSON") from exc
            return _safe_mapping(value, "message")


class AuditBridgeClient:
    """Client embedded in an MCP server process, with fixed trial identity."""

    def __init__(self, config: AuditBridgeConfig) -> None:
        self.config = config
        self.platform_identity_verified = False

    def before_call(
        self, server: str, tool: str, arguments: Mapping[str, Any], *, occurred_at: datetime | None = None,
    ) -> AuditDecision:
        response = self._request({
            "kind": "before_call", "trial_id": self.config.trial_id,
            "authority": self.config.authority, "server": _required(server, "server"),
            "tool": _required(tool, "tool"), "arguments": dict(arguments),
            "occurred_at": _timestamp(occurred_at),
        }, phase="before")
        return AuditDecision(
            allowed=bool(response.get("allowed")), call_id=_required(response.get("call_id"), "call_id"),
            reason=_optional_text(response.get("reason")), payload=dict(response.get("payload") or {}),
        )

    def after_call(
        self, call_id: str, payload: Mapping[str, Any], status: Literal["completed", "failed", "denied", "channel_error"], *,
        occurred_at: datetime | None = None,
    ) -> None:
        response = self._request({
            "kind": "after_call", "trial_id": self.config.trial_id,
            "authority": self.config.authority, "call_id": _required(call_id, "call_id"),
            "payload": dict(payload), "status": status, "occurred_at": _timestamp(occurred_at),
        }, phase="after")
        if response.get("ack") is not True:
            raise AuditBridgeError(self._failure_message(
                "after", _optional_text(response.get("reason")) or "result acknowledgement rejected",
            ))

    def invoke(
        self, server: str, tool: str, arguments: Mapping[str, Any], operation: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Execute ``operation`` only after a Controller acknowledgement.

        MCP server handlers should use this small wrapper around every controlled
        tool.  A bridge timeout, malformed answer, or explicit rejection never
        calls ``operation``.
        """
        decision = self.before_call(server, tool, arguments)
        if not decision.allowed:
            raise AuditBridgeDenied(decision.reason or "Controller denied tool call")
        try:
            payload = dict(operation())
        except Exception as exc:
            self.after_call(decision.call_id, {"error": str(exc)}, "failed")
            raise
        self.after_call(decision.call_id, payload, "completed")
        return payload

    def _request(self, message: Mapping[str, Any], *, phase: Literal["before", "after"]) -> dict[str, Any]:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) + 1 > MAX_MESSAGE_BYTES:
            raise AuditBridgeError(self._failure_message(phase, "audit bridge message exceeds size limit"))
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.config.timeout_seconds)
            self._verify_endpoint()
            connection.connect(str(self.config.socket_path))
            self._verify_server_peer(connection)
            _send_json(connection, message)
            response = _recv_json(connection)
        except (OSError, TimeoutError) as exc:
            raise AuditBridgeError(self._failure_message(phase, "audit bridge unavailable")) from exc
        except AuditBridgeError as exc:
            raise AuditBridgeError(self._failure_message(phase, str(exc))) from exc
        finally:
            connection.close()
        if response.get("ok") is not True:
            raise AuditBridgeError(self._failure_message(
                phase, _optional_text(response.get("reason")) or "audit bridge rejected request",
            ))
        return response

    def _verify_endpoint(self) -> None:
        """Verify filesystem ownership before connecting to the Controller."""
        expected_uid = self.config.controller_uid if self.config.controller_uid is not None else os.getuid()
        directory = self.config.socket_path.parent
        try:
            directory_stat = directory.stat()
            endpoint_stat = self.config.socket_path.lstat()
        except OSError as exc:
            raise AuditBridgeError("audit bridge endpoint is unavailable") from exc
        if (directory_stat.st_uid != expected_uid or directory_stat.st_mode & 0o077 or
                endpoint_stat.st_uid != expected_uid or endpoint_stat.st_mode & 0o077 or
                not stat.S_ISSOCK(endpoint_stat.st_mode)):
            raise AuditBridgeError("audit bridge endpoint ownership or mode is unsafe")

    def _verify_server_peer(self, connection: socket.socket) -> None:
        if _PEERCRED is None:
            # The protected pathname check above remains useful for macOS local
            # tests, but it is intentionally not reported as Linux peer proof.
            self.platform_identity_verified = False
            return
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, _PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except OSError as exc:
            raise AuditBridgeError("could not verify Controller peer identity") from exc
        expected_uid = self.config.controller_uid if self.config.controller_uid is not None else os.getuid()
        if uid != expected_uid:
            raise AuditBridgeError("unexpected Controller peer identity")
        self.platform_identity_verified = True

    @staticmethod
    def _failure_message(phase: Literal["before", "after"], reason: str) -> str:
        if phase == "before":
            return f"{reason}; controlled tool was not executed"
        return f"{reason}; tool may have executed and result audit is unacknowledged"


class AuditBridgeListener:
    """Controller-owned protected Unix-socket listener.

    ``callback`` is invoked synchronously while the MCP operation is pending;
    returning ``{"allowed": false}`` rejects a before-call.  D5-style policy
    changes must therefore only register an asynchronous availability window in
    this callback, never sleep until the window has recovered.
    """

    def __init__(self, config: AuditBridgeConfig, callback: ControllerCallback, *, expected_uid: int | None = None) -> None:
        self.config = config
        self.callback = callback
        self.expected_uid = (
            config.client_uid if expected_uid is None and config.client_uid is not None
            else os.getuid() if expected_uid is None else expected_uid
        )
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._calls: dict[str, _CallState] = {}
        self._calls_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._bound_inode: tuple[int, int] | None = None
        self._bound_path: Path | None = None
        self.platform_identity_verified = _PEERCRED is not None and os.name == "posix" and "linux" in os.uname().sysname.lower()

    def start(self) -> "AuditBridgeListener":
        with self._lifecycle_lock:
            if self._server is not None:
                raise AuditBridgeError("audit bridge listener is already running")
            path = self.config.socket_path.resolve()
            directory = path.parent
            if directory.exists():
                directory_stat = directory.stat()
                if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
                    raise AuditBridgeError("audit bridge directory must be Controller-owned mode 0700")
            else:
                directory.mkdir(mode=0o700, parents=True)
                os.chmod(directory, 0o700)
            if os.path.lexists(path):
                if path.is_socket():
                    raise AuditBridgeError("refusing to replace an existing audit bridge socket")
                raise AuditBridgeError("refusing to replace non-socket audit bridge path")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(path))
                os.chmod(path, 0o600)
                inode = path.lstat()
                server.listen(32)
                server.settimeout(0.1)
            except Exception:
                server.close()
                raise
            self._server = server
            self._bound_inode = (inode.st_dev, inode.st_ino)
            self._bound_path = path
            self._stopped.clear()
            self._thread = threading.Thread(target=self._serve, args=(server,), daemon=True, name="mcp-audit-bridge")
            self._thread.start()
            return self

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stopped.set()
            server, thread, inode, path = self._server, self._thread, self._bound_inode, self._bound_path
            self._server = None
            self._thread = None
            self._bound_inode = None
            self._bound_path = None
        if server is not None:
            server.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if inode is not None and path is not None:
            try:
                actual = path.lstat()
            except FileNotFoundError:
                return
            if (actual.st_dev, actual.st_ino) == inode:
                path.unlink()

    def __enter__(self) -> "AuditBridgeListener":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _serve(self, server: socket.socket) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(self.config.timeout_seconds)
            try:
                if not self._authorized(connection):
                    _send_json(connection, {"ok": False, "reason": "unauthorized audit bridge peer"})
                    return
                message = _recv_json(connection)
                response = self._dispatch(message)
            except (AuditBridgeError, ValueError, KeyError) as exc:
                response = {"ok": False, "reason": str(exc)}
            except Exception:
                response = {"ok": False, "reason": "Controller audit callback failed"}
            try:
                _send_json(connection, response)
            except OSError:
                # A timed-out MCP server may have already closed its side.  Its
                # before-call still did not execute anything without an ACK.
                return

    def _authorized(self, connection: socket.socket) -> bool:
        if _PEERCRED is None:
            # macOS has no portable peer credential API for AF_UNIX.  Directory
            # mode 0700 plus socket mode 0600 is intentionally the test-only
            # local protocol; callers must not claim Linux identity evidence.
            return True
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, _PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid == self.expected_uid
        except OSError:
            return False

    def _dispatch(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if message.get("trial_id") != self.config.trial_id or message.get("authority") != self.config.authority:
            raise AuditBridgeError("cross-trial or wrong-authority audit message")
        kind = message.get("kind")
        if kind == "before_call":
            call_id = uuid4().hex
            call = ToolCall(
                call_id=call_id,
                tool=f"{_required(message.get('server'), 'server')}.{_required(message.get('tool'), 'tool')}",
                arguments=_safe_mapping(message.get("arguments"), "arguments"),
                occurred_at=_parse_timestamp(message.get("occurred_at")),
            )
            decision = dict(self.callback(call, "mcp_server") or {})
            # A missing decision is not an acknowledgement.  In particular a
            # Controller observer which only records events cannot accidentally
            # authorise a mutation.
            allowed = decision.get("allowed") is True
            if allowed:
                with self._calls_lock:
                    self._calls[call_id] = _CallState((self.config.trial_id, self.config.authority))
            else:
                # A Controller denial is an authoritative terminal outcome,
                # not an unpaired ToolCall.  It is emitted by the listener,
                # never trusted from Agent stdout; the operation has not run.
                raw_payload = decision.get("payload")
                payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {
                    "ok": False,
                    "error": {
                        "code": "CONTROLLER_CALL_DENIED",
                        "message": _optional_text(decision.get("reason")) or "调用被控制器拒绝。",
                    },
                }
                payload["controller_call_id"] = call_id
                denied = ToolResult(
                    call_id=call_id, status="denied", payload=payload,
                    occurred_at=call.occurred_at,
                )
                # If recording this result fails, surface an audit failure to
                # the MCP server rather than falsely claiming a denial was
                # durably recorded.
                self.callback(denied, "mcp_server")
            return {"ok": True, "allowed": allowed, "call_id": call_id,
                    "reason": _optional_text(decision.get("reason")),
                    "payload": (payload if not allowed else dict(decision.get("payload") or {}))}
        if kind == "after_call":
            call_id = _required(message.get("call_id"), "call_id")
            fingerprint = _result_fingerprint(message)
            result = ToolResult(
                call_id=call_id, status=_required(message.get("status"), "status"),
                payload=_safe_mapping(message.get("payload"), "payload"),
                occurred_at=_parse_timestamp(message.get("occurred_at")),
            )
            with self._calls_lock:
                state = self._calls.get(call_id)
                if state is None or state.owner != (self.config.trial_id, self.config.authority):
                    raise AuditBridgeError("unknown or cross-trial audit call_id")
                if state.result_fingerprint is not None and state.result_fingerprint != fingerprint:
                    raise AuditBridgeError("conflicting result replay for audit call_id")
                if state.acknowledged:
                    return {"ok": True, "ack": True, "idempotent": True}
                if state.delivering:
                    raise AuditBridgeError("result delivery is already in progress")
                state.result_fingerprint = fingerprint
                state.delivering = True
            try:
                self.callback(result, "mcp_server")
            except Exception:
                # Preserve the call and its fingerprint.  A same-result retry
                # can reconcile a failed Controller callback; a different
                # result cannot overwrite the original operation evidence.
                with self._calls_lock:
                    state.delivering = False
                raise
            with self._calls_lock:
                state.acknowledged = True
                state.delivering = False
            return {"ok": True, "ack": True}
        raise AuditBridgeError("unknown audit bridge message kind")


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditBridgeError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _result_fingerprint(message: Mapping[str, Any]) -> str:
    """Keep duplicate after-call ACKs idempotent without trusting timestamps."""
    try:
        return json.dumps(
            {"status": message.get("status"), "payload": message.get("payload")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AuditBridgeError("result payload is not JSON serializable") from exc
