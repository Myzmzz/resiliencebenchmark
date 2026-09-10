"""Controller-side client for :mod:`harness.agent_exec.server`.

``agent_exec_streaming_runner`` deliberately mirrors the existing streaming
runner's turn-executor arguments.  ``HarnessSession`` owns resume and feedback
state; this module only moves one already-decided native CLI turn to a sidecar.
"""

from __future__ import annotations

import base64
import os
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .protocol import MAX_NATIVE_OUTPUT_BYTES, ProtocolError, recv_frame, send_frame


class AgentExecClientError(RuntimeError):
    """The agent runtime rejected a request or failed authentication."""


# The execution deadline already includes the historical five-second transport
# allowance.  Once cancellation is sent, do not wait indefinitely for a
# broken daemon: close the socket so its disconnect cleanup path is engaged.
CANCEL_TERMINAL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False


LineObserver = Callable[[bytes], object]
CancelRequested = Callable[[], bool]


class AgentExecClient:
    """Authenticated Unix-socket client owned by the control-plane container."""

    def __init__(self, socket_path: Path | str, *, expected_server_uid: int):
        self.socket_path = Path(socket_path)
        self.expected_server_uid = expected_server_uid

    def run(
        self,
        argv: Sequence[str],
        stdin: bytes,
        env: Mapping[str, str],
        timeout_seconds: int,
        stdout_line_observer: LineObserver | None = None,
        cancel_requested: CancelRequested | None = None,
        *,
        cwd: str = ".",
        request_id: str = "agent-exec",
        sandbox: bool = False,
        output_limit_bytes: int = MAX_NATIVE_OUTPUT_BYTES,
    ) -> ExecutionResult:
        """Run one bounded request and return streamed bytes and terminal state."""
        if not 1 <= output_limit_bytes <= MAX_NATIVE_OUTPUT_BYTES:
            raise AgentExecClientError("native output limit is outside allowed bounds")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.socket_path))
            _assert_linux_peer(sock, self.expected_server_uid, "agent runtime")
            send_frame(sock, {
                "type": "start", "request_id": request_id, "argv": list(argv),
                "env": dict(env), "stdin": base64.b64encode(stdin).decode("ascii"),
                "cwd": cwd, "timeout_seconds": timeout_seconds,
                "output_limit_bytes": output_limit_bytes,
                "mode": "sandbox" if sandbox else "agent",
            })
            sock.settimeout(0.2)
            stdout = bytearray()
            stderr = bytearray()
            stdout_pending = bytearray()
            deadline = time.monotonic() + timeout_seconds + 5
            cancel_sent = False
            cancel_terminal_deadline: float | None = None
            frame_buffer = bytearray()

            def cancel_for_output_limit() -> None:
                nonlocal cancel_sent, cancel_terminal_deadline
                if cancel_sent:
                    return
                now = time.monotonic()
                send_frame(sock, {"type": "cancel", "reason": "native_output_limit"})
                cancel_sent = True
                cancel_terminal_deadline = now + CANCEL_TERMINAL_GRACE_SECONDS

            output_truncated = False
            while True:
                now = time.monotonic()
                if now > deadline and not cancel_sent:
                    send_frame(sock, {"type": "cancel", "reason": "client_deadline"})
                    cancel_sent = True
                    cancel_terminal_deadline = now + CANCEL_TERMINAL_GRACE_SECONDS
                if cancel_requested is not None and cancel_requested() and not cancel_sent:
                    send_frame(sock, {"type": "cancel", "reason": "controller_cancelled"})
                    cancel_sent = True
                    cancel_terminal_deadline = now + CANCEL_TERMINAL_GRACE_SECONDS
                if cancel_terminal_deadline is not None and now > cancel_terminal_deadline:
                    raise AgentExecClientError(
                        "agent runtime did not send terminal event after cancellation; "
                        "daemon cleanup is unconfirmed"
                    )
                try:
                    frame = recv_frame(sock, buffer=frame_buffer)
                except TimeoutError:
                    continue
                if frame is None:
                    raise AgentExecClientError("agent runtime disconnected before terminal event")
                kind = frame.get("type")
                if kind == "rejected":
                    raise AgentExecClientError(f"agent runtime rejected request: {frame.get('reason', 'unknown')}")
                if kind == "stream":
                    raw = _decode_output(frame)
                    remaining = max(0, output_limit_bytes - len(stdout) - len(stderr))
                    accepted = raw[:remaining]
                    if len(accepted) < len(raw) or len(stdout) + len(stderr) + len(accepted) >= output_limit_bytes:
                        output_truncated = True
                    if frame.get("stream") == "stdout":
                        stdout.extend(accepted)
                        if stdout_line_observer is not None:
                            stdout_pending.extend(accepted)
                            while True:
                                newline = stdout_pending.find(b"\n")
                                if newline < 0:
                                    break
                                stdout_line_observer(bytes(stdout_pending[: newline + 1]))
                                del stdout_pending[: newline + 1]
                    elif frame.get("stream") == "stderr":
                        stderr.extend(accepted)
                    else:
                        raise AgentExecClientError("agent runtime sent invalid stream")
                    if output_truncated:
                        cancel_for_output_limit()
                    continue
                if kind == "exit":
                    if stdout_pending and stdout_line_observer is not None:
                        stdout_line_observer(bytes(stdout_pending))
                    return ExecutionResult(
                        returncode=_int_field(frame, "returncode"), stdout=bytes(stdout), stderr=bytes(stderr),
                        timed_out=bool(frame.get("timed_out", False)), cancelled=bool(frame.get("cancelled", False)),
                        output_truncated=output_truncated or bool(frame.get("output_truncated", False)),
                    )
                raise AgentExecClientError("agent runtime sent unknown event")
        except (OSError, ProtocolError) as exc:
            raise AgentExecClientError(str(exc)) from exc
        finally:
            sock.close()


def agent_exec_streaming_runner(
    client: AgentExecClient,
    argv: Sequence[str],
    stdin: bytes,
    env: Mapping[str, str],
    timeout_seconds: int,
    stdout_line_observer: LineObserver,
    cancel_requested: CancelRequested | None = None,
    **unsupported: object,
) -> ExecutionResult:
    """Run one turn remotely and reject session-level kwargs that cannot apply.

    Callers needing native resume must inject :func:`agent_exec_turn_executor`
    into ``HarnessSession``.  Silently accepting resume or feedback kwargs here
    would execute only the first turn and corrupt evaluation evidence.
    """
    if unsupported:
        raise TypeError(
            "agent_exec_streaming_runner is a single-turn transport; "
            "inject agent_exec_turn_executor into HarnessSession for: "
            + ", ".join(sorted(unsupported))
        )
    return client.run(argv, stdin, env, timeout_seconds, stdout_line_observer, cancel_requested)


def agent_exec_turn_executor(
    client: AgentExecClient,
    *,
    cwd: str = ".",
    request_id_prefix: str = "agent-session",
) -> Callable[..., ExecutionResult]:
    """Return a per-turn transport suitable for ``HarnessSession.turn_executor``.

    Both the initial argv and every resume argv pass through this same closure;
    session feedback, retry budget, transcript events, and session-id lifetime
    remain in ``HarnessSession`` rather than being reimplemented remotely.
    """
    turn = 0

    def execute(
        argv: Sequence[str],
        stdin: bytes,
        env: Mapping[str, str],
        timeout_seconds: int,
        observe: LineObserver,
        cancel: CancelRequested,
        *,
        output_limit_bytes: int = MAX_NATIVE_OUTPUT_BYTES,
    ) -> ExecutionResult:
        nonlocal turn
        turn += 1
        return client.run(
            argv, stdin, env, timeout_seconds, observe, cancel,
            cwd=cwd, request_id=f"{request_id_prefix}-{turn}", output_limit_bytes=output_limit_bytes,
        )

    return execute


def _assert_linux_peer(sock: socket.socket, expected_uid: int, label: str) -> None:
    if sys_platform() != "linux":
        raise AgentExecClientError(f"{label} peer authentication requires Linux SO_PEERCRED")
    import struct

    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    if uid != expected_uid:
        raise AgentExecClientError(f"{label} uid mismatch")


def sys_platform() -> str:
    # Small seam for platform-specific tests; production never substitutes it.
    return os.uname().sysname.lower()


def _decode_output(frame: Mapping[str, object]) -> bytes:
    data = frame.get("data")
    if not isinstance(data, str):
        raise AgentExecClientError("stream data is missing")
    try:
        return base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise AgentExecClientError("stream data is invalid base64") from exc


def _int_field(frame: Mapping[str, object], key: str) -> int:
    value = frame.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentExecClientError(f"exit event has invalid {key}")
    return value
