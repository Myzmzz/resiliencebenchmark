"""DeepSeek Harness post-hoc session adapter."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from stage2_service.contracts import HarnessKind

from .base import (
    BaseHarnessAdapter,
    CanonicalEvent,
    HarnessAdapterError,
    HarnessCapability,
    ToolCall,
    ToolResult,
    extract_agent_message,
    extract_arguments,
    extract_call_id,
    normalize_tool_name,
    parse_occurred_at,
    payload_from_result,
    session_id_from_mapping,
    status_from_payload,
)


MAX_COMPRESSED_SESSION_BYTES = 128 * 1024 * 1024


class DeepSeekTraceError(HarnessAdapterError):
    pass


class DeepSeekHarnessAdapter(BaseHarnessAdapter):
    kind = HarnessKind.DEEPSEEK

    def capability(self) -> HarnessCapability:
        return HarnessCapability(
            kind=self.kind,
            execution_model="post_hoc",
            streams_tool_results=False,
            post_hoc_trace=True,
            supports_resume=False,
            supports_mid_turn_feedback=False,
            feedback_channels=(),
            code_execution="none",
        )

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]:
        from .base import decode_json_line

        raw_text = line.decode("utf-8", errors="replace").strip()
        value = decode_json_line(line)
        if value is None:
            return []
        occurred_at = parse_occurred_at({})
        if isinstance(value, str):
            return [extract_agent_message(value, occurred_at)]
        event_type = str(value.get("type") or "")
        if event_type in {"tool/call", "tool/result", "tool-call-chunks"}:
            return []
        data = value.get("data") if isinstance(value.get("data"), Mapping) else {}
        if (session_id := session_id_from_mapping(value)) is not None:
            self.session_id = session_id
        if event_type == "session" and isinstance(value.get("id"), str):
            self.session_id = str(value["id"])
            return []
        if event_type == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
            texts = _message_texts(message)
            return [extract_agent_message("\n".join(texts), parse_occurred_at({"time": value.get("time")}))] if texts else []
        message_text = value.get("text") or value.get("message")
        if isinstance(message_text, str) and message_text.strip():
            return [extract_agent_message(message_text, parse_occurred_at(value))]
        if _looks_like_final_json(value):
            return [extract_agent_message(raw_text, parse_occurred_at(value))]
        return []

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        dsh_copies = sorted(artifact_dir.rglob("dsh-session-*.jsonl.zstd"))
        sources = dsh_copies or sorted(artifact_dir.rglob("session.jsonl.zstd"))
        for source in sources:
            events.extend(self._events_from_zstd(source, artifact_dir))
        return events

    def _events_from_zstd(
        self, source: Path, artifact_dir: Path
    ) -> list[CanonicalEvent]:
        output: list[CanonicalEvent] = []
        for line_number, raw in enumerate(iter_zstd_jsonl_lines(source), start=1):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DeepSeekTraceError(
                    f"{source} line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(value, dict):
                continue
            if (session_id := session_id_from_mapping(value)) is not None:
                self.session_id = session_id
            event = self._event_from_record(
                value,
                raw_ref=_relative_ref(source, artifact_dir, line_number),
            )
            if event is None:
                continue
            if isinstance(event, ToolCall):
                recorded = self._record_call(event)
                if recorded is not None:
                    output.append(recorded)
            elif isinstance(event, ToolResult):
                output.append(self._record_result(event))
            else:
                output.append(event)
        return output

    def _event_from_record(
        self, value: Mapping[str, Any], raw_ref: str
    ) -> CanonicalEvent | None:
        event_type = str(value.get("type") or "")
        data = value.get("data") if isinstance(value.get("data"), Mapping) else {}
        occurred_at = parse_occurred_at({"time": value.get("time")})
        if event_type == "session" and isinstance(value.get("id"), str):
            self.session_id = str(value["id"])
            return None
        if event_type == "tool/call":
            call_id = str(data.get("callId") or data.get("call_id") or "").strip()
            tool = normalize_tool_name(data.get("name"))
            if not call_id or not tool:
                raise DeepSeekTraceError(f"{raw_ref} tool/call missing callId or name")
            return ToolCall(
                call_id=call_id,
                tool=tool,
                arguments=extract_arguments(data),
                occurred_at=occurred_at,
            )
        if event_type == "tool/result":
            return self._tool_result_from_record(data, raw_ref, occurred_at)
        if event_type == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
            texts = _message_texts(message)
            if texts:
                return extract_agent_message("\n".join(texts), occurred_at)
        return None

    def _tool_result_from_record(
        self, data: Mapping[str, Any], raw_ref: str, occurred_at
    ) -> ToolResult:
        message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
        for block in _message_blocks(message):
            if block.get("type") not in {"tool-result", "tool_result"}:
                continue
            call_id = (
                block.get("toolCallId")
                or block.get("tool_call_id")
                or data.get("callId")
                or data.get("call_id")
            )
            if not isinstance(call_id, str) or not call_id.strip():
                raise DeepSeekTraceError(f"{raw_ref} tool/result missing toolCallId")
            payload = payload_from_result(block)
            return ToolResult(
                call_id=call_id.strip(),
                status=status_from_payload(
                    native_status="failed" if block.get("isError") or block.get("is_error") else "completed",
                    payload=payload,
                    is_error=bool(block.get("isError") or block.get("is_error")),
                ),
                payload=payload,
                raw_ref=raw_ref,
                occurred_at=occurred_at,
            )
        call_id = extract_call_id(data)
        if not call_id:
            raise DeepSeekTraceError(f"{raw_ref} tool/result missing toolCallId")
        payload = payload_from_result(data)
        return ToolResult(
            call_id=call_id,
            status=status_from_payload(native_status="completed", payload=payload),
            payload=payload,
            raw_ref=raw_ref,
            occurred_at=occurred_at,
        )


def iter_zstd_jsonl_lines(path: Path) -> Iterable[str]:
    try:
        import zstandard as zstd
    except ModuleNotFoundError as exc:
        raise DeepSeekTraceError(
            "DeepSeek session replay requires the Python zstandard package"
        ) from exc
    size = path.stat().st_size
    if size > MAX_COMPRESSED_SESSION_BYTES:
        raise DeepSeekTraceError(
            f"{path} exceeds {MAX_COMPRESSED_SESSION_BYTES} byte compressed session limit"
        )
    with path.open("rb") as handle:
        raw_stream = handle.read(MAX_COMPRESSED_SESSION_BYTES + 1)
    if len(raw_stream) > MAX_COMPRESSED_SESSION_BYTES:
        raise DeepSeekTraceError(
            f"{path} exceeds {MAX_COMPRESSED_SESSION_BYTES} byte compressed session limit"
        )
    _validate_complete_zstd_stream(raw_stream, zstd, path)
    dctx = zstd.ZstdDecompressor()
    buffer = b""
    try:
        reader = dctx.stream_reader(io.BytesIO(raw_stream), read_across_frames=True)
        with reader:
            while True:
                chunk = reader.read(131072)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        yield line.decode("utf-8")
    except zstd.ZstdError as exc:
        raise DeepSeekTraceError(f"{path} is not a complete zstd JSONL stream") from exc
    if buffer.strip():
        yield buffer.decode("utf-8")


def _validate_complete_zstd_stream(raw_stream: bytes, zstd: Any, path: Path) -> None:
    remaining = raw_stream
    while remaining:
        obj = zstd.ZstdDecompressor().decompressobj()
        try:
            obj.decompress(remaining)
        except zstd.ZstdError as exc:
            raise DeepSeekTraceError(
                f"{path} is not a complete zstd JSONL stream"
            ) from exc
        if not obj.eof:
            raise DeepSeekTraceError(f"{path} is not a complete zstd JSONL stream")
        next_remaining = obj.unused_data
        if len(next_remaining) == len(remaining):
            raise DeepSeekTraceError(f"{path} is not a complete zstd JSONL stream")
        remaining = next_remaining


def _message_blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _message_texts(message: Mapping[str, Any]) -> list[str]:
    output: list[str] = []
    for block in _message_blocks(message):
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            output.append(block["text"])
    return output


def _looks_like_final_json(value: Mapping[str, Any]) -> bool:
    if str(value.get("type") or "") in {"agent_message", "assistant_message", "final"}:
        return True
    return any(
        key in value
        for key in ("status", "agent_verdict", "effect_assessment", "recovery_assessment")
    )


def _relative_ref(source: Path, artifact_dir: Path, line_number: int) -> str:
    try:
        rel = source.relative_to(artifact_dir)
    except ValueError:
        rel = source
    return f"{rel}:line:{line_number}"
