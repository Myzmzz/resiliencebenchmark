"""Canonical Stage-2 Harness event contracts and adapter helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import Field

from stage2_service.contracts import ContractModel, HarnessKind
from stage2_service.mcp_tool_catalog import ToolIdentity, resolve_tool_identity


ToolResultStatus: TypeAlias = Literal[
    "completed", "failed", "denied", "channel_error"
]
FeedbackChannel: TypeAlias = Literal["resume", "in_band_mcp"]
CodeExecution: TypeAlias = Literal["none", "platform_sandbox"]
ExecutionModel: TypeAlias = Literal["stream", "post_hoc", "controller_driven"]


class ToolCall(ContractModel):
    call_id: str
    tool: str
    arguments: dict[str, Any]
    # What the agent actually wrote, and how the platform arrived at ``tool``.
    # Keeping both means a client-side "tool does not exist" is still evidence
    # the platform holds rather than something only the agent ever saw (O19).
    raw_tool: str | None = None
    tool_resolution: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolResult(ContractModel):
    call_id: str
    status: ToolResultStatus
    payload: dict[str, Any]
    raw_ref: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentMessage(ContractModel):
    text: str
    structured: dict[str, Any] | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Question(ContractModel):
    question_id: str
    version: int
    request_kind: str
    recommendation: dict[str, Any]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Checkpoint(ContractModel):
    values: dict[str, Any]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


CanonicalEvent: TypeAlias = ToolCall | ToolResult | AgentMessage | Question | Checkpoint


class HarnessAdapterError(ValueError):
    pass


class HarnessCapability(ContractModel):
    kind: HarnessKind
    execution_model: ExecutionModel
    streams_tool_results: bool
    post_hoc_trace: bool
    supports_resume: bool
    supports_mid_turn_feedback: bool
    feedback_channels: tuple[FeedbackChannel, ...] = ()
    code_execution: CodeExecution = "none"
    probe: dict[str, Any] = Field(default_factory=dict)
    qualification_passed: bool = False


class HarnessAdapter(Protocol):
    kind: HarnessKind
    session_id: str | None

    def capability(self) -> HarnessCapability: ...

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]: ...

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]: ...

    def open_calls(self) -> list[ToolCall]: ...


PERMISSION_ERROR_CODES = {
    "access_denied",
    "auth_required",
    "authentication_required",
    "forbidden",
    "invalid_token",
    "mcp_auth_required",
    "permission_denied",
    "rbac_denied",
    "token_expired",
    "token_revoked",
    "unauthorized",
}
CHANNEL_ERROR_CODES = {
    "channel_unavailable",
    "connection_closed",
    "connection_error",
    "connection_refused",
    "connection_reset",
    "connection_timeout",
    "gateway_timeout",
    "mcp_server_unavailable",
    "mcp_transport_error",
    "request_timeout",
    "service_unavailable",
    "timeout",
    "transport_error",
    "transport_timeout",
    "transport_unavailable",
    "upstream_unavailable",
}

# Authorization wording that marks any structured tool error (``error.message``
# or the ``message`` of an ``ok: false`` result) as a denial, whoever reports
# it.  The MCP server audit classifies authoritative results with this rule too
# (mcp_servers/runtime_audit.py), so the list is kept exactly as it was.
STRUCTURED_ERROR_AUTHORIZATION_MARKERS: tuple[str, ...] = (
    "permission denied",
    "authentication required",
    "auth required",
    "401 unauthorized",
    "403 forbidden",
    "token revoked",
)

# Authorization failures reported by a Harness's own MCP client for a call that
# never reached the tool, e.g. after D1 rotated the Trial's chaos_control bearer
# token or D3/D4 rotated the k8s_ro/telemetry_ro/source_ro tokens.  The three
# compared clients word the same HTTP 401 differently (qwen3.8-max, 2026-09-11):
#   codex        payload {"error": {"message": "tool call error: tool call
#                failed for `chaos_control/chaos_create_experiment` ... Transport
#                send error: Transport [rmcp::transport::worker::WorkerTransport
#                <...>] ..."}}, with the 401 further on in the message;
#   claude-code  is_error text 'MCP server "chaos_control" requires
#                re-authorization (token expired)';
#   deepseek     is_error text 'Error: Streamable HTTP error: Error POSTing to
#                endpoint: {"error": "invalid_token", "error_description":
#                "Authentication required"}', the 401 body of the MCP SDK bearer
#                middleware (its 403 body carries "insufficient_scope").
# Every pattern names authorization itself: an HTTP 401/403 status, an RFC 6750
# bearer-token error code, or a demand to (re-)authenticate.  Plan rejections,
# validation errors, timeouts, refused connections and 5xx responses therefore
# stay "failed", and word boundaries keep platform codes such as
# SELECTOR_TARGET_FORBIDDEN or BASELINE_TOKEN_EXPIRED from matching.  The
# patterns apply only to results the client itself flagged as failed: platform
# rejections arrive as normal results and keep the structured rules above.
CLIENT_AUTHORIZATION_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bhttp(?:/\d(?:\.\d)?)?\s+40[13]\b",  # "HTTP 401", "HTTP/1.1 403"
        r"\b(?:http[ _])?status(?:[ _]code)?[\"']?\s*[:=]?\s*40[13]\b",  # "status: 401"
        r"\bunauthori[sz]ed\b",  # HTTP 401 reason phrase
        r"\bforbidden\b",  # HTTP 403 reason phrase
        r"\binvalid_token\b",  # RFC 6750 error code sent with 401
        r"\binsufficient_scope\b",  # RFC 6750 error code sent with 403
        r"\bauth(?:entication)? required\b",
        r"\bre-?authori[sz](?:ation|e)\b",  # "requires re-authorization"
        r"\btoken (?:has )?(?:been )?(?:expired|revoked)\b",
        r"\bpermission denied\b",
    )
)
# Native statuses with which a client reports that the call itself failed.
CLIENT_FAILURE_STATUSES = frozenset({"failed", "failure", "error"})


class BaseHarnessAdapter:
    kind: HarnessKind

    def __init__(self) -> None:
        self._open_calls: dict[str, ToolCall] = {}
        self._all_calls: dict[str, ToolCall] = {}
        self._completed_call_ids: set[str] = set()
        self._seen_call_ids: set[str] = set()
        self._line_index = 0
        self.session_id: str | None = None

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]:
        raise NotImplementedError

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        return []

    def open_calls(self) -> list[ToolCall]:
        return list(self._open_calls.values())

    def _record_call(self, call: ToolCall) -> ToolCall | None:
        existing = self._all_calls.get(call.call_id)
        if existing is not None:
            if existing.tool != call.tool or existing.arguments != call.arguments:
                raise HarnessAdapterError(
                    f"conflicting ToolCall for call_id {call.call_id!r}"
                )
            if call.call_id in self._completed_call_ids:
                return None
            self._open_calls.setdefault(call.call_id, existing)
            return None
        self._all_calls[call.call_id] = call
        if call.call_id in self._completed_call_ids:
            return None
        self._open_calls[call.call_id] = call
        if call.call_id in self._seen_call_ids:
            return None
        self._seen_call_ids.add(call.call_id)
        return call

    def _record_result(self, result: ToolResult) -> ToolResult:
        self._open_calls.pop(result.call_id, None)
        self._completed_call_ids.add(result.call_id)
        return result

    def _raw_ref(self, prefix: str = "stream") -> str:
        self._line_index += 1
        return f"{prefix}:{self._line_index}"


def decode_json_line(line: bytes) -> dict[str, Any] | str | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(value, dict):
        return value
    return None


def parse_occurred_at(value: Mapping[str, Any]) -> datetime:
    for key in ("occurred_at", "timestamp", "created_at", "ts"):
        raw = value.get(key)
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 100_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value, UTC)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    return datetime.now(UTC)


def tool_identity_fields(tool: Any, server: Any = None) -> dict[str, Any] | None:
    """Return the ``ToolCall`` identity fields, or ``None`` for an empty name."""
    identity = resolve_tool_identity(tool, server)
    if not identity.canonical:
        return None
    return {
        "tool": identity.canonical,
        "raw_tool": identity.raw,
        "tool_resolution": identity.resolution,
    }


def normalize_tool_name(tool: Any, server: Any = None) -> str | None:
    """Canonicalize any spelling an agent used, including an unprefixed one.

    A bare name that exactly one MCP server owns is completed rather than left
    unqualified; see ``stage2_service.mcp_tool_catalog`` for why (O19).
    """
    identity = resolve_tool_identity(tool, server)
    if not identity.raw:
        return None
    return identity.canonical


def extract_call_id(value: Mapping[str, Any]) -> str | None:
    for key in (
        "call_id",
        "callId",
        "tool_call_id",
        "toolCallId",
        "tool_use_id",
        "toolUseId",
        "id",
    ):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def extract_arguments(value: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "input", "args", "parameters"):
        raw = value.get(key)
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def extract_text_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def extract_agent_message(text: str, occurred_at: datetime) -> AgentMessage:
    return AgentMessage(
        text=text,
        structured=extract_text_json(text),
        occurred_at=occurred_at,
    )


def payload_from_result(value: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("structured_content", "structuredContent"):
        raw = value.get(key)
        if isinstance(raw, dict):
            return dict(raw)
    content = value.get("content")
    if isinstance(content, str):
        parsed = extract_text_json(content)
        return parsed if parsed is not None else {"text": content}
    if isinstance(content, list):
        text_blocks: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_blocks.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                text_blocks.append(block["text"])
        for text in text_blocks:
            parsed = extract_text_json(text)
            if parsed is not None:
                return parsed
        if text_blocks:
            return {"text": "\n".join(text_blocks)}
    for key in ("result", "output", "error"):
        raw = value.get(key)
        if isinstance(raw, dict):
            return {"error": dict(raw)} if key == "error" else dict(raw)
        if isinstance(raw, str) and raw.strip():
            parsed = extract_text_json(raw)
            if parsed is not None:
                return {"error": parsed} if key == "error" else parsed
            return {key: raw}
    if any(key in value for key in ("ok", "findings")):
        return dict(value)
    return {}


def status_from_payload(
    *,
    native_status: Any = None,
    payload: Mapping[str, Any] | None = None,
    is_error: bool | None = None,
) -> ToolResultStatus:
    """Classify one tool result as completed, failed, denied or channel_error.

    Structured evidence (HTTP status, error code, error message) is read first,
    for Harness clients and the MCP server audit alike.  Free text is read only
    when the client itself flagged the call as failed, and only for the
    authorization failures in ``CLIENT_AUTHORIZATION_FAILURE_PATTERNS``.
    """
    payload = payload or {}
    error = payload.get("error")
    error_code = ""
    error_message = ""
    if isinstance(error, Mapping):
        error_code = normalize_error_code(error.get("code"))
        error_message = str(error.get("message") or "")
        http_status = _http_status(error)
    elif isinstance(error, str):
        error_message = error
        http_status = None
    else:
        http_status = _http_status(payload)
    if payload.get("ok") is False:
        error_message = " ".join(
            part
            for part in (error_message, str(payload.get("message") or ""))
            if part
        )
    status = str(native_status or "").strip().lower()
    text = f"{error_code} {error_message} {status}".lower()
    if http_status in {502, 503, 504}:
        return "channel_error"
    if http_status in {401, 403}:
        return "denied"
    if error_code in CHANNEL_ERROR_CODES or "channel unavailable" in text:
        return "channel_error"
    if error_code in PERMISSION_ERROR_CODES:
        return "denied"
    if any(marker in text for marker in STRUCTURED_ERROR_AUTHORIZATION_MARKERS):
        return "denied"
    client_reported_failure = is_error is True or status in CLIENT_FAILURE_STATUSES
    if client_reported_failure and _names_authorization_failure(
        " ".join((error_message, *_unstructured_texts(payload)))
    ):
        # The client refused the call before it reached the tool; claude-code
        # and deepseek report that only as free text (see the patterns above).
        return "denied"
    if payload.get("ok") is True and is_error is not True:
        return "completed"
    if status in {"completed", "success", "succeeded", "accepted"} and is_error is not True:
        return "completed"
    return "failed"


def normalize_error_code(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _http_status(value: Mapping[str, Any]) -> int | None:
    for key in ("http_status", "status_code", "status"):
        try:
            return int(value.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _unstructured_texts(payload: Mapping[str, Any]) -> list[str]:
    """Return the free text ``payload_from_result`` keeps for non-JSON output."""
    return [
        value
        for key in ("text", "output", "result")
        if isinstance(value := payload.get(key), str)
    ]


def _names_authorization_failure(text: str) -> bool:
    """Tell whether client error text names an authorization failure."""
    return any(
        pattern.search(text) for pattern in CLIENT_AUTHORIZATION_FAILURE_PATTERNS
    )


def session_id_from_mapping(value: Mapping[str, Any]) -> str | None:
    candidates = (
        value.get("session_id"),
        value.get("sessionId"),
        value.get("thread_id"),
        value.get("threadId"),
        value.get("conversation_id"),
        value.get("conversationId"),
    )
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("session", "conversation"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = session_id_from_mapping(nested)
            if found:
                return found
    return None


def stable_call_id(*parts: Any) -> str:
    text = "\x1f".join(str(part) for part in parts if part is not None)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return slug[:120] or "call"
