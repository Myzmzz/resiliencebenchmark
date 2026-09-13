"""Black-box HTTP/SSE transport for a BladeAI 0.7.0 server.

Peer of :mod:`harness.agent_exec` for a harness that is served rather than
spawned.  See :mod:`harness.bladeai_http.client` for the driver and
:mod:`harness.bladeai_http.protocol` for the published wire contract.
"""

from __future__ import annotations

from .client import (
    BladeAIHttpClient,
    BladeAIHttpError,
    EventLog,
    TurnExecution,
    bladeai_http_resume_argv_builder,
    bladeai_http_session_id_provider,
    bladeai_http_streaming_runner,
    bladeai_http_turn_executor,
)
from .protocol import (
    APPROVAL_WORDS,
    CONFIRM_ACTIONS,
    EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    BladeAIProtocolError,
    SSEFrame,
    event_type,
    frame_to_event,
    is_terminal,
    iter_sse_frames,
)

__all__ = [
    "APPROVAL_WORDS",
    "CONFIRM_ACTIONS",
    "EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "BladeAIHttpClient",
    "BladeAIHttpError",
    "BladeAIProtocolError",
    "EventLog",
    "SSEFrame",
    "TurnExecution",
    "bladeai_http_resume_argv_builder",
    "bladeai_http_session_id_provider",
    "bladeai_http_streaming_runner",
    "bladeai_http_turn_executor",
    "event_type",
    "frame_to_event",
    "is_terminal",
    "iter_sse_frames",
]
