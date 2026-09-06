"""Small, bounded framing protocol for the Agent execution sidecar.

This module deliberately has no subprocess knowledge.  It accepts only a
single JSON object in a length-prefixed frame, so an untrusted Agent cannot
smuggle inherited file descriptors, Python objects, or unlimited input through
the controller-to-runtime boundary.
"""

from __future__ import annotations

import base64
import json
import re
import socket
from dataclasses import dataclass
from typing import Any, Mapping


MAX_FRAME_BYTES = 1_048_576
MAX_ARGV_ITEMS = 128
MAX_ARG_BYTES = 8_192
MAX_ENV_ITEMS = 64
MAX_ENV_VALUE_BYTES = 16_384
MAX_ENV_BYTES = 65_536
MAX_STDIN_BYTES = 262_144
MAX_TIMEOUT_SECONDS = 7_200
MAX_NATIVE_OUTPUT_BYTES = 16 * 1024 * 1024
ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class ProtocolError(ValueError):
    """A peer sent a malformed or over-budget protocol message."""


@dataclass(frozen=True)
class StartRequest:
    request_id: str
    argv: tuple[str, ...]
    env: dict[str, str]
    stdin: bytes
    cwd: str
    timeout_seconds: int
    output_limit_bytes: int = MAX_NATIVE_OUTPUT_BYTES
    mode: str = "agent"


def encode_frame(value: Mapping[str, Any] | list[Any]) -> bytes:
    """Encode a bounded JSON payload into a network-order length-prefixed frame."""
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    return len(raw).to_bytes(4, "big") + raw


def decode_frame(data: bytes) -> dict[str, Any]:
    """Decode one complete frame; used both by the socket reader and tests."""
    if len(data) < 4:
        raise ProtocolError("incomplete frame header")
    length = int.from_bytes(data[:4], "big")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    if len(data) != 4 + length:
        raise ProtocolError("incomplete or trailing frame data")
    try:
        decoded = json.loads(data[4:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("frame must contain a JSON object")
    return decoded


def recv_frame(sock: socket.socket, *, buffer: bytearray | None = None) -> dict[str, Any] | None:
    """Read one bounded frame, preserving partial bytes across socket timeouts.

    ``socket.timeout`` is intentionally propagated: callers with a polling
    timeout pass the same ``buffer`` on the next invocation, so a frame split
    over two polls cannot be mistaken for a new header.  The buffer can never
    grow beyond one declared frame plus its four-byte header; the length is
    checked before the body is accumulated.  Blocking daemon call sites may
    omit it without changing their behaviour.
    """
    pending = buffer if buffer is not None else bytearray()
    while True:
        frame = _take_frame(pending)
        if frame is not _INCOMPLETE:
            return frame
        try:
            # Do not accept extra bytes for a second frame before returning the
            # first.  This keeps the caller-owned buffer bounded and makes its
            # framing state auditable.
            needed = _next_read_size(pending)
            chunk = sock.recv(needed)
        except TimeoutError:
            # ``pending`` belongs to the caller when polling, so no bytes are
            # lost here.  A daemon with a blocking socket never takes this path.
            raise
        if not chunk:
            if not pending:
                return None
            raise ProtocolError("peer disconnected mid-frame")
        pending.extend(chunk)


_INCOMPLETE = object()


def _take_frame(pending: bytearray) -> dict[str, Any] | None | object:
    if len(pending) < 4:
        return _INCOMPLETE
    length = int.from_bytes(pending[:4], "big")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    frame_size = 4 + length
    if len(pending) < frame_size:
        return _INCOMPLETE
    raw = bytes(pending[:frame_size])
    del pending[:frame_size]
    return decode_frame(raw)


def _next_read_size(pending: bytearray) -> int:
    if len(pending) < 4:
        return 4 - len(pending)
    length = int.from_bytes(pending[:4], "big")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    remaining = 4 + length - len(pending)
    if remaining <= 0:
        # _take_frame consumes complete frames before a read is attempted.
        raise ProtocolError("invalid buffered frame state")
    return remaining


def send_frame(sock: socket.socket, value: Mapping[str, Any]) -> None:
    sock.sendall(encode_frame(value))


def parse_start_request(value: Mapping[str, Any]) -> StartRequest:
    """Validate the only process-launch request accepted by the daemon."""
    if value.get("type") != "start":
        raise ProtocolError("first message must be start")
    request_id = _bounded_string(value.get("request_id", ""), "request_id", 128)
    argv_raw = value.get("argv")
    if not isinstance(argv_raw, list) or not argv_raw or len(argv_raw) > MAX_ARGV_ITEMS:
        raise ProtocolError("argv must be a non-empty bounded list")
    argv = tuple(_bounded_string(item, "argv item", MAX_ARG_BYTES) for item in argv_raw)
    env_raw = value.get("env")
    if not isinstance(env_raw, dict) or len(env_raw) > MAX_ENV_ITEMS:
        raise ProtocolError("env must be a bounded object")
    env: dict[str, str] = {}
    env_size = 0
    for key, item in env_raw.items():
        if not isinstance(key, str) or not ENV_KEY.fullmatch(key):
            raise ProtocolError("invalid environment key")
        encoded = _bounded_string(item, "environment value", MAX_ENV_VALUE_BYTES)
        env[key] = encoded
        env_size += len(key.encode("utf-8")) + len(encoded.encode("utf-8"))
    if env_size > MAX_ENV_BYTES:
        raise ProtocolError("environment exceeds maximum size")
    stdin_raw = value.get("stdin", "")
    if not isinstance(stdin_raw, str):
        raise ProtocolError("stdin must be base64 text")
    try:
        stdin = base64.b64decode(stdin_raw, validate=True)
    except ValueError as exc:
        raise ProtocolError("stdin is not valid base64") from exc
    if len(stdin) > MAX_STDIN_BYTES:
        raise ProtocolError("stdin exceeds maximum size")
    cwd = _bounded_string(value.get("cwd"), "cwd", 512)
    if cwd != "." and (cwd.startswith("/") or "\\" in cwd or any(part in {"", ".", ".."} for part in cwd.split("/"))):
        raise ProtocolError("cwd must be a non-empty relative path below the Trial root")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ProtocolError("timeout_seconds is outside allowed bounds")
    output_limit = value.get("output_limit_bytes", MAX_NATIVE_OUTPUT_BYTES)
    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or not 1 <= output_limit <= MAX_NATIVE_OUTPUT_BYTES
    ):
        raise ProtocolError("output_limit_bytes is outside allowed bounds")
    mode = value.get("mode", "agent")
    if mode not in {"agent", "sandbox"}:
        raise ProtocolError("mode must be agent or sandbox")
    return StartRequest(request_id, argv, env, stdin, cwd, timeout, output_limit, mode)


def stream_frame(stream: str, data: bytes) -> dict[str, Any]:
    if stream not in {"stdout", "stderr"}:
        raise ProtocolError("invalid output stream")
    return {"type": "stream", "stream": stream, "data": base64.b64encode(data).decode("ascii")}


def _recv_exact(sock: socket.socket, count: int, *, allow_eof: bool = False) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            if allow_eof and not chunks:
                return None
            raise ProtocolError("peer disconnected mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _bounded_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise ProtocolError(f"{field} exceeds maximum size")
    if "\x00" in value:
        raise ProtocolError(f"{field} contains NUL")
    return value
