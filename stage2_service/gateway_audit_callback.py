"""Metadata-only request receipts from the actual LiteLLM 1.92 proxy ingress.

This standalone module is mounted into the gateway, not imported by Controller
code. A receipt proves arrival at the gateway; it does not claim model success.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import urlparse
from typing import Any, Mapping

from litellm.integrations.custom_logger import CustomLogger

MAX_AUDIT_BYTES = 1_000_000
MAX_USAGE_BYTES = 4_000_000
MAX_ROW_BYTES = 4096
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_HARNESSES = frozenset({"codex", "claude-code", "deepseek-harness", "bladeai"})
_WRITE_LOCK = threading.Lock()


def _receipt(data: Mapping[str, Any]) -> dict[str, Any] | None:
    request = data.get("proxy_server_request") or {}
    if not isinstance(request, Mapping):
        raise ValueError("invalid gateway request metadata")
    raw_headers = request.get("headers") or {}
    if not isinstance(raw_headers, Mapping):
        raise ValueError("invalid gateway request headers")
    headers = {str(key).lower(): value for key, value in raw_headers.items()}
    trial_id = headers.get("x-resbench-trial-id")
    if trial_id is None:
        # The responder and model preflight are not evaluated Agent requests.
        return None
    harness = headers.get("x-resbench-harness")
    alias = headers.get("x-resbench-model-alias")
    request_id = headers.get("x-resbench-request-id")
    if (not isinstance(trial_id, str) or not _SAFE_COMPONENT.fullmatch(trial_id)
            or harness not in _HARNESSES
            or not isinstance(alias, str) or not alias or len(alias) > 160
            or data.get("model") != alias
            or not isinstance(request_id, str) or not _SAFE_COMPONENT.fullmatch(request_id)):
        raise ValueError("invalid gateway audit identity")

    config = Path(os.environ["STAGE2_LITELLM_CONFIG_FILE"])
    descriptor = os.open(config, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("invalid gateway configuration file")
        raw = handle.read(MAX_AUDIT_BYTES + 1)
    if len(raw) > MAX_AUDIT_BYTES:
        raise ValueError("gateway configuration is too large")
    return {
        "schema_version": "stage2-gateway-request.v1",
        "trial_id": trial_id,
        "harness": harness,
        "model_alias": alias,
        "request_id": request_id,
        "gateway_config_sha256": hashlib.sha256(raw).hexdigest(),
        "outcome": "received",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "path": urlparse(str(request.get("url") or "")).path,
        "status": None,
    }


def _append_receipt(row: Mapping[str, Any]) -> None:
    root = Path(os.environ["RESBENCH_GATEWAY_AUDIT_DIR"])
    if root.is_symlink():
        raise ValueError("gateway audit directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("gateway audit directory is not private")
        raw = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_ROW_BYTES:
            raise ValueError("gateway audit record is too large")
        with _WRITE_LOCK:
            descriptor = os.open(
                str(row["trial_id"]) + ".jsonl",
                os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600, dir_fd=root_descriptor,
            )
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022
                        or info.st_size + len(raw) > MAX_AUDIT_BYTES):
                    raise ValueError("gateway audit file is unsafe or full")
                if handle.write(raw) != len(raw):
                    raise OSError("incomplete gateway audit record")
                os.fsync(handle.fileno())
    finally:
        os.close(root_descriptor)


def _post_headers(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    """LiteLLM 1.92 places proxy request headers below litellm_params."""
    params = kwargs.get("litellm_params")
    if isinstance(params, Mapping):
        request = params.get("proxy_server_request")
        if isinstance(request, Mapping) and isinstance(request.get("headers"), Mapping):
            return request["headers"]
    request = kwargs.get("proxy_server_request")
    if isinstance(request, Mapping) and isinstance(request.get("headers"), Mapping):
        return request["headers"]
    return {}


def _usage_payload(response_obj: Any) -> tuple[dict[str, Any] | None, str, str | None]:
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, Mapping):
        usage = response_obj.get("usage")
    if usage is None:
        return None, "unavailable", "upstream_usage_missing"
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif not isinstance(usage, Mapping):
        try:
            usage = dict(usage)
        except (TypeError, ValueError):
            return None, "unavailable", "usage_not_mapping"
    usage = dict(usage)
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    total_tokens = usage.get("total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None, "unavailable", "upstream_usage_missing"
    # LiteLLM 1.92 normalizes a non-stream response with no upstream usage to
    # an all-zero usage object.  Zero is not a measured token count here.
    numeric = (input_tokens, output_tokens, total_tokens)
    if all(value == 0 for value in numeric if isinstance(value, (int, float))) and any(
        value is not None for value in numeric
    ):
        return usage, "unavailable", "upstream_usage_missing"
    return usage, "measured", None


def _append_usage_row(row: Mapping[str, Any]) -> None:
    root = Path(os.environ["RESBENCH_GATEWAY_AUDIT_DIR"])
    if root.is_symlink():
        raise ValueError("gateway audit directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("gateway audit directory is not private")
        raw = (json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_ROW_BYTES:
            raise ValueError("gateway usage record is too large")
        with _WRITE_LOCK:
            descriptor = os.open(
                str(row["trial_id"]) + ".usage.jsonl",
                os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_descriptor,
            )
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022
                        or info.st_size + len(raw) > MAX_USAGE_BYTES):
                    raise ValueError("gateway usage file is unsafe or full")
                if handle.write(raw) != len(raw):
                    raise OSError("incomplete gateway usage record")
                os.fsync(handle.fileno())
    finally:
        os.close(root_descriptor)


class GatewayAuditLogger(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Record a proxy receipt without changing the request or logging it."""
        del user_api_key_dict, cache, call_type
        row = await asyncio.to_thread(_receipt, data)
        if row is not None:
            await asyncio.to_thread(_append_receipt, row)
        return None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        headers = {str(key).lower(): value for key, value in _post_headers(kwargs).items()}
        trial_id = headers.get("x-resbench-trial-id")
        if not trial_id:
            return None
        usage, availability, reason = _usage_payload(response_obj)
        usage = usage or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        stream = bool(kwargs.get("stream"))
        if stream and availability == "measured" and headers.get("x-resbench-usage-source") != "upstream":
            availability = "estimated"
        raw_cost = kwargs.get("response_cost")
        cost_usd = raw_cost if isinstance(raw_cost, (int, float)) and raw_cost > 0 else None
        cost_reason = None if cost_usd is not None else "model_price_unavailable"
        row = {
            "schema_version": "stage2-gateway-usage.v1",
            "trial_id": trial_id,
            "request_id": headers.get("x-resbench-request-id"),
            "harness": headers.get("x-resbench-harness"),
            "model_alias": headers.get("x-resbench-model-alias"),
            "source": "agent",
            "phase": headers.get("x-resbench-phase") or "unknown",
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "cached_input_tokens": cached,
            "total_tokens": usage.get("total_tokens"),
            "cost_usd": cost_usd,
            "cost_availability": "measured" if cost_usd is not None else "unavailable",
            "cost_unavailable_reason": cost_reason,
            "started_at": str(start_time),
            "ended_at": str(end_time),
            "duration_ms": int((end_time - start_time).total_seconds() * 1000) if start_time and end_time else None,
            "is_retry": bool(kwargs.get("is_retry")),
            "availability": availability,
            "unavailable_reason": reason,
        }
        await asyncio.to_thread(_append_usage_row, row)
        return None

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        del response_obj
        headers = {str(key).lower(): value for key, value in _post_headers(kwargs).items()}
        trial_id = headers.get("x-resbench-trial-id")
        if not trial_id:
            return None
        row = {
            "schema_version": "stage2-gateway-usage.v1",
            "trial_id": trial_id,
            "request_id": headers.get("x-resbench-request-id"),
            "harness": headers.get("x-resbench-harness"),
            "model_alias": headers.get("x-resbench-model-alias"),
            "source": "agent",
            "phase": headers.get("x-resbench-phase") or "unknown",
            "started_at": str(start_time),
            "ended_at": str(end_time),
            "duration_ms": int((end_time - start_time).total_seconds() * 1000) if start_time and end_time else None,
            "is_retry": bool(kwargs.get("is_retry")),
            "availability": "unavailable",
            "unavailable_reason": "model_call_failed",
            "error": str(kwargs.get("exception") or "")[:240],
        }
        await asyncio.to_thread(_append_usage_row, row)
        return None


logger_instance = GatewayAuditLogger()
