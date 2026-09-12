"""Wire contract for the BladeAI 0.7.0 public HTTP/SSE interface.

This module holds only what the server itself publishes: endpoint paths, the
documented event vocabulary, and the SSE framing rules.  Nothing here imports
or reflects on BladeAI's internals -- the black-box integration depends on the
public interface and event fields alone.

Contract source: ``docs/design/bladeai-blackbox-impl-handoff-20260912.md`` s2,
verified against the 2026-09-11 live run (18 cases).
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any


DEFAULT_API_PREFIX = "/api/v1"


class BladeAIProtocolError(ValueError):
    """The server sent a frame that does not satisfy the published contract."""


def sessions_path(prefix: str = DEFAULT_API_PREFIX) -> str:
    return f"{prefix}/sessions"


def turn_path(session_id: str, prefix: str = DEFAULT_API_PREFIX) -> str:
    return f"{prefix}/sessions/{session_id}/turn"


def interrupt_path(session_id: str, prefix: str = DEFAULT_API_PREFIX) -> str:
    return f"{prefix}/sessions/{session_id}/interrupt"


def cancel_path(session_id: str, prefix: str = DEFAULT_API_PREFIX) -> str:
    return f"{prefix}/sessions/{session_id}/cancel"


def state_path(session_id: str, prefix: str = DEFAULT_API_PREFIX) -> str:
    return f"{prefix}/sessions/{session_id}/state"


def confirm_path(task_id: str, prefix: str = DEFAULT_API_PREFIX) -> str:
    """Execution-gate channel.  Keyed by ``task_id``, not by session id."""
    return f"{prefix}/confirm/{task_id}"


# The event vocabulary BladeAI 0.7.0 emits on the turn stream.  Unknown types
# are forwarded verbatim rather than dropped: the platform must never silently
# lose evidence because upstream added an event kind.
EVENT_TYPES: frozenset[str] = frozenset({
    "node_start", "llm_start", "thinking", "token", "tool_start", "tool_end",
    "node_end", "context_size", "usage", "confirm", "node_message", "result",
    "done", "error",
})

# A turn stream ends on one of these.  ``done`` is end-of-turn, NOT end-of-task:
# BladeAI's clarifying questions are frequently plain text followed by ``done``
# (finding F10), so a caller that treats ``done`` as completion will stall.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"done", "error"})

# ``normalise_answer`` in the server maps exactly these four spellings to
# "approved" and everything else -- including an approval with an explanatory
# sentence attached -- to "rejected" (finding F1).  Any answer the platform
# sends on the intent gate must be one of these verbatim.
APPROVAL_WORDS: tuple[str, ...] = ("approved", "yes", "y", "ok")

# Execution-gate actions accepted by ``POST /api/v1/confirm/{task_id}``.
CONFIRM_ACTIONS: tuple[str, ...] = ("approve", "reject")


@dataclass(frozen=True)
class SSEFrame:
    """One dispatched server-sent event."""

    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None


@dataclass
class _FrameBuffer:
    data_lines: list[str] = field(default_factory=list)
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None
    saw_field: bool = False

    def reset(self) -> None:
        self.data_lines.clear()
        self.event = None
        self.event_id = None
        self.retry = None
        self.saw_field = False

    def dispatch(self) -> SSEFrame | None:
        # Per the SSE specification a block carrying no ``data`` sets the
        # reconnection state only and dispatches no event.
        if not self.data_lines:
            self.reset()
            return None
        frame = SSEFrame(
            data="\n".join(self.data_lines),
            event=self.event,
            event_id=self.event_id,
            retry=self.retry,
        )
        self.reset()
        return frame


def iter_sse_frames(chunks: Iterable[bytes]) -> Iterator[SSEFrame]:
    """Decode a byte stream into dispatched SSE frames.

    Implements the framing rules the specification requires and that a
    hand-rolled ``split(b"\\n\\n")`` gets wrong: CR, LF and CRLF all terminate a
    line; a single optional space after the colon is stripped; repeated
    ``data:`` fields are joined with a newline; lines beginning with a colon are
    comments (upstream keep-alives) and dispatch nothing.
    """
    buffer = _FrameBuffer()
    pending = ""
    # Decoding must carry state across chunks: the socket splits wherever it
    # likes, and a multi-byte character straddling that split would otherwise
    # decode to U+FFFD on both sides.  BladeAI streams user prompts and tool
    # output verbatim, so non-ASCII text is routine, not an edge case.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for chunk in chunks:
        if not chunk:
            continue
        pending += decoder.decode(chunk)
        # A trailing CR may be the first half of a CRLF still in flight; hold it
        # back so the pair is not counted as two line terminators.
        if pending.endswith("\r"):
            body, pending = pending[:-1], "\r"
        else:
            body, pending = pending, ""
        for line, terminated in _iter_lines(body):
            if not terminated:
                pending = line + pending
                break
            frame = _consume_line(buffer, line)
            if frame is not None:
                yield frame
    pending += decoder.decode(b"", final=True)
    for line, terminated in _iter_lines(pending):
        if not terminated:
            # An unterminated tail is an incomplete frame.  Dropping it is
            # correct: dispatching half an event would fabricate evidence.
            break
        frame = _consume_line(buffer, line)
        if frame is not None:
            yield frame


def _iter_lines(text: str) -> Iterator[tuple[str, bool]]:
    start = 0
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if char == "\n":
            yield text[start:index], True
            index += 1
            start = index
            continue
        if char == "\r":
            yield text[start:index], True
            index += 2 if text[index + 1 : index + 2] == "\n" else 1
            start = index
            continue
        index += 1
    if start < length:
        yield text[start:], False


def _consume_line(buffer: _FrameBuffer, line: str) -> SSEFrame | None:
    if line == "":
        return buffer.dispatch() if buffer.saw_field else None
    if line.startswith(":"):
        return None
    buffer.saw_field = True
    name, separator, value = line.partition(":")
    if not separator:
        name, value = line, ""
    elif value.startswith(" "):
        value = value[1:]
    if name == "data":
        buffer.data_lines.append(value)
    elif name == "event":
        buffer.event = value
    elif name == "id":
        buffer.event_id = value
    elif name == "retry":
        try:
            buffer.retry = int(value)
        except ValueError:
            pass
    return None


def frame_to_event(frame: SSEFrame) -> dict[str, Any]:
    """Decode one frame's ``data`` payload into the event object.

    Two servers ship the event kind two different ways: inside the JSON body as
    ``type``, or only as the SSE ``event:`` field.  When the body omits it the
    frame's name is copied in, so downstream sees one consistent ``type`` field.
    The original name is never discarded -- the driver records it alongside the
    event -- so this normalisation loses nothing.
    """
    payload = frame.data.strip()
    if not payload:
        raise BladeAIProtocolError("server-sent event carried an empty data payload")
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BladeAIProtocolError(f"server-sent event data is not JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise BladeAIProtocolError("server-sent event data is not a JSON object")
    if "type" not in event and frame.event:
        event["type"] = frame.event
    return event


def event_type(event: dict[str, Any]) -> str | None:
    value = event.get("type")
    return value if isinstance(value, str) else None


def is_terminal(event: dict[str, Any]) -> bool:
    return event_type(event) in TERMINAL_EVENT_TYPES
